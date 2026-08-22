"""Tests for dca_inverse_martingale: the opt-in deferred-accumulation sizing mode."""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from typing import Any

import pytest

from dca_inverse_martingale import (
    _MAX_PERSISTED_EVENTS,
    STATUS_DIPPING,
    STATUS_FIRING,
    STATUS_NONE,
    CorruptedLedgerStateError,
    InverseMartingaleLedger,
    InverseMartingaleStep,
    archived_split_factor,
    evaluate_inverse_martingale_step,
    rolling_stddev,
    stddev_reference_for_ticker,
    trailing_daily_returns,
)

PCT_TIERS = ((1.0, 1.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0))
DAY1 = date(2026, 8, 3)


def _step(state, *, decision_price, previous_close, trading_day=DAY1, tiers=PCT_TIERS,
          unit_usd=1.0, recovery_offset=1.0, max_cycle_days=30, max_fire_attempts=5,
          basis="pct", stddev_reference=None, per_order_cap_usd=None, stream="afternoon",
          split_factor=None, split_cursor_day=None):
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
        split_factor=split_factor,
        split_cursor_day=split_cursor_day,
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
        "rearm_reference_close": 100.0,
        # Seeded alongside the latch so the split detector has a baseline from
        # the very first post-resolution evaluation.
        # The split window boundary: the day the latch was last evaluated.
        "rearm_last_seen_day": DAY1.isoformat(),
        "fired_floor_price": pytest.approx(95.0),
    }


def test_recovery_gap_past_exact_offset_band_still_fires():
    state, _ = _step({}, decision_price=91.5, previous_close=100.0)  # -8.5%, accum=15
    # Jumps straight from -8.5% to -3% in one step (skips the -7.5% band).
    state, step = _step(state, decision_price=97.0, previous_close=100.0)
    assert step.fired
    assert step.fire_amount_usd == 15.0


def test_fresh_recovery_defers_a_sub_dollar_balance_instead_of_rounding_up():
    """Regression test for "Defer sub-dollar balances instead of rounding them
    up".

    unit_usd=0.3 with a single 1-unit tier accumulates $0.30. Flooring that to
    the $1.00 broker minimum placed more than three times the ladder amount the
    accumulator exists to defer -- and magnified further the smaller the
    configured unit -- then cleared the cycle as if the tracked balance had been
    spent. Recovery must instead hold the balance: nothing is bought, which is
    the accumulator's own contract for a descent it cannot act on.
    """
    small_tiers = ((1.0, 1.0),)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.3)
    assert step.accumulated_usd_after == pytest.approx(0.3)
    state, step = _step(state, decision_price=100.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.3)
    assert not step.fired
    assert step.fire_amount_usd == pytest.approx(0.0)
    assert step.reason == "recovery_below_broker_minimum"
    # The balance survives intact and the cycle stays active, so a deeper later
    # dip can still grow it past the minimum.
    assert step.accumulated_usd_after == pytest.approx(0.3)
    assert state["status"] == STATUS_DIPPING
    assert state["accumulated_usd"] == pytest.approx(0.3)


def test_firing_continuation_also_defers_a_sub_dollar_remainder():
    small_tiers = ((1.0, 1.0),)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, tiers=small_tiers, unit_usd=0.3)
    day2 = date(2026, 8, 4)
    # Force a continuation state directly (as if a prior run's per_order_cap
    # had already put this cycle into STATUS_FIRING with a sub-dollar
    # remainder) rather than relying on the fresh-recovery branch, to pin
    # that the SAME deferral applies in the continuation branch too.
    firing_state = dict(state, status=STATUS_FIRING, fire_attempts=1)
    state, step = _step(firing_state, decision_price=100.0, previous_close=100.0, trading_day=day2,
                         tiers=small_tiers, unit_usd=0.3)
    assert not step.fired
    assert step.fire_amount_usd == pytest.approx(0.0)
    assert step.reason == "continuation_below_broker_minimum"
    assert step.accumulated_usd_after == pytest.approx(0.3)
    assert state["status"] == STATUS_FIRING
    # No order was attempted, so the retry budget must not be consumed either.
    assert state["fire_attempts"] == 1


def test_the_broker_minimum_floor_still_beats_a_sub_dollar_per_order_cap():
    """The floor is unchanged where it only raises the amount back toward money
    the cycle really holds: a $7 balance with a $0.40 cap still places $1.00,
    since a broker-rejected sub-dollar order is a harder constraint than a
    self-imposed cap. Only a sub-dollar TRACKED balance now defers."""
    tiers = ((1.0, 1.0), (2.0, 2.0), (4.0, 4.0))
    state, _ = _step({}, decision_price=96.0, previous_close=100.0, tiers=tiers, unit_usd=1.0)
    assert state["accumulated_usd"] == pytest.approx(7.0)
    state, step = _step(
        state, decision_price=97.0, previous_close=100.0, tiers=tiers, unit_usd=1.0,
        per_order_cap_usd=0.4,
    )
    assert step.fired
    assert step.fire_amount_usd == pytest.approx(1.0)
    assert step.accumulated_usd_after == pytest.approx(6.0)


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


def test_a_capped_fire_never_leaves_a_sub_minimum_remainder():
    """Regression test for "Keep capped fire remainders placeable". A
    per_order_cap_usd tighter than the balance by less than the broker
    minimum ($10.50 accumulated, $10.00 cap) previously fired exactly the
    cap and left a $0.50 STATUS_FIRING remainder -- a status that bypasses
    tier accumulation entirely, so that dust could never grow past the
    broker minimum and was eventually discarded outright by max_cycle_days/
    max_fire_attempts abandonment. The fire amount must instead shrink just
    enough to leave a full broker-minimum remainder, which the very next
    evaluation can actually place.
    """
    tiers = ((1.0, 1.0), (2.0, 2.0))
    # A 2.5% dip crosses both tiers: 3.5 * (1 + 2) == accumulated_usd 10.5.
    state, _ = _step({}, decision_price=97.5, previous_close=100.0, tiers=tiers, unit_usd=3.5)
    assert state["accumulated_usd"] == pytest.approx(10.5)
    # Recovery: 1% drop is 1.5 points off the 2.5% low, clearing the 1.0
    # default recovery_offset.
    state, step = _step(
        state, decision_price=99.0, previous_close=100.0, tiers=tiers, unit_usd=3.5,
        per_order_cap_usd=10.0,
    )
    assert step.fired
    assert step.fire_amount_usd == pytest.approx(9.5)  # shrunk from the $10 cap
    assert step.accumulated_usd_after == pytest.approx(1.0)  # a full, placeable broker minimum
    assert step.status_after == STATUS_FIRING
    assert state["accumulated_usd"] == pytest.approx(1.0)

    # And the very next evaluation places that remainder in full -- it is
    # never stuck below the broker minimum.
    day2 = date(2026, 8, 4)
    state, step2 = _step(
        state, decision_price=50.0, previous_close=100.0, trading_day=day2, tiers=tiers, unit_usd=3.5,
        per_order_cap_usd=10.0,
    )
    assert step2.fired
    assert step2.fire_amount_usd == pytest.approx(1.0)
    assert step2.accumulated_usd_after == pytest.approx(0.0)


def test_a_cap_that_cannot_leave_a_placeable_remainder_is_overridden():
    """Regression test for "Keep the adjusted fire amount above the broker
    minimum". A balance between one and two broker minimums, capped at or
    below the minimum -- a $1.50 balance capped at $1.00 -- previously
    shrank the capped $1.00 down to a $0.50 remainder-zeroing fire that is
    ITSELF below the broker minimum: both evaluation branches reject a
    sub-dollar fire, so the balance became permanently unplaceable, exactly
    the failure mode the shrink exists to prevent. No cap-respecting
    partition of $1.50 leaves both halves placeable, so the cap must give
    way: the fire places the FULL balance in one order instead.
    """
    tiers = ((1.5, 1.5),)
    state, _ = _step({}, decision_price=98.5, previous_close=100.0, tiers=tiers, unit_usd=1.0)
    assert state["accumulated_usd"] == pytest.approx(1.5)
    state, step = _step(
        state, decision_price=100.0, previous_close=100.0, tiers=tiers, unit_usd=1.0,
        per_order_cap_usd=1.0,
    )
    assert step.fired
    assert step.fire_amount_usd == pytest.approx(1.5)  # the cap is overridden, not honored
    assert step.accumulated_usd_after == pytest.approx(0.0)


def test_an_uncapped_fire_still_leaves_no_remainder():
    """The adjustment in _capped_fire_amount must be a no-op when
    per_order_cap_usd never actually capped anything -- firing the whole
    balance already leaves a $0 remainder, which the shrink logic must not
    disturb."""
    state, _ = _step({}, decision_price=91.5, previous_close=100.0)  # accum = 15
    state, step = _step(state, decision_price=97.0, previous_close=100.0)  # no cap
    assert step.fired
    assert step.fire_amount_usd == pytest.approx(15.0)
    assert step.accumulated_usd_after == pytest.approx(0.0)
    assert step.status_after == STATUS_NONE


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
        "rearm_reference_close": pytest.approx(100.0),
        # The split window boundary: the day cycle_reference_close's price
        # SCALE was captured -- cycle start (DAY1), not today (day3) this
        # multi-run continuation happens to resolve on (Codex review:
        # "Retain the reference-scale cursor when a continuation resolves").
        "rearm_last_seen_day": DAY1.isoformat(),
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
        "rearm_reference_close": pytest.approx(100.0),
        # The split window boundary: the day the latch was last evaluated.
        "rearm_last_seen_day": DAY1.isoformat(),
        "fired_floor_price": pytest.approx(95.0),
    }
    # Merely reaching a later day does not make an unchanged descent fresh
    # -- previous_close (99) is still strictly below the reference (100),
    # so price has not yet actually recovered.
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=99.0, previous_close=99.0, trading_day=day2)
    assert step.reason == "awaiting_fresh_dip"
    assert state["rearm_reference_close"] == pytest.approx(100.0)

    # Rearm only after price exits the old dip, then allow a subsequent dip.
    state, step = _step(state, decision_price=100.0, previous_close=100.0, trading_day=day2)
    assert step.reason == "no_action"
    assert state == {}
    day3 = date(2026, 8, 5)
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day3)
    assert step.status_after == STATUS_DIPPING
    assert step.accumulated_usd_after == 1.0


def test_rearm_uses_previous_close_to_recognize_a_fresh_descent():
    resolved = {
        "status": STATUS_NONE,
        "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon",
        "rearm_reference_close": 100.0,
    }

    state, step = _step(
        resolved,
        decision_price=99.0,
        previous_close=101.0,
        trading_day=date(2026, 8, 4),
    )

    assert step.reason == "tier_crossed"
    assert step.status_after == STATUS_DIPPING
    assert state["cycle_reference_close"] == pytest.approx(101.0)


def test_a_new_cycle_rearming_the_same_day_starts_its_age_at_one():
    """Regression test for "Reset the age counter when rearming on the same
    day".

    When a completed cycle exits its latch and breaks the fire floor in a LATER
    STREAM on the same trading day, that path deliberately preserves the
    resolved marker's `last_evaluated_trading_day`. `trading_days_active` is
    computed near function entry and its increment is suppressed when that
    field already equals today, so the freshly armed cycle started at 0 instead
    of 1 -- buying it one extra session beyond max_cycle_days, during which it
    could still fire after the configured cutoff.
    """
    # Morning: a -10% dip fires and resolves, arming the latch at 100.
    state, step = _step({}, decision_price=90.0, previous_close=100.0, stream="morning")
    state, step = _step(state, decision_price=91.0, previous_close=100.0, stream="morning")
    assert step.fired
    assert state["status"] == STATUS_NONE
    assert state["rearm_reference_close"] == pytest.approx(100.0)

    # Afternoon, SAME trading day: previous_close (102) has risen above the
    # latch reference, so the latch exits, AND the price breaks below the
    # fire's own floor of 90 -- a genuinely new descent that re-arms rather
    # than being suppressed by the same-day floor guard.
    state, step = _step(state, decision_price=89.0, previous_close=102.0, stream="afternoon")
    assert step.status_after == STATUS_DIPPING
    # Its first trading day counts as one, not zero -- the old cycle's
    # bookkeeping is not inherited.
    assert state["cycle_start_trading_day"] == DAY1.isoformat()
    assert state["trading_days_active"] == 1


def test_a_previous_close_equal_to_the_rearm_reference_exits_the_latch():
    """Regression test for "Treat an equal prior close as latch recovery".
    An EQUAL previous_close counts as having exited the paid-for dip, same
    as "at or above" everywhere else in this module -- not just a strictly
    GREATER previous_close, which left the exact boundary reachable by
    neither the latch nor the exit branch and permanently stuck."""
    resolved = {
        "status": STATUS_NONE,
        "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon",
        "rearm_reference_close": 100.0,
    }

    day2 = date(2026, 8, 4)
    state, step = _step(resolved, decision_price=99.0, previous_close=100.0, trading_day=day2)

    assert step.reason == "tier_crossed"
    assert step.status_after == STATUS_DIPPING
    assert "rearm_reference_close" not in state


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
    # A later day alone is not a fresh dip: price must first exit the old
    # one (previous_close, 99, still strictly below the reference).
    day2 = date(2026, 8, 4)
    state, step = _step(state, decision_price=99.0, previous_close=99.0, trading_day=day2)
    assert step.reason == "awaiting_fresh_dip"
    assert step.status_after == STATUS_NONE


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
    # prior_close, but nowhere near the -5% floor -- must be a no-op. Note
    # previous_close (100) equals the durable latch's own reference here,
    # but it's an unchanged, REPEATED same-day observation (not a genuinely
    # later day's close), so it does not by itself exit the durable latch --
    # the still-armed latch is authoritative and blocks this directly,
    # never even deferring to the separate (weaker) same-day floor guard
    # (Codex review, third round: "Keep the durable latch authoritative
    # over the same-day floor").
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=DAY1, stream="midday")
    assert step.reason == "awaiting_fresh_dip"
    assert not step.fired
    assert step.newly_crossed_tiers == ()
    assert state["rearm_reference_close"] == pytest.approx(100.0)
    assert state == {**resolved, "rearm_last_seen_day": DAY1.isoformat()}

    # Even a deeper print is still part of the unresolved descent -- the
    # durable latch (still armed, previous_close never having exited it)
    # blocks it regardless of the same-day floor's own, narrower depth check.
    state, step = _step(state, decision_price=94.5, previous_close=100.0, trading_day=DAY1, stream="afternoon")
    assert step.reason == "awaiting_fresh_dip"
    assert step.status_after == STATUS_NONE


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
    # The durable rearm latch itself also legitimately exits on this same
    # call: previous_close (110) already exceeds the latch's own reference
    # (100, the cycle's pre-descent price) -- unrelated to, and independent
    # of, the same-day floor guard blocking the actual credit above.
    assert "rearm_reference_close" not in state
    # rearm_last_seen_day goes with it: the split window boundary is
    # meaningless once there is no latch left for a rebase to correct.
    assert "rearm_last_seen_day" not in state
    assert state == {
        k: v for k, v in resolved.items()
        if k not in ("rearm_reference_close", "rearm_last_seen_day")
    }

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
    # prior_close, but nowhere near the -5% floor -- must be a no-op. The
    # abandonment now also carries the durable rearm_reference_close latch
    # (Codex review, third round: "Preserve the latch when abandoning a
    # partially paid fire"), which is authoritative and blocks this
    # directly rather than deferring to the same-day floor.
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day2, stream="midday")
    assert step.reason == "awaiting_fresh_dip"
    assert not step.fired
    assert step.newly_crossed_tiers == ()
    # rearm_last_seen_day is untouched by this no-op re-evaluation (no
    # split_cursor_day is supplied here) -- it stays at the cycle's own
    # start day (DAY1), the day cycle_reference_close's price scale was
    # actually captured on, matching the abandonment fix above (Codex
    # review: "Retain the reference-scale cursor when a continuation
    # resolves").
    assert state == resolved

    # Even a deeper print (-5.5%) is still part of the unresolved descent --
    # the durable latch has no depth consideration of its own and blocks
    # regardless, same as the analogous fire-floor test above.
    state, step = _step(state, decision_price=94.5, previous_close=100.0, trading_day=day2, stream="afternoon")
    assert step.reason == "awaiting_fresh_dip"
    assert step.status_after == STATUS_NONE


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
    # final evaluation's own drop_metric are 1.0. The durable
    # rearm_reference_close latch is now also carried (Codex review, third
    # round: "Preserve the latch when abandoning a partially paid fire").
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": abandon_day.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": 1.0,
        "fired_floor_price": pytest.approx(99.0),
        "rearm_reference_close": pytest.approx(100.0),
        # The split window boundary: the day cycle_reference_close's price
        # SCALE was captured -- cycle start (DAY1), not the abandonment day
        # 30 trading days later (Codex review: "Retain the reference-scale
        # cursor when a continuation resolves").
        "rearm_last_seen_day": DAY1.isoformat(),
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
    # continuation call's own drop_metric (3.0), so the floor is 8.5. The
    # durable rearm_reference_close latch is now also carried, since this
    # abandonment happened mid-STATUS_FIRING -- earlier continuations may
    # already have placed real, settled partial chunks (Codex review, third
    # round: "Preserve the latch when abandoning a partially paid fire").
    assert state == {
        "status": STATUS_NONE, "last_evaluated_trading_day": day.isoformat(),
        "last_evaluated_stream": "afternoon", "fired_floor_drop_metric": 8.5,
        "fired_floor_price": pytest.approx(91.5),
        "rearm_reference_close": pytest.approx(100.0),
        # The split window boundary: the day cycle_reference_close's price
        # SCALE was captured -- cycle start (DAY1), not the abandonment day
        # (Codex review: "Retain the reference-scale cursor when a
        # continuation resolves").
        "rearm_last_seen_day": DAY1.isoformat(),
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


def _write_split_history(history_dir, ticker, rows, dividends=None):
    """An archive carrying the real ``Stock Splits`` (and ``Dividends``)
    columns, as scripts/refresh_active_ticker_histories.py writes them.

    ``dividends`` optionally maps a row's date to its per-share cash payout;
    every other date defaults to "0.0", the ordinary-day value.
    """
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{ticker}.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    dividends = dividends or {}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for day, close, split in rows:
            writer.writerow({
                "Date": day.isoformat(), "Open": str(close), "High": str(close), "Low": str(close),
                "Close": str(close), "Adj Close": str(close), "Volume": "1000",
                "Dividends": str(dividends.get(day, 0.0)), "Stock Splits": str(split),
            })


def _write_split_history_missing_column(history_dir, ticker, rows, *, omit="Stock Splits"):
    """An archive whose header is missing ``omit`` entirely -- a legacy or
    partially generated file, not merely a column full of "0.0" rows."""
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{ticker}.csv"
    fields = [f for f in
              ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
              if f != omit]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for day, close in rows:
            row = {
                "Date": day.isoformat(), "Open": str(close), "High": str(close), "Low": str(close),
                "Close": str(close), "Adj Close": str(close), "Volume": "1000",
                "Dividends": "0.0", "Stock Splits": "0.0",
            }
            row.pop(omit, None)
            writer.writerow(row)


def test_archived_split_factor_reports_the_recorded_ratio(tmp_path):
    """Regression test for "Require corporate-action evidence before rebasing".

    The two heuristics this replaced asked whether a close-to-close move LOOKED
    like a split. Neither could tell one from an ordinary gap -- an 80 -> 120
    news gap reads as a reverse 3-for-2 and a ~50% crash reads as a 2-for-1,
    both entirely realistic for the volatile tickers this mode targets. The
    archive records the real ratio, so an ordinary gap of any size reports
    nothing.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    _write_split_history(history, "AAPL", [
        (date(2026, 8, 7), 100.0, 0.0),
        (date(2026, 8, 10), 50.0, 2.0),   # a real 2-for-1, recorded as such
        (date(2026, 8, 11), 51.0, 0.0),
    ])
    # The split falls inside the window.
    factor, cursor = archived_split_factor(
        "AAPL", since=date(2026, 8, 7), as_of=as_of, history_dir=history
    )
    assert factor == pytest.approx(2.0)
    # The cursor is the newest row EXAMINED, never as_of -- stamping as_of
    # would skip a split's own effective-day row forever.
    assert cursor == date(2026, 8, 11)
    # ...and outside it, the same archive confirms NO corporate action. That is
    # 1.0 ("nothing happened"), which is a different answer from None.
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 10), as_of=as_of, history_dir=history
    )[0] == pytest.approx(1.0)


def test_archived_split_factor_ignores_an_ordinary_violent_gap(tmp_path):
    """The case both heuristics got wrong: a huge single-session move with no
    corporate action behind it. 80 -> 120 is a 1.5x jump the whole-factor rule
    classified as a reverse 3-for-2; the archive records 0.0 and confirms
    nothing happened."""
    history = tmp_path / "ticker_data"
    _write_split_history(history, "AAPL", [
        (date(2026, 8, 7), 80.0, 0.0),
        (date(2026, 8, 10), 120.0, 0.0),  # news gap, not a split
        (date(2026, 8, 11), 40.0, 0.0),   # ~66% crash, also not a split
    ])
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 7), as_of=date(2026, 8, 12), history_dir=history
    )[0] == pytest.approx(1.0)


def test_archived_split_factor_cannot_confirm_from_a_missing_or_stale_archive(tmp_path):
    """``None`` means "cannot confirm", which the caller treats as "do not
    rebase" -- the fail-closed direction. A stale archive's silence is
    indistinguishable from a market with no splits, so it must not be read as
    confirmation that none occurred."""
    history = tmp_path / "ticker_data"
    # Absent entirely.
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 7), as_of=date(2026, 8, 12), history_dir=history
    ) is None
    # Present but its most recent row predates as_of by more than the staleness
    # budget -- a refresh that silently stopped.
    _write_split_history(history, "AAPL", [(date(2026, 7, 1), 100.0, 0.0)])
    assert archived_split_factor(
        "AAPL", since=date(2026, 6, 1), as_of=date(2026, 8, 12), history_dir=history
    ) is None
    # A path-traversing symbol is refused outright, like trailing_daily_returns.
    assert archived_split_factor(
        "../ESCAPED", since=date(2026, 8, 7), as_of=date(2026, 8, 12), history_dir=history
    ) is None


def test_archived_split_factor_includes_a_split_effective_on_as_of(tmp_path):
    """Regression test for "Include effective-day splits before evaluating the
    latch". A split effective ON as_of (the day being evaluated) must be
    confirmed by the SAME call, not the next one -- decision_price/
    previous_close for that day already reflect the post-split scale, so
    withholding confirmation until tomorrow leaves this evaluation comparing
    a post-split price against a pre-split reference.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    _write_split_history(history, "AAPL", [
        (date(2026, 8, 11), 100.0, 0.0),
        (as_of, 50.0, 2.0),  # the split's own effective day IS as_of
    ])
    factor, cursor = archived_split_factor(
        "AAPL", since=date(2026, 8, 11), as_of=as_of, history_dir=history
    )
    assert factor == pytest.approx(2.0)
    assert cursor == as_of
    # A row strictly AFTER as_of is still genuine lookahead and excluded.
    _write_split_history(history, "MSFT", [
        (date(2026, 8, 11), 100.0, 0.0),
        (as_of, 100.0, 0.0),
        (as_of + timedelta(days=1), 50.0, 2.0),  # tomorrow's split
    ])
    factor2, cursor2 = archived_split_factor(
        "MSFT", since=date(2026, 8, 11), as_of=as_of, history_dir=history
    )
    assert factor2 == pytest.approx(1.0)
    assert cursor2 == as_of


def test_archived_split_factor_rejects_a_malformed_split_cell(tmp_path):
    """Regression test for "Reject malformed split cells before advancing
    the cursor". A ``Stock Splits`` cell of unparseable text, NaN, or a
    negative number reads back through plain float-and-check as ``None`` --
    identical to the genuine ordinary-day value ``"0.0"`` -- so without
    distinguishing the two, a fresh or partially written archive's malformed
    row was confirmed as "no corporate action" and its date advanced past as
    the durable cursor. A later archive correction is then excluded forever
    by ``row_date <= since``, permanently missing a real split.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    bad_date = date(2026, 8, 10)
    history.mkdir(parents=True, exist_ok=True)
    path = history / "AAPL.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "Date": date(2026, 8, 9).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
        writer.writerow({
            "Date": bad_date.isoformat(), "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "not-a-ratio",
        })
        # A later, perfectly ordinary row -- proves the failure short-circuits
        # the whole call rather than merely skipping the bad row.
        writer.writerow({
            "Date": date(2026, 8, 11).isoformat(), "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=as_of, history_dir=history
    ) is None


def test_archived_split_factor_rejects_a_malformed_dividend_cell(tmp_path):
    """Regression test for "Reject malformed dividend cells before advancing
    the cursor". Same ambiguity as the Stock Splits cell, one column over: a
    ``Dividends`` cell of unparseable text, NaN, or a negative number reads
    back as ``None`` -- identical to the genuine ordinary-day value
    ``"0.0"`` -- so a malformed row was confirmed as "no distribution" and
    its date advanced past as the durable cursor, permanently excluding a
    later correction via ``row_date <= since``.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    bad_date = date(2026, 8, 10)
    history.mkdir(parents=True, exist_ok=True)
    path = history / "AAPL.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "Date": date(2026, 8, 9).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
        writer.writerow({
            "Date": bad_date.isoformat(), "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "-3.0", "Stock Splits": "0.0",
        })
        # A later, perfectly ordinary row -- proves the failure short-circuits
        # the whole call rather than merely skipping the bad row.
        writer.writerow({
            "Date": date(2026, 8, 11).isoformat(), "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=as_of, history_dir=history
    ) is None


def test_archived_split_factor_rejects_a_malformed_action_row_date(tmp_path):
    """Regression test for "Reject malformed action-row dates before
    advancing the cursor". A row with a Date present but unparseable could
    be hiding a real split/dividend this call cannot examine -- silently
    skipping it let a LATER, cleanly-dated row become the persisted cursor,
    after which a corrected archive would exclude the malformed row's
    actual date forever via ``row_date <= since``.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    history.mkdir(parents=True, exist_ok=True)
    path = history / "AAPL.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "Date": date(2026, 8, 9).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
        writer.writerow({
            "Date": "not-a-date", "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "2.0",
        })
        # A later, perfectly ordinary, cleanly-dated row -- proves the
        # failure short-circuits the whole call rather than merely skipping
        # the malformed row and letting this one become the cursor.
        writer.writerow({
            "Date": date(2026, 8, 11).isoformat(), "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=as_of, history_dir=history
    ) is None


def test_archived_split_factor_rejects_an_undated_corporate_action_row(tmp_path):
    """Regression test for "Reject undated corporate-action rows before
    advancing". A row with a genuinely BLANK Date but a nonzero Stock
    Splits/Dividends value means the archive recorded a real corporate
    action with nothing to place it on -- silently skipping it (as a
    genuinely inert blank row is skipped) let a LATER, cleanly-dated row
    become the persisted cursor, after which a corrected archive (the date
    filled back in) would exclude this very row forever via
    ``row_date <= since``.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    history.mkdir(parents=True, exist_ok=True)
    path = history / "AAPL.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "Date": date(2026, 8, 9).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
        writer.writerow({
            "Date": "", "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "2.0",
        })
        # A later, perfectly ordinary, cleanly-dated row -- proves the
        # failure short-circuits the whole call rather than merely skipping
        # the undated row and letting this one become the cursor.
        writer.writerow({
            "Date": date(2026, 8, 11).isoformat(), "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=as_of, history_dir=history
    ) is None


def test_archived_split_factor_rejects_a_malformed_undated_corporate_action(tmp_path):
    """Regression test for "Reject malformed actions even when the row is
    undated". A blank-date row with a malformed (unparseable, NaN, or
    negative) Stock Splits/Dividends value parses back to the SAME ``None``
    ``_parse_corporate_action_cell`` returns for a genuinely ordinary "0.0"
    cell -- checking only the parsed value (and discarding the usability
    flag) would let this row be treated as inert, letting a LATER,
    cleanly-dated row become the persisted cursor and permanently excluding
    a corrected version of this row via ``row_date <= since``.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    history.mkdir(parents=True, exist_ok=True)
    path = history / "AAPL.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "Date": date(2026, 8, 9).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
        writer.writerow({
            "Date": "", "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "NaN",
        })
        # A later, perfectly ordinary, cleanly-dated row -- proves the
        # failure short-circuits the whole call rather than reading the
        # malformed undated cell as inert and letting this one become the
        # cursor.
        writer.writerow({
            "Date": date(2026, 8, 11).isoformat(), "Open": "50", "High": "50", "Low": "50",
            "Close": "50", "Adj Close": "50", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=as_of, history_dir=history
    ) is None


def test_archived_split_factor_keeps_a_genuinely_blank_undated_row_inert(tmp_path):
    """Regression test for "Keep blank undated rows inert". A trailing
    CSV row with a blank Date AND blank Stock Splits/Dividends cells (a
    comma-only spreadsheet artifact) is exactly the ordinary trailing-row
    noise the undated-row branch exists to permit -- distinct from the
    malformed-nonblank case covered by
    test_archived_split_factor_rejects_a_malformed_undated_corporate_action.
    ``_parse_corporate_action_cell("")`` returns the SAME ``(False, None)``
    a malformed nonblank cell does, so checking the usability flag alone
    (without first distinguishing "blank" from "nonblank but unparseable")
    would reject this archive too, breaking every split lookup over it
    until the blank row is removed.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    history.mkdir(parents=True, exist_ok=True)
    path = history / "AAPL.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "Date": date(2026, 8, 9).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
        # A genuinely blank trailing row -- every field empty, not just Date.
        writer.writerow({
            "Date": "", "Open": "", "High": "", "Low": "",
            "Close": "", "Adj Close": "", "Volume": "", "Dividends": "", "Stock Splits": "",
        })
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 8), as_of=as_of, history_dir=history
    ) == (1.0, date(2026, 8, 9))


def test_archived_split_factor_rejects_an_archive_missing_the_splits_column(tmp_path):
    """Regression test for "Reject archives that omit split metadata". A
    header missing "Stock Splits" entirely reads back as None for every row --
    identical to a genuine "0.0" -- so without a header check this falsely
    confirms "no corporate action" and advances the cursor past rows the
    archive never actually reported on.
    """
    history = tmp_path / "ticker_data"
    _write_split_history_missing_column(
        history, "AAPL",
        [(date(2026, 8, 10), 100.0), (date(2026, 8, 11), 50.0)],  # a real 2x drop, unrecorded
        omit="Stock Splits",
    )
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=date(2026, 8, 12), history_dir=history
    ) is None


def test_archived_split_factor_rejects_an_archive_missing_the_dividends_column(tmp_path):
    """The writer script always emits "Stock Splits" and "Dividends" together
    (scripts/refresh_active_ticker_histories.py), so a file missing either one
    is equally untrustworthy."""
    history = tmp_path / "ticker_data"
    _write_split_history_missing_column(
        history, "AAPL",
        [(date(2026, 8, 10), 100.0), (date(2026, 8, 11), 100.0)],
        omit="Dividends",
    )
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=date(2026, 8, 12), history_dir=history
    ) is None


def test_archived_split_factor_rejects_an_overflowed_cumulative_factor(tmp_path):
    """Regression test for "Reject an unrepresentable cumulative split
    factor". Two individually-finite split ratios whose product overflows to
    inf must not be returned -- a caller that divides the latch reference by
    infinity gets zero, reading every positive price as having exited the
    latch and re-crediting an already-paid descent.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    _write_split_history(history, "AAPL", [
        (date(2026, 8, 9), 100.0, 1e308),
        (date(2026, 8, 10), 100.0, 1e308),  # product overflows to inf
        (date(2026, 8, 11), 100.0, 0.0),
    ])
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 8), as_of=as_of, history_dir=history
    ) is None


def test_archived_split_factor_never_reports_a_cursor_older_than_since(tmp_path):
    """Regression test for "Never move the split cursor backward". If the
    archive is rolled back to an older-but-still-fresh snapshot, its newest
    row can predate `since` (the previous call's own cursor). Reporting that
    older date as the new cursor would let a caller persist a regression --
    a split already folded into the reference on a later run would then be
    re-examined and folded in a SECOND time once the complete archive
    returns.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    # The archive's newest row (Aug 9) is OLDER than `since` (Aug 10), but
    # still within the staleness budget relative to as_of.
    _write_split_history(history, "AAPL", [
        (date(2026, 8, 8), 100.0, 0.0),
        (date(2026, 8, 9), 100.0, 0.0),
    ])
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 10), as_of=as_of, history_dir=history
    ) is None


def test_archived_split_factor_folds_in_a_confirmed_dividend(tmp_path):
    """Regression test for "Rebase the durable latch across cash
    distributions". A cash dividend mechanically drops the raw share price by
    the payout amount, exactly like a split drops it by the split ratio -- an
    armed latch spanning the ex-dividend date must rebase the same way or it
    stays pinned to an economically unreachable pre-distribution reference.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    ex_date = date(2026, 8, 10)
    # A $20 special dividend on an ex-date close of $80 (already post-drop) --
    # reconstructing the pre-drop scale gives (80 + 20) / 80 == 1.25, and
    # dividing the reference by that scales it down to 0.8x, matching the
    # real mechanical price drop.
    _write_split_history(
        history, "AAPL",
        [(date(2026, 8, 9), 100.0, 0.0), (ex_date, 80.0, 0.0), (date(2026, 8, 11), 81.0, 0.0)],
        dividends={ex_date: 20.0},
    )
    factor, cursor = archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=as_of, history_dir=history
    )
    assert factor == pytest.approx(1.25)
    assert cursor == date(2026, 8, 11)


def test_archived_split_factor_cannot_confirm_a_dividend_with_no_usable_close(tmp_path):
    """Regression test for "Preserve the cursor when a dividend cannot be
    valued". A dividend row whose own Close is missing/unparseable cannot be
    reconstructed into a ratio at all -- the whole call returns None (cannot
    confirm) WITHOUT advancing the cursor past that row, exactly like the
    missing-column and stale-archive guards. Reporting the row as examined
    while silently omitting its adjustment would let a later run, once the
    archive is fixed, never look at that date again.
    """
    history = tmp_path / "ticker_data"
    as_of = date(2026, 8, 12)
    bad_date = date(2026, 8, 10)
    history.mkdir(parents=True, exist_ok=True)
    path = history / "AAPL.csv"
    fields = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "Date": date(2026, 8, 9).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
        writer.writerow({
            "Date": bad_date.isoformat(), "Open": "", "High": "", "Low": "",
            "Close": "not-a-number", "Adj Close": "", "Volume": "1000", "Dividends": "1.0", "Stock Splits": "0.0",
        })
        # A later, perfectly usable row -- proves the failure short-circuits
        # the whole call rather than merely skipping the bad row and
        # continuing to examine (and report a cursor past) what follows it.
        writer.writerow({
            "Date": date(2026, 8, 11).isoformat(), "Open": "100", "High": "100", "Low": "100",
            "Close": "100", "Adj Close": "100", "Volume": "1000", "Dividends": "0.0", "Stock Splits": "0.0",
        })
    assert archived_split_factor(
        "AAPL", since=date(2026, 8, 9), as_of=as_of, history_dir=history
    ) is None


def test_a_failed_split_lookup_leaves_the_cursor_untouched():
    """Regression test for "Retain the split cursor when lookup fails". A
    prior version fell back to stamping today's date as the cursor whenever
    split_cursor_day was None (the archive was temporarily missing,
    unreadable, or stale) -- but that advanced the cursor past every row
    between the OLD cursor and today even though none of them were actually
    examined. Once the archive recovered, archived_split_factor's own
    `row_date <= since` exclusion then permanently skipped that unexamined
    window, missing any real split inside it.
    """
    resolved = {
        "status": STATUS_NONE,
        "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon",
        "rearm_reference_close": 100.0,
        "rearm_last_seen_day": DAY1.isoformat(),
    }
    day2 = date(2026, 8, 4)
    # split_factor=None mirrors what a caller passes when archived_split_
    # factor itself returned None (cannot confirm) -- split_cursor_day is
    # also None in that case, since the two are always returned together.
    state, step = _step(
        resolved, decision_price=49.0, previous_close=50.0,
        trading_day=day2, split_factor=None, split_cursor_day=None,
    )
    assert state["rearm_reference_close"] == pytest.approx(100.0)
    assert state["rearm_last_seen_day"] == DAY1.isoformat()  # unchanged, not day2


def test_a_cycle_resolving_on_its_own_start_day_seeds_the_cursor_before_it():
    """Regression test for "Seed the latch cursor before a split-day cycle
    starts". When a cycle's ``cycle_start_trading_day`` is missing (a
    legacy/corrupted-then-repaired tracker -- the same fail-closed fallback
    the abandonment and tier/basis-reset resolution sites already use), this
    call treats the resolution as though the cycle just started today:
    ``cycle_reference_close``/``reference_close`` falls back to THIS call's
    ``previous_close``, which belongs to the PRECEDING session. Seeding
    ``rearm_last_seen_day`` at ``trading_day`` itself -- rather than the day
    before it -- would let a split or distribution effective ON
    ``trading_day`` be excluded from every future ``archived_split_factor``
    lookup via `row_date <= since`, permanently rebasing the latch against a
    pre-action reference.
    """
    trading_day = date(2026, 8, 4)
    tracker_state = {
        "status": STATUS_DIPPING,
        "accumulated_usd": 15.0,
        "lowest_drop_seen": 8.0,
        "crossed_tier_thresholds": [1.0, 2.0, 4.0, 8.0],
        # A trading day well before `trading_day`, and no cycle_start_trading_day
        # at all -- so the same-day retry guard does not intercept this call,
        # and starting_a_new_cycle reads True from cycle_start_trading_day's
        # absence.
        "last_evaluated_trading_day": date(2020, 1, 1).isoformat(),
        "last_evaluated_stream": "afternoon",
    }
    # decision_price=95 against a reference_close/previous_close of 100 is a
    # 3pt recovery from the persisted 8.0 lowest_drop_seen -- comfortably past
    # the 1.0 recovery_offset -- an uncapped, broker-minimum-cleared full fire
    # that resolves this same call.
    state, step = _step(
        tracker_state, decision_price=95.0, previous_close=100.0,
        trading_day=trading_day, stream="afternoon",
    )
    assert step.fired
    assert step.reason != "tier_crossed"
    assert state["rearm_last_seen_day"] == (trading_day - timedelta(days=1)).isoformat()


@pytest.mark.parametrize("factor", [1.5, 2.0, 3.0, 7.0, 20.0, 100.0])
def test_a_confirmed_forward_split_rebases_the_latch(factor):
    """A confirmed k-for-1 divides the reference down onto post-split prices, so
    the latch keeps measuring the same real dip instead of stranding the ticker
    below a reference it can never reach again."""
    resolved = {
        "status": STATUS_NONE,
        "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon",
        "rearm_reference_close": 100.0,
        "rearm_last_seen_day": DAY1.isoformat(),
    }
    day2 = date(2026, 8, 4)
    post_split = 90.0 / factor
    # A real caller only ever gets a non-None split_factor alongside a
    # non-None split_cursor_day -- archived_split_factor returns them
    # together as one tuple -- so the cursor is supplied here too.
    state, step = _step(
        resolved, decision_price=post_split * 0.99, previous_close=post_split,
        trading_day=day2, split_factor=factor, split_cursor_day=day2,
    )
    assert state["rearm_reference_close"] == pytest.approx(100.0 / factor)
    assert state["rearm_last_seen_day"] == day2.isoformat()


@pytest.mark.parametrize("factor", [2.0, 5.0, 20.0])
def test_a_confirmed_reverse_split_rebases_instead_of_clearing(factor):
    """A 1-for-k records 1/k, so the same division scales the reference UP.
    Without it both closes jump above a pre-split reference, the latch reads
    that as recovery and clears, and the same descent is credited again."""
    resolved = {
        "status": STATUS_NONE,
        "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon",
        "rearm_reference_close": 100.0,
        "rearm_last_seen_day": DAY1.isoformat(),
    }
    day2 = date(2026, 8, 4)
    post_split = 90.0 * factor
    state, step = _step(
        resolved, decision_price=post_split * 0.99, previous_close=post_split,
        trading_day=day2, split_factor=1.0 / factor,
    )
    assert state["rearm_reference_close"] == pytest.approx(100.0 * factor)
    # The latch survives rebased, so the descent is not bought twice.
    assert step.reason == "awaiting_fresh_dip"
    assert step.newly_crossed_tiers == ()


@pytest.mark.parametrize("unconfirmed", [None, 1.0])
def test_an_unconfirmed_split_leaves_the_reference_untouched(unconfirmed):
    """``None`` (cannot confirm) and ``1.0`` (confirmed no corporate action)
    both rebase nothing. A violent ordinary move must never move the reference,
    which is what let a ~50% crash clear the latch under the old heuristic."""
    resolved = {
        "status": STATUS_NONE,
        "last_evaluated_trading_day": DAY1.isoformat(),
        "last_evaluated_stream": "afternoon",
        "rearm_reference_close": 100.0,
        "rearm_last_seen_day": DAY1.isoformat(),
    }
    day2 = date(2026, 8, 4)
    state, step = _step(
        resolved, decision_price=49.0, previous_close=50.0,
        trading_day=day2, split_factor=unconfirmed,
    )
    assert state["rearm_reference_close"] == pytest.approx(100.0)
    assert step.reason == "awaiting_fresh_dip"


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


def test_write_locked_discards_non_dict_event_entries_instead_of_raising(tmp_path):
    """Regression test for "Validate event entries before filtering them".
    _validate_tracker_payload() (see _read_locked) never validates the
    SHAPE of individual audit events -- they are best-effort, not trusted
    state a caller makes financial decisions from -- so a restored or
    hand-edited tracker can carry a syntactically valid ``events`` list
    with a non-object entry (e.g. a bare number) in it. The recovery-event
    eviction-exemption logic tests ``"recovery_metadata" not in
    candidate_event`` against every entry once the list reaches the cap,
    which raises TypeError for a non-dict value; that exception escaping
    this best-effort write would abort the ledger write (and, via
    record_step's caller, potentially the whole scheduled run) over a
    single malformed audit entry.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    # Enough dict events to reach the cap (the recovery-eviction-exemption
    # membership test only runs once combined_events exceeds
    # _MAX_PERSISTED_EVENTS), plus a few non-dict entries mixed in -- the
    # exact shape a restored/hand-edited tracker could carry.
    # The non-dict entries sit at the FRONT of the list -- the eviction loop
    # walks front-to-back and only tests "recovery_metadata" not in
    # candidate_event while it still needs to drop entries to reach the cap,
    # so a malformed entry must be among the earliest candidates considered
    # to actually reach that membership test.
    original = {
        "version": 1, "ticker": "GME", "status": "none",
        "last_evaluated_trading_day": DAY1.isoformat(),
        "events": [42, "not-an-event", None] + [{"reason": "no_action"}] * _MAX_PERSISTED_EVENTS,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(original), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, trading_day=DAY1)
    ledger.record_step("GME", state, step)  # must not raise TypeError
    written = json.loads((ticker_dir / "tracker.json").read_text(encoding="utf-8"))
    # The malformed entries are silently dropped before the cap is ever
    # applied, so the surviving list is bounded and entirely dict events.
    assert len(written["events"]) == _MAX_PERSISTED_EVENTS
    assert all(isinstance(e, dict) for e in written["events"])


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

    with pytest.raises(
        CorruptedLedgerStateError,
        match=r"under MSFT's directory.*records ticker 'AAPL'",
    ):
        ledger.read("MSFT")

    # Refusing the read is fail-closed: it must not rewrite or otherwise
    # "repair" the misplaced source balance into MSFT-owned state.
    assert (misft_dir / "tracker.json").read_text(encoding="utf-8") == aapl_path.read_text(encoding="utf-8")
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


@pytest.mark.parametrize(
    "field",
    ["accumulated_usd", "cycle_reference_close", "lowest_drop_seen", "fired_floor_drop_metric", "fired_floor_price"],
)
def test_ledger_raises_on_an_oversized_integer_field_instead_of_crashing(tmp_path, field):
    """Regression test for "Treat oversized ledger integers as corruption"
    (and, for ``fired_floor_price`` specifically, "Convert oversized
    fire-floor prices into ledger corruption").

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
    if field == "fired_floor_price":
        # fired_floor_price is only validated alongside a fired_floor_drop_
        # metric evaluation date pairing -- see
        # test_ledger_raises_on_fire_floor_metadata_missing_its_evaluation_date.
        payload["fired_floor_drop_metric"] = 4.0
        payload["last_evaluated_trading_day"] = DAY1.isoformat()
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


def _lease_fields(**overrides):
    """The recoverable-lease metadata near_close_dca.py's fire claim writes
    alongside settlement_pending=True. A pending flag is only valid when it
    carries enough information for a later invocation to reconcile the claim
    against broker order history -- see _validate_tracker_payload."""
    fields = {
        # The real deterministic format claim creation writes: "afternoon" is
        # the legacy default stream and carries no token of its own -- see
        # near_close_dca._graded_dca_ref_id_prefix and
        # dca_inverse_martingale._expected_lease_ref_id.
        "settlement_ref_id": f"graded-dca-imart-{DAY1.isoformat()}-GME",
        "settlement_trading_day": DAY1.isoformat(),
        "settlement_claimed_usd": 7.0,
        "settlement_target_state": {"status": "none", "last_evaluated_trading_day": DAY1.isoformat()},
        "settlement_live": True,
        # A real fingerprint's shape: 32 lowercase hex characters, exactly what
        # near_close_dca._broker_account_fingerprint produces. The 16-character
        # value this replaced predated the switch to a keyed HMAC and is a shape
        # no claim can write, so _validate_tracker_payload now rejects it.
        "settlement_account": "a1b2c3d4e5f6071829304a5b6c7d8e9f",
        # Which roster stream made the claim -- read back by recovery to decide
        # whether the recovered dollars belong to the afternoon-only kill-switch
        # figure.
        "settlement_stream": "afternoon",
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize("stream", [None, "morning", "midday", "afternoon"])
def test_lease_ref_id_matches_the_claim_sites_own_prefix_builder(stream):
    """Regression guard for "Validate the settlement ref against its lease
    fields": _validate_tracker_payload rebuilds the deterministic ref id with
    its OWN copy of near_close_dca._graded_dca_ref_id_prefix's format
    (duplicated rather than imported, to avoid a circular import). A
    validator that silently drifted from the real builder would reject every
    future legitimate lease, not just damaged ones -- so this asserts
    byte-for-byte agreement across every stream a claim can actually record,
    including the legacy (stream absent -> "afternoon") case.
    """
    import near_close_dca as ncd
    from dca_inverse_martingale import _expected_lease_ref_id

    claim_day = date(2026, 8, 3)
    real = f"{ncd._graded_dca_ref_id_prefix(stream or 'afternoon', 'imart')}-{claim_day.isoformat()}-GME"
    assert _expected_lease_ref_id(stream, claim_day.isoformat(), "GME") == real


@pytest.mark.parametrize("value", [True, False])
def test_ledger_accepts_a_real_boolean_settlement_pending(tmp_path, value):
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": "none", "settlement_pending": value}
    if value:
        payload.update(_lease_fields())
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["settlement_pending"] is value


def test_ledger_raises_on_a_bare_settlement_lease_with_no_recovery_metadata(tmp_path):
    """Regression test for "Reject leases on dipping trackers", restated in
    terms of the recoverable-lease schema that replaced its status-based
    formulation (near_close_dca.py's fire claim now leases the untouched
    PRE-fire state, which is routinely "dipping"). A restored or hand-edited
    tracker carrying settlement_pending with NO settlement_target_state has
    nothing to settle against, and every downstream call site's plain truthy
    settlement_pending check returns early before any age or abandonment
    logic runs -- permanently freezing the ticker's balance.
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


@pytest.mark.parametrize("status", ["none", "firing", "dipping"])
def test_ledger_accepts_a_fully_specified_settlement_lease(tmp_path, status):
    """Every status a real fire claim can lease -- including "dipping", the
    pre-fire status the recoverable claim deliberately preserves."""
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {"version": 1, "ticker": "GME", "status": status, "settlement_pending": True}
    payload.update(_lease_fields())
    if status in ("firing", "dipping"):
        payload.update({
            "accumulated_usd": 2.0, "crossed_tier_thresholds": [1.0, 2.0, 4.0],
            "cycle_reference_close": 100.0, "lowest_drop_seen": 8.5,
            "cycle_start_trading_day": DAY1.isoformat(),
            "trading_days_active": 3,
        })
    if status == "firing":
        payload["fire_attempts"] = 1
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["settlement_pending"] is True


@pytest.mark.parametrize(
    "account",
    [
        "unknown",  # the sentinel a live claim writes when it cannot compute one
        "a1b2c3d4e5f6071829304a5b6c7d8e9f",  # a real fingerprint's shape
        "00000000000000000000000000000000",
        "ffffffffffffffffffffffffffffffff",
    ],
)
def test_ledger_accepts_every_account_shape_a_claim_can_write(tmp_path, account):
    """The structural check must not over-tighten: rejecting a shape claim
    creation really does produce would raise CorruptedLedgerStateError on a
    perfectly healthy tracker and skip that ticker on every run -- trading one
    silent freeze for a noisy one. Both writable shapes are pinned here
    alongside the rejection cases below.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload: dict[str, Any] = {"version": 1, "ticker": "GME", "status": "none", "settlement_pending": True}
    payload.update(_lease_fields(settlement_account=account))
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    assert ledger.read("GME")["settlement_account"] == account


def test_a_real_fingerprint_always_satisfies_the_ledgers_own_check(tmp_path, monkeypatch):
    """The writer and the validator must agree by construction, not by a
    hand-copied format. Generates a fingerprint through the real code path and
    asserts the ledger's own predicate accepts it.
    """
    import near_close_dca as ncd
    from dca_inverse_martingale import _is_valid_lease_account

    class _Broker:
        def account_number(self):
            return "999000111"

        def available_buying_power(self) -> float:
            return 1000.0

    fingerprint = ncd._broker_account_fingerprint(_Broker(), tmp_path)
    assert fingerprint is not None
    assert _is_valid_lease_account(fingerprint)
    assert _is_valid_lease_account(ncd._LEASE_ACCOUNT_UNKNOWN)


@pytest.mark.parametrize(
    "overrides",
    [
        {"settlement_ref_id": None},
        {"settlement_ref_id": ""},
        {"settlement_ref_id": 12345},
        {"settlement_trading_day": "not-a-date"},
        {"settlement_trading_day": "2026-02-30"},
        {"settlement_trading_day": 20260629},
        {"settlement_claimed_usd": "corrupt"},
        {"settlement_claimed_usd": float("nan")},
        {"settlement_claimed_usd": -1.0},
        {"settlement_claimed_usd": True},
        {"settlement_claimed_usd": 10**400},
        {"settlement_target_state": "corrupt"},
        {"settlement_target_state": {"status": "bogus"}},
        {"settlement_target_state": {"status": "firing", "accumulated_usd": "corrupt"}},
        {"settlement_live": "false"},
        {"settlement_account": ""},
        {"settlement_account": 12345},
        # Regression cases for "Validate settlement account fingerprints
        # structurally". A claim can only ever write the "unknown" sentinel or a
        # 32-character lowercase hex HMAC, so every other nonempty string is
        # damage -- and one that merely LOOKS plausible is the dangerous kind,
        # since it compares unequal to a freshly computed fingerprint on every
        # run and freezes the ticker's accumulator silently forever.
        {"settlement_account": "garbage"},
        {"settlement_account": "a1b2c3d4e5f60718"},  # a pre-HMAC 16-char digest
        {"settlement_account": "A1B2C3D4E5F6071829304A5B6C7D8E9F"},  # uppercase
        {"settlement_account": "a1b2c3d4e5f6071829304a5b6c7d8e9"},  # one short
        {"settlement_account": "a1b2c3d4e5f6071829304a5b6c7d8e9ff"},  # one long
        {"settlement_account": "g1b2c3d4e5f6071829304a5b6c7d8e9f"},  # non-hex
        {"settlement_account": " a1b2c3d4e5f6071829304a5b6c7d8e9"},  # padded
        {"settlement_account": "unknown "},  # not the sentinel
        # Regression cases for "Reject unknown settlement stream values".
        # near_close_dca decides kill-switch eligibility by asking whether the
        # claim's stream is anything OTHER than morning or midday, so an
        # unrecognized value reads as afternoon and injects capital the
        # afternoon-only figure is defined to exclude.
        {"settlement_stream": "garbage"},
        {"settlement_stream": "Afternoon"},  # wrong case
        {"settlement_stream": "afternoon "},  # padded
        {"settlement_stream": ""},
        {"settlement_stream": 12345},
    ],
)
def test_ledger_raises_on_a_corrupted_settlement_lease_field(tmp_path, overrides):
    """Regression test for "Validate the newly persisted settlement fields on
    read". near_close_dca.py's recovery pass converts settlement_claimed_usd
    with a bare float() outside any exception handler, applies
    settlement_target_state verbatim as the ticker's next persisted state, and
    lets settlement_trading_day decide whether an unmatched lease may be
    released. Unvalidated, a damaged tracker aborted the whole scheduled run
    instead of raising here and skipping only this ticker -- or released/wrote
    an outright wrong balance.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload: dict[str, Any] = {"version": 1, "ticker": "GME", "status": "none", "settlement_pending": True}
    payload.update(_lease_fields(**overrides))
    payload = {k: v for k, v in payload.items() if v is not None}
    # NaN/Infinity are not valid JSON but json.dumps emits them by default; a
    # restored tracker written by a non-strict encoder can genuinely carry one.
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


def test_ledger_raises_on_an_oversized_crossed_tier_threshold(tmp_path):
    """Regression test for "Convert oversized crossed tiers into ledger
    corruption". json.loads() parses an arbitrarily large JSON integer into an
    unbounded Python int, which passes the isinstance() check but makes
    math.isfinite() RAISE OverflowError rather than return False. Uncaught, that
    escaped _validate_tracker_payload entirely instead of becoming
    CorruptedLedgerStateError, so the per-ticker recovery boundary at
    near_close_dca.py's injection site never caught it and one damaged tracker
    aborted the WHOLE scheduled run.
    """
    ticker_dir = tmp_path / "GME"
    ticker_dir.mkdir()
    payload = {
        "version": 1, "ticker": "GME", "status": "dipping", "accumulated_usd": 7.0,
        "crossed_tier_thresholds": [10**400], "cycle_reference_close": 100.0,
        "lowest_drop_seen": 1.0, "cycle_start_trading_day": DAY1.isoformat(),
        "trading_days_active": 1,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(payload), encoding="utf-8")
    ledger = InverseMartingaleLedger(root=tmp_path)
    with pytest.raises(CorruptedLedgerStateError):
        ledger.read("GME")


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


def test_ledger_retains_a_recovery_event_past_the_ordinary_event_cap(tmp_path):
    """Regression test for "Keep recovery attribution past the event cap". A
    prior-day or cross-stream crash-lease recovery's event is the ONLY
    durable claim-aware audit record left once a later resolved_marker()
    transition overwrites the top-level last_recovery field with something
    else -- see InverseMartingaleStep.recovery_metadata's own docstring.
    Evicting it the same way ordinary events are, once _MAX_PERSISTED_EVENTS
    ordinary evaluations have since been recorded, would silently lose the
    claim day, stream, ref, and order ID exactly as the pre-audit-event bug
    did. The recovery event must survive indefinitely; only ordinary events
    are evicted to make room for it.
    """
    ledger = InverseMartingaleLedger(root=tmp_path)
    state: dict = {}
    day = DAY1
    state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day)
    recovery_step = InverseMartingaleStep(
        status_before=step.status_before,
        status_after=step.status_after,
        accumulated_usd_before=step.accumulated_usd_before,
        accumulated_usd_after=step.accumulated_usd_after,
        newly_crossed_tiers=step.newly_crossed_tiers,
        fired=step.fired,
        fire_amount_usd=step.fire_amount_usd,
        reason=step.reason,
        recovery_metadata={
            "claim_trading_day": day.isoformat(),
            "claim_stream": "morning",
            "claim_ref": "abc123",
            "order_id": "order-1",
        },
    )
    ledger.record_step("AAPL", state, recovery_step)
    day = date.fromordinal(day.toordinal() + 1)
    # More than _MAX_PERSISTED_EVENTS further ordinary (non-recovery)
    # evaluations -- enough to evict the recovery event under the old,
    # unconditional [-_MAX_PERSISTED_EVENTS:] slice.
    for _ in range(_MAX_PERSISTED_EVENTS + 20):
        state, step = _step(state, decision_price=99.0, previous_close=100.0, trading_day=day)
        ledger.record_step("AAPL", state, step)
        day = date.fromordinal(day.toordinal() + 1)

    raw = json.loads((tmp_path / "AAPL" / "tracker.json").read_text(encoding="utf-8"))
    recovery_events = [e for e in raw["events"] if "recovery_metadata" in e]
    assert len(recovery_events) == 1
    assert recovery_events[0]["recovery_metadata"]["order_id"] == "order-1"
    # The overall list stays bounded at the cap -- the recovery event is
    # exempt from EVICTION, not from counting toward the total -- so it is
    # ordinary events that give up a slot to keep it, not the cap itself
    # growing without bound.
    assert len(raw["events"]) == _MAX_PERSISTED_EVENTS


def test_ledger_caps_the_event_history_even_when_all_events_are_recoveries(tmp_path):
    """Regression test for "Keep recovery retention bounded". A ticker with
    more crash-lease recoveries than _MAX_PERSISTED_EVENTS itself (repeated
    recovery failures) leaves every candidate carrying recovery_metadata --
    the "drop oldest ordinary event" loop alone can never find one to evict,
    so an unconditional exemption would let the persisted events list grow
    without bound, defeating the cap and making every read-modify-write
    progressively more expensive. Once ordinary events are exhausted, the
    OLDEST recovery events must give way too, keeping only the most recent
    _MAX_PERSISTED_EVENTS overall.
    """
    ticker_dir = tmp_path / "AAPL"
    ticker_dir.mkdir()
    day = DAY1
    recovery_events = []
    for i in range(_MAX_PERSISTED_EVENTS + 10):
        recovery_events.append({
            "status_before": "none", "status_after": "none",
            "accumulated_usd_before": 0.0, "accumulated_usd_after": 0.0,
            "newly_crossed_tiers": [], "fired": False, "fire_amount_usd": 0.0,
            "reason": "recovered",
            "recovery_metadata": {"claim_ref": f"claim-{i}"},
        })
        day = date.fromordinal(day.toordinal() + 1)
    original = {
        "version": 1, "ticker": "AAPL", "status": "none",
        "last_evaluated_trading_day": day.isoformat(),
        "events": recovery_events,
    }
    (ticker_dir / "tracker.json").write_text(json.dumps(original), encoding="utf-8")

    ledger = InverseMartingaleLedger(root=tmp_path)
    day = date.fromordinal(day.toordinal() + 1)
    state, step = _step({}, decision_price=99.0, previous_close=100.0, trading_day=day)
    ledger.record_step("AAPL", state, step)  # must not grow the list unbounded

    raw = json.loads((ticker_dir / "tracker.json").read_text(encoding="utf-8"))
    assert len(raw["events"]) == _MAX_PERSISTED_EVENTS
    # The newest recovery attribution (highest claim_ref index) survives
    # over the oldest -- the same most-recent-wins policy ordinary events get.
    recovery_refs = [e["recovery_metadata"]["claim_ref"] for e in raw["events"] if "recovery_metadata" in e]
    assert f"claim-{_MAX_PERSISTED_EVENTS + 9}" in recovery_refs
    assert "claim-0" not in recovery_refs


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


# --- transition compare-and-swap / fire claims --------------------------------

def test_compare_and_swap_transition_rejects_stale_non_fire_state(tmp_path):
    ledger = InverseMartingaleLedger(root=tmp_path)
    snapshot = ledger.read("AAPL")
    winner, winner_step = _step(snapshot, decision_price=99.0, previous_close=100.0)
    stale, stale_step = _step(snapshot, decision_price=98.0, previous_close=100.0)

    assert ledger.compare_and_swap_transition(
        "AAPL", expected_prior_state=snapshot, new_state=winner, step=winner_step
    ) is True
    assert ledger.compare_and_swap_transition(
        "AAPL", expected_prior_state=snapshot, new_state=stale, step=stale_step
    ) is False
    assert ledger.read("AAPL") == winner


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


def test_a_tier_config_change_preserves_a_partially_paid_fire_latch():
    """Regression test for "Preserve the paid-fire latch across tier
    configuration resets".

    "A reset cycle never spent any real money" holds for a STATUS_DIPPING cycle
    (see the two tests below) but NOT for a STATUS_FIRING one: a
    per_order_cap_usd-capped continuation places real, settled chunks over
    successive runs. Resetting that to a latch-free marker let a later stream
    open a brand-new cycle while price was still inside the original descent and
    credit the edited tiers again, duplicating exposure the earlier fills had
    already covered.
    """
    # A capped fire leaves a STATUS_FIRING remainder -- real money is already out.
    state, _ = _step({}, decision_price=91.5, previous_close=100.0)  # accum = $15
    state, step = _step(state, decision_price=97.0, previous_close=100.0, per_order_cap_usd=10.0)
    assert step.fired and state["status"] == STATUS_FIRING

    # Operator edits the tier table while that remainder is still outstanding.
    edited_tiers = ((1.1, 1.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0))
    day2 = date(2026, 8, 4)
    state, step = _step(
        state, decision_price=96.0, previous_close=100.0, trading_day=day2, tiers=edited_tiers,
    )
    assert step.reason == "cycle_reset_tier_config_changed"
    assert step.status_after == STATUS_NONE
    # The cycle is gone, but the descent it already paid for is still latched.
    assert state["rearm_reference_close"] == pytest.approx(100.0)
    assert state["fired_floor_price"] == pytest.approx(91.5)
    assert state["fired_floor_drop_metric"] == pytest.approx(8.5)

    # So a later day still inside that descent cannot re-credit the edited tiers.
    day3 = date(2026, 8, 5)
    state, step = _step(
        state, decision_price=96.0, previous_close=97.0, trading_day=day3, tiers=edited_tiers,
    )
    assert step.status_after == STATUS_NONE
    assert step.newly_crossed_tiers == ()


def test_a_basis_change_preserves_a_paid_fire_floor_as_an_absolute_price():
    """The same preservation across a BASIS change, where the persisted metric
    is denominated in the units being abandoned. The absolute price carries the
    floor instead -- re-persisting a percentage-point metric under a stddev
    config would hand the next evaluation exactly the mismatched-units reading
    the basis reset exists to prevent."""
    state, _ = _step({}, decision_price=91.5, previous_close=100.0, basis="pct")
    state, step = _step(state, decision_price=97.0, previous_close=100.0, basis="pct", per_order_cap_usd=10.0)
    assert state["status"] == STATUS_FIRING

    day2 = date(2026, 8, 4)
    state, step = _step(
        state, decision_price=96.0, previous_close=100.0, trading_day=day2,
        basis="stddev", stddev_reference=0.01,
    )
    assert step.reason == "cycle_reset_basis_changed"
    assert state["rearm_reference_close"] == pytest.approx(100.0)
    assert state["fired_floor_price"] == pytest.approx(91.5)
    # Deliberately absent: an old-basis metric would be misread under the new one.
    assert "fired_floor_drop_metric" not in state


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
