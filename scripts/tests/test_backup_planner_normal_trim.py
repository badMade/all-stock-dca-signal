"""Backup planner: Normal tier only, and trim to fit instead of all-or-nothing.

Written test-first, no implementation yet (red). Operator decision 2026-10-22: "Backups should stay
Normal-only like the slots", and, asked how an over-cap batch should behave, "Normal-only, trim to fit".
This reverses two earlier decisions: tier escalation in the backup planner (option C, 2026-10-15, pinned by
``test_backup_planner_tiers.py``) and the all-or-nothing caps of ADR D4 (``docs/adr-plan-first-backup-executor.md``).

Why (2026-10-16): backup sessions placed 57 orders and $205.90 against the $180 Normal daily cap. The 12:50
backup, with $179.13 already spent that day, still placed $23.65 because the planner had escalated its tier. The
Claude slots never escalate (``docs/safeguards.md``: only the 3:30 final run does), so a backup must not either.

Contract, the same rule the slots follow at Gate 6 ("place the orders that fit, skip the rest"; ``plan_orders``
"stops at a cap boundary but keeps the orders before it"):

* The tier is always Normal: $180 daily, $900 weekly, ``MAX_ORDERS_PER_DAY`` orders. The count of 3x-or-greater
  signals never raises a cap.
* Orders are consumed in the order they are given (allocation priority). The planner keeps the longest PREFIX
  that fits the daily cap, the weekly cap, the order-count cap and the buying power, and STOPS at the first order
  that does not fit. It never shrinks an order and never back-fills a later, smaller one.
* Every order that was not kept is reported in ``skipped`` as ``{"symbol", "reason"}`` with the cap that
  stopped it (``over_daily_cap``, ``over_weekly_cap``, ``over_order_count_cap``, ``insufficient_buying_power``).
* Nothing fitting is not a failure: ``compute`` returns ``VALID_PLAN`` with no orders (the existing no-signal
  convention) and ``finalize`` returns ``PROCEED`` with an empty ``to_place``.
* Unchanged: one bad order (over the per-order cap, unknown symbol, ...) is still a defect, not a trim.

Fixtures self-calibrate: each test first plans the batch alone to learn its orders, then places an earlier
same-day (or same-week) spend so the headroom lands a known distance past a chosen prefix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_data_fixtures import DAY, market_data  # noqa: E402
from test_backup_planner_plan_first import (  # noqa: E402
    DAY as PF_DAY,
)
from test_backup_planner_plan_first import (
    _finalize_argv,
    _plan_first_plan,
    _raw,
)
from test_backup_planner_plan_first import (
    _run as _pf_run,
)

from dca_runner import MAX_ORDERS_PER_DAY  # noqa: E402
from scripts import backup_planner  # noqa: E402

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
NOW = {"0935": "2026-10-07T09:56:00-04:00", "1530": "2026-10-07T15:46:00-04:00"}
SLOTS = ["0935", "1530"]  # one stream-executor slot and the 15:30 engine: the two gate code paths
SYMBOLS = ["VT", "MSFT", "NVDA", "AAPL", "AMZN", "JPM", "GE", "CAT", "QQQ", "TSLA", "AMD", "AVGO", "INTC", "CSCO",
           "IBM", "TXN", "QCOM", "GS"]
EARLIER_SPEND_SYMBOL = "LLY"  # not in SYMBOLS, so the earlier spend never excludes a planned ticker
NORMAL_DAILY = 180.0
NORMAL_WEEKLY = 900.0
HALF_DOLLAR = 0.5  # the headroom left past the kept prefix; smaller than any single planned order


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(json.dumps(obj))
    return str(path)


def _raw_buy(symbol, amount, created):
    return {"id": f"id-{symbol}-{created}", "ref_id": None, "symbol": symbol, "side": "buy", "type": "market",
            "state": "filled", "dollar_based_amount": {"amount": f"{amount:.6f}", "currency_code": "USD"},
            "time_in_force": "gfd", "market_hours": "regular_hours", "trigger": "immediate",
            "placed_agent": "agentic", "created_at": created}


def _spent_today(amount):
    return _raw_buy(EARLIER_SPEND_SYMBOL, amount, f"{DAY}T13:40:00+00:00")


def _spent_this_week(amount):
    return _raw_buy(EARLIER_SPEND_SYMBOL, amount, "2026-10-05T15:31:00+00:00")  # the Monday of the same ISO week


def _compute(tmp_path, capsys, slot, signals, *, rows=()):
    """Run the gated compute on ``signals`` tickers all down 6%, with ``rows`` already in today's history."""
    tape = {symbol: (100.0, 94.0) for symbol in SYMBOLS[:signals]}
    pages = {"account_number": ACCOUNT, "trading_day": DAY, "pages": [{"orders": list(rows)}]}
    argv = ["compute", "--trading-day", DAY, "--slot", slot,
            "--market-data", _write(tmp_path, "market.json", market_data(tape, slot=slot)),
            "--orders-pages", _write(tmp_path, "pages.json", pages), "--now", NOW[slot], "--buying-power", "100000",
            "--deployed-last-7d-usd", "0"]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _batch(tmp_path, capsys, slot, signals):
    """The batch's own orders in plan (allocation-priority) order, planned against an empty history."""
    code, report = _compute(tmp_path, capsys, slot, signals)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    orders = report["plan"]["orders"]
    assert len(orders) == signals, "the fixture must yield one BUY per ticker"
    amounts = [o["notional_usd"] for o in orders]
    assert min(amounts) > HALF_DOLLAR, "every planned order must be larger than the headroom margin"
    return [o["symbol"] for o in orders], amounts


def _skipped(symbols, reason):
    return [{"symbol": symbol, "reason": reason} for symbol in symbols]


# ---------------------------------------------------------------------------
# compute --market-data: Normal tier, trim to fit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize("signals", [6, 11, 18])
def test_compute_trims_to_the_normal_cap_instead_of_escalating(tmp_path, capsys, slot, signals):
    """6, 11 and 18 signals used to select Elevated, High-volatility and Stress; none may raise the cap now."""
    symbols, amounts = _batch(tmp_path, capsys, slot, signals)
    kept = 3
    earlier = NORMAL_DAILY - sum(amounts[:kept]) - HALF_DOLLAR  # headroom = the first 3 orders + $0.50
    code, report = _compute(tmp_path, capsys, slot, signals, rows=[_spent_today(earlier)])
    assert (code, report["status"], report["errors"]) == (0, "VALID_PLAN", [])
    assert [o["symbol"] for o in report["plan"]["orders"]] == symbols[:kept]
    assert report["skipped"] == _skipped(symbols[kept:], "over_daily_cap")


@pytest.mark.parametrize("slot", SLOTS)
def test_compute_stops_at_the_first_order_that_does_not_fit_and_never_back_fills(tmp_path, capsys, slot):
    """Order 3 is larger than the headroom left after orders 1 and 2, order 4 is smaller and would fit it. The plan
    keeps orders 1 and 2 only: it stops at the boundary, as the slots do, rather than filling the gap behind it."""
    symbols, amounts = _batch(tmp_path, capsys, slot, 4)
    leftover = amounts[3] + 0.005
    assert amounts[2] > leftover, "the fixture needs order 3 to be larger than order 4"
    earlier = NORMAL_DAILY - (amounts[0] + amounts[1] + leftover)
    code, report = _compute(tmp_path, capsys, slot, 4, rows=[_spent_today(earlier)])
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert [o["symbol"] for o in report["plan"]["orders"]] == symbols[:2]
    assert report["skipped"] == _skipped(symbols[2:], "over_daily_cap")


@pytest.mark.parametrize("slot", SLOTS)
def test_compute_with_no_headroom_is_an_empty_plan_not_a_failure(tmp_path, capsys, slot):
    """The 2026-10-16 shape: $179.13 already spent leaves $0.87, which no planned order fits."""
    symbols, _ = _batch(tmp_path, capsys, slot, 4)
    code, report = _compute(tmp_path, capsys, slot, 4, rows=[_spent_today(179.13)])
    assert (code, report["status"], report["errors"]) == (0, "VALID_PLAN", [])
    assert report["plan"]["orders"] == []
    assert report["skipped"] == _skipped(symbols, "over_daily_cap")


@pytest.mark.parametrize("slot", SLOTS)
def test_compute_trims_to_the_normal_weekly_cap_too(tmp_path, capsys, slot):
    symbols, amounts = _batch(tmp_path, capsys, slot, 4)
    kept = 2
    code, report = _compute(tmp_path, capsys, slot, 4,
                            rows=[_spent_this_week(NORMAL_WEEKLY - sum(amounts[:kept]) - HALF_DOLLAR)])
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert [o["symbol"] for o in report["plan"]["orders"]] == symbols[:kept]
    assert report["skipped"] == _skipped(symbols[kept:], "over_weekly_cap")


@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize("signals", [4, 18])
def test_a_batch_that_fits_the_normal_caps_is_planned_in_full(tmp_path, capsys, slot, signals):
    """Regression guard (passes today for 4 signals): trimming only ever removes orders past the boundary."""
    code, report = _compute(tmp_path, capsys, slot, signals)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert len(report["plan"]["orders"]) == signals
    assert report.get("skipped", []) == []


# ---------------------------------------------------------------------------
# finalize on a plan-first plan: the placement-time check trims the same way
# ---------------------------------------------------------------------------

ITEMS = (("MSFT", 4.0), ("AAPL", 2.5), ("NVDA", 1.5))
QUOTES = {symbol: 100.0 for symbol, _ in ITEMS}
PRIOR = {symbol: 101.0 for symbol, _ in ITEMS}
SPREADS = {symbol: {"bid": 99.9, "ask": 100.1} for symbol, _ in ITEMS}


def _finalize(tmp_path, capsys, rows=(), bp="1000"):
    plan = _plan_first_plan(tmp_path, capsys, ITEMS)
    return _pf_run(capsys, _finalize_argv(tmp_path, plan, rows, quotes=QUOTES, prior=PRIOR, spreads=SPREADS, bp=bp))


def _symbols(report):
    return [o["symbol"] for o in report["to_place"]]


def test_finalize_places_the_prefix_that_fits_the_daily_cap_and_never_back_fills(tmp_path, capsys):
    """$174 spent leaves $6.00: MSFT $4.00 fits ($2.00 left), AAPL $2.50 does not, so it stops there. NVDA $1.50
    would fit the $2.00 left over, and must not be placed behind the boundary."""
    code, report, _ = _finalize(tmp_path, capsys, [_raw("VT", amount="174.00", created=f"{PF_DAY}T14:00:00+00:00")])
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert _symbols(report) == ["MSFT"]
    assert report["skipped"] == _skipped(["AAPL", "NVDA"], "over_daily_cap")


def test_finalize_places_the_prefix_that_fits_the_weekly_cap(tmp_path, capsys):
    rows = [_raw("VT", amount="894.00", created="2026-10-05T14:00:00+00:00")]  # Monday of the same ISO week
    code, report, _ = _finalize(tmp_path, capsys, rows)
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert _symbols(report) == ["MSFT"]
    assert report["skipped"] == _skipped(["AAPL", "NVDA"], "over_weekly_cap")


def test_finalize_places_the_prefix_that_fits_the_order_count_cap(tmp_path, capsys):
    rows = [_raw("VT", amount="0.01", created=f"{PF_DAY}T14:{i // 60:02d}:{i % 60:02d}+00:00")
            for i in range(MAX_ORDERS_PER_DAY - 1)]  # one attempt of headroom left
    code, report, _ = _finalize(tmp_path, capsys, rows)
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert _symbols(report) == ["MSFT"]
    assert report["skipped"] == _skipped(["AAPL", "NVDA"], "over_order_count_cap")


def test_finalize_places_the_prefix_that_fits_the_buying_power(tmp_path, capsys):
    code, report, _ = _finalize(tmp_path, capsys, bp="5.00")  # MSFT $4.00 fits, then $1.00 is left
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert _symbols(report) == ["MSFT"]
    assert report["skipped"] == _skipped(["AAPL", "NVDA"], "insufficient_buying_power")


def test_finalize_with_no_headroom_proceeds_with_nothing_to_place(tmp_path, capsys):
    """Nothing fitting is a clean empty run, not an abort: the backup places nothing and says why."""
    code, report, _ = _finalize(tmp_path, capsys, [_raw("VT", amount="179.50", created=f"{PF_DAY}T14:00:00+00:00")])
    assert (code, report["status"], report["errors"], report["to_place"]) == (0, "PROCEED", [], [])
    assert report["skipped"] == _skipped(["MSFT", "AAPL", "NVDA"], "over_daily_cap")


def test_finalize_places_everything_when_it_all_fits(tmp_path, capsys):
    """Regression guard (passes today): no trimming when the whole set fits."""
    code, report, _ = _finalize(tmp_path, capsys)
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert _symbols(report) == ["MSFT", "AAPL", "NVDA"]
    assert report["skipped"] == []
