"""Tests for tier escalation in the backup planner's ``finalize``.

Written test-first, no implementation yet. Companion to ``test_backup_planner_tiers.py`` (the gated ``compute``).
Read that module's docstring first: the same gap, the same contract, the same self-calibrating fixtures.

``finalize`` re-checks a saved plan against a FRESH order pull at placement time and today hardwires the Normal
tier (``scripts/backup_planner.py``, ``_cmd_finalize``), so an escalated plan that ``compute`` now produces would be
refused at the last step with ``over_daily_cap``. The contract here:

* ``finalize`` selects the tier from the PLAN: the number of its orders whose multiplier is 3.0 or more, with the
  primary's thresholds (<=5 Normal, 6-10 Elevated, 11-17 High-volatility, >17 Stress). That is the same count
  ``compute`` used, so a plan is always finalized under the tier it was built under.
* The count is taken over ALL the plan's orders, not just the ones left after reconciliation and fresh-quote
  skips. A ticker skipped at placement time (already bought, quote no longer below the prior close, spread too
  wide) must not drop a plan to a lower tier and turn a valid batch into an ``over_daily_cap`` abort.
* Caps stay all-or-nothing against the SELECTED tier, and Stress without a verified rollover reserve keeps the
  Normal $900 effective weekly cap, exactly as in ``compute``.
* Both plan generators are covered: a computed plan (``backup``) and a primary's plan (``claude``). The tier
  logic does not depend on who wrote the plan.

Each plan is first built with the real gated ``compute`` (so its orders, multipliers and prior closes are the
planner's own), then finalized against a history with an earlier same-day or same-week spend placed so the total
lands a known distance past a cap.
"""

from __future__ import annotations

import json

import pytest
from market_data_fixtures import DAY
from test_backup_planner_tiers import (
    ACCOUNT,
    ALL_COUNTS,
    EARLIER_SPEND_SYMBOL,
    ESCALATING,
    MARGIN_USD,
    NOW,
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


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize(("signals", "tier_cap", "lower_cap"), ESCALATING)
def test_finalize_places_a_plan_that_is_over_the_lower_tiers_cap_but_within_its_own(tmp_path, capsys, slot, signals, tier_cap, lower_cap, generator):
    """The step after the 2026-10-15 failure: an escalated plan must survive finalize, not die at the last gate."""
    plan = _plan(tmp_path, capsys, slot, signals, generator)
    earlier = lower_cap - _total(plan) + MARGIN_USD  # day total = lower_cap + 2
    assert lower_cap < earlier + _total(plan) <= tier_cap
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[_spent_today(earlier)])
    assert (code, report["status"]) == (0, "PROCEED")
    assert len(report["to_place"]) == signals


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize(("signals", "tier_cap"), ALL_COUNTS)
def test_finalize_still_aborts_a_batch_over_the_selected_tiers_cap(tmp_path, capsys, slot, signals, tier_cap, generator):
    """Escalation raises the ceiling to the primary's and no further; the fresh pull is still all-or-nothing (D4).

    Passes today (finalize is always Normal); it pins that the implementation does not over-escalate.
    """
    plan = _plan(tmp_path, capsys, slot, signals, generator)
    earlier = tier_cap - _total(plan) + MARGIN_USD  # day total = tier_cap + 2
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[_spent_today(earlier)])
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
def test_finalize_keeps_five_high_multiplier_signals_on_the_normal_tier(tmp_path, capsys, slot, generator):
    """The threshold is 'more than 5': a five-signal plan past the Normal cap is still refused."""
    plan = _plan(tmp_path, capsys, slot, 5, generator)
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[_spent_today(180.0 - _total(plan) + MARGIN_USD)])
    assert (code, report["to_place"]) == (2, [])
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
def test_finalize_selects_the_tier_from_the_whole_plan_not_from_what_is_left_after_skips(tmp_path, capsys, slot, generator):
    """A six-signal plan whose first order is skipped at placement (quote no longer below the prior close) is still
    an Elevated plan: five orders remain, but the day total may use the $240 cap the plan was built under.

    The earlier spend puts the REMAINING five orders $2 past the Normal $180, so counting only what is left would
    wrongly fall back to Normal and abort.
    """
    plan = _plan(tmp_path, capsys, slot, 6, generator)
    remaining = _total(plan) - plan["orders"][0]["notional_usd"]
    earlier = 180.0 - remaining + MARGIN_USD
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[_spent_today(earlier)], skip_first=True)
    assert (code, report["status"]) == (0, "PROCEED")
    assert len(report["to_place"]) == 5
    assert plan["orders"][0]["symbol"] in {s["symbol"] for s in report["skipped"]}


@pytest.mark.parametrize("generator", GENERATORS)
@pytest.mark.parametrize("slot", SLOTS)
def test_finalize_never_lifts_the_weekly_cap_for_the_stress_tier_without_a_verified_reserve(tmp_path, capsys, slot, generator):
    """18 signals select Stress, whose nominal $1,800 weekly cap needs a rollover reserve finalize cannot prove.

    The week total lands $2 past the Normal $900, so the batch must be refused (fail closed either way: the
    effective $900 cap, or the reserve being required). Passes today; it guards the escalation from over-reaching.
    """
    plan = _plan(tmp_path, capsys, slot, 18, generator)
    monday = _raw_buy(EARLIER_SPEND_SYMBOL, 900.0 - _total(plan) + MARGIN_USD, "2026-10-05T15:31:00+00:00")  # same ISO week
    code, report = _finalize(tmp_path, capsys, slot, plan, rows=[monday])
    assert (code, report["to_place"]) == (2, [])
    assert {e["code"] for e in report["errors"]} & {"over_weekly_cap", "weekly_cap_required"}
