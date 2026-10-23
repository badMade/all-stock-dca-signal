"""Tests that the backup planner's ``finalize`` never escalates the budget tier either.

Companion to ``test_backup_planner_tiers.py`` (the gated ``compute``); read its docstring first. History: option C
(2026-10-15) made ``finalize`` pick the tier from the plan's 3x-or-greater count; on 2026-10-16 that let backups
place $205.90 against the $180 Normal cap. Operator decision 2026-10-22: Normal-only, trim to fit.

``finalize`` re-checks a saved plan against a FRESH order pull at placement time. Now, whatever tier the plan was
sized under and whoever wrote it (a computed ``backup`` plan or a primary's ``claude`` plan):

* the Normal $180 daily and $900 weekly caps apply, however many orders are 3x or greater;
* a plan that does not fit places the PREFIX that does and reports the rest in ``skipped``; it is never aborted
  whole, never shrunk, never back-filled (the precise trimming rules live in ``test_backup_planner_normal_trim.py``);
* a ticker skipped at placement (quote no longer below the prior close) does not change the cap applied.

Each plan is first built with the real gated ``compute``, then finalized against a history with an earlier same-day
or same-week spend placed so the total lands a known distance past a cap.
"""

from __future__ import annotations

import json

import pytest
from market_data_fixtures import DAY
from test_backup_planner_tiers import (
    ACCOUNT,
    EARLIER_SPEND_SYMBOL,
    ESCALATING,
    MARGIN_USD,
    NORMAL_DAILY_USD,
    NORMAL_WEEKLY_USD,
    NOW,
    SIGNAL_COUNTS,
    SLOTS,
    _compute,
    _raw_buy,
    _spent_today,
    _write,
)

from scripts import backup_planner

GENERATORS = ["backup", "claude"]
FRESH_DISCOUNT = 0.9  # fresh quote 10% under the prior close: comfortably "still below"
HALF_SPREAD = 0.0005  # a 0.1% bid/ask spread, well inside the primaries' circuit breaker


def _plan(tmp_path, capsys, slot, signals, generator):
    """A real plan from the gated ``compute`` over ``signals`` 3x-or-greater tickers, relabelled ``generator``."""
    code, report = _compute(tmp_path, capsys, slot, signals)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    plan = report["plan"]
    plan["generator"] = generator
    assert len(plan["orders"]) == signals
    assert all(o["multiplier"] >= 3.0 for o in plan["orders"]), "every fixture order must be a 3x-or-greater signal"
    return plan


def _finalize(tmp_path, capsys, slot, plan, *, rows=(), skip_first=False):
    """Run ``finalize`` with fresh quotes under every prior close, tight spreads, and ``rows`` in the order pull.

    ``skip_first`` makes the plan's first order's fresh quote NOT below its prior close, so finalize skips it.
    """
    fresh = {o["symbol"]: round(o["prior_close"] * FRESH_DISCOUNT, 4) for o in plan["orders"]}
    if skip_first:
        first = plan["orders"][0]
        fresh[first["symbol"]] = round(first["prior_close"] * 1.01, 4)
    spreads = {symbol: {"bid": round(price * (1 - HALF_SPREAD), 4), "ask": round(price * (1 + HALF_SPREAD), 4)}
               for symbol, price in fresh.items()}
    pages = {"account_number": ACCOUNT, "trading_day": DAY, "pages": [{"orders": list(rows)}]}
    argv = ["finalize", "--trading-day", DAY, "--slot", slot,
            "--plan", _write(tmp_path, "plan.json", plan), "--orders-pages", _write(tmp_path, "pages.json", pages),
            "--fresh-quotes", _write(tmp_path, "fresh.json", fresh), "--fresh-spreads", _write(tmp_path, "spreads.json", spreads),
            "--buying-power", "100000", "--now", NOW[slot], "--local-status", "off"]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _total(plan):
    return sum(o["notional_usd"] for o in plan["orders"])


def _placed_total(report):
    return sum(o["notional_usd"] for o in report["to_place"])


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize("signals", ESCALATING)
def test_finalize_trims_a_plan_that_is_over_the_normal_cap_instead_of_escalating(tmp_path, capsys, slot, signals, generator):
    plan = _plan(tmp_path, capsys, slot, signals, generator)
    earlier = NORMAL_DAILY_USD - _total(plan) + MARGIN_USD  # day total = Normal cap + 2
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[_spent_today(earlier)])
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert earlier + _placed_total(report) <= NORMAL_DAILY_USD
    assert 0 < len(report["to_place"]) < signals
    left_out = [s for s in report["skipped"] if s["reason"] == "over_daily_cap"]
    assert len(report["to_place"]) + len(left_out) == signals


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize("signals", SIGNAL_COUNTS)
def test_finalize_places_a_plan_in_full_when_it_fits_the_normal_cap(tmp_path, capsys, slot, signals, generator):
    plan = _plan(tmp_path, capsys, slot, signals, generator)
    code, report = _finalize(tmp_path, capsys, slot, plan)
    assert (code, report["status"], len(report["to_place"]), report["skipped"]) == (0, "PROCEED", signals, [])


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
def test_finalize_applies_the_normal_cap_to_what_is_left_after_a_placement_skip(tmp_path, capsys, slot, generator):
    """A six-signal plan whose first order is skipped at placement still answers to the Normal cap.

    The earlier spend puts the REMAINING five orders $2 past the Normal $180, so one of them is trimmed too.
    """
    plan = _plan(tmp_path, capsys, slot, 6, generator)
    remaining = _total(plan) - plan["orders"][0]["notional_usd"]
    earlier = NORMAL_DAILY_USD - remaining + MARGIN_USD
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[_spent_today(earlier)], skip_first=True)
    assert (code, report["status"]) == (0, "PROCEED")
    assert earlier + _placed_total(report) <= NORMAL_DAILY_USD
    assert len(report["to_place"]) < 5
    reasons = {s["symbol"]: s["reason"] for s in report["skipped"]}
    assert reasons[plan["orders"][0]["symbol"]] == "quote_not_below_prior_close"
    assert "over_daily_cap" in set(reasons.values())


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
def test_finalize_never_lifts_the_weekly_cap_for_eighteen_signals(tmp_path, capsys, slot, generator):
    """18 signals select Stress for the primary, whose nominal $1,800 weekly cap needs a reserve finalize cannot prove.

    The week total lands $2 past the Normal $900, so the batch is trimmed to stay inside it.
    """
    plan = _plan(tmp_path, capsys, slot, 18, generator)
    spent = NORMAL_WEEKLY_USD - _total(plan) + MARGIN_USD
    monday = _raw_buy(EARLIER_SPEND_SYMBOL, spent, "2026-10-05T15:31:00+00:00")  # same ISO week
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[monday])
    assert (code, report["status"]) == (0, "PROCEED")
    assert spent + _placed_total(report) <= NORMAL_WEEKLY_USD
    assert {s["reason"] for s in report["skipped"]} == {"over_weekly_cap"}
