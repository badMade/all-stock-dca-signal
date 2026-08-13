"""Tests for dca_inverse_martingale: the opt-in deferred-accumulation sizing mode."""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta

import pytest

from dca_inverse_martingale import (
    _MAX_PERSISTED_EVENTS,
    STATUS_DIPPING,
    STATUS_FIRING,
    STATUS_NONE,
    CorruptedLedgerStateError,
    InverseMartingaleLedger,
    evaluate_inverse_martingale_step,
    rolling_stddev,
    stddev_reference_for_ticker,
    trailing_daily_returns,
)

PCT_TIERS = ((1.0, 1.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0))
DAY1 = date(2026, 8, 3)


def _step(state, *, decision_price, previous_close, trading_day=DAY1, tiers=PCT_TIERS,
          unit_usd=1.0, recovery_offset=1.0, max_cycle_days=30, max_fire_attempts=5,
          basis="pct", stddev_reference=None, per_order_cap_usd=None, stream="afternoon"):
    new_state, step = evaluate_inverse_martingale_step(
        state,
        decision_price=decision_price,
        previous_close=previous_close,
        trading_day=trading_day,
        tier_config=tiers,
        unit_usd=unit_usd,
        recovery_offset=recovery_offset,
        max_cycle_days=max_cycle_days,
        max_fire_attempts=max_fire_attempts,
        basis=basis,
        stddev_reference=stddev_reference,
        per_order_cap_usd=per_order_cap_usd,
        stream=stream,
    )
    # Every call here models a whole separate scheduled run, and in production
    # near_close_dca.py's settlement block always clears settlement_pending
    # before the run ends (it is the release point for the lease claim_fire()
    # just wrote -- see the settlement_pending guard in
    # evaluate_inverse_martingale_step and its release in near_close_dca.py's
    # deferred-settlement block). Strip it here too so consecutive _step()
    # calls correctly model consecutive settled runs rather than an
    # unsettled, still-pending claim; the guard itself is covered directly by
    # test_settlement_pending_refuses_a_concurrent_claim_before_settlement
    # below, which calls evaluate_inverse_martingale_step() twice without
    # going through this settlement-simulating wrapper.
    if "settlement_pending" in new_state:
        new_state = {k: v for k, v in new_state.items() if k != "settlement_pending"}
    return new_state, step


# --- pure step evaluation ----------------------------------------------------

def test_no_tier_crossed_is_a_no_op():
    state, step = _step({}, decision_price=99.9, previous_close=100.0)
    assert state == {}
    assert step.status_after == STATUS_NONE
    assert not step.fired


def test_single_tier_crossing_accumulates_and_arms():
    state, step = _step({}, decision_price=99.0, previous_close=100.0)  # -1.0%
    assert step.status_after == STATUS_DIPPING
    assert step.newly_crossed_tiers == (1.0,)
    assert step.accumulated_usd_after == 1.0
    assert state["accumulated_usd"] == 1.0
    assert state["crossed_tier_thresholds"] == [1.0]


def test_gap_down_crosses_multiple_tiers_in_one_step():
    # -5% in one step should credit the 1%, 2%, and 4% tiers cumulatively.
    state, step = _step({}, decision_price=95.0, previous_close=100.0)
    assert step.newly_crossed_tiers == (1.0, 2.0, 4.0)
    assert step.accumulated_usd_after == 1.0 + 2.0 + 4.0


def test_full_descent_matches_users_worked_example():
    # 1%->1u, 2%->+2 (3), 4%->+4 (7), 8%->+8 (15).
    state, step = _step({}, decision_price=91.5, previous_close=100.0)  # -8.5%
    assert step.newly_crossed_tiers == (1.0, 2.0, 4.0, 8.0)
    assert step.accumulated_usd_after == 15.0


def test_an_overflowing_tier_credit_is_refused_not_persisted():
    """Regression test for "Reject overflowing tier credits before
    persistence". A schema-valid but extreme unit_usd/units combination can
    produce a `units * unit_usd` product that overflows to inf --
    round(inf, 2) stays inf, and `inf <= 0` is False, so the sub-cent floor
    never catches it. Persisting an infinite accumulated_usd would make the
    NEXT ledger read raise CorruptedLedgerStateError forever (the finite-
    number check already in _validate_tracker_payload), permanently
    bricking the ticker. This call must instead refuse up front and leave
    the tracker completely unchanged.
    """
    huge_tiers = ((1.0, 1e200),)
    state, step = evaluate_inverse_martingale_step(
        {}, decision_price=99.0, previous_close=100.0, trading_day=DAY1,
        tier_config=huge_tiers, unit_usd=1e200, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct", stddev_reference=None,
    )
    assert state == {}
    assert not step.fired
    assert step.reason == "invalid_tier_credit"
    assert step.status_after == STATUS_NONE
    assert step.accumulated_usd_after == 0.0


def test_already_crossed_tier_is_not_recredited():
    state, step = _step({}, decision_price=99.0, previous_close=100.0)
    state, step = _step(state, decision_price=99.0, previous_close=100.0)
    assert step.newly_crossed_tiers == ()
    assert step.accumulated_usd_after == 1.0


def test_recovery_below_offset_keeps_waiting():
    state, _ = _step({}, decision_price=95.0, previous_close=100.0)  # -5%, crosses 1/2/4
    # Recovers to -4.5% -- only 0.5pt off the low, below the 1.0 offset.
    state, step = _step(state, decision_price=95.5, previous_close=100.0)
    assert step.status_after == STATUS_DIPPING
    assert not step.fired


def test_recovery_at_or_above_offset_fires_full_amount_when_under_cap():
    state, _ = _step({}, decision_price=95.0, previous_close=100.0, trading_day=DAY1)  # low at -5%, accum=7
    state, step = _step(state, decision_price=96.5, previous_close=100.0, trading_day=DAY1)  # -3.5%, recovered 1.5pt
    assert step.fired
    assert step.fire_amount_usd == 7.0
    assert step.accumulated_usd_after == 0.0
    # Fully resolved: no per-cycle fields remain, but a retry-idempotency
    # marker is retained (see test_same_day_retry_after_full_fire_is_a_no_op),
    # along with the same-day re-arm floor (see
    # test_same_day_fire_floor_blocks_a_shallow_rearm_from_a_different_stream).
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": 5.0,
        "fired_floor_price": pytest.approx(95.0),
    }


def test_recovery_gap_past_exact_offset_band_still_fires():
    state, _ = _step({}, decision_price=91.5, previous_close=100.0)  # -8.5%, accum=15
    # Jumps straight from -8.5% to -3% in one step (skips the -7.5% band).
    state, step = _step(state, decision_price=97.0, previous_close=100.0)
    assert step.fired
    assert step.fire_amount_usd == 15.0


def test_fresh_recovery_fire_amount_is_floored_at_the_broker_minimum():
    """Regression test for "Keep sub-dollar fires in the accumulator".

    unit_usd=0.3 with a single 1-unit tier accumulates $0.30 -- the fire
    amount itself must already be floored to the $1.00 broker minimum
    (not just the candidate built from it downstream), with the remainder
    clamped to 0 rather than going negative, so the ledger's own before/
    after accounting stays internally consistent with what actually gets
    submitted.
    """
    small_tiers = ((1.0, 1.0),)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.3)
    assert step.accumulated_usd_after == pytest.approx(0.3)
    state, step = _step(state, decision_price=100.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.3)
    assert step.fired
    assert step.fire_amount_usd == pytest.approx(1.0)
    assert step.accumulated_usd_after == pytest.approx(0.0)
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": pytest.approx(1.0),
        "fired_floor_price": pytest.approx(99.0),
    }


def test_firing_continuation_fire_amount_is_also_floored_at_the_broker_minimum():
    small_tiers = ((1.0, 1.0),)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.3)
    day2 = date(2026, 8, 4)
    # Force a continuation state directly (as if a prior run's per_order_cap
    # had already put this cycle into STATUS_FIRING with a sub-dollar
    # remainder) rather than relying on the fresh-recovery branch, to pin
    # the SAME floor applies in the continuation branch too.
    firing_state = dict(state, status=STATUS_FIRING, fire_attempts=1)
    state, step = _step(firing_state, decision_price=100.0, previous_close=100.0, trading_day=day2,
                         tiers=small_tiers, unit_usd=0.3)
    assert step.fired
    assert step.fire_amount_usd == pytest.approx(1.0)
    assert step.accumulated_usd_after == pytest.approx(0.0)


def test_a_sub_cent_tier_credit_is_floored_to_one_cent_not_zero():
    """Regression test for "Preserve sub-cent tier credits before
    persisting". unit_usd=0.001 with a single 1-unit tier would otherwise
    round(0.0 + 1 * 0.001, 2) to exactly $0.00 -- crediting nothing for a
    tier that just legitimately crossed, and persisting an active "dipping"
    cycle with a $0.00 balance that _validate_tracker_payload's positive-
    balance check (see test_dca_inverse_martingale.py's ledger tests) would
    then permanently refuse to read back. The credit must floor to one cent
    instead, so the cycle stays genuinely positive and readable.
    """
    small_tiers = ((1.0, 1.0),)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.001)
    assert step.newly_crossed_tiers == (1.0,)
    assert step.accumulated_usd_after == pytest.approx(0.01)
    assert state["accumulated_usd"] == pytest.approx(0.01)
    assert state["status"] == STATUS_DIPPING


def test_a_sub_cent_tier_credit_round_trips_through_the_ledger(tmp_path):
    """The floored one-cent credit above must not just be internally
    consistent -- it must actually survive a real ledger write/read cycle
    (the exact path that used to brick on the next evaluation)."""
    small_tiers = ((1.0, 1.0),)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.001)
    ledger = InverseMartingaleLedger(root=tmp_path)
    ledger.record_step("AAPL", state, step)
    reread = ledger.read("AAPL")  # must not raise CorruptedLedgerStateError
    assert reread["status"] == STATUS_DIPPING
    assert reread["accumulated_usd"] == pytest.approx(0.01)


def test_fire_capped_at_per_order_cap_preserves_remainder():
    state, _ = _step({}, decision_price=91.5, previous_close=100.0)  # accum=15
    state, step = _step(state, decision_price=97.0, previous_close=100.0, per_order_cap_usd=10.0)
    assert step.fired
    assert step.fire_amount_usd == 10.0
    assert step.accumulated_usd_after == 5.0
    assert step.status_after == STATUS_FIRING
    assert state["status"] == STATUS_FIRING
    assert state["accumulated_usd"] == 5.0


def test_firing_continuation_places_remainder_on_a_later_run():
    state, _ = _step({}, decision_price=91.5, previous_close=100.0, trading_day=DAY1)
    day2 = date(2026, 8, 4)
    state, _ = _step(state, decision_price=97.0, previous_close=100.0, trading_day=day2, per_order_cap_usd=10.0)
    assert state["status"] == STATUS_FIRING
    # Next run: price movement during the firing state must not change the
    # frozen remainder amount.
    day3 = date(2026, 8, 5)
    state, step = _step(state, decision_price=50.0, previous_close=100.0, trading_day=day3, per_order_cap_usd=10.0)
    assert step.fired
    assert step.fire_amount_usd == 5.0
    assert step.accumulated_usd_after == 0.0
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": day3.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": pytest.approx(8.5),
        "fired_floor_price": pytest.approx(91.5),
    }


def test_settlement_pending_refuses_a_concurrent_claim_before_settlement():
    # A partial fire (accumulated balance exceeds per_order_cap_usd) leaves a
    # freely-claimable STATUS_FIRING remainder, which an overlapping second
    # process could read and claim before the first process's own deferred
    # settlement runs -- silently discarding the difference between the two
    # processes' balance snapshots if the first process's placement then
    # fails and needs to roll back (Codex review: "Reserve partial fires
    # until their placement settles"). Unlike _step() (which simulates a full
    # settled run), call evaluate_inverse_martingale_step() directly here so
    # the settlement_pending flag the claim wrote survives into the second
    # call, modeling exactly that overlap.
    state, _ = _step({}, decision_price=91.5, previous_close=100.0, trading_day=DAY1)
    day2 = date(2026, 8, 4)
    pending_state, claim_step = evaluate_inverse_martingale_step(
        state, decision_price=97.0, previous_close=100.0, trading_day=day2,
        tier_config=PCT_TIERS, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct",
        stddev_reference=None, per_order_cap_usd=10.0,
    )
    assert pending_state["status"] == STATUS_FIRING
    assert pending_state["settlement_pending"] is True

    # A second, concurrent evaluation of the SAME still-pending state (this
    # run's own settlement has not happened yet) must refuse rather than
    # claim the remainder a second time.
    refused_state, refused_step = evaluate_inverse_martingale_step(
        pending_state, decision_price=50.0, previous_close=100.0, trading_day=day2,
        tier_config=PCT_TIERS, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct",
        stddev_reference=None, per_order_cap_usd=10.0,
    )
    assert refused_step.fired is False
    assert refused_step.reason == "settlement_pending"
    assert refused_step.status_before == refused_step.status_after == STATUS_FIRING
    assert refused_step.accumulated_usd_before == refused_step.accumulated_usd_after == claim_step.accumulated_usd_after
    assert refused_state == pending_state


def test_settlement_pending_refuses_a_concurrent_claim_before_settlement_full_fire():
    """Same guarantee as the partial-fire case above, but for a FULL fire
    (the whole accumulated balance fits under per_order_cap_usd, so the
    claim collapses straight to status "none"). That "none" marker used to
    be unleased -- indistinguishable from a genuinely resolved cycle -- so a
    second overlapping process (or, now that "Distinguish later slots from
    retries" lets a different stream past the ordinary same-day guard, a
    genuinely later same-day slot) could read it and start crediting a BRAND
    NEW cycle before the first process's own deferred settlement runs. If
    that first process's placement then failed and needed to roll the full
    balance back, its settlement CAS would find the state already moved and
    silently drop the rollback (Codex review: "Keep full-fire claims leased
    until settlement"). Call evaluate_inverse_martingale_step() directly
    (bypassing _step()'s settlement-simulating strip) so the settlement_
    pending flag the claim wrote survives into the second call, modeling
    exactly that overlap."""
    state, _ = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)  # accum=$1
    day2 = date(2026, 8, 4)
    pending_state, claim_step = evaluate_inverse_martingale_step(
        state, decision_price=100.0, previous_close=100.0, trading_day=day2,
        tier_config=PCT_TIERS, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct",
        stddev_reference=None, per_order_cap_usd=10.0,
    )
    assert claim_step.fired
    assert claim_step.status_after == STATUS_NONE
    assert pending_state["status"] == STATUS_NONE
    assert pending_state["settlement_pending"] is True

    # A second, concurrent evaluation -- even a fresh -5% dip, and even from
    # a DIFFERENT stream than the one that claimed the fire -- must refuse
    # rather than start crediting a new cycle while the claim is unsettled.
    refused_state, refused_step = evaluate_inverse_martingale_step(
        pending_state, decision_price=95.0, previous_close=100.0, trading_day=day2,
        tier_config=PCT_TIERS, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct",
        stddev_reference=None, per_order_cap_usd=10.0, stream="midday",
    )
    assert refused_step.fired is False
    assert refused_step.reason == "settlement_pending"
    assert refused_step.status_before == refused_step.status_after == STATUS_NONE
    assert refused_state == pending_state


def test_settlement_pending_lease_blocks_abandonment_by_max_fire_attempts():
    """Regression test for "Honor settlement leases before abandoning
    attempts". A partial-fire continuation's OWN increment can push
    fire_attempts to (or past) max_fire_attempts in the very same write that
    sets settlement_pending -- e.g. the 5th (of 5 allowed) partial fire,
    still leaving a nonzero remainder. If the abandonment cutoff ran before
    the settlement_pending guard, a second overlapping process reading that
    exact state would see it as an ordinary exhausted-attempts cycle, claim
    a resolved_marker() over it, and resolve a cycle that is really one
    settlement away from being genuinely done -- making the FIRST process's
    own deferred settlement CAS fail against the now-moved state and
    silently dropping whatever it had placed. The pending lease must win.
    """
    pending_state = {
        "status": STATUS_FIRING,
        "accumulated_usd": 5.0,
        "cycle_start_trading_day": DAY1.isoformat(),
        "last_evaluated_trading_day": DAY1.isoformat(),
        "fire_attempts": 5,
        "trading_days_active": 2,
        "settlement_pending": True,
    }
    day2 = date(2026, 8, 4)
    new_state, step = evaluate_inverse_martingale_step(
        pending_state, decision_price=50.0, previous_close=100.0, trading_day=day2,
        tier_config=PCT_TIERS, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct",
        stddev_reference=None, per_order_cap_usd=10.0,
    )
    assert step.reason == "settlement_pending"
    assert step.fired is False
    assert step.status_after == STATUS_FIRING
    assert new_state == pending_state


def test_settlement_pending_lease_blocks_abandonment_by_max_cycle_days():
    """Same ordering guarantee as the max_fire_attempts case above, but for
    the age-based cutoff: a cycle can simultaneously be past max_cycle_days
    AND mid-settlement (a slow-placing final run right at the age boundary).
    The pending lease must still win over age-based abandonment."""
    pending_state = {
        "status": STATUS_FIRING,
        "accumulated_usd": 5.0,
        "cycle_start_trading_day": DAY1.isoformat(),
        "last_evaluated_trading_day": DAY1.isoformat(),
        "fire_attempts": 1,
        "trading_days_active": 31,
        "settlement_pending": True,
    }
    day2 = date(2026, 9, 4)
    new_state, step = evaluate_inverse_martingale_step(
        pending_state, decision_price=50.0, previous_close=100.0, trading_day=day2,
        tier_config=PCT_TIERS, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct",
        stddev_reference=None, per_order_cap_usd=10.0,
    )
    assert step.reason == "settlement_pending"
    assert new_state == pending_state


def test_settlement_pending_lease_blocks_a_basis_change_reset():
    """Regression test for "Honor settlement leases before resetting changed
    configurations". A partial fire can be settlement_pending at the exact
    moment an operator edits the ticker's basis (or tier table) mid-flight
    -- those config-change resets must not run ahead of the pending-lease
    guard either, or a concurrent process reading the same on-disk state
    could claim_fire() a resolved_marker() over a cycle that is still one
    settlement away from actually being done, making the FIRST process's
    own deferred settlement CAS fail against the now-moved state."""
    pending_state = {
        "status": STATUS_FIRING,
        "accumulated_usd": 5.0,
        "basis": "pct",
        "cycle_start_trading_day": DAY1.isoformat(),
        "last_evaluated_trading_day": DAY1.isoformat(),
        "fire_attempts": 1,
        "trading_days_active": 1,
        "settlement_pending": True,
    }
    day2 = date(2026, 8, 4)
    new_state, step = evaluate_inverse_martingale_step(
        pending_state, decision_price=50.0, previous_close=100.0, trading_day=day2,
        tier_config=PCT_TIERS, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="stddev",  # changed from "pct"
        stddev_reference=0.02, per_order_cap_usd=10.0,
    )
    assert step.reason == "settlement_pending"
    assert new_state == pending_state


def test_settlement_pending_lease_blocks_a_tier_config_change_reset():
    """Same guarantee as the basis-change case above, but for an edited
    tier table (which changes the persisted tier_config_fingerprint)."""
    pending_state = {
        "status": STATUS_FIRING,
        "accumulated_usd": 5.0,
        "tier_config_fingerprint": [1.0, [[1.0, 1.0], [2.0, 2.0], [4.0, 4.0], [8.0, 8.0]]],
        "cycle_start_trading_day": DAY1.isoformat(),
        "last_evaluated_trading_day": DAY1.isoformat(),
        "fire_attempts": 1,
        "trading_days_active": 1,
        "settlement_pending": True,
    }
    day2 = date(2026, 8, 4)
    edited_tiers = ((1.1, 1.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0))  # 1.0 -> 1.1
    new_state, step = evaluate_inverse_martingale_step(
        pending_state, decision_price=50.0, previous_close=100.0, trading_day=day2,
        tier_config=edited_tiers, unit_usd=1.0, recovery_offset=1.0,
        max_cycle_days=30, max_fire_attempts=5, basis="pct",
        stddev_reference=None, per_order_cap_usd=10.0,
    )
    assert step.reason == "settlement_pending"
    assert new_state == pending_state


def test_rearm_after_fire_starts_a_fresh_cycle():
    state, _ = _step({}, decision_price=95.0, previous_close=100.0, trading_day=DAY1)
    state, step = _step(state, decision_price=96.5, previous_close=100.0, trading_day=DAY1)
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": pytest.approx(5.0),
        "fired_floor_price": pytest.approx(95.0),
    }
    # A brand-new dip on a LATER day after the reset behaves exactly like a
    # fresh tracker (a same-day rearm is deliberately blocked -- see
    # test_same_day_retry_after_full_fire_is_a_no_op; the fire floor also
    # only ever applies on the SAME trading_day it was recorded).
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day2)
    assert step.status_after == STATUS_DIPPING
    assert step.accumulated_usd_after == 1.0


def test_same_day_retry_after_full_fire_is_a_no_op():
    """A retry of the SAME scheduled run on the SAME trading day, after a
    cycle already fully fired, must not treat the still-depressed price as a
    brand-new dip (Codex review: "Require a fresh dip before rearming a
    fired cycle")."""
    state, _ = _step({}, decision_price=95.0, previous_close=100.0, trading_day=DAY1)
    state, step = _step(state, decision_price=96.5, previous_close=100.0, trading_day=DAY1)
    assert step.fired
    resolved = dict(state)
    # A retry the SAME day, at the SAME still-below-reference price that
    # would otherwise look like a fresh 1% dip, must be a pure no-op.
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    assert step.reason == "already_evaluated_today"
    assert not step.fired
    assert step.newly_crossed_tiers == ()
    assert state == resolved
    # A genuinely LATER day's fresh dip is unaffected.
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day2)
    assert step.status_after == STATUS_DIPPING
    assert step.accumulated_usd_after == 1.0


def test_same_day_fire_floor_blocks_a_shallow_rearm_from_a_different_stream():
    """Regression test for "Require a fresh descent before rearming in a
    later slot". A different stream, same day, is allowed past the
    already_evaluated_today guard (see "Distinguish later slots from
    retries") -- but a shallow dip that never actually falls below the
    fired cycle's own floor (its lowest_drop_seen at fire time) must not be
    treated as a fresh cycle, or the SAME underlying descent gets
    re-credited (and could re-fire) a second time.
    """
    state, _ = _step({}, decision_price=95.0, previous_close=100.0, trading_day=DAY1, stream="morning")
    state, step = _step(state, decision_price=96.5, previous_close=100.0, trading_day=DAY1, stream="morning")
    assert step.fired
    assert state["fired_floor_drop_metric"] == pytest.approx(5.0)  # the cycle's own -5% low
    resolved = dict(state)

    # A DIFFERENT stream, same day, at a SHALLOW -1% dip -- still below
    # prior_close, but nowhere near the -5% floor -- must be a no-op.
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=DAY1, stream="midday")
    assert step.reason == "same_day_fire_floor_not_broken"
    assert not step.fired
    assert step.newly_crossed_tiers == ()
    assert state == resolved

    # A DIFFERENT stream, same day, at a price that genuinely breaks BELOW
    # the floor (-5.5%) is a real new descent and must be tracked.
    state, step = _step(state, decision_price=94.5, previous_close=100.0, trading_day=DAY1, stream="afternoon")
    assert step.status_after == STATUS_DIPPING
    assert step.newly_crossed_tiers == (1.0, 2.0, 4.0)
    assert step.accumulated_usd_after == pytest.approx(7.0)


def test_same_day_rearm_check_uses_the_frozen_reference_price_not_a_fresh_one():
    """Regression test for "Compare the rearm floor in the same price frame"
    (Codex review, third round). ``fired_floor_drop_metric`` is measured
    against the CYCLE's frozen ``cycle_reference_close`` -- but the legacy
    same-day guard re-derived a comparable ``drop_metric`` from THAT call's
    own fresh ``previous_close``, a different reference once the two drift
    apart, which a multi-day cycle's ``previous_close`` naturally does.
    ``fired_floor_price`` fixes this by persisting the floor as an absolute
    PRICE instead, so the same-day guard compares ``decision_price`` to it
    directly, with no reference conversion at all.
    """
    # Day 1: a fresh cycle starts, referenced to a previous_close of 100,
    # and gap-drops straight to 90 (a 10% drop) -- crossing every configured
    # tier (1/2/4/8%) in one evaluation and setting lowest_drop_seen=10.0.
    state, step = _step({}, decision_price=90.0, previous_close=100.0, trading_day=DAY1, stream="morning")
    assert step.newly_crossed_tiers == (1.0, 2.0, 4.0, 8.0)
    assert state["cycle_reference_close"] == pytest.approx(100.0)
    assert state["lowest_drop_seen"] == pytest.approx(10.0)

    # Day 2: price has genuinely risen since (previous_close is now 110, not
    # the cycle's original 100) but sits at 95 -- a 5% drop from the FROZEN
    # cycle_reference_close, which is >= the 1% recovery_offset below the
    # 10% bottom, so the cycle fires its full accumulated balance.
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=95.0, previous_close=110.0, trading_day=day2, stream="morning")
    assert step.fired
    assert step.status_after == STATUS_NONE
    # The floor is the cycle's true bottom (price 90), not the fire price
    # (95) or anything expressed relative to the fresh previous_close=110.
    assert state["fired_floor_drop_metric"] == pytest.approx(10.0)
    assert state["fired_floor_price"] == pytest.approx(90.0)
    resolved = dict(state)

    # A DIFFERENT stream, SAME day, evaluates at the exact fire price (95)
    # again, with the SAME fresh previous_close (110) the fire itself used.
    # Under the old metric-only comparison this would have recomputed
    # drop_metric = (110-95)/110*100 ~= 13.64%, which is GREATER than the
    # stored fired_floor_drop_metric of 10.0 -- so the old "drop_metric <=
    # same_day_fire_floor" check would have read this as having broken
    # BELOW the floor and incorrectly re-armed a fresh cycle at exactly the
    # price the fire already paid for. The price-based comparison correctly
    # recognizes 95 >= fired_floor_price (90) and blocks instead.
    state, step = _step(state, decision_price=95.0, previous_close=110.0, trading_day=day2, stream="midday")
    assert step.reason == "same_day_fire_floor_not_broken"
    assert not step.fired
    assert step.newly_crossed_tiers == ()
    assert state == resolved

    # A price that genuinely breaks BELOW the true floor (90) is a real new
    # descent and must be tracked, even against that same fresh
    # previous_close of 110.
    state, step = _step(state, decision_price=89.0, previous_close=110.0, trading_day=day2, stream="afternoon")
    assert step.status_after == STATUS_DIPPING
    assert step.newly_crossed_tiers == (1.0, 2.0, 4.0, 8.0)


def test_same_stream_retry_of_a_partial_fire_does_not_burn_a_fire_attempt():
    """Regression test for "Preserve partial balances across same-stream
    retries". A partial fire (blocked by per_order_cap_usd, remainder > 0)
    leaves the tracker in STATUS_FIRING -- the pre-existing already_
    evaluated_today guard only covers STATUS_NONE, so a retry of the SAME
    scheduled stream on the SAME trading day previously fell straight into
    the STATUS_FIRING continuation branch, which unconditionally increments
    fire_attempts. Enough same-stream retries (e.g. an external scheduler
    retrying a crashed run) would burn through max_fire_attempts purely
    from repeated evaluations of the SAME logical scheduled slot, and the
    abandonment cutoff would then discard the still-unplaced balance. A
    genuinely DIFFERENT stream the same day must still be able to
    legitimately continue placing the remainder.
    """
    state, _ = _step({}, decision_price=95.0, previous_close=100.0, trading_day=DAY1, stream="afternoon")
    state, step = _step(
        state, decision_price=96.5, previous_close=100.0, trading_day=DAY1,
        per_order_cap_usd=3.0, stream="afternoon",
    )
    assert step.fired
    assert step.status_after == STATUS_FIRING
    assert state["accumulated_usd"] == pytest.approx(4.0)  # 7.0 accumulated - 3.0 placed
    assert state["fire_attempts"] == 1
    assert state["last_evaluated_stream"] == "afternoon"
    resolved = dict(state)

    # A retry of the SAME stream, same day -- must be a pure no-op, not
    # another continuation attempt.
    state, step = _step(
        state, decision_price=96.5, previous_close=100.0, trading_day=DAY1,
        per_order_cap_usd=3.0, stream="afternoon",
    )
    assert step.reason == "already_evaluated_today"
    assert not step.fired
    assert state == resolved
    assert state["fire_attempts"] == 1  # unchanged, not 2

    # A DIFFERENT stream, same day, legitimately continues placing the
    # remaining balance.
    state, step = _step(
        state, decision_price=96.5, previous_close=100.0, trading_day=DAY1,
        per_order_cap_usd=10.0, stream="morning",
    )
    assert step.fired
    assert step.status_after == STATUS_NONE  # cap (10.0) covers the whole 4.0 remainder
    assert state["fired_floor_drop_metric"] == pytest.approx(5.0)


def test_an_age_only_advance_does_not_impersonate_a_real_same_stream_evaluation():
    """Regression test for "Separate age updates from the retry stream
    identity". near_close_dca.py's age-only bookkeeping for a missing-data
    session (_inverse_martingale_advance_age_for_missing_data) must stamp
    last_evaluated_trading_day to keep trading_days_active from double-
    counting on a later same-day real evaluation -- but that means a
    STATUS_FIRING tracker last REALLY evaluated by "afternoon" on an
    EARLIER day carries that stale stream name forward into today's date
    stamp. If "afternoon" is also the first stream with usable data TODAY,
    the STATUS_FIRING same-day guard must not misread that as a retry of a
    stream that never actually touched this ticker today and swallow the
    real continuation -- last_evaluated_is_real=False (what the age-only
    helper stamps, simulated by hand here since that helper itself lives in
    near_close_dca.py) is what tells this guard the date moved from a
    fabricated bump, not a genuine "afternoon" evaluation.
    """
    state, _ = _step({}, decision_price=91.5, previous_close=100.0, trading_day=DAY1, stream="afternoon")
    day2 = date(2026, 8, 4)
    state, step = _step(
        state, decision_price=97.0, previous_close=100.0, trading_day=day2,
        per_order_cap_usd=10.0, stream="afternoon",
    )
    assert step.fired
    assert state["status"] == STATUS_FIRING
    assert state["last_evaluated_stream"] == "afternoon"
    assert state.get("last_evaluated_is_real", True) is True

    # Simulate near_close_dca.py's age-only advance for a day-3 "morning"
    # session with no usable decision data: trading_days_active ticks up,
    # last_evaluated_trading_day moves to today, but last_evaluated_stream
    # is left untouched (still "afternoon", stale from day 2) and
    # last_evaluated_is_real is stamped False.
    day3 = date(2026, 8, 5)
    age_bumped = dict(state)
    age_bumped["trading_days_active"] = state.get("trading_days_active", 0) + 1
    age_bumped["last_evaluated_trading_day"] = day3.isoformat()
    age_bumped["last_evaluated_is_real"] = False

    # The FIRST stream with usable data today happens to be "afternoon" --
    # the same name the stale last_evaluated_stream already carries. Without
    # the last_evaluated_is_real guard this would be misread as a same-
    # stream retry and silently swallowed.
    state, step = _step(
        age_bumped, decision_price=50.0, previous_close=100.0, trading_day=day3,
        per_order_cap_usd=10.0, stream="afternoon",
    )
    assert step.fired
    assert step.reason != "already_evaluated_today"
    assert step.status_after == STATUS_NONE  # the 5.0 remainder fits under the 10.0 cap
    assert step.fire_amount_usd == pytest.approx(5.0)


def test_same_day_abandonment_floor_blocks_an_immediate_rearm_from_a_different_stream():
    """Regression test for "Prevent abandoned cycles from immediately
    rearming". A cycle abandoned via max_cycle_days/max_fire_attempts never
    spent any real money, but a DIFFERENT stream evaluating the SAME trading
    day (allowed past the already_evaluated_today guard -- see "Distinguish
    later slots from retries") must not treat an unchanged, still-below-tier
    price as a brand-new dip: that would re-credit the SAME underlying
    descent and reset trading_days_active/fire_attempts back to zero,
    defeating the very cutoffs abandonment exists to enforce -- the exact
    same hole the fire case's own fired_floor_drop_metric already closes
    (see test_same_day_fire_floor_blocks_a_shallow_rearm_from_a_different_
    stream just above), now reused for abandonment too.
    """
    state, _ = _step({}, decision_price=95.0, previous_close=100.0, trading_day=DAY1, stream="morning")
    day2 = DAY1 + timedelta(days=1)
    state, step = _step(state, decision_price=95.0, previous_close=100.0,
                         trading_day=day2, max_cycle_days=1, stream="morning")
    assert step.reason == "cycle_abandoned_max_age"
    assert state["fired_floor_drop_metric"] == pytest.approx(5.0)  # the cycle's own -5% low
    resolved = dict(state)

    # A DIFFERENT stream, same day, at a SHALLOW -1% dip -- still below
    # prior_close, but nowhere near the -5% floor -- must be a no-op.
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day2, stream="midday")
    assert step.reason == "same_day_fire_floor_not_broken"
    assert not step.fired
    assert step.newly_crossed_tiers == ()
    assert state == resolved

    # A DIFFERENT stream, same day, at a price that genuinely breaks BELOW
    # the floor (-5.5%) is a real new descent and must be tracked.
    state, step = _step(state, decision_price=94.5, previous_close=100.0, trading_day=day2, stream="afternoon")
    assert step.status_after == STATUS_DIPPING
    assert step.newly_crossed_tiers == (1.0, 2.0, 4.0)


def _run_dipping_cycle_for_n_more_trading_days(state, n, *, start_day, max_cycle_days=30):
    """Advance an already-started dipping cycle through N more distinct
    evaluation calls (one per simulated trading day), asserting none of
    them abandon it. Returns (state, last_day)."""
    day = start_day
    for _ in range(n):
        day = day + timedelta(days=1)
        state, step = _step(state, decision_price=99.0, previous_close=100.0,
                             trading_day=day, max_cycle_days=max_cycle_days)
        assert step.reason != "cycle_abandoned_max_age"
    return state, day


def test_same_day_retry_after_abandonment_is_a_no_op():
    # Call 1 (DAY1) starts the cycle -> trading_days_active=1. 29 more
    # distinct-day calls bring it to 30 (not yet abandoned -- max_cycle_days
    # is a strict ">", not ">="). The 31st call abandons it.
    state, _ = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    state, last_day = _run_dipping_cycle_for_n_more_trading_days(state, 29, start_day=DAY1)
    abandon_day = last_day + timedelta(days=1)
    state, step = _step(state, decision_price=99.0, previous_close=100.0,
                         trading_day=abandon_day, max_cycle_days=30)
    assert step.reason == "cycle_abandoned_max_age"
    resolved = dict(state)
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=abandon_day)
    assert step.reason == "already_evaluated_today"
    assert state == resolved


def test_max_cycle_days_abandons_a_stale_dipping_cycle():
    """A cycle active for more than max_cycle_days TRADING days (not
    calendar days) is abandoned -- each distinct evaluation call represents
    one trading day, since evaluation only ever happens on days the
    scheduled stream actually runs (Codex review: "Count cycle age in
    trading days").
    """
    state, _ = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    state, last_day = _run_dipping_cycle_for_n_more_trading_days(state, 29, start_day=DAY1)
    assert state["trading_days_active"] == 30

    abandon_day = last_day + timedelta(days=1)
    state, step = _step(state, decision_price=99.0, previous_close=100.0,
                         trading_day=abandon_day, max_cycle_days=30)
    assert step.reason == "cycle_abandoned_max_age"
    assert step.status_after == STATUS_NONE
    # fired_floor_drop_metric is now preserved on abandonment too (Codex
    # review: "Prevent abandoned cycles from immediately rearming") -- the
    # price never moved from decision_price=99.0/previous_close=100.0
    # throughout this cycle, so both the persisted lowest_drop_seen and this
    # final evaluation's own drop_metric are 1.0.
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": abandon_day.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": 1.0,
        "fired_floor_price": pytest.approx(99.0),
    }


def test_trading_days_active_does_not_double_count_a_second_stream_the_same_day():
    """Regression test for "Count cycle age in trading days". The canonical
    path can evaluate this ticker up to three times on ONE calendar day
    (market-open, midday, and final-afternoon streams all feed the same
    accumulator) -- a second same-day evaluation must not double-count that
    day, or max_cycle_days would trigger roughly 3x too early on a ticker
    enrolled across multiple streams.
    """
    state, _ = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    assert state["trading_days_active"] == 1
    # A second, later-in-the-day evaluation on the SAME trading_day (a
    # different stream's own run).
    state, step = _step(state, decision_price=98.5, previous_close=100.0, trading_day=DAY1)
    assert state["trading_days_active"] == 1  # still 1, not 2
    day2 = DAY1 + timedelta(days=1)
    state, step = _step(state, decision_price=98.0, previous_close=100.0, trading_day=day2)
    assert state["trading_days_active"] == 2  # a genuinely new day increments it


def test_max_cycle_days_is_not_fooled_by_a_calendar_gap_between_evaluations():
    """The old bug: jumping straight from DAY1 to a date months later in
    just TWO evaluation calls must NOT abandon the cycle -- only 2 trading
    days have actually been evaluated, regardless of the calendar gap
    between them (a real deployment evaluates every actual trading day in
    between; skipping evaluations entirely is not something this pure
    function can distinguish from a market holiday, but either way it must
    count EVALUATIONS, not elapsed calendar time).
    """
    state, _ = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    late_day = date(2026, 10, 1)  # months after DAY1 on the calendar
    state, step = _step(state, decision_price=99.0, previous_close=100.0,
                         trading_day=late_day, max_cycle_days=30)
    assert step.reason != "cycle_abandoned_max_age"
    assert state["trading_days_active"] == 2


def test_max_fire_attempts_abandons_a_stuck_firing_cycle():
    state, _ = _step({}, decision_price=91.5, previous_close=100.0, trading_day=DAY1)
    state, _ = _step(state, decision_price=97.0, previous_close=100.0,
                      trading_day=DAY1, per_order_cap_usd=10.0)
    assert state["status"] == STATUS_FIRING
    assert state["fire_attempts"] == 1
    # Keep "placing" $0 (cap=0) so the remainder never exhausts. fire_attempts
    # climbs 1->2->3->4->5 over 4 more continuations; the 5th call finds
    # fire_attempts already at max_fire_attempts and abandons instead of
    # continuing. Use distinct days so the abandonment isn't itself masked by
    # the same-day-retry guard (abandonment only applies to status "firing",
    # which the guard doesn't touch, but distinct days keep this test's
    # intent unambiguous).
    day = DAY1
    for _ in range(5):
        day = date.fromordinal(day.toordinal() + 1)
        state, step = _step(state, decision_price=97.0, previous_close=100.0,
                             trading_day=day, per_order_cap_usd=0.0, max_fire_attempts=5)
    assert step.reason == "cycle_abandoned_max_attempts"
    # fired_floor_drop_metric is now preserved on abandonment too (Codex
    # review: "Prevent abandoned cycles from immediately rearming") -- the
    # cycle's persisted lowest_drop_seen (8.5, from the initial 91.5 dip
    # that triggered the fire) stays deeper than every constant-97.0
    # continuation call's own drop_metric (3.0), so the floor is 8.5.
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": day.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": 8.5,
        "fired_floor_price": pytest.approx(91.5),
    }


def test_stddev_basis_requires_a_reference_and_fails_closed_without_one():
    state, step = _step({}, decision_price=99.0, previous_close=100.0, basis="stddev",
                         stddev_reference=None)
    assert step.reason == "stddev_unavailable"
    assert state == {}


@pytest.mark.parametrize("bad_reference", [float("nan"), float("inf"), float("-inf")])
def test_stddev_basis_rejects_a_non_finite_reference(bad_reference):
    """Regression test for "Missing math.isfinite() Check on
    effective_stddev_reference". NaN compares false against BOTH
    `not effective_stddev_reference` (bool(nan) is True) and `<= 0` (every
    NaN comparison is False), so a NaN reference used to sail straight
    through the old guard and pollute drop_metric = drop_pct / nan with NaN
    instead of failing closed.
    """
    state, step = _step({}, decision_price=99.0, previous_close=100.0, basis="stddev",
                         stddev_reference=bad_reference)
    assert step.reason == "stddev_unavailable"
    assert state == {}


def test_stddev_basis_snapshots_reference_at_cycle_start():
    stddev_tiers = ((1.0, 1.0), (2.0, 2.0))
    # A 1% daily stddev reference: a 2% drop is exactly 2 sigma.
    state, step = _step({}, decision_price=98.0, previous_close=100.0, basis="stddev",
                         tiers=stddev_tiers, stddev_reference=0.01, recovery_offset=0.5)
    assert step.status_after == STATUS_DIPPING
    assert state["stddev_reference"] == 0.01
    # A later call with a DIFFERENT stddev_reference must not move the
    # already-snapshotted value.
    state, step = _step(state, decision_price=97.0, previous_close=100.0, basis="stddev",
                         tiers=stddev_tiers, stddev_reference=0.05, recovery_offset=0.5)
    assert state["stddev_reference"] == 0.01


def test_rejects_a_drop_metric_that_overflows_to_infinity():
    """Regression test for "Reject non-finite derived drop metrics".

    decision_price/previous_close/reference_close are each individually
    required finite (and positive), but that does not bound their
    difference or quotient: an extreme-but-finite decision_price combined
    with a tiny-but-finite previous_close overflows
    `(reference_close - decision_price) / reference_close` past float's max
    representable magnitude, producing a non-finite drop_pct despite every
    individual input passing its own isfinite() check. Left unchecked, an
    active dipping cycle would read `lowest_drop_seen - (-inf)` as
    infinite in the recovery comparison, unconditionally satisfying any
    configured recovery_offset and firing a real synthetic order from
    numerically invalid market data.
    """
    state, step = _step({}, decision_price=1e308, previous_close=1e-300)
    assert step.reason == "invalid_drop_metric"
    assert step.fired is False
    assert state == {}


def test_invalid_previous_close_fails_closed():
    state, step = _step({}, decision_price=10.0, previous_close=0.0)
    assert step.reason == "invalid_previous_close"
    assert not step.fired


@pytest.mark.parametrize("bad_previous_close", [0.0, -5.0, float("inf"), float("-inf"), float("nan")])
def test_non_finite_or_nonpositive_previous_close_fails_closed(bad_previous_close):
    """Regression test extending "invalid_previous_close" to non-finite
    values, not just non-positive ones -- an infinite previous_close (with
    a finite reference_close, since a fresh cycle uses previous_close AS the
    reference) would otherwise make drop_pct/drop_metric go to +/-inf,
    which can make the recovery check trivially true regardless of the real
    accumulated balance's true price history."""
    state, step = _step({}, decision_price=10.0, previous_close=bad_previous_close)
    assert step.reason == "invalid_previous_close"
    assert not step.fired
    assert state == {}


@pytest.mark.parametrize("bad_decision_price", [0.0, -5.0, float("inf"), float("-inf"), float("nan")])
def test_non_finite_or_nonpositive_decision_price_fails_closed(bad_decision_price):
    """Regression test for "Missing Validation for Non-Finite / Non-Positive
    decision_price" (Antigravity review). near_close_dca.py's own caller
    (_inverse_martingale_reconciled_price) already filters this before ever
    calling evaluate_inverse_martingale_step(), but this pure function is
    also called and unit-tested directly with no such external guarantee.
    An infinite decision_price is dangerous, not merely wrong: drop_pct goes
    to -inf (a "price" infinitely above the reference), which makes ANY
    active cycle's recovery check trivially true regardless of the real
    accumulated balance's true price history -- firing a real synthetic
    order off a non-finite input.
    """
    state, step = _step({}, decision_price=bad_decision_price, previous_close=100.0)
    assert step.reason == "invalid_decision_price"
    assert not step.fired
    assert state == {}

    # Also exercised against an ALREADY-DIPPING cycle (a non-fresh call,
    # where a false recovery would actually fire real money) to prove the
    # guard applies regardless of cycle status, not just a brand-new one.
    dipping_state, _ = _step({}, decision_price=95.0, previous_close=100.0)  # -5%, accum=7
    state, step = _step(dipping_state, decision_price=bad_decision_price, previous_close=100.0)
    assert step.reason == "invalid_decision_price"
    assert not step.fired
    assert state == dipping_state


def test_cycle_measured_against_snapshotted_reference_not_daily_previous_close():
    # Day 1: cycle starts at a 5% drop from a 100 reference (price 95).
    state, step = _step({}, decision_price=95.0, previous_close=100.0)
    assert state["cycle_reference_close"] == 100.0
    assert state["lowest_drop_seen"] == 5.0
    # Day 2: price is UNCHANGED at 95, but today's real previous_close is
    # also 95 (yesterday's actual close) -- naively recomputing the drop
    # against today's previous_close would read as 0% (no change), falsely
    # looking like a full recovery. The cycle's real drop from its frozen
    # 100 reference is still 5%, so nothing should register as recovered.
    state, step = _step(state, decision_price=95.0, previous_close=95.0)
    assert state["cycle_reference_close"] == 100.0  # unchanged
    assert step.reason == "awaiting_recovery"
    assert not step.fired
    assert state["lowest_drop_seen"] == 5.0


def test_lowest_drop_seen_updates_even_when_no_new_tier_crosses():
    # Tiers only go up to 4%; a deeper move to 7.9% crosses no NEW tier but
    # must still push the low deeper so a later recovery is measured from
    # the true bottom, not the last tier-crossing point.
    tiers = ((1.0, 1.0), (2.0, 2.0), (4.0, 4.0))
    state, step = _step({}, decision_price=96.0, previous_close=100.0, tiers=tiers)  # -4%, crosses 4%
    assert state["lowest_drop_seen"] == 4.0
    state, step = _step(state, decision_price=92.1, previous_close=100.0, tiers=tiers)  # -7.9%, no new tier
    assert step.reason == "awaiting_recovery"
    assert state["lowest_drop_seen"] == pytest.approx(7.9)
    # Recovers to -6.9% -- exactly 1.0pt off the TRUE 7.9% low -- must fire.
    state, step = _step(state, decision_price=93.1, previous_close=100.0, tiers=tiers, recovery_offset=1.0)
    assert step.fired
    assert step.fire_amount_usd == 7.0  # 1 + 2 + 4


# --- rolling_stddev -----------------------------------------------------------

def test_rolling_stddev_none_when_insufficient_history():
    assert rolling_stddev([0.01, -0.02], window_days=20) is None


def test_rolling_stddev_computes_population_stdev_of_trailing_window():
    returns = [0.01] * 19 + [0.03]
    result = rolling_stddev(returns, window_days=20)
    assert result is not None
    assert result > 0


# --- trailing_daily_returns / stddev_reference_for_ticker ---------------------

def _write_history_csv(history_dir, ticker, rows):
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{ticker}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Date", "Open", "High", "Low", "Close", "Volume"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_trailing_daily_returns_missing_archive_is_empty(tmp_path):
    assert trailing_daily_returns("AAPL", as_of=date(2026, 8, 12), window_days=5, history_dir=tmp_path) == []


def test_trailing_daily_returns_refuses_a_path_traversing_ticker(tmp_path):
    """Regression test for "Missing Symbol & Path Traversal Validation in
    trailing_daily_returns()". Unlike InverseMartingaleLedger._paths(),
    which validates the ticker against _TICKER_SYMBOL_PATTERN before
    joining it into a filesystem path, this function previously joined an
    unvalidated ticker string straight into `history_dir` -- a ticker
    containing ".." could read a file outside the intended ticker-history
    archive. A real, readable CSV is placed exactly at the traversal
    target: before the fix, `ticker="../ESCAPED"` resolves to
    `history_dir.parent / "ESCAPED.csv"` and would have returned real
    (non-empty) data; after the fix, path validation rejects the ticker
    before ever attempting to read, and this must stay `[]`.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    escape_target = tmp_path / "ESCAPED.csv"  # history_dir.parent / "ESCAPED.csv"
    escape_target.write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n2026-08-01,1,1,1,1,1,1\n2026-08-02,1,1,1,2,2,1\n",
        encoding="utf-8",
    )
    assert (
        trailing_daily_returns("../escaped", as_of=date(2026, 8, 12), window_days=1, history_dir=history_dir) == []
    )


@pytest.mark.parametrize("bad_ticker", ["..", "A/../../B", "/etc/passwd", "AAPL/BBB", "AAPL BBB", "AAPL$"])
def test_trailing_daily_returns_refuses_other_invalid_symbols(tmp_path, bad_ticker):
    assert trailing_daily_returns(bad_ticker, as_of=date(2026, 8, 12), window_days=1, history_dir=tmp_path) == []


def test_trailing_daily_returns_excludes_as_of_and_later_rows_no_lookahead(tmp_path):
    rows = [
        {"Date": "2026-08-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
        {"Date": "2026-08-02", "Open": 100, "High": 101, "Low": 99, "Close": 102, "Volume": 1000},
        {"Date": "2026-08-03", "Open": 102, "High": 103, "Low": 101, "Close": 999, "Volume": 1000},  # future
    ]
    _write_history_csv(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 3), window_days=5, history_dir=tmp_path)
    assert returns == pytest.approx([0.02])


def _write_history_csv_with_adj_close(history_dir, ticker, rows):
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{ticker}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_trailing_daily_returns_prefers_adjusted_close_across_a_split(tmp_path):
    """A 2-for-1 split makes raw Close roughly halve between two rows while
    Adj Close stays smooth/continuous -- the return computed from raw Close
    would read as an extreme, spurious ~-50% single-day loss (Codex review:
    "Calculate sigma from adjusted closes")."""
    rows = [
        {"Date": "2026-08-01", "Open": 200, "High": 201, "Low": 199, "Close": 200, "Adj Close": 100, "Volume": 1000},
        {"Date": "2026-08-02", "Open": 101, "High": 102, "Low": 99, "Close": 101, "Adj Close": 101, "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 3), window_days=1, history_dir=tmp_path)
    assert returns == pytest.approx([0.01])  # from Adj Close: (101-100)/100, not (101-200)/200 = -49.5%


def test_trailing_daily_returns_falls_back_to_close_when_adj_close_missing_for_a_row(tmp_path):
    rows = [
        {"Date": "2026-08-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Adj Close": "", "Volume": 1000},
        {"Date": "2026-08-02", "Open": 100, "High": 101, "Low": 99, "Close": 102, "Adj Close": "", "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 3), window_days=1, history_dir=tmp_path)
    assert returns == pytest.approx([0.02])  # falls back to raw Close


@pytest.mark.parametrize("bad_adj_close", ["nan", "inf", "-inf", "0", "-5"])
def test_trailing_daily_returns_falls_back_to_close_when_adj_close_is_populated_but_unusable(tmp_path, bad_adj_close):
    """Regression test for "Fall back from invalid adjusted closes".

    The original fallback only checked for a MISSING/empty Adj Close string
    -- a populated-but-unusable value (NaN, infinite, zero, negative) skipped
    the fallback to Close entirely. NaN and infinity are especially unsafe:
    both compare false against the old "close <= 0" filter, so either would
    have silently entered the return window and contaminated the computed
    sigma instead of being caught and replaced.
    """
    rows = [
        {"Date": "2026-08-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Adj Close": bad_adj_close, "Volume": 1000},
        {"Date": "2026-08-02", "Open": 100, "High": 101, "Low": 99, "Close": 102, "Adj Close": bad_adj_close, "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 3), window_days=1, history_dir=tmp_path)
    assert returns == pytest.approx([0.02])  # falls back to raw Close, not contaminated by the bad Adj Close


def test_trailing_daily_returns_interpolates_within_the_adjusted_series_not_a_raw_splice(tmp_path):
    """Regression test for "Avoid mixing raw and adjusted prices in the
    sigma window". A per-row raw-Close splice (or a forward-filled inferred
    adjustment ratio, an earlier version of this fix) both get this wrong
    exactly AT a split/dividend transition. Row 3 here sits at a 4-for-1
    split with its own Adj Close missing -- raw Close alone would read as a
    fake ~-75% single-day move (104 -> 26); interpolating within the
    adjusted series instead keeps it smooth (~26 -> ~26.5 -> ~27).
    """
    rows = [
        {"Date": "2026-08-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Adj Close": 25, "Volume": 1000},
        {"Date": "2026-08-02", "Open": 104, "High": 105, "Low": 103, "Close": 104, "Adj Close": 26, "Volume": 1000},
        {"Date": "2026-08-03", "Open": 26, "High": 27, "Low": 25, "Close": 26, "Adj Close": "", "Volume": 1000},
        {"Date": "2026-08-04", "Open": 27, "High": 28, "Low": 26, "Close": 27, "Adj Close": 27, "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 5), window_days=3, history_dir=tmp_path)
    assert len(returns) == 3
    assert all(abs(r) < 0.10 for r in returns)  # nowhere near a fake -75%/+300% move


def test_trailing_daily_returns_scales_a_trailing_gap_by_raw_close_movement(tmp_path):
    """Regression test for "Reject trailing adjusted-price gaps instead of
    flattening them". When the archive's own MOST RECENT rows have a valid
    raw Close but a missing Adj Close (a provider gap that hasn't recovered
    as of `as_of`), holding the last known ADJUSTED price flat (an earlier
    version of this fix) turns every affected day's return into exactly
    0%, artificially suppressing the computed sigma -- and since those rows
    still carry a normal raw Close, the staleness check sees a "fresh"
    archive throughout, hiding the problem entirely. Real day-to-day raw
    Close movement (a genuine +5% then +5% run here) must still show up in
    the computed returns, scaled by the last known adjustment ratio.
    """
    rows = [
        {"Date": "2026-08-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Adj Close": 50, "Volume": 1000},
        {"Date": "2026-08-02", "Open": 105, "High": 106, "Low": 104, "Close": 105, "Adj Close": "", "Volume": 1000},
        {"Date": "2026-08-03", "Open": 110, "High": 111, "Low": 109, "Close": 110.25, "Adj Close": "", "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 4), window_days=2, history_dir=tmp_path)
    assert len(returns) == 2
    # Both trailing rows are scaled by the day-0 ratio (50 / 100 == 0.5):
    # row 1 -> 105 * 0.5 == 52.5 (a real +5% move from 50), row 2 -> 110.25
    # * 0.5 == 55.125 (another real +5% move) -- NOT flat/0% returns.
    assert returns[0] == pytest.approx(0.05, abs=1e-6)
    assert returns[1] == pytest.approx(0.05, abs=1e-6)


def test_trailing_daily_returns_refuses_an_overflowing_return(tmp_path):
    """Regression test for "Reject non-finite returns before computing
    sigma". Two individually finite, positive prices (1e-308 and 1e308) --
    both pass _parse_positive_finite's own per-price finiteness/positivity
    check -- can still overflow to +/-inf once divided into a return here.
    Left unchecked, that infinite return reaches rolling_stddev's
    statistics.pstdev(), which raises instead of the documented fail-closed
    None, and near_close_dca.py's injection site does not catch it -- an
    uncaught exception would abort the entire scheduled run over one
    ticker's corrupted history. The whole window must be rejected instead.
    """
    rows = [
        {"Date": "2026-08-01", "Open": 1, "High": 1, "Low": 1, "Close": 1e-308, "Adj Close": 1e-308, "Volume": 1000},
        {"Date": "2026-08-02", "Open": 1, "High": 1, "Low": 1, "Close": 1e308, "Adj Close": 1e308, "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 3), window_days=1, history_dir=tmp_path)
    assert returns == []


def test_trailing_daily_returns_refuses_a_stale_archive(tmp_path):
    """Regression test for "Reject stale history before freezing sigma". An
    archive whose refresh has silently stopped still has plenty of OLD rows
    to satisfy window_days + 1 -- without a staleness check, this would
    freeze an obsolete volatility estimate into a new cycle instead of
    refusing outright.
    """
    rows = [
        {"Date": f"2026-07-{d:02d}", "Open": 100, "High": 101, "Low": 99, "Close": 100 + d, "Volume": 1000}
        for d in range(1, 21)
    ]
    _write_history_csv(tmp_path, "AAPL", rows)
    # 12 days after the archive's last row (2026-07-20) -- well past the
    # staleness budget.
    assert trailing_daily_returns("AAPL", as_of=date(2026, 8, 1), window_days=3, history_dir=tmp_path) == []


def test_trailing_daily_returns_a_stray_future_dated_row_does_not_mask_staleness(tmp_path):
    """Regression test for "Check freshness against the latest eligible
    history row". The staleness check must be judged against the latest
    row actually ELIGIBLE for the return window (strictly before as_of),
    not the archive's single most recent row overall. A stray future-dated
    (or exactly as_of-dated) row -- a data pipeline glitch, a placeholder
    row appended before real backfill completes -- would otherwise make an
    archive that stopped updating months ago look perfectly fresh, while
    the actual return window below still computes entirely from the
    arbitrarily old rows that ARE eligible.
    """
    rows = [
        {"Date": f"2026-01-{d:02d}", "Open": 100, "High": 101, "Low": 99, "Close": 100 + d, "Volume": 1000}
        for d in range(1, 21)
    ]
    # A stray row dated exactly `as_of` -- excluded from the return window
    # (row_date >= as_of), but under the old bug this alone made the whole
    # archive look fresh (gap == 0) regardless of the real archive's age.
    rows.append({"Date": "2026-08-01", "Open": 999, "High": 999, "Low": 999, "Close": 999, "Volume": 1000})
    _write_history_csv(tmp_path, "AAPL", rows)
    assert trailing_daily_returns("AAPL", as_of=date(2026, 8, 1), window_days=3, history_dir=tmp_path) == []


def test_trailing_daily_returns_an_unusable_recent_row_does_not_mask_staleness(tmp_path):
    """Regression test for "Base history freshness on a usable price row".
    The staleness check must be judged against a row that can actually
    CONTRIBUTE a price -- not merely one that is date-eligible. Rows dated
    within the last week here have BOTH Close and Adj Close missing (a
    provider outage that never backfilled), so they are excluded from the
    return computation entirely; the only genuinely usable rows are three
    weeks old, well past the staleness budget. Under the old bug, the mere
    PRESENCE of those recent-but-unusable rows made the archive look fresh
    (their date was recorded as "latest eligible"), while sigma would have
    been computed entirely from the stale three-week-old prices.
    """
    rows = [
        {"Date": f"2026-07-{d:02d}", "Open": 100, "High": 101, "Low": 99, "Close": 100 + d, "Adj Close": 100 + d, "Volume": 1000}
        for d in range(1, 6)  # usable, but 26+ days before as_of -- stale
    ]
    rows += [
        # Within the 7-day staleness budget, but neither price field is
        # usable -- must NOT count as "the latest eligible row".
        {"Date": "2026-07-27", "Open": 100, "High": 101, "Low": 99, "Close": "", "Adj Close": "", "Volume": 1000},
        {"Date": "2026-07-30", "Open": 100, "High": 101, "Low": 99, "Close": "", "Adj Close": "", "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    assert trailing_daily_returns("AAPL", as_of=date(2026, 8, 1), window_days=3, history_dir=tmp_path) == []


def test_trailing_daily_returns_accepts_an_archive_within_the_staleness_budget(tmp_path):
    rows = [
        {"Date": f"2026-07-{d:02d}", "Open": 100, "High": 101, "Low": 99, "Close": 100 + d, "Volume": 1000}
        for d in range(1, 21)
    ]
    _write_history_csv(tmp_path, "AAPL", rows)
    # Exactly at the boundary (7 days after the archive's last row).
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 7, 27), window_days=3, history_dir=tmp_path)
    assert len(returns) == 3


def test_trailing_daily_returns_skips_a_row_when_both_close_columns_are_unusable(tmp_path):
    rows = [
        {"Date": "2026-08-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Adj Close": 100, "Volume": 1000},
        {"Date": "2026-08-02", "Open": 100, "High": 101, "Low": 99, "Close": "nan", "Adj Close": "nan", "Volume": 1000},
        {"Date": "2026-08-03", "Open": 100, "High": 101, "Low": 99, "Close": 105, "Adj Close": 105, "Volume": 1000},
    ]
    _write_history_csv_with_adj_close(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 8, 4), window_days=5, history_dir=tmp_path)
    # The unusable middle row is skipped entirely -- one return, from the
    # two surviving rows (100 -> 105), not a NaN-contaminated pair.
    assert returns == pytest.approx([0.05])


def test_trailing_daily_returns_keeps_only_the_trailing_window(tmp_path):
    rows = [
        {"Date": f"2026-07-{d:02d}", "Open": 100, "High": 101, "Low": 99, "Close": 100 + d, "Volume": 1000}
        for d in range(1, 21)
    ]
    _write_history_csv(tmp_path, "AAPL", rows)
    # as_of is within the staleness budget of the archive's last row
    # (2026-07-20) -- a wider gap would trip the staleness refusal this
    # function ALSO enforces, which is exercised separately below.
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 7, 25), window_days=3, history_dir=tmp_path)
    assert len(returns) == 3


def test_trailing_daily_returns_deduplicates_a_repeated_date_keeping_the_last_row(tmp_path):
    """Regression test for "Deduplicate dates before computing the
    volatility window". A duplicate Date (a bad archive merge) must not be
    treated as two consecutive trading sessions -- that manufactures an
    artificial same-day "return" which consumes a real window_days slot and
    skews the snapshotted sigma. The LAST row for a repeated date wins,
    matching ml-forecaster/features.py's load_history_csv() convention for
    the identical scenario.
    """
    rows = [
        {"Date": "2026-07-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
        {"Date": "2026-07-02", "Open": 100, "High": 101, "Low": 99, "Close": 102, "Volume": 1000},
        # A duplicate 2026-07-02 row (bad merge) with a wildly different
        # close -- the FIRST 2026-07-02 row (102) must be discarded, not
        # treated as its own extra trading day.
        {"Date": "2026-07-02", "Open": 100, "High": 101, "Low": 99, "Close": 150, "Volume": 1000},
        {"Date": "2026-07-03", "Open": 100, "High": 101, "Low": 99, "Close": 151, "Volume": 1000},
    ]
    _write_history_csv(tmp_path, "AAPL", rows)
    returns = trailing_daily_returns("AAPL", as_of=date(2026, 7, 4), window_days=5, history_dir=tmp_path)

    # Exactly 2 returns from 3 distinct trading days (07-01, 07-02, 07-03),
    # not 3 returns from 4 rows -- the duplicate 07-02 row never contributes
    # its own artificial "return".
    assert len(returns) == 2
    assert returns[0] == pytest.approx((150.0 - 100.0) / 100.0)  # 07-01 -> 07-02 (LAST 07-02 row: 150)
    assert returns[1] == pytest.approx((151.0 - 150.0) / 150.0)  # 07-02 -> 07-03


def test_stddev_reference_for_ticker_none_when_history_too_short(tmp_path):
    rows = [{"Date": "2026-08-01", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}]
    _write_history_csv(tmp_path, "AAPL", rows)
    assert stddev_reference_for_ticker("AAPL", as_of=date(2026, 8, 12), window_days=20, history_dir=tmp_path) is None


def test_stddev_reference_for_ticker_computes_from_archive(tmp_path):
    import datetime as _dt
    closes = [100, 101, 99, 102, 98, 103]
    rows = [
        {"Date": (_dt.date(2026, 7, 1) + _dt.timedelta(days=i)).isoformat(),
         "Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000}
        for i, c in enumerate(closes)
    ]
    _write_history_csv(tmp_path, "AAPL", rows)
    ref = stddev_reference_for_ticker("AAPL", as_of=date(2026, 7, 10), window_days=5, history_dir=tmp_path)
    assert ref is not None
    assert ref > 0


# --- InverseMartingaleLedger ---------------------------------------------------

def test_ledger_read_returns_empty_for_unknown_ticker(tmp_path):
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("AAPL") == {}


def test_ledger_round_trips_state_and_events(tmp_path):
    ledger = InverseMartingaleLedger(root=tmp_path)
    state, step = _step({}, decision_price=99.0, previous_close=100.0)
    ledger.record_step("aapl", state, step)
    reread = ledger.read("AAPL")
    assert reread["accumulated_usd"] == 1.0
    events = ledger.events("AAPL")
    assert len(events) == 1
    assert events[0]["reason"] == "tier_crossed"


def test_ledger_retains_a_retry_idempotency_marker_after_full_resolution(tmp_path):
    ledger = InverseMartingaleLedger(root=tmp_path)
    state, step = _step({}, decision_price=95.0, previous_close=100.0)
    ledger.record_step("MSFT", state, step)
    state, step = _step(state, decision_price=96.5, previous_close=100.0)
    ledger.record_step("MSFT", state, step)  # fires fully, resolves to a marker
    reread = ledger.read("MSFT")
    assert reread["status"] == STATUS_NONE
    assert reread["last_evaluated_trading_day"] == DAY1.isoformat()
    assert (tmp_path / "MSFT" / "tracker.json").exists()


def test_ledger_raises_on_corrupt_json_instead_of_resetting(tmp_path):
    """Regression test for "Refuse unreadable accumulator state instead of
    resetting it". A truncated/malformed tracker.json must NOT be treated
    the same as a genuinely absent one ({}) -- evaluation would otherwise
    start a brand-new cycle from that {} and its own compare-and-swap write
    would overwrite the damaged file, permanently discarding whatever
    dollar balance it held."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    (ticker_dir / "tracker.json").write_text("{not json", encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")
    # The damaged file itself is left completely untouched for recovery.
    assert (ticker_dir / "tracker.json").read_text(encoding="utf-8") == "{not json"


def test_ledger_raises_on_invalid_utf8_instead_of_crashing_the_caller(tmp_path):
    """Regression test for "Convert invalid UTF-8 tracker data into ledger
    corruption". Invalid UTF-8 bytes (disk corruption, a bad write) make
    read_text(encoding="utf-8") raise UnicodeDecodeError -- previously
    uncaught here, propagating past the JSON-parsing try/except that
    already converts a malformed-JSON failure correctly. The injection site
    catches only CorruptedLedgerStateError around read(), so this crashed
    the entire scheduled run over one ticker's damaged file instead of the
    documented "skip only this ticker" behavior.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    (ticker_dir / "tracker.json").write_bytes(b"\xff\xfe\x00invalid utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")
    # The damaged file itself is left completely untouched for recovery.
    assert (ticker_dir / "tracker.json").read_bytes() == b"\xff\xfe\x00invalid utf-8"


def test_ledger_raises_on_a_duplicate_keyed_tracker_file(tmp_path):
    """Regression test for "Reject duplicate keys in tracker JSON". A
    restored or externally edited tracker.json with two "accumulated_usd"
    entries -- plain json.loads() would silently keep only the last
    (999.0) -- must be rejected as corrupted instead of trusting whichever
    value happened to come last, which could inflate a real fire. Every
    OTHER required field for an active "dipping" tracker is present and
    individually valid, so this exercises the duplicate-key rejection
    itself, not an unrelated missing-field/malformed-value refusal.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    (ticker_dir / "tracker.json").write_text(
        '{"version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 1.0, '
        '"accumulated_usd": 999.0, "crossed_tier_thresholds": [1.0, 2.0, 4.0], '
        '"cycle_reference_close": 100.0, "lowest_drop_seen": 4.0, '
        f'"cycle_start_trading_day": "{DAY1.isoformat()}", "trading_days_active": 1}}',
        encoding="utf-8",
    )
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_write_locked_tolerates_an_unreadable_existing_file_when_preserving_events(tmp_path):
    """Regression test for "Exception Handling Consistency in
    InverseMartingaleLedger._write_locked()". _write_locked's own read of
    the EXISTING file (to preserve its prior audit events) previously
    caught only (FileNotFoundError, JSONDecodeError) -- inconsistent with
    _read_locked's broader (OSError, UnicodeDecodeError) handling. Unlike
    _read_locked (which must fail closed since it hands back trusted state
    for real financial decisions), this is only best-effort event-history
    preservation: an unreadable existing file must not block the write
    itself, just fall back to an empty prior event list.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    (ticker_dir / "tracker.json").write_bytes(b"\xff\xfe\x00invalid utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    ledger.record_step("GME", state, step)  # must not raise
    written = json.loads((ticker_dir / "tracker.json").read_text(encoding="utf-8"))
    assert written["status"] == STATUS_DIPPING
    assert len(written["events"]) == 1  # prior (unreadable) history discarded, not preserved or crashed on


def test_ledger_raises_on_unsupported_version_instead_of_resetting(tmp_path):
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    original = json.dumps({"version": 999, "status": "dipping"})
    (ticker_dir / "tracker.json").write_text(original, encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")
    assert (ticker_dir / "tracker.json").read_text(encoding="utf-8") == original


def test_ledger_raises_when_the_file_is_not_a_json_object(tmp_path):
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    (ticker_dir / "tracker.json").write_text("[1, 2, 3]", encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_still_returns_empty_dict_for_a_genuinely_absent_file(tmp_path):
    """A missing tracker.json (no cycle has ever started) is NOT corruption
    -- it must stay the documented {} no-op, not raise."""
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME") == {}


def test_ledger_raises_when_a_tracker_file_is_misplaced_into_the_wrong_ticker_directory(tmp_path):
    """Regression test for "Validate the tracker ticker before using its
    balance". A tracker.json copied/restored into the wrong ticker's
    directory (an operator mistake, a bad backup/restore script) must not
    be silently trusted as that directory's own state -- the directory
    alone previously decided which ticker a file's balance belonged to, so
    a misplaced file could be evaluated against the WRONG ticker's prices
    and fire the SOURCE ticker's real accumulated balance as an order for a
    completely different security.
    """
    ledger = InverseMartingaleLedger(root=tmp_path)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    ledger.record_step("AAPL", state, step)

    # Simulate the file being copied into MSFT's directory by mistake --
    # its own payload still says "ticker": "AAPL" (record_step() always
    # stamps the symbol it was written for into the payload).
    aapl_path = tmp_path / "AAPL" / "tracker.json"
    misft_dir = tmp_path / "MSFT"
    misft_dir.mkdir()
    (misft_dir / "tracker.json").write_text(aapl_path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("MSFT")

    # AAPL's own (correctly-placed) file is completely unaffected.
    assert ledger.read("AAPL")["status"] == STATUS_DIPPING


@pytest.mark.parametrize(
    "malformed_ticker",
    ["../../../tmp/evil", "AAPL/../../etc", "/etc/cron.d/evil", "..", "AAPL/../BBB", ""],
)
def test_ledger_rejects_a_ticker_outside_the_symbol_pattern(tmp_path, malformed_ticker):
    """Regression test for "Constrain ledger paths to valid ticker symbols".
    A ticker containing path separators (or otherwise outside a real
    symbol's alphabet) must never reach Path construction -- self.root /
    symbol would otherwise let such a string escape the intended ledger
    root entirely, creating or overwriting a tracker.json anywhere on disk.
    Reachable via a malformed inverse_martingale.json config key or an
    unvalidated alternate DCA_CONFIG_DIR's tickers.json, neither of which
    this loader itself constrains to a real ticker-symbol pattern.
    """
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read(malformed_ticker)
    with pytest.raises(CorruptedLedgerStateError):
        state, step = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
        ledger.record_step(malformed_ticker, state, step)
    # Nothing was written anywhere outside (or inside) the intended root.
    assert list(tmp_path.rglob("tracker.json")) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "not_a_real_status"),
        ("accumulated_usd", "corrupt"),
        ("accumulated_usd", float("nan")),
        ("cycle_reference_close", "corrupt"),
        ("stddev_reference", "corrupt"),
        ("lowest_drop_seen", "corrupt"),
        ("fire_attempts", "corrupt"),
        ("fire_attempts", 3.5),
        ("fire_attempts", -1),
        ("trading_days_active", "corrupt"),
        ("trading_days_active", -1000),
        ("crossed_tier_thresholds", "not_a_list"),
        ("crossed_tier_thresholds", [1.0, "corrupt"]),
        ("crossed_tier_thresholds", [1.0, float("nan")]),
        ("crossed_tier_thresholds", [1.0, float("inf")]),
        ("fired_floor_drop_metric", "corrupt"),
        ("fired_floor_drop_metric", float("nan")),
        ("fired_floor_drop_metric", float("inf")),
        ("basis", "corrupt"),
        ("basis", "PCT"),  # wrong case -- the real constant is lowercase "pct"
        ("basis", 123),
    ],
)
def test_ledger_raises_on_a_malformed_field_instead_of_crashing_the_caller(tmp_path, field, value):
    """Regression test for "Validate ledger payload fields before returning".
    A syntactically valid tracker.json (right envelope/version/ticker) with a
    corrupted per-cycle field must raise CorruptedLedgerStateError from
    read() itself -- the near_close_dca.py injection site catches only that
    exception around read(), so previously a bad field type here (e.g.
    accumulated_usd: "corrupt") passed this check and instead blew up deep
    inside evaluate_inverse_martingale_step() (float("corrupt")), well
    outside that narrow try/except, aborting the entire scheduled run over
    one ticker's damaged state.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 5.0}
    payload[field] = value
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["dipping", "firing"])
@pytest.mark.parametrize("accumulated_usd", [0.0, -5.0, None])
def test_ledger_raises_on_a_nonpositive_balance_in_an_active_status(tmp_path, status, accumulated_usd):
    """Regression test for "Reject nonpositive balances in active tracker
    states". A "dipping"/"firing" tracker with a zero, negative, or absent
    accumulated_usd is syntactically valid (passes the type/finiteness
    checks above) but semantically impossible -- no legitimate code path
    ever leaves an active cycle with a nonpositive balance. Left unchecked,
    evaluate_inverse_martingale_step's STATUS_FIRING branch floors ANY
    accumulated_usd (including zero or negative) up to the broker minimum
    and fires it unconditionally, so a corrupted or manually edited tracker
    left in this state would produce a real, entirely unearned purchase on
    its very next evaluation instead of being refused here.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": status}
    if accumulated_usd is not None:
        payload["accumulated_usd"] = accumulated_usd
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["none", None])
@pytest.mark.parametrize("stale_accumulated_usd", [1000.0, 0.01, -5.0])
def test_ledger_raises_on_a_nonzero_balance_in_a_resolved_status(tmp_path, status, stale_accumulated_usd):
    """Regression test for "Reject balances on resolved tracker states". A
    resolved ("none") or brand-new (no status key at all) tracker must
    never carry a nonzero accumulated_usd -- resolved_marker() omits the
    key entirely, and a genuinely fresh tracker starts from {} with nothing
    persisted yet. Left unchecked, a restored or manually damaged tracker
    that keeps a STALE balance despite a non-active status passed this
    validation silently and was picked up as the STARTING balance the very
    next time a tier crossed (evaluate_inverse_martingale_step's own
    `accumulated_before = float(tracker_state.get("accumulated_usd", 0.0))`
    reads it unconditionally, regardless of status) -- inflating every
    subsequent capped fire by the leftover stale amount instead of starting
    a genuinely fresh cycle.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "accumulated_usd": stale_accumulated_usd}
    if status is not None:
        payload["status"] = status
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["none", None])
def test_ledger_accepts_a_zero_or_absent_balance_in_a_resolved_status(tmp_path, status):
    """The mirror image of the test above: a resolved/fresh tracker with NO
    accumulated_usd at all, or an explicit 0.0, is exactly what every
    legitimate code path produces and must be accepted -- only a genuinely
    nonzero leftover value is corruption."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "accumulated_usd": 0.0}
    if status is not None:
        payload["status"] = status
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    ledger.read("GME")  # must not raise

    ticker_dir2 = tmp_path / "AAPL"
    ticker_dir2.mkdir()
    payload_no_key = {"version": 1, "ticker": "AAPL"}
    if status is not None:
        payload_no_key["status"] = status
    (ticker_dir2 / "tracker.json").write_text(json.dumps(payload_no_key), encoding="utf-8")
    ledger.read("AAPL")  # must not raise either


@pytest.mark.parametrize("status", ["none", None])
@pytest.mark.parametrize(
    "stale_field,stale_value",
    [
        ("cycle_reference_close", 100.0),
        ("lowest_drop_seen", 4.0),
        ("crossed_tier_thresholds", [1.0, 2.0]),
        ("cycle_start_trading_day", "2026-08-03"),
        ("stddev_reference", 0.02),
        ("tier_config_fingerprint", [1.0, [[1.0, 1.0]]]),
        ("basis", "pct"),
    ],
)
def test_ledger_raises_on_cycle_only_metadata_in_a_resolved_status(tmp_path, status, stale_field, stale_value):
    """Regression test for "Reject cycle snapshots on resolved tracker
    states". A resolved ("none") or brand-new tracker must never carry any
    cycle-only field -- resolved_marker() persists only status/
    last_evaluated_trading_day/last_evaluated_stream/fired_floor_drop_metric/
    fired_floor_price, and a genuinely fresh tracker starts from {}. Left
    unchecked, a restored or manually edited tracker that keeps a STALE
    value for one of these fields would silently survive into the next
    evaluation: evaluate_inverse_martingale_step's new-cycle bootstrap uses
    `new_state.setdefault(...)` for every one of them, which only fills in a
    MISSING key -- so the stale leftover value (e.g. an old
    cycle_reference_close) would win over the freshly computed one the new
    cycle is supposed to start from, corrupting every drop_pct/tier-crossing/
    recovery computation for that cycle's entire lifetime.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", stale_field: stale_value}
    if status is not None:
        payload["status"] = status
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["none", None])
@pytest.mark.parametrize("field", ["fire_attempts", "trading_days_active"])
def test_ledger_raises_on_a_nonzero_age_counter_in_a_resolved_status(tmp_path, status, field):
    """Regression test for "Reject cycle-age counters on resolved
    trackers". resolved_marker() omits fire_attempts/trading_days_active
    entirely, just like accumulated_usd -- a restored tracker that keeps a
    STALE nonzero counter despite a non-active status would otherwise
    survive into the next tier crossing's brand-new cycle and abandon its
    newly-accumulated balance almost immediately, since the next evaluation
    compares that stale value against max_cycle_days/max_fire_attempts.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", field: 30}
    if status is not None:
        payload["status"] = status
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("bad_value", [0.0, -50.0])
def test_ledger_raises_on_a_nonpositive_cycle_reference_close(tmp_path, bad_value):
    """Regression test for "Reject nonpositive cycle reference closes at
    ledger load". A merely-finite check let a value of 0 or negative
    through -- every SUBSEQUENT evaluation would then hit evaluate_inverse_
    martingale_step's own reference_close validity guard and return
    "invalid_reference_close" before ever reaching the age/attempt
    abandonment cutoffs, freezing the balance indefinitely instead of
    eventually abandoning it.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": bad_value, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(),
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["dipping", "firing"])
@pytest.mark.parametrize("bad_value", [0.0, -3.0])
def test_ledger_raises_on_a_nonpositive_lowest_drop_seen(tmp_path, status, bad_value):
    """Regression test for "Reject nonpositive active-cycle lows". A
    merely-finite check let a value of 0 or negative through for an ACTIVE
    (dipping/firing) tracker, even though lowest_drop_seen is only ever set
    once at least one positive configured tier threshold has been crossed.
    A restored STATUS_FIRING tracker with a nonpositive value here would
    settle normally, and its own continuation branch copies this value
    straight into fired_floor_drop_metric -- which the dedicated
    fired_floor_drop_metric positivity check would then reject on the NEXT
    ledger read, permanently skipping the enrolled ticker.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": status, "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": bad_value,
        "cycle_start_trading_day": DAY1.isoformat(),
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("field", ["accumulated_usd", "cycle_reference_close", "lowest_drop_seen", "fired_floor_drop_metric"])
def test_ledger_raises_on_an_oversized_integer_field_instead_of_crashing(tmp_path, field):
    """Regression test for "Treat oversized ledger integers as corruption".

    json.loads() parses an arbitrarily large JSON integer into an unbounded
    Python int -- it passes the isinstance(value, (int, float)) check in
    _validate_tracker_payload()'s per-field loop, but math.isfinite() itself
    raises OverflowError trying to convert an out-of-float-range int, rather
    than returning False. Previously that OverflowError escaped the function
    uncaught instead of becoming CorruptedLedgerStateError, so the per-ticker
    recovery boundary in near_close_dca.py's injection site never caught it
    and the whole scheduled run aborted over one ticker's damaged tracker.
    """
    huge_int = 10**400
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(),
    }
    payload[field] = huge_int
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("bad_value", [0.0, -5.0])
def test_ledger_raises_on_a_nonpositive_fired_floor_drop_metric(tmp_path, bad_value):
    """Regression test for "Reject nonpositive persisted fire-floor
    metrics". Every legitimately produced fired_floor_drop_metric comes
    from a crossed tier's own drop_metric, which can only reach the
    tracker once it already met or exceeded some positive configured
    threshold. A restored resolved marker with a nonpositive value here
    (and no fired_floor_price, the explicitly supported legacy shape) would
    let the same-day re-arm guard's metric-based fallback compare a fresh
    POSITIVE drop_metric against this floor and always conclude it was
    broken, immediately re-crediting tiers from a descent that already
    fired or was abandoned.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "none",
        "last_evaluated_trading_day": DAY1.isoformat(), "last_evaluated_stream": "afternoon",
        "fired_floor_drop_metric": bad_value,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("field", ["fired_floor_drop_metric", "fired_floor_price"])
def test_ledger_raises_on_fire_floor_metadata_missing_its_evaluation_date(tmp_path, field):
    """Regression test for "Require a date for persisted fire floors".
    Every legitimate fired_floor_drop_metric/fired_floor_price is written
    by resolved_marker() together with last_evaluated_trading_day, in the
    SAME call. A restored or truncated marker that keeps a valid floor but
    LOSES last_evaluated_trading_day would otherwise pass validation, and
    the same-day re-arm guard's own `tracker_state.get(
    "last_evaluated_trading_day") == trading_day.isoformat()` check can
    then never match today's date -- silently disabling the floor
    protection, so a later stream evaluating the SAME calendar day would
    treat an unchanged price still within the already-covered range as a
    genuinely fresh dip, re-crediting tiers a real fire already paid for.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "none",
        "last_evaluated_stream": "afternoon",
        field: 5.0,
        # deliberately no "last_evaluated_trading_day"
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_accepts_fire_floor_metadata_with_its_evaluation_date_present(tmp_path):
    """The legitimate shape -- both fields present together -- must remain accepted."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "none",
        "last_evaluated_trading_day": DAY1.isoformat(), "last_evaluated_stream": "afternoon",
        "fired_floor_drop_metric": 5.0, "fired_floor_price": 91.5,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    state = ledger.read("GME")
    assert state["fired_floor_drop_metric"] == pytest.approx(5.0)
    assert state["fired_floor_price"] == pytest.approx(91.5)


def test_ledger_raises_on_a_crossed_tier_not_in_the_persisted_fingerprint(tmp_path):
    """Regression test for "Validate crossed tiers against the persisted
    tier table". A crossed_tier_thresholds value that isn't one of the
    fingerprint's own real thresholds doesn't shadow anything: the next
    evaluation's tier-crossing loop iterates the CURRENT config's own real
    tiers and checks `threshold not in crossed_before` -- so every real
    tier still reads as "not yet crossed" and gets credited a second time
    on top of the existing balance.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [3.0],  # not a real threshold in the fingerprint below
        "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(),
        "tier_config_fingerprint": [1.0, [[1.0, 1.0], [2.0, 2.0], [4.0, 4.0]]],
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_raises_on_a_duplicate_crossed_tier(tmp_path):
    """A crossed_tier_thresholds value repeated twice can never legitimately
    happen -- crossed_before is built from a set."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 1.0, 2.0],
        "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(),
        "tier_config_fingerprint": [1.0, [[1.0, 1.0], [2.0, 2.0], [4.0, 4.0]]],
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_accepts_crossed_tiers_that_are_a_genuine_subset_of_the_fingerprint(tmp_path):
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 3.0,
        "crossed_tier_thresholds": [1.0, 2.0],
        "cycle_reference_close": 100.0, "lowest_drop_seen": 2.0,
        "cycle_start_trading_day": DAY1.isoformat(), "trading_days_active": 2,
        "tier_config_fingerprint": [1.0, [[1.0, 1.0], [2.0, 2.0], [4.0, 4.0]]],
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    ledger.read("GME")  # must not raise


@pytest.mark.parametrize(
    "missing_field", ["crossed_tier_thresholds", "cycle_reference_close", "lowest_drop_seen"]
)
def test_ledger_raises_on_a_dipping_tracker_missing_a_required_cycle_field(tmp_path, missing_field):
    """Regression test for "Require complete snapshots for active
    trackers". A "dipping" tracker with a positive balance but missing
    crossed_tier_thresholds, cycle_reference_close, or lowest_drop_seen is
    syntactically valid but never legitimately produced -- every real cycle
    sets all three the moment it starts and only ever carries them forward.
    Left unchecked, each is individually "handled" by silently
    reconstructing from CURRENT prices instead of refusing: most
    dangerously, a missing crossed_tier_thresholds makes the next
    evaluation re-credit every already-crossed tier a second time (a real
    $7 balance at tiers 1/2/4 becomes $14).
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
    }
    del payload[missing_field]
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("missing_field", ["cycle_reference_close", "lowest_drop_seen"])
def test_ledger_raises_on_a_firing_tracker_missing_a_required_cycle_field(tmp_path, missing_field):
    """Regression test for "Require the fire-floor metadata on firing
    trackers". A "firing" tracker with a positive balance but missing
    cycle_reference_close or lowest_drop_seen is syntactically valid but
    never legitimately produced -- both are set the moment a cycle first
    enters "dipping" and carried forward unchanged into "firing", never
    dropped. Left unchecked: a missing cycle_reference_close silently falls
    back to TODAY's previous_close for the drop_pct computation (the
    `reference_close = ... tracker_state.get("cycle_reference_close",
    previous_close)` fallback applies to ANY active status, not just
    "dipping"); a missing lowest_drop_seen means the STATUS_FIRING
    continuation branch's own `resolved_marker(..., fired_floor_drop_
    metric=tracker_state.get("lowest_drop_seen"))` call -- reached when the
    fire fully resolves -- persists NO fired_floor_drop_metric at all,
    silently reopening the "a different stream later the same day
    re-credits the same tiers at an unchanged price" hole that field exists
    to close.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "firing", "accumulated_usd": 4.0,
        "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(), "fire_attempts": 1, "trading_days_active": 1,
    }
    del payload[missing_field]
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["dipping", "firing"])
def test_ledger_raises_on_an_active_tracker_missing_trading_days_active(tmp_path, status):
    """Regression test for "Require age counters on active trackers".
    evaluate_inverse_martingale_step unconditionally sets trading_days_active
    for every real dipping/firing evaluation -- a restored tracker missing
    it is not "handled" by the int(tracker_state.get(..., 0)) fallback: that
    silently restarts the age counter at zero even if cycle_start_trading_day
    is months old, granting the stale balance another full max_cycle_days
    budget instead of abandoning it on schedule.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": status, "accumulated_usd": 4.0,
        "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(),
    }
    if status == "dipping":
        payload["crossed_tier_thresholds"] = [1.0, 2.0, 4.0]
    else:
        payload["fire_attempts"] = 1
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_raises_on_a_firing_tracker_missing_fire_attempts(tmp_path):
    """Regression test for "Require age counters on active trackers". A
    "firing" tracker missing fire_attempts is not "handled" by the
    int(tracker_state.get(..., 0)) fallback either: it silently regains the
    full max_fire_attempts retry budget it may have already exhausted. Not
    required for "dipping" -- fire_attempts is only ever initialized the
    moment a cycle first enters "firing".
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "firing", "accumulated_usd": 4.0,
        "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(), "trading_days_active": 3,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_accepts_a_firing_tracker_with_no_crossed_tier_thresholds_at_all(tmp_path):
    """Unlike "dipping", a "firing" tracker's own evaluation branch never
    reads crossed_tier_thresholds at all (it returns before the
    tier-crossing loop that field feeds), so its absence on an otherwise
    complete "firing" tracker must NOT be treated as corruption."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "firing", "accumulated_usd": 4.0,
        "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(), "fire_attempts": 1, "trading_days_active": 1,
        # no "crossed_tier_thresholds" key at all
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["status"] == STATUS_FIRING


@pytest.mark.parametrize("status", ["dipping", "firing"])
@pytest.mark.parametrize("bad_stddev_reference", [None, 0.0, -0.01])
def test_ledger_raises_on_an_active_stddev_cycle_missing_its_frozen_sigma(tmp_path, status, bad_stddev_reference):
    """Regression test for "Require the frozen sigma for active stddev
    cycles". evaluate_inverse_martingale_step deliberately ignores the
    caller's freshly computed sigma for an active cycle and reads only the
    PERSISTED stddev_reference -- a "dipping"/"firing" tracker can never
    legitimately reach either active status with basis="stddev" and no
    valid stddev_reference in the first place (both are set together the
    moment a stddev-basis cycle first becomes "dipping"). Left unchecked,
    such a tracker hits the stddev_unavailable refusal on EVERY future
    evaluation, before this function ever reaches the age/attempt
    abandonment cutoffs -- the cycle can never age out, never resolve, and
    never fire, while enrollment keeps suppressing the ticker's normal
    ladder indefinitely instead of failing closed at load time.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": status, "accumulated_usd": 4.0,
        "basis": "stddev", "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(), "fire_attempts": 1, "trading_days_active": 1,
    }
    if status == "dipping":
        payload["crossed_tier_thresholds"] = [1.0]
    if bad_stddev_reference is not None:
        payload["stddev_reference"] = bad_stddev_reference
    # else: no "stddev_reference" key at all
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["dipping", "firing"])
def test_ledger_accepts_an_active_stddev_cycle_with_a_valid_frozen_sigma(tmp_path, status):
    """The mirror image of the test above: a "dipping"/"firing" tracker
    with basis="stddev" and a genuinely positive, finite stddev_reference is
    exactly what every legitimate stddev-basis cycle looks like and must be
    accepted."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": status, "accumulated_usd": 4.0,
        "basis": "stddev", "stddev_reference": 0.02, "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(), "fire_attempts": 1, "trading_days_active": 1,
    }
    if status == "dipping":
        payload["crossed_tier_thresholds"] = [1.0]
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["status"] == status


def test_ledger_raises_on_a_dipping_tracker_with_empty_crossed_tier_thresholds(tmp_path):
    """An empty list is just as impossible as a missing key for a dipping
    tracker with a positive balance -- a real cycle cannot accumulate
    dollars without crossing at least one tier."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["dipping", "firing"])
def test_ledger_raises_on_an_active_tracker_missing_cycle_start_trading_day(tmp_path, status):
    """Regression test for "Require cycle-start metadata for active
    trackers". A legitimate tracker in EITHER active status always carries
    cycle_start_trading_day -- it is written together with
    cycle_reference_close/lowest_drop_seen the moment a cycle first enters
    "dipping" and carried forward unchanged into "firing" -- so a restored
    or manually damaged tracker missing only this field is never a
    legitimate state. Left unchecked, evaluate_inverse_martingale_step's own
    max_cycle_days/max_fire_attempts abandonment check is gated on this
    field being truthy, even though trading_days_active/fire_attempts are
    tracked independently and would enforce the cutoff correctly on their
    own -- such a tracker would bypass both cutoffs indefinitely and still
    fire a real, unbounded-age purchase whenever recovery happens to occur.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": status, "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "fire_attempts": 1, "trading_days_active": 3,
        # no "cycle_start_trading_day" key at all
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["dipping", "firing"])
@pytest.mark.parametrize("bad_value", ["not-a-date", "2026-02-30", "", 20260201, 20260201.0])
def test_ledger_raises_on_a_malformed_cycle_start_trading_day(tmp_path, status, bad_value):
    """Regression test for "Validate cycle_start_trading_day ISO Date
    Format". Presence alone isn't the whole contract: a value that LOOKS
    like a trading day but isn't one (wrong type, or a string that doesn't
    parse) is exactly the kind of quiet corruption this function otherwise
    exists to catch, even though this field is only ever compared for
    truthiness downstream (never parsed or equality-compared), unlike
    last_evaluated_trading_day.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": status, "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "fire_attempts": 1, "trading_days_active": 3,
        "cycle_start_trading_day": bad_value,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize(
    "bad_fingerprint",
    [
        "not-a-list",
        [1.0],  # wrong length
        [1.0, 2.0, 3.0],  # wrong length
        ["not-a-number", [[1.0, 1.0]]],  # unit_usd wrong type
        [True, [[1.0, 1.0]]],  # unit_usd is a bool, not a real number
        [float("nan"), [[1.0, 1.0]]],  # unit_usd non-finite
        [1.0, "not-a-list"],  # tiers not a list
        [1.0, [[1.0]]],  # tier pair wrong length
        [1.0, [["a", "b"]]],  # tier pair wrong type
        [1.0, [[1.0, True]]],  # tier pair contains a bool
        [0.0, [[1.0, 1.0]]],  # unit_usd zero
        [-1.0, [[1.0, 1.0]]],  # unit_usd negative
        [1.0, [[0.0, 1.0]]],  # tier threshold zero
        [1.0, [[1.0, -2.0]]],  # tier units negative
        [1.0, [[-1.0, 1.0]]],  # tier threshold negative
        [1.0, []],  # empty tier table -- no real config can produce this
    ],
)
def test_ledger_raises_on_a_malformed_tier_config_fingerprint(tmp_path, bad_fingerprint):
    """Regression test for "Unvalidated tier_config_fingerprint ... Causes
    Silent State & Balance Reset", for the nonpositive-valued cases "Reject
    nonpositive values in persisted tier fingerprints", and for the empty-
    tiers case "Require a nonempty persisted tier table". This field
    is read only for an INEQUALITY comparison against the currently-
    configured tier table (`persisted_tier_fingerprint !=
    current_tier_fingerprint` in evaluate_inverse_martingale_step) -- a
    corrupted value compares unequal to any real fingerprint (every real,
    config-loaded fingerprint has strictly positive unit_usd/thresholds/
    units, and inverse_martingale.schema.json requires at least one tier)
    and would be silently misread as "the operator edited the tier
    config," resolving the cycle and permanently discarding a real,
    positive accumulated_usd balance instead of being caught here. The
    empty-tiers case is distinct from the others: `all(...)` over an empty
    iterable is vacuously True, so it passed every prior shape/positivity
    check without this explicit nonempty requirement.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(),
        "tier_config_fingerprint": bad_fingerprint,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_accepts_a_well_formed_tier_config_fingerprint(tmp_path):
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "cycle_start_trading_day": DAY1.isoformat(), "trading_days_active": 3,
        "tier_config_fingerprint": [1.0, [[1.0, 1.0], [2.0, 2.0], [4.0, 4.0]]],
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["tier_config_fingerprint"] == [1.0, [[1.0, 1.0], [2.0, 2.0], [4.0, 4.0]]]


@pytest.mark.parametrize("bad_value", ["false", "true", "", 0, 1])
def test_ledger_raises_on_a_non_boolean_settlement_pending(tmp_path, bad_value):
    """Regression test for "Require a boolean settlement lease". Every call
    site (this module's own claim guard, and near_close_dca.py's
    age-only/disabled-enrollment cleanup passes) reads settlement_pending
    via a plain truthy check -- a non-bool truthy value (e.g. the STRING
    "false", which Python treats as truthy) would otherwise be treated as
    an active lease everywhere, forever, permanently freezing the ticker's
    balance and age. Note 0/1 are deliberately included: int, not bool
    (bool is an int subclass in Python, but the reverse is not true) --
    0 is falsy (would silently pass as "no lease", masking a real
    corruption) while 1 is truthy (would freeze the tracker) -- both wrong.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": "none", "settlement_pending": bad_value}
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("value", [True, False])
def test_ledger_accepts_a_real_boolean_settlement_pending(tmp_path, value):
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": "none", "settlement_pending": value}
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["settlement_pending"] is value


def test_ledger_raises_on_a_settlement_lease_on_a_dipping_tracker(tmp_path):
    """Regression test for "Reject leases on dipping trackers". Every
    legitimate write of settlement_pending=True happens at exactly two
    points in evaluate_inverse_martingale_step -- a fully-resolved fire's
    STATUS_NONE marker, or a partial fire's STATUS_FIRING continuation --
    never while status stays "dipping". A restored or hand-edited tracker
    combining status="dipping" with settlement_pending=True would otherwise
    pass validation, and every downstream call site's plain truthy
    settlement_pending check would then return early before any age or
    abandonment logic runs, permanently freezing the ticker's balance.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 1.0,
        "cycle_start_trading_day": DAY1.isoformat(), "settlement_pending": True,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("status", ["none", "firing"])
def test_ledger_accepts_a_settlement_lease_on_none_or_firing_trackers(tmp_path, status):
    """The two statuses evaluate_inverse_martingale_step actually leases --
    must remain accepted, unlike the "dipping" case above."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": status, "settlement_pending": True}
    if status == "firing":
        payload.update({
            "accumulated_usd": 2.0, "crossed_tier_thresholds": [1.0, 2.0, 4.0],
            "cycle_reference_close": 100.0, "lowest_drop_seen": 8.5,
            "cycle_start_trading_day": DAY1.isoformat(), "fire_attempts": 1,
            "trading_days_active": 3,
        })
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["settlement_pending"] is True


@pytest.mark.parametrize("bad_value", ["not-a-date", "2026-02-30", "", 20260201, 20260201.0])
def test_ledger_raises_on_a_corrupted_last_evaluated_trading_day(tmp_path, bad_value):
    """Regression test for "Validate persisted retry identity fields".
    last_evaluated_trading_day is compared via ``==``, never parsed, so a
    garbage value can't crash -- but it also never legitimately equals a
    real trading_day.isoformat(), so the same-day no-op guard in
    evaluate_inverse_martingale_step silently never fires: trading_days_active
    then advances on EVERY evaluation that trading day (not just the
    first), abandoning a real balance well before max_cycle_days actually
    elapses. Left unvalidated, this reaches the pure decision function
    unchecked instead of being caught here and skipped as corrupted state.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "none",
        "last_evaluated_trading_day": bad_value,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_accepts_a_well_formed_last_evaluated_trading_day(tmp_path):
    """A genuinely valid ISO date string must not be rejected."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "none",
        "last_evaluated_trading_day": DAY1.isoformat(),
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["last_evaluated_trading_day"] == DAY1.isoformat()


@pytest.mark.parametrize("bad_value", ["not-a-stream", "morning ", "MORNING", "", 1, True])
def test_ledger_raises_on_a_corrupted_last_evaluated_stream(tmp_path, bad_value):
    """Regression test for "Validate persisted retry identity fields".
    last_evaluated_stream is compared via ``in``, never parsed, so a
    garbage value can't crash -- but it also never legitimately matches a
    real stream (or None), so a genuine SAME-STREAM retry of an
    already-continued STATUS_FIRING fire is no longer recognized as one:
    fire_attempts re-increments, and once settlement_pending is no longer
    set, another real order can be placed for what is really the same
    logical slot.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "none",
        "last_evaluated_stream": bad_value,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


@pytest.mark.parametrize("stream", ["morning", "midday", "afternoon"])
def test_ledger_accepts_every_real_stream_value(tmp_path, stream):
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": "none", "last_evaluated_stream": stream}
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["last_evaluated_stream"] == stream


@pytest.mark.parametrize("status", ["dipping", "firing"])
def test_ledger_raises_on_an_active_tracker_with_a_corrupted_basis_instead_of_discarding_its_balance(
    tmp_path, status
):
    """Regression test for "Validate persisted basis before resetting the
    cycle". Before this field was validated, a corrupted `basis` (e.g.
    "corrupt", or any value outside {"pct", "stddev"}) reached
    evaluate_inverse_martingale_step() unchanged, where
    `persisted_basis != basis` treated the corruption as an ordinary
    operator config-edit and reset the cycle via resolved_marker() --
    permanently discarding a real, positive accumulated_usd balance instead
    of raising CorruptedLedgerStateError and letting the caller skip only
    this one damaged ticker (the documented "skip only this ticker"
    contract every other field here already gets). A well-formed but
    invalid basis is otherwise syntactically indistinguishable from a
    genuine "pct" -> "stddev" edit, so this must be caught at read() time,
    before evaluate_inverse_martingale_step() ever compares it.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": status, "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 4.0,
        "fire_attempts": 1, "trading_days_active": 3, "cycle_start_trading_day": DAY1.isoformat(),
        "basis": "corrupt",
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_accepts_a_null_stddev_reference_when_the_required_dipping_fields_are_present(tmp_path):
    """A pct-basis cycle (no "basis" key at all here, matching a legacy
    tracker that predates the field -- see "Require complete snapshots for
    active trackers") legitimately has stddev_reference: null -- the
    malformed-field validation must not treat that as corruption, given the
    OTHER fields a "dipping" tracker requires (crossed_tier_thresholds,
    cycle_reference_close, lowest_drop_seen) are present. Unlike a genuine
    "stddev"-basis cycle -- which can never legitimately reach "dipping"
    without a frozen stddev_reference in the first place (see "Require the
    frozen sigma for active stddev cycles" and its own dedicated tests) --
    a pct/legacy tracker's stddev_reference is simply unused and its
    nullness carries no meaning either way.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping",
        "accumulated_usd": 1.0, "stddev_reference": None,
        "crossed_tier_thresholds": [1.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 1.0,
        "cycle_start_trading_day": "2026-01-05", "trading_days_active": 1,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["status"] == STATUS_DIPPING


def test_ledger_caps_the_persisted_event_history(tmp_path):
    """Regression test for "Bound the persisted accumulator event history".
    An enrolled ticker's tracker persists across day boundaries by design,
    and EVERY evaluation (even a plain no-op) reaches _write_locked --
    without a cap, a long-running enrolled ticker's tracker.json would grow
    without bound, and every read-modify-write would get progressively
    slower re-serializing an ever-larger event list.
    """
    ledger = InverseMartingaleLedger(root=tmp_path)
    state: dict = {}
    day = DAY1
    for _ in range(_MAX_PERSISTED_EVENTS + 20):
        # A fresh trading_day each call avoids the same-day
        # already_evaluated_today no-op guard -- this is exercising the
        # ledger's own write-time cap, not the pure function's dedup logic.
        state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day)
        ledger.record_step("AAPL", state, step)
        day = date.fromordinal(day.toordinal() + 1)

    raw = json.loads((tmp_path / "AAPL" / "tracker.json").read_text(encoding="utf-8"))
    assert len(raw["events"]) == _MAX_PERSISTED_EVENTS


def test_ledger_persists_across_simulated_day_boundary(tmp_path):
    ledger = InverseMartingaleLedger(root=tmp_path)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    ledger.record_step("TSLA", state, step)
    day2 = date(2026, 8, 4)
    reread = ledger.read("TSLA")
    state, step = _step(reread, decision_price=98.0, previous_close=100.0, trading_day=day2)
    assert step.newly_crossed_tiers == (2.0,)
    assert step.accumulated_usd_after == 3.0
    assert state["cycle_start_trading_day"] == DAY1.isoformat()


@pytest.mark.parametrize("ticker", ["aapl", "AAPL", " aapl "])
def test_ledger_paths_are_case_and_whitespace_normalized(tmp_path, ticker):
    ledger = InverseMartingaleLedger(root=tmp_path)
    state, step = _step({}, decision_price=99.0, previous_close=100.0)
    ledger.record_step(ticker, state, step)
    assert ledger.read("AAPL")["accumulated_usd"] == 1.0


# --- claim_fire (compare-and-swap) ---------------------------------------------

def test_claim_fire_succeeds_when_prior_state_matches(tmp_path):
    ledger = InverseMartingaleLedger(root=tmp_path)
    prior, prior_step = _step({}, decision_price=95.0, previous_close=100.0)
    ledger.record_step("AAPL", prior, prior_step)
    new_state, step = _step(prior, decision_price=96.5, previous_close=100.0)
    claimed = ledger.claim_fire("AAPL", expected_prior_state=prior, new_state=new_state, step=step)
    assert claimed is True
    assert ledger.read("AAPL")["status"] == STATUS_NONE


def test_claim_fire_fails_when_prior_state_has_changed(tmp_path):
    """Simulates two overlapping processes both reading the same pre-fire
    state and independently deciding to fire: the second process's claim,
    made against a now-stale snapshot, must lose the race (Codex review:
    "Reserve each fire atomically before broker placement")."""
    ledger = InverseMartingaleLedger(root=tmp_path)
    prior, first_step = _step({}, decision_price=95.0, previous_close=100.0)
    ledger.record_step("AAPL", prior, first_step)

    # Both "process A" and "process B" read the SAME pre-fire snapshot...
    snapshot = ledger.read("AAPL")
    a_new_state, a_step = _step(snapshot, decision_price=96.5, previous_close=100.0)
    b_new_state, b_step = _step(snapshot, decision_price=96.5, previous_close=100.0)

    # ...process A claims first and wins.
    assert ledger.claim_fire("AAPL", expected_prior_state=snapshot, new_state=a_new_state, step=a_step) is True
    # process B's claim, against the now-stale snapshot, must lose.
    assert ledger.claim_fire("AAPL", expected_prior_state=snapshot, new_state=b_new_state, step=b_step) is False
    # The ledger reflects only A's claim.
    assert ledger.read("AAPL")["status"] == STATUS_NONE  # A's fire fully resolved


def test_claim_fire_fails_against_a_never_started_ticker_with_wrong_expectation(tmp_path):
    ledger = InverseMartingaleLedger(root=tmp_path)
    new_state, step = _step({}, decision_price=99.0, previous_close=100.0)
    # Expecting a non-empty prior state for a ticker that was never started.
    claimed = ledger.claim_fire(
        "MSFT", expected_prior_state={"status": "dipping", "accumulated_usd": 5.0}, new_state=new_state, step=step
    )
    assert claimed is False
    assert ledger.read("MSFT") == {}


# --- basis persistence / mid-cycle basis-change reset --------------------------

def test_cycle_persists_its_basis_at_start():
    state, step = _step({}, decision_price=99.0, previous_close=100.0, basis="pct")
    assert state["basis"] == "pct"


def test_basis_change_mid_cycle_resets_instead_of_getting_stuck():
    """An operator switching an enrolled ticker's basis (pct -> stddev, or
    vice versa) while a cycle is active must reset the cycle, not silently
    misinterpret mismatched units or get permanently stuck in
    stddev_unavailable before ever reaching the age-abandonment check
    (Codex review: "Reset active cycles when the configured basis
    changes")."""
    state, step = _step({}, decision_price=99.0, previous_close=100.0, basis="pct")
    assert state["status"] == STATUS_DIPPING
    assert state["basis"] == "pct"

    # Now evaluate with basis="stddev" against the SAME persisted state --
    # simulating an operator's config edit mid-cycle.
    state, step = _step(
        state, decision_price=98.0, previous_close=100.0, basis="stddev", stddev_reference=0.01,
    )
    assert step.reason == "cycle_reset_basis_changed"
    assert step.status_after == STATUS_NONE
    assert state == {"status": STATUS_NONE, "last_evaluated_trading_day": DAY1.isoformat(), "last_evaluated_stream": "afternoon"}

    # The reset cycle can now start fresh under the NEW basis on a later day.
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=98.0, previous_close=100.0, trading_day=day2,
                         basis="stddev", stddev_reference=0.01)
    assert step.status_after == STATUS_DIPPING
    assert state["basis"] == "stddev"


def test_missing_persisted_basis_is_not_treated_as_a_mismatch():
    """A tracker created before the `basis` field existed (persisted_basis is
    None) must not be treated as a mismatch -- backward compatible, doesn't
    retroactively reset every pre-existing cycle."""
    legacy_state = {
        "status": STATUS_DIPPING,
        "accumulated_usd": 1.0,
        "crossed_tier_thresholds": [1.0],
        "cycle_start_trading_day": DAY1.isoformat(),
        "cycle_reference_close": 100.0,
        "lowest_drop_seen": 1.0,
        "last_evaluated_trading_day": DAY1.isoformat(),
        # no "basis" key at all
    }
    day2 = date(2026, 8, 4)
    state, step = _step(legacy_state, decision_price=98.0, previous_close=100.0, trading_day=day2)
    assert step.reason != "cycle_reset_basis_changed"
    assert step.newly_crossed_tiers == (2.0,)


def test_missing_persisted_basis_under_a_stddev_config_resets_instead_of_bricking():
    """Regression test for "Handle missing basis in the frozen-sigma check".

    A legacy tracker missing BOTH "basis" and "stddev_reference" (created
    before the "basis" field existed) is safe to treat as a no-op mismatch
    when evaluated under a "pct" config (see
    test_missing_persisted_basis_is_not_treated_as_a_mismatch above) -- but
    NOT under a "stddev" config: nothing could ever have populated
    stddev_reference for a cycle that also never recorded its own basis, so
    without this reset the stddev-basis branch reads
    tracker_state.get("stddev_reference"), finds None, and returns
    "stddev_unavailable" on EVERY future evaluation, forever, before ever
    reaching the age/attempt abandonment cutoffs -- permanently bricking the
    ticker instead of resetting and letting a fresh cycle start under the
    now-current basis.
    """
    legacy_state = {
        "status": STATUS_DIPPING,
        "accumulated_usd": 1.0,
        "crossed_tier_thresholds": [1.0],
        "cycle_start_trading_day": DAY1.isoformat(),
        "cycle_reference_close": 100.0,
        "lowest_drop_seen": 1.0,
        "last_evaluated_trading_day": DAY1.isoformat(),
        # no "basis" key, no "stddev_reference" key at all
    }
    day2 = date(2026, 8, 4)
    state, step = _step(
        legacy_state, decision_price=98.0, previous_close=100.0, trading_day=day2,
        basis="stddev", stddev_reference=0.01,
    )
    assert step.reason == "cycle_reset_basis_changed"
    assert step.status_after == STATUS_NONE
    assert step.fired is False
    assert state == {"status": STATUS_NONE, "last_evaluated_trading_day": day2.isoformat(), "last_evaluated_stream": "afternoon"}

    # The reset cycle can now start fresh under the stddev basis on a later day.
    day3 = date(2026, 8, 5)
    state, step = _step(state, decision_price=98.0, previous_close=100.0, trading_day=day3,
                         basis="stddev", stddev_reference=0.01)
    assert step.status_after == STATUS_DIPPING
    assert state["basis"] == "stddev"


# --- tier-config persistence / mid-cycle tier-change reset --------------------

def test_cycle_persists_its_tier_config_fingerprint_at_start():
    state, step = _step({}, decision_price=99.0, previous_close=100.0)
    assert state["tier_config_fingerprint"] == [1.0, [[1.0, 1.0], [2.0, 2.0], [4.0, 4.0], [8.0, 8.0]]]


def test_tier_threshold_change_mid_cycle_resets_instead_of_double_crediting():
    """An operator editing a tier's threshold (e.g. 1.0 -> 1.1) while a cycle
    is active must reset the cycle, not silently double-credit the same
    conceptual allocation under its new threshold value (Codex review:
    "Reset or freeze cycles when tier thresholds change")."""
    state, step = _step({}, decision_price=99.0, previous_close=100.0)  # -1% crosses tier 1.0 -> $1
    assert state["status"] == STATUS_DIPPING
    assert state["crossed_tier_thresholds"] == [1.0]
    assert state["accumulated_usd"] == 1.0

    # Operator edits config: tier 1.0 -> 1.1. Evaluate against the SAME
    # persisted state with the new tier table.
    edited_tiers = ((1.1, 1.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0))
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=96.0, previous_close=100.0, trading_day=day2, tiers=edited_tiers)
    assert step.reason == "cycle_reset_tier_config_changed"
    assert step.status_after == STATUS_NONE
    assert state == {"status": STATUS_NONE, "last_evaluated_trading_day": day2.isoformat(), "last_evaluated_stream": "afternoon"}

    # The reset cycle starts fresh under the NEW tier table on a later day,
    # crediting the full -4% drop cleanly against the edited thresholds
    # rather than double-crediting anything already accounted for above.
    day3 = date(2026, 8, 5)
    state, step = _step(state, decision_price=96.0, previous_close=100.0, trading_day=day3, tiers=edited_tiers)
    assert step.newly_crossed_tiers == (1.1, 2.0, 4.0)
    assert state["accumulated_usd"] == 7.0  # 1 + 2 + 4, not a leftover $1 double-counted


def test_tier_units_change_mid_cycle_also_resets():
    """A threshold-preserving but units-changing edit (e.g. tier 1.0's units
    2.0 -> 3.0) must also reset -- the fingerprint covers the whole tier
    table, not just thresholds, since a changed unit count changes the
    dollar value of an already-crossed tier."""
    state, step = _step({}, decision_price=99.0, previous_close=100.0)  # crosses tier 1.0
    assert state["accumulated_usd"] == 1.0

    edited_tiers = ((1.0, 3.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0))  # tier 1.0 now worth 3 units
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=98.0, previous_close=100.0, trading_day=day2, tiers=edited_tiers)
    assert step.reason == "cycle_reset_tier_config_changed"


def test_unit_usd_change_mid_cycle_also_resets():
    """A tier-table-preserving but unit_usd-changing edit must also reset --
    otherwise tiers already crossed before the edit have banked dollars at
    the OLD unit_usd while tiers crossed after bank dollars at the NEW one,
    into the same accumulated_usd total, with no way to recover which
    portion used which rate (Codex review: "Freeze the dollar unit for
    active cycles")."""
    state, step = _step({}, decision_price=99.0, previous_close=100.0, unit_usd=1.0)  # crosses tier 1.0 -> $1
    assert state["accumulated_usd"] == 1.0

    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=98.0, previous_close=100.0, trading_day=day2, unit_usd=2.0)
    assert step.reason == "cycle_reset_tier_config_changed"
    assert step.status_after == STATUS_NONE

    # The reset cycle starts fresh under the NEW unit_usd on a later day.
    # -1% again crosses only tier 1.0 (1 unit), same as the very first step.
    day3 = date(2026, 8, 5)
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day3, unit_usd=2.0)
    assert state["accumulated_usd"] == 2.0  # 1 unit * the new $2/unit, not a leftover $1 at the old rate


def test_missing_persisted_tier_fingerprint_is_not_treated_as_a_mismatch():
    """A tracker created before this field existed (persisted fingerprint is
    None) must not be treated as a mismatch -- backward compatible."""
    legacy_state = {
        "status": STATUS_DIPPING,
        "accumulated_usd": 1.0,
        "crossed_tier_thresholds": [1.0],
        "cycle_start_trading_day": DAY1.isoformat(),
        "cycle_reference_close": 100.0,
        "basis": "pct",
        "lowest_drop_seen": 1.0,
        "last_evaluated_trading_day": DAY1.isoformat(),
        # no "tier_config_fingerprint" key at all
    }
    day2 = date(2026, 8, 4)
    state, step = _step(legacy_state, decision_price=98.0, previous_close=100.0, trading_day=day2)
    assert step.reason != "cycle_reset_tier_config_changed"
    assert step.newly_crossed_tiers == (2.0,)
