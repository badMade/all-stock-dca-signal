"""Tests for the opt-in whole-share accumulation module (see docs/whole_share_accumulation.md)."""

import pytest

from dca_whole_share_accumulator import (
    WholeShareAccumulationLedger,
    apply_contribution,
    compute_virtual_contribution_usd,
)
from near_close_dca import (
    BASE_NOT_BELOW_CLOSE,
    OrderFill,
    StrategyConfig,
    _reconcile_unresolved_whole_share_claim,
    _usable_positive_float,
)

# --- apply_contribution (pure decision function) -----------------------------


def test_contribution_below_share_price_stays_pending():
    decision = apply_contribution(0.0, 2.0, 10.0)

    assert decision.shares_to_buy == 0
    assert decision.order_notional_usd == 0.0
    assert decision.pending_after_usd == 2.0


def test_contribution_crossing_share_price_fires_one_share():
    decision = apply_contribution(8.0, 3.0, 10.0)

    assert decision.pending_before_usd == 8.0
    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0
    assert decision.pending_after_usd == 1.0


def test_exact_share_price_fires_with_zero_remainder():
    decision = apply_contribution(0.0, 10.0, 10.0)

    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0
    assert decision.pending_after_usd == 0.0


def test_large_contribution_can_fire_multiple_shares_at_once():
    decision = apply_contribution(0.0, 25.0, 10.0)

    assert decision.shares_to_buy == 2
    assert decision.order_notional_usd == 20.0
    assert decision.pending_after_usd == 5.0


def test_zero_contribution_is_a_no_op_when_still_short():
    decision = apply_contribution(4.0, 0.0, 10.0)

    assert decision.shares_to_buy == 0
    assert decision.pending_after_usd == 4.0


@pytest.mark.parametrize("bad_price", [0.0, -5.0, float("nan"), float("inf")])
def test_nonpositive_or_non_finite_share_price_raises(bad_price):
    with pytest.raises(ValueError):
        apply_contribution(0.0, 1.0, bad_price)


def test_sub_cent_share_price_raises_instead_of_dividing_by_zero():
    """Codex review ("Reject prices that quantize to zero"): a positive,
    finite price below half a cent passes the initial check but rounds to
    $0.00, which would otherwise raise ZeroDivisionError inside the floor
    division rather than a clear ValueError.
    """
    with pytest.raises(ValueError):
        apply_contribution(0.0, 1.0, 0.001)


def test_negative_pending_or_contribution_raises():
    with pytest.raises(ValueError):
        apply_contribution(-1.0, 1.0, 10.0)
    with pytest.raises(ValueError):
        apply_contribution(1.0, -1.0, 10.0)


# --- compute_virtual_contribution_usd ----------------------------------------


def test_no_buy_above_prior_close_yields_zero_contribution():
    config = StrategyConfig()
    amount, reason = compute_virtual_contribution_usd("BYDDY", 10.5, 10.0, 9.5, config)

    assert amount == 0.0
    assert reason == BASE_NOT_BELOW_CLOSE


def test_shallow_drop_below_minimum_tier_yields_zero_contribution():
    config = StrategyConfig()
    # A drop of a hundredth of a percent clears "below close" but not the
    # first ladder tier.
    amount, reason = compute_virtual_contribution_usd("BYDDY", 9.999, 10.0, 9.5, config)

    assert amount == 0.0
    assert reason == BASE_NOT_BELOW_CLOSE


def test_drop_below_close_and_low_uses_the_two_dollar_base():
    config = StrategyConfig()
    # 5% below both prior close and prior low -> comfortably past the first
    # tier, base_amount_and_low ($2) scale.
    amount, reason = compute_virtual_contribution_usd("BYDDY", 9.5, 10.0, 9.6, config)

    assert amount > 0
    assert reason == "below_prior_close_and_low"


def test_drop_below_close_only_uses_the_one_dollar_base():
    config = StrategyConfig()
    # Below close but not below the (higher) prior low -> reduced $1 scale.
    amount, reason = compute_virtual_contribution_usd("BYDDY", 9.5, 10.0, 9.0, config)

    assert amount > 0
    assert reason == "below_prior_close_only"


def test_missing_prior_low_is_treated_conservatively_as_not_below_low():
    config = StrategyConfig()
    amount, reason = compute_virtual_contribution_usd("BYDDY", 9.0, 10.0, None, config)

    assert amount > 0
    assert reason == "below_prior_close_only"


def test_nonpositive_previous_close_never_produces_a_contribution():
    config = StrategyConfig()
    amount, reason = compute_virtual_contribution_usd("BYDDY", 1.0, 0.0, None, config)

    assert amount == 0.0
    assert reason == BASE_NOT_BELOW_CLOSE


# --- WholeShareAccumulationLedger --------------------------------------------


def test_ledger_persists_pending_balance_across_calls(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)

    first = ledger.record_contribution("byddy", "2026-08-16", 3.0, 10.0, stream="morning")
    assert first.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 3.0

    second = ledger.record_contribution("BYDDY", "2026-08-17", 4.0, 10.0, stream="midday")
    assert second.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 7.0

    third = ledger.record_contribution("BYDDY", "2026-08-18", 5.0, 10.0, stream="afternoon")
    assert third.shares_to_buy == 1
    assert third.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 2.0


def test_ledger_survives_across_separate_instances_reading_the_same_root(tmp_path):
    ledger_a = WholeShareAccumulationLedger(tmp_path)
    ledger_a.record_contribution("BYDDY", "2026-08-16", 6.0, 10.0)

    ledger_b = WholeShareAccumulationLedger(tmp_path)
    assert ledger_b.pending_usd("BYDDY") == 6.0


def test_idempotency_key_prevents_double_counting_a_retry(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    key = "morning_dca/20260816_1030ET"

    first = ledger.record_contribution("BYDDY", "2026-08-16", 3.0, 10.0, idempotency_key=key)
    second = ledger.record_contribution("BYDDY", "2026-08-16", 3.0, 10.0, idempotency_key=key)

    # A below-threshold contribution is resolved immediately (nothing to
    # settle/preserve), so the retry hits the resolved-entry branch: same
    # shares/notional/pending as the first call, but contribution_usd is
    # 0.0 -- this retry call itself contributed nothing new (Codex review:
    # "Report zero contribution for resolved slot retries"), unlike the
    # ORIGINAL call, which genuinely did.
    assert first.contribution_usd == 3.0
    assert second.contribution_usd == 0.0
    assert first.shares_to_buy == second.shares_to_buy == 0
    assert first.pending_after_usd == second.pending_after_usd == 3.0
    assert ledger.pending_usd("BYDDY") == 3.0


def test_different_idempotency_keys_both_apply(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)

    ledger.record_contribution("BYDDY", "2026-08-16", 3.0, 10.0, idempotency_key="slot-a")
    ledger.record_contribution("BYDDY", "2026-08-16", 3.0, 10.0, idempotency_key="slot-b")

    assert ledger.pending_usd("BYDDY") == 6.0


def test_preserve_pending_adds_the_deducted_amount_to_the_current_balance(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("BYDDY", "2026-08-16", 8.0, 10.0)

    decision = ledger.record_contribution("BYDDY", "2026-08-17", 5.0, 10.0, idempotency_key="slot-1")
    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 3.0

    # Placement was blocked this run (e.g. buying power) -- the caller
    # restores what THIS fire deducted (order_notional_usd=10), added to
    # the current balance (3), not an absolute recomputed total.
    restored = ledger.preserve_pending("BYDDY", "2026-08-17", 10.0, idempotency_key="slot-1")
    assert restored == 13.0
    assert ledger.pending_usd("BYDDY") == 13.0


def test_preserve_pending_does_not_clobber_a_concurrent_contribution(tmp_path):
    """Codex review ("Preserve concurrent contributions instead of
    overwriting them"): a fire from $8 + $5 leaves $3 pending; a concurrent
    $4 contribution (a different stream, different idempotency key) raises
    that to $7 before this fire's own restoration runs. Restoring must land
    on $7 + $10 = $17, not clobber the ledger back down to the original $13.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("BYDDY", "2026-08-16", 8.0, 10.0)
    decision = ledger.record_contribution("BYDDY", "2026-08-17", 5.0, 10.0, idempotency_key="slot-1")
    assert decision.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 3.0

    # A concurrent contribution from a different stream lands first.
    ledger.record_contribution("BYDDY", "2026-08-17", 4.0, 10.0, idempotency_key="slot-2")
    assert ledger.pending_usd("BYDDY") == 7.0

    restored = ledger.preserve_pending("BYDDY", "2026-08-17", 10.0, idempotency_key="slot-1")
    assert restored == 17.0
    assert ledger.pending_usd("BYDDY") == 17.0


def test_retry_after_preserve_pending_returns_zero_shares_not_the_original_fire(tmp_path):
    """Codex review ("Reconcile cached fire decisions before returning
    them"): a scheduler retry using the SAME idempotency key after
    preserve_pending() must not receive the original positive
    shares_to_buy again -- that balance is already back in the pool and
    would otherwise be spent a second time by a caller trusting the cache.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("BYDDY", "2026-08-16", 8.0, 10.0)
    first = ledger.record_contribution("BYDDY", "2026-08-17", 5.0, 10.0, idempotency_key="slot-1")
    assert first.shares_to_buy == 1
    ledger.preserve_pending("BYDDY", "2026-08-17", 10.0, idempotency_key="slot-1")

    retry = ledger.record_contribution("BYDDY", "2026-08-17", 5.0, 10.0, idempotency_key="slot-1")
    assert retry.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 13.0  # unchanged by the retry
    # Codex review ("Report zero contribution for resolved slot retries"):
    # this retry made NO new contribution -- the ledger already reflects the
    # original $5 (now preserved back into the pool). Reporting the original
    # $5 again would make a caller double-report it as newly accumulated.
    assert retry.contribution_usd == 0.0


def test_overlapping_call_for_an_unresolved_fire_does_not_get_the_same_actionable_decision(tmp_path):
    """Codex review ("Claim unresolved fires before returning cached
    decisions"): two overlapping scheduler attempts for the SAME
    idempotency key, before either calls settle()/preserve_pending(), must
    not both receive the positive fire as independently actionable -- both
    could otherwise submit the quantity order, or both restore the same
    deduction and inflate pending_usd.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.shares_to_buy == 1

    # A second, overlapping process calls with the SAME key moments later,
    # well within the claim's reservation window -- must get "nothing to do"
    # instead of the same actionable fire.
    overlapping = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:05-04:00"
    )
    assert overlapping.shares_to_buy == 0
    # The ledger still reflects only the FIRST call's deduction -- the
    # second caller made no independent write.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_overlapping_call_with_the_same_recorded_at_cannot_share_the_fire_claim(tmp_path):
    """Scheduler attempts can share an exact slot timestamp.

    Claim ownership is established by the first locked write, not by seeing a
    different ``recorded_at`` value.  A second process presenting the same
    timestamp must therefore still receive a non-actionable cached decision.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"

    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    overlapping = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )

    assert first.shares_to_buy == 1
    assert overlapping.shares_to_buy == 0
    assert overlapping.order_notional_usd == 0.0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_a_stale_unresolved_claim_is_reclaimed_by_a_later_retry(tmp_path):
    """A genuinely abandoned claim (e.g. the original caller crashed before
    settling) must not permanently strand the deducted pending balance -- a
    retry attempted well after the claim window elapses reclaims the SAME
    cached fire so it can still be completed.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.shares_to_buy == 1

    # Retried 10 minutes later (well past UNRESOLVED_CLAIM_STALE_SECONDS) --
    # the original caller is presumed abandoned, so this reclaims the fire.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )
    assert retry.shares_to_buy == 1
    assert retry.order_notional_usd == 10.0


def test_still_claimed_is_true_immediately_after_recording(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:30:00-04:00") is True


def test_an_actively_claimed_overlapping_retry_reports_zero_contribution(tmp_path):
    """Codex review ("Report zero contribution for actively claimed
    retries"): an overlapping retry within UNRESOLVED_CLAIM_STALE_SECONDS
    (still reserved by the original caller, not yet stale) must report
    contribution_usd=0.0 -- this invocation mutates nothing -- mirroring the
    resolved-entry branch's own identical fix. Reporting the ORIGINAL
    positive amount would let a caller (see _process_whole_share_candidate)
    double-report that contribution in run artifacts while the original
    invocation is separately still processing the actionable fire.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.contribution_usd == 10.0
    assert first.shares_to_buy == 1

    # Retried 1 minute later -- well within the claim window, still reserved.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert retry.contribution_usd == 0.0
    assert retry.shares_to_buy == 0


def test_cached_fire_decision_reports_zero_contribution_while_still_claimed(tmp_path):
    """Same fix as above, for the read-only preview counterpart."""
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:31:00-04:00"  # 1 minute later -- not stale
    )
    assert decision.contribution_usd == 0.0
    assert decision.shares_to_buy == 0


def test_still_claimed_is_false_after_a_stale_reclaim(tmp_path):
    """Codex review ("Fence the previous owner before reclaiming stale
    fires"): elapsed time alone does not prove the original claimant
    exited, so a caller MUST re-verify still_claimed immediately before
    the irreversible placement step. Once a later retry reclaims the fire,
    the ORIGINAL caller's own recorded_at must no longer read as current.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", original_recorded_at) is True

    # A retry reclaims the fire 10 minutes later (past the stale window).
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # The ORIGINAL caller, resurfacing with its own original recorded_at,
    # must find it no longer holds the claim.
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", original_recorded_at) is False
    # The reclaiming caller's own recorded_at is the current claim.
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00") is True


def test_still_claimed_is_false_once_resolved(tmp_path):
    """Codex review ("Reject resolved fire claims before placement"): a
    resolved entry (settled or preserved) means the fire is already
    finalized -- by anyone -- so NO caller, including the very one who held
    the original claim, may place again. The old "resolved -> True" behavior
    let a reclaiming retry settle a fire while the original caller was
    paused mid-broker-call; the original would then pass this check (since
    resolved alone used to mean "not blocked") and submit the same quantity
    a second time.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.settle("BYDDY", "2026-08-16", 1, 10.0, idempotency_key="slot-1", recorded_at=recorded_at)

    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", recorded_at) is False


def test_cached_fire_decision_is_none_when_never_recorded(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    assert ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:30:00-04:00") is None


def test_cached_fire_decision_is_zero_shares_once_resolved(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.settle("BYDDY", "2026-08-16", 1, 10.0, idempotency_key="slot-1", recorded_at=recorded_at)

    decision = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", recorded_at)
    assert decision.shares_to_buy == 0
    assert decision.order_notional_usd == 0.0
    # Codex review ("Report zero contribution for resolved slot retries"):
    # mirrors record_contribution's own fix for this preview path too.
    assert decision.contribution_usd == 0.0


def test_a_zero_contribution_retry_redispatches_a_preserved_fire(tmp_path):
    """Codex review ("Allow same-day redispatch of preserved fires"): a
    fire blocked by a transient gate (ceiling/cap/spread/review) and
    restored via preserve_pending() marks its slot resolved -- but the
    restored balance is real money still sitting in pending_usd. A SAME-day
    zero-contribution retry for the SAME idempotency slot (e.g. the
    preserved-fire redispatch loop, once the earlier gate clears) must not
    be flattened to "nothing to do" forever just because this exact slot
    already resolved once -- if the restored balance ALONE now covers a
    share, this is a genuinely new, actionable fire.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert first.shares_to_buy == 1
    # Blocked by a transient gate -- restores the deducted $10 and marks
    # this slot resolved.
    ledger.preserve_pending("BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at)
    assert ledger.pending_usd("BYDDY") == 10.0

    redispatch_recorded_at = "2026-08-16T15:35:00-04:00"
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at=redispatch_recorded_at
    )
    assert redispatch.shares_to_buy == 1
    assert redispatch.order_notional_usd == 10.0
    assert redispatch.contribution_usd == 0.0
    assert redispatch.pending_after_usd == 0.0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_a_nonzero_contribution_retry_does_not_redispatch_a_resolved_slot(tmp_path):
    """The redispatch fix above is scoped to ZERO-contribution retries only
    -- a genuine fresh BUY signal (nonzero contribution_usd) against an
    already-resolved slot must keep the pre-existing "nothing new, $0
    contribution" behavior, since re-opening a resolved slot for new money
    risks double-counting the original contribution.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending("BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at)
    assert ledger.pending_usd("BYDDY") == 10.0

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 5.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert retry.shares_to_buy == 0
    assert retry.contribution_usd == 0.0
    assert ledger.pending_usd("BYDDY") == 10.0  # untouched by this retry


def test_cached_fire_decision_reflects_a_redispatchable_preserved_fire(tmp_path):
    """Same fix as above, for the read-only preview counterpart."""
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending("BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at)

    decision = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0)
    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0


def test_cached_fire_decision_does_not_redispatch_a_resolved_slot_for_a_fresh_buy(tmp_path):
    """Codex review ("Distinguish fresh BUY retries in cached previews"): a
    preview of a GENUINE fresh BUY (contribution_usd positive) against an
    already-resolved slot must NOT redispatch -- the real
    record_contribution call for that same retry would not reopen the
    slot either (redispatch is scoped to zero-contribution callers only,
    to avoid double-counting a fresh contribution).
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending("BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at)
    assert ledger.pending_usd("BYDDY") == 10.0

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0, 5.0
    )
    assert decision.shares_to_buy == 0
    assert decision.contribution_usd == 0.0


def test_unresolved_claims_any_day_finds_a_claim_from_an_earlier_trading_day(tmp_path):
    """Codex review ("Reconcile unresolved claims across trading days"): a
    claim left unresolved on one trading day must still be discoverable by
    a LATER day's run, which passes a brand-new trading_day it was never
    recorded under.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00"
    )

    found = ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")
    assert len(found) == 1
    trading_day, claim = found[0]
    assert trading_day == "2026-08-15"
    assert claim["shares_to_buy"] == 1

    # Not found under the WRONG trading day, and unresolved_claim (the
    # single-day counterpart) doesn't see it either.
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


def test_unresolved_claims_any_day_returns_empty_once_resolved(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-15T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    ledger.settle("BYDDY", "2026-08-15", 1, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at)

    assert ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY") == []


def test_unresolved_claims_any_day_returns_every_match_sorted_oldest_first(tmp_path):
    """Codex review ("Reconcile every unresolved claim instead of only the
    first"): a ticker can accumulate MORE THAN ONE unresolved claim over
    time -- an older placement genuinely rejected (never gets an exact
    ref-id match) alongside a LATER ambiguous placement that was actually
    accepted. Returning only the first would let the older, irreconcilable
    entry starve the later one from ever being checked.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-14", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at="2026-08-14T15:30:00-04:00"
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00"
    )

    found = ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")
    assert [trading_day for trading_day, _ in found] == ["2026-08-14", "2026-08-15"]


def test_reconcile_unresolved_whole_share_claim_uses_the_original_claim_timestamp(tmp_path):
    """Codex review ("Reconcile using the original claim timestamp"):
    settle()'s ownership guard only reconciles (marks resolved, restores
    any shortfall) when the passed recorded_at matches the cache entry's
    OWN claimed_at -- a fresh timestamp from the reconciling call itself
    would silently fail that guard, still bump shares_purchased_total, and
    leave the claim (and any shortfall) unresolved forever.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=original_recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")
    assert unresolved["claimed_at"] == original_recorded_at

    class _MatchingBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-1", "state": "filled", "quantity": "1"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _MatchingBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled == {"order_id": "RH-1", "shares_purchased": 1, "order_notional_usd": 10.0}
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None  # now resolved
    assert ledger.pending_usd("BYDDY") == 0.0


def test_reconcile_unresolved_whole_share_claim_requires_an_exact_ref_id_match(tmp_path):
    """Codex review ("Require an exact order match before settling"): a
    broad "some same-day order exists" signal is not enough -- only an
    EXACT ref_id match proves THIS claim's own order landed. A completely
    unrelated live order (a manual trade, or a different stream's own
    fire) must not settle this claim.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _NoExactMatchBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # some OTHER, unrelated order exists -- not this claim's

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _NoExactMatchBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None  # still unresolved


def test_reconcile_unresolved_whole_share_claim_defers_a_still_live_order(tmp_path):
    """Codex review ("Defer settling recovered orders until they are
    terminal"): when the exact matching order is still queued or
    working (not yet terminal), its final filled quantity is not yet
    known. An earlier fix reserved credit for the order's FULL submitted
    quantity and settled immediately -- but that still marks the claim
    resolved before it is actually true: if the working remainder is
    later cancelled or rejected, there is no unresolved claim left to
    revisit, so the ledger permanently overstates purchased shares while
    withholding the unfilled credit forever. The claim must stay reserved
    (unresolved, untouched) until a LATER reconciliation attempt finds it
    in a terminal state.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _StillQueuedBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-QUEUED-1", "state": "queued", "cumulative_quantity": "0", "quantity": "1"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _StillQueuedBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    # Codex review ("Book live orders discovered during claim recovery"):
    # a live/working match is real evidence of a broker attempt, so the
    # caller must still be told to book it as submitted/unfilled -- but the
    # LEDGER claim itself stays exactly as reserved/unresolved as before.
    assert reconciled == {
        "live_order_found": True,
        "order_id": "RH-QUEUED-1",
        "planned_notional_usd": 10.0,
        "planned_quantity": 1,
    }
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None  # still unresolved
    assert ledger.pending_usd("BYDDY") == 0.0  # still deducted, not restored either


def test_reconcile_unresolved_whole_share_claim_settles_a_terminal_filled_order(tmp_path):
    """Companion to the deferred-settlement test above: once the matched
    order actually reaches a TERMINAL "filled" state, reconciliation
    proceeds normally using its cumulative_quantity.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _FilledBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-FILLED-1", "state": "filled", "cumulative_quantity": "1"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _FilledBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled == {"order_id": "RH-FILLED-1", "shares_purchased": 1, "order_notional_usd": 10.0}
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.pending_usd("BYDDY") == 0.0


def test_reconcile_unresolved_whole_share_claim_settles_a_partial_fill_before_cancellation(tmp_path):
    """Codex review ("Reconcile partial fills before releasing canceled
    claims"): the live-only ref-id lookup excludes a genuinely TERMINAL
    order (cancelled/rejected/expired -- see RobinhoodBroker's own
    _LIVE_ORDER_STATES comment: "never consumed capacity"). An order that
    PARTIALLY filled and was THEN cancelled is real, already-executed
    capital -- the live-only lookup's clean "no match" must not be treated
    as proof nothing happened. The terminal-inclusive lookup
    (same_day_order_with_ref_id_prefix_any_state) must be checked, and a
    positive filled quantity settled for real, using the broker's own
    average_price for the booked notional rather than the stale cached
    signal-time price.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    # $20 pending at a cached $10.00/share price plans 2 shares.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _PartialFillThenCanceledBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # terminal orders are excluded from the live-only lookup

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Only 1 of the planned 2 shares filled before cancellation, at a
            # real fill price ABOVE the $10.00 cached signal-time price.
            return {
                "id": "RH-PARTIAL-THEN-CANCELED-1",
                "state": "cancelled",
                "cumulative_quantity": "1",
                "average_price": "10.50",
            }

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _PartialFillThenCanceledBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    # The booked notional uses the REAL $10.50 fill price, not the stale
    # $10.00 cached price.
    assert reconciled["order_id"] == "RH-PARTIAL-THEN-CANCELED-1"
    assert reconciled["shares_purchased"] == 1
    assert reconciled["order_notional_usd"] == 10.50
    # Codex review ("Reuse known fill data when logging recovered claims"):
    # the broker's own average_price/executed quantity are carried through
    # as known_fill so the caller can log this execution without a separate
    # (potentially eventually-consistent) get_order_fill() re-query.
    assert reconciled["known_fill"] == OrderFill(avg_fill_price=10.50, quantity_filled=1)
    assert ledger.shares_purchased_total("BYDDY") == 1
    # settle()'s own shortfall math (computed from the CACHED price/share
    # counts, not the real fill price) releases the 1 unfilled share's
    # $10.00 cached price back into pending_usd.
    assert ledger.pending_usd("BYDDY") == 10.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


def test_reconcile_unresolved_whole_share_claim_defers_a_still_live_any_state_fallback_match(tmp_path):
    """Codex review ("Verify terminal state after the any-state fallback"):
    the terminal-inclusive any-state fallback carries no state filter of its
    own, so it can surface a LIVE order that only became visible in the
    moment between the live-only lookup (which found nothing) and the
    any-state lookup (which then does). That order's cumulative_quantity is
    not yet final -- settling a positive value now would resolve the claim
    while the order can still fill further; releasing a zero value would
    hand back the full reservation while the order may still fill later.
    Only a genuinely terminal state (or "filled") is authoritative; a still-
    live match found via the any-state fallback must leave the claim
    reserved exactly like the live-only branch's own nonterminal guard.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _RaceConditionStillLiveBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup ran before the broker indexed the order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # The SAME order is now visible, still working -- not terminal.
            return {"id": "RH-RACE-1", "state": "confirmed", "cumulative_quantity": "0"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _RaceConditionStillLiveBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None  # still unresolved
    assert ledger.pending_usd("BYDDY") == 0.0  # still deducted, not restored either


def test_reconcile_unresolved_whole_share_claim_falls_back_to_quantity_for_a_filled_any_state_match(tmp_path):
    """Codex review ("Use requested quantity for any-state filled orders"):
    when the any-state fallback finds the order EXPLICITLY "filled" (not
    merely non-fill-terminal like cancelled/rejected), it is exactly as
    authoritative as the live matched_state == "filled" branch, which
    already falls back to "quantity" when cumulative_quantity is absent. An
    earlier version of this fix restricted the any-state fallback to
    cumulative_quantity ALONE regardless of state, so a filled order that
    only carries "quantity" (no cumulative_quantity) left filled_shares at
    0 and silently restored the FULL claim as if nothing executed --
    letting its already-spent credit be reused.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _FilledViaAnyStateNoCumulativeQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup missed the (eventually consistent) order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Filled, but the response only carries "quantity", not
            # "cumulative_quantity".
            return {"id": "RH-FILLED-ANY-STATE-1", "state": "filled", "quantity": "1"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _FilledViaAnyStateNoCumulativeQuantityBroker(), "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-16", unresolved,
    )

    assert reconciled == {"order_id": "RH-FILLED-ANY-STATE-1", "shares_purchased": 1, "order_notional_usd": 10.0}
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


def test_reconcile_unresolved_whole_share_claim_does_not_fall_back_to_quantity_for_a_canceled_any_state_match(
    tmp_path,
):
    """Companion to the test above: a genuinely non-fill terminal state
    (cancelled/rejected/expired) found via the any-state fallback must
    still NOT fall back to "quantity" -- that field is merely what was
    requested, not what executed, for a non-filled order (Codex review:
    "Do not treat requested quantity as a terminal fill", preserved by the
    new filled-only fallback).
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _CanceledViaAnyStateNoCumulativeQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-CANCELED-ANY-STATE-1", "state": "cancelled", "quantity": "1"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _CanceledViaAnyStateNoCumulativeQuantityBroker(), "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-16", unresolved,
    )

    # No cumulative_quantity, and "quantity" is not trusted for a
    # non-filled terminal order -- nothing settled.
    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_reconcile_unresolved_whole_share_claim_does_not_treat_requested_quantity_as_filled(tmp_path):
    """Codex review ("Do not treat requested quantity as a terminal
    fill"): when an exact-ref terminal cancelled/rejected order omits
    cumulative_quantity but still carries its requested `quantity` field,
    that requested size must NOT be treated as executed -- unlike the
    matched_state == "filled" branch (where a fallback to `quantity` is
    legitimate, since the order is already known to be fully filled), a
    non-filled terminal order's own `quantity` is merely what was
    submitted, not what actually executed.

    A later round ("Keep terminal claims reserved when fill quantity is
    absent") further tightened what happens next: an absent
    cumulative_quantity proves NOTHING about what executed (a rejected
    order can still have partially filled first), so the claim must stay
    reserved for later reconciliation rather than being released as if a
    zero fill were confirmed.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _RejectedNoCumulativeQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # No cumulative_quantity at all -- only the REQUESTED quantity,
            # which must not be mistaken for what actually executed.
            return {"id": "RH-REJECTED-NO-FILL-INFO-1", "state": "rejected", "quantity": "2"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _RejectedNoCumulativeQuantityBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16",
        unresolved,
    )

    # Nothing was settled, and nothing was released either -- an unknown
    # quantity leaves the claim exactly as record_contribution left it.
    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_usable_positive_float_rejects_non_finite_values():
    """Codex review ("Reject non-finite recovered fill prices"): a
    parseable but non-finite value (e.g. "Infinity"/"NaN") must be
    rejected -- infinity IS greater than zero, so a bare `value > 0` check
    alone would let it through and compute an infinite order notional.
    """
    assert _usable_positive_float("Infinity") is None
    assert _usable_positive_float("-Infinity") is None
    assert _usable_positive_float("NaN") is None
    assert _usable_positive_float(float("inf")) is None
    assert _usable_positive_float(float("nan")) is None
    # Still accepts a genuine finite positive value.
    assert _usable_positive_float("10.50") == 10.50
    # Still rejects a finite non-positive value.
    assert _usable_positive_float("0") is None
    assert _usable_positive_float("-5.0") is None
    assert _usable_positive_float(None) is None
    assert _usable_positive_float("not-a-number") is None


def test_reconcile_unresolved_whole_share_claim_releases_a_confirmed_zero_fill_cancellation_immediately(tmp_path):
    """Companion to the partial-fill test above: a terminal order that
    executed NOTHING (a clean rejection/cancellation before any fill) is
    definitive evidence -- unlike the "found nothing at all" case, there is
    no need to wait for the claim to go stale before releasing its credit.

    A later round ("Count pre-roll zero-fill orders toward the attempt
    cap") changed the return contract for this specific case: it no longer
    returns plain ``None`` (nothing to book at all) but a distinct
    ``{"zero_fill_attempt": True, "order_id"}`` shape, so the caller can
    still count this REAL broker attempt toward `max_orders_per_day`
    without misreporting it as a settled purchase.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    # Claimed just now -- nowhere near UNRESOLVED_CLAIM_STALE_SECONDS.
    recorded_at = "2026-08-16T15:44:59-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _ZeroFillRejectedBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-REJECTED-1", "state": "rejected", "cumulative_quantity": "0"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _ZeroFillRejectedBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved,
        now_iso="2026-08-16T15:45:00-04:00",  # 1 second after claimed_at -- not stale
    )

    # A real attempt to book (order id, not a settlement) -- NOT None, and
    # NOT a settled-order shape (no shares_purchased/order_notional_usd).
    assert reconciled == {"zero_fill_attempt": True, "order_id": "RH-REJECTED-1"}
    assert ledger.shares_purchased_total("BYDDY") == 0
    # Released immediately despite not being stale -- the terminal
    # confirmation is definitive.
    assert ledger.pending_usd("BYDDY") == 10.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


def test_reconcile_unresolved_whole_share_claim_leaves_the_claim_reserved_when_terminal_quantity_is_absent(
    tmp_path,
):
    """Codex review ("Keep terminal claims reserved when fill quantity is
    absent"): a cancelled/rejected/expired order that OMITS
    cumulative_quantity entirely is not the same as one that explicitly
    reports zero -- the field's absence proves nothing about what actually
    executed (a cancelled order may have partially filled first). The old
    behavior defaulted filled_shares to 0 for BOTH cases and released the
    full reservation either way, which can drop real, already-spent
    capital's evidence. Only an EXPLICIT parseable zero may release the
    claim; an absent quantity must leave it reserved for later
    reconciliation.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:44:59-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _TerminalNoQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Terminal, but no cumulative_quantity field at all.
            return {"id": "RH-CANCELLED-1", "state": "cancelled"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _TerminalNoQuantityBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved,
        now_iso="2026-08-16T15:45:00-04:00",  # 1 second after claimed_at -- not stale
    )

    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0
    # NOT released -- the claim is left exactly as record_contribution left it.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_reconcile_unresolved_whole_share_claim_leaves_the_claim_untouched_when_terminal_lookup_raises(tmp_path):
    """Codex review ("Preserve claims when terminal-history lookup fails"):
    a RAISED terminal-inclusive lookup establishes NEITHER outcome -- it is
    NOT proof the order was rejected. An earlier version of this fix caught
    the exception and set terminal_order=None, which then fell through to
    the SAME staleness-release branch a clean "found nothing" no-match
    uses -- for a stale claim whose order genuinely partially filled and
    then cancelled, a transient failure on this second history request
    would restore the ENTIRE reservation even though shares executed,
    letting that spent credit fund another purchase. The claim must be
    left exactly as record_contribution left it.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    # Claimed well past UNRESOLVED_CLAIM_STALE_SECONDS -- stale enough that
    # the OLD behavior would have released it on a fallthrough.
    recorded_at = "2026-08-16T15:00:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _TerminalLookupRaisesBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # cleanly no live match

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            raise RuntimeError("order history temporarily unavailable")

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _TerminalLookupRaisesBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved,
        now_iso="2026-08-16T15:45:00-04:00",  # well past staleness
    )

    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0
    # NOT released -- left exactly as record_contribution left it.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_cached_fire_decision_is_zero_shares_while_still_within_claim_window(tmp_path):
    """Codex review ("Account for actionable stale claims in tier
    planning"): an unresolved fire still within UNRESOLVED_CLAIM_STALE_SECONDS
    is reserved for whoever recorded it -- a real record_contribution() call
    right now would return zero shares (not reclaim), so the preview must
    match that.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:31:00-04:00"  # 1 minute later -- not stale
    )
    assert decision.shares_to_buy == 0
    assert decision.order_notional_usd == 0.0


def test_cached_fire_decision_returns_the_positive_decision_once_stale(tmp_path):
    """Codex review ("Account for actionable stale claims in tier
    planning"): an unresolved fire PAST UNRESOLVED_CLAIM_STALE_SECONDS is
    reclaimable -- a real record_contribution() call right now would
    reclaim it and return its cached POSITIVE decision, so the preview must
    reflect that actionable outcome, not a flat zero.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00"  # 10 minutes later -- stale
    )
    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0


def test_reclaiming_a_stale_fire_reprices_against_the_current_quote(tmp_path):
    """Codex review ("Reprice reclaimed stale fires before placement"): a
    reclaimed stale fire must be re-priced against the CURRENT quote, not
    the original attempt's stale cached price. Without this, a fire claimed
    at $10 and reclaimed after the price moved to $20 would still hand back
    a decision sized for the stale $10 price -- and the caller passes that
    STALE price straight into _whole_share_risk_gates for the
    max_per_order_usd ceiling check, letting a one-share order that
    actually costs ~$20 pass a $10-per-order ceiling as if it only cost $10.

    $10 pending + $10 contribution = $10.00 committed total at the original
    $10 claim (1 share, $10 notional, $0 remainder). Reclaiming at a
    doubled $20 price must recompute from that SAME $10 total: 0 shares fit
    now, not the stale cached 1.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    decision = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        20.0,  # doubled price
        idempotency_key="slot-1",
        recorded_at="2026-08-16T15:40:00-04:00",  # 10 minutes later -- stale
    )

    assert decision.current_share_price_usd == 20.0
    assert decision.shares_to_buy == 0  # $10 total no longer covers a $20 share
    assert decision.order_notional_usd == 0.0
    assert decision.pending_after_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 10.0


def test_reclaiming_a_stale_fire_appends_an_event(tmp_path):
    """Codex review ("Record stale-claim repricing in the ledger event
    history"): a stale-claim reclaim mutates the cached quantity, notional,
    resolution state, and global pending balance exactly like a fresh
    contribution does, but the original fix only appended to state["events"]
    on the fresh-contribution branch. events() and the persisted audit
    history then retained only the original stale fire, with nothing
    explaining the later balance adjustment or repriced decision -- making
    operator reconciliation of the recovery path incorrect.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert len(ledger.events("BYDDY")) == 1

    ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        20.0,  # doubled price
        idempotency_key="slot-1",
        recorded_at="2026-08-16T15:40:00-04:00",  # 10 minutes later -- stale
    )

    events = ledger.events("BYDDY")
    assert len(events) == 2
    reclaim_event = events[1]
    assert reclaim_event["decision"] == "reclaim_reprice"
    assert reclaim_event["current_share_price_usd"] == 20.0
    assert reclaim_event["shares_to_buy"] == 0
    assert reclaim_event["pending_after_usd"] == 10.0


def test_reclaiming_a_stale_fire_preserves_newer_activity_recorded_under_another_key(tmp_path):
    """Codex review ("Preserve newer ledger activity while repricing stale
    claims"): the repricing fix above must reconcile against the ledger's
    CURRENT balance, not overwrite it with an absolute snapshot recomputed
    solely from the stale entry's own original total -- mirroring
    preserve_pending's own established convention.

    A $15 fire at $10 leaves $5 pending. A LATER $4 contribution under a
    DIFFERENT key advances the balance to $9. Reclaiming the first fire at a
    doubled $20 price must write $9 + ($15 repriced remainder - $5 original
    remainder) = $19, not a bare $15 that silently discards the intervening
    $4 contribution.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 15.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 5.0  # $15 total, 1 share @ $10, $5 remainder

    # A later, different-key contribution advances the balance independently.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 1_000_000.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 9.0

    # Reclaim the FIRST fire, 10+ minutes after its own claim, at a doubled price.
    reclaim_decision = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        20.0,
        idempotency_key="slot-1",
        recorded_at="2026-08-16T15:41:00-04:00",
    )

    assert ledger.pending_usd("BYDDY") == 19.0
    # Codex review ("Return the reconciled pending balance after a stale
    # reprice"): the RETURNED decision must report the same reconciled $19
    # just persisted above, not the isolated repriced.pending_after_usd
    # ($15, computed from the frozen $15 total alone) -- a caller (see
    # _process_whole_share_candidate) persists this field as
    # whole_share_pending_usd, so returning the pre-reconciliation value
    # would report a stale balance to run artifacts.
    assert reclaim_decision.pending_after_usd == 19.0


def test_a_claim_repriced_to_zero_shares_is_resolved_and_cannot_re_spend_consumed_credit(tmp_path):
    """Codex review ("Prevent old claims from re-spending consumed credit"):
    a stale claim repriced UPWARD to 0 shares restores its committed dollars
    into the global pending balance via the delta fix above, but if that
    entry stays unresolved, a LATER stale-reclaim of the SAME key recomputes
    apply_contribution from the same frozen pending_before_usd/
    contribution_usd, treating those dollars as still owed even after a
    DIFFERENT key has since spent the restored balance down -- driving
    pending_usd negative.

    $0 pending + $10 contribution @ $10 = 1 share, $0 remainder (fire
    committed). Reclaimed 10+ minutes later at a doubled $20: 0 shares fit
    the $10 total now, so the $10 is restored to pending_usd (delta fix) --
    this entry must become RESOLVED, since nothing is left to place or
    settle. A different key (slot-2) then contributes $0 at $10/share,
    consuming the restored $10 down to a single share and $0 remainder. A
    SECOND stale reclaim of the ORIGINAL key (back at the original $10
    price, 10+ minutes after ITS reprice) must return the already-resolved,
    zero-shares "nothing to do" decision reflecting the CURRENT ($0)
    balance -- not recompute and restore another $10 that was never really
    there, which would leave pending_usd at $-10.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 0.0  # 1 share bought, no remainder

    # Reclaim slot-1 stale, 10+ minutes later, at a doubled price -- 0 shares
    # fit the frozen $10 total now, so it's restored to pending_usd.
    first_reclaim = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        20.0,
        idempotency_key="slot-1",
        recorded_at="2026-08-16T15:41:00-04:00",
    )
    assert first_reclaim.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 10.0

    # A different key consumes the restored balance back down to $0.
    slot_2_decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:45:00-04:00"
    )
    assert slot_2_decision.shares_to_buy == 1
    assert ledger.pending_usd("BYDDY") == 0.0

    # A SECOND stale reclaim attempt on the ORIGINAL (now-resolved) key, back
    # at the original $10 price, 10+ minutes after its own reprice -- must
    # NOT reclaim and restore another $10 that was already spent by slot-2.
    second_reclaim = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        10.0,
        idempotency_key="slot-1",
        recorded_at="2026-08-16T15:52:00-04:00",
    )
    assert second_reclaim.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_a_downward_reprice_that_would_overdraw_the_pool_defers_instead_of_going_negative(tmp_path):
    """Codex review ("Prevent downward repricing from overdrawing pending
    credit"): a stale claim's reclaim total is recomputed from the ORIGINAL
    frozen pending_before_usd/contribution_usd, which assumes the full
    original remainder is still exclusively this claim's. But if a
    DIFFERENT key already spent part of that shared pool (funded in part by
    this claim's original remainder), a downward reprice (price fell) can
    demand MORE dollars back than the pool currently holds -- applying the
    delta anyway would drive pending_usd negative and hand back an
    actionable decision that spends more than what remains.

    A $15 fire at $10 leaves $5 pending (1 share, $10 notional). A different
    key's later $7 contribution advances the balance to $12 total, buying
    another share at $10 and leaving $2. Reclaiming the FIRST fire's stale
    claim at a DROPPED $5 price would recompute the frozen $15 total as 3
    shares/$15 notional/$0 remainder -- a $-5 delta that would leave
    pending_usd at $2 + (-5) = $-3. The reclaim must instead defer: return
    the same "nothing to do" decision, leaving pending_usd at $2 and the
    claim untouched.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 15.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 5.0  # 1 share @ $10, $5 remainder

    ledger.record_contribution(
        "BYDDY", "2026-08-16", 7.0, 10.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 2.0  # $5 + $7 = $12, 1 more share @ $10, $2 remainder

    # Reclaim the FIRST fire, 10+ minutes after its own claim, at a DROPPED price.
    deferred = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        5.0,
        idempotency_key="slot-1",
        recorded_at="2026-08-16T15:41:00-04:00",
    )

    assert deferred.shares_to_buy == 0
    assert deferred.order_notional_usd == 0.0
    assert ledger.pending_usd("BYDDY") == 2.0  # untouched, never went negative


def test_cached_fire_decision_reprices_a_stale_claim_when_given_a_price(tmp_path):
    """Same repricing fix, applied to the read-only preview: a caller with a
    live quote on hand must see the SAME repriced decision a real reclaim
    would produce, not the stale cached share count. Omitting the price
    (the pre-existing contract) still returns the stale cached decision
    unchanged, for a caller with no live price context.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    stale_recorded_at = "2026-08-16T15:40:00-04:00"
    without_price = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", stale_recorded_at)
    assert without_price.shares_to_buy == 1  # unchanged, stale cached price

    with_price = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", stale_recorded_at, 20.0)
    assert with_price.shares_to_buy == 0  # repriced against the doubled quote
    assert with_price.current_share_price_usd == 20.0


def test_cached_fire_decision_reprice_reports_the_reconciled_balance(tmp_path):
    """Codex review ("Return the reconciled balance from stale-claim
    previews"): mirrors record_contribution's own reconciliation fix,
    applied to the read-only preview. A $15 fire at $10 leaves $5 pending.
    A LATER $4 contribution under a DIFFERENT key advances the balance to
    $9. Previewing a reclaim of the FIRST fire at a doubled $20 -- exactly
    what a real record_contribution() call would return and persist -- must
    report $9 + ($15 repriced remainder - $5 original remainder) = $19, not
    the isolated repriced remainder alone ($15), which a real reclaim would
    never actually leave in the ledger.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 15.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 5.0

    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 1_000_000.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 9.0

    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:41:00-04:00", 20.0
    )

    assert preview.pending_after_usd == 19.0
    # Read-only -- the ledger itself is untouched by a preview call.
    assert ledger.pending_usd("BYDDY") == 9.0


def test_record_contribution_rejects_a_path_traversal_ticker(tmp_path):
    """Codex review ("Validate whole-share symbols before using them as
    paths"): a malformed ticker symbol (path separators, "..") must never
    reach ``self.root / symbol`` -- StrategyConfig.whole_share_tickers isn't
    schema-validated at every entry point (an alternate $DCA_CONFIG_DIR or
    an explicit whole_share_tickers= override bypasses
    schemas/tickers.schema.json entirely), so this ledger must defend
    itself against a symbol that could resolve outside its own artifact
    tree.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    with pytest.raises(ValueError):
        ledger.record_contribution(
            "../../outside", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
        )
    # Nothing should have been written outside the ledger's own root.
    assert not (tmp_path.parent / "outside").exists()


def test_record_contribution_rejects_a_dots_only_ticker(tmp_path):
    """Codex review ("Require a letter in whole-share symbols"): the
    symbol pattern's bare "[A-Z.]{1,6}" character class also accepts a
    symbol made ENTIRELY of dots (e.g. ".." or "."), which
    self.root / symbol / "ledger.json" still resolves as a real
    filesystem path component -- ".." specifically writes the ledger and
    its lock in the PARENT directory instead of under this ledger's own
    whole-share stream tree, risking collisions with unrelated artifacts.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    with pytest.raises(ValueError):
        ledger.record_contribution(
            "..", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
        )
    with pytest.raises(ValueError):
        ledger.record_contribution(
            ".", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
        )
    # Nothing should have been written outside the ledger's own root.
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path.parent / "ledger.json").exists()


def test_still_claimed_is_true_without_an_idempotency_key(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    assert ledger.still_claimed("BYDDY", "2026-08-16", None, None) is True


def test_preserve_pending_refuses_to_credit_a_fenced_out_caller(tmp_path):
    """Codex review ("Reject resolutions from callers that lost the
    claim"): a caller whose claim was reclaimed by a newer overlapping
    attempt must not be able to restore its deduction after the fact --
    that money belongs to the reclaiming attempt's fire now, and crediting
    it back here would double-count once that attempt settles or is itself
    preserved.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert ledger.pending_usd("BYDDY") == 0.0

    # A retry reclaims the fire 10 minutes later (past the stale window).
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # The ORIGINAL (now fenced-out) caller tries to restore its deduction.
    restored = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )

    assert restored == 0.0
    assert ledger.pending_usd("BYDDY") == 0.0  # unchanged -- no double-credit


def test_preserve_pending_refuses_to_credit_an_already_resolved_fire(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.settle("BYDDY", "2026-08-16", 1, 10.0, idempotency_key="slot-1", recorded_at=recorded_at)
    assert ledger.pending_usd("BYDDY") == 0.0

    # A second, redundant preserve_pending() call for the SAME already-
    # settled fire must not credit anything back either.
    restored = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )

    assert restored == 0.0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_only_reclaiming_caller_can_preserve_and_resolve_a_stale_fire(tmp_path):
    """A stale owner cannot restore funds or resolve the retry's claim."""
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    retry_recorded_at = "2026-08-16T15:40:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=retry_recorded_at
    )

    stale_result = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert stale_result == 0.0
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", retry_recorded_at) is True

    retry_result = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=retry_recorded_at
    )
    assert retry_result == 10.0
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", retry_recorded_at) is False

    # Resolution cannot be replayed to restore the deduction a second time.
    assert (
        ledger.preserve_pending(
            "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=retry_recorded_at
        )
        == 10.0
    )


def test_settle_bumps_shares_purchased_total(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-a")

    assert ledger.shares_purchased_total("BYDDY") == 0
    ledger.settle("BYDDY", "2026-08-16", 1, 10.0, stream="morning", idempotency_key="slot-a")
    assert ledger.shares_purchased_total("BYDDY") == 1

    ledger.record_contribution("BYDDY", "2026-08-17", 10.0, 10.0, idempotency_key="slot-b")
    ledger.settle("BYDDY", "2026-08-17", 1, 10.0, stream="midday", idempotency_key="slot-b")
    assert ledger.shares_purchased_total("BYDDY") == 2


def test_settle_with_a_clamped_smaller_notional_auto_restores_the_shortfall(tmp_path):
    """Codex review ("Restore unsubmitted notional during settlement"):
    settle() must restore the unfilled portion itself when idempotency_key
    identifies the original fire, not require the caller to separately call
    preserve_pending() for the shortfall.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    # Two shares' worth accumulated and deducted...
    decision = ledger.record_contribution("BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1")
    assert decision.shares_to_buy == 2
    assert decision.order_notional_usd == 20.0
    assert ledger.pending_usd("BYDDY") == 0.0

    # ...but only 1 share actually got placed (e.g. a downstream cap) -- the
    # $10 shortfall is restored automatically.
    ledger.settle("BYDDY", "2026-08-16", 1, 10.0, stream="morning", idempotency_key="slot-1")
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.pending_usd("BYDDY") == 10.0


def test_settle_shortfall_uses_the_ledgers_own_rounded_price_not_submitted_notional(tmp_path):
    """Codex review ("Restore clamped credit using the ledger price"): the
    shortfall must be derived from the CACHED share count and the ledger's
    own cent-rounded price, not from `planned_notional - submitted_notional_usd`
    -- a caller computing submitted_notional_usd from a raw, un-rounded
    price would otherwise corrupt the shortfall by the rounding difference.

    $20 pending at a raw $0.996 price rounds to $1.00 in the ledger and
    deducts 20 shares ($20.00). Clamping submission to 10 shares must
    restore exactly $10.00 (10 unbought shares * the $1.00 ledger price),
    even if the caller (incorrectly) reports a submitted_notional_usd
    computed from the raw $0.996 price ($9.96, which would otherwise
    produce a wrong $10.04 restoration).
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    decision = ledger.record_contribution("BYDDY", "2026-08-16", 20.0, 0.996, idempotency_key="slot-1")
    assert decision.current_share_price_usd == 1.00  # rounded to the cent
    assert decision.shares_to_buy == 20
    assert decision.order_notional_usd == 20.00
    assert ledger.pending_usd("BYDDY") == 0.0

    # Caller clamps to 10 shares but (incorrectly) computes the submitted
    # notional from the raw, un-rounded price -- settle() must not trust it
    # for the shortfall math.
    ledger.settle("BYDDY", "2026-08-16", 10, 9.96, stream="morning", idempotency_key="slot-1")

    assert ledger.shares_purchased_total("BYDDY") == 10
    assert ledger.pending_usd("BYDDY") == 10.00  # not 10.04


def test_settle_from_a_fenced_out_caller_does_not_resolve_the_new_owners_claim(tmp_path):
    """Codex review ("Reject settlements from callers that lost the
    claim"): a caller whose claim was reclaimed by a newer overlapping
    attempt (after this caller's own still_claimed() check but before this
    settle() call -- a real, if narrow, race) must not credit a shortfall
    or mark the cached entry resolved -- both belong to whichever attempt
    currently owns the claim. The real shares this caller placed are still
    recorded (a genuine order happened), just not the cached-entry mutation.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert decision.shares_to_buy == 2

    # A retry reclaims the fire 10 minutes later (past the stale window) --
    # simulating a reclaim that happened after the original caller's own
    # still_claimed() pre-placement check passed, but before this settle().
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # The ORIGINAL (now fenced-out) caller settles with its stale recorded_at.
    ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1", recorded_at=original_recorded_at
    )

    # Real shares were placed, so the audit count still reflects them...
    assert ledger.shares_purchased_total("BYDDY") == 2
    # ...but no shortfall was credited and the entry was not resolved by the
    # stale caller -- the reclaiming attempt still owns this fire's claim.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00") is True
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", original_recorded_at) is False


def test_settle_with_require_claim_ownership_skips_all_mutation_when_fenced_out(tmp_path):
    """Codex review ("Update ledger totals only for the winning
    reconciliation"): a RECONCILIATION caller (settle() called with
    require_claim_ownership=True) that loses the claim-ownership race must
    not mutate ANYTHING -- not shares_purchased_total, not the audit event
    -- unlike a genuine in-line placement caller (the default,
    require_claim_ownership=False, exercised by the sibling test above),
    which always counts a real order it just placed. Without this, two
    concurrent reconcilers discovering the SAME physical broker order would
    double-count it in the ledger's own audit trail even though only one
    of them settles' cached-entry mutation actually resolves the claim.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # The ORIGINAL (now fenced-out) reconciler settles with require_claim_
    # ownership=True and its stale recorded_at.
    resolved = ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1",
        recorded_at=original_recorded_at, require_claim_ownership=True,
    )

    assert resolved is False
    # NOTHING was mutated -- unlike the unconditional-counting default
    # behavior, shares_purchased_total stays at 0.
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00") is True


def test_settle_without_idempotency_key_does_not_auto_restore(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("BYDDY", "2026-08-16", 20.0, 10.0)

    ledger.settle("BYDDY", "2026-08-16", 1, 10.0, stream="morning")
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.pending_usd("BYDDY") == 0.0  # no idempotency_key -> no planned amount to reconcile against


def test_events_records_full_decision_history(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("BYDDY", "2026-08-16", 3.0, 10.0, stream="morning", recorded_at="10:30")
    ledger.record_contribution("BYDDY", "2026-08-17", 8.0, 10.0, stream="midday", recorded_at="12:30")

    events = ledger.events("BYDDY")
    assert len(events) == 2
    assert events[0]["decision"] == "pending"
    assert events[0]["stream"] == "morning"
    assert events[1]["decision"] == "fire"
    assert events[1]["stream"] == "midday"


def test_unknown_ticker_has_zero_pending_and_no_events(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)

    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.events("BYDDY") == []


def test_symbol_lookup_is_case_insensitive(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("byddy", "2026-08-16", 3.0, 10.0)

    assert ledger.pending_usd("BYDDY") == 3.0
    assert ledger.pending_usd("byddy") == 3.0


def test_blank_ticker_string_raises(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    with pytest.raises(ValueError):
        ledger.record_contribution("   ", "2026-08-16", 3.0, 10.0)
