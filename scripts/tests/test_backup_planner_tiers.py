"""Tests that the backup planner's gated ``compute --market-data`` never escalates the budget tier.

Companion to ``test_backup_planner_gate.py`` (slot 1530) and ``test_backup_planner_streams.py`` (the six stream
slots); the trimming contract itself is pinned in ``test_backup_planner_normal_trim.py``.

History: on 2026-10-15 (option C) the planner started picking the tier from the count of 3x-or-greater signals,
the way the primary does (<=5 Normal $180, 6-10 Elevated $240, 11-17 High-volatility $300, >17 Stress $360). On
2026-10-16 that let the backups place $205.90 against the $180 Normal cap. Operator decision 2026-10-22: "Backups
should stay Normal-only like the slots", with an over-cap batch trimmed to fit. These tests now pin that:

* However many signals are at 3x or more, the planned batch never exceeds the Normal $180 daily cap.
* The Normal weekly cap ($900) is likewise never exceeded, and the Stress tier's rollover-reserve weekly ceiling
  is never reached.
* Five or fewer signals are unaffected.

Fixtures self-calibrate against the planner's own batch, so the ladder's dollar amounts are not pinned.
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

NORMAL_DAILY_USD = 180.0
NORMAL_WEEKLY_USD = 900.0
# Signal counts that the primary would map to Normal, Elevated, High-volatility and Stress; the backup ignores them.
SIGNAL_COUNTS = [5, 6, 10, 11, 17, 18]
ESCALATING = [6, 10, 11, 17, 18]


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
@pytest.mark.parametrize("signals", SIGNAL_COUNTS)
def test_the_planned_batch_never_exceeds_the_normal_daily_cap_whatever_the_signal_count(tmp_path, capsys, slot, signals):
    """A batch that would have escalated the tier is held to $180: earlier spend leaves 2 USD less than it needs."""
    total = _batch_total(tmp_path, capsys, slot, signals)
    earlier = NORMAL_DAILY_USD - total + MARGIN_USD  # the full batch would land at the Normal cap + 2
    code, report = _compute(tmp_path, capsys, slot, signals, rows=[_spent_today(earlier)])
    planned = sum(o["notional_usd"] for o in report["plan"]["orders"])
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert earlier + planned <= NORMAL_DAILY_USD
    assert len(report["plan"]["orders"]) < signals
    assert {s["reason"] for s in report["skipped"]} == {"over_daily_cap"}


@pytest.mark.parametrize("slot", SLOTS)
def test_five_high_multiplier_signals_are_planned_in_full_under_the_normal_tier(tmp_path, capsys, slot):
    """The unchanged case: a batch inside the Normal caps is planned whole."""
    code, report = _compute(tmp_path, capsys, slot, 5)
    assert (code, report["status"], len(report["plan"]["orders"])) == (0, "VALID_PLAN", 5)


@pytest.mark.parametrize("slot", SLOTS)
def test_eighteen_signals_never_lift_the_weekly_cap_to_the_stress_tier(tmp_path, capsys, slot):
    """18 signals select Stress for the primary, whose $1,800 weekly cap needs a rollover reserve the planner lacks.

    The week total would land $2 past the Normal $900, so the batch is trimmed to stay inside it.
    """
    total = _batch_total(tmp_path, capsys, slot, 18)
    monday = _raw_buy(EARLIER_SPEND_SYMBOL, NORMAL_WEEKLY_USD - total + MARGIN_USD, "2026-10-05T15:31:00+00:00")  # same ISO week
    code, report = _compute(tmp_path, capsys, slot, 18, rows=[monday])
    planned = sum(o["notional_usd"] for o in report["plan"]["orders"])
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert (NORMAL_WEEKLY_USD - total + MARGIN_USD) + planned <= NORMAL_WEEKLY_USD
    assert {s["reason"] for s in report["skipped"]} == {"over_weekly_cap"}
