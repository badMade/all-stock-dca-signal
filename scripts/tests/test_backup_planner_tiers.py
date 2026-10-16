"""Tests for tier escalation in the backup planner's gated ``compute --market-data``.

Written test-first, no implementation yet. Companion to ``test_backup_planner_gate.py`` (slot 1530) and
``test_backup_planner_streams.py`` (the six stream slots).

The gap (2026-10-15, slot 0935): on a broad-dip morning the Perplexity backup computed a batch above the $180
Normal-tier daily cap and, because the planner is all-or-nothing, placed nothing. ``scripts/backup_planner.py``
hardwires ``_NORMAL_TIER`` ("never escalates it"), while the primary selects the tier from the count of
3x-or-greater signals alone (``near_close_dca.select_budget_tier``; ``config/budgets.json`` ``selection_rule``).
A reconstruction of that morning had 41 to 46 signals at 3x or above, i.e. the Stress tier, whose $360 daily cap
the batch fits under.

Contract (option C, operator-approved 2026-10-15):

* The gated ``compute`` picks the budget tier from the number of BUY decisions whose multiplier is 3.0 or more,
  using the primary's own thresholds: <=5 Normal ($180), 6-10 Elevated ($240), 11-17 High-volatility ($300),
  >17 Stress ($360). The count comes from the generator's decisions, never from the planned spend.
* Caps stay all-or-nothing: a batch over the SELECTED tier's daily cap still plans nothing.
* The Stress tier's weekly ceiling needs a verified rollover reserve, which the planner has no input for. With no
  reserve the effective weekly cap stays at the Normal $900 (``config/budgets.json``: "absence of rollover means
  a $900 effective budget"), so escalating to Stress never lifts the weekly cap.

Fixtures self-calibrate: each test first plans the batch alone to learn its total ``T``, then places an earlier
same-day spend so the day total lands a known distance past a cap. That keeps the boundary exact without pinning
the ladder's dollar amounts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_data_fixtures import DAY, market_data  # noqa: E402

from scripts import backup_planner  # noqa: E402

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
NOW = {"0935": "2026-10-07T09:56:00-04:00", "1530": "2026-10-07T15:46:00-04:00"}
SLOTS = ["0935", "1530"]  # one stream-executor slot and the 15:30 engine: the two gate code paths
# Liquid universe tickers, none of which is the earlier-spend symbol below. A 6% drop is well past the 3x tier for all.
SYMBOLS = ["VT", "MSFT", "NVDA", "AAPL", "AMZN", "JPM", "GE", "CAT", "QQQ", "TSLA", "AMD", "AVGO", "INTC", "CSCO",
           "IBM", "TXN", "QCOM", "GS"]
EARLIER_SPEND_SYMBOL = "LLY"  # not in SYMBOLS, so the earlier spend never excludes a planned ticker
MARGIN_USD = 2.0

# (signal count, that count's tier daily cap, the next tier DOWN's daily cap)
ESCALATING = [(6, 240.0, 180.0), (10, 240.0, 180.0), (11, 300.0, 240.0), (17, 300.0, 240.0), (18, 360.0, 300.0)]
ALL_COUNTS = [(5, 180.0), (6, 240.0), (10, 240.0), (11, 300.0), (17, 300.0), (18, 360.0)]


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(json.dumps(obj))
    return str(path)


def _raw_buy(symbol, amount, created):
    return {"id": f"id-{symbol}-{created}", "ref_id": None, "symbol": symbol, "side": "buy", "type": "market",
            "state": "filled", "dollar_based_amount": {"amount": f"{amount:.6f}", "currency_code": "USD"},
            "time_in_force": "gfd", "market_hours": "regular_hours", "trigger": "immediate",
            "placed_agent": "agentic", "created_at": created}


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


def _batch_total(tmp_path, capsys, slot, signals):
    """The batch's own notional ``T``, planned against an empty history (always under every tier's cap)."""
    code, report = _compute(tmp_path, capsys, slot, signals)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert len(report["plan"]["orders"]) == signals, "the fixture must yield one BUY per ticker"
    assert all(o["multiplier"] >= 3.0 for o in report["plan"]["orders"]), "every fixture buy must be a 3x-or-greater signal"
    return sum(o["notional_usd"] for o in report["plan"]["orders"])


def _spent_today(amount):
    return _raw_buy(EARLIER_SPEND_SYMBOL, amount, f"{DAY}T13:40:00+00:00")


@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize(("signals", "tier_cap", "lower_cap"), ESCALATING)
def test_a_batch_over_the_lower_tiers_cap_is_planned_under_the_escalated_tier(tmp_path, capsys, slot, signals, tier_cap, lower_cap):
    """The 2026-10-15 failure: day total past the Normal cap, but within the cap the primary would have used."""
    total = _batch_total(tmp_path, capsys, slot, signals)
    earlier = lower_cap - total + MARGIN_USD  # day total = lower_cap + 2, so the lower tier would refuse it
    assert lower_cap < earlier + total <= tier_cap
    code, report = _compute(tmp_path, capsys, slot, signals, rows=[_spent_today(earlier)])
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert len(report["plan"]["orders"]) == signals


@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize(("signals", "tier_cap"), ALL_COUNTS)
def test_a_batch_over_the_selected_tiers_cap_still_plans_nothing(tmp_path, capsys, slot, signals, tier_cap):
    """Escalation raises the ceiling to the primary's, no further: the batch is still all-or-nothing (D4).

    Passes today (the planner is always Normal); it pins that the implementation does not over-escalate.
    """
    total = _batch_total(tmp_path, capsys, slot, signals)
    earlier = tier_cap - total + MARGIN_USD  # day total = tier_cap + 2
    code, report = _compute(tmp_path, capsys, slot, signals, rows=[_spent_today(earlier)])
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("slot", SLOTS)
def test_five_high_multiplier_signals_stay_on_the_normal_tier(tmp_path, capsys, slot):
    """The escalation threshold is 'more than 5': five signals and a Normal-cap breach is still refused."""
    total = _batch_total(tmp_path, capsys, slot, 5)
    code, report = _compute(tmp_path, capsys, slot, 5, rows=[_spent_today(180.0 - total + MARGIN_USD)])
    assert (code, report["plan"]) == (2, None)
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("slot", SLOTS)
def test_the_stress_tier_never_lifts_the_weekly_cap_without_a_verified_reserve(tmp_path, capsys, slot):
    """18 signals select Stress, whose nominal $1,800 weekly cap needs a rollover reserve the planner cannot prove.

    The week total lands $2 past the Normal $900, so the batch must be refused (fail closed either way: the
    effective $900 cap, or the reserve being required). Passes today; it guards the escalation from over-reaching.
    """
    total = _batch_total(tmp_path, capsys, slot, 18)
    monday = _raw_buy(EARLIER_SPEND_SYMBOL, 900.0 - total + MARGIN_USD, "2026-10-05T15:31:00+00:00")  # same ISO week
    code, report = _compute(tmp_path, capsys, slot, 18, rows=[monday])
    assert (code, report["plan"]) == (2, None)
    assert {e["code"] for e in report["errors"]} & {"over_weekly_cap", "weekly_cap_required"}
