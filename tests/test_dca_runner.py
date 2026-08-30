"""Tests for ``dca_runner`` -- the committed live-runner entrypoint.

Written test-first, before any implementation exists, per the repo's
AI-assisted development guidelines (TDD-first: derive the test from expected
behaviour, not from implementation).

Context for the reviewer
------------------------
``PRODUCTION_READINESS.md`` names "live execution is prompt-driven, not
code-driven" as gap #1 and prescribes "one committed, tested runner entrypoint
per stream (wiring + auth + ``run_pipeline`` + ...)". ``dca_runner`` is that
entrypoint. These tests pin the behaviour the seven scheduled slots currently
carry as prose in their task text, so the guardrails stop depending on prompt
text surviving the schedule service's documented truncation
(``config/scheduled_tasks.json`` -> ``live_task_text_compactness_policy``).

Two behaviours here are deliberately stricter than "make it work", because both
have already cost a live day:

1. ``load_strategy_state`` must FAIL CLOSED on a corrupt file. ``docs/live_wiring.md``
   warns that a fresh ``StrategyState`` "silently resets the reserve/weekly
   accounting and defeats the documented budget caps". A corrupt-file path that
   quietly returns a fresh state is that same bug wearing a different hat.
2. Over-cap orders are REFUSED, never clamped. Clamping hides a sizing bug;
   refusing surfaces it.

Note: ``docs/live_wiring.md`` shows ``load_strategy_state`` / ``save_strategy_state``
in its worked example, but neither function exists anywhere in the repo today.
These tests define that contract for the first time.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time as time_module
import types
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from conftest import FIXTURE_ACCOUNT_NUMBER
from near_close_dca import WHOLE_SHARE_TICKERS, BudgetTier, StrategyState, iso_week_key

dca_runner = pytest.importorskip(
    "dca_runner",
    reason="dca_runner is not implemented yet; these tests define its contract.",
)

ET = ZoneInfo("America/New_York")

# conftest.py sets $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER to this synthetic value
# before any module import, so it is also what dca_runner.execute_plan's
# account-identity check (Codex review: "Verify the authorized account before
# placing orders") requires broker.account_number to equal. Reused here
# (rather than an arbitrary literal) so every test that flips both live
# placement gates satisfies that check without needing its own monkeypatch,
# and so it never trips test_config_invariants.py's account-shaped-literal
# scan, which only allowlists this exact synthetic value.
TEST_ACCOUNT_NUMBER = FIXTURE_ACCOUNT_NUMBER

# Ample headroom passed as execute_plan's now-required buying_power_usd in
# tests that aren't specifically exercising the buying-power ceiling itself.
TEST_BUYING_POWER_USD = 1000.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _order(
    symbol, amount, created_at_utc, *,
    placed_agent="agentic", side="buy", state="filled", executed_dollar_amount=None,
):
    """Build a normalized broker order row as ``dca_runner`` should accept it."""
    return dca_runner.BrokerOrder(
        symbol=symbol,
        side=side,
        dollar_amount=amount,
        created_at=created_at_utc,
        placed_agent=placed_agent,
        state=state,
        executed_dollar_amount=executed_dollar_amount,
    )


def _utc(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


# ===========================================================================
# A. StrategyState persistence  (highest-risk: exotic key types)
# ===========================================================================
#
# StrategyState carries JSON-hostile types:
#   buys_by_ticker_by_day     Dict[str, Dict[date, int]]      -> date keys
#   submitted_notional_by_day Dict[date, float]               -> date keys
#   submitted_notional_by_week Dict[tuple[int,int], float]    -> TUPLE keys
#   finalized_budget_days     Set[date]                       -> set
#   selected_tier             Optional[BudgetTier]            -> dataclass
#   reserve_week_key          Optional[tuple[int,int]]        -> tuple
# A naive json.dump loses every one of these. Round-trip fidelity is what the
# weekly and rollover-reserve caps actually rest on.


def test_roundtrip_empty_state(tmp_path):
    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(StrategyState(), path)
    assert dca_runner.load_strategy_state(path) == StrategyState()


def test_roundtrip_preserves_date_keyed_daily_notional(tmp_path):
    state = StrategyState()
    state.record_buy("AAPL", date(2026, 7, 28), 4.0)
    state.record_buy("MSFT", date(2026, 7, 28), 6.0)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.day_notional(date(2026, 7, 28)) == pytest.approx(10.0)
    assert isinstance(next(iter(loaded.submitted_notional_by_day)), date)


def test_roundtrip_preserves_earlier_week_cross_stream_bookkeeping(tmp_path):
    """Codex review: "Reconcile earlier-week spend instead of adding it".
    earlier_week_cross_stream_seeded_usd/_poison_usd must round-trip like
    every other ledger, or a fresh process reload would see "nothing seeded
    yet this week" and re-add the full earlier-week cross-stream total on
    top of spend a prior invocation already reconciled in."""
    state = StrategyState()
    day = date(2026, 7, 28)
    state.reconcile_earlier_week_cross_stream_notional(day, 150.0)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    week_key = iso_week_key(day)
    assert loaded.earlier_week_cross_stream_seeded_usd[week_key] == pytest.approx(150.0)
    assert isinstance(next(iter(loaded.earlier_week_cross_stream_seeded_usd)), tuple)

    # A later invocation reloading this persisted state must still treat the
    # $150 as already-seeded, not re-add it.
    loaded.reconcile_earlier_week_cross_stream_notional(day, 150.0)
    assert loaded.week_notional(day) == pytest.approx(150.0)


def test_state_with_seeded_bookkeeping_exceeding_the_weekly_ledger_fails_closed(tmp_path):
    """Codex review: "Reject seeded bookkeeping that exceeds the weekly
    ledger". reconcile_earlier_week_cross_stream_notional() only ever writes
    a seeded-so-far value <= the weekly bucket it was just folded into. A
    document with a seeded value of $900 against a $0 weekly bucket is
    impossible from any legitimate write path: a later reconciliation would
    compute a negative increment against that inflated baseline, silently
    forgetting real earlier-week spend just discovered instead of adding it
    (e.g. a $100 real scan lands on incremental = 100 - 900 = -800, guarded
    to a no-op, leaving the weekly bucket at $0 forever)."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {"2026-31": 0.0},
        "earlier_week_cross_stream_seeded_usd": {"2026-31": 900.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_roundtrip_preserves_seeded_prior_context_bookkeeping(tmp_path):
    """Codex review: "Reconcile increases to already-seeded ticker
    allocations". seeded_prior_context_amount_by_ticker_day/
    _attempts_by_ticker_day must round-trip, or a fresh process reload would
    forget how much of a ticker's aggregate has already been folded in via
    reconcile_prior_allocation."""
    state = StrategyState()
    day = date(2026, 7, 28)
    state.reconcile_prior_allocation("AAPL", day, amount_usd=2.0, attempt_count=1)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.seeded_prior_context_amount_by_ticker_day["AAPL"][day] == pytest.approx(2.0)
    assert loaded.seeded_prior_context_attempts_by_ticker_day["AAPL"][day] == 1

    # A later invocation reloading this persisted state must top up only the
    # newly-discovered delta, not re-add the $2/1 attempt already reconciled.
    loaded.reconcile_prior_allocation("AAPL", day, amount_usd=4.0, attempt_count=2)
    assert loaded.day_notional(day) == pytest.approx(4.0)
    assert loaded.buys_today("AAPL", day) == 2


def test_state_with_seeded_ticker_attempts_exceeding_the_buy_ledger_fails_closed(tmp_path):
    """Codex review ("Reject seeded bookkeeping that exceeds the weekly
    ledger" -- same class of corruption applied to the per-ticker
    bookkeeping): reconcile_prior_allocation() only ever writes a seeded
    attempt count <= buys_by_ticker_by_day's own count for that ticker/day.
    A document with a seeded count of 5 against a buy ledger of 1 is
    impossible from any legitimate write path."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}},
        "submitted_notional_by_day": {"2026-07-28": 2.0},
        "submitted_notional_by_week": {"2026-31": 2.0},
        "seeded_prior_context_attempts_by_ticker_day": {"AAPL": {"2026-07-28": 5}},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_seeded_ticker_amount_exceeding_the_day_ledger_fails_closed(tmp_path):
    """Same as above, for the dollar-amount bookkeeping against the day-level
    submitted_notional_by_day total."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}},
        "submitted_notional_by_day": {"2026-07-28": 2.0},
        "submitted_notional_by_week": {"2026-31": 2.0},
        "seeded_prior_context_amount_by_ticker_day": {"AAPL": {"2026-07-28": 900.0}},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_roundtrip_preserves_tuple_keyed_weekly_notional(tmp_path):
    """(iso_year, iso_week) tuple keys are the piece a naive dict dump destroys."""
    state = StrategyState()
    day = date(2026, 7, 28)
    state.record_buy("AAPL", day, 25.0)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.week_notional(day) == pytest.approx(25.0)
    key = iso_week_key(day)
    assert key in loaded.submitted_notional_by_week
    assert isinstance(key[0], int) and isinstance(key[1], int)


def test_roundtrip_preserves_per_ticker_buy_counts(tmp_path):
    """One-order-per-ticker-per-stream-per-day depends on these counts."""
    state = StrategyState()
    day = date(2026, 7, 28)
    state.record_buy("NVDA", day, 2.0)
    state.record_buy("NVDA", day, 2.0)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.buys_today("NVDA", day) == 2
    assert loaded.buys_today("AMD", day) == 0


def test_roundtrip_preserves_submitted_order_count_by_day(tmp_path):
    """Codex review: "Persist the shared daily attempt counter".
    submitted_order_count_by_day (config/budgets.json circuit_breakers.
    max_orders_per_day) must round-trip like every other ledger, not reload
    empty -- otherwise a reconstructed process undercounts real prior
    attempts against the shared breaker. seed_prior_allocation's
    attempt_count (2, distinct from record_buy's fixed +1) exercises the
    field's real write path."""
    state = StrategyState()
    day = date(2026, 7, 28)
    state.seed_prior_allocation("AAPL", day, 4.0, attempt_count=2)
    state.record_buy("MSFT", day, 6.0)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.day_order_count(day) == 3


def test_legacy_state_without_order_count_backfills_from_buy_counts(tmp_path):
    """A document written before submitted_order_count_by_day existed never
    persisted it at all. Rather than loading an empty counter (and letting
    _seed_prior_allocations() silently forget every already-booked ticker's
    attempts, since it skips reseeding a ticker whose buys_today() is
    already nonzero), it must be reconstructed losslessly from
    buys_by_ticker_by_day's per-day sum -- exactly what that counter always
    equalled before this field existed to track it separately."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}, "MSFT": {"2026-07-28": 2}},
            "submitted_notional_by_day": {"2026-07-28": 10.0},
            "submitted_notional_by_week": {"2026-31": 10.0},
            "weekly_rollover_reserve_usd": 0.0,
            "finalized_budget_days": [],
            "reserve_week_key": None,
            "selected_tier": None,
        })
    )
    loaded = dca_runner.load_strategy_state(path)
    assert loaded.day_order_count(date(2026, 7, 28)) == 3


def test_state_with_mismatched_order_count_fails_closed(tmp_path):
    """A genuinely current document (the key IS present) whose per-day
    attempt count disagrees with buys_by_ticker_by_day's per-ticker sum is
    corrupt -- record_buy()/seed_prior_allocation() always bump both
    together by the same amount, so they can never legitimately diverge."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}},
            "submitted_notional_by_day": {"2026-07-28": 4.0},
            "submitted_notional_by_week": {"2026-31": 4.0},
            "submitted_order_count_by_day": {"2026-07-28": 5},
            "weekly_rollover_reserve_usd": 0.0,
            "finalized_budget_days": [],
            "reserve_week_key": None,
            "selected_tier": None,
        })
    )
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_roundtrip_preserves_reserve_fields_and_finalized_days(tmp_path):
    """The reserve here must be one roll_weekly_reserve() could actually
    derive from the rest of this fixture: the most recent finalized week
    before reserve_week_key (2026-31, containing 2026-07-27/28) spent
    $900 - $137.25 of its $900 Normal cap, leaving exactly $137.25 unused
    going into 2026-32 (Codex review: "Verify persisted rollover reserve
    against prior-week state")."""
    state = StrategyState()
    state.weekly_rollover_reserve_usd = 137.25
    state.reserve_week_key = iso_week_key(date(2026, 8, 3))
    state.finalized_budget_days = {date(2026, 7, 27), date(2026, 7, 28)}
    state.record_buy("AAPL", date(2026, 7, 28), 900.0 - 137.25)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.weekly_rollover_reserve_usd == pytest.approx(137.25)
    assert loaded.reserve_week_key == iso_week_key(date(2026, 8, 3))
    assert loaded.finalized_budget_days == {date(2026, 7, 27), date(2026, 7, 28)}
    assert all(isinstance(d, date) for d in loaded.finalized_budget_days)


def test_roundtrip_never_persists_selected_tier(tmp_path):
    """selected_tier is run-scoped (near_close_dca.StrategyState.select_daily_budget()
    only (re)computes it when None) with no day key to validate freshness
    against, so a save/load round-trip must never carry it forward -- a Stress
    day's tier surviving into a later day would authorize spend above what
    that day's own candidates justify (Codex review: "Do not persist a
    run-scoped budget tier across runs")."""
    state = StrategyState()
    state.selected_tier = BudgetTier(
        name="high_volatility", daily_cap_usd=300.0, weekly_cap_usd=1500.0
    )

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.selected_tier is None


def test_missing_state_file_returns_fresh_state(tmp_path):
    """First run of a brand-new week/machine is legitimate, not an error."""
    assert dca_runner.load_strategy_state(tmp_path / "nope.json") == StrategyState()


def test_corrupt_state_file_fails_closed_and_does_not_reset(tmp_path):
    """A truncated/garbled file must NOT silently become a fresh state.

    Silently resetting is precisely the failure docs/live_wiring.md warns about:
    it wipes the weekly and rollover accounting and defeats the budget caps
    without anyone noticing. Abort loudly instead.
    """
    path = tmp_path / "state.json"
    path.write_text('{"submitted_notional_by_day": {"2026-07-28": 12.0')  # truncated

    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"schema_version": 1}',  # missing every accounting key
        '{"submitted_notional_by_day": {"2026-07-28": 12.0}}',  # no schema_version
    ],
)
def test_incomplete_but_valid_json_state_fails_closed_and_does_not_reset(tmp_path, raw):
    """A syntactically valid but PARTIAL document is as dangerous as a truncated one.

    _state_from_json defaults every missing field, so a truncated writer or an
    accidental replacement that leaves e.g. "{}" would otherwise load as a
    brand-new state with zero weekly reserve and zero submitted notional
    (Codex review: "Reject incomplete valid-JSON state documents").
    """
    path = tmp_path / "state.json"
    path.write_text(raw)

    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


@pytest.mark.parametrize(
    "raw",
    [
        # json.loads accepts the non-standard NaN literal even though it isn't
        # valid JSON; a NaN reserve would make effective_weekly_cap() invent
        # full rollover capacity regardless of what was actually earned.
        '{"schema_version": 1, "buys_by_ticker_by_day": {}, '
        '"submitted_notional_by_day": {}, "submitted_notional_by_week": {}, '
        '"weekly_rollover_reserve_usd": NaN, "finalized_budget_days": [], '
        '"reserve_week_key": null, "selected_tier": null}',
        # A negative persisted day notional would undercount real prior spend.
        json.dumps({
            "schema_version": 1,
            "buys_by_ticker_by_day": {},
            "submitted_notional_by_day": {"2026-07-28": -12.0},
            "submitted_notional_by_week": {},
            "weekly_rollover_reserve_usd": 0.0,
            "finalized_budget_days": [],
            "reserve_week_key": None,
            "selected_tier": None,
        }),
    ],
)
def test_state_with_unsafe_numeric_fields_fails_closed(tmp_path, raw):
    """NaN/negative persisted numerics must never reach budget arithmetic
    (Codex review: "Reject semantically corrupt persisted budget state")."""
    path = tmp_path / "state.json"
    path.write_text(raw)
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_duplicate_json_keys_fails_closed(tmp_path):
    """Python's stdlib json.loads silently resolves a duplicate object key by
    keeping the last occurrence, which would let a corrupted or maliciously
    edited state file overwrite e.g. weekly_rollover_reserve_usd with a second
    value the file's own author never intended to be authoritative -- json.dumps
    can never itself produce this, so it can only arrive via manual/external
    tampering with the persisted file (Codex review: "Reject duplicate keys in
    persisted state JSON")."""
    path = tmp_path / "state.json"
    path.write_text(
        '{"schema_version": 1, "buys_by_ticker_by_day": {}, '
        '"submitted_notional_by_day": {}, "submitted_notional_by_week": {}, '
        '"weekly_rollover_reserve_usd": 0.0, "weekly_rollover_reserve_usd": 900.0, '
        '"finalized_budget_days": [], "reserve_week_key": null, "selected_tier": null}'
    )
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_aliased_week_keys_fails_closed(tmp_path):
    """Distinct textual keys can normalize to the same ISO week ("2026-1" and
    "2026-01" both parse to (2026, 1)): the duplicate-key JSON hook only
    catches two literally identical keys, not two different spellings that
    alias to the same tuple, so the later one would otherwise silently
    overwrite the earlier instead of raising (Codex review: "Reject aliases
    of persisted week keys")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {"2026-1": 900.0, "2026-01": 0.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_inconsistent_daily_and_weekly_ledgers_fails_closed(tmp_path):
    """record_buy() always updates the day and week ledgers together in the
    same call, and neither is ever pruned independently of the other, so a
    legitimately-written state's weekly total can never fall SHORT of the
    sum of that week's daily entries. A document with $900 recorded for a
    day but $0 for that ISO week is corrupt: StrategyState.week_notional()
    would silently forget the real spend and authorize another week's worth
    of buys even though the same document proves the prior spend (Codex
    review: "Reject inconsistent persisted daily and weekly ledgers")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {"2026-07-28": 900.0},
        "submitted_notional_by_week": {"2026-31": 0.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_weekly_ledger_may_legitimately_exceed_its_own_daily_sum(tmp_path):
    """Codex review: "Reconcile earlier-week spend instead of adding it".
    bump_week_notional_only() adds EARLIER-weekday cross-stream spend to the
    week bucket ONLY, with no corresponding submitted_notional_by_day entry
    in THIS state (that spend belongs to a sibling stream or an earlier
    day's own history). A week total exceeding its own daily sum is
    therefore a legitimate, expected state -- not the corruption the
    equality check above catches -- and must round-trip through save/load
    without raising."""
    state = StrategyState()
    day = date(2026, 7, 28)
    state.record_buy("AAPL", day, 100.0)
    state.bump_week_notional_only(day, 50.0)
    assert state.week_notional(day) == pytest.approx(150.0)
    assert state.day_notional(day) == pytest.approx(100.0)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.week_notional(day) == pytest.approx(150.0)
    assert loaded.day_notional(day) == pytest.approx(100.0)


def test_state_with_a_cent_level_ledger_mismatch_fails_closed(tmp_path):
    """A flat 0.01 tolerance treats an exact one-cent discrepancy as noise,
    but floating-point subtraction of the two independently accumulated
    totals can land just under that threshold even for a real corrupt
    mismatch -- daily entries summing to $899.01 against a persisted $899.00
    must still fail closed rather than silently understating the real weekly
    ledger by a cent (Codex review: "Reject cent-level persisted ledger
    mismatches")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}},
        "submitted_notional_by_day": {"2026-07-28": 899.01},
        "submitted_notional_by_week": {"2026-31": 899.00},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_sub_cent_persisted_notional_fails_closed(tmp_path):
    """Every real notional this module ever writes is exactly cent-precision,
    so a value like $899.004 is corrupt regardless of whether it happens to
    round to the same cent total as another field it is compared against --
    rounding both sides to whole cents before comparing would otherwise treat
    a daily total of $899.004 against a weekly total of $899.00 as
    consistent, since both round to 89900 cents (Codex review: "Reject
    sub-cent persisted ledger values")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}},
        "submitted_notional_by_day": {"2026-07-28": 899.004},
        "submitted_notional_by_week": {"2026-31": 899.00},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_buy_count_but_no_notional_loads_successfully(tmp_path):
    """Codex review: "Keep zero-notional attempts out of the buy ledger".
    seed_prior_allocation() bumps buys_by_ticker_by_day by attempt_count for
    a RECONSTRUCTED zero-capital attempt (a candidate that obtained an
    order_id but was confirmed rejected/canceled, or a placement that
    raised) -- an all-rejected day for a ticker is a legitimate buy-count-
    with-zero-notional state, not corruption. A stricter equality check here
    previously made save_strategy_state()'s own pre-save validation reject
    this state, turning an otherwise-valid reconstruction into
    StateCorruptError on every day with an all-zero-notional prior attempt."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    state = dca_runner.load_strategy_state(path)
    assert state.buys_today("AAPL", date(2026, 7, 28)) == 1
    assert state.day_notional(date(2026, 7, 28)) == 0.0


def test_seed_prior_allocation_zero_notional_attempt_round_trips(tmp_path):
    """End-to-end version of the fixture above, using the real writer
    (StrategyState.seed_prior_allocation) instead of a hand-built document,
    exercising the exact save/load path save_strategy_state's own pre-save
    validation runs against."""
    state = StrategyState()
    day = date(2026, 7, 28)
    state.seed_prior_allocation("AAPL", day, notional_usd=0.0, attempt_count=2)

    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(state, path)
    loaded = dca_runner.load_strategy_state(path)

    assert loaded.buys_today("AAPL", day) == 2
    assert loaded.day_notional(day) == 0.0
    assert loaded.day_order_count(day) == 2


def test_state_with_a_whitespace_only_ticker_key_fails_closed(tmp_path):
    """A key containing only whitespace normalizes to "" and would
    otherwise load successfully whenever its daily/weekly ledger entries
    happen to agree, even though this state can never have been produced
    by record_buy(), the only runtime writer, which always uses a real,
    whitelisted ticker (Codex review: "Reject empty or non-whitelisted
    persisted ticker keys")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"   ": {"2026-07-28": 1}},
        "submitted_notional_by_day": {"2026-07-28": 2.0},
        "submitted_notional_by_week": {"2026-31": 2.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_delisted_historical_ticker_loads_successfully(tmp_path):
    """buys_by_ticker_by_day retains historical entries indefinitely, while
    ACTIVE_TICKERS reflects only today's config -- a ticker legitimately
    bought in the past and later removed from the active universe must not
    make every subsequent load_strategy_state() call abort (Codex review:
    "Preserve historical ticker counts after whitelist changes")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"ZZZZ_DELISTED": {"2026-07-28": 1}},
        "submitted_notional_by_day": {"2026-07-28": 2.0},
        "submitted_notional_by_week": {"2026-31": 2.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    state = dca_runner.load_strategy_state(path)
    assert state.buys_today("ZZZZ_DELISTED", date(2026, 7, 28)) == 1


def test_state_with_notional_but_no_buy_count_fails_closed(tmp_path):
    """The inverse of the buy-count cross-check above: record_buy() is the
    only runtime writer of all three structures, always together, so a
    positive submitted_notional_by_day entry can never legitimately exist
    with no ticker's buy count on that date. Without this, buys_today()
    reports zero for every ticker on that date and can authorize another
    same-day buy despite the same document proving spend already happened
    (Codex review: "Reject notional days with no persisted buy count")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {"2026-07-28": 2.0},
        "submitted_notional_by_week": {"2026-31": 2.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_aliased_ticker_keys_fails_closed(tmp_path):
    """A persisted key such as "aapl" or " AAPL " is accepted verbatim even
    though runtime lookups (buys_today()) use the canonical "AAPL" -- the
    state would load successfully but buys_today("AAPL", day) returns zero,
    permitting another same-day AAPL buy. Two distinct raw spellings
    normalizing to the same ticker must also be rejected outright, the same
    as an aliased date/week key (Codex review: "Normalize persisted ticker
    keys before restoring counts")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1}, "aapl": {"2026-07-29": 1}},
        "submitted_notional_by_day": {"2026-07-28": 2.0, "2026-07-29": 2.0},
        "submitted_notional_by_week": {"2026-31": 4.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_aliased_day_keys_fails_closed(tmp_path):
    """date.fromisoformat() accepts both the extended YYYY-MM-DD and the
    basic, dash-free ISO spelling, so two distinct JSON date keys can alias
    to the same date and the later value would otherwise silently overwrite
    the earlier one -- e.g. replacing a $900 daily entry with zero -- exactly
    the same risk already closed for week keys (Codex review: "Reject
    aliased persisted date keys")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {"2026-07-28": 900.0, "20260728": 0.0},
        "submitted_notional_by_week": {"2026-31": 0.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_aliased_buy_count_day_keys_fails_closed(tmp_path):
    """The same date-key aliasing risk applies to buys_by_ticker_by_day's
    per-ticker day maps, not only submitted_notional_by_day."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 1, "20260728": 1}},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_zero_persisted_buy_count_fails_closed(tmp_path):
    """StrategyState.record_buy() only ever creates a buy-count entry by
    incrementing from buys_today()'s own zero default, so a stored 0 never
    comes from legitimate use -- a real day/ticker with no buy simply has no
    entry at all. Trusting a persisted 0 would let buys_today() report no
    prior buy for a ticker that has one, permitting a second same-day order
    (Codex review: "Reject zero persisted buy counts")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": 0}},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("submitted_notional_by_day", False),
        ("submitted_notional_by_week", False),
        ("weekly_rollover_reserve_usd", False),
        ("submitted_notional_by_day", "12.0"),
        ("submitted_notional_by_week", "12.0"),
        ("weekly_rollover_reserve_usd", "12.0"),
    ],
)
def test_state_with_a_non_number_ledger_value_fails_closed(tmp_path, field, bad_value):
    """_state_to_json never writes a bool or a numeric string for a notional
    field. float() would otherwise coerce both silently -- float(False) ==
    0.0 -- letting a persisted positive spend replaced with false load as if
    nothing had been spent (Codex review: "Reject non-number ledger values
    before float coercion")."""
    base = {
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }
    if field == "weekly_rollover_reserve_usd":
        base[field] = bad_value
    else:
        base[field] = {"2026-07-28" if field.endswith("_day") else "2026-31": bad_value}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(base))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_an_oversized_ledger_value_fails_closed(tmp_path):
    """A syntactically valid but astronomically large JSON integer (which
    json.loads parses as a plain, unbounded Python int) makes float() raise
    OverflowError rather than ValueError; left uncaught, this would crash
    the load instead of producing the documented StateCorruptError (Codex
    review: "Convert oversized JSON numbers into a controlled rejection")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {"2026-07-28": 10**1000},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_read_failure_raises_state_corrupt_error(tmp_path, monkeypatch):
    """path.exists() only proves the file was there a moment ago -- a
    permissions change, a filesystem I/O error, or the file being removed
    between that check and the read all raise OSError from read_bytes()
    itself, which neither UnicodeDecodeError nor ValueError catches. Left
    uncaught, this would escape load_strategy_state's documented
    StateCorruptError fail-closed contract with a raw OSError instead (Codex
    review: "Convert durable-state read failures to StateCorruptError")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    original_read_bytes = dca_runner.Path.read_bytes

    def _boom(self, *args, **kwargs):
        if self == path:
            raise OSError("permission denied")
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(dca_runner.Path, "read_bytes", _boom)
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_stat_failure_raises_state_corrupt_error(tmp_path, monkeypatch):
    """A prior round wrapped read_bytes()'s own OSError, but the path.exists()
    stat probe that runs BEFORE that guarded block was untouched: an
    inaccessible ancestor directory or another filesystem error makes
    Path.exists() itself raise, not just return False, escaping
    load_strategy_state's documented StateCorruptError fail-closed contract
    with a raw exception before Gate 9 could ever classify it as corruption
    (Codex review: "Wrap state stat failures as StateCorruptError")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    original_exists = dca_runner.Path.exists

    def _boom(self, *args, **kwargs):
        if self == path:
            raise OSError("permission denied")
        return original_exists(self, *args, **kwargs)

    monkeypatch.setattr(dca_runner.Path, "exists", _boom)
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_dict_finalized_budget_days_fails_closed(tmp_path):
    """_state_to_json only ever emits a list of ISO date strings for
    finalized_budget_days. A dict instead (e.g. {"2026-07-24": "invented"})
    would otherwise iterate silently over its KEYS -- which look like valid
    date strings and parse fine -- reading a structurally malformed document
    as a legitimate list of finalized days and, downstream, as prior-week
    activity with zero spend that could justify rollover capacity never
    earned (Codex review: "Reject non-list finalized-day state")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": {"2026-07-24": "invented"},
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_weekend_finalized_budget_day_fails_closed(tmp_path):
    """A finalized_budget_days entry is accepted so far purely because it
    parses as a date, so a corrupt Saturday entry can count as prior-week
    activity below even though Gate 0 could never have finalized a live
    budget on a day the exchange was closed. 2026-07-25 is a Saturday,
    per config/market_calendar.json. An empty prior-week ledger paired with
    a real week key and a nonzero reserve would otherwise let the full
    $900 look earned (Codex review: "Reject non-trading finalized days
    before granting reserve")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": ["2026-07-25"],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_tampered_selected_tier_loads_with_it_discarded(tmp_path):
    """selected_tier is never round-tripped at all -- a document naming a real
    tier but carrying altered caps is not merely rejected, it is ignored
    outright, same as any other value in this field (Codex review: "Do not
    persist a run-scoped budget tier across runs")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": {
            "name": "stress",
            "daily_cap_usd": 360.0,
            "weekly_cap_usd": 999999.0,  # tampered: real stress cap is 1800.0
            "uses_rollover_reserve": True,
        },
    }))
    state = dca_runner.load_strategy_state(path)
    assert state.selected_tier is None


def test_state_with_a_nonzero_reserve_and_a_null_week_key_fails_closed(tmp_path):
    """A real reserve is never legitimately unkeyed -- roll_weekly_reserve
    always stamps reserve_week_key in the same write that sets a nonzero
    weekly_rollover_reserve_usd (Codex review: "Require a week key for every
    nonzero rollover reserve")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 900.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


@pytest.mark.parametrize(
    "bad_week_key",
    ["2026-00", "2026-54", "2027-53"],  # 2027 has no ISO week 53
)
def test_state_with_an_impossible_iso_week_key_fails_closed(tmp_path, bad_week_key):
    """int() alone accepts a week number outside what iso_week_key() ever
    produces. A corrupted key would silently miss on every later lookup and
    week_notional() would read back 0.0, forgetting persisted weekly spend
    rather than raising (Codex review: "Reject impossible ISO week keys")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {bad_week_key: 900.0},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_an_impossible_reserve_week_key_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 900.0,
        "finalized_budget_days": [],
        "reserve_week_key": "2026-00",
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_an_unjustified_rollover_reserve_fails_closed(tmp_path):
    """A week key alone only proves SOME reserve was rolled for that week,
    never that this amount is what roll_weekly_reserve() would actually have
    derived from the document's own finalized_budget_days and prior-week
    submitted_notional_by_week: a document with no finalized days and no
    spend anywhere can never legitimately carry a nonzero reserve (Codex
    review: "Verify persisted rollover reserve against prior-week state")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 900.0,
        "finalized_budget_days": [],
        "reserve_week_key": "2026-31",
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_state_with_a_legitimately_zeroed_rollover_reserve_loads_fine(tmp_path):
    """A real week key paired with weekly_rollover_reserve_usd=0 is not
    always corrupt: with no earlier finalized week at all, $0 is exactly
    what roll_weekly_reserve() would derive, and the broadened check (Codex
    review: "Validate zero rollover reserves against persisted history")
    must still accept it rather than treating every zeroed reserve as
    unjustified."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],  # no earlier finalized week at all
        "reserve_week_key": "2026-31",
        "selected_tier": None,
    }))
    state = dca_runner.load_strategy_state(path)
    assert state.weekly_rollover_reserve_usd == 0.0


def test_state_with_a_zeroed_but_unjustified_rollover_reserve_fails_closed(tmp_path):
    """The mirror image of the unjustified-nonzero-reserve check: a document
    stamped with a real, current week key but weekly_rollover_reserve_usd=0
    previously skipped the recompute-and-compare check entirely (it only ran
    when the stored amount was nonzero), even though this document's own
    finalized_budget_days (a prior week finalized with zero spend) actually
    justifies the full $900 reserve. Loading it successfully would silently
    cap Stress-tier weekly capacity at Normal's $900 for the whole week
    instead of the full $1,800 (Codex review: "Validate zero rollover
    reserves against persisted history")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": ["2026-07-20"],  # prior ISO week (2026, 30), zero spend
        "reserve_week_key": "2026-31",  # the following week -- e.g. 2026-07-28
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


@pytest.mark.parametrize("bad_count", [-0.5, 0.5, True, -1])
def test_state_with_a_fractional_or_non_integer_buy_count_fails_closed(tmp_path, bad_count):
    """int(n) truncates -0.5/0.5 to 0, which would silently pass a post-
    conversion non-negative check and make a reload report no prior buy for
    a ticker that already has one (Codex review: "Reject fractional
    persisted buy counts")."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "buys_by_ticker_by_day": {"AAPL": {"2026-07-28": bad_count}},
        "submitted_notional_by_day": {},
        "submitted_notional_by_week": {},
        "weekly_rollover_reserve_usd": 0.0,
        "finalized_budget_days": [],
        "reserve_week_key": None,
        "selected_tier": None,
    }))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.load_strategy_state(path)


def test_load_and_save_strategy_state_use_a_default_path_when_omitted(tmp_path, monkeypatch):
    """docs/live_wiring.md's worked example calls both with no path argument
    (Codex review: "Supply the documented default state path")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    state = StrategyState()
    state.record_buy("AAPL", date(2026, 7, 28), 4.0)

    dca_runner.save_strategy_state(state)
    loaded = dca_runner.load_strategy_state()

    assert loaded.day_notional(date(2026, 7, 28)) == pytest.approx(4.0)
    assert (tmp_path / "dca_runner" / "strategy_state.json").exists()


def test_save_strategy_state_refuses_a_semantically_invalid_state(tmp_path):
    """record_buy() does not itself validate notional_usd -- a NaN or
    negative notional would otherwise be written successfully, os.replace()
    would destroy the previous good file, and the very next
    load_strategy_state() call would reject the document just written,
    breaking every subsequent scheduled invocation until someone repairs the
    file by hand (Codex review: "Validate state before replacing the durable
    file")."""
    path = tmp_path / "state.json"
    good = StrategyState()
    good.record_buy("AAPL", date(2026, 7, 28), 5.0)
    dca_runner.save_strategy_state(good, path)

    bad = StrategyState()
    bad.record_buy("AAPL", date(2026, 7, 28), float("nan"))
    with pytest.raises(dca_runner.StateCorruptError):
        dca_runner.save_strategy_state(bad, path)

    # The previous good file must survive the refused write untouched.
    reloaded = dca_runner.load_strategy_state(path)
    assert reloaded.day_notional(date(2026, 7, 28)) == pytest.approx(5.0)
    assert list(path.parent.glob("*.tmp*")) == []


def test_state_write_is_atomic_no_partial_file_left_behind(tmp_path):
    """A crash mid-write must leave the previous good state intact."""
    path = tmp_path / "state.json"
    good = StrategyState()
    good.record_buy("AAPL", date(2026, 7, 28), 5.0)
    dca_runner.save_strategy_state(good, path)

    with pytest.raises(RuntimeError):
        dca_runner.save_strategy_state(good, path, _fail_after_temp_write=True)

    reloaded = dca_runner.load_strategy_state(path)
    assert reloaded.day_notional(date(2026, 7, 28)) == pytest.approx(5.0)
    assert list(path.parent.glob("*.tmp*")) == []


def test_state_write_fsyncs_the_file_and_directory(tmp_path, monkeypatch):
    """write_text() + os.replace() alone makes the rename atomic but is not
    crash-durable: neither the temp file's contents nor the parent
    directory are fsynced, so a host power loss shortly after
    save_strategy_state() returns can still lose the write or the rename
    itself and silently revive the previous state. save_strategy_state now
    routes through dca_fileio.atomic_open_w, which fsyncs both (Codex
    review: "Fsync durable state before reporting a successful save")."""
    path = tmp_path / "state.json"
    good = StrategyState()
    good.record_buy("AAPL", date(2026, 7, 28), 5.0)

    fsync_calls = []
    original_fsync = dca_runner.os.fsync

    def _spy_fsync(fd):
        fsync_calls.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(dca_runner.os, "fsync", _spy_fsync)
    dca_runner.save_strategy_state(good, path)
    assert len(fsync_calls) >= 1

    reloaded = dca_runner.load_strategy_state(path)
    assert reloaded.day_notional(date(2026, 7, 28)) == pytest.approx(5.0)


def test_state_transaction_loads_mutates_and_saves(tmp_path):
    """The basic load-mutate-save contract: mutations inside the `with` block
    are visible to a subsequent load once it exits cleanly."""
    path = tmp_path / "state.json"
    good = StrategyState()
    good.record_buy("AAPL", date(2026, 7, 28), 5.0)
    dca_runner.save_strategy_state(good, path)

    with dca_runner.state_transaction(path) as state:
        assert state.day_notional(date(2026, 7, 28)) == pytest.approx(5.0)
        state.record_buy("MSFT", date(2026, 7, 28), 3.0)

    reloaded = dca_runner.load_strategy_state(path)
    assert reloaded.day_notional(date(2026, 7, 28)) == pytest.approx(8.0)


def test_state_transaction_saves_a_mutation_even_when_the_block_raises(tmp_path):
    """state.record_buy() mutates state in memory as soon as the broker
    accepts an order, long before later artifact/reporting work returns or
    raises -- discarding that mutation because something downstream failed
    would let the next invocation reload pre-order totals and risk a
    duplicate buy or a later cap overrun for an order that already happened
    (Codex review: "Persist partial state before releasing the
    transaction")."""
    path = tmp_path / "state.json"
    good = StrategyState()
    good.record_buy("AAPL", date(2026, 7, 28), 5.0)
    dca_runner.save_strategy_state(good, path)

    with pytest.raises(RuntimeError):
        with dca_runner.state_transaction(path) as state:
            state.record_buy("MSFT", date(2026, 7, 28), 3.0)
            raise RuntimeError("simulated post-placement reporting failure")

    reloaded = dca_runner.load_strategy_state(path)
    assert reloaded.day_notional(date(2026, 7, 28)) == pytest.approx(8.0)


def test_state_transaction_preserves_the_body_exception_alongside_a_callback_failure(tmp_path):
    """Codex review ("Preserve the transaction-body exception when callbacks
    fail"): a `raise` executed from the `finally` block that runs queued
    ``pending_durable_commit_callbacks`` REPLACES whatever exception is
    already propagating out of the `with` block's own body -- if that body
    raised (the actual reason this transaction is unwinding) and a queued
    callback ALSO then raises, the callback's exception must not silently
    discard the original body exception. Both must be retrievable by a
    caller inspecting the raised error.
    """
    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(StrategyState(), path)

    def _failing_callback():
        raise RuntimeError("simulated marker-write failure")

    with pytest.raises(ExceptionGroup) as exc_info:
        with dca_runner.state_transaction(path) as state:
            state.pending_durable_commit_callbacks.append(_failing_callback)
            raise ValueError("simulated run_pipeline failure")

    exceptions = exc_info.value.exceptions
    assert any(isinstance(exc, ValueError) for exc in exceptions)
    assert any(isinstance(exc, RuntimeError) for exc in exceptions)


def test_state_transaction_groups_a_keyboard_interrupt_without_masking_it(tmp_path):
    """Codex review ("Use BaseExceptionGroup when grouping callback
    interrupts"): plain ``ExceptionGroup`` raises TypeError if asked to
    wrap a direct ``BaseException`` (e.g. ``KeyboardInterrupt``/
    ``SystemExit``) rather than an ``Exception``. The callback-error
    collection here deliberately catches ``BaseException``, so a callback
    interrupted by Ctrl-C mid-write -- combined with the transaction
    body's own in-flight exception -- must not raise an unrelated
    TypeError that masks BOTH original failures.
    """
    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(StrategyState(), path)

    def _interrupted_callback():
        raise KeyboardInterrupt()

    with pytest.raises(BaseExceptionGroup) as exc_info:
        with dca_runner.state_transaction(path) as state:
            state.pending_durable_commit_callbacks.append(_interrupted_callback)
            raise ValueError("simulated run_pipeline failure")

    exceptions = exc_info.value.exceptions
    assert any(isinstance(exc, ValueError) for exc in exceptions)
    assert any(isinstance(exc, KeyboardInterrupt) for exc in exceptions)


def test_state_transaction_serializes_two_overlapping_callers(tmp_path):
    """Two overlapping scheduled invocations must not both load the same
    prior state and independently save over each other's mutations -- the
    second transaction's flock() call must block until the first's `with`
    block has exited (and saved), not run concurrently with it (Codex
    review: "Serialize concurrent durable-state updates")."""
    path = tmp_path / "state.json"
    dca_runner.save_strategy_state(StrategyState(), path)

    first_holds_lock = threading.Event()
    let_first_finish = threading.Event()

    def hold_first():
        with dca_runner.state_transaction(path) as state:
            state.record_buy("AAPL", date(2026, 7, 28), 2.0)
            first_holds_lock.set()
            let_first_finish.wait(timeout=5)

    first_thread = threading.Thread(target=hold_first)
    first_thread.start()
    assert first_holds_lock.wait(timeout=5)

    second_saw_first_buy = []

    def run_second():
        with dca_runner.state_transaction(path) as state:
            # If the lock actually serialized the two transactions, the
            # first's AAPL buy must already be visible here.
            second_saw_first_buy.append(state.day_notional(date(2026, 7, 28)))
            state.record_buy("MSFT", date(2026, 7, 28), 3.0)

    second_thread = threading.Thread(target=run_second)
    second_thread.start()
    time_module.sleep(0.1)  # give the second thread a chance to (wrongly) race ahead
    let_first_finish.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert second_saw_first_buy == [pytest.approx(2.0)]
    final = dca_runner.load_strategy_state(path)
    assert final.day_notional(date(2026, 7, 28)) == pytest.approx(5.0)


def _fake_msvcrt(locking_side_effects):
    """A stand-in ``msvcrt`` module: ``locking()`` pops one entry from
    ``locking_side_effects`` per call, raising it if it's an exception or
    returning it otherwise."""
    calls = []

    def locking(fd, mode, nbytes):
        calls.append((fd, mode, nbytes))
        effect = locking_side_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    fake: Any = types.ModuleType("msvcrt")
    fake.locking = locking
    fake.LK_LOCK = 1
    fake.LK_UNLCK = 0
    fake.calls = calls
    return fake


def test_acquire_exclusive_lock_retries_only_the_documented_windows_contention_error(
    monkeypatch,
):
    """msvcrt.locking() raises PermissionError -- not a permanent failure --
    when its own internal ~10s retry window expires without acquiring the
    lock; that specific, documented condition must keep retrying until the
    lock is free, matching fcntl.flock(LOCK_EX)'s indefinite-block behavior
    on POSIX."""
    monkeypatch.setattr(dca_runner.os, "name", "nt")
    fake = _fake_msvcrt([PermissionError(), PermissionError(), None])
    monkeypatch.setitem(sys.modules, "msvcrt", fake)

    class _FakeLockFile:
        def seek(self, offset):
            pass

        def fileno(self):
            return 3

    dca_runner._acquire_exclusive_lock(_FakeLockFile())
    assert len(fake.calls) == 3


def test_acquire_exclusive_lock_propagates_a_non_contention_windows_error(monkeypatch):
    """A bare `except OSError` around msvcrt.locking() also caught a
    PERMANENT failure (a bad descriptor, a corrupted filesystem, ...) the
    same way it caught the documented transient PermissionError, looping
    forever instead of propagating so state_transaction() can fail closed
    and the scheduled run can persist evidence and alert (Codex review:
    "Stop retrying permanent Windows lock errors")."""
    monkeypatch.setattr(dca_runner.os, "name", "nt")
    fake = _fake_msvcrt([OSError("disk I/O error")])
    monkeypatch.setitem(sys.modules, "msvcrt", fake)

    class _FakeLockFile:
        def seek(self, offset):
            pass

        def fileno(self):
            return 3

    with pytest.raises(OSError):
        dca_runner._acquire_exclusive_lock(_FakeLockFile())
    assert len(fake.calls) == 1  # never retried


# ===========================================================================
# B. Gate 0 -- market day
# ===========================================================================


@pytest.mark.parametrize("day", [date(2026, 8, 1), date(2026, 8, 2)])  # Sat, Sun
def test_gate0_weekend_is_closed(day):
    assert dca_runner.market_day_status(day).is_closed


def test_gate0_nyse_holiday_is_closed():
    """2026-09-07 Labor Day, from config/market_calendar.json."""
    assert dca_runner.market_day_status(date(2026, 9, 7)).is_closed


def test_gate0_normal_weekday_is_open():
    status = dca_runner.market_day_status(date(2026, 7, 28))  # Tuesday
    assert not status.is_closed
    assert not status.is_early_close


def test_gate0_early_close_day_is_open_but_flagged():
    """2026-11-27 closes 13:00 ET."""
    status = dca_runner.market_day_status(date(2026, 11, 27))
    assert not status.is_closed
    assert status.is_early_close


def test_gate0_early_close_silences_slots_at_or_after_1300(early_close_slot):
    """market_calendar.json: 'any slot at or after 1:00 PM ET must end silently'."""
    status = dca_runner.market_day_status(date(2026, 11, 27))
    assert status.slot_should_end_silently(early_close_slot)


def test_gate0_early_close_does_not_silence_morning_slots():
    status = dca_runner.market_day_status(date(2026, 11, 27))
    assert not status.slot_should_end_silently(dca_runner.SLOTS["0935"])


def test_gate0_date_beyond_calendar_coverage_fails_closed():
    """years_covered is [2026, 2027, 2028]. Never guess a holiday schedule."""
    with pytest.raises(dca_runner.CalendarCoverageError):
        dca_runner.market_day_status(date(2029, 3, 15))


def test_gate0_rejects_datetime_instead_of_silently_missing_holiday():
    """Codex review: "Normalize datetime inputs before holiday membership".

    2026-12-25 is a configured holiday closure. A ``datetime`` passed
    directly (instead of a plain ``date``) must never silently report
    ``is_closed=False`` because ``datetime == date`` comparisons never
    match calendar membership sets built from ``date`` objects.
    """
    with pytest.raises(ValueError):
        dca_runner.market_day_status(datetime(2026, 12, 25, 12, tzinfo=dca_runner.ET))


def test_gate0_rejects_datetime_for_early_close_day_too():
    with pytest.raises(ValueError):
        dca_runner.market_day_status(datetime(2026, 11, 27, 12, tzinfo=dca_runner.ET))


@pytest.fixture(params=["1330", "1430", "1530"])
def early_close_slot(request):
    return dca_runner.SLOTS[request.param]


@pytest.fixture(params=sorted(dca_runner.SLOTS))
def all_slots(request):
    """Every registered slot, so a gate test proves it holds for the whole
    seven-slot roster instead of only the market-open (0935) slot that most
    of this file's tests default to via ``_clean_kwargs``."""
    return dca_runner.SLOTS[request.param]


def test_gate0_early_close_does_not_silence_the_midday_slot():
    """slot_should_end_silently compares slot.nominal_et, not window_end_et,
    against 13:00 ET -- a documented, currently-inert gap (Codex review: "End
    the midday window before an early market close"; see the method's
    docstring). 1230's nominal is 12:30, so it pins TODAY's actual behavior
    (not silenced) even though its window technically reaches the 13:00
    early-close cutoff, distinguishing it from early_close_slot's
    1330/1430/1530 (nominal strictly after 13:00, silenced)."""
    status = dca_runner.market_day_status(date(2026, 11, 27))
    assert not status.slot_should_end_silently(dca_runner.SLOTS["1230"])


# ===========================================================================
# C. Gate 1 -- time sanity
# ===========================================================================


def test_gate1_all_seven_slots_are_registered():
    assert set(dca_runner.SLOTS) == {"0935", "1030", "1130", "1230", "1330", "1430", "1530"}


def test_gate1_inside_window_passes():
    spec = dca_runner.SLOTS["0935"]
    assert dca_runner.gate_time_sanity(datetime(2026, 7, 28, 9, 35, tzinfo=ET), spec)


def test_gate1_jittered_fire_time_still_passes():
    """Observed jitterSeconds=494 moves the 9:35 fire to 09:43:14 ET."""
    spec = dca_runner.SLOTS["0935"]
    assert dca_runner.gate_time_sanity(datetime(2026, 7, 28, 9, 43, 14, tzinfo=ET), spec)


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 28, 9, 29, 59, tzinfo=ET),  # a second early
        datetime(2026, 7, 28, 10, 0, 1, tzinfo=ET),   # a second late
        datetime(2026, 7, 28, 15, 30, tzinfo=ET),     # another slot's window
    ],
)
def test_gate1_outside_window_fails(when):
    """An off-schedule firing is a scheduler fault, never a late run."""
    assert not dca_runner.gate_time_sanity(when, dca_runner.SLOTS["0935"])


def test_gate1_naive_datetime_is_rejected_rather_than_assumed():
    """Timezone ambiguity around the 2026-11-01 EDT->EST switch must not be guessed."""
    with pytest.raises(ValueError):
        dca_runner.gate_time_sanity(datetime(2026, 7, 28, 9, 35), dca_runner.SLOTS["0935"])


def test_gate1_window_bounds_hold_for_every_slot_not_just_0935(all_slots):
    """The window checks above only ever exercised the 0935 slot. Each of the
    other six slots has its own, different window_start_et/window_end_et
    (dca_runner.py's SLOTS table), so passing for 0935 alone does not prove
    gate_time_sanity's inclusive-start/exclusive-end arithmetic generalizes.
    """
    day = date(2026, 7, 28)
    start = datetime.combine(day, time(*all_slots.window_start_et), tzinfo=ET)
    end = datetime.combine(day, time(*all_slots.window_end_et), tzinfo=ET)

    assert dca_runner.gate_time_sanity(start, all_slots)  # inclusive start
    assert dca_runner.gate_time_sanity(end - timedelta(seconds=1), all_slots)
    assert not dca_runner.gate_time_sanity(start - timedelta(seconds=1), all_slots)
    assert not dca_runner.gate_time_sanity(end, all_slots)  # exclusive end


def test_gate1_naive_datetime_is_rejected_for_every_slot(all_slots):
    with pytest.raises(ValueError):
        dca_runner.gate_time_sanity(datetime(2026, 7, 28, *all_slots.nominal_et), all_slots)


def test_gate1_excludes_the_market_close_instant_for_the_final_slot():
    """An inclusive closing bound let the final (1530) slot's window accept
    `now` at exactly 16:00:00 ET, the regular-session close itself, so a
    placement-time recheck could still call the broker right at (or a
    moment after) the close (Codex review: "Exclude the market-close
    endpoint from slot windows")."""
    spec = dca_runner.SLOTS["1530"]
    assert dca_runner.gate_time_sanity(datetime(2026, 7, 28, 15, 59, 59, tzinfo=ET), spec)
    assert not dca_runner.gate_time_sanity(datetime(2026, 7, 28, 16, 0, 0, tzinfo=ET), spec)


def test_assess_aborts_on_a_naive_assessment_clock():
    """now.astimezone(ET) silently interprets a naive value in the host's
    local timezone rather than raising, so a naive `now` (e.g. the common
    datetime.now() with no tzinfo) would otherwise reach gate_time_sanity(),
    which explicitly rejects it -- with nothing before it to catch that
    ValueError, escaping assess()'s never-raises contract before Gate 9
    could write evidence or alert (Codex review: "Convert a naive
    assessment clock into an abort verdict")."""
    r = dca_runner.assess(**_clean_kwargs(now=datetime(2026, 7, 28, 9, 35)))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_now", [None, "2026-07-28T09:35:00-04:00", 1753702500.0])
def test_assess_aborts_on_a_non_datetime_assessment_clock(bad_now):
    """now.astimezone(ET) assumes `now` IS a datetime -- an injected
    scheduler/time layer supplying None, an ISO timestamp string, or a
    bare epoch number makes that call raise a bare AttributeError before
    out() is even defined, escaping assess()'s never-raises contract with
    no ABORT payload at all (the later naive-clock check only handles
    actual datetime instances) (Codex review: "Validate the assessment
    clock before converting it")."""
    r = dca_runner.assess(**_clean_kwargs(now=bad_now))
    assert r["verdict"] == "ABORT"
    assert r["trading_day"] is None


# ===========================================================================
# D. Gate 3 -- trading window
# ===========================================================================


def test_gate3_inside_approved_window_passes():
    assert dca_runner.gate_trading_window(date(2026, 7, 28))


def test_gate3_past_end_date_fails():
    """config/trading_window.json end_date is 2026-10-30."""
    assert not dca_runner.gate_trading_window(date(2026, 10, 31))


def test_gate3_before_start_date_fails():
    assert not dca_runner.gate_trading_window(date(2026, 7, 1))


@pytest.mark.parametrize(
    "bad_trading_window_status",
    [
        FileNotFoundError("trading_window.json"),
        json.JSONDecodeError("Expecting value", "", 0),
        KeyError("start_date"),
        TypeError("argument of type 'int' is not iterable"),
    ],
)
def test_assess_converts_an_unreadable_trading_window_into_an_abort_verdict(
    monkeypatch, bad_trading_window_status
):
    """gate_trading_window() reads config/trading_window.json and parses its
    dates directly: a missing file, malformed JSON, or an invalid date string
    all mean the config cannot be trusted, the same as Gate 0's
    market_calendar.json handling. Without this, a deployment/configuration
    failure would raise past assess() before Gate 9 could write evidence or
    alert (Codex review: "Convert trading-window config failures into
    ABORT")."""

    def _raise(day):
        raise bad_trading_window_status

    monkeypatch.setattr(dca_runner, "gate_trading_window", _raise)
    r = dca_runner.assess(**_clean_kwargs())
    assert r["verdict"] == "ABORT"


# ===========================================================================
# E. Gate 4 -- sole-executor interlock
# ===========================================================================


def test_gate4_empty_window_passes():
    spec = dca_runner.SLOTS["0935"]
    others = [_order("AAPL", 2.0, _utc(2026, 7, 28, 16, 19))]  # 12:19 ET, different slot
    assert dca_runner.gate_sole_executor(others, spec, date(2026, 7, 28)) == []


def test_gate4_any_order_in_window_trips_regardless_of_placed_agent():
    """A manual order inside the slot window is still a second writer."""
    spec = dca_runner.SLOTS["0935"]
    orders = [_order("AAPL", 2.0, _utc(2026, 7, 28, 13, 45), placed_agent="user")]
    assert dca_runner.gate_sole_executor(orders, spec, date(2026, 7, 28))


def test_gate4_matches_on_created_at_not_fill_time():
    """Submission time is what identifies the writer; fills can lag arbitrarily."""
    spec = dca_runner.SLOTS["0935"]
    order = _order("AAPL", 2.0, _utc(2026, 7, 28, 13, 45), state="filled")
    assert dca_runner.gate_sole_executor([order], spec, date(2026, 7, 28))


@pytest.mark.parametrize(
    "created",
    [
        _utc(2026, 7, 28, 13, 29, 59),  # one second before 13:30 UTC
        _utc(2026, 7, 28, 14, 0, 1),    # one second after 14:00 UTC
    ],
)
def test_gate4_orders_just_outside_boundary_do_not_trip(created):
    spec = dca_runner.SLOTS["0935"]
    assert dca_runner.gate_sole_executor([_order("AAPL", 2.0, created)], spec, date(2026, 7, 28)) == []


def test_gate4_ignores_orders_from_other_days():
    spec = dca_runner.SLOTS["0935"]
    yesterday = _order("AAPL", 2.0, _utc(2026, 7, 27, 13, 45))
    assert dca_runner.gate_sole_executor([yesterday], spec, date(2026, 7, 28)) == []


def test_gate4_window_boundaries_hold_for_every_slot_not_just_0935(all_slots):
    """The boundary tests above only ever exercised the 0935 slot's own UTC
    window. gate_sole_executor's window is inclusive on both ends (unlike
    Gate 1's exclusive-end gate_time_sanity), so pin that per-slot rather
    than assuming it generalizes from one slot's pass/fail cases."""
    day = date(2026, 7, 28)
    start, end = all_slots.window_bounds_utc(day)

    assert dca_runner.gate_sole_executor([_order("AAPL", 2.0, start)], all_slots, day)
    assert dca_runner.gate_sole_executor([_order("AAPL", 2.0, end)], all_slots, day)
    assert dca_runner.gate_sole_executor(
        [_order("AAPL", 2.0, start - timedelta(seconds=1))], all_slots, day
    ) == []
    assert dca_runner.gate_sole_executor(
        [_order("AAPL", 2.0, end + timedelta(seconds=1))], all_slots, day
    ) == []


def test_assess_serializes_a_colliding_orders_non_string_symbol():
    """gate_sole_executor()'s row-shape check only confirms each row IS a
    BrokerOrder instance, never that its externally-supplied .symbol is
    itself JSON-native (e.g. a caller-constructed BrokerOrder with a date
    object as symbol). assess()'s ABORT payload is documented as
    JSON-serialisable; a raw non-native symbol here would still let
    assess() produce its intended ABORT, but json.dumps()-ing that payload
    downstream would raise TypeError, preventing Gate 9 from ever
    persisting the collision evidence or alerting (Codex review:
    "Serialize collision symbols before returning"). A non-string symbol is
    now reported only by type, not stringified, since str()-ing an
    arbitrary object made it JSON-safe but not secret-safe (Codex review:
    "Redact malformed symbols from collision evidence")."""
    colliding = _order(date(2020, 1, 1), 2.0, _utc(2026, 7, 28, 13, 45))
    r = dca_runner.assess(**_clean_kwargs(orders=[colliding]))
    assert r["verdict"] == "ABORT"
    assert r["colliding_orders"] == ["<non-string symbol, type=date>"]
    json.dumps(r)  # must not raise


def test_assess_redacts_a_secret_carrying_colliding_orders_symbol():
    """A malformed BrokerOrder.symbol in Gate 4's collision evidence can be
    arbitrary connector data -- a dict carrying an access token or account
    identifier, for instance -- rather than a real ticker. This ABORT
    payload is exactly what gets persisted as run evidence and sent through
    alert channels, so only a safe classification (the value's type) may
    appear, never the value's contents (Codex review: "Redact malformed
    symbols from collision evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    colliding = _order(
        {"account_number": secret_account_number, "token": "shh"}, 2.0, _utc(2026, 7, 28, 13, 45)
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[colliding]))
    assert r["verdict"] == "ABORT"
    assert r["colliding_orders"] == ["<non-string symbol, type=dict>"]
    serialized = json.dumps(r)
    assert secret_account_number not in serialized
    assert "shh" not in serialized


def test_assess_gate4_grace_period_defaults_to_off_and_still_aborts():
    """Regression guard for the new sole_executor_grace_period_active kwarg:
    leaving it unset (the default) must reproduce today's ABORT behavior
    byte-for-byte on the reason and colliding_orders fields. Every existing
    Gate 4 caller (all seven scheduled prompts, today) omits this kwarg
    entirely, so its default must be a strict no-op -- proposed 2026-08-09
    per Geoff's request to let a same-window collision pass through when
    another AI (Perplexity) may be trading the same Robinhood account this
    week, without weakening Gate 4 for every other caller or every other
    week."""
    colliding = _order("AAPL", 2.0, _utc(2026, 7, 28, 13, 45))
    r = dca_runner.assess(**_clean_kwargs(orders=[colliding]))
    assert r["verdict"] == "ABORT"
    assert r["reason"] == "orders already present in the 0935 ET window, a second executor may be live"
    assert r["colliding_orders"] == ["AAPL"]
    assert "gate4_grace_period_applied" not in r


def test_assess_gate4_grace_period_allows_proceed_despite_collision():
    """With sole_executor_grace_period_active=True, a same-window collision
    that would normally ABORT instead lets the run continue to Gate 6 as
    though Gate 4 passed -- but the collision is never silently dropped:
    the returned payload still carries gate4_grace_period_applied=True and
    the same colliding_orders evidence Gate 9 would otherwise have alerted
    on, so a human reviewing run artifacts this week can see a grace-period
    override was applied rather than assuming Gate 4 found nothing. This is
    a caller-supplied flag (mirroring buying_power_usd,
    weekly_rollover_reserve_usd): assess() itself never reads config or a
    clock to decide whether the grace period is active, and never silently
    re-derives an expiry -- the caller (executor prompt / scheduler) is
    responsible for checking config/scheduled_tasks.json's dated grace
    window before passing True, keeping assess() pure and unit-testable."""
    colliding = _order("AAPL", 2.0, _utc(2026, 7, 28, 13, 45))
    r = dca_runner.assess(
        **_clean_kwargs(
            orders=[colliding],
            candidates=[("MSFT", 2.0)],
            sole_executor_grace_period_active=True,
        )
    )
    assert r["verdict"] == "PROCEED"
    assert r["gate4_grace_period_applied"] is True
    assert r["gate4_collisions"] == ["AAPL"]
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["MSFT"]


@pytest.mark.parametrize("placed_agent", [None, "unknown-executor", "user"])
def test_assess_gate4_grace_period_does_not_override_non_agentic_collision(placed_agent):
    """The grace override is scoped to the recognized compatible executor."""
    colliding = _order(
        "AAPL", 2.0, _utc(2026, 7, 28, 13, 45), placed_agent=placed_agent
    )
    r = dca_runner.assess(
        **_clean_kwargs(
            orders=[colliding],
            candidates=[("MSFT", 2.0)],
            sole_executor_grace_period_active=True,
        )
    )
    assert r["verdict"] == "ABORT"
    assert r["colliding_orders"] == ["AAPL"]
    assert "gate4_grace_period_applied" not in r


def test_assess_gate4_grace_period_aborts_mixed_compatible_and_rogue_window():
    """Any rogue signal wins even when a compatible executor is also present."""
    orders = [
        _order("AAPL", 2.0, _utc(2026, 7, 28, 13, 45), placed_agent="agentic"),
        _order("MSFT", 2.0, _utc(2026, 7, 28, 13, 46), placed_agent="rogue"),
    ]
    r = dca_runner.assess(
        **_clean_kwargs(
            orders=orders,
            candidates=[("NVDA", 2.0)],
            sole_executor_grace_period_active=True,
        )
    )
    assert r["verdict"] == "ABORT"
    assert r["colliding_orders"] == ["AAPL", "MSFT"]
    assert "gate4_grace_period_applied" not in r


def test_assess_gate4_grace_period_flag_alone_does_not_fabricate_a_collision():
    """The grace-period fields must only appear when Gate 4 actually found a
    collision -- a run with the flag set but an empty window must look
    identical to today's plain PROCEED (no phantom gate4_collisions: []
    noise on every ordinary run for the rest of the week)."""
    r = dca_runner.assess(
        **_clean_kwargs(candidates=[("MSFT", 2.0)], sole_executor_grace_period_active=True)
    )
    assert r["verdict"] == "PROCEED"
    assert "gate4_grace_period_applied" not in r
    assert "gate4_collisions" not in r


# ===========================================================================
# F. Gate 6 -- budget, computed from broker history
# ===========================================================================
#
# Policy chosen 2026-07-28: COUNT EVERYTHING. Every order counts toward the
# shared daily/weekly cap regardless of placed_agent, so manual buying and
# automated buying draw on one pool.


def test_gate6_day_total_counts_manual_and_agentic_alike():
    orders = [
        _order("AAPL", 20.0, _utc(2026, 7, 28, 16, 13), placed_agent="user"),
        _order("MSFT", 5.0, _utc(2026, 7, 28, 16, 14), placed_agent="agentic"),
    ]
    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.day_notional_usd == pytest.approx(25.0)


def test_gate6_week_total_uses_iso_week_monday_start():
    """Mon 2026-07-27 and Tue 2026-07-28 are the same ISO week."""
    orders = [
        _order("AAPL", 173.50, _utc(2026, 7, 27, 15, 36)),
        _order("MSFT", 297.00, _utc(2026, 7, 28, 16, 13)),
    ]
    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.week_notional_usd == pytest.approx(470.50)
    assert snap.day_notional_usd == pytest.approx(297.00)


def test_gate6_prior_week_orders_excluded_from_week_total():
    orders = [
        _order("AAPL", 50.0, _utc(2026, 7, 24, 15, 0)),  # Friday, prior ISO week
        _order("MSFT", 10.0, _utc(2026, 7, 28, 16, 0)),
    ]
    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.week_notional_usd == pytest.approx(10.0)


def test_gate6_sell_orders_never_counted_or_generated():
    """Accumulate-only. A sell in history is an anomaly, not budget consumption."""
    orders = [_order("AAPL", 100.0, _utc(2026, 7, 28, 16, 0), side="sell")]
    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.day_notional_usd == pytest.approx(0.0)


@pytest.mark.parametrize("dead_state", ["cancelled", "canceled", "rejected", "failed", "voided"])
def test_gate6_terminal_failed_orders_do_not_consume_budget(dead_state):
    orders = [_order("AAPL", 10.0, _utc(2026, 7, 28, 16, 0), state=dead_state)]
    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.day_notional_usd == pytest.approx(0.0)


@pytest.mark.parametrize("dead_state", ["cancelled", "canceled", "rejected", "failed", "voided"])
def test_gate6_a_terminal_order_with_a_partial_fill_counts_the_executed_portion(dead_state):
    """A buy that was partially filled and then cancelled still spent real
    money on the filled portion. dollar_amount may still reflect the
    original, larger requested size, so only executed_dollar_amount is
    trustworthy spend for a dead order (Codex review: "Account for
    partially filled cancelled buys")."""
    orders = [
        _order(
            "AAPL", 10.0, _utc(2026, 7, 28, 16, 0),
            state=dead_state, executed_dollar_amount=3.0,
        )
    ]
    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.day_notional_usd == pytest.approx(3.0)  # executed, not the requested $10
    assert snap.week_notional_usd == pytest.approx(3.0)


def test_gate6_a_terminal_order_with_no_fill_still_consumes_no_budget():
    orders = [
        _order(
            "AAPL", 10.0, _utc(2026, 7, 28, 16, 0),
            state="cancelled", executed_dollar_amount=None,
        )
    ]
    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.day_notional_usd == pytest.approx(0.0)


def test_a_partially_filled_cancelled_order_blocks_another_same_day_buy(monkeypatch):
    """The same partial-fill notional that must count toward budget must also
    count toward the one-order-per-ticker-per-day dedup, or a cancelled
    partial fill would let assess() plan a second same-day buy for the same
    ticker (Codex review: "Account for partially filled cancelled buys")."""
    # first-slot-wins is now OFF by default (config/budgets.json order_rules.one_order_per_ticker_per_day=false, set 2026-07-31). This test covers that rule, so it pins it ON explicitly rather than depending on the shipped default.
    monkeypatch.setattr(dca_runner, "ONE_ORDER_PER_TICKER_PER_DAY", True)
    earlier = [
        _order(
            "AAPL", 10.0, _utc(2026, 7, 28, 13, 0),  # 9:00 AM ET, before the 9:43 AM "now" below
            state="cancelled", executed_dollar_amount=3.0,
        )
    ]
    r = dca_runner.assess(**_clean_kwargs(
        orders=earlier, candidates=[("AAPL", 2.0), ("MSFT", 2.0)]
    ))
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["MSFT"]
    assert ["AAPL", dca_runner.REFUSED_ALREADY_BOUGHT_TODAY] in r["plan"]["refused"]


@pytest.mark.parametrize("padded_state", ["REJECTED", " cancelled "])
def test_a_case_or_whitespace_variant_terminal_state_is_normalized_before_budget_accounting(padded_state):
    """A case- or whitespace-variant terminal state ("REJECTED",
    " cancelled ") must still be recognized as dead, not treated as live
    and charged its full requested dollar_amount instead of its real (zero,
    since it has no fill) executed amount -- that can falsely exhaust the
    shared cap and skip the rest of the day's valid buys (Codex review:
    "Normalize broker-history states before budget accounting")."""
    earlier = [
        _order(
            "MSFT", 180.0, _utc(2026, 7, 28, 13, 0),
            state=padded_state, executed_dollar_amount=None,
        )
    ]
    r = dca_runner.assess(**_clean_kwargs(orders=earlier, candidates=[("AAPL", 2.0)]))
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["AAPL"]


def test_an_expired_broker_order_with_no_fill_is_classified_as_dead():
    """near_close_dca's canonical terminal-state set includes "expired", but a
    hand-copied broker-history set had omitted it: an expired buy with no
    fill would otherwise be treated as live and charged its full requested
    dollar_amount, falsely exhausting the shared daily cap and blocking a
    later valid buy (Codex review: "Classify expired broker orders as
    dead")."""
    earlier = [
        _order(
            "MSFT", 180.0, _utc(2026, 7, 28, 13, 0),
            state="expired", executed_dollar_amount=None,
        )
    ]
    r = dca_runner.assess(**_clean_kwargs(orders=earlier, candidates=[("AAPL", 2.0)]))
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["AAPL"]


@pytest.mark.parametrize("weird_side", ["BUY", " buy ", "Buy"])
def test_an_uppercase_or_padded_buy_side_still_counts_and_blocks_resubmission(weird_side, monkeypatch):
    """An uppercase or whitespace-padded side must not fail the exact-match
    comparison and be silently excluded from budget accounting and the
    same-day ticker interlock, treating real committed spend as zero (Codex
    review: "Normalize broker order sides before budget filtering")."""
    # pins first-slot-wins ON; see the note on the partially-filled-cancelled test.
    monkeypatch.setattr(dca_runner, "ONE_ORDER_PER_TICKER_PER_DAY", True)
    earlier = [_order("AAPL", 10.0, _utc(2026, 7, 28, 13, 0), side=weird_side)]
    r = dca_runner.assess(**_clean_kwargs(
        orders=earlier, candidates=[("AAPL", 2.0), ("MSFT", 2.0)]
    ))
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["MSFT"]
    assert ["AAPL", dca_runner.REFUSED_ALREADY_BOUGHT_TODAY] in r["plan"]["refused"]


def test_an_unrecognized_broker_side_fails_closed():
    """A side that is neither a recognized "buy" nor "sell" spelling is
    untrustworthy data, not automatically "not a buy" -- silently excluding
    it could hide real spend the same way trusting it blindly would."""
    earlier = [_order("AAPL", 10.0, _utc(2026, 7, 28, 13, 0), side="hold")]
    r = dca_runner.assess(**_clean_kwargs(orders=earlier))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_state", [[], {}])
def test_a_broker_order_with_an_unhashable_state_fails_closed(bad_state):
    """An unhashable state (a list, a dict) must raise ValueError, not a bare
    TypeError from the frozenset `in` membership test -- assess()'s Gate 6
    only catches ValueError/BudgetDataError, so a TypeError would escape its
    never-raises contract before Gate 9 could write evidence or alert (Codex
    review: "Convert malformed broker states into an abort verdict")."""
    bad_order = _order("AAPL", 10.0, _utc(2026, 7, 28, 13, 0), state=bad_state)
    with pytest.raises(ValueError):
        dca_runner.budget_snapshot([bad_order], date(2026, 7, 28))
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_symbol", [None, True])
def test_assess_aborts_when_a_broker_order_has_a_malformed_symbol(bad_symbol):
    """A non-string symbol (None, a bool) on a same-day buy raises a bare
    AttributeError from _normalize_ticker()'s .strip()/.upper() before
    assess() can convert it into the documented ABORT payload, escaping its
    never-raises contract before Gate 9 could write evidence or alert
    (Codex review: "Convert malformed broker symbols into an abort
    verdict")."""
    bad_order = dca_runner.BrokerOrder(
        symbol=bad_symbol, side="buy", dollar_amount=10.0,
        created_at=_utc(2026, 7, 28, 13, 0),
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"


def test_assess_redacts_a_malformed_broker_symbol_from_the_abort_reason():
    """A broker wrapper's BrokerOrder.symbol can be arbitrary connector data
    -- e.g. a dict carrying an account identifier or token -- rather than a
    real ticker string. _normalize_ticker()'s raised ValueError embeds that
    value's full repr, and this ABORT reason is exactly what gets persisted
    as run evidence and sent through alert channels; only a safe
    classification (the exception's type name) may appear there (Codex
    review: "Redact malformed broker symbols from abort evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    bad_order = dca_runner.BrokerOrder(
        symbol={"account_number": secret_account_number, "token": "shh"},
        side="buy", dollar_amount=10.0,
        created_at=_utc(2026, 7, 28, 13, 0),
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "shh" not in r["reason"]
    assert "ValueError" in r["reason"]


@pytest.mark.parametrize("blank_symbol", ["", "   "])
def test_assess_aborts_when_a_broker_order_has_a_blank_symbol(blank_symbol):
    """A symbol that normalizes to "" would otherwise let the same-day ticker
    interlock (bought_today) record an anonymous "" entry instead of the
    order's real ticker, so a genuine same-day buy whose wrapper lost the
    symbol would not block a second live order for that same, unverifiable
    ticker (Codex review: "Reject normalized-empty broker symbols")."""
    bad_order = dca_runner.BrokerOrder(
        symbol=blank_symbol, side="buy", dollar_amount=10.0,
        created_at=_utc(2026, 7, 28, 13, 0),
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_amount", [float("nan"), float("inf"), 0.0, -5.0])
def test_gate6_broker_history_with_invalid_notional_fails_closed(bad_amount):
    """A malformed broker notional must abort rather than being silently
    excluded from the sum or summed as-is.

    Summing NaN makes every downstream cap comparison False, silently
    bypassing every cap; excluding the row instead understates real spend --
    the true amount is unknown, not zero, and treating it as zero can
    authorize a batch beyond the actual cap (Codex review: "Abort on
    malformed broker notionals").
    """
    orders = [
        _order("AAPL", bad_amount, _utc(2026, 7, 28, 16, 0)),
        _order("MSFT", 10.0, _utc(2026, 7, 28, 16, 1)),
    ]
    with pytest.raises(dca_runner.BudgetDataError):
        dca_runner.budget_snapshot(orders, date(2026, 7, 28))


def test_assess_redacts_a_malformed_broker_notional_from_the_abort_reason():
    """budget_snapshot()'s raised BudgetDataError embeds the offending
    order's symbol and amount via repr(), and either can be arbitrary
    connector data -- a dict carrying an access token or account identifier,
    for instance. This ABORT reason is exactly what gets persisted as run
    evidence and sent through alert channels, so only a safe classification
    (the exception's type name) may appear there (Codex review: "Redact
    malformed broker notionals from abort evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    bad_order = _order(
        "AAPL", {"account_number": secret_account_number, "token": "shh"}, _utc(2026, 7, 28, 16, 0)
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "shh" not in r["reason"]
    assert "BudgetDataError" in r["reason"]


@pytest.mark.parametrize("bad_amount", [float("nan"), float("-inf"), -5.0])
def test_gate6_dead_order_with_malformed_executed_amount_fails_closed(bad_amount):
    """A cancelled/rejected order's ``executed_dollar_amount`` being NaN or
    negative must abort budget_snapshot(), not read as "no fill" and get
    silently treated as zero exposure.

    ``counts_toward_budget``'s own ``> 0`` comparison would previously read a
    malformed value as False (no fill) and filter the row out before
    ``budget_snapshot()``'s fail-closed check ever saw it, treating an
    unknown fill as zero spend (Codex review: "Reject malformed terminal-fill
    amounts before filtering")."""
    orders = [
        _order(
            "AAPL", 10.0, _utc(2026, 7, 28, 16, 0),
            state="cancelled", executed_dollar_amount=bad_amount,
        ),
    ]
    with pytest.raises(dca_runner.BudgetDataError):
        dca_runner.budget_snapshot(orders, date(2026, 7, 28))


@pytest.mark.parametrize("bad_amount", [True, False, "10.0", None])
def test_gate6_non_numeric_broker_notional_fails_closed(bad_amount):
    """A JSON boolean survives BrokerOrder construction as a Python bool, and
    ``math.isfinite(True)`` succeeds since bool is an int subclass -- summing
    it as real spend would understate an unknown true amount as exactly $1.
    A string or ``None`` instead raised an uncaught ``TypeError`` from
    ``math.isfinite()``, escaping the documented ``BudgetDataError``/``ABORT``
    path entirely (Codex review: "Reject non-number broker notionals before
    summing")."""
    orders = [_order("AAPL", bad_amount, _utc(2026, 7, 28, 16, 0))]
    with pytest.raises(dca_runner.BudgetDataError):
        dca_runner.budget_snapshot(orders, date(2026, 7, 28))


@pytest.mark.parametrize("bad_amount", ["bad", [1.0]])
def test_gate6_non_numeric_terminal_fill_amount_fails_closed(bad_amount):
    """A canceled order's externally normalized executed_dollar_amount being
    a string (or other non-number) must not raise a bare TypeError from
    math.isfinite() inside counts_toward_budget() before budget_snapshot()
    ever reaches its own BudgetDataError validation -- that would let
    assess() raise instead of returning the documented ABORT payload,
    preventing Gate 9 evidence and alerts (Codex review: "Validate
    terminal-fill types before calling isfinite")."""
    orders = [
        _order(
            "AAPL", 10.0, _utc(2026, 7, 28, 16, 0),
            state="cancelled", executed_dollar_amount=bad_amount,
        ),
    ]
    with pytest.raises(dca_runner.BudgetDataError):
        dca_runner.budget_snapshot(orders, date(2026, 7, 28))


def test_gate6_oversized_broker_notional_fails_closed():
    """A syntactically valid but astronomically large JSON integer (e.g.
    10**1000, which json.loads parses as a plain Python int of unbounded
    size) survives the numeric-type check but makes math.isfinite() raise
    OverflowError rather than return False, previously escaping
    budget_snapshot()'s documented BudgetDataError/ABORT path entirely
    instead of failing closed the same way every other unusable notional
    does (Codex review: "Convert oversized broker integers into a
    budget-data abort")."""
    orders = [_order("AAPL", 10**1000, _utc(2026, 7, 28, 16, 0))]
    with pytest.raises(dca_runner.BudgetDataError):
        dca_runner.budget_snapshot(orders, date(2026, 7, 28))


def test_gate6_oversized_terminal_fill_amount_fails_closed():
    """The same oversized-integer case, reached through a dead order's
    executed_dollar_amount inside counts_toward_budget() instead of the live
    dollar_amount path (Codex review: "Convert oversized broker integers
    into a budget-data abort")."""
    orders = [
        _order(
            "AAPL", 10.0, _utc(2026, 7, 28, 16, 0),
            state="cancelled", executed_dollar_amount=10**1000,
        ),
    ]
    with pytest.raises(dca_runner.BudgetDataError):
        dca_runner.budget_snapshot(orders, date(2026, 7, 28))


def test_gate6_accumulated_broker_totals_that_overflow_fail_closed():
    """Each individual amount can pass its own finiteness check while the
    ACCUMULATED day/week total still overflows to inf: float addition does
    not raise on overflow the way int-to-float conversion does. PROCEEDing
    with an infinite budget value is the wrong verdict for malformed
    history, and Infinity is not valid standard JSON for the required run
    evidence either way (Codex review: "Reject broker-history totals that
    overflow to infinity")."""
    orders = [
        _order("AAPL", 1e308, _utc(2026, 7, 28, 16, 0)),
        _order("MSFT", 1e308, _utc(2026, 7, 28, 16, 1)),
    ]
    with pytest.raises(dca_runner.BudgetDataError):
        dca_runner.budget_snapshot(orders, date(2026, 7, 28))


def test_gate4_naive_created_at_is_rejected_rather_than_assumed():
    """Mirrors gate_time_sanity: a naive broker timestamp must fail closed."""
    spec = dca_runner.SLOTS["0935"]
    naive_order = dca_runner.BrokerOrder(
        symbol="AAPL", side="buy", dollar_amount=2.0,
        created_at=datetime(2026, 7, 28, 13, 45),  # no tzinfo
    )
    with pytest.raises(ValueError):
        dca_runner.gate_sole_executor([naive_order], spec, date(2026, 7, 28))


def test_gate6_naive_created_at_is_rejected_rather_than_assumed():
    naive_order = dca_runner.BrokerOrder(
        symbol="AAPL", side="buy", dollar_amount=2.0,
        created_at=datetime(2026, 7, 28, 16, 0),  # no tzinfo
    )
    with pytest.raises(ValueError):
        dca_runner.budget_snapshot([naive_order], date(2026, 7, 28))


@pytest.mark.parametrize("bad_created_at", ["2026-07-28T13:45:00Z", None])
def test_gate4_non_datetime_created_at_is_rejected_as_malformed(bad_created_at):
    """A string or None created_at must raise ValueError (as a naive
    datetime does), not a bare AttributeError from accessing .tzinfo on a
    non-datetime -- assess()'s Gate 4 only catches ValueError, so an
    AttributeError would escape its never-raises contract before Gate 9
    could write evidence or alert (Codex review: "Reject non-datetime
    broker timestamps as malformed")."""
    spec = dca_runner.SLOTS["0935"]
    bad_order = dca_runner.BrokerOrder(
        symbol="AAPL", side="buy", dollar_amount=2.0, created_at=bad_created_at,
    )
    with pytest.raises(ValueError):
        dca_runner.gate_sole_executor([bad_order], spec, date(2026, 7, 28))


@pytest.mark.parametrize("bad_created_at", ["2026-07-28T13:45:00Z", None])
def test_assess_aborts_when_a_broker_order_has_a_non_datetime_timestamp(bad_created_at):
    bad_order = dca_runner.BrokerOrder(
        symbol="AAPL", side="buy", dollar_amount=2.0, created_at=bad_created_at,
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"


def test_gate6_reproduces_the_observed_2026_07_28_figures():
    """Regression pin against real broker history read on 2026-07-28.

    26 orders totalling $297.00 on the day; $173.50 the prior day; $470.50 ISO week.
    """
    tue = [2, 2, 3, 2, 18, 5, 4, 16, 9, 18, 20, 18, 16, 14, 18, 20, 10, 10, 6, 16, 20, 2, 20, 2, 20, 6]
    mon = [1, 4, 1, 1, 1, 10, 3.5, 2, 2, 3, 3, 8, 3, 7, 6, 5, 4, 9, 9, 10, 5, 5, 3, 7, 9, 1.5,
           6, 8, 2, 5, 9, 1, 2.5, 4, 1, 1, 1, 4, 1, 5]
    orders = [_order(f"T{i}", amt, _utc(2026, 7, 28, 16, 13), placed_agent="user")
              for i, amt in enumerate(tue)]
    orders += [_order(f"M{i}", amt, _utc(2026, 7, 27, 15, 36)) for i, amt in enumerate(mon)]

    snap = dca_runner.budget_snapshot(orders, date(2026, 7, 28))
    assert snap.day_notional_usd == pytest.approx(297.00)
    assert snap.week_notional_usd == pytest.approx(470.50)


# --- per-order ceiling: refuse, never clamp -------------------------------


def test_gate6_order_above_ceiling_is_refused_not_clamped():
    """$10.00 = 5x the $2.00 base. Clamping to $10 would hide a sizing bug."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 12.50)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert plan.refused == [("AAPL", dca_runner.REFUSED_OVER_PER_ORDER_CAP)]


def test_gate6_order_exactly_at_ceiling_is_allowed():
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 10.00)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.to_place[0].amount_usd == pytest.approx(10.00)


def test_plan_orders_refuses_a_boolean_candidate_amount():
    """plan_orders() is a public, independently-callable entry point
    (docs/live_wiring.md's "direct Python caller" path) that a Candidate can
    reach WITHOUT ever passing through _parse_candidate_rows()'s own
    boolean/type validation. bool is an int subclass, so
    Candidate("AAPL", True) would otherwise pass math.isfinite() and every
    later ladder check as amount_usd=1 (True == 1), producing a real $1
    PlannedOrder for a direct caller instead of the documented
    REFUSED_INVALID_AMOUNT (Codex review: "Reject boolean amounts in direct
    plans")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", True)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert plan.refused == [("AAPL", dca_runner.REFUSED_INVALID_AMOUNT)]


def test_plan_orders_refuses_an_unsortable_candidate_without_crashing_the_batch():
    """plan_orders() is a public, independently-callable entry point that a
    caller-built Candidate can reach WITHOUT ever passing through
    _parse_candidate_rows()'s own type validation. sorted(candidates,
    key=_allocation_priority_key) previously ran before any per-candidate
    validation: a non-numeric amount_usd makes math.isfinite() raise a bare
    TypeError inside the sort key itself, crashing sorted() for the WHOLE
    batch before this function's own controlled REFUSED_INVALID_AMOUNT path
    ever got to run for ANY candidate, not just the malformed one (Codex
    review: "Validate candidates before sorting them")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", "bad"), dca_runner.Candidate("MSFT", 2.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["MSFT"]
    assert ("AAPL", dca_runner.REFUSED_INVALID_AMOUNT) in plan.refused


def test_plan_orders_refuses_an_oversized_integer_candidate_without_crashing_the_batch():
    """A syntactically valid but astronomically large Python int (e.g.
    10**1000) passes the isinstance(x, (int, float)) checks inside
    _allocation_priority_key(), but makes math.isfinite() raise OverflowError
    -- not TypeError/ValueError -- while computing the sort key. Left
    uncaught by _candidate_sortable_for_planning(), that still crashes
    sorted() for the whole batch, exactly the failure mode the prior
    sort-validation fix closed for TypeError/ValueError but missed for
    OverflowError (Codex review: "Catch oversized candidate integers before
    sorting")."""
    plan = dca_runner.plan_orders(
        candidates=[
            dca_runner.Candidate("AAPL", 10**1000),
            dca_runner.Candidate("MSFT", 2.0),
        ],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["MSFT"]
    assert ("AAPL", dca_runner.REFUSED_INVALID_AMOUNT) in plan.refused


def test_gate6_refuses_a_sub_cent_candidate_amount():
    """A non-cent amount like 9.996 must be refused at planning time, not
    just at execute_plan()'s placement-time boundary: every cap/buying-power
    comparison in plan_orders() (and, downstream, assess()'s tier-selection
    sums) uses the raw, unrounded float, but placement sends
    f"{amount_usd:.2f}" -- the rounded cent value the broker actually
    receives. 9.996 rounds to the same cents as a real $10 allocation, so it
    would otherwise authenticate against the ladder and reach to_place, while
    a caller formatting the returned amount as ordinary currency submits
    $10.00, exceeding a $9.996 boundary by the rounding delta (Codex review:
    "Refuse sub-cent candidate notionals during planning")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 9.996)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert plan.refused == [("AAPL", dca_runner.REFUSED_NON_CENT_AMOUNT)]


def test_tier_selection_inputs_excludes_a_sub_cent_candidate():
    """plan_orders() itself refuses a non-cent amount (REFUSED_NON_CENT_AMOUNT)
    and never places it, so it must not count toward planned_spend/high_mult
    either: without this same check, an unplaceable sub-cent row could still
    select a richer tier than the real, placeable plan needs (Codex review:
    "Exclude sub-cent candidates from tier selection")."""
    valid_cands, planned_spend, high_mult = dca_runner._tier_selection_inputs(
        [dca_runner.Candidate("AAPL", 9.996)]
    )
    assert valid_cands == []
    assert planned_spend == 0.0
    assert high_mult == 0


def test_assess_does_not_escalate_the_tier_on_sub_cent_candidates_alone(monkeypatch):
    """Near the $129 Normal->Elevated escalation boundary, a sub-cent row
    that plan_orders() would refuse anyway must not be the thing that tips
    tier selection: 30 real 2x candidates ($4 each, $120 total, real,
    placeable, low-multiplier so high_mult stays 0) stay under $129 on
    their own, but adding one more, sub-cent candidate whose
    (wrongly-counted) amount would push the total to $129.996 must NOT
    escalate to Elevated -- the sub-cent row can never actually be placed,
    so it must never inflate the caps the REST of the batch spends against
    (Codex review: "Exclude sub-cent candidates from tier selection")."""
    # raises the daily order-count cap out of the way: this test is about TIER SELECTION with 30 candidates, and the shipped cap of 8 (circuit_breakers.max_orders_per_day) would otherwise trim to_place to 8 and mask what is being asserted.
    monkeypatch.setattr(dca_runner, "MAX_ORDERS_PER_DAY", 100)
    real_tickers = sorted(dca_runner.ACTIVE_TICKERS)[:30]
    candidates = [(t, 4.0, 2.0) for t in real_tickers] + [("QBTS", 9.996)]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["verdict"] == "PROCEED"
    assert r["budget"]["tier"] == "normal"
    assert sorted(o["ticker"] for o in r["plan"]["to_place"]) == sorted(real_tickers)
    assert ["QBTS", dca_runner.REFUSED_NON_CENT_AMOUNT] in r["plan"]["refused"]


def test_gate6_one_bad_order_does_not_refuse_the_whole_batch():
    """Candidates are walked in allocation-priority order (higher estimated
    multiplier first), so MSFT (99.0 -> ~49.5x) sorts before NVDA (6.0 -> 3x)
    and AAPL (4.0 -> 2x); MSFT alone is refused for being over the per-order
    cap, and does not cost the rest of the batch."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 4.0),
                    dca_runner.Candidate("MSFT", 99.0),
                    dca_runner.Candidate("NVDA", 6.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["NVDA", "AAPL"]
    assert plan.refused == [("MSFT", dca_runner.REFUSED_OVER_PER_ORDER_CAP)]


# --- invalid amounts: refused, never let NaN/negative reach arithmetic ---


@pytest.mark.parametrize("bad_amount", [float("nan"), float("inf"), 0.0, -5.0])
def test_gate6_non_finite_or_non_positive_amount_is_refused(bad_amount):
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", bad_amount)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert plan.refused == [("AAPL", dca_runner.REFUSED_INVALID_AMOUNT)]


def test_gate6_invalid_amount_does_not_refuse_the_whole_batch():
    """MSFT's NaN amount estimates a 0x (MIN_LADDER_MULTIPLIER) priority, so it
    sorts last and does not disturb NVDA/AAPL's own allocation-priority order
    (NVDA's estimated 3x outranks AAPL's 2x) -- see
    test_allocation_priority_key_falls_back_to_zero_for_a_non_finite_drop's
    amount_usd analogue (Codex review: "Reject malformed amounts before
    allocation sorting")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 4.0),
                    dca_runner.Candidate("MSFT", float("nan")),
                    dca_runner.Candidate("NVDA", 6.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["NVDA", "AAPL"]
    assert plan.refused == [("MSFT", dca_runner.REFUSED_INVALID_AMOUNT)]


# --- buying power: an additional ceiling on top of the caps ---------------


def test_gate6_buying_power_stops_the_batch_even_under_cap_headroom():
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 4.0), dca_runner.Candidate("MSFT", 4.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
        buying_power_usd=5.0,
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert [t for t, reason in plan.skipped] == ["MSFT"]
    assert plan.skipped[0][1] == dca_runner.SKIPPED_INSUFFICIENT_BUYING_POWER


def test_gate6_no_buying_power_ceiling_when_omitted():
    """Backward compatible: omitting buying_power_usd enforces only the caps."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 4.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]


# --- weekly_cap_usd override: the effective stress-tier rollover cap -----


def test_gate6_weekly_cap_override_replaces_the_tier_nominal_cap():
    """Stress tier's real ceiling is the rollover reserve, not its nominal $1,800."""
    spent = [_order("X", 950.0, _utc(2026, 7, 27, 14, 0))]
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 6.0)],
        snapshot=dca_runner.budget_snapshot(spent, date(2026, 7, 28)),
        tier=BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True),
        weekly_cap_usd=900.0,  # no rollover reserve available this week
    )
    assert plan.to_place == []
    assert [t for t, _ in plan.skipped] == ["AAPL"]
    assert all(reason == dca_runner.SKIPPED_WEEKLY_CAP for _, reason in plan.skipped)


# --- active-universe whitelist: reject anything outside it ---------------


def test_gate6_ticker_outside_active_universe_is_refused():
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("BYDDY", 2.0), dca_runner.Candidate("AAPL", 2.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert ["BYDDY", dca_runner.REFUSED_NOT_ACTIVE_TICKER] in [list(x) for x in plan.refused]


def test_gate6_ticker_whitelist_is_case_insensitive():
    """Also proves the placed ticker is normalized to its canonical (upper) form."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("aapl", 2.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]


def test_gate6_ticker_dedup_is_case_insensitive():
    """AAPL and aapl in the same batch must not both plan a live order for the
    same security, violating the one-order-per-ticker circuit breaker (Codex
    review: "Normalize tickers before deduplicating them")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("aapl", 3.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.to_place[0].amount_usd == pytest.approx(3.0)  # higher estimated multiplier wins


# --- allocation priority: higher multiplier, then drop, then ticker ------


def test_gate6_allocation_priority_orders_by_multiplier_then_drop_then_ticker():
    """config/budgets.json: higher multiplier first, then larger drop, then ticker."""
    candidates = [
        # Alphabetically first of the three, but the lowest multiplier: must
        # still be placed last, proving multiplier outranks the ticker tie-break.
        dca_runner.Candidate("AAAU", 2.0, multiplier=1.0),
        dca_runner.Candidate("MSFT", 6.0, multiplier=3.0, drop_pct=6.1),
        dca_runner.Candidate("AAPL", 6.0, multiplier=3.0, drop_pct=6.3),
    ]
    plan = dca_runner.plan_orders(
        candidates=candidates,
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    # AAPL and MSFT both 3x; AAPL's larger drop (6.3 > 6.1) puts it first.
    assert [c.ticker for c in plan.to_place] == ["AAPL", "MSFT", "AAAU"]


def test_a_higher_priority_malformed_duplicate_does_not_suppress_a_valid_one():
    """A higher-priority malformed duplicate must not claim the ticker in
    `seen` before its own amount is validated -- Candidate("AAPL", NaN,
    multiplier=5) followed by a valid $2 AAPL row must refuse the former and
    still place the latter, not silently discard the valid row as an
    already-seen duplicate (Codex review: "Validate duplicate candidates
    before marking them seen")."""
    candidates = [
        dca_runner.Candidate("AAPL", float("nan"), multiplier=5.0),
        dca_runner.Candidate("AAPL", 2.0),
    ]
    plan = dca_runner.plan_orders(
        candidates=candidates,
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.to_place[0].amount_usd == pytest.approx(2.0)
    assert ("AAPL", dca_runner.REFUSED_INVALID_AMOUNT) in plan.refused


# --- malformed multipliers must not distort priority or tier counting ----


def test_gate6_out_of_range_multiplier_falls_back_to_notional_estimate():
    """An inf multiplier must not let a $2 candidate sort as if it were 5x+.

    Falls back to the same notional-based estimate used when no multiplier is
    supplied (Codex review: "Validate multipliers before tier selection").
    """
    candidates = [
        dca_runner.Candidate("AAPL", 2.0, multiplier=float("inf")),  # bogus -> ~1.0x estimate
        dca_runner.Candidate("MSFT", 6.0, multiplier=3.0),           # real 3x
    ]
    plan = dca_runner.plan_orders(
        candidates=candidates,
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["MSFT", "AAPL"]


def test_gate6_refuses_a_candidate_whose_multiplier_does_not_match_its_notional():
    """("AAPL", 5.99, 3.0): 3.0 is a genuine ladder multiplier, but neither
    the $2 nor the $1 base x 3.0 produces $5.99 -- this is sizing corruption,
    not a shallow candidate that merely sorts lower, and must not reach
    to_place at all (Codex review: "Refuse inconsistent multiplier and
    notional pairs")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 5.99, multiplier=3.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert ("AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER) in plan.refused


def test_gate6_refuses_a_candidate_with_no_multiplier_whose_amount_matches_no_allocation():
    """("AAPL", 5.99) -- no multiplier at all -- is exactly as unrepresentable
    as ("AAPL", 5.99, 3.0): the configured $1/$2 bases and 1x-5x half-step
    ladder cannot produce $5.99 from any combination. Omitting the
    multiplier must not exempt the notional from validation entirely (Codex
    review: "Reject amounts that match no ladder allocation")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 5.99)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert ("AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER) in plan.refused


@pytest.mark.parametrize("bad_multiplier", [None, float("nan"), 99.0])
def test_gate6_refuses_a_candidate_whose_drop_matches_no_tier_without_a_usable_multiplier(
    bad_multiplier,
):
    """("AAPL", 2.0, None, -1.0): $2.00 matches the 1.0x tier's notional, but
    no configured tier's drop range is ever negative. Without a usable
    multiplier claim, the prior fallback validated only the notional and
    ignored the reported drop entirely, so this (and the same row with NaN or
    99.0 as its multiplier) would otherwise reach PROCEED/to_place with a
    drop that could never have produced any real signal (Codex review:
    "Validate reported drops without a usable multiplier")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0, multiplier=bad_multiplier, drop_pct=-1.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert ("AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER) in plan.refused


def test_gate6_accepts_a_candidate_with_no_multiplier_whose_drop_matches_some_tier():
    """A genuine signal with no multiplier claim at all -- $2.00 (the 1.0x
    notional) and a real 1.0x-range drop -- must still place; the new
    drop-without-multiplier cross-check must not reject an otherwise
    consistent candidate."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0, drop_pct=0.5)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.refused == []


def test_assess_excludes_a_multiplier_notional_mismatch_from_tier_selection():
    """The same inconsistent pair must not inflate planned_spend/high_mult
    for tier selection either, or assess() could select a richer tier than
    the real (deduped, validated) plan plan_orders() would ever place."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 5.99, 3.0)]))
    assert r["verdict"] == "PROCEED"
    assert r["budget"]["tier"] == "normal"


def test_gate6_refuses_a_candidate_whose_drop_does_not_match_its_claimed_multiplier():
    """("AAPL", 10.0, 5.0, -0.01): $10 really is 5.0x the $2 base, but a
    negative drop could never have produced a 5x signal -- config/sizing_ladder.json's
    5.0x tier requires a drop of at least 8%. A real, notional-consistent
    multiplier is not enough on its own; the reported drop must also be
    consistent with that specific multiplier's configured tier range (Codex
    review: "Bind the claimed multiplier to the reported drop")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 10.0, multiplier=5.0, drop_pct=-0.01)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert ("AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER) in plan.refused


def test_assess_excludes_a_multiplier_drop_mismatch_from_tier_selection():
    """The same drop/multiplier inconsistency must not inflate
    planned_spend/high_mult for tier selection either, mirroring
    test_assess_excludes_a_multiplier_notional_mismatch_from_tier_selection."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 10.0, 5.0, -0.01)]))
    assert r["verdict"] == "PROCEED"
    assert r["budget"]["tier"] == "normal"


def test_gate6_accepts_a_candidate_whose_drop_matches_its_claimed_multiplier():
    """A genuine 5x signal -- $10 notional, an 8%+ drop -- must still place;
    the new drop/multiplier cross-check must not reject a real, correctly
    consistent candidate."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 10.0, multiplier=5.0, drop_pct=9.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.refused == []


def test_gate6_accepts_a_kill_switch_capped_minimum_multiplier_despite_a_mismatched_drop():
    """near_close_dca.graded_buy_multiplier()'s 7-day kill-switch caps a deep-drop
    signal's multiplier down to the ladder's minimum (1.0x) while the candidate
    still carries its real, uncapped drop_pct (the now-removed GATE5 proposal
    doc's #3.6; see PRODUCTION_READINESS.md gap #1).
    ("AAPL", 2.0, multiplier=1.0, drop_pct=8.5) is exactly that: a real 1.0x
    notional match, but drop_pct=8.5 falls far outside 1.0x's own narrow
    [0.1, 1.0) tier range. The drop/multiplier consistency check must not
    reject this legitimate kill-switch-capped candidate (Codex review:
    "Accept kill-switch-capped multipliers")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0, multiplier=1.0, drop_pct=8.5)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.refused == []


def test_gate6_rejects_a_kill_switch_capped_minimum_multiplier_with_a_sub_threshold_drop():
    """The kill-switch exemption waives the minimum multiplier's UPPER range
    only. ("AAPL", 2.0, multiplier=1.0, drop_pct=-1.0) has a real 1.0x
    notional match, but the kill-switch only ever caps a genuine buy signal
    (a real drop of at least the ladder's 0.1% no_buy_below_pct minimum)
    down to the floor multiplier -- it never fabricates one, so a capped
    candidate reporting a negative or sub-threshold drop is still corrupted
    evidence, not a legitimate capping, and must still be refused (Codex
    review: "Reject sub-threshold drops for capped minimum multipliers")."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0, multiplier=1.0, drop_pct=-1.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert ("AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER) in plan.refused


def test_gate6_non_minimum_multipliers_still_reject_a_mismatched_drop():
    """The kill-switch exemption is scoped to MIN_LADDER_MULTIPLIER only --
    a non-minimum multiplier with an implausible drop must still be refused,
    proving the exemption did not broaden the original fraud-detection gap
    for any other multiplier."""
    assert dca_runner.MIN_LADDER_MULTIPLIER != 3.0
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 6.0, multiplier=3.0, drop_pct=8.5)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert ("AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER) in plan.refused


def test_gate6_a_missing_drop_pct_does_not_trigger_the_drop_mismatch_check():
    """cand.drop_pct is None -- a metadata gap, not corruption -- must fall
    through to the ordinary notional-only validation, consistent with
    _candidate_multiplier_notional_mismatch's own treatment of missing
    metadata."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 10.0, multiplier=5.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.refused == []


def test_assess_refuses_a_no_multiplier_candidate_whose_amount_matches_no_allocation():
    """assess(..., candidates=[("AAPL", 5.99)]) must not return PROCEED with a
    $5.99 live order -- the configured $1/$2 bases and 1x-5x half-step
    ladder cannot produce $5.99 from any combination, multiplier supplied or
    not (Codex review: "Reject amounts that match no ladder allocation")."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 5.99)]))
    assert r["verdict"] == "PROCEED"
    assert r["plan"]["to_place"] == []
    assert ["AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER] in r["plan"]["refused"]


# --- optional ceilings must themselves be finite and non-negative --------


@pytest.mark.parametrize("bad_cap", [float("nan"), float("-inf"), -1.0])
def test_gate6_rejects_non_finite_weekly_cap_usd(bad_cap):
    with pytest.raises(ValueError):
        dca_runner.plan_orders(
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            tier=BudgetTier("normal", 180.0, 900.0),
            weekly_cap_usd=bad_cap,
        )


@pytest.mark.parametrize("bad_cap", [float("nan"), float("-inf"), -1.0])
def test_gate6_rejects_non_finite_buying_power_usd(bad_cap):
    with pytest.raises(ValueError):
        dca_runner.plan_orders(
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=bad_cap,
        )


@pytest.mark.parametrize("field", ["weekly_cap_usd", "buying_power_usd"])
@pytest.mark.parametrize("bad_value", [True, False, "10", 10**1000])
def test_plan_orders_rejects_boolean_nonnumeric_and_oversized_ceilings(field, bad_value):
    """A bool survives isinstance(x, (int, float)) (bool is an int
    subclass) and would otherwise be accepted as a $0/$1 ceiling; a string
    instead raises an uncaught TypeError from math.isfinite() itself; and a
    syntactically valid but astronomically large int (e.g. a broker-
    supplied buying_power_usd=10**1000) passes the isinstance check but
    makes math.isfinite() raise OverflowError rather than return False --
    all three must produce plan_orders()'s documented ValueError instead of
    escaping uncaught or silently disabling the cap (Codex review:
    "Validate planner ceilings before calling isfinite")."""
    with pytest.raises(ValueError):
        dca_runner.plan_orders(
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            tier=BudgetTier("normal", 180.0, 900.0),
            **{field: bad_value},
        )


# --- cap boundary: stop, never shrink ------------------------------------


def test_gate6_stops_at_daily_cap_boundary_and_reports_skips():
    """Place what fits, skip the rest, name them. Never shrink to squeeze under."""
    spent = [_order("X", 175.0, _utc(2026, 7, 28, 14, 0))]
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 4.0),   # 175 -> 179, fits
                    dca_runner.Candidate("MSFT", 4.0),   # would be 183, over 180
                    dca_runner.Candidate("NVDA", 1.0)],  # must NOT be back-filled
        snapshot=dca_runner.budget_snapshot(spent, date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert [t for t, _ in plan.skipped] == ["MSFT", "NVDA"]
    assert all(reason == dca_runner.SKIPPED_DAILY_CAP for _, reason in plan.skipped)


def test_gate6_never_shrinks_an_order_to_fit_remaining_headroom():
    spent = [_order("X", 178.0, _utc(2026, 7, 28, 14, 0))]  # $2 headroom
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 6.0)],
        snapshot=dca_runner.budget_snapshot(spent, date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert [t for t, _ in plan.skipped] == ["AAPL"]


def test_gate6_weekly_cap_also_stops_the_batch():
    spent = [_order("X", 898.0, _utc(2026, 7, 27, 14, 0))]
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 6.0)],
        snapshot=dca_runner.budget_snapshot(spent, date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert [t for t, _ in plan.skipped] == ["AAPL"]
    assert all(reason == dca_runner.SKIPPED_WEEKLY_CAP for _, reason in plan.skipped)


def test_gate6_exhausted_cap_yields_empty_plan_not_an_error():
    """Today's real shape: $297 already spent against a $180 normal cap."""
    spent = [_order("X", 297.0, _utc(2026, 7, 28, 16, 13), placed_agent="user")]
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=dca_runner.budget_snapshot(spent, date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert plan.skipped


def test_gate6_no_ticker_appears_twice_in_one_batch():
    """Duplicates are walked in allocation-priority order first, so the
    higher-estimated-multiplier duplicate ($3.00 -> 1.5x) is the one seen
    (and kept) before the lower one ($2.00 -> 1.0x)."""
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("AAPL", 3.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert plan.to_place[0].amount_usd == pytest.approx(3.0)


def test_gate6_every_planned_order_is_a_buy():
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 4.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert all(c.side == "buy" for c in plan.to_place)


def test_plan_orders_accepts_the_same_raw_candidate_rows_assess_does():
    """docs/live_wiring.md's documented "direct Python caller" pattern says to
    "call plan_orders directly with the same snapshot/tier/candidates" --
    reusing the SAME raw (ticker, amount_usd[, multiplier[, drop_pct]]) rows
    assess() accepts, not pre-converted Candidate objects. plan_orders()
    previously never normalized a raw row itself: _allocation_priority_key()
    dereferences cand.drop_pct, so a tuple raised a bare AttributeError
    instead of producing a plan (Codex review: "Normalize documented
    candidate rows before planning")."""
    plan = dca_runner.plan_orders(
        candidates=[("AAPL", 2.0), ("MSFT", 6.0, 3.0), ("GOOGL", 10.0, 5.0, 9.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert {c.ticker for c in plan.to_place} == {"AAPL", "MSFT", "GOOGL"}
    assert plan.refused == []


def test_plan_orders_refuses_a_malformed_raw_candidate_row_without_raising():
    """A malformed raw row (wrong arity here) must be classified the same
    REFUSED_INVALID_AMOUNT way a malformed Candidate already is, not raise
    and lose the whole batch."""
    plan = dca_runner.plan_orders(
        candidates=[("AAPL", 2.0, 1.0, 0.5, "extra")],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert len(plan.refused) == 1
    assert plan.refused[0][1] == dca_runner.REFUSED_INVALID_AMOUNT


@pytest.mark.parametrize("bad_candidate", [{"ticker": "AAPL", "amount_usd": 2.0}, "AAPL", 5])
def test_plan_orders_refuses_a_malformed_direct_candidate_object_without_crashing(bad_candidate):
    """A malformed direct candidate that is not a list, tuple, or Candidate
    -- a mapping, a bare string, an int -- previously skipped
    _parse_candidate_rows() entirely and reached
    _candidate_sortable_for_planning(), where _allocation_priority_key()
    dereferences .drop_pct and raises an uncaught AttributeError, aborting
    planning for the entire batch including otherwise-valid candidates.
    Every non-Candidate row is now routed through the parser, which
    classifies it REFUSED_INVALID_AMOUNT instead (Codex review: "Refuse
    malformed direct candidate objects without crashing")."""
    plan = dca_runner.plan_orders(
        candidates=[bad_candidate, dca_runner.Candidate("MSFT", 2.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert [c.ticker for c in plan.to_place] == ["MSFT"]
    assert len(plan.refused) == 1
    assert plan.refused[0][1] == dca_runner.REFUSED_INVALID_AMOUNT


def test_plan_orders_redacts_a_secret_carrying_malformed_candidate():
    """A malformed direct candidate can be arbitrary connector data -- a
    dict carrying an access token or account identifier -- rather than a
    plain unrecognizable row. Plan.refused can be persisted as run evidence
    or included in alerts, so only a safe classification (the value's type)
    may appear when the candidate (or a malformed row's own first element)
    isn't itself a plain string (Codex review: "Redact malformed candidates
    before returning them")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    bad_candidate = {"account_number": secret_account_number, "token": "shh"}
    plan = dca_runner.plan_orders(
        candidates=[bad_candidate],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    assert plan.to_place == []
    assert len(plan.refused) == 1
    ticker_repr, refusal = plan.refused[0]
    assert refusal == dca_runner.REFUSED_INVALID_AMOUNT
    assert secret_account_number not in ticker_repr
    assert "shh" not in ticker_repr
    assert "dict" in ticker_repr


# ===========================================================================
# G. Placement gate -- the switch stays off
# ===========================================================================


def test_placement_disabled_by_default():
    """enable_live_orders is the second gate and defaults False.

    Chosen 2026-07-28: it stays False until a human flips it after reviewing
    dry-run output. The runner must never enable it on its own.
    """
    assert dca_runner.build_runner_broker(place_fn=lambda **kw: None).enable_live_orders is False


def test_dry_run_defaults_true_and_is_the_first_gate():
    """dry_run mirrors StrategyConfig.dry_run and stays True until flipped explicitly."""
    assert dca_runner.build_runner_broker(place_fn=lambda **kw: None).dry_run is True


def test_enable_live_orders_alone_does_not_place_while_dry_run_stays_true():
    """One switch must never bypass the two-gate design.

    Setting enable_live_orders=True must not submit real orders while the
    strategy configuration's dry_run stays at its default True (Codex review:
    "Preserve the StrategyConfig dry-run placement gate").
    """
    placed = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: placed.append(kw), enable_live_orders=True
    )
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    result = dca_runner.execute_plan(plan, broker)
    assert placed == []
    assert result.placement_enabled is False


@pytest.mark.parametrize(
    "enable_live_orders,dry_run",
    [
        ("true", False),  # a truthy but non-literal enable_live_orders
        (True, "false"),  # a truthy but non-literal (non-False) dry_run
    ],
)
def test_non_literal_boolean_gate_values_never_enable_placement(enable_live_orders, dry_run):
    """A common wiring mistake like enable_live_orders="false" (a nonempty,
    truthy string) must not enable placement just because it is truthy: both
    live-order gates must be the literal booleans True/False, not merely
    truthy/falsy (Codex review: "Require literal booleans for both
    live-order gates")."""
    placed = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: placed.append(kw) or {"id": "ok"},
        enable_live_orders=enable_live_orders,
        dry_run=dry_run,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    result = dca_runner.execute_plan(plan, broker)
    assert placed == []
    assert result.placement_enabled is False


def test_both_gates_flipped_places_and_passes_account_and_ref_id():
    placed = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: placed.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="0935-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]
    assert placed[0]["account_number"] == TEST_ACCOUNT_NUMBER
    assert placed[0]["ref_id"] == "dca-runner-0935-2026-07-28-AAPL"


@pytest.mark.parametrize("run_id", ["dca-abc-2026-07-28", "dca-xyz-2026-07-28"])
def test_ref_id_is_bound_to_slot_and_day_not_the_caller_run_id(run_id):
    """Two overlapping invocations of the SAME slot can each pass a
    different caller-generated run_id prefix that both merely contain
    today's date (the only thing run_id validation enforces); since the
    sole-executor check is not atomic, each can fetch the same still-empty
    history before either placement becomes visible, so keying ref_id off
    run_id itself would let each derive a DIFFERENT idempotency key for the
    same ticker/slot/day, and broker-side idempotency could not deduplicate
    the two requests. ref_id must instead be derived from slot_id and
    trading_day directly, so any run_id sharing the same slot/day produces
    the IDENTICAL ref_id (Codex review: "Bind the idempotency key to the
    slot")."""
    placed = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: placed.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    dca_runner.execute_plan(
        plan,
        broker,
        run_id=run_id,
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert placed[0]["ref_id"] == "dca-runner-0935-2026-07-28-AAPL"


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "error": "insufficient funds"},
        {"data": {"order": {"state": "rejected"}}},
        # An explicit zero fill: the order died having executed nothing, which
        # is what makes it isolatable. Omitting the figure instead halts --
        # see test_a_terminal_order_with_no_fill_figure_at_all_halts.
        {"order": {"id": "ok", "state": "cancelled", "cumulative_quantity": "0"}},
        {},  # no order id anywhere
    ],
)
def test_failure_shaped_response_is_isolated_without_raising(response):
    """A place_fn that returns a failure envelope instead of raising must still
    be RECOGNIZED as a failure -- and, since 2026-08-24, must cost only its own
    ticker: the next order in the plan is still attempted."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        return response

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False, account_number=TEST_ACCOUNT_NUMBER
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)],
    )
    assert calls == ["AAPL", "MSFT"]  # MSFT is still attempted after AAPL's failure
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL", "MSFT"]
    assert result.halted_on is None  # two failures, below the 3-failure breaker


def test_a_returned_notional_mismatch_halts_rather_than_reads_as_success():
    """A response proves what the broker actually processed: every per-
    order, daily, weekly, and buying-power ceiling in this module is
    enforced against the REQUESTED amount, so a returned dollar_amount that
    does not match it (e.g. $100 processed for a requested $2 buy) can
    exceed every one of those ceilings, and recording it as a clean
    submission would hide the overrun entirely (Codex review: "Verify the
    returned order notional")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "100.00",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"


def test_a_one_cent_returned_notional_mismatch_halts_rather_than_reads_as_success():
    """A tolerance of a full cent previously accepted an exact one-cent
    mismatch ($10.01 returned for a requested $10.00) as a match, even
    though that one cent can be exactly what pushed the order over the
    per-order/daily/weekly/buying-power ceilings enforced against the
    REQUESTED amount (Codex review: "Reject a one-cent returned-notional
    mismatch")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "10.01",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 10.0)])
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[("AAPL", 10.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"


@pytest.mark.parametrize("response", [None, "not a dict", 42, ["order", "ok"]])
def test_non_dict_placement_response_halts_rather_than_reads_as_success(response):
    """A malformed non-dict response must never be read as "no failure found".

    Reading it as success would record the ticker as submitted with no order
    id or confirmation it exists (Codex review: "Fail closed on
    non-dictionary placement responses").
    """
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: response,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"


def test_a_boolean_placement_order_id_halts_rather_than_reads_as_success():
    """A bare truthiness check would accept {"id": true} as proof an order
    exists, when it is not a usable identifier at all -- a malformed
    response could then be recorded as a successful submission with no real
    broker order id backing it (Codex review: "Reject boolean placement
    order IDs")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": True, "state": "queued", "symbol": "AAPL", "side": "buy"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # ... and the batch STOPS. An unusable id would ordinarily prove nothing
    # was accepted and isolate (2026-08-24), but this fixture also reports
    # state "queued", and a working state is acceptance evidence in its own
    # right -- so the response describes an order that may exist while naming
    # it unusably, which is the least accountable shape there is (Codex
    # review, PR #786: "Honor working states even when an order ID is
    # missing"). This also settles the question left open on the round-18
    # thread -- whether a malformed id should halt -- in the safe direction,
    # for the responses that say the broker took the order.
    assert result.halted_on == "AAPL"


@pytest.mark.parametrize("bad_id", [["ORD"], {"value": "ORD"}])
def test_a_non_scalar_placement_order_id_halts_rather_than_reads_as_success(bad_id):
    """Any truthy container (a list, a dict) passes a bare truthiness check
    just like a boolean did, despite providing no usable broker order
    identifier (Codex review: "Reject non-scalar placement order IDs")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": bad_id, "state": "queued", "symbol": "AAPL", "side": "buy"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # ... and the batch STOPS. An unusable id would ordinarily prove nothing
    # was accepted and isolate (2026-08-24), but this fixture also reports
    # state "queued", and a working state is acceptance evidence in its own
    # right -- so the response describes an order that may exist while naming
    # it unusably, which is the least accountable shape there is (Codex
    # review, PR #786: "Honor working states even when an order ID is
    # missing"). This also settles the question left open on the round-18
    # thread -- whether a malformed id should halt -- in the safe direction,
    # for the responses that say the broker took the order.
    assert result.halted_on == "AAPL"


def test_a_boolean_returned_notional_halts_rather_than_reads_as_success():
    """float(True) succeeds as 1.0, so a response carrying
    dollar_amount=true would otherwise be accepted for a requested $1 order
    despite the broker supplying no numeric confirmation of the processed
    amount (Codex review: "Reject boolean returned notionals before
    conversion")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": True,
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 1.0)])
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[("AAPL", 1.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
@pytest.mark.parametrize("padded_state", [" rejected ", "cancelled\t", "\nrejected"])
def test_a_whitespace_padded_terminal_state_halts_rather_than_reads_as_success(padded_state):
    """Lowercasing alone does not match _DEAD_PLACEMENT_STATES against a
    whitespace-padded terminal state, so the response would otherwise be
    recorded as a successful submission despite the broker rejecting or
    cancelling the order (Codex review: "Strip returned order states before
    terminal-state checks")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": padded_state, "symbol": "AAPL", "side": "buy",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # Recognized as terminal rather than read as a submission -- which is what
    # this test is about. It halts because the response reports no fill figure
    # at all, so it does not establish that nothing executed (2026-08-24).
    assert result.halted_on == "AAPL"


@pytest.mark.parametrize("bad_state", [False, [], {"value": "rejected"}, 0])
def test_a_non_string_placement_state_halts_rather_than_reads_as_success(bad_state):
    """A present but non-string state -- str(False)/str([])/str({"value": ...})
    all produce a value that is not in _DEAD_PLACEMENT_STATES -- must not be
    read as a healthy, non-terminal order: the wrapper never actually
    provided a usable state at all (Codex review: "Reject non-string
    placement states")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": bad_state, "symbol": "AAPL", "side": "buy",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_a_non_string_placement_state_is_redacted_to_its_type():
    """raw_state's full repr was previously embedded in the failure reason,
    but a malformed broker response's "state" field can be arbitrary
    connector data -- a dict carrying an access token, account identifier,
    or request headers, for instance -- rather than a real order state
    string. _redacted_envelope_error() never processes order metadata like
    this, and the downstream account-number-specific replace only catches
    that one known secret, so only a safe classification (the value's type)
    may appear in ExecutionResult.failure_reason (Codex review: "Redact
    malformed placement states before recording them")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD",
            "state": {"account_number": secret_account_number, "token": "shh"},
            "symbol": "AAPL", "side": "buy",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert secret_account_number not in result.failures[0].reason
    assert "shh" not in result.failures[0].reason
    assert "dict" in result.failures[0].reason


def test_an_unrecognized_placement_state_redacts_a_token_shaped_value():
    """A malformed/hostile broker response could set a confirmed-string
    "state" field to something token-shaped; _redacted_envelope_error()
    only ever processes envelope["error"], never this field, so the same
    token-shaped redaction must be applied directly here too (Codex review:
    "Redact malformed placement states before recording them")."""
    secret_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc123def456ghi789"  # synthetic, 20+ chars
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": f"denied-Bearer-{secret_token}", "symbol": "AAPL", "side": "buy",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert secret_token not in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
@pytest.mark.parametrize("blank_state", ["", "   ", "\t"])
def test_a_blank_placement_state_halts_rather_than_reads_as_success(blank_state):
    """A present "state" key whose value is blank or whitespace-only
    normalizes to "", which is not in _DEAD_PLACEMENT_STATES and would
    otherwise read exactly like an OMITTED state key -- a healthy default --
    even though the wrapper supplied the key but no usable value at all
    (Codex review: "Reject blank placement states")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": blank_state, "symbol": "AAPL", "side": "buy",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
@pytest.mark.parametrize("unrecognized_state", ["error", "denied", "mystery"])
def test_an_unrecognized_placement_state_halts_rather_than_reads_as_success(unrecognized_state):
    """A present, nonblank state that is neither a recognized dead state
    (_DEAD_PLACEMENT_STATES) nor one of dca_live_adapters._LIVE_ORDER_STATES
    fell through as a healthy default: every nonterminal string was treated
    as proof a live order exists. An unrecognized state like "error",
    "denied", or "mystery" is untrustworthy evidence, not confirmation of a
    real submission, and must halt rather than be recorded as submitted
    (Codex review: "Require a recognized live placement state")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": unrecognized_state, "symbol": "AAPL", "side": "buy",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
@pytest.mark.parametrize("live_state", ["new", "queued", "confirmed", "unconfirmed", "partially_filled", "filled"])
def test_a_recognized_live_placement_state_still_succeeds(live_state):
    """Every state in dca_live_adapters._LIVE_ORDER_STATES must still be
    accepted as a successful submission -- the new recognized-state
    requirement must not reject a genuinely healthy, real broker state."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": live_state, "symbol": "AAPL", "side": "buy",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]


def test_an_expired_placement_response_halts_rather_than_reads_as_success():
    """near_close_dca's canonical terminal-state set includes "expired", but a
    hand-copied placement-response set had omitted it: a response carrying a
    valid order id and state="expired" can never fill, so recording it as a
    successful submission and continuing the batch would be wrong (Codex
    review: "Treat expired placement responses as terminal")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ORD", "state": "expired", "symbol": "AAPL", "side": "buy"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # Recognized as terminal rather than read as a submission -- which is what
    # this test is about. It halts because the response reports no fill figure
    # at all, so it does not establish that nothing executed (2026-08-24).
    assert result.halted_on == "AAPL"


@pytest.mark.parametrize("padded_side", [" buy ", "BUY\t", "\nbuy"])
def test_a_whitespace_padded_returned_side_is_still_accepted(padded_side):
    """A whitespace-padded but otherwise valid returned side like " buy " is a
    real accepted order; comparing it unstripped would treat it as a
    mismatch, halt the batch, and drop the real order out of `submitted`
    (Codex review: "Normalize the returned placement side before
    comparison")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ORD", "state": "filled", "symbol": "AAPL", "side": padded_side},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]
    assert result.halted_on is None


def test_outer_envelope_failure_is_not_hidden_by_a_healthy_looking_nested_order():
    """A top-level failure flag must not be discarded just because data.order looks fine."""
    response = {"success": False, "data": {"order": {"id": "ORD", "state": "queued"}}}
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: response,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
@pytest.mark.parametrize(
    "response",
    [
        {"success": 0, "data": {"order": {"id": "ORD", "state": "queued"}}},
        {"success": "", "data": {"order": {"id": "ORD", "state": "queued"}}},
        {"success": "false", "data": {"order": {"id": "ORD", "state": "queued"}}},
        {"ok": 0, "data": {"order": {"id": "ORD", "state": "queued"}}},
        {"approved": "false", "data": {"order": {"id": "ORD", "state": "queued"}}},
    ],
)
def test_a_non_boolean_falsy_status_flag_halts_rather_than_reads_as_success(response):
    """A broker wrapper that encodes an explicit failure as 0, "", or "false"
    instead of the literal JSON boolean false must still halt the batch --
    an identity check against False alone would let it through and record
    the ticker as submitted despite the wrapper reporting failure (Codex
    review: "Reject non-boolean failure flags")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: response,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"


def test_an_order_level_failure_envelope_is_not_hidden_by_a_healthy_looking_id_and_state():
    """A wrapper reporting an explicit failure INSIDE the unwrapped order
    (success=false alongside a truthy id and a nonterminal state) must not
    be read as a healthy submission just because the id/state checks alone
    would pass (Codex review: "Inspect order-level failure envelopes")."""
    response = {
        "data": {"order": {"success": False, "error": "denied", "id": "ORD", "state": "queued"}}
    }
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: response,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
@pytest.mark.parametrize(
    "response",
    [
        {"data": {"order": None, "id": "REQUEST", "state": "queued"}},
        {"data": None, "id": "REQUEST", "state": "queued"},
    ],
)
def test_a_malformed_nested_placement_container_halts_rather_than_reads_as_success(response):
    """When "data" or "order" is PRESENT but explicitly non-dict (e.g. null),
    that must be treated as a malformed container, not as though the key
    were simply absent -- falling back to search the enclosing envelope for
    an id would otherwise read "REQUEST" (which may only identify the
    wrapper's own request, not a confirmed created order) as a healthy,
    unnested response (Codex review: "Reject malformed nested placement
    containers")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: response,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"


@pytest.mark.parametrize("bad_id", ["   ", float("nan"), float("inf"), float("-inf")])
def test_a_blank_or_non_finite_placement_order_id_halts_rather_than_reads_as_success(bad_id):
    """A whitespace-only string or a NaN/infinite numeric id is truthy, so a
    bare-truthiness/type check alone would accept either as proof an order
    exists, when neither is a usable broker-assigned identifier (Codex
    review: "Reject blank and non-finite placement IDs")."""
    response = {"id": bad_id, "state": "queued"}
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: response,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"


@pytest.mark.parametrize("bad_id", [-1, -1.0, 0, 0.0])
def test_a_non_positive_placement_order_id_halts_rather_than_reads_as_success(bad_id):
    """A finite negative (or zero) numeric id is truthy and finite, so a
    truthiness/type/finiteness check alone would accept it as proof an
    order exists -- e.g. a wrapper using -1 as a sentinel for "no order id
    was actually assigned" -- when it is exactly as unusable as NaN/infinity
    despite being finite (Codex review: "Reject negative numeric placement
    IDs")."""
    response = {"id": bad_id, "state": "queued", "symbol": "AAPL", "side": "buy"}
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: response,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
@pytest.mark.parametrize("ref_id_field", ["ref_id", "client_order_id", "client_order_uuid", "client_id"])
def test_a_stale_client_reference_in_the_response_halts_rather_than_reads_as_success(ref_id_field):
    """A cached or stale response can carry a valid order id and matching
    symbol/side/amount for a PRIOR request while actually confirming that
    other request, not this one -- comparing only symbol/side/amount would
    let that prior order be mistaken for confirmation of this submission
    (Codex review: "Verify a returned client reference before recording
    success"). Checked against any of the four client-reference fields this
    repo's broker adapter already recognizes."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "2.00", ref_id_field: "dca-runner-some-other-run-AAPL",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert "client reference" in result.failures[0].reason


@pytest.mark.parametrize("falsy_ref_id", ["", 0, False])
def test_a_present_but_falsy_client_reference_still_halts_placement(falsy_ref_id):
    """An `or` chain of order.get(...) calls would treat a present-but-falsy
    reference ("", 0, False) as though the field were entirely absent and
    skip this check as if no usable confirmation were returned at all,
    letting a healthy-looking id/symbol/side/amount alone record the order
    as submitted (Codex review: "Reject falsy returned client
    references")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "2.00", "ref_id": falsy_ref_id,
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert "client reference" in result.failures[0].reason


def test_a_present_null_client_reference_still_halts_placement():
    """A present ref_id key with an explicit null value carries no usable
    idempotency confirmation, exactly like a present-but-falsy one -- it
    must not be treated the same as an entirely absent key (which the
    symbol check's own idiom does exempt), or a cached/stale response with
    a valid-looking id/symbol/side/amount could still be recorded as
    confirmation of this submission via that explicit null (Codex review:
    "Reject null returned client references")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "2.00", "ref_id": None,
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert "client reference" in result.failures[0].reason


def test_a_present_null_placement_symbol_still_halts_placement():
    """A present symbol key with an explicit null value carries no usable
    ticker confirmation, exactly like the present null client reference
    case: it must not be treated the same as an entirely absent key, or a
    valid-looking order id/state/amount/client-reference alone could still
    be recorded as a successful submission without ever confirming the
    returned order is for the requested ticker (Codex review: "Reject a
    present null placement symbol")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": None, "side": "buy",
            "dollar_amount": "2.00", "ref_id": kw["ref_id"],
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert "symbol" in result.failures[0].reason


def test_a_present_null_placement_side_still_halts_placement():
    """A present side key with an explicit null value carries no usable
    confirmation that the returned order is a buy rather than a stale or
    mismatched order -- it must not be treated the same as an entirely
    absent key, or a valid-looking id/reference/symbol/amount response
    alone could still be recorded as a successful buy (Codex review:
    "Reject a present null placement side")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": None,
            "dollar_amount": "2.00", "ref_id": kw["ref_id"],
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert "side" in result.failures[0].reason


def test_a_present_null_returned_notional_still_halts_placement():
    """A present dollar_amount key with an explicit null value is not the
    same as the field being omitted: if the wrapper returned null because
    it could not confirm the processed amount, a larger broker notional
    (and a per-order or aggregate cap overrun) could be hidden behind the
    remaining fields alone (Codex review: "Reject a present null returned
    notional")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": None, "ref_id": kw["ref_id"],
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert "dollar_amount" in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_a_matching_client_reference_in_the_response_still_reads_as_success():
    """The new client-reference check must not reject a genuine confirmation
    that correctly echoes back this call's own ref_id."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "2.00", "ref_id": kw["ref_id"],
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]
    assert result.halted_on is None


def test_place_fn_exception_is_recorded_as_a_safe_classification_only():
    """place_fn's raised exception message is connector-defined and can
    embed the requested account number, an access token, or other secret-
    shaped context; failure_reason is explicitly sent to alerts and durable
    run evidence. Only a safe classification (the exception's type name)
    may be recorded -- never its message (Codex review: "Redact broker
    exceptions before storing failure evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value

    def _boom(**kw):
        raise ConnectionError(f"denied for account {secret_account_number}")

    broker = dca_runner.build_runner_broker(
        place_fn=_boom,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # A raise halts (2026-08-24) -- see
    # test_a_raised_place_fn_halts_because_nothing_can_be_established -- but
    # the redaction rule this test is about applies to the per-ticker reason
    # and the halt reason alike.
    assert result.halted_on == "AAPL"
    for text in (result.failures[0].reason, result.failure_reason):
        assert secret_account_number not in text
        assert "denied" not in text
    assert "ConnectionError" in result.failures[0].reason


def test_a_returned_broker_error_has_the_account_number_redacted():
    """envelope["error"] can embed the account number this request was
    placed against; failure_reason is explicitly sent to alerts and durable
    run evidence. Unlike a raised exception's unknowable shape, the
    account number is a specific secret this module itself knows for
    certain, so it is redacted from the (otherwise preserved, per "Preserve
    the placement failure reason") error text rather than discarding the
    whole message (Codex review: "Redact returned broker errors before
    persisting them")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "success": False, "error": f"denied for account {TEST_ACCOUNT_NUMBER}"
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # One isolated rejection no longer stops the batch (2026-08-24).
    assert result.halted_on is None
    assert TEST_ACCOUNT_NUMBER not in result.failures[0].reason
    assert "denied for account" in result.failures[0].reason


@pytest.mark.parametrize(
    "mismatched_order",
    [
        {"id": "ORD", "state": "queued", "symbol": "MSFT"},
        {"id": "ORD", "state": "queued", "instrument_symbol": "MSFT"},
        {"id": "ORD", "state": "queued", "symbol": "AAPL", "side": "sell"},
        # A present-but-falsy symbol ("", 0, False): `order.get("symbol") or
        # order.get("instrument_symbol")` would otherwise read this as "no
        # symbol reported" and skip the check entirely (Codex review:
        # "Reject falsy returned symbols").
        {"id": "ORD", "state": "queued", "symbol": ""},
        {"id": "ORD", "state": "queued", "symbol": 0},
        {"id": "ORD", "state": "queued", "symbol": False},
    ],
)
def test_a_mismatched_order_in_the_response_halts_rather_than_reads_as_success(mismatched_order):
    """A response proving the broker actually processed a different order
    (wrong symbol or side) must not be trusted just because it carries a
    healthy-looking id and state (Codex review: "Halt when the broker
    returns a mismatched order")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"order": mismatched_order},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"


def test_a_non_string_mismatched_symbol_is_redacted_to_its_type():
    """A malformed or mismatched broker response's "symbol" can be arbitrary
    connector data -- a dict carrying an access token or request headers,
    for instance -- rather than a real ticker: str(returned_symbol) only
    needs to be nonempty to reach the mismatch branch, not actually
    ticker-shaped. execute_plan() copies this failure string into
    ExecutionResult.failure_reason, which is persisted/alerted, and the
    downstream account-number-specific replace does not catch this. Only a
    safe classification (the value's type) may appear when the returned
    symbol is not a plain string (Codex review: "Redact returned symbols
    before storing failure evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "order": {
                "id": "ORD", "state": "queued",
                "symbol": {"account_number": secret_account_number, "token": "shh"},
                "side": "buy",
            },
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert secret_account_number not in result.failures[0].reason
    assert "shh" not in result.failures[0].reason
    assert "dict" in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_a_matching_order_in_the_response_is_still_accepted():
    """The new identity check must not reject a normal, matching response."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"order": {"id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy"}},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]


def test_execute_plan_requires_run_id_when_placement_is_enabled():
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(plan, broker)


def test_execute_plan_rejects_a_run_id_not_bound_to_the_trading_day():
    """A nonempty run_id alone does not establish run-scoped uniqueness: a
    caller reusing a constant run_id across days would derive the identical
    ref_id every day, and an idempotent broker could reject a later day's buy
    or return a prior day's order as if it were a successful current
    submission (Codex review: "Bind the idempotency key to the trading
    day")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="scheduled",  # constant across days, not bound to trading_day
            snapshot=snapshot,
            tier=tier,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def _valid_execute_plan_kwargs(**over):
    """A complete, otherwise-passing execute_plan() kwargs set for the
    documented direct plan_orders() -> execute_plan() path (no assess()),
    so a guard test only needs to override the one input it targets and a
    monkeypatched collaborator, rather than reconstruct every required
    field from scratch."""
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate("AAPL", 2.0)]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    base = dict(
        plan=plan,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        tier=tier,
        trading_day=date(2026, 7, 28),
        orders=[],
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=candidates,
    )
    base.update(over)
    return base


def test_execute_plan_refuses_when_the_market_calendar_is_unreadable(monkeypatch):
    """execute_plan() is the only place Gates 0/1/3 are guaranteed to run at
    all on the documented direct plan_orders() -> execute_plan() path (no
    assess()), so a market_calendar.json read failure here must refuse to
    place, mirroring assess()'s own Gate 0 exception handling, rather than
    let a raw CalendarCoverageError/OSError/etc. escape past this live
    placement boundary."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )

    def _raise(trading_day):
        raise dca_runner.CalendarCoverageError("2026 not covered")

    monkeypatch.setattr(dca_runner, "market_day_status", _raise)
    kwargs = _valid_execute_plan_kwargs()
    plan = kwargs.pop("plan")
    with pytest.raises(ValueError, match="market calendar unavailable"):
        dca_runner.execute_plan(plan, broker, **kwargs)


def test_execute_plan_refuses_when_the_trading_window_config_is_unreadable(monkeypatch):
    """Mirrors assess()'s own conversion of an unreadable trading_window.json
    into a refusal (Codex review: "Convert trading-window config failures
    into ABORT"), but at the execute_plan() boundary, which is the only
    place this recheck is guaranteed to run on the direct plan_orders() ->
    execute_plan() path."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )

    def _raise(trading_day):
        raise KeyError("start_date")

    monkeypatch.setattr(dca_runner, "gate_trading_window", _raise)
    kwargs = _valid_execute_plan_kwargs()
    plan = kwargs.pop("plan")
    with pytest.raises(ValueError, match="trading window configuration unavailable"):
        dca_runner.execute_plan(plan, broker, **kwargs)


def test_execute_plan_redacts_a_malformed_broker_symbol():
    """Fresh evidence beyond assess()'s own equivalent fix: the documented
    direct execute_plan() boundary's bought_today check still normalized
    broker-controlled symbols with no exception boundary. A malformed
    BrokerOrder.symbol can be arbitrary connector data -- a dict carrying an
    account identifier, token, or other request context --
    _normalize_ticker() raises a ValueError containing its full repr, and
    that raw exception IS this documented boundary's required failure
    evidence, so it must be redacted to a safe classification (Codex
    review: "Redact malformed symbols at the direct execution boundary")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    bad_order = _order(
        {"account_number": secret_account_number, "token": "shh"}, 10.0, _utc(2026, 7, 28, 16, 0)
    )
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError) as excinfo:
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            tier=tier,
            trading_day=date(2026, 7, 28),
            orders=[bad_order],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )
    assert secret_account_number not in str(excinfo.value)
    assert "shh" not in str(excinfo.value)
    assert "ValueError" in str(excinfo.value)


@pytest.mark.parametrize("bad_run_id", [123, 123.0, True, ["2026-07-28"]])
def test_execute_plan_rejects_a_non_string_run_id(bad_run_id):
    """A truthy non-string run_id (e.g. the int 123) passed the old bare
    `not run_id` truthiness check, then reached _ISO_DATE_RE.findall(),
    which raises a bare TypeError on a non-string/bytes-like argument --
    escaping this documented direct live boundary's controlled invalid-input
    ValueError before any placement (Codex review: "Validate run IDs before
    regex matching")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id=bad_run_id,
            snapshot=snapshot,
            tier=tier,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_a_plan_containing_a_non_buy_order():
    """Plan/PlannedOrder are public; execute_plan must not trust side blindly."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0, side="sell")])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(plan, broker, run_id="test-run-2026-07-28")


def test_execute_plan_refuses_a_plan_containing_a_non_whitelisted_ticker():
    """Plan/PlannedOrder are public; a caller-built Plan bypassing
    plan_orders' whitelist check must still be refused here, not forwarded
    to place_fn (Codex review: "Revalidate buy plans at the placement
    boundary")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("BYDDY", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("BYDDY", 2.0)],
        )
    assert calls == []


@pytest.mark.parametrize("bad_amount", [True, False])
def test_execute_plan_refuses_a_plan_containing_a_boolean_amount(bad_amount):
    """PlannedOrder is public and constructible independently of
    plan_orders(): math.isfinite(True) succeeds since bool is an int
    subclass, so amount_usd=True would otherwise pass every check (positivity,
    cap, cent-precision) at this independently callable live boundary and
    submit a real $1.00 order (Codex review: "Reject boolean planned-order
    amounts")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", bad_amount)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 10.0)],
        )
    assert calls == []


def test_execute_plan_refuses_a_plan_containing_an_over_cap_amount():
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 15.0)])  # over per-order, under daily cap
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 15.0)],
        )
    assert calls == []


@pytest.mark.parametrize("bad_amount", [float("nan"), float("inf"), 0.0, -5.0])
def test_execute_plan_refuses_a_plan_containing_an_invalid_amount(bad_amount):
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", bad_amount)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 10.0)],
        )
    assert calls == []


# --- aggregate caps must be reapplied for a caller-built plan -------------


def test_execute_plan_requires_snapshot_and_tier_when_placement_is_enabled():
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(plan, broker, run_id="test-run-2026-07-28")


def test_execute_plan_refuses_a_caller_built_plan_that_exceeds_the_daily_cap():
    """Fresh evidence after the per-order boundary fix is that many distinct,
    individually-valid orders can still blow through the aggregate daily cap
    (Codex review: "Reapply aggregate caps to caller-built plans")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    tickers = [
        "DFTX", "TRIN", "AAAU", "SPCX", "EVR", "LUNR", "IRDM", "DXYZ",
        "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD",
        "CBRS", "MRVL", "BTX",
    ]  # 19 tickers * $10 = $190, over the $180 normal daily cap
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder(t, 10.0) for t in tickers])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[(t, 10.0) for t in tickers],
        )
    assert calls == []


def test_execute_plan_refuses_a_plan_that_exceeds_the_weekly_cap_given_prior_spend():
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    prior = [_order("X", 898.0, _utc(2026, 7, 28, 14, 0))]  # $2 of weekly headroom left
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 6.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(prior, date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=prior, tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 6.0)],
        )
    assert calls == []


def test_execute_plan_refuses_a_plan_that_exceeds_buying_power():
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(
        to_place=[dca_runner.PlannedOrder("AAPL", 4.0), dca_runner.PlannedOrder("MSFT", 4.0)]
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=5.0,
            candidates=[("AAPL", 4.0), ("MSFT", 4.0)],
        )
    assert calls == []


def test_execute_plan_requires_weekly_cap_usd_when_tier_uses_rollover_reserve():
    """The Stress tier's nominal $1,800 weekly cap overstates the real
    ceiling unless a verified rollover reserve is folded in and passed
    explicitly; omitting weekly_cap_usd must not silently fall back to the
    nominal cap (Codex review: "Require the effective rollover cap for
    Stress execution")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_recomputes_spend_from_orders_not_a_stale_snapshot():
    """A caller could pass fresh `orders` (for the bought-today check) with
    a stale `snapshot` whose totals predate a same-day order for a
    DIFFERENT ticker. The aggregate cap check must use spend recomputed
    from `orders`, not `snapshot`'s own (stale) totals (Codex review:
    "Recompute spend from placement-time order history")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    stale_snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))  # zero spend
    current_orders = [_order("MSFT", 178.0, _utc(2026, 7, 28, 14, 0))]  # $178 spent today
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 4.0)])  # would total $182
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=stale_snapshot,
            trading_day=date(2026, 7, 28),
            orders=current_orders,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 4.0)],
        )
    assert calls == []


def test_execute_plan_rejects_a_tier_with_inflated_caps():
    """BudgetTier is public and constructible independently of
    select_budget_tier(): a caller-built tier naming a real tier but
    carrying tampered caps must not be trusted at face value, or every
    aggregate comparison would use the inflated limits (Codex review:
    "Reject noncanonical budget tier ceilings")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("normal", 1_000_000.0, 1_000_000.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_rejects_a_tier_that_is_not_the_real_selection_for_todays_candidates():
    """Matching tier's own fields against a canonical BUDGET_TIERS entry
    proves the tier's DEFINITION is legitimate, never that it was the
    correct SELECTION for today's actual candidates: a stale or simply wrong
    (but otherwise canonical) Elevated tier, with only $8 of new,
    non-high-multiplier candidates -- which select_budget_tier() would
    choose Normal for -- must be refused, not trusted just because it names
    a real tier (Codex review: "Derive the tier from verified candidates";
    "Bind tier inputs to the plan's verified candidates")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 8.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("elevated", 240.0, 1200.0),  # canonical, but wrong for $8/0
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 8.0)],
        )


def test_execute_plan_rejects_a_plan_order_with_no_matching_candidate():
    """candidates is used only to select the tier -- without also binding
    plan.to_place to it, a caller could supply thirteen unrelated $10
    candidates (selecting Elevated) while submitting a completely different
    AAPL order that, considered alone, would only justify Normal, letting
    $175 already spent take the day to $185 on a $10 AAPL order with no
    verified signal behind it at all (Codex review: "Bind tier candidates to
    the plan being submitted")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    prior = [_order("X", 175.0, _utc(2026, 7, 28, 14, 0))]
    unrelated_tickers = [
        "DFTX", "TRIN", "AAAU", "SPCX", "EVR", "LUNR", "IRDM", "DXYZ",
        "LITE", "ASML", "DRAM", "STX", "MSFT", "AMAT", "AMD", "CBRS", "MRVL", "BTX",
    ]  # 18 tickers * $10 = $180 -> selects Elevated ($240 daily cap)
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 10.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(prior, date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=prior,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("elevated", 240.0, 1200.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[(t, 10.0) for t in unrelated_tickers],
        )


def test_execute_plan_rejects_a_plan_order_matching_only_a_losing_duplicate_candidate():
    """Binding plan.to_place to the raw, pre-dedup candidate list would let a
    plan order pair with a lower-priority duplicate ticker that
    _tier_selection_inputs itself never keeps -- plan_orders() would have
    refused or never produced that exact order. Only the deduped, validated
    winners (valid_cands) are legitimate matches (Codex review: "Match plans
    only against eligible candidate winners")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    # Two AAPL rows: the 2x/$4 row wins allocation priority (higher
    # multiplier); the 1x/$2 row is a losing duplicate that
    # _tier_selection_inputs excludes from valid_cands entirely.
    candidates = [
        dca_runner.Candidate("AAPL", 4.0, multiplier=2.0),
        dca_runner.Candidate("AAPL", 2.0, multiplier=1.0),
    ]
    # A hand-built plan claiming the LOSING duplicate's amount, not the real
    # winner's.
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=candidates,
        )


def test_execute_plan_rejects_a_hand_picked_subset_of_a_tier_inflating_candidate_set():
    """Every individual order matching some verified candidate is not enough:
    with $176 already spent, 26 valid $5 candidates (planned spend $130)
    select Elevated ($240 daily cap), but a hand-built plan containing only
    ONE of those 26 candidates would, considered alone, belong under
    Normal's $180 cap ($176 + $5 = $181 > $180). Riding on the richer tier's
    cap while submitting only a hand-picked subset of the candidates that
    earned it is exactly what rebuilding and requiring an exact match against
    plan_orders()'s own output closes (Codex review: "Rebuild the submitted
    plan from verified candidates")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    prior = [_order("X", 176.0, _utc(2026, 7, 28, 14, 0))]
    tickers = sorted(dca_runner.ACTIVE_TICKERS)[:26]  # 26 * $5 = $130 -> selects Elevated
    candidates = [(t, 5.0) for t in tickers]
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder(tickers[0], 5.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(prior, date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=prior,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("elevated", 240.0, 1200.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=candidates,
        )


def test_execute_plan_rejects_a_reordered_permutation_of_the_expected_plan():
    """The same orders in the wrong order must be refused too, not just a
    subset: placement is sequential and stops at the first failure, so
    submitting AAPL (the higher-multiplier, higher-priority candidate) after
    MSFT instead of before it would let a failed AAPL placement still leave
    the lower-priority MSFT bought, silently inverting plan_orders()'s
    configured allocation priority in exactly the partial-failure case that
    priority exists to protect (Codex review: "Preserve the rebuilt plan's
    allocation order")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [
        dca_runner.Candidate("AAPL", 4.0, multiplier=2.0),  # higher multiplier -> placed first
        dca_runner.Candidate("MSFT", 2.0, multiplier=1.0),
    ]
    # The real plan_orders() output would place AAPL then MSFT; this hand-built
    # plan reverses that order while keeping the exact same set of orders.
    plan = dca_runner.Plan(to_place=[
        dca_runner.PlannedOrder("MSFT", 2.0),
        dca_runner.PlannedOrder("AAPL", 4.0),
    ])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=candidates,
        )


def test_execute_plan_requires_candidates_when_placing():
    """Omitting candidates -- the verifiable input tier selection is bound
    to -- must not silently trust the caller-supplied tier at face value."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
        )


def test_execute_plan_redacts_a_candidate_source_exception():
    """A lazy candidates iterable (e.g. a generator wrapping a live
    market-data/signal-generation call) that raises while being consumed by
    _parse_candidate_rows() can embed an API token, account context, or
    request headers in its own exception message; this ValueError becomes
    the caller's required failure evidence and alerts. Only a safe
    classification, the exception's type name, may be recorded -- never its
    message, matching the same fix already applied to assess()'s own
    candidate-parsing exception path (Codex review: "Redact candidate
    exceptions at the live boundary")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )

    def _boom():
        raise ConnectionError(f"denied for account {secret_account_number}")
        yield  # pragma: no cover - makes this a generator function

    with pytest.raises(ValueError) as exc_info:
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=_boom(),
        )
    message = str(exc_info.value)
    assert secret_account_number not in message
    assert "denied" not in message
    assert "ConnectionError" in message


def test_execute_plan_redacts_a_direct_history_materialization_exception():
    """A lazy broker-history iterable (e.g. an API client wrapping a real
    fetch) passed directly to the documented plan_orders() -> execute_plan()
    path can raise while being materialized here, embedding an account
    number, an access token, or other connector context in its own
    exception message; this ValueError becomes the caller's required
    failure evidence and alerts. Only a safe classification, the
    exception's type name, may be recorded -- never its message, matching
    the same fix already applied to the assessment and candidate paths
    (Codex review: "Redact direct history materialization failures")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )

    def _boom():
        raise ConnectionError(f"denied for account {secret_account_number}")
        yield  # pragma: no cover - makes this a generator function

    with pytest.raises(ValueError) as exc_info:
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=_boom(),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )
    message = str(exc_info.value)
    assert secret_account_number not in message
    assert "denied" not in message
    assert "ConnectionError" in message


def test_execute_plan_rejects_a_weekly_cap_override_above_the_tier_nominal_cap():
    """weekly_cap_usd exists only to LOWER the effective ceiling below the
    tier's own nominal cap (e.g. a Stress tier with a limited rollover
    reserve), never to raise it -- a finite, non-negative override greater
    than tier.weekly_cap_usd must be refused for any tier, not only ones
    that use a rollover reserve (Codex review: "Reject caller-inflated
    weekly cap overrides")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("normal", 180.0, 900.0),
            weekly_cap_usd=1_000_000.0,  # canonical tier's own cap is $900
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_allows_a_weekly_cap_override_at_or_below_the_tier_nominal_cap():
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        trading_day=date(2026, 7, 28),
        orders=[],
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        tier=BudgetTier("normal", 180.0, 900.0),
        weekly_cap_usd=180.0,  # a legitimate, lower override for a non-rollover tier
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]


def test_execute_plan_requires_weekly_rollover_reserve_usd_when_tier_uses_rollover_reserve():
    """A weekly_cap_usd override alone only proves a number was given, never
    that it reflects a real rollover reserve (Codex review: "Bind Stress
    overrides to verified reserve state")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True),
            weekly_cap_usd=1800.0,  # the tier's own nominal cap, unverified
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


@pytest.mark.parametrize("bad_reserve", [True, False])
def test_execute_plan_rejects_a_boolean_weekly_rollover_reserve_usd(bad_reserve):
    """math.isfinite(True) succeeds since bool is an int subclass, so a
    boolean reserve would otherwise be retained as $1 (True) or $0 (False)
    and silently inflate (or understate) the derived Stress-tier weekly
    ceiling (Codex review: "Reject boolean rollover-reserve values")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True),
            weekly_rollover_reserve_usd=bad_reserve,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_rejects_an_oversized_weekly_rollover_reserve_usd():
    """A syntactically valid but astronomically large JSON integer (e.g.
    10**1000, injected via persisted StrategyState) passes the isinstance
    check but makes math.isfinite() raise OverflowError -- not return False
    -- while this live placement boundary validates weekly_rollover_reserve_usd.
    Left uncaught, that would escape with a raw OverflowError instead of the
    documented ValueError every other malformed reserve already gets here,
    matching the same fix already applied to assess() (Codex review: "Handle
    rollover-reserve overflow at the live boundary").

    Candidates must actually justify a stress-tier selection: a single
    ``AAPL`` candidate (as this test previously used) trips the EARLIER
    "tier does not match select_budget_tier()'s own choice for this batch"
    guard before weekly_rollover_reserve_usd is ever inspected, so the
    ValueError this test asserted on was never actually the one described
    above -- it was a coincidental one three checks earlier, and the
    OverflowError guard itself had no regression coverage at all.
    ``_stress_tier_candidates()`` is the same fixture the neighboring
    verified-reserve tests already use to legitimately reach a stress tier.
    """
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    candidates = _stress_tier_candidates()
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True)
    plan = dca_runner.plan_orders(
        dca_runner._parse_candidate_rows(candidates),
        snapshot,
        tier,
        buying_power_usd=TEST_BUYING_POWER_USD,
    )
    with pytest.raises(ValueError, match="weekly_rollover_reserve_usd"):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            weekly_rollover_reserve_usd=10**1000,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=candidates,
        )


def test_execute_plan_rejects_a_stress_batch_beyond_the_verified_reserve():
    """With $1,000 already spent this week and zero reserve, the effective
    ceiling is $900 (Normal's nominal weekly cap), not the Stress tier's
    $1,800 -- a batch that would push weekly spend to $1,002 must be
    refused even though a caller-supplied weekly_cap_usd=1800 would have
    allowed it under the old, unverified check."""
    prior = [_order("X", 1000.0, _utc(2026, 7, 28, 14, 0))]
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(prior, date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=prior,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True),
            weekly_rollover_reserve_usd=0.0,  # no reserve earned
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_allows_a_stress_batch_within_the_verified_reserve(monkeypatch):
    """weekly_rollover_reserve_usd=900 (the reserve cap) is only accepted here
    because finalized_budget_days/submitted_notional_by_week actually justify
    it: a prior week finalized with zero spend leaves the full $900 unused,
    which is exactly what roll_weekly_reserve()'s algorithm would carry
    forward (Codex review: "Verify rollover reserve before raising the
    weekly cap").

    plan is built via plan_orders() on the exact candidates/snapshot/tier
    also passed to execute_plan(), per docs/live_wiring.md's documented
    "reuse the same Plan ... for both calls" pattern -- execute_plan() now
    requires plan.to_place to match, exactly, what plan_orders() itself
    would produce from these same verified inputs (Codex review: "Rebuild
    the submitted plan from verified candidates"), so a hand-picked subset
    (e.g. just the GOOGL order) is no longer accepted here."""
    # raises the daily order-count cap out of the way: this test is about the stress-tier weekly reserve, not the count cap.
    monkeypatch.setattr(dca_runner, "MAX_ORDERS_PER_DAY", 100)
    calls = []
    current_week_key = iso_week_key(date(2026, 7, 28))
    prior_week_key = iso_week_key(date(2026, 7, 20))  # an earlier ISO week
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    # GOOGL, not one of _stress_tier_candidates()'s own tickers, so this
    # extra candidate is its only entry and cannot be shadowed by dedup
    # against a same-ticker stress-tier winner.
    candidates = _stress_tier_candidates() + [("GOOGL", 2.0)]
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True)
    plan = dca_runner.plan_orders(
        dca_runner._parse_candidate_rows(candidates),
        snapshot,
        tier,
        buying_power_usd=TEST_BUYING_POWER_USD,
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[],
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        tier=tier,
        weekly_rollover_reserve_usd=900.0,
        weekly_rollover_reserve_week_key=current_week_key,
        finalized_budget_days={date(2026, 7, 20)},
        submitted_notional_by_week={prior_week_key: 0.0},
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=candidates,
    )
    assert set(result.submitted) == {t for t, *_ in _stress_tier_candidates()} | {"GOOGL"}
    assert len(result.submitted) == 19


def test_execute_plan_validates_a_malformed_weekly_cap_override_before_applying_min():
    """A malformed weekly_cap_usd (e.g. the serialized string "900") used to
    reach the Stress-tier-specific min(weekly_cap_usd, derived_cap)
    calculation before its own type/finiteness validation ran, raising a
    raw TypeError from comparing a string against a float instead of the
    documented ValueError every other malformed weekly_cap_usd already gets
    (Codex review: "Validate Stress cap overrides before applying min")."""
    current_week_key = iso_week_key(date(2026, 7, 28))
    prior_week_key = iso_week_key(date(2026, 7, 20))
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    candidates = _stress_tier_candidates()
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True)
    plan = dca_runner.plan_orders(
        dca_runner._parse_candidate_rows(candidates),
        snapshot,
        tier,
        buying_power_usd=TEST_BUYING_POWER_USD,
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            weekly_cap_usd="900",
            weekly_rollover_reserve_usd=900.0,
            weekly_rollover_reserve_week_key=current_week_key,
            finalized_budget_days={date(2026, 7, 20)},
            submitted_notional_by_week={prior_week_key: 0.0},
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=candidates,
        )


def test_execute_plan_zeroes_a_stale_stress_reserve():
    """A reserve stamped for an earlier ISO week is zeroed the same way
    assess() zeroes it, collapsing the effective cap to Normal's $900."""
    stale_week_key = iso_week_key(date(2026, 7, 21))  # a week earlier
    prior = [_order("X", 850.0, _utc(2026, 7, 28, 14, 0))]
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 60.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(prior, date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=prior,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True),
            weekly_rollover_reserve_usd=1000.0,
            weekly_rollover_reserve_week_key=stale_week_key,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 60.0)],
        )


def test_execute_plan_rejects_orders_already_exhausted_building_the_snapshot():
    """A documented direct caller can pass a single-use iterable to
    budget_snapshot() to build `snapshot`, then reuse that SAME
    now-exhausted iterable here: orders materializes to an empty list,
    verified_snapshot silently reports zero spend, and an earlier same-day
    buy disappears from both the cap check and the per-ticker interlock --
    even though `snapshot` (built before the iterable was exhausted) still
    shows the real $178 prior spend. A recomputed total LOWER than the
    already-established snapshot must be refused, not trusted (Codex
    review: "Detect history consumed while constructing the snapshot")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    prior = [_order("MSFT", 178.0, _utc(2026, 7, 28, 14, 0))]  # $178 spent today
    shared_iterable = iter(prior)
    snapshot = dca_runner.budget_snapshot(shared_iterable, date(2026, 7, 28))  # exhausts it
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError, match="recomputed day/week spend"):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=shared_iterable,  # already exhausted by budget_snapshot() above
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


def test_execute_plan_accepts_orders_as_a_single_pass_iterable():
    """orders must be materialized before both the bought-today comprehension
    and budget_snapshot() consume it, or a generator would be exhausted by
    the first traversal and silently drop every order from the recomputed
    daily/weekly totals (Codex review: "Materialize placement history before
    reusing it")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    prior = [_order("MSFT", 178.0, _utc(2026, 7, 28, 14, 0))]  # $178 spent today
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 4.0)])  # would total $182
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(prior, date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=iter(prior),  # single-pass: exhausted by a naive first use,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 4.0)],
        )
    assert calls == []


def test_execute_plan_allows_a_plan_within_every_aggregate_cap():
    """The reapplied aggregate checks must not reject a legitimately valid plan."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        orders=[], tier=BudgetTier("normal", 180.0, 900.0),
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        trading_day=date(2026, 7, 28),
        buying_power_usd=100.0,
        candidates=[("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]


def test_execute_plan_refuses_a_non_cent_order_amount():
    """Every cap/buying-power check below uses the raw float, but placement
    sends f"{amount:.2f}" -- the rounded value the broker actually receives.
    A $9.996 order with $170.004 already spent and a $180 daily cap would
    pass the aggregate check at exactly $180.00 unrounded, then submit
    $10.00, exceeding the confirmed ceiling by the rounding delta (Codex
    review: "Apply caps to the rounded notional sent to the broker")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 9.996)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=BudgetTier("normal", 180.0, 900.0),
            trading_day=date(2026, 7, 28),
            buying_power_usd=100.0,
            candidates=[("AAPL", 9.996)],
        )


def test_execute_plan_requires_trading_day_when_placement_is_enabled():
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_rejects_a_non_date_trading_day():
    """The mismatch branch calls trading_day.isoformat(); a documented
    direct caller supplying a serialized (e.g. ISO string) or otherwise
    non-date trading_day would otherwise raise a bare AttributeError there
    instead of this boundary's controlled ValueError (Codex review:
    "Validate the placement trading day before formatting it")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day="2026-07-28",
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_rejects_a_non_date_snapshot_day():
    """The mismatch branch calls snapshot.day.isoformat(); a documented
    direct caller supplying a caller-built BudgetSnapshot (public and
    constructible independently of budget_snapshot()) whose day is
    serialized (e.g. an ISO string) or otherwise not a date would otherwise
    raise a bare AttributeError there instead of this boundary's controlled
    ValueError (Codex review: "Validate snapshot days before formatting
    them")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    bad_snapshot = dca_runner.BudgetSnapshot(day="2026-07-28", day_notional_usd=0.0, week_notional_usd=0.0)
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=bad_snapshot,
            trading_day=date(2026, 7, 28),
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


@pytest.mark.parametrize("bad_total", [float("nan"), float("-inf"), -1.0])
def test_execute_plan_rejects_a_non_finite_or_negative_supplied_snapshot_total(bad_total):
    """A caller-built BudgetSnapshot (public and constructible independently
    of budget_snapshot()) with a NaN, -inf, or negative total defeats the
    monotonicity check that detects an exhausted/reused history iterable:
    any comparison against NaN is False, and nothing compares less than
    -inf, so a recomputed history no longer registers as suspiciously lower
    than claimed and a live plan could be authorized from zero real history
    (Codex review: "Validate supplied snapshot totals before comparison")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    bad_snapshot = dca_runner.BudgetSnapshot(
        day=date(2026, 7, 28), day_notional_usd=bad_total, week_notional_usd=0.0
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=bad_snapshot,
            trading_day=date(2026, 7, 28),
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_rejects_a_trading_day_that_does_not_match_the_snapshot():
    """A stale or mismatched BudgetSnapshot must be refused, not trusted with
    the wrong day's spend already netted out (Codex review: "Reject stale
    budget snapshots at live placement")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            trading_day=date(2026, 7, 29),  # one day off from the snapshot
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_requires_orders_when_placement_is_enabled():
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            tier=BudgetTier("normal", 180.0, 900.0),
            trading_day=date(2026, 7, 28),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_refuses_a_ticker_already_bought_today_per_broker_history():
    """BudgetSnapshot carries only totals, not per-ticker identities: without
    independently reverifying against broker history, a caller-built Plan
    naming a ticker already bought earlier today would pass every other
    check and submit a second same-day buy (Codex review: "Recheck same-day
    ticker history before placement")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    earlier = [_order("AAPL", 2.0, _utc(2026, 7, 28, 14, 0))]
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(earlier, date(2026, 7, 28)),
            tier=BudgetTier("normal", 180.0, 900.0),
            trading_day=date(2026, 7, 28),
            orders=earlier,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


def test_execute_plan_allows_a_ticker_not_bought_today_alongside_unrelated_history():
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    # Afternoon, same calendar day but well outside the 0935 slot's own
    # 9:30-10:00 ET window, so this exercises only the bought-today dedup
    # check below, not the (separately tested) sole-executor interlock.
    earlier = [_order("MSFT", 2.0, _utc(2026, 7, 28, 18, 0))]
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=dca_runner.budget_snapshot(earlier, date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
        trading_day=date(2026, 7, 28),
        orders=earlier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]


def test_execute_plan_still_blocks_a_ticker_whose_earlier_buy_today_was_cancelled():
    """A cancelled/rejected buy with no fill still consumed the day's
    one-order-per-ticker attempt, even though it consumed no budget:
    ``counts_toward_budget`` is False for a fill-less dead order, but the
    same-day dedup must use the broader ``is_buy_attempt`` instead, or a
    rejected order would make its ticker eligible again the same day (Codex
    review: "Keep canceled orders in the daily ticker interlock")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    earlier = [
        _order(
            "AAPL", 2.0, _utc(2026, 7, 28, 14, 0),
            state="cancelled", executed_dollar_amount=None,
        )
    ]
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot(earlier, date(2026, 7, 28)),
            tier=BudgetTier("normal", 180.0, 900.0),
            trading_day=date(2026, 7, 28),
            orders=earlier,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


@pytest.mark.parametrize("bad_cap", [float("nan"), float("-inf"), -1.0])
def test_execute_plan_rejects_non_finite_weekly_cap_usd(bad_cap):
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            weekly_cap_usd=bad_cap,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_requires_buying_power_usd_when_placement_is_enabled():
    """Omitting buying_power_usd must not silently skip the absolute
    buying-power circuit breaker (Codex review: "Require buying power for
    live execution")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            candidates=[("AAPL", 2.0)],
        )


@pytest.mark.parametrize("bad_power", [True, False, "5.00"])
def test_execute_plan_rejects_a_non_numeric_buying_power_usd(bad_power):
    """For the direct execute_plan() path (the one docs/live_wiring.md
    recommends for a live caller not routing through assess()),
    math.isfinite(True) succeeds since bool is an int subclass, so a
    boolean buying_power_usd would otherwise be accepted as $1 of confirmed
    balance and reach live placement with no numeric confirmation at all; a
    string instead raised an uncaught TypeError (Codex review: "Reject
    nonnumeric buying power at the live boundary")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=bad_power,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


def test_execute_plan_rejects_an_oversized_buying_power_usd():
    """A syntactically valid but astronomically large JSON integer (e.g.
    10**1000, supplied by the broker) passes the isinstance check but makes
    math.isfinite() raise OverflowError -- not return False -- while this
    live placement boundary validates buying_power_usd. Left uncaught, that
    would escape with a raw OverflowError instead of the documented
    ValueError every other malformed buying_power_usd already gets (Codex
    review: "Catch oversized buying power at the placement boundary")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=10**1000,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


def test_execute_plan_rejects_an_oversized_planned_amount():
    """A syntactically valid but astronomically large PlannedOrder.amount_usd
    (e.g. 10**1000, from a caller-built plan) passes the isinstance check but
    makes math.isfinite() raise OverflowError -- not return False -- while
    this live placement boundary validates each planned order's amount. Left
    uncaught, that would escape with a raw OverflowError instead of the
    documented ValueError every other malformed amount already gets (Codex
    review: "Reject oversized planned amounts without overflowing")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 10**1000)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


@pytest.mark.parametrize("bad_value", [True, False, "900", 10**1000])
def test_execute_plan_rejects_boolean_nonnumeric_and_oversized_weekly_cap_overrides(bad_value):
    """The caller-supplied weekly_cap_usd override at the live boundary must
    get the same non-boolean numeric and overflow-safe validation already
    applied to buying_power_usd/tier caps: a bool survives isinstance(x,
    (int, float)) and would otherwise be accepted as a $0/$1 ceiling; a
    string instead raises an uncaught TypeError from math.isfinite(); and a
    syntactically valid but astronomically large int (e.g. 10**1000) passes
    the isinstance check but makes math.isfinite() raise OverflowError
    rather than return False (Codex review: "Validate the weekly cap
    override before isfinite")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            weekly_cap_usd=bad_value,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


@pytest.mark.parametrize("bad_value", [float("nan"), float("-inf"), -1.0])
def test_execute_plan_rejects_a_non_finite_snapshot_day_notional(bad_value):
    """A caller-built BudgetSnapshot with NaN must not silently disable the
    daily-cap comparison (Codex review: "Validate aggregate budget inputs
    before comparison")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    bad_snapshot = dca_runner.BudgetSnapshot(
        day=date(2026, 7, 28), day_notional_usd=bad_value, week_notional_usd=0.0
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=bad_snapshot,
            tier=BudgetTier("normal", 180.0, 900.0),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1.0])
def test_execute_plan_rejects_a_non_finite_tier_daily_cap(bad_value):
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", bad_value, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )


def test_execute_plan_refuses_a_plan_containing_the_same_ticker_twice():
    """A public caller-built Plan naming AAPL twice must not call place_fn
    twice with the same ref_id (Codex review: "Reject duplicate tickers in
    caller-built plans")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(
        to_place=[dca_runner.PlannedOrder("AAPL", 2.0), dca_runner.PlannedOrder("aapl", 2.0)]
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[("AAPL", 2.0)],
        )
    assert calls == []


def test_execute_plan_preserves_the_failure_reason():
    """Gate 9 needs the failing reason without repeating placement (Codex
    review: "Preserve the placement failure reason")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"success": False, "error": "insufficient funds"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # One isolated rejection no longer stops the batch (2026-08-24).
    assert result.halted_on is None
    assert "insufficient funds" in result.failures[0].reason


def test_execute_plan_redacts_non_account_secrets_from_broker_response_errors():
    """A broker response's free-text error field is arbitrary external data.
    The account-number-specific replace only catches the ONE secret this
    module knows for certain; an access token, request headers, or other
    connector credentials embedded in the error string are otherwise
    persisted/alerted verbatim. Fresh evidence beyond the account-number
    redaction requires token-shaped substrings to be redacted too, while the
    ordinary human-readable reason ("insufficient funds"-style text) must
    still survive (Codex review: "Redact non-account secrets from broker
    response errors")."""
    secret_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc123def456ghi789"  # synthetic, 20+ chars
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"success": False, "error": f"denied: Bearer {secret_token}"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert secret_token not in result.failures[0].reason
    assert "denied" in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_execute_plan_rejects_an_mcp_iserror_placement_envelope():
    """dca_live_adapters._is_error_response() already treats isError=True as
    a definitive network/bridge failure signal for every other MCP response
    this repo parses, even when its own "error" message is absent. This
    placement-response path never checked it: a response shaped like
    {"isError": true, "id": "request-id"} -- carrying none of
    success/ok/approved/error -- previously passed straight through every
    check and would have been recorded as a successful submission even
    though no broker order was ever created (Codex review: "Reject MCP
    isError placement envelopes")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"isError": True, "id": "request-id"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == []
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert "isError" in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_execute_plan_accepts_a_non_boolean_iserror_field():
    """isError must be checked by strict identity (is True), mirroring
    dca_live_adapters._is_error_response()'s own strict check: a non-boolean
    value (e.g. the string "true") is not this canonical bridge-failure
    signal and must not itself halt an otherwise-healthy response."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"isError": "true", "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert result.submitted == ["AAPL"]


def test_execute_plan_redacts_a_non_string_mismatched_client_reference():
    """A mismatched client reference can be arbitrary connector data -- a
    dict carrying a token or other request metadata, not just a stale
    string. str(returned_ref_id) is only used for the comparison, so a
    non-string value reached this failure message unexamined. Only a
    plain-string mismatch (useful diagnostic text) is embedded; anything
    else must be reported only by type (Codex review: "Redact mismatched
    client references before recording failures")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "ref_id": {"account_number": secret_account_number, "token": "shh"},
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert secret_account_number not in result.failures[0].reason
    assert "shh" not in result.failures[0].reason
    assert "dict" in result.failures[0].reason


@pytest.mark.parametrize("ref_id_field", ["ref_id", "client_order_id", "client_order_uuid", "client_id"])
def test_execute_plan_redacts_a_string_mismatched_client_reference(ref_id_field):
    """A string mismatched client reference can still contain token/account-shaped
    substrings from broker metadata and must be redacted before it is persisted."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD",
            "state": "queued",
            "symbol": "AAPL",
            "side": "buy",
            ref_id_field: f"{secret_account_number}-token_xxxxxxxxxxxxxxxxxxxxxxxx",
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[],
        tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert secret_account_number not in result.failures[0].reason
    assert "token_xxxxxxxxxxxxxxxxxxxxxxxx" not in result.failures[0].reason
    assert "[REDACTED-TOKEN]" in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_execute_plan_redacts_a_malformed_success_flag():
    """A malformed success/ok/approved flag can be arbitrary connector data
    -- a dict carrying an access token or account identifier, for instance
    -- rather than the boolean-ish scalar every established response shape
    in this repo uses. Only a scalar value is embedded as-is; anything else
    must be reported by type (Codex review: "Redact malformed placement
    status flags")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"success": {"account_number": secret_account_number, "token": "shh"}},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # One isolated rejection no longer stops the batch (2026-08-24).
    assert result.halted_on is None
    assert secret_account_number not in result.failures[0].reason
    assert "shh" not in result.failures[0].reason
    assert "dict" in result.failures[0].reason


def test_execute_plan_redacts_a_token_shaped_string_success_flag():
    """A malformed success/ok/approved flag can be a string too -- a
    connector could accidentally place an access token or account
    identifier in this field -- so a string flag is not automatically safe
    just because it's a scalar. It must be scrubbed of any token-shaped
    substring the same way a string error/side/dollar_amount already is
    (Codex review: "Redact string-valued placement status flags")."""
    secret_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc123def456ghi789"  # synthetic, 20+ chars
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"success": f"Bearer-{secret_token}"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert secret_token not in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_execute_plan_redacts_a_non_string_returned_dollar_amount():
    """A nonnumeric dollar_amount can be arbitrary connector data -- a dict
    or token-bearing string, for instance -- rather than a malformed but
    plain numeric-looking value. Only a safe classification (the value's
    type) may appear when it isn't a plain string (Codex review: "Redact
    malformed returned notionals")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": {"account_number": secret_account_number, "token": "shh"},
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    # This response may still hold a live order, so the batch halts even though
    # an ordinary rejection would now be isolated (2026-08-24).
    assert result.halted_on == "AAPL"
    assert secret_account_number not in result.failures[0].reason
    assert "shh" not in result.failures[0].reason
    assert "dict" in result.failures[0].reason


def test_execute_plan_redacts_a_non_string_mismatched_side():
    """A malformed or mismatched broker response's "side" can be arbitrary
    connector data -- a dict carrying an access token, account identifier,
    or request headers -- rather than a real side string. Only a safe
    classification (the value's type) may appear when it isn't a plain
    string (Codex review: "Redact returned sides before storing
    failures")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {
            "id": "ORD", "state": "queued", "symbol": "AAPL",
            "side": {"account_number": secret_account_number, "token": "shh"},
        },
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
    )
    assert [f.ticker for f in result.failures] == ["AAPL"]
    assert secret_account_number not in result.failures[0].reason
    assert "shh" not in result.failures[0].reason
    assert "dict" in result.failures[0].reason


    # Whether this halts or is isolated follows the live-order rule (see
    # test_halt_vs_isolate_follows_whether_a_live_order_may_exist); what this
    # test pins is that the response was never read as SUCCESS.
def test_assess_redacts_malformed_history_metadata_from_the_abort_reason():
    """budget_snapshot() touches order.counts_toward_budget, which calls
    _normalize_side()/_is_dead_order_state() -- both raise ValueError
    embedding the malformed side/state value's full repr, and either can be
    arbitrary connector data (a dict carrying an access token or account
    identifier). This generic ValueError catch previously embedded that
    message verbatim, unlike the neighboring BudgetDataError path's
    type-only redaction (Codex review: "Redact malformed history metadata
    in abort evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    bad_order = dca_runner.BrokerOrder(
        symbol="AAPL",
        side={"account_number": secret_account_number, "token": "shh"},
        dollar_amount=10.0,
        created_at=_utc(2026, 7, 28, 13, 0),
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "shh" not in r["reason"]
    assert "ValueError" in r["reason"]


def test_assess_redacts_a_malformed_broker_timestamp_from_the_abort_reason():
    """_require_aware() raises ValueError embedding a non-datetime
    created_at's full repr, and both assess() and execute_plan() copy that
    message into their persisted/alerted failure evidence. A malformed
    created_at can be arbitrary connector data (a dict carrying an access
    token or account identifier), so only a safe classification (the
    value's type) may appear (Codex review: "Redact malformed broker
    timestamps from failure evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    bad_order = dca_runner.BrokerOrder(
        symbol="AAPL",
        side="buy",
        dollar_amount=10.0,
        created_at={"account_number": secret_account_number, "token": "shh"},
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_order]))
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "shh" not in r["reason"]
    assert "dict" in r["reason"]


# --- authorized-account verification --------------------------------------


def test_execute_plan_refuses_when_account_number_is_missing():
    """Omitting account_number must not silently place against an unpinned account."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"}, enable_live_orders=True, dry_run=False
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
        trading_day=date(2026, 7, 28),
            orders=[], tier=tier,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_when_account_number_has_the_wrong_shape():
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number="not-a-real-account",
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
        trading_day=date(2026, 7, 28),
            orders=[], tier=tier,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_when_account_number_does_not_match_configured_account():
    """A well-formed but different account must never receive real orders."""
    assert "123456789" != TEST_ACCOUNT_NUMBER
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number="123456789",  # well-formed, but not TEST_ACCOUNT_NUMBER
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
        trading_day=date(2026, 7, 28),
            orders=[], tier=tier,
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_account_mismatch_error_never_includes_the_mismatched_account_number():
    """failure_reason and any exception raised here are meant for alerts and
    durable run evidence (message-center, email drafts, repo alert files);
    an account-pinning mismatch must not copy a live brokerage account
    identifier into those channels (Codex review: "Redact the mismatched
    brokerage account")."""
    mismatched_account = "123456789"
    assert mismatched_account != TEST_ACCOUNT_NUMBER
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=mismatched_account,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError) as exc_info:
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )
    assert mismatched_account not in str(exc_info.value)


def test_execute_plan_requires_orders_account_number_when_placement_is_enabled():
    """`orders` carries no account identity of its own -- pinning
    broker.account_number proves only where placement WOULD go, never where
    the history behind every placement-time cap check actually came from
    (Codex review: "Bind placement-time history to the authorized
    account")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_requires_now_and_slot_id_when_placement_is_enabled():
    """docs/live_wiring.md documents a supported direct-call path that never
    calls assess() at all, so execute_plan() is the only place Gates 0/1/3
    (market day, time sanity, trading window) are guaranteed to run before a
    real order is submitted (Codex review: "Recheck market gates before live
    placement")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


@pytest.mark.parametrize("bad_now", ["2026-07-28T09:43:00-04:00", date(2026, 7, 28)])
def test_execute_plan_rejects_a_non_datetime_now(bad_now):
    """The assessment-side clock fix does not cover the documented direct
    plan_orders() -> execute_plan() path: now.tzinfo assumes `now` IS a
    datetime, so a caller supplying an ISO timestamp string or a bare date
    (None is already covered by the earlier `now is None` check) would
    otherwise raise a bare AttributeError instead of this boundary's
    controlled ValueError (Codex review: "Validate the direct-placement
    clock type before access")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=bad_now,
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


@pytest.mark.parametrize("bad_value", ["2026-07-28T09:43:00-04:00", date(2026, 7, 28)])
def test_execute_plan_rejects_a_non_datetime_orders_fetched_at(bad_value):
    """orders_fetched_at is externally supplied alongside broker history;
    orders_fetched_at.tzinfo assumes it IS a datetime, so a caller
    supplying a serialized (e.g. ISO string) or otherwise non-datetime
    value would otherwise raise a bare AttributeError before the freshness
    check can produce its own controlled refusal (Codex review: "Validate
    the history-fetch timestamp type before access")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=bad_value,
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_requires_now_fn_when_placement_is_enabled():
    """The single pre-loop `now` check alone only proves the window was open
    when the batch started, not when each individual order is actually
    submitted; now_fn is REQUIRED (not merely accepted) so every live
    placement reruns the check immediately beforehand, closing this for
    every caller instead of leaving it an opt-in that could be forgotten
    (Codex review: "Require a fresh clock for every live placement")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_to_place_on_a_weekend_trading_day():
    """A caller-built plan for a Saturday/Sunday trading_day must be refused
    even though nothing else here checks the calendar (Codex review: "Recheck
    market gates before live placement")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    saturday = date(2026, 8, 1)
    snapshot = dca_runner.budget_snapshot([], saturday)
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-08-01",
            snapshot=snapshot,
            trading_day=saturday,
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 8, 1, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 8, 1, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 8, 1, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_to_place_outside_the_slots_time_window():
    """now falling outside the named slot's acceptance window must refuse
    placement even though trading_day itself is a normal trading day (Codex
    review: "Recheck market gates before live placement")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 15, 45, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 15, 45, tzinfo=ET),  # long after the 0935 slot's window
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 15, 45, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_when_now_is_on_a_different_et_day_than_trading_day():
    """gate_time_sanity alone only checks the time-of-day portion of `now`
    against the slot window on `now`'s OWN date, never against `trading_day`
    itself. A caller passing yesterday's matching snapshot/run_id alongside a
    merely time-of-day-valid `now` must still be refused, or the calendar/
    budget gates would validate against the stale supplied day while today's
    real spend is excluded from every recomputed total (Codex review: "Bind
    the placement day to the current clock")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            # Time-of-day is inside the 0935 window, but the calendar day is
            # tomorrow -- the mismatch this test exists to catch.
            now=datetime(2026, 7, 29, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 29, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 29, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_a_run_id_carrying_more_than_one_date():
    """A plain substring check accepts a run_id carrying more than one
    ISO-shaped date (e.g. a multi-day batch label), passing on every one of
    those days despite deriving the identical per-ticker ref_id on each --
    an idempotent broker could then return an earlier day's order as if it
    were a fresh submission (Codex review: "Require a single-day idempotency
    key")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="batch-2026-07-28-2026-07-29",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_placement_when_a_different_ticker_collides_in_the_window():
    """The documented direct plan_orders() -> execute_plan() path never calls
    assess() at all, so this is the only place a second writer's order
    inside this slot's window is ever detected: an existing MSFT order at
    09:40 ET must stop a second 09:35 executor from submitting AAPL, even
    though MSFT would not itself collide with AAPL's per-ticker dedup checks
    (Codex review: "Recheck the sole-executor interlock before
    placement")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    colliding_order = _order("MSFT", 2.0, _utc(2026, 7, 28, 13, 40))  # 09:40 ET
    snapshot = dca_runner.budget_snapshot([colliding_order], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[colliding_order],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_orders_fetched_for_a_different_account():
    """If wiring accidentally fetches empty or incomplete history from
    another accessible account, verified_snapshot would report zero prior
    spend and execute_plan would place into the authorized account anyway.
    Requiring the caller to assert which account `orders` was fetched for,
    and verifying it against broker.account_number, catches that mismatch
    even though `orders` itself (here, an empty list) carries no evidence of
    its own origin (Codex review: "Bind placement-time history to the
    authorized account")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number="123456789",  # a different, well-formed account,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_requires_orders_fetched_at_when_placement_is_enabled():
    """`orders` itself carries no fetch timestamp; without one, a stale
    same-account fetch reused after another slot or a manual buy consumed
    the remaining cap would let `verified_snapshot` silently report a
    too-low total (Codex review: "Bind cap checks to a fresh history
    snapshot")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    with pytest.raises(ValueError):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_a_stale_orders_fetch():
    """An orders fetch from well outside MAX_ORDERS_FETCH_AGE_SECONDS of the
    placement clock must be refused -- it could predate a same-account buy
    from another slot or a manual order that already consumed the remaining
    cap (Codex review: "Bind cap checks to a fresh history snapshot")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    now = datetime(2026, 7, 28, 9, 43, tzinfo=ET)
    stale_fetch = now - timedelta(seconds=dca_runner.MAX_ORDERS_FETCH_AGE_SECONDS + 1)
    with pytest.raises(ValueError, match=r"outside the .* freshness window"):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=now,
            now_fn=lambda: now,
            slot_id="0935",
            orders_fetched_at=stale_fetch,
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_execute_plan_refuses_an_orders_fetch_from_the_future():
    """A clock/wiring bug producing orders_fetched_at after now is just as
    untrustworthy as a stale one -- neither proves the history is current
    relative to the placement clock (Codex review: "Bind cap checks to a
    fresh history snapshot")."""
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)], snapshot=snapshot, tier=tier
    )
    now = datetime(2026, 7, 28, 9, 43, tzinfo=ET)
    future_fetch = now + timedelta(seconds=5)
    with pytest.raises(ValueError, match=r"outside the .* freshness window"):
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=snapshot,
            trading_day=date(2026, 7, 28),
            orders=[],
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=now,
            now_fn=lambda: now,
            slot_id="0935",
            orders_fetched_at=future_fetch,
            tier=tier,
            buying_power_usd=TEST_BUYING_POWER_USD,
            candidates=[dca_runner.Candidate("AAPL", 2.0)],
        )


def test_runner_produces_plan_without_placing_when_disabled():
    placed = []
    broker = dca_runner.build_runner_broker(place_fn=lambda **kw: placed.append(kw))
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
        tier=BudgetTier("normal", 180.0, 900.0),
    )
    dca_runner.execute_plan(plan, broker)
    assert placed == []
    assert [c.ticker for c in plan.to_place] == ["AAPL"]


def test_execute_plan_isolates_one_rejection_and_finishes_the_batch():
    """The 2026-08-24 policy change, and the incident that motivated it.

    This used to assert the opposite ("stops entire batch on first
    rejection"), and that is exactly what produced "GS, EVR: never submitted
    -- batch halted after RVII rejection": one ticker the broker refused cost
    every ticker behind it in the plan its buy for the day. A rejection is now
    isolated to its own ticker -- recorded, never retried -- and the tickers
    after it are still placed.
    """
    calls = []

    def flaky(**kw):
        calls.append(kw["symbol"])
        if kw["symbol"] == "MSFT":
            # A refusal RESPONSE, not a raise: this is the reported incident's
            # own shape (the broker returning "cannot buy this symbol"), and
            # since 2026-08-24 it is also the only shape that isolates -- a
            # raise proves nothing about whether an order landed and halts.
            return {"success": False, "error": "rejected by broker"}
        return {"id": "ok"}

    broker = dca_runner.build_runner_broker(
        place_fn=flaky, enable_live_orders=True, dry_run=False, account_number=TEST_ACCOUNT_NUMBER
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0),
                    dca_runner.Candidate("MSFT", 2.0),
                    dca_runner.Candidate("NVDA", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0),
                    dca_runner.Candidate("MSFT", 2.0),
                    dca_runner.Candidate("NVDA", 2.0)],
    )

    assert calls == ["AAPL", "MSFT", "NVDA"]  # NVDA is no longer collateral damage
    assert result.submitted == ["AAPL", "NVDA"]
    assert [f.ticker for f in result.failures] == ["MSFT"]
    assert "rejected by broker" in result.failures[0].reason
    assert result.halted_on is None
    assert result.failure_reason is None
    # No retry: the refused ticker is attempted exactly once.
    assert calls.count("MSFT") == 1


def _three_ticker_plan(tickers):
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate(t, 2.0) for t in tickers]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    return plan, snapshot, tier, candidates


def _execute(plan, broker, snapshot, tier, candidates):
    return dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier,
        buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=candidates,
    )


def test_the_reported_incident_shape_no_longer_loses_the_tickers_behind_a_rejection():
    """Reproduces "GS, EVR: never submitted -- batch halted after RVII
    rejection" against the current code: the refused ticker is recorded, and
    the two behind it are bought.

    IBM stands in for RVII as the refused symbol. RVII itself can no longer
    reach this path at all -- it moved to WHOLE_SHARE_TICKERS on 2026-08-24
    (see near_close_dca.WHOLE_SHARE_TICKERS), so execute_plan's whitelist
    check refuses it outright, which fixes that ONE symbol. This test is about
    the general defect the incident exposed: any refused ticker used to take
    the rest of the plan down with it. Given a 5x amount so plan_orders()'s
    allocation priority puts it first, exactly where RVII sat.
    """
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if kw["symbol"] == "IBM":
            return {"success": False, "error": "not tradable for fractional buys"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [
        dca_runner.Candidate("IBM", 10.0),  # 5x -> highest allocation priority
        dca_runner.Candidate("GS", 2.0),
        dca_runner.Candidate("EVR", 2.0),
    ]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    assert [o.ticker for o in plan.to_place][0] == "IBM", "fixture must place the refused ticker first"

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [o.ticker for o in plan.to_place]  # every planned order attempted
    assert set(result.submitted) == {"GS", "EVR"}
    assert [f.ticker for f in result.failures] == ["IBM"]
    assert "not tradable" in result.failures[0].reason
    assert result.halted_on is None


def test_a_whole_share_ticker_can_no_longer_reach_the_fractional_placement_path():
    """The other half of the RVII fix: a whole-share ticker is not in
    ACTIVE_TICKERS, so execute_plan refuses a plan containing it before any
    order is placed rather than discovering it at the broker."""
    for ticker in WHOLE_SHARE_TICKERS:
        assert ticker not in dca_runner.ACTIVE_TICKERS


@pytest.mark.parametrize(
    "response,may_hold_live_order",
    [
        # Proven nothing was accepted -> isolate, the reported incident's shape.
        # No order id AND nothing said about execution.
        ({"success": False, "error": "not tradable for fractional buys"}, False),
        ({}, False),
        # An empty id is not usable, but "queued" is acceptance evidence on its
        # own, so this halts rather than isolating.
        ({"order": {"id": "", "state": "queued"}}, True),
        # ... whereas the same unusable id with nothing said about execution
        # is still an ordinary refusal.
        ({"order": {"id": "", "state": "rejected"}}, False),
        # An accepted id on an ENCLOSING envelope, with the inner one carrying
        # only an error. The id lookup used to read the deepest mapping alone
        # while the fill scan already read every level, so this isolated an
        # order that may still be live (Codex review, PR #786: "Inspect order
        # IDs across every response envelope").
        ({"success": False, "id": "ORD", "state": "queued",
          "data": {"error": "timeout"}}, True),
        # A truthy-but-INVALID `id` beside a valid `order_id`. `a or b` picks
        # `a` whenever it is truthy, so this short-circuited the fallback and
        # then failed validation, reporting no id at all (Codex review,
        # PR #786: "Check order_id after rejecting an invalid id").
        ({"success": False, "id": True, "order_id": "ORD", "state": "queued"}, True),
        # The falsy invalid ids never had the problem -- `or` falls through
        # them -- but pin them so a future rewrite cannot reintroduce it.
        ({"success": False, "id": 0, "order_id": "ORD", "state": "queued"}, True),
        ({"success": False, "id": [], "order_id": "ORD", "state": "queued"}, True),
        # ... and an unusable id with NO valid sibling still names no order,
        # so with nothing said about execution it stays isolatable.
        ({"success": False, "id": True, "error": "not tradable"}, False),
        # The id and the state still come from ONE level -- an enclosing id
        # with a terminal state and an explicit zero fill on that same level
        # is proof, and still isolates.
        ({"success": False, "id": "ORD", "state": "rejected",
          "cumulative_quantity": 0, "data": {"error": "refused"}}, False),
        # ... and an enclosing id whose own level says nothing about the fill
        # halts, exactly as it does when the id is on the deepest mapping.
        ({"success": False, "id": "ORD", "state": "cancelled",
          "data": {"error": "refused"}}, True),
        # EVERY named order has to be provably dead, not just the first one
        # found. A wrapper can report its own request identity on the outer
        # envelope -- terminal, explicitly zero-filled -- while the real
        # broker order sits queued underneath. Reading only the outermost
        # identity saw proof and isolated, leaving ORD live at the broker and
        # absent from every remaining order's cap and buying-power arithmetic
        # (Codex review, PR #786: "Inspect all response identities before
        # isolating").
        ({"success": False, "id": "REQ", "state": "failed",
          "cumulative_quantity": 0,
          "data": {"order": {"id": "ORD", "state": "queued"}}}, True),
        # ... and the mirror image: a live outer identity over a dead inner
        # one halts too, so the rule is not "check the innermost instead".
        ({"id": "REQ", "state": "queued", "cumulative_quantity": 0,
          "data": {"order": {"id": "ORD", "state": "rejected",
                             "cumulative_quantity": 0}}}, True),
        # An identity whose own level omits the state is not provably dead
        # either, even when a deeper identity is.
        ({"id": "REQ", "cumulative_quantity": 0,
          "data": {"order": {"id": "ORD", "state": "rejected",
                             "cumulative_quantity": 0}}}, True),
        # ... but when EVERY named order is dead and every supplied fill
        # figure is an explicit zero, the response is still proof and still
        # isolates -- the multi-identity rule must not turn ordinary wrapped
        # rejections into halts.
        ({"success": False, "id": "REQ", "state": "rejected",
          "cumulative_quantity": 0,
          "data": {"order": {"id": "ORD", "state": "cancelled",
                             "cumulative_quantity": 0}}}, False),
        # ... and "every supplied figure" is asked PER ORDER, not once for
        # the response. One identity's explicit zero must not answer for a
        # sibling that said nothing: REQ's zero used to isolate the whole
        # response while ORD -- terminal, accepted, and silent about its fill
        # -- was never required to rule a partial fill out (Codex review,
        # PR #786: "Require zero fill evidence for each terminal identity").
        ({"success": False, "id": "REQ", "state": "rejected",
          "cumulative_quantity": 0,
          "data": {"order": {"id": "ORD", "state": "cancelled"}}}, True),
        # Symmetrically, the silent one may be the OUTER identity.
        ({"success": False, "id": "REQ", "state": "failed",
          "data": {"order": {"id": "ORD", "state": "cancelled",
                             "cumulative_quantity": 0}}}, True),
        # Attribution is by WHERE the figure sits: a figure on an envelope
        # that names an order describes THAT order, and one on an envelope
        # naming none is unattributed and counts toward every identity. The
        # second half is what keeps the round-15 shape isolating -- a lone
        # order reported deep, its zero attached to an outer wrapper envelope
        # that names nothing.
        ({"success": False, "cumulative_quantity": 0,
          "data": {"order": {"id": "ORD", "state": "cancelled"}}}, False),
        # ... and an unattributed POSITIVE figure still poisons every
        # identity, however many there are.
        ({"success": False, "executed_notional": 5,
          "data": {"order": {"id": "ORD", "state": "cancelled",
                             "cumulative_quantity": 0}}}, True),
        # ... but "per order" means per DISTINCT order. A wrapper echoing one
        # id at two levels, with the fill reported on only one of them, was
        # read as two orders, and the echo -- carrying no figure of its own --
        # was asked for evidence it could never supply, so an ordinary
        # rejected order with an explicit zero halted the batch (Codex review,
        # PR #786: "Deduplicate envelopes that repeat the same order ID").
        ({"id": "ORD", "state": "rejected", "cumulative_quantity": 0,
          "data": {"id": "ORD", "state": "rejected"}}, False),
        # ... and ids match on TYPE as well as characters. Numeric 5 and
        # string "5" are not provably one order, and stringifying alone merged
        # them, letting the outer request's explicit zero answer for an inner
        # order that reported no fill at all (Codex review, PR #786: "Keep
        # differently typed order IDs in separate groups"). Merging is the
        # dangerous direction; splitting at worst costs a halt.
        ({"id": 5, "state": "rejected", "cumulative_quantity": 0,
          "data": {"id": "5", "state": "rejected"}}, True),
        # Same type and same value still group, which is the case the grouping
        # exists for.
        ({"id": 5, "state": "rejected", "cumulative_quantity": 0,
          "data": {"id": 5, "state": "rejected"}}, False),
        # `1`, `1.0` and `True` all hash equal in Python, so a raw-value key
        # would have merged them; the type name in the key keeps them apart.
        ({"id": 1, "state": "rejected", "cumulative_quantity": 0,
          "data": {"order": {"id": 1.0, "state": "cancelled"}}}, True),
        # ... and grouping never lets two levels of ONE order disagree: a
        # rejected outer over a queued inner is not provably dead.
        ({"id": "ORD", "state": "rejected", "cumulative_quantity": 0,
          "data": {"id": "ORD", "state": "queued"}}, True),
        # Ids are matched exactly, never case-folded: folding would merge two
        # genuinely distinct orders, and merging is the unsafe direction.
        ({"id": "ORD", "state": "rejected", "cumulative_quantity": 0,
          "data": {"id": "ord", "state": "rejected"}}, True),
        # The traversal follows the whole response, not the one
        # response->data->data.order branch the descent picks. A wrapper can
        # hang the accepted order off the OUTER envelope as a sibling of
        # `data`, and following only the branch never visited it at all (Codex
        # review, PR #786: "Inspect order mappings outside the selected data
        # branch").
        ({"success": False, "order": {"id": "ORD", "state": "queued"},
          "data": {"error": "timeout"}}, True),
        # ... including through lists: an `orders` array is an obvious way to
        # report more than one.
        ({"success": False, "data": {"orders": [{"id": "ORD", "state": "queued"}]}}, True),
        # An unattributed zero can only vouch for ONE order. With two named
        # orders, a single zero on an id-less wrapper cannot say which of them
        # it describes -- or whether it is their sum -- so it proves neither
        # (Codex review, PR #786: "Require distinct zero-fill evidence for
        # each order").
        ({"success": False, "cumulative_quantity": 0,
          "orders": [{"id": "A", "state": "cancelled"},
                     {"id": "B", "state": "cancelled"}]}, True),
        # ... and when each order carries its OWN zero, both are proven.
        ({"success": False, "cumulative_quantity": 0,
          "orders": [{"id": "A", "state": "cancelled", "cumulative_quantity": 0},
                     {"id": "B", "state": "cancelled", "cumulative_quantity": 0}]}, False),
        # The single-order case is untouched: one named order, its zero on an
        # outer envelope that names nothing, still isolates.
        ({"success": False, "cumulative_quantity": 0,
          "data": {"order": {"id": "ORD", "state": "cancelled"}}}, False),
        # One envelope naming TWO DIFFERENT orders cannot be attributed: its
        # state and fill figures are single-valued, so there is no saying which
        # order they describe. The first id used to win and the second was
        # never recorded, so this read as one proven-unfilled order (Codex
        # review, PR #786: "Inspect both identifier keys in each envelope").
        ({"success": False, "id": "REQ", "order_id": "ORD", "state": "rejected",
          "cumulative_quantity": 0}, True),
        # ... but one id echoed under BOTH keys is one order, and still
        # isolates on its explicit zero.
        ({"success": False, "id": "ORD", "order_id": "ORD", "state": "rejected",
          "cumulative_quantity": 0}, False),
        # An order id is not the ONLY way a response says the broker accepted
        # something. Without an id, a working or completed state is itself
        # acceptance evidence, and isolating it left that order out of
        # `submitted` and out of every remaining order's cap and buying-power
        # arithmetic (Codex review, PR #786: "Honor working states even when
        # an order ID is missing").
        ({"success": False, "state": "queued"}, True),
        ({"success": False, "state": "confirmed"}, True),
        ({"success": False, "state": "partially_filled"}, True),
        ({"success": False, "state": "filled"}, True),
        ({"success": False, "data": {"state": "new"}}, True),
        # ... scoped to states that READ as acceptance, unlike the with-an-id
        # branch where anything unreadable halts. Without an id, an absent,
        # null, or unfamiliar state is the ordinary shape of a refusal, and
        # halting on it would restore halt-on-first-rejection.
        ({"success": False, "state": "rejected"}, False),
        ({"success": False, "state": None, "error": "not tradable"}, False),
        ({"success": False, "state": "broker_specific_refusal"}, False),
        # A missing id alongside an explicit zero fill is still isolatable.
        ({"data": {"state": "cancelled", "cumulative_quantity": 0}}, False),
        # ... but a missing id alongside a REPORTED fill is real spend, and the
        # absent id makes it less accountable, not more (Codex review, PR #786:
        # "Check fill evidence before isolating missing-ID responses").
        ({"data": {"state": "cancelled", "executed_dollar_amount": 5}}, True),
        ({"data": {"state": "rejected", "cumulative_quantity": "2.5"}}, True),
        ({"data": {"state": "cancelled", "cumulative_quantity": "many"}}, True),
        # A terminal order that EXPLICITLY reports a zero fill is isolatable.
        ({"order": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0}}, False),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": "0.0000"}}, False),
        # ... but EVERY supplied figure has to rule execution out, not just the
        # first one read. A quantity and a notional describe the same fill in
        # different units, and a wrapper can populate one and default the other.
        ({"order": {"id": "ORD", "state": "cancelled",
                    "cumulative_quantity": 0, "executed_dollar_amount": 5}}, True),
        ({"order": {"id": "ORD", "state": "cancelled",
                    "cumulative_quantity": "0", "executed_notional": "12.50"}}, True),
        ({"order": {"id": "ORD", "state": "cancelled",
                    "cumulative_quantity": 0, "executed_notional": float("nan")}}, True),
        # Zeroes in every supplied field still isolate.
        ({"order": {"id": "ORD", "state": "cancelled",
                    "cumulative_quantity": 0, "executed_dollar_amount": "0.00"}}, False),
        # A null alongside an explicit zero does not spoil the proof: null is
        # "not reported", exactly like an omitted key.
        ({"order": {"id": "ORD", "state": "cancelled",
                    "cumulative_quantity": 0, "executed_notional": None}}, False),
        # ... but an OMITTED, null, or blank fill figure establishes nothing.
        # A usable order id in this codebase is itself evidence the broker
        # accepted something, so an unquantified terminal order halts.
        ({"order": {"id": "ORD", "state": "rejected"}}, True),
        ({"order": {"id": "ORD", "state": "cancelled"}}, True),
        ({"order": {"id": "ORD", "state": "expired"}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": None}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": "  "}}, True),
        # ... but a terminal order that PARTIALLY FILLED before it died moved
        # real money, and that spend is in neither `submitted` nor the batch's
        # arithmetic -> halt (Codex review, PR #786: "Reconcile partial fills
        # before isolating terminal responses").
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": "3.0"}}, True),
        ({"order": {"id": "ORD", "state": "rejected", "executed_notional": 4.5}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "filled_quantity": 1}}, True),
        # An unreadable fill figure cannot RULE OUT a fill, so it halts too --
        # the same asymmetry BrokerOrder.counts_toward_budget applies.
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": float("nan")}}, True),
        # An arbitrarily large JSON integer: math.isfinite converts to float
        # first and RAISES rather than answering, and that exception would
        # escape execute_plan's placement try block and take the whole
        # ExecutionResult -- including already-submitted orders -- with it
        # (Codex review, PR #786: "Treat oversized fill integers as
        # unreadable").
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": 10 ** 1000}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": "1" + "0" * 400}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": -1}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": True}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": "many"}}, True),
        ({"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": {"v": 0}}}, True),
        # A fill reported OUTSIDE the deepest mapping is still a fill. The
        # classifier reads the id and state off one selected envelope, but
        # wrappers attach the executed quantity/notional at whichever level
        # they please -- on the outermost envelope beside `success`, or beside
        # `data.order` rather than inside it. Scanning only the deepest
        # mapping discarded that figure and reported real spend as proof that
        # no order exists (Codex review, PR #786: "Inspect fill evidence
        # across every response envelope").
        ({"success": False, "executed_dollar_amount": 5,
          "data": {"state": "cancelled"}}, True),
        ({"success": False, "data": {"executed_dollar_amount": 5,
                                     "order": {"state": "cancelled"}}}, True),
        ({"success": False, "data": {"executed_notional": 5,
                                     "order": {"id": "ORD", "state": "cancelled",
                                               "cumulative_quantity": 0}}}, True),
        ({"cumulative_quantity": 3, "data": {"order": {"id": "ORD", "state": "rejected",
                                                       "cumulative_quantity": 0}}}, True),
        # ... and an outer envelope that says nothing does not spoil an inner
        # explicit zero: the combined verdict is still "proven unfilled".
        ({"success": False, "data": {"order": {"id": "ORD", "state": "cancelled",
                                               "cumulative_quantity": 0}}}, False),
        ({"success": False, "executed_dollar_amount": 0,
          "data": {"order": {"id": "ORD", "state": "cancelled",
                             "cumulative_quantity": 0}}}, False),
        # A live-looking order whose details we could not verify -> halt.
        ({"id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy",
          "dollar_amount": "100.00"}, True),
        ({"id": "ORD", "state": "queued", "symbol": "MSFT"}, True),
        ({"success": False, "error": "denied", "id": "ORD", "state": "queued"}, True),
        # Unreadable shapes fail SAFE (halt), never toward continuing.
        (None, True),
        ("nope", True),
        ({"data": None}, True),
        ({"order": []}, True),
    ],
)
def test_halt_vs_isolate_follows_whether_a_live_order_may_exist(response, may_hold_live_order):
    """The rule the detection tests above defer to.

    A failure that PROVES nothing was accepted is isolated: the remaining
    orders' caps and buying power are untouched. A response that may still hold
    a live order halts -- the canonical case is $100 confirmed for a requested
    $2, where an order exists for an amount no pre-flight check accounted for
    (Codex review, PR #786: "Halt after responses that may represent accepted
    orders")."""
    assert dca_runner._placement_response_may_hold_a_live_order(response) is may_hold_live_order


def test_a_live_looking_mismatched_response_halts_the_batch():
    """End to end: the $100-for-$2 case must not let the rest of the batch
    place against cap arithmetic that is already wrong."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"id": "ORD", "state": "queued", "symbol": kw["symbol"],
                    "side": "buy", "dollar_amount": "100.00"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.submitted == []
    assert result.halted_on == plan.to_place[0].ticker
    assert "may still hold a live order" in result.failure_reason
    # Still recorded as a per-ticker failure too, so Gate 9 names it.
    assert [f.ticker for f in result.failures] == [plan.to_place[0].ticker]


def test_a_success_flagged_response_without_an_id_halts_the_batch():
    """Exercise the acceptance-evidence rule through ``execute_plan``.

    The focused classifier test protects the response vocabulary; this test
    protects the caller contract that ambiguous acceptance stops subsequent
    placements rather than merely recording an isolated ticker failure.
    """
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": True}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [plan.to_place[0].ticker]
    assert result.submitted == []
    assert result.halted_on == plan.to_place[0].ticker
    assert "may still hold a live order" in result.failure_reason
    assert [failure.ticker for failure in result.failures] == [plan.to_place[0].ticker]


@pytest.mark.parametrize(
    "ambiguous_response",
    [
        {
            "success": False,
            "id": "REQ",
            "order_id": "ORD",
            "state": "rejected",
            "cumulative_quantity": 0,
        },
        {
            "success": False,
            "order": {"id": "ORD", "state": "queued"},
            "data": {"error": "timeout"},
        },
        {"isError": True, "error": "timeout"},
        {"success": False, "results": [[{"id": "ORD", "state": "queued"}]]},
    ],
)
def test_ambiguous_response_shapes_halt_execute_plan(ambiguous_response):
    """The exhaustive envelope checks must govern the placement loop too.

    Unit-level classifier coverage is not enough: these shapes previously let
    ``execute_plan`` continue after an unaccounted broker order. Pin the
    public execution result and prove that no later ticker reaches the broker.
    """
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        return ambiguous_response

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [plan.to_place[0].ticker]
    assert result.submitted == []
    assert result.halted_on == plan.to_place[0].ticker
    assert "may still hold a live order" in result.failure_reason
    assert [failure.ticker for failure in result.failures] == [plan.to_place[0].ticker]


def test_a_cancelled_order_that_partially_filled_halts_the_batch():
    """A terminal state is not proof nothing was bought.

    An order that filled 3 shares and was THEN cancelled has moved real money
    that appears in neither ``submitted`` nor the caps the remaining orders are
    placed against -- exactly the accounting hole
    ``BrokerOrder.counts_toward_budget`` closes for broker history (Codex
    review, PR #786: "Reconcile partial fills before isolating terminal
    responses")."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"order": {"id": "ORD", "state": "cancelled",
                              "cumulative_quantity": "3.0000"}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.halted_on == plan.to_place[0].ticker
    assert [f.ticker for f in result.failures] == [plan.to_place[0].ticker]


def test_a_cancelled_order_reporting_an_explicit_zero_fill_is_still_isolated():
    """The other side of it: a terminal order that EXPLICITLY reports a zero
    fill must stay isolatable -- otherwise the partial-fill guard would
    silently restore halt-on-first-rejection, the behavior this whole change
    exists to remove."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"order": {"id": "ORD", "state": "rejected",
                              "cumulative_quantity": "0.0000"}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [o.ticker for o in plan.to_place]
    assert len(result.submitted) == 2
    assert result.halted_on is None


def test_the_reported_incident_shape_is_isolated_without_any_fill_evidence():
    """The guard must not reach for fill evidence that a real refusal never
    carries. A broker refusing a ticker outright returns an error envelope with
    NO order id, so it is isolated before the terminal-state branch runs at all
    -- which is why requiring an explicit zero there costs the reported
    incident nothing."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False, "error": "not tradable for fractional buys"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [o.ticker for o in plan.to_place]
    assert len(result.submitted) == 2
    assert result.halted_on is None


def test_a_zero_quantity_alongside_a_positive_notional_halts():
    """End to end: the first fill field read must not decide it alone.

    A wrapper that reports the executed dollars but leaves the share count at
    its zero default describes a real fill; returning on that leading zero
    classified it as definitely unfilled and dropped the spend from the
    remaining orders' cap arithmetic (Codex review, PR #786: "Inspect all fill
    fields before ruling out execution")."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"order": {"id": "ORD", "state": "cancelled",
                              "cumulative_quantity": 0,
                              "executed_dollar_amount": 5}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.halted_on == plan.to_place[0].ticker


def test_a_missing_order_id_reporting_a_fill_halts():
    """A missing id is not a licence to ignore a reported fill.

    The no-id branch runs before the terminal-state check, so it used to return
    "nothing was accepted" without ever looking at the supplied fill figures --
    dropping the spend in a response like {"success": false, "data": {"state":
    "cancelled", "executed_dollar_amount": 5}} out of the remaining orders'
    arithmetic (Codex review, PR #786: "Check fill evidence before isolating
    missing-ID responses")."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False,
                    "data": {"state": "cancelled", "executed_dollar_amount": 5}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.halted_on == plan.to_place[0].ticker


def test_a_fill_reported_outside_the_deepest_envelope_halts():
    """The descent selects ONE mapping for the id and state; the fill scan must
    not inherit that narrowing.

    A wrapper can report the executed dollars on the outer envelope beside
    `success`, or beside `data.order` rather than inside it. Reading only the
    deepest mapping threw that figure away and classified real spend as proof
    that no order exists (Codex review, PR #786: "Inspect fill evidence across
    every response envelope")."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False, "executed_dollar_amount": 5,
                    "data": {"order": {"id": "ORD", "state": "cancelled",
                                       "cumulative_quantity": 0}}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.halted_on == plan.to_place[0].ticker


def test_an_outer_envelope_saying_nothing_does_not_spoil_an_inner_zero():
    """The guard against over-correcting the finding above.

    Combining envelopes must not turn every ordinary refusal into a halt: an
    envelope that reports no fill figure at all contributes nothing, so an
    inner EXPLICIT zero still proves the order never executed and the ticker
    is still isolated."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False,
                    "data": {"order": {"id": "ORD", "state": "cancelled",
                                       "cumulative_quantity": 0}}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [o.ticker for o in plan.to_place]
    assert len(result.submitted) == 2
    assert result.halted_on is None


def test_an_order_id_on_an_enclosing_envelope_is_still_found():
    """The id lookup has to be as wide as the fill scan.

    A wrapper that reports the accepted id beside `success` and an error
    inside `data` read as "no order id anywhere"; with no fill reported
    either, the batch isolated and kept placing while that order may have been
    live at the broker (Codex review, PR #786: "Inspect order IDs across every
    response envelope")."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False, "id": "ORD", "state": "queued",
                    "data": {"error": "timeout"}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.halted_on == plan.to_place[0].ticker


def test_an_invalid_id_does_not_hide_a_valid_order_id():
    """`a or b` picks `a` on truthiness, before validity is known.

    A broker envelope carrying `{"id": true, "order_id": "ORD"}` selected the
    boolean, failed the scalar check, and reported no order id -- so with no
    fill reported the batch isolated and kept placing while ORD may have been
    live (Codex review, PR #786: "Check order_id after rejecting an invalid
    id"). Only `True` is both truthy and invalid; 0/[]/{} always fell
    through."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False, "id": True, "order_id": "ORD", "state": "queued"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.halted_on == plan.to_place[0].ticker


def test_widening_the_id_search_did_not_break_the_reported_incident():
    """The guard against over-correcting.

    A broker refusing a ticker outright names no order at ANY level and says
    nothing about execution, so it still costs only its own ticker -- which is
    the whole point of this PR."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False, "error": "not tradable for fractional buys"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [o.ticker for o in plan.to_place]
    assert len(result.submitted) == 2
    assert result.halted_on is None


def test_silence_about_the_fill_is_read_differently_with_and_without_an_id():
    """The one asymmetry worth pinning explicitly.

    With an order id, the broker accepted something and saying nothing about
    the fill leaves that unresolved -> halt. Without an id, saying nothing
    about the fill is the ordinary shape of a refusal -> isolate. Collapsing
    the two would either lose real spend or undo the whole change.

    The no-id side uses `rejected`, not `cancelled`: a rejection is the
    refusal this asymmetry exists to keep isolatable, whereas a cancellation
    says an order existed first and is pinned separately below."""
    with_id = {"order": {"id": "ORD", "state": "rejected"}}
    without_id = {"order": {"state": "rejected"}}

    assert dca_runner._placement_response_may_hold_a_live_order(with_id) is True
    assert dca_runner._placement_response_may_hold_a_live_order(without_id) is False


def test_a_truncated_response_walk_halts_rather_than_reporting_no_order():
    """The envelope walk is bounded, and reaching a bound is NOT harmless.

    I claimed the opposite when the bounds went in -- that inspecting fewer
    envelopes "can only make the classifier halt more readily". It is exactly
    backwards: an order-bearing branch left unvisited reads as no order at
    all, which ISOLATES. So a walk that could not finish reports that, and the
    classifier treats it like any other shape it cannot fully read (Codex
    review, PR #786: "Fail closed when response traversal is truncated")."""
    nested: dict[str, Any] = {"id": "ORD", "state": "queued"}
    for _ in range(7):
        nested = {"wrapper": nested}
    deep = {"success": False, "data": nested}
    envelopes, truncated, _ = dca_runner._response_envelopes(
        deep, deep["data"], deep["data"]
    )
    assert truncated, "fixture must exceed the depth bound"
    assert dca_runner._placement_response_may_hold_a_live_order(deep) is True

    # The node cap fails closed the same way, even when nothing is deep.
    wide = {"success": False, "data": {f"k{i}": {"noise": i} for i in range(80)}}
    assert dca_runner._placement_response_may_hold_a_live_order(wide) is True

    # ... and an ordinary response is nowhere near either bound, so the
    # reported incident's own shape still isolates.
    shallow = {"success": False, "error": "not tradable for fractional buys"}
    _, shallow_truncated, _ = dca_runner._response_envelopes(shallow, shallow, shallow)
    assert not shallow_truncated
    assert dca_runner._placement_response_may_hold_a_live_order(shallow) is False


def test_a_missing_id_cancellation_still_needs_an_explicit_zero():
    """`cancelled` and `expired` are not refusals. An order must exist before
    it can be cancelled and must be working before it can expire, so either may
    have partially filled first -- and with no order id that spend is LESS
    accountable, not more. The with-an-id branch has always demanded an
    explicit zero for exactly this; the no-id branch treated these as ordinary
    refusals (Codex review, PR #786: "Require zero fills for missing-ID
    cancellations")."""
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "state": "cancelled"}
    ) is True
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "state": "expired"}
    ) is True
    # An EXPLICIT zero still proves it and still isolates.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "state": "cancelled", "cumulative_quantity": 0}
    ) is False
    # ... and `rejected`/`failed`/`voided` stay ordinary refusals, or the first
    # untradable ticker would halt the batch and undo the whole change.
    for refusal in ("rejected", "failed", "voided"):
        assert dca_runner._placement_response_may_hold_a_live_order(
            {"success": False, "state": refusal, "error": "not tradable"}
        ) is False, refusal


def test_an_id_less_acceptance_state_is_read_beside_a_named_order():
    """Naming one order does not license ignoring what the response says about
    levels that name none. The per-identity loop only ever inspects envelopes
    that carry an id, so once any order was named the ID-less acceptance-state
    check was skipped outright and `{"state": "queued", "data": {"id": "ORD",
    "state": "rejected", "cumulative_quantity": 0}}` isolated (Codex review,
    PR #786: "Inspect ID-less acceptance states beside named orders")."""
    for live_state in ("queued", "confirmed", "partially_filled", "filled", "new"):
        assert dca_runner._placement_response_may_hold_a_live_order(
            {
                "state": live_state,
                "data": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0},
            }
        ) is True, live_state

    # Both readings of that shape demand a halt, so no disambiguation is
    # needed: either the ID-less level describes a SECOND, unnamed order that
    # is live and in nobody's arithmetic, or it describes ORD itself in a state
    # contradicting `rejected` -- and the per-identity loop already halts on
    # exactly that disagreement when both states sit on id-bearing levels.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "data": {"id": "ORD", "state": "rejected"},
            "order": {"id": "ORD", "state": "queued"},
        }
    ) is True

    # The ordinary shapes still isolate. An outer level that merely RESTATES
    # the named order's own refusal says nothing new...
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "state": "rejected",
            "data": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0},
        }
    ) is False
    # ... and an ordinary rejection with no outer state at all is untouched.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "success": False,
            "data": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0},
        }
    ) is False
    # `cancelled`/`expired` are deliberately NOT re-checked here: an outer
    # level restating a named order's cancellation would otherwise halt every
    # ordinary cancellation, and _terminal_order_may_have_filled already reads
    # unattributed envelopes and knows when one named order's explicit zero
    # vouches for them.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "state": "cancelled",
            "data": {"id": "ORD", "state": "cancelled", "cumulative_quantity": 0},
        }
    ) is False


def test_an_id_less_terminal_state_that_contradicts_a_named_order_halts():
    """The other half of the ID-less level rule, and a correction to how round
    28 scoped it. That round excluded `cancelled`/`expired` on unattributed
    levels wholesale, justified as "an unnamed level restating a named order's
    own cancellation is not new information" — true, but the rule was written
    broader than its own justification. `{"state": "cancelled", "data":
    {"id": "ORD", "state": "rejected", ...}}` restates nothing: no named order
    reports `cancelled`, so that level is describing something else, which may
    have partially filled before it died (Codex review, PR #786: "Inspect
    ID-less terminal states beside named orders")."""
    # Broker state strings are normalized before comparison; keep mixed-case
    # and padded variants in the regression matrix so a cosmetic spelling
    # difference cannot hide the contradictory unnamed order again.
    for outer in ("cancelled", "canceled", "expired", " CANCELLED ", "Expired"):
        assert dca_runner._placement_response_may_hold_a_live_order(
            {
                "state": outer,
                "data": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0},
            }
        ) is True, outer

    # A genuine restatement is still not new information, and still isolates —
    # otherwise every ordinary cancellation echoed at two levels would halt.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "state": "cancelled",
            "data": {"id": "ORD", "state": "cancelled", "cumulative_quantity": 0},
        }
    ) is False
    # ... including when the echo is one of several named orders' states.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "state": "cancelled",
            "data": {"id": "A", "state": "rejected", "cumulative_quantity": 0},
            "order": {"id": "B", "state": "cancelled", "cumulative_quantity": 0},
        }
    ) is False
    # An unattributed level in a REFUSAL state says no order was created, so
    # it is not read this way at all — same exclusion as everywhere else.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "state": "rejected",
            "data": {"id": "ORD", "state": "cancelled", "cumulative_quantity": 0},
        }
    ) is False
    # ... and a level with no state contributes nothing either way.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "success": False,
            "data": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0},
        }
    ) is False


def test_a_success_flagged_response_with_no_order_id_halts():
    """`{"success": true}` reaches this classifier having ALREADY been read by
    `_placement_failure_reason`, which rejected it only for want of an order id
    — nothing said the placement failed. The no-identity branch then found no
    state and no fill and isolated it, so the batch continued past a response
    whose wrapper explicitly said the order went through (Codex review,
    PR #786: "Halt on success-flagged responses that omit an ID")."""
    for key in ("success", "ok", "approved"):
        assert dca_runner._placement_response_may_hold_a_live_order({key: True}) is True, key
        # Read at any level, like every other signal here.
        assert dca_runner._placement_response_may_hold_a_live_order(
            {"data": {key: True}}
        ) is True, key
    # Precondition: these are exactly the responses the failure reader passes
    # through for want of an id, not because anything reported a failure.
    assert dca_runner._placement_failure_reason(
        {"success": True}, expected_symbol="RVII", expected_side="buy",
        expected_amount_usd=2.0,
    ) == "broker response carried no order id"

    # An explicit zero fill does NOT rescue it: the flag says an order was
    # accepted, and an accepted order can still fill later. Same posture as
    # `_accepted_state_reported`, which ignores fill figures for the same
    # reason.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": True, "cumulative_quantity": 0}
    ) is True

    # STRICT booleans only, matching `_placement_failure_reason`'s own reading
    # of these keys. A truthy non-bool is arbitrary connector data, and
    # treating it as acceptance would halt ordinary refusals.
    for not_the_flag in ("true", 1, "yes", [1]):
        assert dca_runner._placement_response_may_hold_a_live_order(
            {"success": not_the_flag}
        ) is False, not_the_flag

    # The reported incident's own shape is untouched.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "error": "not tradable for fractional buys"}
    ) is False
    # ... and once a response NAMES an order, `success: true` is describing the
    # API call rather than that order's fate, so it is not read this way at
    # all — otherwise an ordinary rejection under a successful call would halt.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {
            "success": True,
            "data": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0},
        }
    ) is False


def test_both_order_id_validators_agree_on_every_shape():
    """`_usable_order_ids` and `_placement_failure_reason` each decide whether
    a response names an order, on independent traversals. They disagreed:
    this side rejected only falsy/bool/non-scalar, so a whitespace-only,
    negative or non-finite id read as a NAMED order here while the failure
    reader called it no id at all.

    That is not the harmless direction. Naming an order routes the response
    past the no-identity branch, which is where the acceptance-state and
    success-flag checks live (Codex review, PR #786: "Treat invalid IDs as
    missing before reading success flags")."""
    invalid = ["", "   ", "\t\n", 0, -1, -0.5, float("nan"), float("inf"),
               float("-inf"), True, False, None, ["ORD"], {"value": "ORD"},
               # An oversized JSON integer belongs in this list, and reaching
               # it used to RAISE rather than return a verdict: math.isfinite()
               # converts to float first, so 10**1000 gave OverflowError on
               # both traversals (Codex review, PR #786: "Treat oversized
               # numeric order IDs as unusable").
               10 ** 1000, -(10 ** 1000)]
    valid = ["ORD", " ORD ", "0", 1, 42, 3.5]

    for order_id in invalid:
        assert not dca_runner._is_usable_order_id(order_id), order_id
        assert dca_runner._usable_order_ids({"id": order_id}) == [], order_id
        assert dca_runner._usable_order_id({"id": order_id}) is None, order_id
        # ... and the failure reader agrees it is no usable id, whatever
        # message it picks. Agreement is the invariant; the wording is not.
        reason = dca_runner._placement_failure_reason(
            {"id": order_id}, expected_symbol="RVII", expected_side="buy",
            expected_amount_usd=2.0,
        )
        assert reason is not None and "order id" in reason, (order_id, reason)

    for order_id in valid:
        assert dca_runner._is_usable_order_id(order_id), order_id
        assert dca_runner._usable_order_ids({"id": order_id}) == [order_id], order_id

    # The shape the finding named, plus its numeric siblings: an explicit
    # success flag now reaches the no-identity check instead of being routed
    # past it by an unusable id.
    for bad_id in ("   ", -1, float("nan"), float("inf")):
        assert dca_runner._placement_response_may_hold_a_live_order(
            {"success": True, "id": bad_id, "state": "rejected",
             "cumulative_quantity": 0}
        ) is True, bad_id

    # A REAL id under a success flag still isolates — there the flag is
    # describing the API call, not the named order's fate (round 30's scoping).
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": True, "id": "ORD", "state": "rejected", "cumulative_quantity": 0}
    ) is False
    # ... and an unusable id with no success flag behaves exactly as the
    # no-identity branch always has, so nothing ordinary started halting.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"id": "   ", "state": "rejected", "cumulative_quantity": 0}
    ) is False
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"id": "   ", "state": "cancelled", "cumulative_quantity": 0}
    ) is False
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"id": "   ", "state": "queued"}
    ) is True


@pytest.mark.parametrize("bad_id", ["   ", -1, float("nan"), float("inf")])
def test_execute_plan_halts_on_success_flagged_responses_with_invalid_ids(bad_id):
    """Invalid ids must reach the no-identity success-flag safety check.

    The classifier-level assertions above pin the predicate, while this
    regression test pins the reported consequence at the public execution
    boundary: the invalid response stops the batch before the next ticker is
    submitted rather than being isolated as a proven broker refusal.
    """
    calls = []

    def place_fn(**kwargs):
        calls.append(kwargs["symbol"])
        return {
            "success": True,
            "id": bad_id,
            "state": "rejected",
            "cumulative_quantity": 0,
        }

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [
        dca_runner.Candidate("MSFT", 2.0),
        dca_runner.Candidate("NVDA", 2.0),
    ]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)

    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[],
        tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=candidates,
    )

    assert calls == ["MSFT"]
    assert result.halted_on == "MSFT"
    assert result.submitted == []
    assert [failure.ticker for failure in result.failures] == ["MSFT"]


def test_an_id_less_bridge_error_halts_like_a_raise():
    """`{"isError": true, "error": "timeout"}` is the response-shaped twin of a
    raised `place_fn`: the MCP transport failed, and it does not say whether it
    failed before the request reached the broker or while carrying the broker's
    acceptance back. `execute_plan` already halts unconditionally on a raise
    for exactly that reason, and this envelope left the batch placing on
    against the same unknown (Codex review, PR #786: "Halt on ID-less MCP
    bridge errors")."""
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"isError": True, "error": "timeout"}
    ) is True
    # Read at any level, like every other signal here.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"data": {"isError": True, "error": "connection reset"}}
    ) is True
    # An explicit zero does not rescue it — the transport, not the order, is
    # what could not be read.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"isError": True, "error": "timeout", "cumulative_quantity": 0}
    ) is True

    # STRICT `is True`, matching `_placement_failure_reason` and
    # `_is_error_response`. A truthy non-bool is not this canonical marker.
    for not_the_marker in (False, "true", 1, None):
        assert dca_runner._placement_response_may_hold_a_live_order(
            {"isError": not_the_marker, "success": False, "error": "not tradable"}
        ) is False, not_the_marker

    # And the reported incident's own shape is untouched: a broker refusing a
    # ticker reports `success: false` and carries no `isError` at all. This is
    # the distinction that makes an unconditional halt here safe.
    refusal = {"success": False, "error": "not tradable for fractional buys"}
    assert "isError" not in refusal
    assert dca_runner._placement_response_may_hold_a_live_order(refusal) is False
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "data": {"id": "ORD", "state": "rejected", "cumulative_quantity": 0}}
    ) is False


def test_orders_nested_inside_arrays_are_walked():
    """The walk iterated a list one level and `continue`d past any non-dict
    item, so a NESTED array was skipped silently — not even marked truncated.
    `{"success": false, "results": [[{"id": "ORD", "state": "queued"}]]}` was
    therefore read as naming no order at all and isolated (Codex review,
    PR #786: "Traverse nested response arrays before isolating")."""
    for depth, payload in (
        (1, {"success": False, "orders": [{"id": "ORD", "state": "queued"}]}),
        (2, {"success": False, "results": [[{"id": "ORD", "state": "queued"}]]}),
        (3, {"success": False, "r": [[[{"id": "ORD", "state": "queued"}]]]}),
    ):
        envelopes, truncated, _ = dca_runner._response_envelopes(payload, payload, payload)
        assert not truncated, depth
        assert any(e.get("id") == "ORD" for e in envelopes), depth
        assert dca_runner._placement_response_may_hold_a_live_order(payload) is True, depth

    # A nested order that really is provably dead still isolates — the fix is
    # about SEEING it, not about halting on everything found in an array.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "r": [[{"id": "ORD", "state": "rejected", "cumulative_quantity": 0}]]}
    ) is False
    # Mixed and non-dict items are skipped without disturbing the walk.
    payload = {"success": False, "r": [["noise", 1, None, {"id": "ORD", "state": "queued"}]]}
    assert dca_runner._placement_response_may_hold_a_live_order(payload) is True


def test_nested_array_traversal_cannot_hang_the_batch():
    """`place_fn` is injected Python and may return a malformed structure. The
    flatten loop added for nested arrays was bounded only by queue LENGTH,
    which bounds neither runaway shape: a chain of singleton arrays keeps
    `len(pending) == 1` while descending without limit, and a self-referential
    list re-appends itself and never terminates.

    This is the one defect in this classifier that costs more than a
    misclassification — an unbounded walk hangs the whole batch before the
    placement breaker, the window check, or anything else can run (Codex
    review, PR #786: "Bound nested response-array traversal")."""
    # A self-referential list terminates. Before the fix this looped until
    # killed; the assertion is simply that we get here at all.
    loop = []
    loop.append(loop)
    payload = {"success": False, "results": loop}
    envelopes, truncated, _ = dca_runner._response_envelopes(payload, payload, payload)
    assert isinstance(envelopes, list)

    # A cycle must not HIDE an order, which is the risk of skipping repeats:
    # everything reachable through the cycle was already enumerated once.
    cyclic: list[Any] = [{"id": "ORD", "state": "queued"}]
    cyclic.append(cyclic)
    payload = {"success": False, "results": cyclic}
    envelopes, _, _ = dca_runner._response_envelopes(payload, payload, payload)
    assert any(e.get("id") == "ORD" for e in envelopes)
    assert dca_runner._placement_response_may_hold_a_live_order(payload) is True
    # ... including an order buried deeper inside one.
    inner: list[Any] = [{"id": "DEEP", "state": "queued"}]
    outer: list[Any] = [inner]
    inner.append(outer)
    payload = {"success": False, "results": outer}
    assert dca_runner._placement_response_may_hold_a_live_order(payload) is True

    # A singleton chain deeper than the walk's own bound is TRUNCATED, not
    # silently walked forever — and truncation halts, per round 26.
    deep = current = []
    for _ in range(200):
        nxt = []
        current.append(nxt)
        current = nxt
    current.append({"id": "ORD", "state": "queued"})
    payload = {"success": False, "results": deep}
    _, truncated, _ = dca_runner._response_envelopes(payload, payload, payload)
    assert truncated is True
    assert dca_runner._placement_response_may_hold_a_live_order(payload) is True

    # Ordinary nesting is unaffected: still walked, still not truncated.
    for depth_payload in (
        {"success": False, "orders": [{"id": "ORD", "state": "queued"}]},
        {"success": False, "results": [[{"id": "ORD", "state": "queued"}]]},
        {"success": False, "r": [[[{"id": "ORD", "state": "queued"}]]]},
    ):
        envelopes, truncated, _ = dca_runner._response_envelopes(
            depth_payload, depth_payload, depth_payload
        )
        assert not truncated
        assert any(e.get("id") == "ORD" for e in envelopes)
    # ... and the reported incident's own shape still isolates.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "error": "not tradable for fractional buys"}
    ) is False


def test_a_terminal_order_with_no_fill_figure_at_all_halts():
    """Absence is not proof. An order id plus a terminal state but no fill
    field establishes nothing about what executed, and the adapters document
    exactly that shape for a partially filled order whose cancellation
    surfaced as the outer failure (Codex review, PR #786: "Treat omitted fill
    evidence as ambiguous")."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False, "data": {"id": "ORD", "state": "cancelled"}}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.halted_on == plan.to_place[0].ticker


def test_a_raised_place_fn_halts_because_nothing_can_be_established():
    """A raise gives no response to classify.

    A connector that timed out while returning the broker's acceptance is
    indistinguishable from one that failed before the request left, so an
    accepted order can exist that never reaches `submitted` or anything
    downstream reads off it. near_close_dca already fails its run for this
    event class once an order is published, and Gate 7's prompt says the same
    (Codex review, PR #786: "Halt when the placement call raises
    ambiguously")."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            raise RuntimeError("connector timeout")
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert len(calls) == 1, "the remaining orders must not be attempted"
    assert result.submitted == []
    assert result.halted_on == plan.to_place[0].ticker
    assert "cannot be established from here" in result.failure_reason
    # Still named per ticker so Gate 9 reports it.
    assert result.failures[0].reason == "place_fn raised RuntimeError"


def test_a_broker_refusal_still_isolates_after_the_raise_policy_change():
    """The guard against over-correcting. Halting on a RAISE must not bleed
    into halting on a REFUSAL: the reported incident was a broker returning a
    response that proves no order exists, and that still costs only its own
    ticker."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if len(calls) == 1:
            return {"success": False, "error": "not tradable for fractional buys"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT", "NVDA"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [o.ticker for o in plan.to_place]
    assert len(result.submitted) == 2
    assert result.halted_on is None


def test_consecutive_placement_failures_still_halt_the_batch():
    """Isolation is per ticker, not unconditional. An unbroken run of failures
    means the broker leg itself is unhealthy, and every further placement is a
    doomed live call."""
    calls = []

    def always_fails(**kw):
        calls.append(kw["symbol"])
        return {"success": False, "error": "insufficient funds"}

    broker = dca_runner.build_runner_broker(
        place_fn=always_fails, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    tickers = ["AAPL", "MSFT", "NVDA", "GS", "EVR"]
    plan, snapshot, tier, candidates = _three_ticker_plan(tickers)

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert dca_runner.MAX_CONSECUTIVE_PLACEMENT_FAILURES == 3
    assert len(calls) == 3, "the breaker must stop the batch, not attempt all five"
    assert result.submitted == []
    assert len(result.failures) == 3
    assert result.halted_on == calls[-1]
    assert "3 consecutive placement failures" in result.failure_reason
    assert "2 order(s) never attempted" in result.failure_reason


def test_a_success_between_failures_resets_the_consecutive_failure_streak():
    """Cumulative counting would re-create the old behavior on any batch with a
    few independently bad symbols; only an UNBROKEN run is systemic."""
    calls = []

    def alternating(**kw):
        calls.append(kw["symbol"])
        # Fail every other ticker, so the streak never reaches the breaker.
        if len(calls) % 2 == 1:
            return {"success": False, "error": "rejected"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=alternating, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    tickers = ["AAPL", "MSFT", "NVDA", "GS", "EVR", "IBM"]
    plan, snapshot, tier, candidates = _three_ticker_plan(tickers)

    result = _execute(plan, broker, snapshot, tier, candidates)

    assert calls == [o.ticker for o in plan.to_place]
    assert len(result.submitted) == 3
    assert len(result.failures) == 3
    assert result.halted_on is None


def test_an_isolated_failure_reason_is_redacted_like_a_halt_reason():
    """A per-ticker reason reaches the same alerts and durable evidence a halt
    reason does, so it must be scrubbed just as thoroughly."""
    secret_token = "tokenABCDEFGHIJKLMNOPQRSTUVWXYZ012345"

    def place_fn(**kw):
        if kw["symbol"] == "AAPL":
            return {"success": False, "error": f"denied for account {TEST_ACCOUNT_NUMBER} {secret_token}"}
        return {"id": f"ORD-{kw['symbol']}"}

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan, snapshot, tier, candidates = _three_ticker_plan(["AAPL", "MSFT"])

    result = _execute(plan, broker, snapshot, tier, candidates)

    reason = result.failures[0].reason
    assert TEST_ACCOUNT_NUMBER not in reason
    assert secret_token not in reason
    assert "denied for account" in reason
    assert result.submitted == ["MSFT"]


def test_max_consecutive_placement_failures_rejects_untrustworthy_config():
    for bad in (True, False, 0, -1, 2.5, "3", None):
        with pytest.raises(ValueError):
            dca_runner._validated_max_consecutive_placement_failures(bad)
    assert dca_runner._validated_max_consecutive_placement_failures(1) == 1


def test_execute_plan_recheck_the_clock_halts_the_remainder_when_the_window_closes():
    """The single pre-loop `now` check only proves the window was open when
    the batch started, not when each individual order is actually submitted.
    A slow place_fn call (or a large batch) can straddle the slot's
    acceptance window; now_fn, when supplied, is sampled fresh before every
    placement and halts the remainder the moment it reports the window has
    closed -- without raising, since AAPL's order already succeeded and is
    real, committed spend (Codex review: "Recheck the clock before each
    placement")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw["symbol"]) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    # The 0935 slot's window is 09:30-10:00 ET (see SLOTS); the first now_fn
    # call (before AAPL) is still inside it, the second (before MSFT) is not.
    clock_readings = iter([
        datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        datetime(2026, 7, 28, 10, 5, tzinfo=ET),
    ])
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)],
        now_fn=lambda: next(clock_readings),
    )
    assert calls == ["AAPL"]  # MSFT never attempted
    assert result.submitted == ["AAPL"]
    assert result.halted_on == "MSFT"
    assert "window has closed" in result.failure_reason


def test_execute_plan_now_fn_raising_halts_the_remainder_without_propagating():
    """now_fn is an injected callable (e.g. wrapping a system clock or an
    MCP-backed time source) that can raise like any other injected callable.
    If it succeeds for AAPL (which is accepted) and then raises for MSFT,
    execute_plan must return an ExecutionResult reporting AAPL as submitted,
    not propagate the exception and deny the caller a reconcilable result
    for a batch with real, already-committed spend (Codex review: "Preserve
    partial results when the fresh clock fails")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw["symbol"]) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)]
    plan = dca_runner.plan_orders(
        candidates=candidates,
        snapshot=snapshot,
        tier=tier,
    )
    clock_readings = iter([datetime(2026, 7, 28, 9, 43, tzinfo=ET)])

    def _now_fn():
        return next(clock_readings)  # raises StopIteration on the second call

    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=candidates,
        now_fn=_now_fn,
    )
    assert calls == ["AAPL"]  # MSFT never attempted
    assert result.submitted == ["AAPL"]
    assert result.halted_on == "MSFT"
    assert "now_fn raised" in result.failure_reason


def test_now_fn_exception_is_recorded_as_a_safe_classification_only():
    """now_fn's raised exception message is wrapper-defined and can embed
    the requested account number, an access token, or other secret-shaped
    context; failure_reason is explicitly sent to alerts and durable run
    evidence. Only a safe classification (the exception's type name) may be
    recorded -- never its message, the same fix already applied to
    place_fn's own raised exceptions (Codex review: "Redact exceptions
    raised by the placement clock")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )

    def _boom():
        raise ConnectionError(f"denied for account {secret_account_number}")

    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        now_fn=_boom,
    )
    assert result.halted_on == "AAPL"
    assert result.failures == []  # a clock failure halts; it is not a per-ticker rejection
    assert secret_account_number not in result.failure_reason
    assert "denied" not in result.failure_reason
    assert "ConnectionError" in result.failure_reason


def test_now_fn_returning_a_malformed_value_is_redacted_to_its_type():
    """now_fn's RETURN VALUE, not just an exception it raises, can be a
    connector response the wrapper mistakenly returned instead of a real
    datetime -- e.g. a dict carrying an account identifier or token.
    Interpolating it verbatim into failure_reason would leak that into
    alerts and durable run evidence; only the returned type may be
    reported, matching assess()'s own non-datetime clock path (Codex
    review: "Redact malformed values returned by the placement clock")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        snapshot=snapshot,
        tier=tier,
    )
    malformed_clock_reading = {"account_number": secret_account_number, "token": "shh"}

    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate("AAPL", 2.0)],
        now_fn=lambda: malformed_clock_reading,
    )
    assert result.halted_on == "AAPL"
    assert result.failures == []  # a clock failure halts; it is not a per-ticker rejection
    assert secret_account_number not in result.failure_reason
    assert "shh" not in result.failure_reason
    assert "dict" in result.failure_reason


def test_execute_plan_recheck_the_clock_catches_history_going_stale_mid_batch():
    """The pre-loop orders_fetched_at freshness check only proves the
    history was fresh when the batch started. now_fn's fresh reading is
    rechecked against orders_fetched_at before every placement too, so
    history that goes stale partway through a slow or large batch halts the
    remainder instead of letting a later order rely on order history that no
    longer represents an intervening manual or scheduled buy (Codex review:
    "Measure history freshness against the fresh placement clock")."""
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw["symbol"]) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    orders_fetched_at = datetime(2026, 7, 28, 9, 35, tzinfo=ET)
    # Both readings stay inside the 0935 slot's 09:30-10:00 ET window, so
    # only the orders-freshness check (not the window check) can be why the
    # second order halts. The second reading is 130s past orders_fetched_at,
    # just over MAX_ORDERS_FETCH_AGE_SECONDS (120s).
    clock_readings = iter([
        datetime(2026, 7, 28, 9, 36, tzinfo=ET),
        datetime(2026, 7, 28, 9, 37, 10, tzinfo=ET),
    ])
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 36, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=orders_fetched_at,
        candidates=candidates,
        now_fn=lambda: next(clock_readings),
    )
    assert calls == ["AAPL"]  # MSFT never attempted
    assert result.submitted == ["AAPL"]
    assert result.halted_on == "MSFT"
    assert "freshness window" in result.failure_reason


def test_execute_plan_submits_sequentially_never_in_parallel():
    order_seen = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: order_seen.append(kw["symbol"]) or {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    plan = dca_runner.plan_orders(
        candidates=[dca_runner.Candidate(t, 2.0) for t in ("AAPL", "MSFT", "NVDA")],
        snapshot=snapshot,
        tier=tier,
    )
    dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier, buying_power_usd=TEST_BUYING_POWER_USD,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        candidates=[dca_runner.Candidate(t, 2.0) for t in ("AAPL", "MSFT", "NVDA")],
    )
    assert order_seen == ["AAPL", "MSFT", "NVDA"]


def test_runner_exposes_no_sell_or_option_path():
    """Standing prohibition, asserted structurally rather than by convention."""
    surface = dir(dca_runner)
    assert not [n for n in surface if "sell" in n.lower()]
    assert not [n for n in surface if "option" in n.lower()]


# ===========================================================================
# H. Config drift guards
# ===========================================================================


def test_slot_windows_match_the_safeguards_roster():
    """docs/safeguards.md is authoritative for the seven-slot roster."""
    expected = {
        "0935": (9, 35), "1030": (10, 30), "1130": (11, 30),
        "1230": (12, 30), "1330": (13, 30), "1430": (14, 30), "1530": (15, 30),
    }
    for slot_id, (hh, mm) in expected.items():
        assert dca_runner.SLOTS[slot_id].nominal_et == (hh, mm)


def test_every_slot_window_tolerates_observed_scheduler_jitter():
    """Observed jitter ran to 594s. Each window must absorb at least 15 minutes."""
    for slot in dca_runner.SLOTS.values():
        assert slot.window_minutes >= 15


def test_per_order_cap_is_base_times_max_ladder_multiplier():
    """$2.00 base x 5.0 max multiplier = $10.00. Pins the two configs together."""
    ladder = json.loads((dca_runner.CONFIG_DIR / "sizing_ladder.json").read_bytes())
    max_mult = max(t["multiplier"] for t in ladder["tiers"])
    assert dca_runner.MAX_PER_ORDER_USD == pytest.approx(ladder["base_amount_usd"] * max_mult)
    assert dca_runner.MAX_PER_ORDER_USD == pytest.approx(10.0)


@pytest.mark.parametrize(
    "base_amount_usd,multiplier",
    [
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (-2.0, 1.0),
        (0.0, 1.0),
        (True, 1.0),
        (2.0, float("nan")),
        (2.0, True),
        (2.0, -1.0),
    ],
)
def test_validated_sizing_ladder_rejects_non_finite_or_boolean_numbers(
    tmp_path, monkeypatch, base_amount_usd, multiplier
):
    """Python's JSON/float coercion accepts a non-finite base or multiplier
    (a bare NaN, or a boolean since bool is an int subclass); left
    unvalidated, MAX_PER_ORDER_USD would become NaN and every later
    amount_usd > MAX_PER_ORDER_USD check would be False, letting a single
    order clear the documented $10 per-order circuit breaker (Codex review:
    "Reject non-finite per-order cap configuration")."""
    (tmp_path / "sizing_ladder.json").write_text(json.dumps({
        "base_amount_usd": base_amount_usd,
        "base_amount_below_close_usd": 1.0,
        "tiers": [{"min_drop_pct": 0.1, "max_drop_pct": None, "multiplier": multiplier}],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_sizing_ladder()


def test_validated_sizing_ladder_rejects_a_max_at_or_below_its_own_min(tmp_path, monkeypatch):
    """A tier whose max_drop_pct is at or below its own min_drop_pct is
    schema-valid (both are independently exclusiveMinimum: 0) but
    internally nonsensical -- no drop could ever fall in an empty or
    inverted range (Codex review: "Reject inconsistent sizing-ladder
    ranges at import")."""
    (tmp_path / "sizing_ladder.json").write_text(json.dumps({
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "tiers": [{"min_drop_pct": 1.0, "max_drop_pct": 1.0, "multiplier": 1.0}],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_sizing_ladder()


def test_validated_sizing_ladder_rejects_a_gap_between_tiers(tmp_path, monkeypatch):
    """The core signal generator selects a multiplier purely from ordered
    minimum thresholds and never consults the maxima; a gap between one
    tier's max_drop_pct and the next tier's min_drop_pct (e.g. a 1x range
    of [0.1, 0.5) followed by a 1.5x tier starting at 1.0) lets the
    generator produce a genuine 1x signal for a drop like 0.75% that this
    runner's own drop_ranges would then refuse as inconsistent, since 0.75
    falls in neither tier's stored range (Codex review: "Reject
    inconsistent sizing-ladder ranges at import")."""
    (tmp_path / "sizing_ladder.json").write_text(json.dumps({
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "tiers": [
            {"min_drop_pct": 0.1, "max_drop_pct": 0.5, "multiplier": 1.0},
            {"min_drop_pct": 1.0, "max_drop_pct": None, "multiplier": 1.5},
        ],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_sizing_ladder()


def test_validated_sizing_ladder_accepts_a_contiguous_ladder(tmp_path, monkeypatch):
    """A genuinely contiguous ladder (each tier's max exactly matching the
    next tier's min) must still load successfully; the new adjacency check
    must not reject a well-formed configuration."""
    (tmp_path / "sizing_ladder.json").write_text(json.dumps({
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "tiers": [
            {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 1.0},
            {"min_drop_pct": 1.0, "max_drop_pct": None, "multiplier": 1.5},
        ],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    ladder = dca_runner._validated_sizing_ladder()
    assert ladder["drop_ranges"][1.0] == (0.1, 1.0)
    assert ladder["drop_ranges"][1.5] == (1.0, None)


def test_validated_sizing_ladder_rejects_a_finite_max_on_the_final_tier(tmp_path, monkeypatch):
    """The adjacency check only validates tiers that have a successor, so a
    finite max_drop_pct on the sorted FINAL (highest-range) tier passes it
    silently. near_close_dca.graded_buy_multiplier() ignores maxima
    entirely and keeps selecting the last tier's multiplier for any drop
    above its min_drop_pct with no upper bound; a finite final max (e.g. a
    5x tier ending at 9%) would let the generator produce a genuine 10%
    signal that this runner would then refuse as inconsistent (Codex
    review: "Require an open-ended final sizing tier")."""
    (tmp_path / "sizing_ladder.json").write_text(json.dumps({
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "tiers": [
            {"min_drop_pct": 0.1, "max_drop_pct": 8.0, "multiplier": 1.0},
            {"min_drop_pct": 8.0, "max_drop_pct": 9.0, "multiplier": 5.0},
        ],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_sizing_ladder()


def test_validated_sizing_ladder_rejects_a_duplicate_multiplier(tmp_path, monkeypatch):
    """drop_ranges is keyed by multiplier: two adjacent tiers reusing the
    same multiplier (e.g. [0.1, 1.0) and [1.0, 2.0), both 1.0x) would
    silently overwrite the first tier's range, so
    _candidate_drop_mismatches_claimed_multiplier() would only ever see the
    LAST tier's range for that multiplier -- refusing a genuine 0.5%/1.0x
    candidate the generator produced from the now-invisible earlier tier
    (Codex review: "Reject duplicate ladder multipliers before indexing
    ranges")."""
    (tmp_path / "sizing_ladder.json").write_text(json.dumps({
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "tiers": [
            {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 1.0},
            {"min_drop_pct": 1.0, "max_drop_pct": None, "multiplier": 1.0},
        ],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_sizing_ladder()


def test_validated_sizing_ladder_rejects_a_sub_cent_producing_multiplier(tmp_path, monkeypatch):
    """A schema-valid finite positive interior multiplier like 2.505 (with
    the configured 5x maximum and contiguous drop ranges otherwise intact)
    produces $2.505 for the reduced $1 base -- a genuine candidate the
    signal generator would emit but plan_orders() always refuses as
    REFUSED_NON_CENT_AMOUNT. Every configured multiplier must produce
    cent-precise allocations for BOTH bases so the generator and runner
    cannot disagree (Codex review: "Reject multipliers that produce
    sub-cent allocations")."""
    (tmp_path / "sizing_ladder.json").write_text(json.dumps({
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "tiers": [
            {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 2.505},
            {"min_drop_pct": 1.0, "max_drop_pct": None, "multiplier": 5.0},
        ],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_sizing_ladder()


@pytest.mark.parametrize("bad_value", [True, False, float("nan"), float("inf"), -1.0, 0.0])
def test_positive_finite_config_number_rejects_boolean_high_multiplier_threshold(bad_value):
    """float(True) is 1.0, so an unvalidated boolean high_multiplier_threshold
    would make every valid ladder candidate count as high-multiplier and
    select a richer tier than the candidates' real notionals justify (Codex
    review: "Reject boolean high-multiplier thresholds")."""
    with pytest.raises(ValueError):
        dca_runner._positive_finite_config_number(
            bad_value, "high_multiplier_threshold", "budgets.json"
        )


def test_validated_max_per_order_hard_limit_rejects_a_drifted_sizing_ladder(tmp_path, monkeypatch):
    """If $DCA_CONFIG_DIR supplies a sizing_ladder.json whose base or max
    multiplier drifted from budgets.json's own circuit_breakers.max_per_order_usd
    (e.g. base_amount_usd raised to $20 with the circuit breaker left at $10),
    the derived MAX_PER_ORDER_USD would otherwise silently rise past the
    authoritative absolute limit budgets.json still declares (Codex review:
    "Cross-check the derived per-order cap against the hard limit")."""
    (tmp_path / "budgets.json").write_text(json.dumps({
        "circuit_breakers": {"max_per_order_usd": 10.0},
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_max_per_order_hard_limit(100.0)


def test_validated_max_per_order_hard_limit_accepts_a_matching_sizing_ladder(tmp_path, monkeypatch):
    (tmp_path / "budgets.json").write_text(json.dumps({
        "circuit_breakers": {"max_per_order_usd": 10.0},
        "base_amount_usd": dca_runner._SIZING_LADDER["base_amount_usd"],
        "base_amount_below_close_usd": dca_runner._SIZING_LADDER["base_amount_below_close_usd"],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    assert dca_runner._validated_max_per_order_hard_limit(10.0) == pytest.approx(10.0)


def test_validated_max_per_order_hard_limit_rejects_a_drifted_base_amount(tmp_path, monkeypatch):
    """A sizing ladder whose base AND max multiplier both drifted (e.g. a $10
    base with a 1x maximum) reproduces the same $10 product as the real $2
    base x 5x maximum, so the product-only check alone would pass even though
    budgets.json's own base_amount_usd still declares $2 -- letting
    plan_orders() generate $10 "1x" candidates instead of the documented $2
    allocation (Codex review: "Cross-check both configured base amounts").
    Simulated here as budgets.json itself drifting to $20 while the real
    (imported) sizing ladder's base stays at $2 -- the same mismatch, from
    the other side."""
    (tmp_path / "budgets.json").write_text(json.dumps({
        "circuit_breakers": {"max_per_order_usd": 10.0},
        "base_amount_usd": 20.0,
        "base_amount_below_close_usd": dca_runner._SIZING_LADDER["base_amount_below_close_usd"],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_max_per_order_hard_limit(10.0)


def test_validated_max_per_order_hard_limit_rejects_a_drifted_below_close_base(tmp_path, monkeypatch):
    (tmp_path / "budgets.json").write_text(json.dumps({
        "circuit_breakers": {"max_per_order_usd": 10.0},
        "base_amount_usd": dca_runner._SIZING_LADDER["base_amount_usd"],
        "base_amount_below_close_usd": 5.0,
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_max_per_order_hard_limit(10.0)


def test_validated_max_per_order_hard_limit_rejects_a_sub_cent_drifted_base(tmp_path, monkeypatch):
    """round(derived * 100) == round(authoritative * 100) alone would treat a
    sub-cent-drifted base (e.g. sizing_ladder.json's $2.004 vs. budgets.json's
    authoritative $2.00) as identical, since both round to 200 cents --
    letting import succeed while every order this base multiplies is priced
    a fraction of a cent off from what budgets.json actually authorizes, e.g.
    a real $5.00 order (2.5x) becoming a cent-precise $5.01 (Codex review:
    "Reject sub-cent drift in authoritative base amounts")."""
    (tmp_path / "budgets.json").write_text(json.dumps({
        "circuit_breakers": {"max_per_order_usd": 10.0},
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": dca_runner._SIZING_LADDER["base_amount_below_close_usd"],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    drifted_ladder = dict(dca_runner._SIZING_LADDER)
    drifted_ladder["base_amount_usd"] = 2.004
    monkeypatch.setattr(dca_runner, "_SIZING_LADDER", drifted_ladder)
    with pytest.raises(ValueError):
        dca_runner._validated_max_per_order_hard_limit(10.0)


def test_validated_max_per_order_hard_limit_rejects_a_sub_cent_drifted_authoritative_base(
    tmp_path, monkeypatch
):
    """The prior fix only required sizing_ladder.json's derived value to be
    cent-precise; if an override instead keeps sizing_ladder.json's base at
    a cent-precise $2.00 while budgets.json's own authoritative value
    drifts to a sub-cent $2.004, both still round to 200 cents and the
    equality check alone would pass -- but the runner actually sizes every
    order from the cent-precise $2.00, silently ignoring the 0.4-cent drift
    base_amount_policy.source_of_truth declares authoritative (Codex
    review: "Validate cent precision on the authoritative base")."""
    (tmp_path / "budgets.json").write_text(json.dumps({
        "circuit_breakers": {"max_per_order_usd": 10.0},
        "base_amount_usd": 2.004,
        "base_amount_below_close_usd": dca_runner._SIZING_LADDER["base_amount_below_close_usd"],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_max_per_order_hard_limit(10.0)


def test_max_per_order_usd_matches_budgets_json_circuit_breaker():
    """Pins the real, shipped configuration: MAX_PER_ORDER_USD (derived from
    sizing_ladder.json) and budgets.json's own circuit_breakers.max_per_order_usd
    must already agree, not just tolerate each other."""
    cfg = json.loads((dca_runner.CONFIG_DIR / "budgets.json").read_bytes())
    assert dca_runner.MAX_PER_ORDER_USD == pytest.approx(
        cfg["circuit_breakers"]["max_per_order_usd"]
    )


# --- _load_max_orders_per_day: fail-closed config validation ------------------
#
# MAX_ORDERS_PER_DAY = _load_max_orders_per_day() runs once at import time, so
# these branches have no way to be reached through execute_plan()/assess() in
# an already-imported test process -- they must be exercised by calling the
# loader directly against a swapped-in CONFIG_DIR, the same pattern used above
# for _validated_max_per_order_hard_limit.


def test_load_max_orders_per_day_rejects_a_missing_key(tmp_path, monkeypatch):
    (tmp_path / "budgets.json").write_text(json.dumps({"circuit_breakers": {}}))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="is missing"):
        dca_runner._load_max_orders_per_day()


@pytest.mark.parametrize("bad_value", ["8", 8.0, True, False, None])
def test_load_max_orders_per_day_rejects_a_non_integer(tmp_path, monkeypatch, bad_value):
    """A bool passes isinstance(x, int) (bool is an int subclass) and must be
    rejected explicitly, same as every other config-sourced numeric guard in
    this module."""
    (tmp_path / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_orders_per_day": bad_value}})
    )
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="must be an"):
        dca_runner._load_max_orders_per_day()


@pytest.mark.parametrize("bad_value", [0, -1])
def test_load_max_orders_per_day_rejects_a_non_positive_value(tmp_path, monkeypatch, bad_value):
    (tmp_path / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_orders_per_day": bad_value}})
    )
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match=">= 1"):
        dca_runner._load_max_orders_per_day()


def test_load_max_orders_per_day_accepts_a_valid_positive_integer(tmp_path, monkeypatch):
    (tmp_path / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_orders_per_day": 8}})
    )
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    assert dca_runner._load_max_orders_per_day() == 8


def test_budget_tiers_match_config_budgets_json():
    """Tier values are hardcoded in near_close_dca and documented in config."""
    cfg = json.loads((dca_runner.CONFIG_DIR / "budgets.json").read_bytes())["budget_tiers"]
    runner_tiers = {t.name: (t.daily_cap_usd, t.weekly_cap_usd) for t in dca_runner.BUDGET_TIERS}
    for entry in cfg:
        assert runner_tiers[entry["name"]] == (entry["daily_cap_usd"], entry["weekly_cap_usd"])


def _real_budget_tiers_config():
    return json.loads((dca_runner.CONFIG_DIR / "budgets.json").read_bytes())["budget_tiers"]


def test_validate_budget_tiers_against_config_rejects_a_deployment_time_override(
    tmp_path, monkeypatch
):
    """test_budget_tiers_match_config_budgets_json only protects the
    CHECKED-IN config -- $DCA_CONFIG_DIR can point CONFIG_DIR at an entirely
    different, deployment-time config directory. A budgets.json there with
    Normal's daily cap lowered to $1 must still fail module-style validation,
    not silently let assess()/plan_orders() keep enforcing the stale,
    hard-coded $180 near_close_dca.BUDGET_TIERS entry (Codex review: "Reject
    overridden budget-tier drift")."""
    tiers = _real_budget_tiers_config()
    for entry in tiers:
        if entry["name"] == "normal":
            entry["daily_cap_usd"] = 1.0
    (tmp_path / "budgets.json").write_text(json.dumps({"budget_tiers": tiers}))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validate_budget_tiers_against_config()


def test_validate_budget_tiers_against_config_accepts_the_real_config():
    """The real, shipped budgets.json must already pass this check."""
    dca_runner._validate_budget_tiers_against_config()


def test_validate_budget_tiers_against_config_rejects_a_drifted_spend_threshold(
    tmp_path, monkeypatch
):
    """Caps and the rollover flag matching does not prove the SELECTION
    boundaries match too: an override that keeps Normal's $180 daily cap but
    raises its escalate_above_planned_spend_usd to $200 would let a $130
    plan silently select Elevated's caps in select_budget_tier() (still
    hard-coded at $129) while this module's own tier-definition check above
    never notices (Codex review: "Validate overridden tier-selection
    thresholds")."""
    tiers = _real_budget_tiers_config()
    for entry in tiers:
        if entry["name"] == "normal":
            entry["escalate_above_planned_spend_usd"] = 200.0
    (tmp_path / "budgets.json").write_text(json.dumps({"budget_tiers": tiers}))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validate_budget_tiers_against_config()


def test_validate_budget_tiers_against_config_rejects_a_drifted_signal_threshold(
    tmp_path, monkeypatch
):
    tiers = _real_budget_tiers_config()
    for entry in tiers:
        if entry["name"] == "elevated":
            entry["escalate_above_high_multiplier_signals"] = 12
    (tmp_path / "budgets.json").write_text(json.dumps({"budget_tiers": tiers}))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validate_budget_tiers_against_config()


def test_validate_budget_tiers_against_config_rejects_a_missing_escalation_threshold(
    tmp_path, monkeypatch
):
    """A budgets.json missing the new escalation fields entirely (e.g. an
    older deployment's config, not just a deliberately drifted one) must
    fail closed too, not silently skip the check."""
    tiers = _real_budget_tiers_config()
    for entry in tiers:
        entry.pop("escalate_above_planned_spend_usd", None)
    (tmp_path / "budgets.json").write_text(json.dumps({"budget_tiers": tiers}))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validate_budget_tiers_against_config()


def test_read_config_rejects_duplicate_keys(tmp_path, monkeypatch):
    """The standard JSON parser silently keeps the last occurrence of a
    duplicate configuration key. A market_calendar.json corrupted to carry
    an authoritative holidays_closed list followed by another, empty
    holidays_closed key must not parse successfully with every configured
    holiday silently treated as open (Codex review: "Reject duplicate keys
    in runner configuration")."""
    (tmp_path / "market_calendar.json").write_text(
        '{"years_covered": [2026], "holidays_closed": ["2026-12-25"], '
        '"holidays_closed": [], "early_close_1300": []}'
    )
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._read_config("market_calendar.json")


@pytest.mark.parametrize("bad_holidays", [{}, "", {"2026-07-03": True}, "2026-07-03"])
def test_market_day_status_rejects_a_wrong_shaped_holidays_closed(tmp_path, monkeypatch, bad_holidays):
    """A wrong-shaped but valid-JSON holidays_closed fails OPEN, not closed:
    ``"2026-07-03" in {}`` is False (dict membership checks keys) and
    ``"2026-07-03" in ""`` is also False (substring test), so a configured
    closure would otherwise be silently treated as an open trading day
    instead of raising the documented calendar-unavailable ABORT (Codex
    review: "Validate calendar collection types before membership
    checks")."""
    (tmp_path / "market_calendar.json").write_text(json.dumps({
        "years_covered": [2026],
        "holidays_closed": bad_holidays,
        "early_close_1300": [],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner.market_day_status(date(2026, 7, 3))


@pytest.mark.parametrize(
    "bad_active",
    [
        "AAPL",  # frozenset() of a bare string silently iterates characters
        [],
        ["AAPL", ""],
        ["AAPL", 123],
        {"AAPL": True},
        ["aapl"],  # not already upper-case-normalized
        [" AAPL "],  # not already stripped
        ["AAPL", "msft"],
    ],
)
def test_validated_active_tickers_rejects_a_malformed_active_field(tmp_path, monkeypatch, bad_active):
    """A malformed tickers.json 'active' field -- especially a bare string,
    which frozenset() would otherwise silently iterate character-by-character
    into a bogus per-letter whitelist -- must fail closed at import/load time
    rather than produce a silently wrong ACTIVE_TICKERS collection (Codex
    review: "Validate the active-ticker collection before freezing it"). A
    not-yet-canonical entry (lowercase, or padded with whitespace) must fail
    closed too, rather than freeze verbatim into ACTIVE_TICKERS: a real
    candidate's ticker is always normalized (stripped, upper-cased) before
    the whitelist check runs, so an un-normalized entry here would never
    match its own candidate and silently disable that ticker instead (Codex
    review: "Normalize active tickers before freezing them")."""
    (tmp_path / "tickers.json").write_text(json.dumps({"active": bad_active}))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner._validated_active_tickers()


def test_validated_active_tickers_accepts_a_wellformed_active_field(tmp_path, monkeypatch):
    (tmp_path / "tickers.json").write_text(json.dumps({"active": ["AAPL", "MSFT"]}))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    assert dca_runner._validated_active_tickers() == frozenset({"AAPL", "MSFT"})


def test_market_day_status_rejects_a_wrong_shaped_early_close_1300(tmp_path, monkeypatch):
    """Same failure mode as holidays_closed, for the early-close list."""
    (tmp_path / "market_calendar.json").write_text(json.dumps({
        "years_covered": [2026],
        "holidays_closed": [],
        "early_close_1300": {},
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner.market_day_status(date(2026, 7, 3))


def test_market_day_status_recognizes_a_basic_iso_holiday_spelling(tmp_path, monkeypatch):
    """A closure written in the basic, dash-free ISO spelling is a perfectly
    good date but never textually equals day.isoformat()'s extended
    YYYY-MM-DD form; comparing parsed date objects instead of raw strings
    must still recognize it as a closure (Codex review: "Normalize calendar
    entries before membership checks")."""
    (tmp_path / "market_calendar.json").write_text(json.dumps({
        "years_covered": [2026],
        "holidays_closed": [date(2026, 7, 3).isoformat().replace("-", "")],
        "early_close_1300": [],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    assert dca_runner.market_day_status(date(2026, 7, 3)).is_closed


def test_market_day_status_rejects_an_unparseable_calendar_entry(tmp_path, monkeypatch):
    (tmp_path / "market_calendar.json").write_text(json.dumps({
        "years_covered": [2026],
        "holidays_closed": ["not-a-date"],
        "early_close_1300": [],
    }))
    monkeypatch.setattr(dca_runner, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        dca_runner.market_day_status(date(2026, 7, 3))


# ===========================================================================
# I. assess() -- the single entrypoint the scheduled prompts call
# ===========================================================================
#
# The whole point of this surface is that the seven task prompts stop carrying
# gate logic as prose. Each prompt fetches broker orders, hands them here, and
# does what the verdict says. Three verdicts only:
#   END_SILENTLY  a normal calendar fact (weekend, holiday, early close)
#   ABORT         something is wrong; alert
#   PROCEED       gates pass; a plan is attached


def _clean_kwargs(**over):
    base = dict(
        slot_id="0935",
        orders=[],
        candidates=[],
        now=datetime(2026, 7, 28, 9, 43, 14, tzinfo=ET),
        # assess() ABORTs on an omitted buying_power_usd (Codex review:
        # "Abort assessment when buying power is unavailable"); tests that
        # specifically exercise that ABORT pass buying_power_usd=None
        # explicitly to override this default.
        buying_power_usd=TEST_BUYING_POWER_USD,
    )
    base.update(over)
    return base


def test_assess_weekend_ends_silently():
    r = dca_runner.assess(**_clean_kwargs(now=datetime(2026, 8, 1, 9, 43, tzinfo=ET)))
    assert r["verdict"] == "END_SILENTLY"


def test_assess_nyse_holiday_ends_silently():
    r = dca_runner.assess(**_clean_kwargs(now=datetime(2026, 9, 7, 9, 43, tzinfo=ET)))
    assert r["verdict"] == "END_SILENTLY"


def test_assess_off_schedule_firing_aborts_rather_than_running_late():
    r = dca_runner.assess(**_clean_kwargs(now=datetime(2026, 7, 28, 11, 15, tzinfo=ET)))
    assert r["verdict"] == "ABORT"
    assert "window" in r["reason"].lower()


def test_assess_past_trading_window_end_date_aborts():
    r = dca_runner.assess(**_clean_kwargs(now=datetime(2026, 11, 2, 9, 43, tzinfo=ET)))
    assert r["verdict"] == "ABORT"


def test_assess_trips_interlock_when_window_already_has_an_order():
    orders = [_order("AAPL", 2.0, _utc(2026, 7, 28, 13, 45), placed_agent="user")]
    r = dca_runner.assess(**_clean_kwargs(orders=orders))
    assert r["verdict"] == "ABORT"
    assert "executor" in r["reason"].lower()


def test_assess_clean_run_proceeds_and_attaches_a_plan():
    """MSFT's larger $6.00 candidate (higher estimated multiplier) sorts
    before AAPL's $4.00 under the documented allocation priority."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 4.0), ("MSFT", 6.0)]))
    assert r["verdict"] == "PROCEED"
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["MSFT", "AAPL"]
    assert r["plan"]["total_notional_usd"] == pytest.approx(10.0)


def test_assess_zero_candidates_proceeds_with_an_empty_plan():
    """A quiet tape is a legitimate outcome, not a failure."""
    r = dca_runner.assess(**_clean_kwargs())
    assert r["verdict"] == "PROCEED"
    assert r["plan"]["to_place"] == []


def test_assess_proceeds_for_every_slot_at_its_own_nominal_time(all_slots):
    """Every assess()-level test above defaults to slot_id="0935" via
    _clean_kwargs, so the full Gates-0/1/3/4/6 pipeline had never actually
    run for the other six slots. Firing each slot at its own nominal_et on a
    day inside the approved trading window (2026-07-28, used throughout this
    file) must reach the same clean PROCEED, proving no gate silently
    special-cases the market-open slot."""
    now = datetime(2026, 7, 28, *all_slots.nominal_et, tzinfo=ET)
    r = dca_runner.assess(**_clean_kwargs(slot_id=all_slots.slot_id, now=now))
    assert r["verdict"] == "PROCEED"
    assert r["slot"] == all_slots.slot_id
    assert r["slot_label"] == all_slots.label


def test_assess_reports_budget_actually_consumed_today():
    orders = [_order("X", 297.0, _utc(2026, 7, 28, 16, 13), placed_agent="user")]
    r = dca_runner.assess(**_clean_kwargs(orders=orders))
    assert r["budget"]["day_notional_usd"] == pytest.approx(297.0)


def test_assess_materializes_a_single_pass_orders_iterable():
    """A generator must not be exhausted by Gate 4's first pass, leaving
    Gate 6's budget_snapshot() and same-day dedup with no history at all
    (Codex review: "Materialize order history before running multiple
    gates")."""
    order = _order("X", 297.0, _utc(2026, 7, 28, 16, 13), placed_agent="user")
    r = dca_runner.assess(**_clean_kwargs(orders=(o for o in [order])))
    assert r["budget"]["day_notional_usd"] == pytest.approx(297.0)


def test_assess_redacts_a_broker_history_exception_from_the_abort_reason():
    """A lazy broker-history iterable (e.g. an API client wrapping a real
    fetch) that raises while being consumed can embed an account number, an
    access token, or other connector context in its own exception message;
    this ABORT reason is exactly what the executor prompts persist as run
    evidence and may send through alert channels. Only a safe
    classification, the exception's type name, may be recorded -- never its
    message (Codex review: "Redact broker-history exceptions before
    returning evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value

    def _boom():
        raise ConnectionError(f"denied for account {secret_account_number}")
        yield  # pragma: no cover - makes this a generator function

    r = dca_runner.assess(**_clean_kwargs(orders=_boom()))
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "denied" not in r["reason"]
    assert "ConnectionError" in r["reason"]


def test_assess_redacts_a_malformed_broker_row_from_the_abort_reason():
    """A malformed raw row from a broker wrapper (e.g. a dict, not a
    BrokerOrder instance) can itself carry an account number, a token, or
    request headers; _reject_non_broker_order_rows() previously embedded
    repr(row) verbatim in its ValueError, and assess() copies that message
    into the ABORT reason it writes as run evidence and may send through
    alert channels. Only the row's type is reported now (Codex review:
    "Redact malformed broker rows from abort evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    malformed_row = {"account_number": secret_account_number, "note": "not a BrokerOrder"}

    r = dca_runner.assess(**_clean_kwargs(orders=[malformed_row]))
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "dict" in r["reason"]


def test_assess_redacts_a_candidate_source_exception_from_the_abort_reason():
    """A lazy candidates iterable (e.g. a generator wrapping a live
    market-data/signal-generation call) that raises while being consumed
    can embed a market-data token, account context, or other connector
    detail in its own exception message; this ABORT reason is exactly what
    the executor prompts persist as run evidence and may send through
    alert channels. Only a safe classification, the exception's type name,
    may be recorded -- never its message, matching the same fix already
    applied to the broker-history and placement exception paths (Codex
    review: "Redact candidate-source exceptions from assessment
    evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value

    def _boom():
        raise ConnectionError(f"denied for account {secret_account_number}")
        yield  # pragma: no cover - makes this a generator function

    r = dca_runner.assess(**_clean_kwargs(candidates=_boom()))
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "denied" not in r["reason"]
    assert "ConnectionError" in r["reason"]


def test_assess_output_is_json_serialisable():
    """The prompts read this as JSON; a stray dataclass would break them."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 4.0)]))
    assert json.loads(json.dumps(r))["verdict"] == "PROCEED"


def test_assess_reports_placement_enabled_only_on_proceed():
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 4.0)]))
    assert r["verdict"] == "PROCEED"
    assert r["placement_enabled"] is True


def test_assess_reports_placement_disabled_on_abort():
    r = dca_runner.assess(**_clean_kwargs(now=datetime(2026, 7, 28, 11, 15, tzinfo=ET)))
    assert r["verdict"] == "ABORT"
    assert r["placement_enabled"] is False


def test_assess_reports_placement_disabled_on_end_silently():
    r = dca_runner.assess(**_clean_kwargs(now=datetime(2026, 8, 1, 9, 43, tzinfo=ET)))
    assert r["verdict"] == "END_SILENTLY"
    assert r["placement_enabled"] is False


def test_assess_requires_a_known_slot_id():
    with pytest.raises(KeyError):
        dca_runner.assess(**_clean_kwargs(slot_id="0000"))


def test_assess_converts_a_naive_broker_timestamp_into_an_abort_verdict():
    """assess() must never let a ValueError from a naive created_at escape:
    that would terminate the scheduled caller before Gate 9 writes evidence
    or sends an alert (Codex review: "Convert malformed broker timestamps
    into an abort verdict")."""
    naive_order = dca_runner.BrokerOrder(
        symbol="AAPL", side="buy", dollar_amount=2.0,
        created_at=datetime(2026, 7, 28, 13, 45),  # no tzinfo
    )
    r = dca_runner.assess(**_clean_kwargs(orders=[naive_order]))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_row", [None, {"id": "ORD", "state": "queued"}])
def test_assess_converts_a_malformed_history_row_into_an_abort_verdict(bad_row):
    """A materialized broker-history row that is not a BrokerOrder at all --
    None, or a raw dict -- makes gate_sole_executor()'s `o.created_at` access
    raise a bare AttributeError before _require_aware() ever runs. assess()
    must convert that into the documented ABORT verdict too, not just a
    ValueError from a naive timestamp on an otherwise well-shaped row, or it
    crashes before Gate 9 can write evidence or alert (Codex review: "Convert
    malformed history rows into ABORT")."""
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_row]))
    assert r["verdict"] == "ABORT"


def test_assess_converts_a_partial_history_row_into_an_abort_verdict():
    """A duck-typed row exposing only .created_at (a valid, aware datetime)
    clears gate_sole_executor()'s own row-shape check, but is not a
    BrokerOrder and would otherwise crash downstream the first time
    budget_snapshot() or .counts_toward_budget/.is_buy_attempt reads .side,
    .state, or .executed_dollar_amount -- none of which Gate 4 ever touches
    (Codex review: "Validate complete history rows before running later
    gates")."""

    class _PartialRow:
        def __init__(self, created_at):
            self.created_at = created_at

    bad_row = _PartialRow(datetime(2026, 7, 28, 13, 45, tzinfo=timezone.utc))
    r = dca_runner.assess(**_clean_kwargs(orders=[bad_row]))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize(
    "bad_market_day_status",
    [
        FileNotFoundError("market_calendar.json"),
        json.JSONDecodeError("Expecting value", "", 0),
        KeyError("years_covered"),
        TypeError("argument of type 'int' is not iterable"),
    ],
)
def test_assess_converts_an_unreadable_calendar_into_an_abort_verdict(
    monkeypatch, bad_market_day_status
):
    """market_day_status() reads market_calendar.json off disk and indexes
    into it directly: a missing file, malformed JSON, or a missing/wrong-
    shaped key all mean the calendar cannot be trusted, the same as the
    already-handled CalendarCoverageError. Without this, a deployment/config
    failure would raise past assess() before Gate 9 could write evidence or
    alert (Codex review: "Convert unreadable calendar files into an abort
    verdict")."""

    def _raise(day):
        raise bad_market_day_status

    monkeypatch.setattr(dca_runner, "market_day_status", _raise)
    r = dca_runner.assess(**_clean_kwargs())
    assert r["verdict"] == "ABORT"


def test_assess_counts_high_multiplier_by_actual_multiplier_not_notional():
    """Reduced ($1) base candidates at 3x-5x must count as high-multiplier.

    Six $3.00 candidates on the reduced base are all valid 3x signals, but
    $3.00 is below 3x the *global* $2.00 base, so estimating the multiplier
    from notional alone would miss every one of them (Codex review: "Count
    high-multiplier signals by multiplier, not notional").
    """
    tickers = ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "IBM"]  # all in config/tickers.json active
    candidates = [(t, 3.0, 3.0) for t in tickers]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "elevated"


def test_assess_without_multiplier_falls_back_to_notional_estimate():
    """Backward compatible: a 2-tuple candidate still works, just estimated."""
    tickers = ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "IBM"]
    candidates = [(t, 3.0) for t in tickers]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "normal"


def test_assess_rejects_a_cent_off_multiplier_notional_pairing():
    """A full-cent tolerance treats $5.99 with multiplier=3 as consistent
    with the configured $2 x 3 = $6.00 size, when the two are a full cent
    apart and only one can be the real, config-derived notional. Six such
    malformed candidates must not count as six high-multiplier signals
    (Codex review: "Require exact configured candidate sizes")."""
    tickers = ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "IBM"]
    candidates = [(t, 5.99, 3.0) for t in tickers]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "normal"


def test_assess_rejects_a_multiplier_the_ladder_never_configures():
    """1.0-5.0 range membership alone still admits a value the ladder never
    produces (e.g. 2.9, between the configured 2.5 and 3.0 rungs); only an
    exact match against the configured ladder multipliers may be trusted
    (Codex review: "Require exact configured candidate sizes")."""
    tickers = ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "IBM"]
    candidates = [(t, 5.8, 2.9) for t in tickers]  # $5.8 == 2.9 * $2 exactly, but 2.9 isn't a rung
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "normal"


def test_assess_rejects_a_multiplier_inconsistent_with_its_notional():
    """A legitimate candidate's amount is always multiplier * base for one of
    the two configured bases ($2.00 global or $1.00 reduced). A $2.00
    notional falsely claiming a 5x multiplier matches neither base's 5x
    notional ($10 or $5), so it must fall back to the notional-based estimate
    (1x here) rather than inflating high-multiplier tier counting on the
    claimed value alone (Codex review: "Validate candidate multipliers
    against their notionals")."""
    tickers = [
        "DFTX", "TRIN", "AAAU", "SPCX", "EVR", "LUNR", "IRDM", "DXYZ",
        "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD",
        "CBRS", "MRVL",
    ]
    candidates = [(t, 2.0, 5.0) for t in tickers]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "normal"


def test_assess_deduplicates_tier_candidates_only_after_validation():
    """A higher-priority (higher estimated multiplier) but over-cap duplicate
    for a ticker must not claim that ticker in the tier-selection dedup
    before its own validity is checked -- otherwise a later, valid duplicate
    for the same ticker is silently dropped from tier selection even though
    plan_orders() itself would still place it, undercounting planned_spend/
    high_mult and selecting a cheaper tier than the real plan needs (Codex
    review: "Deduplicate tier candidates only after validation").

    17 clean 3x signals sit at the high_volatility/stress boundary (11-17 ->
    high_volatility, >17 -> stress per config/budgets.json). The 18th
    ticker's FIRST row is a $15 notional inconsistent with any base/multiplier
    pairing -- falling back to an inflated notional estimate (7.5x) that
    sorts it ahead of the ticker's own valid $3/3x row -- and is itself
    over the $10 per-order cap, so it must be skipped without blocking the
    valid row that follows. If the buggy mark-before-validate dedup were
    still in place, that valid row would be dropped, leaving only 17 valid
    signals (high_volatility) instead of 18 (stress).
    """
    tickers = [
        "DFTX", "TRIN", "AAAU", "SPCX", "EVR", "LUNR", "IRDM", "DXYZ",
        "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD",
        "CBRS",
    ]
    candidates = [(t, 3.0, 3.0) for t in tickers]
    candidates.append(("MRVL", 15.0, 3.0))  # over cap, inconsistent notional
    candidates.append(("MRVL", 3.0, 3.0))  # valid duplicate -- must still count
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "stress"


def test_assess_weekly_rollover_reserve_usd_reflects_into_the_stress_tier_cap():
    """The effective weekly ceiling must be derived from the tier assess()
    itself selects for today's candidates, never from a caller-precomputed
    value that could reflect a different (possibly stale) tier (Codex
    review: "Derive the weekly ceiling from the tier selected here")."""
    tickers = [
        "DFTX", "TRIN", "AAAU", "SPCX", "EVR", "LUNR", "IRDM", "DXYZ",
        "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD",
        "CBRS", "MRVL",
    ]
    candidates = [(t, 3.0, 3.0) for t in tickers]  # 18 3x signals -> stress tier

    current_week_key = iso_week_key(date(2026, 7, 28))

    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "stress"
    assert r["budget"]["weekly_cap_usd"] == pytest.approx(900.0)  # no reserve -> Normal's cap

    r_partial = dca_runner.assess(
        **_clean_kwargs(
            candidates=candidates,
            weekly_rollover_reserve_usd=500.0,
            weekly_rollover_reserve_week_key=current_week_key,
        )
    )
    assert r_partial["budget"]["weekly_cap_usd"] == pytest.approx(1400.0)

    r_full = dca_runner.assess(
        **_clean_kwargs(
            candidates=candidates,
            weekly_rollover_reserve_usd=1000.0,
            weekly_rollover_reserve_week_key=current_week_key,
        )
    )
    assert r_full["budget"]["weekly_cap_usd"] == pytest.approx(1800.0)  # capped at nominal


def test_assess_normal_tier_ignores_the_rollover_reserve():
    """Only a tier with uses_rollover_reserve=True should ever be affected."""
    r = dca_runner.assess(**_clean_kwargs(weekly_rollover_reserve_usd=1000.0))
    assert r["budget"]["tier"] == "normal"
    assert r["budget"]["weekly_cap_usd"] == pytest.approx(900.0)


@pytest.mark.parametrize("bad_reserve", [float("nan"), float("-inf"), -1.0])
def test_assess_rejects_non_finite_weekly_rollover_reserve_usd(bad_reserve):
    with pytest.raises(ValueError):
        dca_runner.assess(**_clean_kwargs(weekly_rollover_reserve_usd=bad_reserve))


@pytest.mark.parametrize("bad_reserve", [True, False])
def test_assess_rejects_a_boolean_weekly_rollover_reserve_usd(bad_reserve):
    """math.isfinite(True) succeeds since bool is an int subclass, so a
    boolean reserve would otherwise be retained as $1 (True) or $0 (False)
    and silently inflate (or understate) the derived Stress-tier weekly
    ceiling (Codex review: "Reject boolean rollover-reserve values")."""
    with pytest.raises(ValueError):
        dca_runner.assess(**_clean_kwargs(weekly_rollover_reserve_usd=bad_reserve))


def test_assess_rejects_an_oversized_weekly_rollover_reserve_usd():
    """A syntactically valid but astronomically large JSON integer (e.g.
    10**1000, injected via persisted StrategyState) passes the isinstance
    check but makes math.isfinite() raise OverflowError -- not return False
    -- while this validation runs, before assess()'s out() helper (and thus
    any ABORT payload) is even defined. Left uncaught, that would escape
    assess() entirely instead of producing the same controlled ValueError
    every other malformed reserve already gets (Codex review: "Catch
    oversized rollover reserves before finiteness checks")."""
    with pytest.raises(ValueError):
        dca_runner.assess(**_clean_kwargs(weekly_rollover_reserve_usd=10**1000))


def _stress_tier_candidates():
    tickers = [
        "DFTX", "TRIN", "AAAU", "SPCX", "EVR", "LUNR", "IRDM", "DXYZ",
        "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD",
        "CBRS", "MRVL",
    ]
    return [(t, 3.0, 3.0) for t in tickers]


def test_assess_zeroes_a_rollover_reserve_stamped_for_a_different_week():
    """A reserve computed for an earlier (or later) ISO week is stale and must
    not inflate this week's Stress-tier ceiling (Codex review: "Refresh the
    rollover reserve for the current week")."""
    candidates = _stress_tier_candidates()
    trading_day = date(2026, 7, 28)  # Tuesday
    stale_week_key = iso_week_key(trading_day.replace(day=trading_day.day - 7))

    r = dca_runner.assess(
        **_clean_kwargs(
            candidates=candidates,
            weekly_rollover_reserve_usd=1000.0,
            weekly_rollover_reserve_week_key=stale_week_key,
        )
    )
    assert r["budget"]["tier"] == "stress"
    assert r["budget"]["weekly_cap_usd"] == pytest.approx(900.0)  # stale reserve -> zeroed


def test_assess_honors_a_rollover_reserve_stamped_for_the_current_week():
    candidates = _stress_tier_candidates()
    trading_day = date(2026, 7, 28)
    current_week_key = iso_week_key(trading_day)

    r = dca_runner.assess(
        **_clean_kwargs(
            candidates=candidates,
            weekly_rollover_reserve_usd=1000.0,
            weekly_rollover_reserve_week_key=current_week_key,
        )
    )
    assert r["budget"]["tier"] == "stress"
    assert r["budget"]["weekly_cap_usd"] == pytest.approx(1800.0)


def test_assess_omitted_week_key_zeroes_a_nonzero_reserve():
    """A nonzero reserve is never legitimately unkeyed: a missing key must be
    treated as stale, not trusted, or a persisted/caller-supplied reserve
    with no key would keep applying to every future week forever (Codex
    review: "Require a week key for every nonzero rollover reserve")."""
    candidates = _stress_tier_candidates()
    r = dca_runner.assess(
        **_clean_kwargs(candidates=candidates, weekly_rollover_reserve_usd=1000.0)
    )
    assert r["budget"]["weekly_cap_usd"] == pytest.approx(900.0)


def test_assess_omitted_week_key_does_not_affect_a_zero_reserve():
    """A zero reserve is unaffected by the missing-key check either way,
    since there is nothing to zero."""
    candidates = _stress_tier_candidates()
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["weekly_cap_usd"] == pytest.approx(900.0)


def test_assess_carries_drop_pct_through_to_the_allocation_priority():
    """A 4-tuple's drop_pct must reach plan_orders' tie-break, or equal-
    multiplier candidates fall back to alphabetical ordering (Codex review:
    "Carry drop percentage through assess")."""
    candidates = [
        ("MSFT", 6.0, 3.0, 6.1),
        ("AAPL", 6.0, 3.0, 6.3),  # same multiplier, larger drop -> placed first
    ]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["AAPL", "MSFT"]


@pytest.mark.parametrize(
    "bad_candidates",
    [
        [("AAPL", None)],
        [("AAPL", "bad")],
        [("AAPL",)],
        [(1, 2, 3, 4, 5)],
    ],
)
def test_assess_aborts_on_a_malformed_candidate_row(bad_candidates):
    """Signal-generation output is external repo data with no upstream
    validation: a malformed row shape or a nonnumeric amount/multiplier/
    drop_pct would otherwise raise straight out of candidate coercion,
    escaping assess()'s never-raises contract before Gate 9 could write
    evidence or alert (Codex review: "Convert malformed candidate values
    into an abort verdict")."""
    r = dca_runner.assess(**_clean_kwargs(candidates=bad_candidates))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize(
    "bad_candidates",
    [
        [{"AAPL": "ignored", 2.0: "ignored"}],  # a dict iterates its KEYS, not values
        [{"AAPL": "ignored", 2.0: "ignored", 3.0: "ignored"}],
        ["AB"],  # a bare string is also iterable-with-length
    ],
)
def test_assess_aborts_on_a_mapping_candidate_row(bad_candidates):
    """Generic iterable unpacking does not care WHAT it unpacks: a mapping
    row like {"AAPL": "ignored", 2.0: "ignored"} iterates its KEYS, not its
    values, so it silently unpacks to the valid-looking pair ("AAPL", 2.0)
    -- laundering a row whose actual (ignored) values contain no usable
    candidate data at all into a real order, instead of aborting on the
    malformed shape (Codex review: "Reject mapping candidate rows before
    unpacking keys")."""
    r = dca_runner.assess(**_clean_kwargs(candidates=bad_candidates))
    assert r["verdict"] == "ABORT"


def test_parse_candidate_rows_rejects_a_mapping_row_directly():
    """The same rejection, exercised directly against _parse_candidate_rows
    rather than through assess()'s ABORT wrapper (Codex review: "Reject
    mapping candidate rows before unpacking keys")."""
    with pytest.raises(ValueError):
        dca_runner._parse_candidate_rows([{"AAPL": "ignored", 2.0: "ignored"}])


@pytest.mark.parametrize(
    "bad_candidates",
    [
        [("AAPL", True)],
        [("AAPL", 2.0, True)],
        [("AAPL", 2.0, 3.0, True)],
    ],
)
def test_assess_aborts_on_a_boolean_candidate_field(bad_candidates):
    """float(True) silently succeeds as 1.0, so a malformed amount_usd/
    multiplier/drop_pct=true would otherwise launder into a valid-looking
    numeric field that no later boundary can tell apart from a real one
    (Codex review: "Reject boolean candidate amounts before coercion")."""
    r = dca_runner.assess(**_clean_kwargs(candidates=bad_candidates))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize(
    "bad_candidate",
    [
        dca_runner.Candidate("AAPL", True),
        dca_runner.Candidate("AAPL", 2.0, multiplier=True),
        dca_runner.Candidate("AAPL", 2.0, drop_pct=True),
        dca_runner.Candidate("AAPL", "bad"),
    ],
)
def test_parse_candidate_rows_rejects_a_malformed_prebuilt_candidate(bad_candidate):
    """A prebuilt Candidate is just as public and caller-constructible as a
    raw tuple row -- Candidate("AAPL", True) must fail the same way a tuple
    ("AAPL", True) already does, instead of bypassing _json_number entirely
    and either laundering a boolean into a valid-looking order or raising an
    uncaught TypeError (Codex review: "Validate fields on prebuilt Candidate
    rows")."""
    with pytest.raises(ValueError):
        dca_runner._parse_candidate_rows([bad_candidate])


def test_assess_aborts_on_a_malformed_prebuilt_candidate():
    """The same prebuilt-Candidate validation gap, exercised through
    assess()'s own never-raises contract rather than _parse_candidate_rows
    directly."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[dca_runner.Candidate("AAPL", True)]))
    assert r["verdict"] == "ABORT"


def test_assess_aborts_when_order_history_is_none():
    """A broker-history layer that returns ``None`` instead of an iterable
    makes ``list(orders)`` raise ``TypeError`` before Gate 4/6 ever run; left
    uncaught, this would crash past assess()'s never-raises contract instead
    of producing the documented ABORT verdict (Codex review: "Return ABORT
    when order history cannot be materialized")."""
    r = dca_runner.assess(**_clean_kwargs(orders=None))
    assert r["verdict"] == "ABORT"


def test_assess_aborts_when_order_history_raises_while_being_consumed():
    """A lazy order-history iterable (e.g. a generator wrapping a paginated
    MCP call) that raises partway through consumption must fail closed into
    ABORT, not propagate past assess()'s never-raises contract before Gate 9
    can write evidence or alert (Codex review: "Return ABORT when order
    history cannot be materialized")."""

    def _boom():
        yield {"id": "ORD-1", "symbol": "AAPL", "side": "buy", "state": "filled"}
        raise RuntimeError("broker history stream disconnected")

    r = dca_runner.assess(**_clean_kwargs(orders=_boom()))
    assert r["verdict"] == "ABORT"


def test_assess_aborts_on_an_oversized_candidate_amount():
    """A syntactically valid but astronomically large amount_usd (as a
    caller could pass directly, or as json.loads would parse from
    signal-generation output containing a huge literal integer) makes
    float() raise OverflowError rather than ValueError; left uncaught, this
    would crash the scheduled run instead of producing the documented ABORT
    (Codex review: "Convert oversized JSON numbers into a controlled
    rejection")."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 10**1000)]))
    assert r["verdict"] == "ABORT"


def test_assess_aborts_when_a_representable_finite_amount_overflows_cent_conversion():
    """A representable finite candidate amount (e.g. 1e308) is well past any
    real per-order cap but is still a legitimate Python float, so
    _json_number() lets it through -- amount_usd * 100 then overflows to
    +/-inf, and round() on an infinite float raises OverflowError. _cents()
    must absorb that instead of crashing plan_orders()/assess() before
    either can refuse the single malformed candidate or return ABORT (Codex
    review: "Reject oversized finite amounts before cent conversion")."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 1e308, 3.0)]))
    assert r["verdict"] == "PROCEED"
    # The per-order-cap check runs before the multiplier/notional-mismatch
    # check and already refuses this candidate (1e308 > MAX_PER_ORDER_USD
    # trivially) -- the point of this test is that neither check crashes on
    # the _cents() overflow, not which refusal reason wins.
    assert ["AAPL", dca_runner.REFUSED_OVER_PER_ORDER_CAP] in r["plan"]["refused"]


def test_assess_aborts_when_candidates_raises_while_being_consumed():
    """A lazy signal iterable (e.g. a generator wrapping a failing
    signal-generation service) that raises an unrelated exception
    (RuntimeError, ConnectionError, ...) partway through consumption must
    fail closed into ABORT the same way a malformed row shape does, not
    escape assess()'s never-raises contract before Gate 9 can write evidence
    or alert (Codex review: "Convert lazy candidate failures into ABORT")."""

    def _boom():
        yield ("AAPL", 2.0)
        raise RuntimeError("signal-generation service disconnected")

    r = dca_runner.assess(**_clean_kwargs(candidates=_boom()))
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_drop", [float("nan"), float("inf"), float("-inf")])
def test_allocation_priority_key_falls_back_to_zero_for_a_non_finite_drop(bad_drop):
    """A NaN drop_pct must not reach the sort key directly: NaN comparisons
    are always False, so Python's sort order would depend on the surrounding
    candidates' order instead of on drop (Codex review: "Reject non-finite
    drop percentages before sorting")."""
    good = dca_runner.Candidate("AAPL", 6.0, multiplier=3.0, drop_pct=0.08)
    bad = dca_runner.Candidate("MSFT", 6.0, multiplier=3.0, drop_pct=bad_drop)
    key_good = dca_runner._allocation_priority_key(good)
    key_bad = dca_runner._allocation_priority_key(bad)
    assert key_bad[1] == 0.0
    assert key_good < key_bad  # AAPL's real 8% drop outranks MSFT's fallback 0%


@pytest.mark.parametrize("bad_amount", [float("nan"), float("inf"), float("-inf")])
def test_estimated_multiplier_falls_back_to_the_ladder_minimum_for_a_non_finite_amount(bad_amount):
    """A NaN amount_usd with no valid multiplier must not poison the sort key
    the same way an unguarded NaN drop_pct did (Codex review: "Reject
    malformed amounts before allocation sorting")."""
    bad = dca_runner.Candidate("MSFT", bad_amount)
    assert dca_runner._estimated_multiplier(bad) == dca_runner.MIN_LADDER_MULTIPLIER
    good = dca_runner.Candidate("AAPL", 2.0)  # 1x, the ladder minimum
    assert dca_runner._allocation_priority_key(good) <= dca_runner._allocation_priority_key(bad)


def test_estimated_multiplier_infers_the_reduced_base_multiplier_from_a_usable_drop():
    """Candidate("AAPL", 4.0, None, 7.0): no multiplier claim, but $4.00 is
    exactly 4.0x the reduced $1.00 base, and 7.0% falls in the 4x tier's
    [7.0, 7.5) drop range -- a genuine reduced-base 4x signal. Dividing
    amount_usd by the $2 base alone (the prior behavior) would estimate only
    2x ($4.00 / $2.00), understating both high-multiplier tier counting and
    allocation priority against real 3x candidates (Codex review: "Infer
    reduced-base multipliers from usable drops")."""
    cand = dca_runner.Candidate("AAPL", 4.0, drop_pct=7.0)
    assert dca_runner._estimated_multiplier(cand) == pytest.approx(4.0)


def test_estimated_multiplier_ignores_a_drop_that_disagrees_with_the_notional():
    """A usable drop_pct whose implied multiplier does NOT reproduce
    amount_usd on either base must not be trusted -- falls through to the
    plain notional-based estimate instead."""
    # drop_pct=7.0 implies the 4x tier, but $3.00 is neither 4.0 x $2.00 nor
    # 4.0 x $1.00 -- the drop-based inference must not apply here.
    cand = dca_runner.Candidate("AAPL", 3.0, drop_pct=7.0)
    assert dca_runner._estimated_multiplier(cand) == pytest.approx(1.5)  # 3.0 / 2.0 base


def test_assess_counts_a_reduced_base_signal_as_high_multiplier_via_its_drop():
    """The same reduced-base inference must reach assess()'s tier selection:
    a 4x reduced-base candidate with no multiplier claim must count toward
    high_mult, matching HIGH_MULTIPLIER_THRESHOLD (3.0x)."""
    r = dca_runner.assess(**_clean_kwargs(candidates=[("AAPL", 4.0, None, 7.0)]))
    assert r["verdict"] == "PROCEED"
    assert r["plan"]["to_place"][0]["ticker"] == "AAPL"


def test_assess_buying_power_usd_caps_the_plan():
    r = dca_runner.assess(
        **_clean_kwargs(candidates=[("AAPL", 4.0), ("MSFT", 4.0)], buying_power_usd=5.0)
    )
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["AAPL"]
    assert [t for t, reason in r["plan"]["skipped"]] == ["MSFT"]


def test_assess_aborts_when_buying_power_is_unconfirmed():
    """Every executor prompt's Gate 6 requires an abort when buying power
    cannot be confirmed; treating an omitted buying_power_usd as unlimited
    headroom would let assess() return PROCEED with a placeable plan
    despite no confirmed funds (Codex review: "Abort assessment when buying
    power is unavailable")."""
    r = dca_runner.assess(
        **_clean_kwargs(candidates=[("AAPL", 2.0)], buying_power_usd=None)
    )
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_power", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_assess_aborts_when_buying_power_is_malformed(bad_power):
    """A supplied-but-unusable buying_power_usd is not caught by the omitted-
    value check above and would otherwise reach plan_orders(), whose own
    finiteness check raises a bare ValueError that escapes assess()'s
    documented never-raises contract (Codex review: "Return ABORT for
    malformed buying power")."""
    r = dca_runner.assess(
        **_clean_kwargs(candidates=[("AAPL", 2.0)], buying_power_usd=bad_power)
    )
    assert r["verdict"] == "ABORT"


@pytest.mark.parametrize("bad_power", [True, False, "5.00"])
def test_assess_aborts_when_buying_power_is_non_numeric(bad_power):
    """math.isfinite(True) succeeds since bool is an int subclass, so a
    boolean buying_power_usd would otherwise be treated as $1 of confirmed
    buying power and could PROCEED with a live order despite zero real
    balance; a string instead raised an uncaught TypeError. Both must
    produce the same fail-closed ABORT as the omitted/NaN cases (Codex
    review: "Reject nonnumeric buying-power values")."""
    r = dca_runner.assess(
        **_clean_kwargs(candidates=[("AAPL", 2.0)], buying_power_usd=bad_power)
    )
    assert r["verdict"] == "ABORT"


def test_assess_redacts_a_malformed_buying_power_value_from_the_abort_reason():
    """A nonnumeric buying_power_usd can be arbitrary data the injected
    account-data layer returned instead of a scalar -- a dict carrying an
    account identifier, token, or other request metadata, for instance --
    and this ABORT reason is exactly what gets persisted as run evidence and
    sent through alert channels. Only a safe classification (the value's
    type) may appear there, not its repr (Codex review: "Redact malformed
    buying-power values from evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    r = dca_runner.assess(
        **_clean_kwargs(
            candidates=[("AAPL", 2.0)],
            buying_power_usd={"account_number": secret_account_number, "token": "shh"},
        )
    )
    assert r["verdict"] == "ABORT"
    assert secret_account_number not in r["reason"]
    assert "shh" not in r["reason"]
    assert "dict" in r["reason"]


def test_execute_plan_redacts_a_malformed_buying_power_value():
    """Same redaction requirement at the direct execute_plan() boundary: the
    raised ValueError IS the caller's required failure evidence (Codex
    review: "Redact malformed buying-power values from evidence")."""
    secret_account_number = "123456789"  # synthetic, allow-listed account-shaped value
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: {"id": "ok"},
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    plan = dca_runner.Plan(to_place=[dca_runner.PlannedOrder("AAPL", 2.0)])
    with pytest.raises(ValueError) as excinfo:
        dca_runner.execute_plan(
            plan,
            broker,
            run_id="test-run-2026-07-28",
            snapshot=dca_runner.budget_snapshot([], date(2026, 7, 28)),
            trading_day=date(2026, 7, 28),
            orders=[], tier=BudgetTier("normal", 180.0, 900.0),
            orders_account_number=TEST_ACCOUNT_NUMBER,
            now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            slot_id="0935",
            orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
            buying_power_usd={"account_number": secret_account_number, "token": "shh"},
            candidates=[("AAPL", 2.0)],
        )
    assert secret_account_number not in str(excinfo.value)
    assert "shh" not in str(excinfo.value)
    assert "dict" in str(excinfo.value)


def test_assess_aborts_when_buying_power_is_oversized():
    """A syntactically valid but astronomically large JSON integer (e.g.
    10**1000) passes the numeric-type check but makes math.isfinite() raise
    OverflowError rather than return False, previously escaping assess()'s
    never-raises contract entirely instead of producing the documented
    ABORT (Codex review: "Convert oversized buying power into an abort")."""
    r = dca_runner.assess(
        **_clean_kwargs(candidates=[("AAPL", 2.0)], buying_power_usd=10**1000)
    )
    assert r["verdict"] == "ABORT"


def test_assess_weekend_ends_silently_even_with_unconfirmed_buying_power():
    """A normal calendar fact (weekend/holiday) must still end silently, not
    abort, even when buying power was never confirmed -- nothing was going
    to be bought anyway."""
    r = dca_runner.assess(
        **_clean_kwargs(now=datetime(2026, 8, 1, 9, 43, tzinfo=ET), buying_power_usd=None)
    )
    assert r["verdict"] == "END_SILENTLY"


def test_assess_weekend_ends_silently_even_with_unmaterializable_order_history():
    """A weekend/holiday/early-close afternoon needs no broker history at all
    to establish END_SILENTLY -- Gate 0 must run BEFORE orders is ever
    touched, or a None order-history response (or a lazy iterable that
    raises) would produce an actionable ABORT alert instead of the correct,
    silent no-op for a day nothing was ever going to be bought (Codex
    review: "Run the calendar gate before consuming broker history")."""
    r = dca_runner.assess(
        **_clean_kwargs(now=datetime(2026, 8, 1, 9, 43, tzinfo=ET), orders=None)
    )
    assert r["verdict"] == "END_SILENTLY"


def test_assess_excludes_a_ticker_already_bought_earlier_today(monkeypatch):
    """A 10:30 candidate for a ticker the 09:35 slot already bought must be refused.

    Otherwise the same ticker can be bought once per slot, up to seven times a
    day, violating one_order_per_ticker_per_day (Codex review: "Exclude
    tickers already bought earlier today").
    """
    # pins first-slot-wins ON; this test exists to cover exactly that rule.
    monkeypatch.setattr(dca_runner, "ONE_ORDER_PER_TICKER_PER_DAY", True)
    earlier = [_order("AAPL", 2.0, _utc(2026, 7, 28, 13, 40))]  # 09:40 ET, the 09:35 slot
    r = dca_runner.assess(**_clean_kwargs(
        slot_id="1030",
        now=datetime(2026, 7, 28, 10, 43, tzinfo=ET),
        orders=earlier,
        candidates=[("AAPL", 4.0), ("MSFT", 4.0)],
    ))
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["MSFT"]
    assert ["AAPL", dca_runner.REFUSED_ALREADY_BOUGHT_TODAY] in r["plan"]["refused"]


def test_assess_dedupes_and_validates_before_selecting_the_tier():
    """Duplicate/invalid rows must not inflate the tier used for real caps.

    14 duplicate $10 AAPL rows plus one $2 MSFT row is really only $12 of
    unique demand; using the raw (undeduped) $142 would wrongly select a
    richer tier (Codex review: "Deduplicate candidates before selecting the
    tier").
    """
    candidates = [("AAPL", 10.0)] * 14 + [("MSFT", 2.0)]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    assert r["budget"]["tier"] == "normal"
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["AAPL", "MSFT"]


def test_assess_dedupe_keeps_the_allocation_priority_winner_for_tier_selection():
    """When duplicate rows for one ticker disagree, tier selection must keep
    the same (higher-multiplier) row plan_orders() itself keeps, not
    whichever row happened to come first in caller order (Codex review:
    "Deduplicate candidates using allocation priority")."""
    tickers = ["MSFT", "NVDA", "AMD", "INTC", "IBM"]  # 5 other 3x signals
    candidates = [
        ("AAPL", 2.0, 1.0),  # duplicate #1: 1x, low priority
        ("AAPL", 6.0, 3.0),  # duplicate #2: 3x, high priority -- the real signal
        *[(t, 6.0, 3.0) for t in tickers],
    ]
    r = dca_runner.assess(**_clean_kwargs(candidates=candidates))
    # 6 total 3x signals (AAPL's winning row + 5 others) -> Elevated, not Normal.
    assert r["budget"]["tier"] == "elevated"


# ---------------------------------------------------------------------------
# Daily order-count cap (config/budgets.json circuit_breakers.max_orders_per_day)
# Added 2026-07-31 at Geoff's direction: at most 8 orders a day across all seven
# slots and manual buying, changeable by editing that one config number.
# ---------------------------------------------------------------------------


def _count_cap_tier():
    return dca_runner.select_budget_tier(0.0, 0)


def _count_cap_cands(n):
    """n whitelisted 1x candidates at the $2 base, so nothing is refused for
    sizing reasons and only the count cap can stop the batch."""
    return [dca_runner.Candidate(t, 2.0, 1.0) for t in sorted(dca_runner.ACTIVE_TICKERS)[:n]]


def _count_cap_snapshot(day_order_count, *, day_notional_usd=0.0):
    return dca_runner.BudgetSnapshot(
        day=date(2026, 8, 4),
        day_notional_usd=day_notional_usd,
        week_notional_usd=day_notional_usd,
        day_order_count=day_order_count,
    )


def test_the_daily_order_count_cap_stops_the_batch_at_the_configured_limit():
    """Twelve placeable candidates, cap of 8, dollar headroom to spare: exactly
    8 are planned and the rest are skipped for the count, not the notional."""
    plan = dca_runner.plan_orders(_count_cap_cands(12), _count_cap_snapshot(0), _count_cap_tier())
    assert len(plan.to_place) == dca_runner.MAX_ORDERS_PER_DAY == 8
    assert len(plan.skipped) == 4
    assert {reason for _, reason in plan.skipped} == {dca_runner.SKIPPED_DAILY_ORDER_COUNT_CAP}


def test_the_daily_order_count_cap_counts_orders_already_placed_today():
    """The cap is shared across the whole day, not per run: six orders already
    submitted leaves room for two, whoever placed them."""
    plan = dca_runner.plan_orders(_count_cap_cands(5), _count_cap_snapshot(6), _count_cap_tier())
    assert len(plan.to_place) == 2
    assert {reason for _, reason in plan.skipped} == {dca_runner.SKIPPED_DAILY_ORDER_COUNT_CAP}


def test_the_daily_order_count_cap_places_nothing_once_the_day_is_full():
    plan = dca_runner.plan_orders(_count_cap_cands(3), _count_cap_snapshot(8), _count_cap_tier())
    assert plan.to_place == []
    assert len(plan.skipped) == 3


def test_the_daily_order_count_cap_is_changeable(monkeypatch):
    """The limit is a config number, not a constant baked into the planner."""
    monkeypatch.setattr(dca_runner, "MAX_ORDERS_PER_DAY", 3)
    assert len(dca_runner.plan_orders(
        _count_cap_cands(9), _count_cap_snapshot(0), _count_cap_tier()).to_place) == 3
    monkeypatch.setattr(dca_runner, "MAX_ORDERS_PER_DAY", 20)
    assert len(dca_runner.plan_orders(
        _count_cap_cands(9), _count_cap_snapshot(0), _count_cap_tier()).to_place) == 9


def test_the_dollar_caps_still_bind_independently_of_the_count_cap():
    """The count cap is additional to the daily notional cap, not a replacement:
    a day under 8 orders but at its dollar ceiling still stops."""
    snapshot = _count_cap_snapshot(0, day_notional_usd=179.0)
    plan = dca_runner.plan_orders(_count_cap_cands(3), snapshot, _count_cap_tier())
    assert plan.to_place == []
    assert dca_runner.SKIPPED_DAILY_CAP in {reason for _, reason in plan.skipped}


def test_budget_snapshot_counts_todays_buy_attempts_including_dead_ones():
    """A cancelled or rejected buy still consumed one of the day's orders."""
    day = date(2026, 8, 4)
    orders = [
        dca_runner.BrokerOrder(
            symbol="AAPL", side="buy", dollar_amount=2.0,
            created_at=datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc), state="filled"),
        dca_runner.BrokerOrder(
            symbol="MSFT", side="buy", dollar_amount=2.0,
            created_at=datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc), state="cancelled"),
        dca_runner.BrokerOrder(  # a different day, must not count
            symbol="AMD", side="buy", dollar_amount=2.0,
            created_at=datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc), state="filled"),
    ]
    assert dca_runner.budget_snapshot(orders, day).day_order_count == 2


def test_a_same_day_repeat_is_not_refused_when_first_slot_wins_is_off(monkeypatch):
    """With one_order_per_ticker_per_day false, assess() must leave the ticker
    alone so scripts/same_day_top_up_gate.py can size the top-up. If assess()
    also refused it, the top-up path could never execute."""
    monkeypatch.setattr(dca_runner, "ONE_ORDER_PER_TICKER_PER_DAY", False)
    prior = dca_runner.BrokerOrder(
        symbol="AAPL", side="buy", dollar_amount=2.0,
        created_at=datetime(2026, 7, 28, 13, 40, tzinfo=timezone.utc), state="filled")
    # A later slot, so the 09:40 ET prior buy sits OUTSIDE this slot's own
    # sole-executor window and Gate 4 does not (correctly) abort on it first.
    r = dca_runner.assess(**_clean_kwargs(
        slot_id="1030",
        now=datetime(2026, 7, 28, 10, 43, 14, tzinfo=ET),
        orders=[prior],
        candidates=[("AAPL", 2.0, 1.0)],
    ))
    assert r["verdict"] == "PROCEED"
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["AAPL"]
    assert not any(reason == dca_runner.REFUSED_ALREADY_BOUGHT_TODAY
                   for _, reason in r["plan"]["refused"])


def test_assess_reports_the_count_cap_and_the_ticker_rule_in_its_verdict():
    """Both knobs are visible in the run evidence, so a report can show which
    limit was in force without anyone re-reading config."""
    r = dca_runner.assess(**_clean_kwargs())
    assert r["budget"]["max_orders_per_day"] == dca_runner.MAX_ORDERS_PER_DAY
    assert r["budget"]["one_order_per_ticker_per_day"] is False
    assert r["budget"]["day_order_count"] == 0


def test_plan_orders_accepts_complete_per_ticker_calibration():
    """A valid calibrated tier must not be checked against the global curve."""
    candidate = dca_runner.Candidate(
        "TRIN", 5.0, multiplier=5.0, drop_pct=3.1,
        sizing_base_usd=1.0, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert [(order.ticker, order.amount_usd) for order in plan.to_place] == [("TRIN", 5.0)]
    assert plan.refused == []


def test_plan_orders_refuses_partial_per_ticker_calibration():
    candidate = dca_runner.Candidate("TRIN", 10.0, multiplier=5.0, drop_pct=3.1, sizing_base_usd=2.0)
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert plan.refused == [("TRIN", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER)]


def test_plan_orders_refuses_calibration_not_bound_to_ticker_config():
    candidate = dca_runner.Candidate(
        "AAPL", 9.0, multiplier=3.0, drop_pct=3.0,
        sizing_base_usd=3.0, sizing_min_drop_pct=2.0, sizing_max_drop_pct=4.0,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert plan.refused == [("AAPL", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER)]


def test_plan_orders_refuses_calibration_for_a_fallback_ticker():
    """A ticker listed in per_ticker_sizing.json's fallback_tickers is sized
    off the global ladder; its own (stale) tickers[] entry is not
    authoritative and must not validate calibration evidence."""
    sizing = dca_runner._read_config("per_ticker_sizing.json")
    fallback_ticker = sizing["fallback_tickers"][0]
    ticker_sizing = sizing["tickers"][fallback_ticker]
    tier = ticker_sizing["tiers"][0]
    candidate = dca_runner.Candidate(
        fallback_ticker, ticker_sizing["base_amount_usd"] * tier["multiplier"],
        multiplier=tier["multiplier"], drop_pct=tier["min_drop_pct"],
        sizing_base_usd=ticker_sizing["base_amount_usd"],
        sizing_min_drop_pct=tier["min_drop_pct"], sizing_max_drop_pct=None,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert plan.refused == [(fallback_ticker, dca_runner.REFUSED_INCONSISTENT_MULTIPLIER)]


def test_plan_orders_refuses_oversized_calibration_integer_instead_of_crashing():
    """math.isfinite(10**1000) raises OverflowError; a persisted JSON
    integer can be arbitrarily large, and the per-candidate refusal must
    survive that rather than aborting the whole plan_orders() call."""
    candidate = dca_runner.Candidate(
        "TRIN", 5.0, multiplier=5.0, drop_pct=3.1,
        sizing_base_usd=10**1000, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert plan.refused == [("TRIN", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER)]


def test_candidate_multiplier_notional_mismatch_refuses_an_oversized_amount_usd():
    """math.isfinite(10**1000) raises OverflowError; an astronomically
    large amount_usd must fail closed the same way an oversized
    calibration value already does, rather than escaping uncaught."""
    candidate = dca_runner.Candidate("AAPL", 10**1000, multiplier=1.0, drop_pct=1.0)
    assert dca_runner._candidate_multiplier_notional_mismatch(candidate) is True


def test_plan_orders_refuses_an_oversized_amount_usd_instead_of_crashing():
    candidate = dca_runner.Candidate("AAPL", 10**1000, multiplier=1.0, drop_pct=1.0)
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert len(plan.refused) == 1
    assert plan.refused[0][0] == "AAPL"


def test_plan_orders_keeps_kill_switch_capped_calibrated_candidate_eligible():
    """dca_risk.apply_risk_guard can cap a deep-drop calibrated signal down
    to the floor multiplier while the candidate keeps its real, deep
    drop_pct -- the floor tier's own finite upper bound must not refuse it."""
    candidate = dca_runner.Candidate(
        "TRIN", 1.0, multiplier=1.0, drop_pct=5.0,
        sizing_base_usd=1.0, sizing_min_drop_pct=0.1185, sizing_max_drop_pct=0.5585,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert [(order.ticker, order.amount_usd) for order in plan.to_place] == [("TRIN", 1.0)]
    assert plan.refused == []


def test_plan_orders_refuses_a_sub_threshold_drop_for_a_capped_minimum_calibrated_multiplier():
    """The floor's own LOWER bound is still enforced even for a capped
    candidate -- the kill-switch only ever caps a genuine buy signal down to
    the floor, it never fabricates one."""
    candidate = dca_runner.Candidate(
        "TRIN", 1.0, multiplier=1.0, drop_pct=0.05,
        sizing_base_usd=1.0, sizing_min_drop_pct=0.1185, sizing_max_drop_pct=0.5585,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert plan.refused == [("TRIN", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER)]


def test_plan_orders_accepts_a_reduced_same_day_topup_calibrated_amount():
    """scripts/same_day_top_up_gate.py (Gate 6.5) can shrink buy_usd below
    the full calibrated allocation while the candidate still carries its
    original sizing evidence; the full-allocation product is a ceiling, not
    an exact target."""
    candidate = dca_runner.Candidate(
        "TRIN", 3.0, multiplier=5.0, drop_pct=3.5,
        sizing_base_usd=1.0, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert [(order.ticker, order.amount_usd) for order in plan.to_place] == [("TRIN", 3.0)]
    assert plan.refused == []


def test_plan_orders_still_refuses_a_calibrated_amount_above_the_full_allocation_ceiling():
    candidate = dca_runner.Candidate(
        "TRIN", 6.0, multiplier=5.0, drop_pct=3.5,
        sizing_base_usd=1.0, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert plan.refused == [("TRIN", dca_runner.REFUSED_INCONSISTENT_MULTIPLIER)]


def test_parse_candidate_rows_preserves_calibration_fields_on_a_prebuilt_candidate():
    """_parse_candidate_rows() must carry sizing_base_usd/sizing_min_drop_pct/
    sizing_max_drop_pct through, not just ticker/amount/multiplier/drop_pct --
    both assess() and execute_plan() route Candidate rows through this
    parser, so dropping the evidence there rejects the same calibrated
    candidate plan_orders() accepts directly."""
    candidate = dca_runner.Candidate(
        "TRIN", 5.0, multiplier=5.0, drop_pct=3.1,
        sizing_base_usd=1.0, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
    )
    parsed = dca_runner._parse_candidate_rows([candidate])
    assert parsed == [candidate]


def test_assess_accepts_a_calibrated_candidate_carried_through_parsing():
    r = dca_runner.assess(**_clean_kwargs(
        candidates=[dca_runner.Candidate(
            "TRIN", 5.0, multiplier=5.0, drop_pct=3.1,
            sizing_base_usd=1.0, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
        )],
    ))
    assert r["verdict"] == "PROCEED"
    assert [o["ticker"] for o in r["plan"]["to_place"]] == ["TRIN"]
    assert not any(reason == dca_runner.REFUSED_INCONSISTENT_MULTIPLIER
                   for _, reason in r["plan"]["refused"])


def test_plan_orders_accepts_a_configured_ticker_with_complete_calibration_evidence():
    """Same DFTX candidate as above, but with the real calibration evidence
    supplied and an actually-qualifying drop, must still be accepted."""
    candidate = dca_runner.Candidate(
        "DFTX", 5.0, multiplier=5.0, drop_pct=11.5,
        sizing_base_usd=1.0, sizing_min_drop_pct=11.0446, sizing_max_drop_pct=None,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert [(order.ticker, order.amount_usd) for order in plan.to_place] == [("DFTX", 5.0)]
    assert plan.refused == []


def test_plan_orders_refuses_a_calibrated_topup_below_the_broker_minimum():
    """config/per_ticker_sizing.json's minimum_order_usd is $1.00 --
    docs/dca_operational_todos.md records Robinhood actually rejecting a
    same-day top-up below the broker's own minimum order size. A reduced
    top-up must be refused here, not forwarded to native placement."""
    candidate = dca_runner.Candidate(
        "TRIN", 0.50, multiplier=5.0, drop_pct=3.5,
        sizing_base_usd=1.0, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
    )
    snapshot = dca_runner.BudgetSnapshot(date(2026, 8, 11), 0.0, 0.0, 0)
    plan = dca_runner.plan_orders([candidate], snapshot, dca_runner.BUDGET_TIERS[0])
    assert plan.to_place == []
    assert plan.refused == [("TRIN", dca_runner.REFUSED_BELOW_BROKER_MINIMUM_ORDER_SIZE)]


def test_estimated_multiplier_trusts_a_reduced_calibrated_topup():
    """A same-day top-up reduced below the full calibrated allocation (e.g.
    a TRIN 5x buy shrunk from $5 to $3 by Gate 6.5) must still report its
    real 5x multiplier for tier counting and allocation priority, not fall
    back to a plain notional-division estimate that undercounts it."""
    candidate = dca_runner.Candidate(
        "TRIN", 3.0, multiplier=5.0, drop_pct=3.5,
        sizing_base_usd=1.0, sizing_min_drop_pct=3.0727, sizing_max_drop_pct=None,
    )
    assert dca_runner._estimated_multiplier(candidate) == 5.0


def test_oversized_numeric_response_values_never_raise_out_of_execute_plan():
    """An arbitrarily large JSON integer must produce a VERDICT, not an
    exception.

    ``math.isfinite()`` converts its argument to a float first, so
    ``10**1000`` made it raise ``OverflowError`` rather than return ``False``.
    ``_placement_failure_reason`` runs INSIDE ``execute_plan``'s placement
    ``try``, so its raise was converted into an ordinary failure and hid the
    problem -- but ``_placement_response_may_hold_a_live_order`` is consulted
    OUTSIDE that ``try``, where the same value aborted ``execute_plan`` and
    destroyed its ``ExecutionResult``, taking the record of every order
    already submitted earlier in the batch with it (Codex review, PR #786:
    "Treat oversized numeric order IDs as unusable").

    The module already documented and caught exactly this trap for candidate
    amounts (``_candidate_is_sortable``) and fill figures (``budget_snapshot``);
    the order-id validators added in this PR did not inherit it."""
    big = 10 ** 1000
    responses = [
        {"id": big},
        {"order_id": big},
        {"id": "ORD", "dollar_amount": big},
        {"id": big, "state": "queued", "cumulative_quantity": big},
        {"data": {"orders": [{"id": big}]}},
    ]
    for response in responses:
        # Neither traversal may raise -- that is the whole finding.
        reason = dca_runner._placement_failure_reason(
            response, expected_symbol="RVII", expected_side="buy",
            expected_amount_usd=2.0,
        )
        assert reason is not None, response
        verdict = dca_runner._placement_response_may_hold_a_live_order(response)
        assert isinstance(verdict, bool), response

    # An oversized POSITIVE id halts. This assertion originally read `is
    # False` -- isolate -- on the reasoning that an unusable id names no order.
    # Round 39 showed that was wrong: it is the broker naming an order this
    # batch cannot track, and the identical shape with an ordinary id is a
    # clean submission (see
    # test_an_untrackable_positive_id_halts_rather_than_reading_as_no_order).
    # The assertion was wrong, not the fix.
    assert dca_runner._placement_response_may_hold_a_live_order({"id": big}) is True
    # ... and a real identity beside an unreadable figure still halts: the
    # figure cannot rule execution out, which is the standing rule.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"id": "ORD", "state": "cancelled", "cumulative_quantity": big}
    ) is True


def test_oversized_values_do_not_abort_execute_plan_mid_batch():
    """The end-to-end consequence: the batch keeps its ExecutionResult.

    Before the fix the second ticker's oversized id raised out of
    ``execute_plan`` entirely -- from the ``_placement_response_may_hold_a_
    live_order`` call that sits OUTSIDE the placement ``try`` -- so the first
    ticker's confirmed submission, a real order at the broker, was lost along
    with the result object (Codex review, PR #786: "Treat oversized numeric
    order IDs as unusable").

    That oversized id now HALTS rather than isolating (round 39: an id we
    cannot use is not no id), which makes this the sharper demonstration of the
    same point -- the batch stops deliberately, in an orderly way, and AAPL's
    record survives, where before the whole result object was destroyed."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        if kw["symbol"] == "MSFT":
            return {"id": 10 ** 1000}
        return {
            "id": f"ORD-{kw['symbol']}", "state": "queued", "symbol": kw["symbol"],
            "side": "buy", "dollar_amount": kw["dollar_amount"],
            "client_order_id": kw["ref_id"],
        }

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [
        dca_runner.Candidate("AAPL", 2.0),
        dca_runner.Candidate("MSFT", 2.0),
        dca_runner.Candidate("NVDA", 2.0),
    ]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    result = dca_runner.execute_plan(
        plan, broker,
        run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=candidates,
    )

    # No raise. The batch stopped AT the malformed response rather than
    # crashing through it, and everything placed before it is still recorded.
    assert calls == ["AAPL", "MSFT"]
    assert result.halted_on == "MSFT"
    assert [f.ticker for f in result.failures] == ["MSFT"]
    assert len(result.submitted) == 1
    assert result.submitted == ["AAPL"]


def test_an_untrackable_positive_id_halts_rather_than_reading_as_no_order():
    """An id we cannot USE is not the same thing as no id at all.

    Round 38 made `_is_usable_order_id` reject an oversized integer instead of
    raising on it. The no-identity branch then read that False as "the broker
    named no order" and ISOLATED -- while the identical response shape carrying
    an ordinary id is a clean submission. One shape cannot mean both "order
    placed" and "no order exists" depending only on how large the number is
    (Codex review, PR #786: "Preserve acceptance evidence from oversized
    positive IDs").

    Untrackability is AMBIGUITY, which is what the rest of this classifier
    already does with it: isolate only on proof nothing was accepted."""
    big = 10 ** 1000

    # The asymmetry itself: an ordinary id on this shape is a SUBMISSION.
    assert dca_runner._placement_failure_reason(
        {"id": "ORD"}, expected_symbol="RVII", expected_side="buy",
        expected_amount_usd=2.0,
    ) is None

    # So the oversized twin must not be read as proof of no order.
    for value in (big, float("inf")):
        assert dca_runner._placement_response_may_hold_a_live_order({"id": value}) is True, value
        assert dca_runner._placement_response_may_hold_a_live_order({"order_id": value}) is True, value
    # ... including when it is buried, since the walk is level-by-level.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"data": {"order": {"id": big}}}
    ) is True


def test_no_id_sentinels_still_isolate_after_the_untrackable_id_halt():
    """The carve-out stays no WIDER than its justification (round 29's rule).

    A positive number we cannot represent is the broker handing us something.
    Every value rejected for being a NON-id must keep isolating -- above all
    the documented `-1`/`0` sentinels a wrapper uses for "no id was assigned",
    since halting on those would halt ordinary refusals and undo round 21."""
    big = 10 ** 1000
    for value in (-big, float("-inf"), float("nan"), -1, 0, "", "   ", True, False, None):
        assert dca_runner._placement_response_may_hold_a_live_order(
            {"id": value}
        ) is False, value

    # And the reported incident's own shape -- no id key at all -- is untouched.
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "error": "RVII is not tradable"}
    ) is False
    assert dca_runner._placement_response_may_hold_a_live_order(
        {"success": False, "state": "rejected", "cumulative_quantity": 0}
    ) is False


def test_every_best_effort_numeric_parse_survives_an_unbounded_integer():
    """The round-38 sweep, run properly across the modules that carry their
    own copy of this pattern.

    `float(10**1000)` raises `OverflowError`, which is neither `TypeError` nor
    `ValueError`, and a JSON-decoded integer is unbounded in Python -- so every
    best-effort parse reachable from a broker or artifact payload violated its
    own contract. `morning_dca._finite_float_or_none`'s docstring said "Never
    raises" in as many words."""
    import dca_live_adapters
    import morning_dca
    import near_close_dca

    big = 10 ** 1000
    assert math.isnan(near_close_dca._alpaca_float_or_nan(big))
    assert near_close_dca._csv_float(big) is None
    assert near_close_dca._usable_positive_float(big) is None
    assert morning_dca._finite_float_or_none(big) is None
    assert dca_live_adapters._to_float(big) == 0.0
    assert dca_live_adapters._to_float(big, 7.5) == 7.5
    # ... and the caller that reads a fill through one of them.
    assert near_close_dca._parse_alpaca_order_fill(
        {"filled_qty": big, "filled_avg_price": 1.0}
    ) is None


def test_a_successful_response_naming_a_second_order_halts_the_batch():
    """"One order validated" and "only one order exists" are different claims.

    ``_placement_failure_reason`` checks the single order its
    ``response`` -> ``data`` -> ``data.order`` descent selects. Returning
    ``None`` says that order matches the request; it says nothing about the
    rest of the payload. Every identity rule in this module was built for the
    FAILURE path, so a sibling branch naming a second live order was recorded
    as one clean submission and the batch continued -- with that order absent
    from the run's cap and buying-power arithmetic and from `submitted`
    (Codex review, PR #786: "Inspect sibling orders before accepting a
    successful response")."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    # The finding's own example: the primary validates cleanly.
    assert dca_runner._placement_failure_reason(
        {"data": primary}, expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    ) is None

    halts = dca_runner._accepted_response_names_unaccounted_orders
    assert halts({"data": primary, "orders": [{"id": "B", "state": "queued"}]}) is True
    # One envelope naming two different orders is unattributable here too.
    assert halts({"data": dict(primary, order_id="B")}) is True
    # A bridge failure is not resolved by the primary order having validated.
    assert halts({"isError": True, "data": primary}) is True

    # ... and the ordinary successful shapes must stay cheap and never halt,
    # or every clean placement in the batch stops it.
    assert halts({"data": primary}) is False
    # A wrapper echoing the SAME id at two levels is one order, not two --
    # round 21's over-halt, which this must not reintroduce.
    assert halts({"id": "A", "data": primary}) is False
    assert halts({"success": True}) is False


def test_the_batch_records_primary_then_stops_for_an_id_less_matching_state_sibling():
    """The end-to-end consequence, on the success path.

    Before the fix the ticker landed in `submitted` as a single order and the
    batch carried on, so the second live order was never counted anywhere."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        return {
            "data": {
                "id": f"A-{kw['symbol']}", "state": "queued", "symbol": kw["symbol"],
                "side": "buy", "dollar_amount": kw["dollar_amount"],
                "ref_id": kw["ref_id"],
            },
            # An ID-less sibling whose accepted state matches the primary.
            # This is still a distinct off-chain mapping, not a wrapper echo.
            "orders": [{"state": "queued"}],
        }

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn, enable_live_orders=True, dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    result = dca_runner.execute_plan(
        plan, broker,
        run_id="test-run-2026-07-28", snapshot=snapshot,
        trading_day=date(2026, 7, 28), orders=[], tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=candidates,
    )

    assert result.halted_on == "AAPL"
    # RECORDED, then halted. This originally asserted `submitted == []`, which
    # dropped an order `_placement_failure_reason` had just proven accepted and
    # matching the request -- understating what reached the broker, the same
    # harm this change exists to prevent (Codex review, PR #786: "Record the
    # validated primary order before halting"). The assertion was wrong, not
    # the fix: `halted_on` and a submission for one ticker is the honest
    # reading of "this order went through, and its response described another".
    assert result.submitted == ["AAPL"]
    assert "cannot account for what" in (result.failure_reason or "")
    # Stopped AT the unaccountable response rather than continuing past it.
    assert calls == ["AAPL"]


@pytest.mark.parametrize(
    "sibling",
    [
        {"id": "A-AAPL", "state": "rejected"},
        {"id": "A-AAPL", "state": "filled", "executed_dollar_amount": 100},
        {"id": "A-AAPL", "state": "filled", "executed_notional": 100},
        {"id": "A-AAPL", "state": "filled", "filled_notional": 100},
    ],
)
def test_execute_plan_halts_on_unsafe_same_id_echoes(sibling):
    """The placement loop must enforce same-ID echo checks, not just its helper."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        return {
            "data": {
                "id": f"A-{kw['symbol']}",
                # State is deliberately absent: acceptance validation permits
                # it, but a terminal echo must still stop the batch.
                "symbol": kw["symbol"],
                "side": "buy",
                "dollar_amount": kw["dollar_amount"],
                "ref_id": kw["ref_id"],
            },
            "orders": [sibling],
        }

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], date(2026, 7, 28))
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("MSFT", 2.0)]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)

    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-07-28",
        snapshot=snapshot,
        trading_day=date(2026, 7, 28),
        orders=[],
        tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        now_fn=lambda: datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        slot_id="0935",
        orders_fetched_at=datetime(2026, 7, 28, 9, 43, tzinfo=ET),
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=candidates,
    )

    assert result.submitted == ["AAPL"]
    assert result.halted_on == "AAPL"
    assert "cannot account for what" in (result.failure_reason or "")
    assert calls == ["AAPL"]


def test_an_id_less_sibling_order_halts_a_successful_response():
    """A sibling does not need an ID to be an order.

    Round 41's success-path check counted only usable identities and
    ``isError``, so ``orders: [{"symbol": "MSFT", "state": "filled",
    "executed_dollar_amount": 100}]`` beside a validated AAPL order passed --
    $100 of MSFT spend absent from cap and buying-power accounting while the
    batch continued (Codex review, PR #786: "Inspect ID-less sibling orders on
    successful responses").

    That check's own docstring justified skipping states and fill figures
    because "the validated order is allowed to be queued" -- true of THAT
    order's envelopes, and not a reason to ignore an envelope describing
    something else. Round 29's rule applied to a carve-out of mine: no wider
    than the sentence justifying it."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    # The reported shape: the primary still validates cleanly.
    reported = {
        "data": primary,
        "orders": [{"symbol": "MSFT", "state": "filled", "executed_dollar_amount": 100}],
    }
    assert dca_runner._placement_failure_reason(
        reported, expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    ) is None
    assert halts(reported) is True

    # A sibling reporting spend, or a state meaning an order existed and died
    # (it may have partially filled first), is unaccounted either way.
    assert halts({"data": primary, "orders": [{"executed_dollar_amount": 100}]}) is True
    assert halts({"data": primary, "orders": [{"state": "cancelled"}]}) is True
    # An explicit zero from a sibling rules its own execution out.
    assert halts({"data": primary, "orders": [{"cumulative_quantity": 0}]}) is False


def test_execute_plan_stops_after_an_id_less_filled_sibling():
    """The success-path sibling check must protect the placement loop too."""
    calls = []

    def place_fn(**kw):
        calls.append(kw["symbol"])
        return {
            "data": {
                "id": f"A-{kw['symbol']}",
                "state": "queued",
                "symbol": kw["symbol"],
                "side": "buy",
                "dollar_amount": kw["dollar_amount"],
                "ref_id": kw["ref_id"],
            },
            "orders": [
                {
                    "symbol": "MSFT",
                    "state": "filled",
                    "executed_dollar_amount": 100,
                }
            ],
        }

    broker = dca_runner.build_runner_broker(
        place_fn=place_fn,
        enable_live_orders=True,
        dry_run=False,
        account_number=TEST_ACCOUNT_NUMBER,
    )
    trading_day = date(2026, 8, 26)
    snapshot = dca_runner.budget_snapshot([], trading_day)
    tier = BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate("AAPL", 2.0), dca_runner.Candidate("GOOG", 2.0)]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    now = datetime(2026, 8, 26, 9, 43, tzinfo=ET)

    result = dca_runner.execute_plan(
        plan,
        broker,
        run_id="test-run-2026-08-26",
        snapshot=snapshot,
        trading_day=trading_day,
        orders=[],
        tier=tier,
        orders_account_number=TEST_ACCOUNT_NUMBER,
        now=now,
        now_fn=lambda: now,
        slot_id="0935",
        orders_fetched_at=now,
        buying_power_usd=TEST_BUYING_POWER_USD,
        candidates=candidates,
    )

    assert result.submitted == ["AAPL"]
    assert result.halted_on == "AAPL"
    assert "cannot account for what" in (result.failure_reason or "")
    assert calls == ["AAPL"]


def test_the_validated_order_s_own_envelopes_never_halt_a_success():
    """The dangerous direction, pinned.

    The `response` -> `data` -> `data.order` chain is the validated order's own
    nesting, so a state or fill figure on one of those levels describes THAT
    order. Reading them as evidence of a second order would halt every clean
    placement in the batch -- a worse failure than the one being fixed, and
    the reason this check distinguishes ancestors from siblings at all."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    # An outer envelope carrying the validated order's own fill, or echoing
    # its state -- both ordinary successful shapes.
    assert halts({"executed_dollar_amount": 2.00, "data": primary}) is False
    assert halts({"state": "queued", "data": primary}) is False
    assert halts({"state": "filled", "cumulative_quantity": 1, "data": primary}) is False
    # The plain and deeply-nested successful shapes.
    assert halts({"data": primary}) is False
    assert halts({"data": {"order": primary}}) is False
    # Round 21's over-halt stays fixed: one id echoed at two levels is one
    # order.
    assert halts({"id": "A", "data": primary}) is False


def test_a_sibling_contradicts_the_primary_whatever_spelling_it_uses():
    """A field-by-field comparison never puts two spellings side by side.

    Round 44 taught this check to compare an off-chain same-id envelope
    against the primary on symbol, side and client reference -- but named the
    client reference `ref_id`/`client_order_id` only, while
    `_placement_failure_reason` has accepted `client_order_uuid` and
    `client_id` as the same field since round 18. A sibling naming a DIFFERENT
    reference under either was compared against nothing (Codex review,
    PR #786: "Compare client-reference aliases across echoed envelopes").

    The gap is not confined to the two spellings the report named. Comparing
    field by field means the primary's `ref_id` and the sibling's
    `client_order_id` never meet either -- each loop finds one side empty and
    skips -- so a spelling that IS in the round-44 list still bypassed it. And
    `symbol`/`instrument_symbol` had the identical gap, which the report did
    not mention."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    def response(sibling, over=None):
        return {"data": dict(primary, **(over or {})), "orders": [sibling]}

    # The original P1 regression: attribution by ID must not discard the
    # sibling before its contradictory ticker and unapproved fill are read.
    contradictory_echo = response(
        {"id": "A", "symbol": "MSFT", "state": "queued", "executed_dollar_amount": 100}
    )
    assert dca_runner._placement_failure_reason(
        contradictory_echo,
        expected_symbol="AAPL",
        expected_side="buy",
        expected_amount_usd=2.0,
        expected_ref_id="REF",
    ) is None
    assert halts(
        contradictory_echo,
        expected_symbol="AAPL",
        expected_side="buy",
        expected_amount_usd=2.0,
        expected_ref_id="REF",
    ) is True

    # Every client-reference spelling contradicts, including the one round 44
    # did list -- the primary spells it `ref_id`, so that loop found nothing
    # to compare.
    for key in ("client_order_id", "client_order_uuid", "client_id", "ref_id"):
        reported = response({"id": "A", key: "SOMEONE-ELSES-KEY"})
        assert dca_runner._placement_failure_reason(
            reported, expected_symbol="AAPL", expected_side="buy",
            expected_amount_usd=2.0, expected_ref_id="REF",
        ) is None, key
        assert halts(reported) is True, key

    # ... and in the mirror direction, where the PRIMARY uses the alias.
    assert halts(
        {"data": {"id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
                  "dollar_amount": "2.00", "client_order_uuid": "REF"},
         "orders": [{"id": "A", "ref_id": "SOMEONE-ELSES-KEY"}]}
    ) is True

    # The symbol alias, unreported and identical in kind -- both against the
    # same-id agreement check and against the id-less off-chain symbol check,
    # which read the one key.
    assert halts(response({"id": "A", "instrument_symbol": "MSFT"})) is True
    assert halts(response({"instrument_symbol": "MSFT"})) is True
    assert halts(
        response({"id": "A", "symbol": "MSFT"}, over={"instrument_symbol": "AAPL",
                                                      "symbol": None})
    ) is True


def test_an_executed_figure_above_the_request_is_spend_no_cap_approved():
    """`dollar_amount` is what was REQUESTED; these are what filled.

    A same-id echo reporting its fill through `executed_dollar_amount`,
    `executed_notional` or `filled_notional` slipped past the requested-notional
    comparison, and `attributed` then removed that envelope before
    `_combined_fill_evidence` could see it — so a validated $2 order beside
    `orders: [{"id": "A", "state": "filled", "executed_dollar_amount": 100}]`
    recorded the planned $2 and carried on (Codex review, PR #786: "Validate
    executed notionals in same-ID echoes").

    Compared as a CEILING rather than for equality, which is the whole
    difference from the test above: a figure BELOW the request is an ordinary
    partial or complete fill and must not halt."""
    halts = dca_runner._accepted_response_names_unaccounted_orders
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }

    def check(sibling):
        return halts({"data": dict(primary), "orders": [sibling]}, **request)

    assert dca_runner._EXECUTED_NOTIONAL_KEYS == (
        "executed_dollar_amount", "executed_notional", "filled_notional",
    )
    for field in dca_runner._EXECUTED_NOTIONAL_KEYS:
        assert check({"id": "A", "state": "filled", field: 100}) is True, field
        # A cent over the request is still spend nobody approved.
        assert check({"id": "A", field: 2.02}) is True, field
        # At or below it is an ordinary fill.
        for amount in (2.0, "2.00", 1.5, 0, 0.0, 2.0005):
            assert check({"id": "A", "state": "filled", field: amount}) is False, (field, amount)

    assert check({"id": "A"}) is False
    # CORRECTED, not worked around. Round 51 asserted here that "the ancestor
    # case is untouched — a wrapper reporting the validated order's own fill
    # never reaches this loop", and that assertion encoded the defect: whose
    # order the figure describes is not what makes $100 on a $2 request
    # unaccounted spend. The ancestor is now checked against the REQUEST
    # before any sibling question is asked (Codex review, PR #786: "Validate
    # execution figures on the primary envelope").
    # Exercise every supported execution field both on the selected primary
    # itself and on its wrapper chain.  The original regression covered only
    # one spelling on the wrapper, which could not prevent the primary and the
    # other aliases from drifting back to the off-chain-only validation path.
    for field in dca_runner._EXECUTED_NOTIONAL_KEYS:
        assert halts({"data": {**primary, field: 100}}, **request) is True, field
        assert halts({field: 100, "data": primary}, **request) is True, field
    # ...and the reason the carve-out existed still holds: an ancestor
    # reporting the order's own fill AT OR BELOW the request is the ordinary
    # successful shape and must never halt the batch.
    for field in dca_runner._EXECUTED_NOTIONAL_KEYS:
        for amount in (2.0, "2.00", 1.5, 0, 0.0, 2.0005):
            assert halts({field: amount, "data": primary}, **request) is False, (
                field,
                amount,
            )


def test_a_terminal_echo_contradicts_a_working_order_rather_than_progressing_it():
    """Round 44 excluded state; the exclusion was right for progress and wrong
    for contradiction.

    That round reasoned the order is known accepted, so `queued` beside
    `filled` changes nothing about accountability — true of a wrapper
    reporting an order at two points in its life. An echo of the validated
    order saying `rejected`/`cancelled`/`expired` is not a later stage of
    `queued`; it is the broker denying the acceptance the caps just booked
    (Codex review, PR #786: "Halt on contradictory terminal echoes").

    Deliberately asymmetric — only off-chain DEAD beside on-chain WORKING
    halts — and measured rather than assumed, because the over-halt round 44
    was protecting against is what stops clean placements."""
    halts = dca_runner._accepted_response_names_unaccounted_orders
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }

    def check(sibling, over=None):
        return halts({"data": dict(primary, **(over or {})), "orders": [sibling]}, **request)

    for state in ("rejected", "failed", "voided", "cancelled", "canceled",
                  "expired", " REJECTED "):
        assert check({"id": "A", "state": state}) is True, state

    # PROGRESS, in either direction, is not a contradiction.
    for state in ("new", "queued", "confirmed", "unconfirmed", "partially_filled", "filled"):
        assert check({"id": "A", "state": state}) is False, state
    assert check({"id": "A", "state": "queued"}, {"state": "filled"}) is False
    assert check({"id": "A"}) is False

    # A primary that OMITS state does not excuse the sibling either.
    # `_placement_failure_reason` permits an order mapping with no state, and
    # returning None from it means the order was accepted — so silence reads
    # as accepted-and-not-known-dead, which a `rejected` echo contradicts
    # exactly as it contradicts an explicit `queued` (Codex review, PR #786:
    # "Reject terminal echoes when the primary omits state"). Round 49's
    # lesson, not carried into the check added one round later.
    stateless = {
        "id": "A", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    assert dca_runner._placement_failure_reason(
        dict(stateless), expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    ) is None
    for state in ("rejected", "failed", "voided", "cancelled", "expired"):
        assert halts(
            {"data": dict(stateless), "orders": [{"id": "A", "state": state}]}, **request
        ) is True, state
    for state in ("queued", "filled", "new"):
        assert halts(
            {"data": dict(stateless), "orders": [{"id": "A", "state": state}]}, **request
        ) is False, state
    assert halts({"data": dict(stateless), "orders": [{"id": "A"}]}, **request) is False
    assert halts({"data": dict(stateless)}, **request) is False

    # Ancestors never reach THIS comparison — it needs an off-chain sibling —
    # so a wrapper echoing a WORKING state at an enclosing level is untouched,
    # which is the accept side round 44's exclusion existed to protect.
    assert halts({"state": "queued", "data": primary}, **request) is False
    assert halts({"executed_dollar_amount": 2.0, "data": primary}, **request) is False
    assert halts({"data": primary}, **request) is False
    # CORRECTED, not worked around. Round 50 asserted a `rejected` ancestor was
    # untouched too, sweeping it in with the progress case above — but round
    # 44's exclusion was about PROGRESS, and a dead ancestor over a working
    # primary is the contradiction, not a later stage of it. "No check reaches
    # this" was a true description of the code and a wrong claim about what
    # should happen; the chain now answers it separately from this loop (Codex
    # review, PR #786: "Validate every primary-chain envelope against the
    # request").
    assert halts({"state": "rejected", "data": primary}, **request) is True


def test_an_unrecognized_ancestor_state_is_not_excused():
    """Codex review, PR #786 ("Reject unrecognized states on primary-chain
    ancestors").

    The ancestor chain-state check rejected a NON-STRING state and a
    RECOGNIZED dead one, but a STRING state that is neither -- blank, or a
    word this vocabulary does not define -- passed both tests: `{"state":
    "mystery", "data": {"order": <clean queued inner>}}` and `{"state": "",
    ...}` are both strings (so the non-string check passes them) and
    neither is in `_DEAD_PLACEMENT_STATES` (so the membership test passes
    them too), so both response validators accepted a response whose own
    chain supplied state evidence this classifier could not interpret. This
    is round 62's rule for a same-ID sibling's unrecognized state reaching
    the ancestor chain it did not touch.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(outer_state):
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"state": outer_state, "data": {"order": dict(primary)}}, **request
        )

    assert halts("mystery") is True
    assert halts("") is True
    assert halts("   ") is True
    # Recognized live states are still ordinary progress, unchanged.
    assert halts("queued") is False
    assert halts("filled") is False
    # Recognized dead states already halted before this fix and still do.
    assert halts("rejected") is True
    assert halts("cancelled") is True


def test_a_sibling_is_checked_against_the_request_when_the_primary_is_silent():
    """A primary that says nothing about a field is not a reason to skip it.

    `_placement_failure_reason` only checks the fields a response actually
    supplies, so `{"id": "A", "state": "queued"}` validates for an AAPL buy.
    The agreement loop then found `_primary` empty and `continue`d, leaving a
    sibling naming MSFT -- the broker's ONLY symbol evidence -- compared
    against nothing (Codex review, PR #786: "Validate sibling fields when the
    primary omits them"). The same skip bypassed side, client reference and
    notional.

    The request is the right fallback reference: the failure path has already
    proven the primary matches it on every field the primary DOES carry, so it
    is what the primary would have said had it said anything."""
    halts = dca_runner._accepted_response_names_unaccounted_orders
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    bare = {"id": "A", "state": "queued"}

    def check(sibling):
        return halts({"data": dict(bare), "orders": [sibling]}, **request)

    # The primary validates for an AAPL buy while carrying no symbol at all.
    assert dca_runner._placement_failure_reason(
        dict(bare), expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id=None,
    ) is None

    for sibling in ({"id": "A", "symbol": "MSFT"},
                    {"id": "A", "instrument_symbol": "MSFT"},
                    {"id": "A", "side": "sell"},
                    {"id": "A", "ref_id": "OTHER"},
                    {"id": "A", "client_order_uuid": "OTHER"},
                    {"id": "A", "dollar_amount": "100.00"}):
        assert check(sibling) is True, sibling

    # Pin the review's complete response shape as well as each independent
    # field contradiction above.  In particular, sharing the primary ID must
    # not let a sibling's mutually reinforcing symbol/side/reference/notional
    # evidence pass merely because the primary omitted all four fields.
    assert check({
        "id": "A",
        "symbol": "MSFT",
        "side": "sell",
        "client_order_id": "OTHER",
        "dollar_amount": "100.00",
        "state": "queued",
    }) is True

    # A sibling that AGREES with the request is an echo, and the accept side is
    # where the expensive mistake lives.
    for sibling in ({"id": "A", "symbol": "AAPL"}, {"id": "A", "symbol": "aapl"},
                    {"id": "A", "side": "BUY"}, {"id": "A", "ref_id": "REF"},
                    {"id": "A", "client_order_id": "REF"},
                    {"id": "A", "dollar_amount": "2.00"},
                    {"id": "A", "dollar_amount": 2.0},
                    {"id": "A"}, {"id": "A", "state": "filled"}):
        assert check(sibling) is False, sibling

    # A bare primary with no siblings at all is an ordinary success.
    assert halts({"data": dict(bare)}, **request) is False
    # And when the primary DOES carry the fields, it stays the reference.
    full = {"id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "2.00", "ref_id": "REF"}
    assert halts({"data": full, "orders": [{"id": "A", "symbol": "MSFT"}]}, **request) is True
    assert halts({"data": full, "orders": [{"id": "A", "symbol": "AAPL"}]}, **request) is False
    assert halts({"executed_dollar_amount": 2.0, "data": full}, **request) is False
    # Called without the request at all (the default), behaviour is unchanged.
    assert halts({"data": dict(bare), "orders": [{"id": "A", "symbol": "MSFT"}]}) is False


def test_a_client_reference_is_compared_with_its_case_intact():
    """Pooling made the comparison spelling-blind; it must not make it
    case-blind.

    A ticker or a side is the same whichever case a wrapper echoes it in, so
    folding those absorbs noise. A client reference is an idempotency key --
    `_placement_failure_reason` compares it with an exact `str(...) !=`, and
    this module's generated reference embeds the UPPERCASE ticker. Folding it
    made `...-AAPL` and `...-aapl` compare equal, leaving the success-path
    check quietly weaker than the one that validated the order (Codex review,
    PR #786: "Preserve client-reference case when comparing aliases")."""
    ref = "dca-runner-1530-2026-08-25-AAPL"
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": ref,
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    def response(sibling):
        return {"data": primary, "orders": [sibling]}

    assert halts(response({"id": "A", "client_order_id": ref.lower()})) is True
    assert halts(response({"id": "A", "ref_id": ref.upper()})) is True
    # The same reference, spelled differently or padded, is still an echo.
    assert halts(response({"id": "A", "client_order_id": ref})) is False
    assert halts(response({"id": "A", "ref_id": f"  {ref}  "})) is False
    # Symbol and side stay case-folded -- that is noise, not identity.
    assert halts(response({"id": "A", "symbol": "aapl"})) is False
    assert halts(response({"id": "A", "side": "BUY"})) is False


def test_an_untrackable_positive_sibling_id_halts_a_success():
    """Round 39's rule, reaching the success path.

    An id this batch cannot TRACK is still an id the broker assigned.
    `_usable_order_ids` discards it, so `identities` stays at one, and a
    sibling carrying nothing but that id has no state, symbol or fill for the
    unattributed checks to catch either -- it was invisible to every check on
    this path (Codex review, PR #786: "Halt on untrackable positive IDs in
    successful siblings"). The intersection of two findings already fixed on
    their own sides: round 39 taught the failure path this, round 41 built
    this function without it."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    def response(sibling):
        return {"data": primary, "orders": [sibling]}

    assert halts(response({"id": 10 ** 1000})) is True
    assert halts(response({"id": float("inf")})) is True
    # Confined to POSITIVE values, exactly as on the failure path: 0 and -1
    # are the documented wrapper sentinels for "no id assigned", and halting
    # on them would undo round 21's over-halt fix here as it would there.
    assert halts(response({"id": 0})) is False
    assert halts(response({"id": -1})) is False
    assert halts(response({"id": float("nan")})) is False
    assert halts(response({"id": "A"})) is False


def test_one_order_disagreeing_with_itself_confirms_nothing():
    """The aliases are spellings of ONE field, in the primary order too.

    `_placement_failure_reason` resolves to the first spelling present, so an
    order carrying `ref_id` matching this request beside a `client_order_id`
    naming a different one confirmed the request through the first key while
    the response itself said the two disagree. Round 45 taught the echo check
    to compare aliases across envelopes; this is the same question inside a
    single one (Codex review, PR #786: "Validate every client-reference alias
    in the primary order").

    The finding named the client reference; `symbol`/`instrument_symbol` is
    the same shape and was not mentioned."""
    def reason(order):
        return dca_runner._placement_failure_reason(
            order, expected_symbol="AAPL", expected_side="buy",
            expected_amount_usd=2.0, expected_ref_id="REF",
        )

    base = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    for key in ("client_order_id", "client_order_uuid", "client_id"):
        assert reason(dict(base, **{key: "SOMEONE-ELSES"})) is not None, key
    assert reason(dict(base, instrument_symbol="MSFT")) is not None

    # An order that agrees with itself is unaffected, whichever spellings it
    # uses and however many.
    assert reason(dict(base)) is None
    assert reason(dict(base, client_order_id="REF")) is None
    assert reason(dict(base, instrument_symbol="AAPL")) is None
    assert reason(dict(base, instrument_symbol="aapl")) is None
    assert reason(dict(base, client_order_id=None)) is None
    assert reason({
        "id": "A", "state": "queued", "instrument_symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "client_order_uuid": "REF",
    }) is None
    # The values are request-identifying, so only the count is reported --
    # this string is persisted into ExecutionResult.failure_reason verbatim.
    message = reason(dict(base, client_order_id="SOMEONE-ELSES"))
    assert "SOMEONE-ELSES" not in message and "REF" not in message

    # The failure is ambiguous rather than an isolatable rejection: the
    # usable order id and working state still say that the broker accepted an
    # order, while the disagreeing references mean this run cannot prove it
    # accepted *our* order.  Keep the failure classifier and the batch-halt
    # classifier pinned together for this exact response shape.
    contradictory = dict(base, client_order_id="SOMEONE-ELSES")
    assert dca_runner._placement_response_may_hold_a_live_order(contradictory) is True


def test_a_sibling_spelling_the_same_value_differently_is_still_an_echo():
    """The accept side, which is where the expensive mistake lives.

    Making the comparison blind to spelling must not make it blind to
    agreement: a wrapper reporting the validated order's own reference under
    `client_order_id` while the primary called it `ref_id` is one order
    described twice, and halting on it would stop clean placements. The values
    are pooled across spellings for exactly this reason -- the rule stays "any
    value the sibling names that the primary did not", and only the spelling
    stops mattering."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    def response(sibling):
        return {"data": primary, "orders": [sibling]}

    # Same value, different spelling -- an echo, on every alias.
    for key in ("client_order_id", "client_order_uuid", "client_id"):
        assert halts(response({"id": "A", key: "REF"})) is False, key
    assert halts(response({"id": "A", "instrument_symbol": "AAPL"})) is False
    # Round 44's accept side is untouched.
    assert halts(response({"id": "A"})) is False
    assert halts(response({"id": "A", "state": "filled"})) is False
    assert halts(response({"id": "A", "ref_id": "REF", "symbol": "AAPL"})) is False
    # And the ancestors never reach the comparison at all.
    assert halts({"executed_dollar_amount": 2.00, "data": primary}) is False


def test_both_alias_readers_share_one_list_of_spellings():
    """The drift itself, pinned rather than the symptom.

    Round 45's finding is two copies of one fact -- which keys a broker may
    write a field under -- disagreeing. Re-listing the spellings in a third
    place would set up the same failure again, so both readers take them from
    `_CLIENT_REFERENCE_KEYS`/`_SYMBOL_KEYS` and this asserts the failure path
    still honours every one, in its own priority order."""
    assert dca_runner._CLIENT_REFERENCE_KEYS == (
        "ref_id", "client_order_id", "client_order_uuid", "client_id",
    )
    assert dca_runner._SYMBOL_KEYS == ("symbol", "instrument_symbol")

    for key in dca_runner._CLIENT_REFERENCE_KEYS:
        assert dca_runner._placement_failure_reason(
            {"id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
             "dollar_amount": "2.00", key: "STALE"},
            expected_symbol="AAPL", expected_side="buy",
            expected_amount_usd=2.0, expected_ref_id="REF",
        ) is not None, key
    for key in dca_runner._SYMBOL_KEYS:
        assert dca_runner._placement_failure_reason(
            {"id": "A", "state": "queued", "side": "buy",
             "dollar_amount": "2.00", "ref_id": "REF", key: "MSFT"},
            expected_symbol="AAPL", expected_side="buy",
            expected_amount_usd=2.0, expected_ref_id="REF",
        ) is not None, key
    # Priority is unchanged: the FIRST key present wins, a present null
    # included, so a later alias cannot rescue an explicit null.
    assert dca_runner._placement_failure_reason(
        {"id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
         "dollar_amount": "2.00", "ref_id": None, "client_order_id": "REF"},
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    ) is not None


def test_an_off_chain_sibling_halts_even_when_its_state_matches():
    """A matching state is not an echo once the chain is already excluded.

    The first version of this check excused a sibling whose state equalled the
    validated order's, borrowing the failure path's contradiction test. That
    test is right THERE, where the candidates for "echo" include the enclosing
    wrapper. Here they are not: `unattributed` has already removed every
    mapping in the `response` -> `data` -> `data.order` chain, so an entry
    inside `orders` cannot be the outer-wrapper echo the carve-out was written
    to tolerate (Codex review, PR #786: "Treat off-chain accepted states as
    sibling orders").

    The carve-out borrowed a rule together with a justification the chain
    exclusion had already made false."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    reported = {"data": primary, "orders": [{"state": "queued"}]}
    assert dca_runner._placement_failure_reason(
        reported, expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    ) is None
    assert halts(reported) is True

    # Same for a symbol reported off-chain, matching or not.
    assert halts({"data": primary, "orders": [{"symbol": "AAPL"}]}) is True
    assert halts({"data": primary, "orders": [{"symbol": "MSFT"}]}) is True

    # The ancestors this carve-out was actually protecting are unaffected --
    # they never reach the loop, because the chain exclusion removes them.
    assert halts({"state": "queued", "data": primary}) is False
    assert halts({"executed_dollar_amount": 2.00, "data": primary}) is False
    assert halts({"data": primary}) is False
    # An off-chain entry that rules its own execution out still accepts.
    assert halts({"data": primary, "orders": [{"cumulative_quantity": 0}]}) is False


def test_a_same_id_sibling_that_contradicts_the_primary_halts():
    """Repeating the primary's ID does not make an envelope an echo.

    Grouping by id classified the whole group as attributed and dropped it
    before any sibling check ran, so supplying the primary's ID bypassed every
    one of them: ``orders: [{"id": "A", "symbol": "MSFT", "state": "queued",
    "executed_dollar_amount": 100}]`` beside a validated AAPL order said the
    same broker order may have spent $100 on MSFT, and the batch recorded the
    planned AAPL amount and carried on (Codex review, PR #786: "Inspect
    contradictory same-ID sibling envelopes")."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    reported = {
        "data": primary,
        "orders": [{
            "id": "A", "symbol": "MSFT", "state": "queued",
            "executed_dollar_amount": 100,
        }],
    }
    assert dca_runner._placement_failure_reason(
        reported, expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    ) is None
    assert halts(reported) is True

    # Every identity-or-amount field, not just the symbol.
    assert halts({"data": primary, "orders": [{"id": "A", "dollar_amount": "100.00"}]}) is True
    assert halts({"data": primary, "orders": [{"id": "A", "ref_id": "OTHER"}]}) is True
    assert halts({"data": primary, "orders": [{"id": "A", "side": "sell"}]}) is True


def test_a_same_id_sibling_that_agrees_is_still_an_echo():
    """The accept side, which is what keeps this from halting clean batches.

    A wrapper echoing the created order in an array is the ordinary shape, and
    STATE is deliberately not compared: the failure path halts on disagreeing
    states because there they decide whether the order is dead, while here the
    order is already known accepted, so `queued` beside `filled` changes
    nothing about accountability."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    assert halts({"data": primary, "orders": [{"id": "A"}]}) is False
    assert halts({
        "data": primary,
        "orders": [{
            "id": "A", "symbol": "AAPL", "side": "buy",
            "dollar_amount": "2.00", "ref_id": "REF",
        }],
    }) is False
    # Progress reported at a second level, same order.
    assert halts({
        "data": primary,
        "orders": [{"id": "A", "state": "filled", "cumulative_quantity": 1}],
    }) is False
    # ... and a numerically equal notional in a different format still agrees.
    assert halts({"data": primary, "orders": [{"id": "A", "dollar_amount": 2.0}]}) is False


def test_every_primary_chain_envelope_is_checked_against_the_request():
    """Codex review, PR #786 ("Validate every primary-chain envelope against
    the request").

    `_placement_failure_reason` validates exactly the envelope its descent
    SELECTS and reads none of the levels above it, so an ancestor could
    contradict the request on the requested notional, the ticker, the side or
    the state and both validators still returned a clean success — the batch
    recorded the planned $2, reset the failure streak, and carried on.

    Round 52 fixed the executed-notional half of this same boundary and left
    the rest.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(response):
        return dca_runner._accepted_response_names_unaccounted_orders(response, **request)

    # The selected order is clean in every case below, so the failure path
    # says nothing — which is what left these unexamined.
    for ancestor in (
        {"dollar_amount": 100},
        {"dollar_amount": 2.02},
        {"dollar_amount": True},
        {"dollar_amount": "not-a-number"},
        {"symbol": "MSFT"},
        {"instrument_symbol": "MSFT"},
        {"side": "sell"},
        {"state": "rejected"},
        {"state": "cancelled"},
        {"state": "failed"},
        {"state": {"name": "queued"}},
    ):
        response = dict(ancestor, data=dict(primary))
        assert dca_runner._placement_failure_reason(response, **request) is None, ancestor
        assert halts(response) is True, ancestor

    # ...and the ordinary shapes an ancestor really does carry must not halt,
    # or every clean placement in the batch stops.
    for ancestor in (
        {},
        {"dollar_amount": 2},
        {"dollar_amount": 2.0005},
        {"symbol": "aapl"},            # same ticker, different spelling
        {"side": "BUY"},
        {"state": "queued"},
        {"state": "filled"},           # progress, not contradiction
        {"executed_dollar_amount": 2.0},
        {"success": True, "message": "ok"},
        # A transport wrapper's OWN reference beside the order's `ref_id` is
        # ordinary connector metadata. `client_id` — and ONLY `client_id` —
        # is deliberately not compared against the request here: the sibling
        # check never trips on it either, because there the chain's own values
        # ARE the reference set.
        {"client_id": "TRANSPORT-9"},
        # An ancestor echoing this request's OWN reference is not a second
        # request, whichever spelling it uses.
        {"ref_id": "REF"},
        {"client_order_id": "REF"},
        {"client_order_uuid": "REF"},
    ):
        assert halts(dict(ancestor, data=dict(primary))) is False, ancestor

    # CORRECTED, not worked around. Round 53 asserted `{"ref_id": "OTHER"}`
    # was fine, having exempted the WHOLE `_CLIENT_REFERENCE_KEYS` family
    # because ONE member of it (`client_id`) carries a second meaning. The
    # other three name the order's idempotency request and nothing else, so an
    # ancestor carrying a different one is the response saying it describes two
    # different requests (Codex review, PR #786: "Validate request-reference
    # aliases on ancestor envelopes"). My own rule — a carve-out is exactly as
    # wide as the sentence justifying it — broken in the round that cited it.
    for ancestor in ({"ref_id": "OTHER"}, {"client_order_id": "OTHER"},
                     {"client_order_uuid": "OTHER"},
                     # Compared EXACTLY: an idempotency key, never case-folded.
                     {"ref_id": "ref"}):
        response = dict(ancestor, data=dict(primary))
        assert dca_runner._placement_failure_reason(response, **request) is None, ancestor
        assert halts(response) is True, ancestor

    # Apply the same alias check at every ancestor in the selected chain, not
    # just the outer response envelope. Connector wrappers commonly add an
    # intermediate ``data`` layer before the actual ``order`` object.
    for alias in ("ref_id", "client_order_id", "client_order_uuid"):
        response = {"data": {alias: "OTHER", "order": dict(primary)}}
        assert dca_runner._placement_failure_reason(response, **request) is None, alias
        assert halts(response) is True, alias
    assert halts({"data": {"client_id": "TRANSPORT-9", "order": dict(primary)}}) is False

    # Nesting one level deeper still resolves the same way.
    assert halts({"data": {"order": dict(primary)}}) is False
    assert halts({"state": "rejected", "data": {"order": dict(primary)}}) is True


def test_non_order_metadata_carrying_only_a_symbol_is_not_a_second_order():
    """Codex review, PR #786 ("Ignore non-order symbol metadata when scanning
    siblings").

    The unattributed-symbol rule halted on ANY symbol, so a wrapper carrying
    non-order metadata beside the order — no id, no state, no fill, and the
    REQUEST'S OWN ticker — stopped every remaining ticker in the batch on a
    completely ordinary successful response.

    The sentence justifying that rule was about `{"symbol": "MSFT"}` naming a
    second order, and the rule is now exactly that wide.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(response):
        return dca_runner._accepted_response_names_unaccounted_orders(response, **request)

    for metadata in ({"symbol": "AAPL"}, {"symbol": " aapl "}, {"instrument_symbol": "AAPL"}):
        assert halts({"data": dict(primary), "metadata": metadata}) is False, metadata

    # A DIFFERENT ticker is what the rule was written for and still halts.
    for metadata in ({"symbol": "MSFT"}, {"instrument_symbol": "MSFT"}):
        assert halts({"data": dict(primary), "metadata": metadata}) is True, metadata

    # Nothing is lost by the narrowing: an envelope repeating the request's own
    # symbol WITH real order evidence is still caught, by the state check above
    # it or the fill-evidence check below it.
    assert halts({"data": dict(primary), "orders": [{"symbol": "AAPL", "state": "queued"}]}) is True
    assert halts(
        {"data": dict(primary), "orders": [{"symbol": "AAPL", "executed_dollar_amount": 100}]}
    ) is True

    # With no request symbol to compare against, any symbol still halts — an
    # unverifiable claim is not a cleared one.
    assert dca_runner._accepted_response_names_unaccounted_orders(
        {"data": dict(primary), "metadata": {"symbol": "AAPL"}},
        expected_side="buy", expected_amount_usd=2.0, expected_ref_id="REF",
    ) is True


def test_an_order_bearing_sibling_is_not_excused_by_repeating_the_symbol():
    """Codex review, PR #786 ("Recognize order-bearing siblings that repeat
    the request symbol").

    The claim behind the round-56 narrowing above — that an envelope
    repeating the request's own symbol WITH real order evidence is still
    caught by the state check or the fill-evidence check — was measured
    wrong. `{"symbol": "AAPL", "side": "sell", "dollar_amount": 100,
    "ref_id": "OTHER"}` beside a validated $2 AAPL buy repeats the request's
    own ticker (so the symbol check passes it), carries no `state` key (so
    that check passes it too), and its `dollar_amount` is what was ASKED
    FOR rather than one of the EXECUTED spellings `_fill_evidence` reads —
    so the fill-evidence check at the very end passes it over as well. Both
    response validators then accepted a response plainly describing a
    second, opposite-side order this run never counted.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(sibling):
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"data": dict(primary), "orders": [sibling]}, **request
        )

    # The reported shape: same symbol, opposite side, a different requested
    # notional, and a different order reference — no state, no fill figure.
    assert halts({"symbol": "AAPL", "side": "sell", "dollar_amount": 100, "ref_id": "OTHER"}) is True
    # Each field alone is enough — a genuinely different order need not
    # differ on all three at once.
    assert halts({"symbol": "AAPL", "side": "sell"}) is True
    assert halts({"symbol": "AAPL", "ref_id": "OTHER"}) is True
    assert halts({"symbol": "AAPL", "dollar_amount": 100}) is True
    # A supplied-but-unreadable requested notional is a different answer from
    # an absent one (round 57/59's rule), reaching this new check too.
    assert halts({"symbol": "AAPL", "dollar_amount": "unknown"}) is True
    # Case-insensitive side and instrument_symbol alias, same as the rest of
    # this module's field handling.
    assert halts({"instrument_symbol": "AAPL", "side": "SELL"}) is True

    # CORRECTED one round later: repeating the SAME side/amount/reference
    # alongside the symbol was read as an ordinary echo, not a second order
    # -- but agreement is not attribution without an id, and this is a
    # fully order-shaped entry (four matching fields), not the thin
    # single-field echo `{"symbol": "AAPL"}` stays exempt for (Codex
    # review, PR #786: "Halt on matching ID-less order-list entries").
    assert halts({"symbol": "AAPL", "side": "buy", "dollar_amount": 2, "ref_id": "REF"}) is True
    assert halts({"symbol": "AAPL"}) is False
    # `client_id` stays excluded from the reference aliases here for the same
    # round-53 reason it is excluded on the chain check: it may carry a
    # transport wrapper's own request id rather than the order's.
    assert halts({"symbol": "AAPL", "client_id": "OTHER"}) is False

    # Nothing is lost by the earlier narrowing: an envelope repeating the
    # request's own symbol WITH state or fill-evidence order evidence is
    # still caught by those checks, unchanged.
    assert halts({"symbol": "AAPL", "state": "queued"}) is True
    assert halts({"symbol": "AAPL", "executed_dollar_amount": 100}) is True


def test_a_sibling_naming_two_different_symbol_aliases_is_not_excused():
    """Codex review, PR #786 ("Validate every symbol alias on sibling
    envelopes").

    The unattributed-symbol check resolved to the FIRST of `_SYMBOL_KEYS`
    present via `_first_present_key`, so a self-contradictory sibling
    supplying BOTH aliases with DIFFERENT values -- `{"symbol": "AAPL",
    "instrument_symbol": "MSFT", ...}` beside a validated AAPL buy -- matched
    on `symbol` and the second alias was never read, even though the
    envelope itself disagrees about which ticker it names.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(sibling):
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"data": dict(primary), "orders": [sibling]}, **request
        )

    # Matching `symbol` hid a contradicting `instrument_symbol`.
    assert halts({"symbol": "AAPL", "instrument_symbol": "MSFT"}) is True
    # And the reverse order of contradiction.
    assert halts({"instrument_symbol": "AAPL", "symbol": "MSFT"}) is True
    # Both aliases agreeing (including case-folded) is still an ordinary echo.
    assert halts({"symbol": "AAPL", "instrument_symbol": "aapl"}) is False
    assert halts({"symbol": "AAPL", "instrument_symbol": "AAPL"}) is False
    # Single-alias cases are unchanged from before this fix.
    assert halts({"symbol": "AAPL"}) is False
    assert halts({"instrument_symbol": "AAPL"}) is False
    assert halts({"symbol": "MSFT"}) is True
    assert halts({"instrument_symbol": "MSFT"}) is True
    # A blank alias contributes nothing, exactly as an absent one does.
    assert halts({"symbol": "AAPL", "instrument_symbol": "  "}) is False


def test_an_id_less_sibling_with_an_unrecognized_state_is_not_excused():
    """Codex review, PR #786 ("Reject unreadable states on ID-less sibling
    orders").

    The unattributed-envelope state check only halted on a RECOGNIZED live
    or dead state, so `orders: [{"symbol": "AAPL", "state": "pending"}]`
    beside a validated AAPL buy read as though it said nothing: `pending`
    is in neither `_CANONICAL_LIVE_ORDER_STATES` nor
    `_POST_ACCEPTANCE_TERMINAL_STATES`, its matching symbol passed the next
    check (same symbol, not a differing one), and it carries no
    fill-evidence key either -- so both response validators accepted a
    response that may describe a second, uncounted live order. This is
    round 62's rule for a same-ID sibling's unrecognized state, reaching
    the id-less sibling loop it did not touch.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(sibling):
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"data": dict(primary), "orders": [sibling]}, **request
        )

    # The reported shape: an unrecognized-but-present state word.
    assert halts({"symbol": "AAPL", "state": "pending"}) is True
    assert halts({"symbol": "AAPL", "state": "mystery"}) is True
    # Blank and non-string present values are equally unreadable, not absent.
    assert halts({"symbol": "AAPL", "state": "  "}) is True
    assert halts({"symbol": "AAPL", "state": 123}) is True
    assert halts({"symbol": "AAPL", "state": None}) is True
    # Recognized live and dead states already halted before this fix and
    # still do.
    assert halts({"symbol": "AAPL", "state": "queued"}) is True
    assert halts({"symbol": "AAPL", "state": "cancelled"}) is True
    # ABSENT is the only value that says nothing -- the bare-metadata
    # carve-out (round 56) and an ordinary id-less echo of the SAME order
    # (round 63) both omit `state` entirely and must stay clean.
    assert halts({"symbol": "AAPL"}) is False
    assert halts({}) is False


def test_non_order_metadata_states_reached_via_a_mapping_field_are_ignored():
    """Codex review, PR #786 ("Ignore non-order metadata states when
    scanning siblings").

    The unconditional `"state" in envelope` check above was measured wrong
    in the OPPOSITE direction the very next round: `_response_envelopes`
    walks plain MAPPING fields as well as arrays (round 69), so a validated
    order beside a completely unrelated nested mapping that happens to have
    its own `state` field -- `{"data": <validated order>, "metadata":
    {"state": "healthy"}}`, a service-health block sharing the word "state"
    by coincidence -- landed in `unattributed` and halted the whole batch,
    even though it names no order id, side, reference, notional, or fill
    figure at all.

    An item of a LIST field keeps halting unconditionally regardless of
    other evidence -- `orders: [{"state": "queued"}]` is exactly the
    round-41/62 shape and stays covered, since a list's own presence is
    positional evidence of an order.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(response):
        return dca_runner._accepted_response_names_unaccounted_orders(response, **request)

    # The reported shape: an unrelated mapping FIELD (not a list item)
    # carrying only a `state` key that names no order at all.
    assert halts({"data": dict(primary), "metadata": {"state": "healthy"}}) is False
    assert halts({"data": dict(primary), "metadata": {"state": "pending"}}) is False
    # A mapping field carrying a `state` key AND real order-bearing evidence
    # (a symbol, here) is still exactly round 65's rule -- "presence
    # decides" for evidence this ambiguous.
    assert halts({"data": dict(primary), "metadata": {"symbol": "AAPL", "state": "pending"}}) is True
    assert halts({"data": dict(primary), "metadata": {"side": "sell", "state": "pending"}}) is True
    assert halts({"data": dict(primary), "metadata": {"id": "OTHER", "state": "pending"}}) is True
    # A LIST item with only a bare `state` still halts unconditionally --
    # this is the exact shape round 41/62 were written for and must not
    # regress.
    assert halts({"data": dict(primary), "orders": [{"state": "pending"}]}) is True
    assert halts({"data": dict(primary), "orders": [{"state": "queued"}]}) is True


def test_a_bare_success_flag_on_an_id_less_sibling_order_is_not_excused():
    """Codex review, PR #786 ("Inspect success flags on ID-less sibling
    orders").

    `orders: [{"success": true}]` (the same for `ok`/`approved`) beside a
    validated primary carries no id, no state, no symbol, no side, no
    reference, and no fill-evidence key -- every check the unattributed-
    sibling loop makes passes it over, and the final fill-evidence read
    inspects quantity/notional keys, not boolean flags, so it passed that
    too. The failure-path classifier already reads exactly this vocabulary
    as acceptance evidence for a no-identity response; an off-chain sibling
    on the SUCCESS path deserves the same reading.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(sibling):
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"data": dict(primary), "orders": [sibling]}, **request
        )

    assert halts({"success": True}) is True
    assert halts({"ok": True}) is True
    assert halts({"approved": True}) is True
    # A non-bool truthy value is NOT this signal -- same strictness as the
    # failure-path classifier's own `success`/`ok`/`approved` reads.
    assert halts({"success": "true"}) is False
    assert halts({"success": 1}) is False
    # An explicit False is an ordinary echo of the API call, not evidence of
    # a second order.
    assert halts({"success": False}) is False
    assert halts({}) is False


def test_a_success_flag_reached_via_a_mapping_field_is_ignored():
    """Codex review, PR #786 ("Restrict sibling success flags to
    order-bearing entries").

    The round-70 fix above read `success`/`ok`/`approved` unconditionally
    across every unattributed envelope, including one reached through a
    plain MAPPING field rather than an `orders`-shaped list --
    `{"data": <validated order>, "metadata": {"success": true}}` has no id,
    symbol, side, reference, state, or fill evidence of its own, and is not
    an item of any list either, so it names no order at all. Gated the same
    way as the `state` check: a LIST item's presence is positional evidence
    on its own, but a mapping-field envelope needs real order-bearing
    evidence before its success flag means anything.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(response):
        return dca_runner._accepted_response_names_unaccounted_orders(response, **request)

    # A completely unrelated mapping field naming a success flag but no
    # order evidence at all.
    assert halts({"data": dict(primary), "metadata": {"success": True}}) is False
    assert halts({"data": dict(primary), "metadata": {"ok": True}}) is False
    # The SAME flag, but the mapping field also carries real order-bearing
    # evidence -- round 65's rule for `state` applies here too.
    assert halts({"data": dict(primary), "metadata": {"symbol": "OTHER", "success": True}}) is True
    assert halts({"data": dict(primary), "metadata": {"id": "OTHER", "success": True}}) is True
    # A LIST item with only a bare success flag still halts unconditionally
    # -- this is round 70's own shape and must not regress.
    assert halts({"data": dict(primary), "orders": [{"success": True}]}) is True


def test_a_matching_id_less_order_list_entry_halts_the_batch():
    """Codex review, PR #786 ("Halt on matching ID-less order-list
    entries").

    Every per-field check in the unattributed-sibling loop only halts on a
    DIFFERENCE from the request, so a list-item sibling that repeats every
    field the request specifies passed all of them as an "ordinary echo"
    (round 63) -- but it carries no id tying it to the primary, so field
    agreement is not attribution. See
    `test_an_order_bearing_sibling_is_not_excused_by_repeating_the_symbol`
    for the flip of that round's own test; this test covers the boundary
    of the new rule directly.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(sibling):
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"data": dict(primary), "orders": [sibling]}, **request
        )

    # Two matching fields is already a genuinely order-shaped claim, not a
    # thin echo -- this is not confined to the reported four-field case.
    assert halts({"symbol": "AAPL", "side": "buy"}) is True
    assert halts({"symbol": "AAPL", "ref_id": "REF"}) is True
    assert halts({"symbol": "AAPL", "dollar_amount": 2}) is True
    assert halts({"side": "buy", "ref_id": "REF"}) is True
    # A single matching field stays the thin echo round 56 carved out --
    # unaffected by this rule.
    assert halts({"symbol": "AAPL"}) is False
    assert halts({"side": "buy"}) is False
    assert halts({"symbol": "AAPL", "client_id": "OTHER"}) is False

    # Confined to LIST items: the identical fully-matching evidence reached
    # through a plain MAPPING field carries no positional "this is an
    # order" evidence of its own, so it stays an ordinary echo.
    response = {
        "data": dict(primary),
        "metadata": {"symbol": "AAPL", "side": "buy", "dollar_amount": 2, "ref_id": "REF"},
    }
    assert dca_runner._accepted_response_names_unaccounted_orders(response, **request) is False


def test_a_zero_fill_sibling_with_a_symbol_halts_the_batch():
    """Codex review, PR #786 ("Halt on zero-fill sibling order entries").

    A fill-evidence key was not one of the categories
    `_comparable_evidence_count` counted, so `orders: [{"symbol": "AAPL",
    "filled_quantity": 0}]` -- list position, a matching ticker, AND a
    fill field, but no id -- summed to a single category (symbol alone)
    and stayed under the >=2 threshold. The aggregate fill-evidence read
    at the end of this function reads EXPLICIT ZERO as "rules its own
    execution out," which is the right answer for MONEY, but this is a
    separate, distinct order-shaped entry: a rejected or cancelled second
    buy attempt spends nothing yet must still count toward
    max_orders_per_day and the consecutive-failure breaker.

    A fill key counts as a category alongside symbol/side/ref/amount,
    keeping the SAME >=2 threshold -- not as an independently sufficient
    signal -- so `test_an_id_less_sibling_order_halts_a_successful_
    response`'s and `test_an_off_chain_sibling_halts_even_when_its_state_
    matches`'s own bare `{"cumulative_quantity": 0}` (ruling out only the
    ONE order this response already names) must stay an ordinary,
    non-halting echo.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2, "ref_id": "REF",
    }

    def halts(sibling):
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"data": dict(primary), "orders": [sibling]}, **request
        )

    # The reported shape, and every supported fill-evidence alias.
    assert halts({"symbol": "AAPL", "filled_quantity": 0}) is True
    assert halts({"symbol": "AAPL", "cumulative_quantity": 0}) is True
    assert halts({"symbol": "AAPL", "executed_quantity": 0}) is True
    assert halts({"symbol": "AAPL", "quantity_filled": 0}) is True
    assert halts({"symbol": "AAPL", "executed_notional": 0}) is True
    assert halts({"symbol": "AAPL", "executed_dollar_amount": 0}) is True
    assert halts({"symbol": "AAPL", "filled_notional": 0}) is True
    # Side, ref, or amount plus a fill key are equally two categories.
    assert halts({"side": "buy", "filled_quantity": 0}) is True
    assert halts({"ref_id": "REF", "filled_quantity": 0}) is True
    assert halts({"dollar_amount": 2, "filled_quantity": 0}) is True

    # A BARE fill key alone -- no symbol, no other evidence -- is still
    # exactly round 41's ordinary same-order echo and must not regress.
    assert halts({"cumulative_quantity": 0}) is False
    assert halts({"filled_quantity": 0}) is False


def test_a_cancellation_echo_is_recognized_across_both_spellings():
    """Codex review, PR #786 ("Normalize cancellation aliases before comparing
    echoes").

    `cancelled` and `canceled` are one state spelled two ways — both are in
    `_POST_ACCEPTANCE_TERMINAL_STATES` and `_DEAD_PLACEMENT_STATES`. The
    unattributed-state test compared the raw strings, so a wrapper reporting
    one spelling over a named, explicitly zero-filled order reporting the
    other read as a level describing an order the response never names, and
    halted the whole batch on an ordinary cancellation echo.

    This is the OVER-HALT direction, which costs every remaining ticker its
    buy — the failure this PR exists to prevent.
    """
    def isolates(response):
        return dca_runner._placement_response_may_hold_a_live_order(response) is False

    for outer, inner in (("cancelled", "canceled"), ("canceled", "cancelled"),
                         ("cancelled", "cancelled"), ("canceled", "canceled"),
                         ("CANCELLED", " canceled "), ("expired", "expired")):
        response = {"state": outer,
                    "data": {"order": {"id": "A", "state": inner, "cumulative_quantity": 0}}}
        assert isolates(response), (outer, inner)

    # Folding spellings must not fold DIFFERENT states: `expired` over
    # `cancelled` is still a level describing something the response does not
    # name, and still halts.
    for outer, inner in (("expired", "cancelled"), ("cancelled", "expired")):
        response = {"state": outer,
                    "data": {"order": {"id": "A", "state": inner, "cumulative_quantity": 0}}}
        assert not isolates(response), (outer, inner)

    # ...and the zero-fill rule is untouched: the same echo WITHOUT an explicit
    # zero still halts, because a cancelled order may have partially filled.
    assert not isolates(
        {"state": "cancelled", "data": {"order": {"id": "A", "state": "canceled"}}}
    )

    # The canonicalizer folds only SPELLING, never identity — merging two
    # orders is the unsafe direction, and ids are still compared exactly.
    assert dca_runner._canonical_order_state(" CANCELED ") == "cancelled"
    assert dca_runner._canonical_order_state("cancelled") == "cancelled"
    assert dca_runner._canonical_order_state("expired") == "expired"
    assert dca_runner._canonical_order_state("rejected") == "rejected"
    # Both spellings really are in the sets this test reasons about.
    assert {"cancelled", "canceled"} <= dca_runner._POST_ACCEPTANCE_TERMINAL_STATES
    assert {"cancelled", "canceled"} <= dca_runner._DEAD_PLACEMENT_STATES


_ABSENT = object()


def test_a_supplied_but_unreadable_execution_figure_is_ambiguous():
    """Codex review, PR #786 ("Reject unreadable execution notionals").

    `_reports_executed_notional_above` read each executed spelling through
    `_coerced_float` and skipped on `None`, so a broker that explicitly
    supplied execution evidence this batch cannot read — `true`, `"unknown"`,
    `NaN`, a container — recorded a clean submission and continued. A negative
    figure went the same way, which let a wrapper suppress the whole check by
    reporting `-1`.

    This is the rule the chain's own `dollar_amount` and `state` checks already
    apply — a SUPPLIED but unreadable value cannot be treated like an absent
    one — reaching the executed spellings, which predate it.
    """
    request = dict(
        expected_symbol="AAPL", expected_side="buy",
        expected_amount_usd=2.0, expected_ref_id="REF",
    )
    primary = {
        "id": "A", "state": "filled", "symbol": "AAPL",
        "side": "buy", "dollar_amount": 2.0, "ref_id": "REF",
    }

    def halts(extra, field="executed_notional"):
        order = dict(primary)
        order.update({field: extra} if extra is not _ABSENT else {})
        return dca_runner._accepted_response_names_unaccounted_orders(
            {"data": {"order": order}}, **request
        )

    for field in dca_runner._EXECUTED_NOTIONAL_KEYS:
        # Supplied and unreadable, or supplied and negative: ambiguous.
        for value in (True, False, "unknown", float("nan"), float("inf"),
                      {}, [], -5, "-5", None):
            assert halts(value, field) is True, (field, value)
        # Readable and at or below what the caps approved: an ordinary fill.
        for value in (2.0, 1.5, 0, 0.0, "2.00", 2.0005):
            assert halts(value, field) is False, (field, value)
        # Above it: spend no cap approved, as before.
        assert halts(100.0, field) is True, field

    # ABSENT is not the same as unreadable — silence about the fill is governed
    # by the id branches, not here, and halting on it would stop every ordinary
    # success that simply does not report an executed figure.
    assert halts(_ABSENT) is False
    assert dca_runner._accepted_response_names_unaccounted_orders(
        {"data": {"order": dict(primary)}}, **request
    ) is False


def test_an_unreadable_requested_notional_on_a_same_id_echo_is_not_an_absent_one():
    """Round 57's rule — absent is not the same answer as unreadable — on the
    line directly ABOVE the one round 57 fixed.

    The same-id echo's `dollar_amount` test read `_coerced_float(...) is None`
    as "this envelope did not state an amount", which is true for an absent key
    and false for a present one holding `null`, `true`, `"unknown"` or a
    container. Worse, the envelope is then removed from `unattributed` by the
    identity attribution below, so `_combined_fill_evidence` never sees it
    either: measured, a clean $2 primary beside
    `orders: [{"id": "ORD", "dollar_amount": "unknown"}]` recorded a successful
    $2 submission while the response supplied contradictory amount evidence
    nobody could read (Codex review, PR #786: "Reject unreadable notionals in
    same-ID echoes").

    A `{"amount": ...}` mapping lands here too. It is not parsed as money on
    this path and does not need to be — treating it as unreadable halts, which
    is the safe direction for a placement response, and the reconciliation path
    that DOES have to value such a figure parses it there.
    """
    import dca_runner

    def unaccounted(echo):
        response = {
            "id": "ORD",
            "state": "queued",
            "symbol": "AAPL",
            "side": "buy",
            "dollar_amount": 2.0,
            "orders": [echo],
        }
        return dca_runner._accepted_response_names_unaccounted_orders(
            response,
            expected_symbol="AAPL",
            expected_side="buy",
            expected_ref_id=None,
            expected_amount_usd=2.0,
        )

    # SUPPLIED and unreadable: the response said something about the amount
    # that nobody can read, which cannot establish what the broker accepted.
    for value in (None, True, False, "unknown", "", {}, {"amount": "100"}, [], float("nan")):
        assert unaccounted({"id": "ORD", "dollar_amount": value}) is True, value

    # A readable DISAGREEMENT still halts, as it always did.
    assert unaccounted({"id": "ORD", "dollar_amount": 100}) is True

    # A readable AGREEMENT is an ordinary echo.
    for value in (2.0, "2.00", 2.0005):
        assert unaccounted({"id": "ORD", "dollar_amount": value}) is False, value

    # ABSENT is unchanged and must stay that way: an echo that simply does not
    # restate the amount is ordinary, and halting on it would stop the batch on
    # every wrapper that repeats an id without repeating the figures.
    assert unaccounted({"id": "ORD"}) is False
    assert unaccounted({"id": "ORD", "state": "queued"}) is False


def test_the_sequence_item_budget_is_shared_across_response_arrays():
    """`_MAX_ENVELOPES` is documented as the walk's GLOBAL bound, but the
    per-field sequence scan re-initialized its own item counter every time it
    entered a new array-valued field, so a response with many separately-keyed
    arrays gave each one its own fresh budget rather than sharing one.

    Measured before the fix: 100,000 single-key arrays of 63 scalar items each
    (6.3M items) ran to completion UNTRUNCATED in ~0.66s, synchronously,
    between one placement and the next in a live Gate 7 batch (Codex review,
    PR #786: "Share the traversal budget across response arrays").
    """
    import dca_runner

    hostile: dict[str, Any] = {"success": False}
    for i in range(100_000):
        hostile[f"k{i}"] = list(range(63))

    envelopes, truncated, _ = dca_runner._response_envelopes(hostile, {}, {})
    assert truncated is True, "a shared budget must trip truncation on this shape"

    # Ordinary responses with a handful of small array fields are unaffected:
    # nothing here should ever read as truncated, and a real order nested in
    # one of several small arrays must still be found.
    ordinary = {"a": [1, 2, 3], "b": [4, 5, 6], "orders": [{"id": "ORD", "state": "queued"}]}
    envelopes2, truncated2, _ = dca_runner._response_envelopes(ordinary, {}, {})
    assert truncated2 is False
    assert any(e.get("id") == "ORD" for e in envelopes2)

    # The self-referential-array termination (round 37/38) is unaffected by
    # sharing the counter across fields.
    loop = []
    loop.append(loop)
    envelopes3, truncated3, _ = dca_runner._response_envelopes(
        {"success": False, "results": loop}, {}, {}
    )
    assert truncated3 is False


def test_mapping_field_traversal_shares_the_same_budget_as_arrays():
    """Codex review, PR #786 ("Bound mapping-field traversal as well as
    arrays").

    The round that shared the traversal budget across ARRAY-valued fields
    left plain MAPPING fields uncharged: `for value in parent.values()`
    visits every field of every mapping with no budget check of its own, so
    a single mapping carrying an unbounded number of fields -- scalar or
    dict-valued -- runs past the outer per-DEPTH check entirely before that
    check gets another chance to fire.

    Measured before the fix: 2,000,000 dict-valued fields in one mapping
    took over 3 seconds to walk, synchronously, between one placement call
    and the next in a live Gate 7 batch.
    """
    import time

    import dca_runner

    hostile: dict[str, Any] = {"id": "A", "state": "queued"}
    hostile.update({f"k{i}": {"nested": i} for i in range(2_000_000)})

    start = time.time()
    envelopes, truncated, _ = dca_runner._response_envelopes({"data": hostile}, hostile, hostile)
    elapsed = time.time() - start

    assert truncated is True, "a shared budget must trip truncation on this shape"
    # Generous ceiling for a slow CI runner -- the fix drops this from
    # seconds to microseconds, so anything under a second proves the bound
    # actually engaged rather than merely finishing eventually.
    assert elapsed < 1.0, f"mapping-field traversal took {elapsed:.3f}s -- budget did not engage"

    # A hostile mapping of many SCALAR (non-dict, non-list) fields is bounded
    # the same way, even though none of them are order-bearing evidence.
    scalar_hostile: dict[str, Any] = {"id": "A", "state": "queued"}
    scalar_hostile.update({f"k{i}": i for i in range(5_000_000)})
    start = time.time()
    _, truncated_scalar, _ = dca_runner._response_envelopes(
        {"data": scalar_hostile}, scalar_hostile, scalar_hostile
    )
    elapsed_scalar = time.time() - start
    assert truncated_scalar is True
    assert elapsed_scalar < 1.0, f"scalar field traversal took {elapsed_scalar:.3f}s -- budget did not engage"

    # Ordinary responses with a handful of mapping fields are unaffected.
    ordinary = {
        "data": {"order": {"id": "ORD", "state": "queued", "symbol": "AAPL", "side": "buy"}},
        "metadata": {"latency_ms": 12},
    }
    envelopes2, truncated2, _ = dca_runner._response_envelopes(
        ordinary, ordinary["data"], ordinary["data"]["order"]
    )
    assert truncated2 is False
    assert any(e.get("id") == "ORD" for e in envelopes2)


def test_a_same_id_sibling_with_an_unreadable_state_is_unaccounted():
    """Unlike the recognized terminal echoes above, a same-ID sibling
    supplying a present but blank, non-string, or unrecognized state was
    read as though it said nothing about state at all -- this condition only
    matched a string found in `_DEAD_PLACEMENT_STATES`.

    `orders: [{"id": "A", "state": "mystery"}]` beside a clean `queued`
    primary satisfied both response validators, so `execute_plan` recorded
    the submission and reset the placement-failure streak even though the
    broker supplied state evidence nobody could interpret (Codex review,
    PR #786: "Reject unreadable states in same-ID echoes"). A present value
    outside both the dead and live vocabularies cannot be ruled out as a
    dead-state contradiction, so it is now unaccounted -- the same treatment
    the validated chain's own unreadable state already gets a few lines
    above this one."""
    primary = {
        "id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
        "dollar_amount": "2.00", "ref_id": "REF",
    }
    halts = dca_runner._accepted_response_names_unaccounted_orders

    # PRESENT but unreadable as either a recognized dead or live state: halts.
    for state in ("mystery", "", "   ", None, True, 123, ["rejected"], {"x": 1}):
        reported = {"data": primary, "orders": [{"id": "A", "state": state}]}
        assert halts(reported) is True, state

    # A recognized DEAD state still halts, unchanged.
    assert halts({"data": primary, "orders": [{"id": "A", "state": "rejected"}]}) is True
    # Case-insensitive: the live vocabulary is matched normalized, not raw.
    assert halts({"data": primary, "orders": [{"id": "A", "state": "QUEUED"}]}) is False

    # A recognized LIVE state is ordinary progress and must not halt.
    for state in ("queued", "filled", "confirmed", "new", "unconfirmed", "partially_filled"):
        reported = {"data": primary, "orders": [{"id": "A", "state": state}]}
        assert halts(reported) is False, state

    # ABSENT is unchanged: an echo that simply does not restate the state
    # says nothing, exactly like an echo that omits `dollar_amount`.
    assert halts({"data": primary, "orders": [{"id": "A"}]}) is False
