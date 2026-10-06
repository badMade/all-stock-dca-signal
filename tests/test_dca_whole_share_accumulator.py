"""Tests for the opt-in whole-share accumulation module (see docs/whole_share_accumulation.md)."""

import json
from typing import Any, cast

import pytest

from dca_whole_share_accumulator import (
    WholeShareAccumulationLedger,
    WholeShareClaimPersistenceError,
    _claim_credit_remainder_usd,
    _snapshot_key,
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


def test_null_claim_remainder_snapshot_is_written_off():
    cached = {
        "claim_credit_remainder_usd": 4.0,
        "claim_credit_remainder_as_of_withdrawn_usd": None,
    }

    assert _claim_credit_remainder_usd(cached, 4.0, 10.0) == 0.0

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
    restored = ledger.preserve_pending(
        "BYDDY", "2026-08-17", 10.0, idempotency_key="slot-1", claim_token=decision.claim_token
    )
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

    restored = ledger.preserve_pending(
        "BYDDY", "2026-08-17", 10.0, idempotency_key="slot-1", claim_token=decision.claim_token
    )
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
    ledger.preserve_pending("BYDDY", "2026-08-17", 10.0, idempotency_key="slot-1", claim_token=first.claim_token)

    retry = ledger.record_contribution("BYDDY", "2026-08-17", 5.0, 10.0, idempotency_key="slot-1")
    assert retry.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 13.0  # unchanged by the retry
    # Codex review ("Report zero contribution for resolved slot retries"):
    # this retry made NO new contribution -- the ledger already reflects the
    # original $5 (now preserved back into the pool). Reporting the original
    # $5 again would make a caller double-report it as newly accumulated.
    assert retry.contribution_usd == 0.0


def test_retry_after_settle_returns_zero_shares_not_the_original_fire(tmp_path):
    """A settled fire is final, so its cached decision is not actionable.

    This is the successful-order counterpart to the preserve_pending()
    regression above: replaying the original positive decision after settle()
    would tell a scheduler retry to submit the same whole-share purchase again.
    """
    # Codex review ("Make the retry distinguish settled from unresolved
    # claims"): all three calls previously omitted recorded_at, so this test
    # passed even if settle() failed to mark the cached fire resolved -- an
    # unresolved entry with claimed_at=None also returns zero shares because
    # _claim_is_stale(None, None) is always false. Pin the initial claim and
    # settle() to the same timestamp, then retry well past
    # UNRESOLVED_CLAIM_STALE_SECONDS so a stale-but-unresolved claim (which
    # would be reclaimed as actionable) cannot produce the same zero-share
    # result as a genuinely settled one -- only settle() having resolved the
    # claim explains the assertions below.
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-17", 13.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-17T15:30:00-04:00"
    )
    assert first.shares_to_buy == 1
    ledger.settle(
        "BYDDY",
        "2026-08-17",
        1,
        10.0,
        idempotency_key="slot-1",
        recorded_at="2026-08-17T15:30:00-04:00",
        claim_token=first.claim_token,
    )

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-17", 13.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-17T15:36:00-04:00"
    )

    assert retry.shares_to_buy == 0
    assert retry.order_notional_usd == 0.0
    assert retry.pending_after_usd == 3.0
    assert ledger.pending_usd("BYDDY") == 3.0  # unchanged by the retry
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_retry_immediately_reclaims_an_unresolved_fire(tmp_path):
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

    # A retry inside the canonical scheduler window can recover immediately;
    # execute_claimed() fences the former owner at placement time.
    overlapping = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:05-04:00"
    )
    assert overlapping.shares_to_buy == 1
    # The ledger still reflects only the FIRST call's deduction -- the
    # second caller made no independent write.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_overlapping_call_with_the_same_recorded_at_reclaims_the_fire_claim(tmp_path):
    """Scheduler attempts can share an exact slot timestamp.

    PR #515 ("Fix whole-share retry ownership and fill settlement") replaced
    the wall-clock ``UNRESOLVED_CLAIM_STALE_SECONDS`` lease that used to make
    an overlapping ``record_contribution`` call within the claim window a
    non-actionable no-op: an unresolved, not-yet-``placed`` fire is now
    reclaimed IMMEDIATELY by ANY overlapping caller, including one presenting
    the identical ``recorded_at`` -- this call cannot itself distinguish "a
    second overlapping attempt" from "a genuine retry of a crashed caller"
    from timestamps alone, so it no longer tries to. Real ownership safety
    for the one truly irreversible step (placement) comes from
    :meth:`WholeShareAccumulationLedger.execute_claimed` holding the
    per-ticker cross-process lock across both its own ownership check and the
    broker call itself (see that method's own docstring and
    ``test_execute_claimed_rejects_a_caller_whose_claim_was_reclaimed``
    below), not from anything ``record_contribution`` does on its own.
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
    assert overlapping.shares_to_buy == 1
    assert overlapping.order_notional_usd == 10.0
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

    # A later retry also reclaims the fire.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )
    assert retry.shares_to_buy == 1
    assert retry.order_notional_usd == 10.0


def test_still_claimed_is_true_immediately_after_recording(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:30:00-04:00", decision.claim_token
    ) is True


def test_an_overlapping_retry_immediately_reclaims_and_reports_the_reclaimed_contribution(tmp_path):
    """PR #515 ("Fix whole-share retry ownership and fill settlement")
    removed the wall-clock ``UNRESOLVED_CLAIM_STALE_SECONDS`` lease this test
    used to exercise (a retry within that window used to get a flat
    zero-shares "still reserved" no-op). An unresolved, not-yet-``placed``
    fire is now reclaimed immediately regardless of elapsed time, repriced
    against the reclaiming call's own ``current_share_price_usd`` -- here the
    price is unchanged, so the reclaimed decision reports the SAME
    ``contribution_usd``/``shares_to_buy`` the original call recorded, not a
    flat zero. See ``dca_whole_share_accumulator.record_contribution``'s own
    "Reclaim immediately" comment for why real ownership safety now lives in
    ``execute_claimed`` instead of here.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.contribution_usd == 10.0
    assert first.shares_to_buy == 1

    # Retried 1 minute later -- immediately reclaimed, no wall-clock wait.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert retry.contribution_usd == 10.0
    assert retry.shares_to_buy == 1


def test_cached_fire_decision_previews_the_immediate_reclaim(tmp_path):
    """Same PR #515 behavior change as above, for the read-only preview
    counterpart: an unresolved, not-yet-``placed`` entry is now ALWAYS
    previewed as immediately reclaimable, with no wall-clock staleness gate.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:31:00-04:00"  # 1 minute later -- reclaimed immediately
    )
    assert decision is not None
    assert decision.contribution_usd == 10.0
    assert decision.shares_to_buy == 1


def test_still_claimed_is_false_after_a_stale_reclaim(tmp_path):
    """Codex review ("Fence the previous owner before reclaiming stale
    fires"): elapsed time alone does not prove the original claimant
    exited, so a caller MUST re-verify still_claimed immediately before
    the irreversible placement step. Once a later retry reclaims the fire,
    the ORIGINAL caller's own recorded_at must no longer read as current.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    original = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", original_recorded_at, original.claim_token
    ) is True

    # A retry reclaims the fire 10 minutes later (past the stale window).
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    assert retry.claim_token != original.claim_token
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", original_recorded_at, original.claim_token
    ) is False
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00", retry.claim_token
    ) is True


def test_reclaim_fences_owner_when_both_attempts_have_the_same_recorded_at(tmp_path):
    """A logical run timestamp is not an ownership token.

    Scheduled retries can share ``ctx.now``.  Even in that case, replacing
    the persisted random token must fence the first attempt before placement.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    original = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )

    # Force the lease to appear stale while retaining the same logical time
    # that both scheduled attempts will present.
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    state["recorded_idempotency_keys"]["2026-08-16:slot-1"]["claimed_at"] = "2026-08-16T15:20:00-04:00"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )

    assert retry.claim_token != original.claim_token
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", recorded_at, original.claim_token) is False
    assert ledger.still_claimed("BYDDY", "2026-08-16", "slot-1", recorded_at, retry.claim_token) is True


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
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.settle(
        "BYDDY", "2026-08-16", 1, 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=decision.claim_token,
    )

    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", recorded_at, decision.claim_token
    ) is False


def test_cached_fire_decision_is_none_when_never_recorded(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    assert ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:30:00-04:00") is None


def test_cached_fire_decision_is_zero_shares_once_resolved(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.settle(
        "BYDDY", "2026-08-16", 1, 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )

    decision = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", recorded_at)
    assert decision is not None
    assert decision.shares_to_buy == 0
    assert decision.order_notional_usd == 0.0
    # Codex review ("Report zero contribution for resolved slot retries"):
    # mirrors record_contribution's own logic for this preview path too.
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
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
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
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
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
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )

    decision = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0)
    assert decision is not None
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
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0, 5.0
    )
    assert decision is not None
    assert decision.shares_to_buy == 0
    assert decision.contribution_usd == 0.0


def _preserved_fire_with_unrelated_pending(tmp_path):
    """A preserved $10 fire whose restored credit sits in a pool that ALSO
    holds $0.60 of unrelated, later contribution.

    The unrelated contribution is recorded against a deliberately high share
    price so it cannot fire on its own key and leaves the whole $10.60 pending.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert first.shares_to_buy == 1
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    unrelated = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.60, 20.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:32:00-04:00"
    )
    assert unrelated.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 10.60
    return ledger


def test_cached_fire_decision_mirrors_the_restored_credit_cap(tmp_path):
    """Codex review discussion_r3849268544 ("Mirror the restored-credit cap in
    cached previews"): record_contribution() sizes a zero-contribution
    redispatch off the claim's OWN restored credit, so a preserved fire whose
    restored credit alone falls short of the current share price returns zero
    shares even when unrelated pending contributions push the pool balance over
    it. A preview that still applied apply_contribution() to the whole pending
    balance reported an order the real call would skip -- phantom planned
    deployment that can select a higher budget tier and authorize unrelated
    spending.
    """
    ledger = _preserved_fire_with_unrelated_pending(tmp_path)
    # $10.50 a share: the $10.00 restored credit is short, the $10.60 pool is not.
    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.50
    )
    assert preview is not None
    assert preview.shares_to_buy == 0
    assert preview.order_notional_usd == 0.0

    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.50, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert live.shares_to_buy == preview.shares_to_buy
    assert ledger.pending_usd("BYDDY") == 10.60  # untouched


def test_cached_fire_decision_reports_the_full_pool_balances_for_a_capped_redispatch(tmp_path):
    """The cap governs SIZING only. When the restored credit does cover a
    share, the preview must report the same shares/notional AND the same
    full-pool pending_before/pending_after that record_contribution() rewrites
    onto its own decision -- otherwise the preview misstates the post-fire
    balance even though it agrees on the order.
    """
    ledger = _preserved_fire_with_unrelated_pending(tmp_path)
    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0
    )
    assert preview is not None
    assert preview.shares_to_buy == 1
    assert preview.order_notional_usd == 10.0
    assert preview.pending_before_usd == 10.60
    assert preview.pending_after_usd == 0.60

    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert live.shares_to_buy == preview.shares_to_buy
    assert live.order_notional_usd == preview.order_notional_usd
    assert live.pending_before_usd == preview.pending_before_usd
    assert live.pending_after_usd == preview.pending_after_usd
    assert ledger.pending_usd("BYDDY") == 0.60


def test_a_below_threshold_accumulation_redispatches_on_a_price_drop(tmp_path):
    """Codex review ("Preserve zero-share contributions for same-slot
    redispatch"): a genuinely FRESH contribution that never reaches the share
    price is created already resolved (nothing to place or settle), with
    order_notional_usd zero BY CONSTRUCTION -- not because an order was
    preserved. _redispatchable_credit_usd()'s missing-field fallback used to
    read that zero unconditionally, capping a same-slot zero-contribution
    retry at nothing even though the entry's real accumulated contribution
    sits, aged, in claim_credit_remainder_usd. A $5 contribution at $10/share
    stays pending; a later price drop to $4/share makes that same $5 actionable,
    but the un-fixed cap stranded it forever.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "XYZ", "2026-08-16", 5.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.shares_to_buy == 0
    assert ledger.pending_usd("XYZ") == 5.0

    preview = ledger.cached_fire_decision(
        "XYZ", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 4.0
    )
    assert preview is not None
    assert preview.shares_to_buy == 1
    assert preview.order_notional_usd == 4.0

    redispatch = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 4.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert redispatch.shares_to_buy == 1
    assert redispatch.order_notional_usd == 4.0
    assert ledger.pending_usd("XYZ") == 1.0


def test_a_pre_upgrade_ledger_entry_still_redispatches(tmp_path):
    """Codex review discussion_r3849268546 ("Preserve redispatch for
    pre-upgrade ledger entries"): a fire restored by the PREVIOUS code has a
    cache entry with no restored_credit_usd field at all. Defaulting that
    missing field to $0.00 caps every subsequent zero-contribution retry at
    nothing and permanently strands an actionable preserved fire across an
    upgrade or rollback cycle, even though the ledger still holds the restored
    balance. A MISSING field falls back to the pre-upgrade semantics -- the
    full pending balance.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    # Rewrite the persisted entry into its pre-upgrade shape.
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:slot-1"]
    del entry["restored_credit_usd"]
    state_path.write_text(json.dumps(state, indent=2))

    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0
    )
    assert preview is not None
    assert preview.shares_to_buy == 1
    assert preview.order_notional_usd == 10.0

    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert live.shares_to_buy == 1
    assert live.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_pre_restored_credit_entry_starts_aging_before_later_withdrawals(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="a", recorded_at="2026-08-16T15:30:00-04:00"
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="a", recorded_at="2026-08-16T15:30:00-04:00",
        claim_token=first.claim_token,
    )
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:a"]
    del entry["restored_credit_usd"]
    del entry["restored_credit_as_of_withdrawn_usd"]
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="b", recorded_at="2026-08-16T15:31:00-04:00"
    )
    migrated = json.loads(state_path.read_bytes())["recorded_idempotency_keys"]["2026-08-16:a"]
    assert migrated["restored_credit_usd"] == 10.0
    assert migrated["restored_credit_as_of_withdrawn_usd"] == 10.0

    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 100.0, idempotency_key="c", recorded_at="2026-08-16T15:32:00-04:00"
    )
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="a", recorded_at="2026-08-16T15:33:00-04:00"
    )
    assert retry.shares_to_buy == 0, "B consumed A's legacy credit; C's contribution does not restore it"


def test_a_pre_upgrade_entry_still_may_not_spend_an_unrelated_contribution(tmp_path):
    """The pre-upgrade fallback is a fallback, not an escape hatch.

    Falling a MISSING restored_credit_usd back to the whole pending balance
    would keep discussion_r3849268544's P1 hazard alive for every entry an
    earlier build wrote: a $10 preserved fire sharing a pool with $4 of
    unrelated credit could buy a $12 share on another key's money. The entry's
    own order_notional_usd is the faithful stand-in for what its restoration
    put back, so the cap survives the upgrade window intact.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert first.shares_to_buy == 1
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    unrelated = ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 20.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:32:00-04:00"
    )
    assert unrelated.shares_to_buy == 0  # still accumulating toward its own share
    assert ledger.pending_usd("BYDDY") == 14.0

    # Rewrite the ledger as a build predating restored_credit_usd left it.
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    del state["recorded_idempotency_keys"]["2026-08-16:slot-1"]["restored_credit_usd"]
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    preview = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 12.0)
    assert preview is not None
    assert preview.shares_to_buy == 0
    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 12.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert live.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 14.0  # the unrelated credit is untouched

    # ...but the fire is NOT stranded: at its own restored credit it still fires.
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:36:00-04:00"
    )
    assert redispatch.shares_to_buy == 1
    assert redispatch.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 4.0


def test_a_zero_restored_credit_entry_is_honored_as_a_real_cap(tmp_path):
    """The pre-upgrade fallback keys off the field being ABSENT, not falsy: an
    entry that explicitly recorded restoring $0.00 restored nothing, and must
    stay capped at nothing rather than being handed the whole pool.
    """
    ledger = _preserved_fire_with_unrelated_pending(tmp_path)
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    state["recorded_idempotency_keys"]["2026-08-16:slot-1"]["restored_credit_usd"] = 0.0
    state_path.write_text(json.dumps(state, indent=2))

    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0
    )
    assert preview is not None
    assert preview.shares_to_buy == 0
    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert live.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 10.60


def test_a_zero_share_reclaim_keeps_the_claim_redispatchable(tmp_path):
    """Codex review discussion_r3867947156 ("Preserve credit when a reclaim
    reprices to zero shares").

    A reclaim that reprices an unresolved fire down to zero shares hands the
    claim's WHOLE reservation back to the shared pool and marks the entry
    resolved -- the same release preserve_pending() performs. It used to leave
    no trace of that: it overwrote order_notional_usd with $0.00 and wrote no
    restored_credit_usd, so the next zero-contribution retry took
    _redispatchable_credit_usd()'s missing-field fallback, capped itself at that
    overwritten zero, and reported zero shares forever -- even once the price
    fell back within reach. $10 claimed at $10, reclaimed at $20, retried at $10
    left the actionable $10 stranded in the pool.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0,
        idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert first.shares_to_buy == 1
    assert ledger.pending_usd("BYDDY") == 0.0

    # The owner crashed without settling; a retry reclaims the claim at a price
    # its committed total no longer covers.
    reclaimed = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 20.0,
        idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert reclaimed.shares_to_buy == 0
    # The reservation is back in the pool, in full.
    assert ledger.pending_usd("BYDDY") == 10.0
    entry = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())[
        "recorded_idempotency_keys"
    ]["2026-08-16:slot-1"]
    assert entry["resolved"] is True
    assert entry["order_notional_usd"] == 0.0
    # ...and the release is recorded as the claim's own entitlement, so the
    # missing-field fallback (which would read the overwritten $0.00) is never
    # reached. The remainder is folded in and zeroed, exactly as
    # preserve_pending() does, so it can never be counted twice.
    assert entry["restored_credit_usd"] == 10.0
    assert entry["claim_credit_remainder_usd"] == 0.0

    # The price falls back: the preserved credit is actionable again.
    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00", 10.0
    )
    assert preview is not None
    assert preview.shares_to_buy == 1
    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0,
        idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00",
    )
    assert live.shares_to_buy == 1
    assert live.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_a_zero_share_reclaim_may_not_redispatch_an_unrelated_contribution(tmp_path):
    """The credit stamped by a zero-share reclaim is a CAP, not a licence on the
    whole pool: the claim gets back exactly what it released, and another key's
    accumulated balance stays out of its reach.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0,
        idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 20.0,
        idempotency_key="slot-2", recorded_at="2026-08-16T15:31:00-04:00",
    )
    reclaimed = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 20.0,
        idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert reclaimed.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 14.0

    # A $12 share is covered by the POOL but not by this claim's own $10.
    capped = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 12.0,
        idempotency_key="slot-1", recorded_at="2026-08-16T15:36:00-04:00",
    )
    assert capped.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 14.0

    # At its own restored credit it fires, leaving slot-2's $4 untouched.
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0,
        idempotency_key="slot-1", recorded_at="2026-08-16T15:37:00-04:00",
    )
    assert redispatch.shares_to_buy == 1
    assert redispatch.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 4.0


def test_a_zero_share_reclaim_writes_off_a_remainder_another_claim_already_spent(tmp_path):
    """Codex review discussion_r3871187647-shaped finding ("Reduce restored
    credit by the consumed remainder"): the zero-share reclaim fix above
    credited the claim's WHOLE original total (sizing basis + contribution)
    back via ``restored_credit_usd``, without checking whether a DIFFERENT
    claim had already spent part of what this claim's own remainder left
    behind in the shared pool.

    Claim A contributes $15 at $10/share -- one share, $5 remainder left in
    the pool. Claim B then contributes $5 at the same price, which combines
    with A's $5 remainder to buy its own share, spending A's remainder down
    to nothing. When A is later reclaimed and reprices to zero shares (price
    rose), crediting it the full original $15 would let a later contribution
    from an UNRELATED key fund a redispatch that was never A's to spend --
    only A's own $10 reservation is genuinely released; the $5 remainder is
    gone, and ``withdrawn_usd_total`` proves it.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "XYZ", "2026-08-16", 15.0, 10.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 5.0  # A's own $5 remainder

    ledger.record_contribution(
        "XYZ", "2026-08-16", 5.0, 10.0,
        idempotency_key="claim-b", recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 0.0  # B spent A's remainder down to zero

    reclaimed = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 30.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert reclaimed.shares_to_buy == 0
    assert ledger.pending_usd("XYZ") == 10.0  # only A's own reservation returns

    entry = json.loads((tmp_path / "XYZ" / "ledger.json").read_bytes())[
        "recorded_idempotency_keys"
    ]["2026-08-16:claim-a"]
    assert entry["restored_credit_usd"] == 10.0, (
        "A's spent remainder must not be credited back -- only the $10 "
        "reservation this reclaim actually released"
    )

    # At $5/share, A's genuine $10 buys exactly 2 shares; the un-fixed $15
    # credit would buy 3, spending $5 that belongs to whoever contributes it
    # next.
    redispatch = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 5.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:36:00-04:00",
    )
    assert redispatch.shares_to_buy == 2
    assert redispatch.order_notional_usd == 10.0
    assert ledger.pending_usd("XYZ") == 0.0


def test_an_upward_reprice_draw_is_capped_at_the_surviving_claim_remainder(tmp_path):
    """Codex review ("Cap reprice draws at the surviving claim remainder") --
    the sibling of the write-off fixes above, on the DRAW side rather than the
    write-off side: the pool-level guard (`state["pending_usd"] + pending_delta
    < 0`) only proves the POOL as a whole can cover an upward reprice's extra
    draw, not that the draw is this claim's OWN money.

    Claim A contributes $15 at $10/share -- one share, $5 remainder. Claim B
    spends A's $5 remainder. Claim C then contributes $2 that never fires
    (pool now $2, entirely C's). A is reclaimed at $12/share: the reprice is
    capped at A's own reserved $10 + its now-zero surviving remainder = $10,
    which does not even buy one share at $12. Codex review ("Release the
    surviving reservation after a zero-share reprice") then requires A's own
    $10 reservation to be released back to the pool -- resolved, not left
    stranded -- rather than deferred indefinitely: only C's $2 is untouched
    (never drawn into A's order), the pool as a whole correctly grows by
    A's released $10.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "XYZ", "2026-08-16", 15.0, 10.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 5.0  # A's own $5 remainder

    ledger.record_contribution(
        "XYZ", "2026-08-16", 5.0, 10.0,
        idempotency_key="claim-b", recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 0.0  # B spent A's remainder down to zero

    ledger.record_contribution(
        "XYZ", "2026-08-16", 2.0, 100.0,
        idempotency_key="claim-c", recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 2.0  # C's own $2, still accumulating

    reclaimed = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 12.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert reclaimed.shares_to_buy == 0
    assert reclaimed.order_notional_usd == 0.0
    assert ledger.pending_usd("XYZ") == 12.0, (
        "A's own $10 reservation is released back into the pool alongside "
        "C's untouched $2, not left stranded"
    )

    entry = json.loads((tmp_path / "XYZ" / "ledger.json").read_bytes())[
        "recorded_idempotency_keys"
    ]["2026-08-16:claim-a"]
    assert entry["order_notional_usd"] == 0.0, "A's reservation was released, not re-fired"
    assert entry["resolved"] is True
    assert entry["restored_credit_usd"] == 10.0, (
        "the just-released $10 reservation becomes A's own restored credit, "
        "aged from this point forward -- distinct from the earlier $5 "
        "remainder B already spent, which stays gone"
    )

    # C's money is genuinely safe: A's own entitlement caps a later
    # zero-contribution retry at exactly the $10 it was just credited, never
    # drawing on C's separate $2.
    redispatch = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 10.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:40:00-04:00",
    )
    assert redispatch.shares_to_buy == 1
    assert redispatch.order_notional_usd == 10.0
    assert ledger.pending_usd("XYZ") == 2.0, "only C's $2 remains after A redispatches its own $10"


def test_cached_fire_decision_mirrors_the_upward_reprice_remainder_guard(tmp_path):
    """Codex review ("Mirror the claim-remainder guard in cached previews"):
    the fix above lives in record_contribution()'s LIVE reclaim path.
    cached_fire_decision() -- the read-only preview used for dry-run output
    and budget-tier planning -- mirrors that path's pool-level guard but not
    the new claim-ownership one, so it could preview an upward reprice as a
    real order the live call would actually defer. Since cached_fire_decision
    feeds planned_spend tier selection, that phantom order could select a
    higher cap and authorize unrelated spending execution never places.

    Same scenario as the live-path test above, but asserting the PREVIEW
    agrees with what record_contribution() would actually do.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "XYZ", "2026-08-16", 15.0, 10.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.record_contribution(
        "XYZ", "2026-08-16", 5.0, 10.0,
        idempotency_key="claim-b", recorded_at="2026-08-16T15:31:00-04:00",
    )
    ledger.record_contribution(
        "XYZ", "2026-08-16", 2.0, 100.0,
        idempotency_key="claim-c", recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 2.0  # C's own $2, still accumulating

    preview = ledger.cached_fire_decision(
        "XYZ", "2026-08-16", "claim-a", "2026-08-16T15:35:00-04:00", 12.0
    )
    assert preview is not None
    assert preview.shares_to_buy == 0, (
        "the preview must agree exactly with what record_contribution() "
        "itself does -- not report a phantom order spending C's $2"
    )
    assert preview.order_notional_usd == 0.0

    # And it must agree with the live call for the same inputs.
    live = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 12.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert live.shares_to_buy == preview.shares_to_buy
    assert live.order_notional_usd == preview.order_notional_usd


def test_a_positive_reprice_writes_off_a_remainder_another_claim_already_spent(tmp_path):
    """Codex review ("Preserve only the surviving remainder after a positive
    reprice") -- the sibling of the zero-share write-off above, for the
    OTHER reclaim outcome: repricing still produces a positive fire
    (shares_to_buy > 0), so the claim stays unresolved and the write-off
    lands in ``claim_credit_remainder_usd`` instead of ``restored_credit_usd``.

    Claim A contributes $15 at $10/share -- one share, $5 remainder. Claim B
    spends A's $5 remainder. A is then reclaimed at $8/share: still one
    share, but the new $8 reservation releases only $2 of A's old $10 one.
    Crediting the un-written-off $7 (A's $15 basis minus the new $8
    reservation) -- what the code did before this fix -- would let A redispatch
    money that was never its own once this claim is later preserved and
    retried; only $2 (the release) plus A's $0 surviving remainder (B spent
    it) is genuinely A's.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "XYZ", "2026-08-16", 15.0, 10.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 5.0  # A's own $5 remainder

    ledger.record_contribution(
        "XYZ", "2026-08-16", 5.0, 10.0,
        idempotency_key="claim-b", recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert ledger.pending_usd("XYZ") == 0.0  # B spent A's remainder down to zero

    reclaimed = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 8.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert reclaimed.shares_to_buy == 1
    assert reclaimed.order_notional_usd == 8.0
    assert ledger.pending_usd("XYZ") == 2.0  # only A's own $2 release returns

    entry = json.loads((tmp_path / "XYZ" / "ledger.json").read_bytes())[
        "recorded_idempotency_keys"
    ]["2026-08-16:claim-a"]
    assert entry["claim_credit_remainder_usd"] == 2.0, (
        "A's spent remainder must not survive the reprice -- only the $2 "
        "this reclaim actually released"
    )

    # The order this reclaim just sized fails, and its $8 is preserved. Only
    # A's own money -- the $2 remainder plus the $8 just returned -- comes
    # back; the unfixed $7 remainder would have inflated this to $15.
    ledger.preserve_pending(
        "XYZ", "2026-08-16", 8.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:36:00-04:00",
        claim_token=reclaimed.claim_token,
    )
    assert ledger.pending_usd("XYZ") == 10.0

    # At $5/share, A's genuine $10 buys exactly 2 shares; the unfixed $15
    # credit would buy 3, spending money that was never A's.
    redispatch = ledger.record_contribution(
        "XYZ", "2026-08-16", 0.0, 5.0,
        idempotency_key="claim-a", recorded_at="2026-08-16T15:37:00-04:00",
    )
    assert redispatch.shares_to_buy == 2
    assert redispatch.order_notional_usd == 10.0
    assert ledger.pending_usd("XYZ") == 0.0


def test_a_remainder_spent_then_replenished_is_still_written_off(tmp_path):
    """Codex review ("Track remainder ownership across pool replenishment"):
    clamping a stale remainder to the CURRENT balance cannot prove the
    remainder survived, because the pool can fall to zero and be refilled
    before the fold reads it. A leaves $4, B spends it to nothing, C
    contributes a fresh $4, and a balance-only clamp saw $4 present and handed
    A a $20 cap -- letting A redispatch C's money.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 20.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 20.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    # A redispatches at $8: $16 reserved, $4 of its own left in the pool.
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 8.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert redispatch.order_notional_usd == 16.0
    assert ledger.pending_usd("BYDDY") == 4.0

    # B spends A's remainder down to nothing...
    b = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 4.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:36:00-04:00"
    )
    assert b.order_notional_usd == 4.0
    assert ledger.pending_usd("BYDDY") == 0.0

    # ...and C replenishes the pool with money that is C's, not A's.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 100.0, idempotency_key="slot-3", recorded_at="2026-08-16T15:37:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 4.0

    # A's order fails. Only its own $16 is its own again.
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 16.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00",
        claim_token=redispatch.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 20.0

    at_twenty = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 20.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:45:00-04:00"
    )
    assert at_twenty.shares_to_buy == 0, (
        "the $4 B consumed is gone -- C's replenishment must not revive it"
    )
    at_sixteen = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 16.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:46:00-04:00"
    )
    assert at_sixteen.shares_to_buy == 1
    assert at_sixteen.order_notional_usd == 16.0


def test_an_upward_reprice_records_its_extra_draw_on_the_pool(tmp_path):
    """Codex review ("Record the pre-assignment reprice withdrawal delta"):
    the reclaim path recorded its withdrawal as
    `repriced.order_notional_usd - cached["order_notional_usd"]` AFTER that
    cache field had already been overwritten with the repriced value, so the
    subtraction was always exactly zero and the monotonic counter missed every
    upward reprice. A draw the counter never saw cannot write off another
    claim's remainder, which is what the counter exists to do.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    # A fires at $10 out of a $14 pool, leaving $4 of its own entitlement.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 100.0, idempotency_key="slot-0", recorded_at="2026-08-16T15:29:00-04:00"
    )
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 4.0

    # B's own claim leaves a $4 remainder recorded against the pool.
    b_first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 4.0, idempotency_key="slot-0", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert b_first.shares_to_buy == 0  # slot-0 is resolved; nothing to redispatch

    # A is reclaimed at $14 -- an UPWARD reprice that draws a further $4 out of
    # the pool. That draw must land on the counter.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 14.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    state = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())
    assert state["withdrawn_usd_total"] == 14.0, (
        "the $10 original reservation plus the $4 the upward reprice drew"
    )


def test_a_zero_share_reclaim_does_not_double_register_its_own_tie_offset(tmp_path):
    """Codex review ("Avoid registering the reclaimed credit twice"): when a
    stale fire reprices to zero shares, its remainder was already allocated
    a slice of the shared tie batch when _CLAIM_REMAINDER_TIE_OFFSET_FIELD
    was written. Allocating the SAME reclaimed_remainder again for
    _RESTORED_CREDIT_TIE_OFFSET_FIELD just below treats the same dollars as
    two distinct claims on the batch, inflating the restored credit's own
    offset by its own amount -- protecting it from aging it should not be
    exempt from. After a later withdrawal fully drains the claim's own
    reclaimed money and a completely unrelated contribution replenishes the
    pool to the same balance, the stale claim must not still redispatch
    against that unrelated money.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"
    ticker = "BYDDY"

    # slot-1 fires $10 of a fresh $14 contribution at $10/share, leaving a
    # $4 remainder still outstanding as an unresolved fire (not yet
    # reclaimed/settled/preserved).
    first = ledger.record_contribution(
        ticker, day, 14.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.shares_to_buy == 1
    assert first.order_notional_usd == 10.0
    assert ledger.pending_usd(ticker) == 4.0

    # Reclaimed at a price so high the whole $14 basis no longer buys even
    # one share -- reprices to ZERO shares, releasing the whole $14 back to
    # the pool and folding it into restored_credit_usd (the "resolved"
    # branch this finding is about).
    reclaimed = ledger.record_contribution(
        ticker, day, 0.0, 1000.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert reclaimed.shares_to_buy == 0
    assert ledger.pending_usd(ticker) == 14.0

    # An unrelated fresh claim drains the WHOLE $14 pool -- a real
    # withdrawal that fully consumes slot-1's just-reclaimed $14.
    drain = ledger.record_contribution(
        ticker, day, 0.0, 14.0, idempotency_key="drain", recorded_at="2026-08-16T15:32:00-04:00"
    )
    assert drain.shares_to_buy == 1
    assert ledger.pending_usd(ticker) == 0.0

    # A completely unrelated claim then replenishes the pool back to $14 --
    # coincidentally the SAME balance, but none of it is slot-1's money.
    replenish = ledger.record_contribution(
        ticker, day, 14.0, 1000.0, idempotency_key="replenish", recorded_at="2026-08-16T15:33:00-04:00"
    )
    assert replenish.shares_to_buy == 0
    assert ledger.pending_usd(ticker) == 14.0

    # slot-1's own reclaimed $14 was already fully consumed by the drain --
    # its zero-contribution retry must not fire using the replenishing
    # claim's money.
    retry = ledger.record_contribution(
        ticker, day, 0.0, 14.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:34:00-04:00"
    )
    assert retry.shares_to_buy == 0, (
        "slot-1's own credit was already fully consumed by the drain -- it "
        "must not fire using the replenishing claim's money"
    )
    assert ledger.pending_usd(ticker) == 14.0


def _capped_redispatch_in_a_padded_pool(tmp_path):
    """A $20-restored claim holding an unresolved capped redispatch, in a pool
    an unrelated $5 contribution has padded to $25."""
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 20.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert first.shares_to_buy == 1
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 20.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    unrelated = ledger.record_contribution(
        "BYDDY", "2026-08-16", 5.0, 100.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert unrelated.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 25.0

    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 20.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    # Capped at the claim's own $20 even though the pool holds $25...
    assert redispatch.shares_to_buy == 1
    assert redispatch.order_notional_usd == 20.0
    # ...while the balance fields still describe the pool it drew from.
    assert redispatch.pending_before_usd == 25.0
    assert redispatch.pending_after_usd == 5.0
    assert ledger.pending_usd("BYDDY") == 5.0
    return ledger


def test_reclaiming_a_capped_redispatch_does_not_reopen_the_cap(tmp_path):
    """Codex review ("Preserve the restored-credit cap through claim
    reclaims"): a capped redispatch deliberately caches the whole shared pool
    as pending_before_usd, because the decision's balance fields describe the
    pool. The reclaim path then repriced from that field, re-opening the cap
    the redispatch had just applied -- a $20 claim in a $25 pool retried at $12
    authorized two shares ($24), spending $4 that was never its own.
    """
    ledger = _capped_redispatch_in_a_padded_pool(tmp_path)

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 12.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )
    assert retry.shares_to_buy == 1, (
        "the reclaim must re-size from the claim's own $20, not the $25 pool"
    )
    assert retry.order_notional_usd == 12.0
    # $20 reserved, now only $12: the pool gets the $8 difference back, and
    # slot-2's $5 is untouched throughout.
    assert ledger.pending_usd("BYDDY") == 13.0


def test_previewing_a_capped_redispatch_reclaim_matches_the_live_call(tmp_path):
    """The preview reprices from the same claim-owned basis, so tier planning
    never sees a bigger order than the live reclaim would place."""
    ledger = _capped_redispatch_in_a_padded_pool(tmp_path)

    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00", 12.0
    )
    assert preview is not None
    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 12.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )
    assert preview.shares_to_buy == live.shares_to_buy == 1
    assert preview.order_notional_usd == live.order_notional_usd == 12.0
    assert preview.pending_after_usd == live.pending_after_usd == 13.0


def test_previewing_a_capped_reprice_matches_the_live_reclaim(tmp_path):
    """Codex review ("Cap cached reprice previews by the surviving
    remainder"): an UNRESOLVED claim's preview used to reprice from the full
    frozen basis, uncapped by what the claim still owns -- the live reclaim
    caps it at the reserved notional plus the aged surviving remainder, so
    the two could disagree about whether (and how much) an order fires.

    A fires $16 of a $20 basis at $8/share, leaving $4 of its own; B consumes
    that $4; C contributes an unrelated $4 that never fires. Reclaiming A at
    $10/share off the uncapped $20 basis would preview/fire 2 shares ($20),
    spending C's $4 -- capped at the claim's own $16 reservation (its
    surviving remainder is $0), it must preview and fire only 1 share ($10).
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 8.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 4.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 99.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )

    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "A", "2026-08-16T15:40:00-04:00", 10.0
    )
    assert preview is not None
    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:42:00-04:00",
    )
    assert (preview.shares_to_buy, preview.order_notional_usd) == (1, 10.0), (
        "the preview must cap at the claim's own $16 reservation, not fire "
        "2 shares off the uncapped $20 basis and spend C's contribution"
    )
    assert (preview.shares_to_buy, preview.order_notional_usd) == (
        live.shares_to_buy,
        live.order_notional_usd,
    ), "the preview must match exactly what the live reclaim actually does"


def test_a_remainder_another_claim_already_spent_is_not_folded_back(tmp_path):
    """Codex review ("Reduce the remainder after another claim spends it"):
    the recorded remainder is a snapshot from when the fire was sized, and
    nothing reduces it when a DIFFERENT idempotency key draws the pool down in
    the meantime. Claim A reserving $16 of a $20 credit leaves $4; claim B then
    fires on that $4 plus $4 of its own. Folding A's stale $4 back on top of its
    restored $16 capped A at $20 against a pool holding $16, so an unrelated
    later $4 contribution became A's to spend.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 20.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert first.shares_to_buy == 1
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 20.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )

    # A redispatches at $8: two shares for $16, leaving $4 of its own credit.
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 8.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert redispatch.order_notional_usd == 16.0
    assert ledger.pending_usd("BYDDY") == 4.0

    # B contributes $4 and fires on the $8 pool -- consuming A's remainder.
    other = ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 8.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:36:00-04:00"
    )
    assert other.shares_to_buy == 1
    assert other.order_notional_usd == 8.0
    assert ledger.pending_usd("BYDDY") == 0.0

    # A's order is rejected: only its own $16 comes back.
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 16.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00",
        claim_token=redispatch.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 16.0

    # An unrelated contribution lands. It is NOT A's to spend.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 50.0, idempotency_key="slot-3", recorded_at="2026-08-16T15:41:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 20.0

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 20.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:45:00-04:00"
    )
    assert retry.shares_to_buy == 0, (
        "A's cap is the $16 it actually got back -- the $4 remainder claim B "
        "already spent must not let it reach slot-3's contribution"
    )
    assert ledger.pending_usd("BYDDY") == 20.0
    # ...but A's own $16 is still fully available to it.
    at_own_credit = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 16.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:46:00-04:00"
    )
    assert at_own_credit.shares_to_buy == 1
    assert at_own_credit.order_notional_usd == 16.0


def test_a_remainder_survives_when_only_unrelated_credit_joins_the_pool(tmp_path):
    """The clamp is an upper bound, never a floor: an unrelated contribution
    that INFLATES the pool must not inflate the claim's cap, and must not
    reduce a remainder that is genuinely still there either.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 20.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 20.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 8.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert redispatch.order_notional_usd == 16.0
    assert ledger.pending_usd("BYDDY") == 4.0

    # Unrelated credit joins the pool but spends nothing of A's remainder.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 6.0, 50.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:36:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 16.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00",
        claim_token=redispatch.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 26.0

    # A's full $20 entitlement survived...
    at_entitlement = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:45:00-04:00", 20.0
    )
    assert at_entitlement is not None
    assert at_entitlement.shares_to_buy == 1
    # ...but not a cent of slot-2's $6.
    beyond = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:45:00-04:00", 26.0
    )
    assert beyond is not None
    assert beyond.shares_to_buy == 0


def test_a_failed_redispatch_preserves_the_claim_owned_remainder(tmp_path):
    """Codex review ("Preserve the claim-owned remainder across failed
    redispatches"): a fire rarely spends its entitlement exactly. A restored
    $20 credit redispatched at $8 a share buys two shares for $16 and leaves $4
    of the CLAIM'S OWN money in the pool. Preserving that failed $16 order
    restores the pool to $20, but recording deducted_usd alone as the new cap
    stranded the $4 -- a later retry at $18 was capped at $16 and reported zero
    shares even though the claim's own credit covers it.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 20.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert first.shares_to_buy == 1
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 20.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 20.0

    # Redispatch at $8: 2 shares for $16, leaving $4 of this claim's credit.
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 8.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert redispatch.shares_to_buy == 2
    assert redispatch.order_notional_usd == 16.0
    assert ledger.pending_usd("BYDDY") == 4.0

    # That order is rejected -- the pool goes back to $20, and so must the cap.
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 16.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00",
        claim_token=redispatch.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 20.0

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 18.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:45:00-04:00"
    )
    assert retry.shares_to_buy == 1
    assert retry.order_notional_usd == 18.0
    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:45:00-04:00", 18.0
    )
    # Already fired above, so the preview now reports the resolved slot; the
    # point is that the retry itself was not capped at $16.
    assert preview is not None


def test_the_remainder_fold_still_bars_unrelated_contributions(tmp_path):
    """The remainder is the claim's OWN unspent change, never the pool's. A
    fire that spends its entitlement exactly leaves nothing to carry, so
    unrelated money sitting in the pool alongside it stays out of reach.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert first.shares_to_buy == 1
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    # $5 of unrelated money, recorded against a price too high to fire on its own.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 5.0, 99.0, idempotency_key="slot-2", recorded_at="2026-08-16T15:32:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 15.0

    # The claim restored exactly $10 and spent exactly $10 on its original
    # fire, so its cap is $10 -- the unrelated $5 must stay unreachable.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 14.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert retry.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 15.0


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
    first = ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    ledger.settle(
        "BYDDY", "2026-08-15", 1, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )

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
    assert unresolved is not None
    assert unresolved["claimed_at"] == original_recorded_at

    class _MatchingBroker:
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-1", "state": "filled", "quantity": "1"}

    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _MatchingBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # some OTHER, unrelated order exists -- not this claim's

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _NoExactMatchBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None  # still unresolved


@pytest.mark.parametrize("working_state", ["new", "queued", "confirmed", "partially_filled"])
def test_reconcile_unresolved_whole_share_claim_defers_a_still_live_order(tmp_path, working_state):
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

    class _StillWorkingBroker:
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-WORKING-1",
                "state": working_state,
                # In particular, a partial fill is cumulative progress, not
                # a terminal quantity that is safe to settle yet.
                "cumulative_quantity": "0.5" if working_state == "partially_filled" else "0",
                "quantity": "1",
            }

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _StillWorkingBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    # Codex review ("Book live orders discovered during claim recovery"):
    # a live/working match is real evidence of a broker attempt, so the
    # caller must still be told to book it as submitted/unfilled -- but the
    # LEDGER claim itself stays exactly as reserved/unresolved as before.
    assert reconciled == {
        "live_order_found": True,
        "order_id": "RH-WORKING-1",
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-FILLED-1", "state": "filled", "cumulative_quantity": "1"}

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _FilledBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled == {"order_id": "RH-FILLED-1", "shares_purchased": 1, "order_notional_usd": 10.0}
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.pending_usd("BYDDY") == 0.0


@pytest.mark.parametrize("blank_cumulative", [None, "", " "])
def test_reconcile_still_falls_back_when_the_cumulative_field_is_blank(tmp_path, blank_cumulative):
    """A broker null/blank ``cumulative_quantity`` is an ABSENCE marker, exactly
    like a missing key (Codex review: "Treat null recovery quantities as
    absent"): it says nothing about executed size, so the terminal "filled"
    match falls through to a USABLE submitted ``quantity`` and settles
    normally rather than being read as a malformed fill.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0,
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-16T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _NoCumulativeFieldBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-NOCUM-1",
                "state": "filled",
                "cumulative_quantity": blank_cumulative,
                "quantity": "1",
            }

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _NoCumulativeFieldBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled == {"order_id": "RH-NOCUM-1", "shares_purchased": 1, "order_notional_usd": 10.0}
    assert ledger.shares_purchased_total("BYDDY") == 1


@pytest.mark.parametrize("blank_quantity", [None, "", " "])
def test_reconcile_uses_the_claim_size_when_both_size_fields_are_blank(tmp_path, blank_quantity):
    """The last resort: a terminal "filled" response whose quantity fields are
    null or blank, just as when they are missing, settles on the claim's own
    planned size -- the documented answer for a response that said nothing
    about size at all.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0,
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-16T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _SizelessBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-NOSIZE-1",
                "state": "filled",
                "cumulative_quantity": blank_quantity,
                "quantity": blank_quantity,
            }

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _SizelessBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled == {"order_id": "RH-NOSIZE-1", "shares_purchased": 1, "order_notional_usd": 10.0}
    assert ledger.shares_purchased_total("BYDDY") == 1


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
        def account_number(self) -> str:
            return "test-account"

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

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _PartialFillThenCanceledBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
    )

    assert reconciled is not None

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


@pytest.mark.parametrize("working_state", ["new", "confirmed", "partially_filled"])
def test_reconcile_unresolved_whole_share_claim_defers_a_still_live_any_state_fallback_match(
    tmp_path, working_state
):
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup ran before the broker indexed the order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # The SAME order is now visible, still working -- not terminal.
            return {
                "id": "RH-RACE-1",
                "state": working_state,
                "cumulative_quantity": "1" if working_state == "partially_filled" else "0",
            }

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _RaceConditionStillLiveBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup missed the (eventually consistent) order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Filled, but the response only carries "quantity", not
            # "cumulative_quantity".
            return {"id": "RH-FILLED-ANY-STATE-1", "state": "filled", "quantity": "1"}

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _FilledViaAnyStateNoCumulativeQuantityBroker()), "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-16", unresolved,
    )

    assert reconciled == {"order_id": "RH-FILLED-ANY-STATE-1", "shares_purchased": 1, "order_notional_usd": 10.0}
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


@pytest.mark.parametrize("blank_cumulative", ["", " "])
def test_reconcile_falls_back_to_quantity_for_a_filled_any_state_match_with_a_blank_cumulative(
    tmp_path, blank_cumulative
):
    """The test above, but with a BLANK cumulative_quantity rather than an
    absent key -- the broker's other spelling of "this field says nothing".

    The any-state "filled" fallback used a bare ``is None`` check to decide
    whether to read ``quantity`` instead, so only the absent-key spelling
    reached that fallback. An empty or whitespace-only string pinned
    raw_terminal_quantity to a value that parses as unknown, and the filled
    order was booked as an unresolved live order rather than settled -- its
    real, already-executed purchase left permanently uncredited while the
    claim stayed reserved. ``_process_whole_share_candidate``'s own copy of
    this fallback has always used ``_is_blank_broker_field`` here; the two
    copies had drifted (Codex review: "Treat whitespace-only cumulative
    quantities as absent").
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="afternoon-BYDDY", recorded_at=recorded_at
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")

    class _FilledViaAnyStateBlankCumulativeBroker:
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup missed the (eventually consistent) order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Filled, and the executed size is reported ONLY in "quantity" --
            # cumulative_quantity is present but blank, which says nothing.
            return {
                "id": "RH-FILLED-ANY-STATE-BLANK-1",
                "state": "filled",
                "cumulative_quantity": blank_cumulative,
                "quantity": "1",
            }

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _FilledViaAnyStateBlankCumulativeBroker()), "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-16", unresolved,
    )

    assert reconciled == {
        "order_id": "RH-FILLED-ANY-STATE-BLANK-1", "shares_purchased": 1, "order_notional_usd": 10.0
    }
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-CANCELED-ANY-STATE-1", "state": "cancelled", "quantity": "1"}

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _CanceledViaAnyStateNoCumulativeQuantityBroker()), "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-16", unresolved,
    )

    # No cumulative_quantity, and "quantity" is not trusted as a fill for a
    # non-filled terminal order. Nothing settles, but the attempt remains
    # conservatively bookable for risk accounting.
    assert reconciled == {
        "live_order_found": True,
        "order_id": "RH-CANCELED-ANY-STATE-1",
        "planned_notional_usd": 10.0,
        "planned_quantity": 1,
    }
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # No cumulative_quantity at all -- only the REQUESTED quantity,
            # which must not be mistaken for what actually executed.
            return {"id": "RH-REJECTED-NO-FILL-INFO-1", "state": "rejected", "quantity": "2"}

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _RejectedNoCumulativeQuantityBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16",
        unresolved,
    )

    # Nothing was settled or released. The claim stays unresolved while the
    # real broker attempt is returned for conservative risk accounting.
    assert reconciled == {
        "live_order_found": True,
        "order_id": "RH-REJECTED-NO-FILL-INFO-1",
        "planned_notional_usd": 20.0,
        "planned_quantity": 2,
    }
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-REJECTED-1", "state": "rejected", "cumulative_quantity": "0"}

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _ZeroFillRejectedBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved,
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Terminal, but no cumulative_quantity field at all.
            return {"id": "RH-CANCELLED-1", "state": "cancelled"}

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _TerminalNoQuantityBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved,
        now_iso="2026-08-16T15:45:00-04:00",  # 1 second after claimed_at -- not stale
    )

    assert reconciled == {
        "live_order_found": True,
        "order_id": "RH-CANCELLED-1",
        "planned_notional_usd": 10.0,
        "planned_quantity": 1,
    }
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
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # cleanly no live match

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            raise RuntimeError("order history temporarily unavailable")

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _TerminalLookupRaisesBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved,
        now_iso="2026-08-16T15:45:00-04:00",  # well past staleness
    )

    assert reconciled is None
    assert ledger.shares_purchased_total("BYDDY") == 0
    # NOT released -- left exactly as record_contribution left it.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_cached_fire_decision_is_actionable_immediately_no_wall_clock_wait(tmp_path):
    """PR #515 ("Fix whole-share retry ownership and fill settlement")
    removed the wall-clock ``UNRESOLVED_CLAIM_STALE_SECONDS`` lease this test
    used to exercise -- a real ``record_contribution()`` call right now would
    reclaim this unresolved, not-yet-``placed`` fire immediately regardless
    of elapsed time, so the read-only preview must match that: actionable,
    not a flat zero, even moments after the original claim.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:31:00-04:00"  # 1 minute later -- reclaimed immediately
    )
    assert decision is not None
    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0


def test_cached_fire_decision_returns_the_positive_decision_once_stale(tmp_path):
    """An unresolved, not-yet-``placed`` fire is reclaimable immediately (no
    wall-clock wait since PR #515) -- a real ``record_contribution()`` call
    right now would reclaim it and return its cached POSITIVE decision, so
    the preview must reflect that actionable outcome, not a flat zero, at
    any elapsed time (including well past the FORMER staleness window).
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )

    decision = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00"  # 10 minutes later -- stale
    )
    assert decision is not None
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

    # Even if newer activity subsequently replenishes the shared pool, the
    # already-resolved slot-1 key cannot redispatch against dollars that
    # belong to that newer activity: both its read-only preview and a real
    # retry stay at zero shares and leave the replenished $10 untouched.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 20.0, idempotency_key="slot-3", recorded_at="2026-08-16T15:53:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 10.0
    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T16:04:00-04:00", 10.0
    )
    assert preview is not None
    assert preview.shares_to_buy == 0
    assert preview.order_notional_usd == 0.0
    resolved_retry = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        10.0,
        idempotency_key="slot-1",
        recorded_at="2026-08-16T16:04:00-04:00",
    )
    assert resolved_retry.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 10.0


def test_a_zero_share_reclaim_registers_its_credit_in_the_tie_batch_only_once(tmp_path):
    """Codex review ("Avoid registering the reclaimed credit twice"): a
    stale reclaim that reprices to 0 shares writes the SAME dollar figure
    into both claim_credit_remainder_usd (immediately zeroed after) and
    restored_credit_usd (the entitlement's final resting place) -- ONE
    claim's entitlement, not two. Calling _allocate_credit_tie_offset()
    once for each write double-counted it into credit_tie_batch_width_usd,
    inflating this claim's own offset with its own amount and letting a
    later, unrelated pool replenishment be redispatched against it. After
    the fix, a $10 reclaim-to-zero-shares must widen the tie batch by
    exactly $10, not $20.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 0.0  # 1 share bought, no remainder

    # Reclaim slot-1 stale, 10+ minutes later, at a doubled price -- 0
    # shares fit the frozen $10 total now, so it reprices to zero shares
    # and the $10 is restored as credit (the exact scenario this finding
    # flags).
    reclaim = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        0.0,
        20.0,
        idempotency_key="slot-1",
        recorded_at="2026-08-16T15:41:00-04:00",
    )
    assert reclaim.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    assert state["credit_tie_batch_width_usd"] == 10.0, (
        "the reclaimed $10 must widen the tie batch by $10 exactly once, "
        "not $20 from being registered under both field names"
    )
    cached = state["recorded_idempotency_keys"]["2026-08-16:slot-1"]
    assert cached["restored_credit_tie_offset_usd"] == cached["claim_credit_remainder_tie_offset_usd"]


def test_repeated_same_price_reclaims_do_not_re_register_an_unchanged_remainder(tmp_path):
    """Codex review ("Do not re-register an unchanged remainder on
    reclaim"): a $14 contribution firing a $10 order leaves a $4 remainder,
    registered once in the tie batch. Reclaiming that SAME unresolved claim
    again at the SAME price changes nothing (pending_delta is 0, so
    _record_withdrawal() no-ops and the withdrawal counter does not move) --
    the claim's remainder is still the same $4 in the same batch instant, so
    re-registering it must not widen the batch a second time. Two same-price
    reclaims back to back must leave the batch width at exactly $4, not $8
    or $12.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 14.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 4.0

    for at in ("2026-08-16T15:31:00-04:00", "2026-08-16T15:32:00-04:00"):
        reclaimed = ledger.record_contribution(
            "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at=at
        )
        assert reclaimed.shares_to_buy == 1
        assert reclaimed.order_notional_usd == 10.0

    assert ledger.pending_usd("BYDDY") == 4.0
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    assert state["credit_tie_batch_width_usd"] == 4.0, (
        "two same-price reclaims of the same unresolved claim must not "
        "widen the tie batch beyond the claim's own unchanged $4 remainder"
    )


def test_preserving_a_fired_claims_remainder_registers_its_credit_in_the_tie_batch_only_once(tmp_path):
    """Codex review ("Avoid registering the preserved remainder twice"): a
    $14 contribution firing a $10 order leaves a $4 remainder, already
    registered in the tie batch when the claim was recorded. Preserving
    that order (a blocked placement, restoring the $10 deduction) folds
    the surviving $4 remainder plus the restored $10 into a $14
    restored_credit_usd -- but that must widen the tie batch by the
    genuinely NEW $10 only, not by the full $14 (which would double-count
    the $4 that already widened the batch once). Same withdrawal-counter
    instant throughout (preserving never calls _record_withdrawal()), so
    the tie batch must widen by exactly $14 total, not $18.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 14.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert decision.shares_to_buy == 1
    assert decision.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 4.0  # $4 remainder left in the pool

    restored = ledger.preserve_pending(
        "BYDDY",
        "2026-08-16",
        10.0,
        idempotency_key="slot-1",
        claim_token=decision.claim_token,
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert restored == 14.0
    assert ledger.pending_usd("BYDDY") == 14.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    assert state["credit_tie_batch_width_usd"] == 14.0, (
        "the preserved claim's credit must widen the tie batch by $14 exactly "
        "once, not $18 from the $4 remainder being registered twice"
    )
    cached = state["recorded_idempotency_keys"]["2026-08-16:slot-1"]
    assert cached["restored_credit_tie_offset_usd"] == 0.0


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

    # UPDATED for Codex review ("Cap repricing by the claim's surviving
    # remainder"). The reprice is now sized from what this claim still OWNS --
    # its $10 reservation plus whatever of its own remainder survives -- rather
    # than from the frozen $15 total. slot-2 consumed slot-1's $5 remainder, so
    # nothing of it survives and the reprice is capped at the $10 already
    # reserved: 2 shares at the dropped $5 price, drawing NOTHING further from
    # the pool.
    #
    # That is a better outcome than the deferral this test originally pinned,
    # and it satisfies the same invariant the deferral existed to protect --
    # pending_usd never goes negative and the decision never spends more than
    # remains. The cap makes the overdraw unreachable by construction: the
    # extra draw a reprice can demand is at most the surviving remainder, which
    # is itself already clamped to the pool. The deferral guard downstream
    # stays as defence in depth.
    assert deferred.shares_to_buy == 2
    assert deferred.order_notional_usd == 10.0
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
    assert without_price is not None
    assert without_price.shares_to_buy == 1  # unchanged, stale cached price

    with_price = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", stale_recorded_at, 20.0)
    assert with_price is not None
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

    assert preview is not None
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
    original = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert ledger.pending_usd("BYDDY") == 0.0

    # A retry reclaims the fire 10 minutes later (past the stale window).
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # The ORIGINAL (now fenced-out) caller tries to restore its deduction,
    # presenting its OWN (now stale) claim_token -- it mismatches the
    # reclaiming attempt's freshly stored token, so this is a no-op.
    restored = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at,
        claim_token=original.claim_token,
    )

    assert restored == 0.0
    assert ledger.pending_usd("BYDDY") == 0.0  # unchanged -- no double-credit


def test_preserve_pending_refuses_to_credit_an_already_resolved_fire(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    ledger.settle(
        "BYDDY", "2026-08-16", 1, 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 0.0

    # A second, redundant preserve_pending() call for the SAME already-
    # settled fire must not credit anything back either.
    restored = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=recorded_at,
        claim_token=first.claim_token,
    )

    assert restored == 0.0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_only_reclaiming_caller_can_preserve_and_resolve_a_stale_fire(tmp_path):
    """A stale owner cannot restore funds or resolve the retry's claim."""
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    retry_recorded_at = "2026-08-16T15:40:00-04:00"
    original = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=retry_recorded_at
    )

    stale_result = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at,
        claim_token=original.claim_token,
    )
    assert stale_result == 0.0
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", retry_recorded_at, retry.claim_token
    ) is True

    retry_result = ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=retry_recorded_at,
        claim_token=retry.claim_token,
    )
    assert retry_result == 10.0
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", retry_recorded_at, retry.claim_token
    ) is False

    # Resolution cannot be replayed to restore the deduction a second time.
    assert (
        ledger.preserve_pending(
            "BYDDY", "2026-08-16", 10.0, idempotency_key="slot-1", recorded_at=retry_recorded_at,
            claim_token=retry.claim_token,
        )
        == 10.0
    )


def test_settle_bumps_shares_purchased_total(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution("BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-a")

    assert ledger.shares_purchased_total("BYDDY") == 0
    ledger.settle(
        "BYDDY", "2026-08-16", 1, 10.0, stream="morning", idempotency_key="slot-a",
        claim_token=first.claim_token,
    )
    assert ledger.shares_purchased_total("BYDDY") == 1

    second = ledger.record_contribution("BYDDY", "2026-08-17", 10.0, 10.0, idempotency_key="slot-b")
    ledger.settle(
        "BYDDY", "2026-08-17", 1, 10.0, stream="midday", idempotency_key="slot-b",
        claim_token=second.claim_token,
    )
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
    ledger.settle(
        "BYDDY", "2026-08-16", 1, 10.0, stream="morning", idempotency_key="slot-1",
        claim_token=decision.claim_token,
    )
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.pending_usd("BYDDY") == 10.0


def test_settle_releases_placed_for_an_explicit_zero_fill(tmp_path):
    """discussion_r3838771878: a claim marked "placed" (execute_claimed()
    durably recorded a broker submission) that settles with EXACTLY ZERO
    shares purchased -- a confirmed terminal zero-fill, e.g. a "filled"
    order whose own cumulative_quantity is 0 -- must release "placed", not
    just "placing". Unlike a real (non-zero) settlement, where "placed"
    staying True is accurate terminal history of a genuine purchase, a
    zero-fill claim's shortfall restores its ENTIRE deduction (nothing was
    ever actually bought), so leaving "placed" set here would permanently
    block every later zero-contribution retry under this same idempotency
    slot from redispatching that fully-restored credit -- the ledger.settle()
    docstring's OWN example scenario.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    decision = ledger.record_contribution("BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1")
    assert decision.shares_to_buy == 1
    assert ledger.pending_usd("BYDDY") == 0.0

    # execute_claimed() durably marks "placing" before the broker call, and
    # "placed" once the broker confirms acceptance -- simulate that here by
    # directly claiming the pending fire the same way execute_claimed()
    # would (the exact call is exercised elsewhere; this test is scoped to
    # settle()'s own release behavior).
    ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", None, lambda: "RH-ZERO-FILL-1",
        claim_token=decision.claim_token,
    )

    # The broker order terminally confirmed a ZERO fill -- nothing was
    # actually purchased -- so settle() restores the entire $10 deduction.
    ledger.settle(
        "BYDDY", "2026-08-16", 0, 0.0, stream="morning", idempotency_key="slot-1",
        claim_token=decision.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    # "placed" must be released -- a later zero-contribution retry under
    # this SAME slot must be free to redispatch the fully-restored credit.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T16:05:00-04:00"
    )
    assert retry.shares_to_buy == 1
    assert ledger.pending_usd("BYDDY") == 0.0


def test_settle_marks_placed_true_for_a_recovered_positive_fill(tmp_path):
    """Codex review ("Mark recovered positive fills as placed"): if the
    process dies after the broker accepts an order but before
    execute_claimed() persists its post-call update, the durable claim is
    left at placing=true, placed=false. A LATER reconciliation that
    confirms a positive terminal fill calls settle(), which clears
    "placing" but -- before this fix -- never touched "placed", leaving it
    at its stale False. That shape (resolved=true, placed=false,
    placing=false) is exactly what the zero-contribution redispatch gate
    treats as safe to retry, so a claim that ALREADY bought shares could
    be redispatched a second time. settle() must mark "placed" True for
    any confirmed non-zero fill, not just leave it untouched.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    decision = ledger.record_contribution("BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1")
    assert decision.shares_to_buy == 1

    ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", None, lambda: "RH-CRASH-1",
        claim_token=decision.claim_token,
    )

    # Simulate the crash window: execute_claimed() above durably set
    # "placed" True on success -- roll it back to False here (leaving
    # "placing" cleared, exactly as execute_claimed() itself already
    # leaves it) to reproduce a process that died before the "placed"
    # write landed.
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:slot-1"]
    assert entry["placed"] is True
    entry["placed"] = False
    state_path.write_text(json.dumps(state), encoding="utf-8")

    # A later reconciliation confirms the order actually filled for 1 share.
    # The full $10 deduction is fully accounted for by the fill (shortfall
    # is 0), so this alone leaves no money in the pool to redispatch.
    ledger.settle(
        "BYDDY", "2026-08-16", 1, 10.0, stream="morning", idempotency_key="slot-1",
        claim_token=decision.claim_token,
    )

    # An UNRELATED later contribution (a different idempotency_key) adds
    # new money to BYDDY's shared pool without fully consuming it (price
    # too high to afford a share) -- this is the money the bug lets a
    # stale, already-placed claim wrongly redispatch against.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 5.0, 100.0, idempotency_key="slot-2", recorded_at="2026-08-16T16:00:00-04:00"
    )
    assert ledger.pending_usd("BYDDY") == 5.0

    # A zero-contribution retry under the OLD slot-1 (already placed and
    # settled with a REAL fill) must stay blocked -- it must not redispatch
    # using the unrelated money slot-2 just added to the shared pool.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 5.0, idempotency_key="slot-1", recorded_at="2026-08-16T16:05:00-04:00"
    )
    assert retry.shares_to_buy == 0


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
    ledger.settle(
        "BYDDY", "2026-08-16", 10, 9.96, stream="morning", idempotency_key="slot-1",
        claim_token=decision.claim_token,
    )

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
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # The ORIGINAL (now fenced-out) caller settles with its stale recorded_at.
    ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1", recorded_at=original_recorded_at, claim_token=decision.claim_token
    )

    # Real shares were placed, so the audit count still reflects them...
    assert ledger.shares_purchased_total("BYDDY") == 2
    # ...but no shortfall was credited and the entry was not resolved by the
    # stale caller -- the reclaiming attempt still owns this fire's claim.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00", retry.claim_token
    ) is True
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", original_recorded_at, decision.claim_token
    ) is False


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
    original = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # The ORIGINAL (now fenced-out) reconciler settles with require_claim_
    # ownership=True, its own stale recorded_at, and its own (now stale)
    # claim_token -- it mismatches the reclaiming attempt's freshly stored
    # token.
    resolved = ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1",
        recorded_at=original_recorded_at, claim_token=original.claim_token, require_claim_ownership=True,
    )

    assert resolved is False
    # NOTHING was mutated -- unlike the unconditional-counting default
    # behavior, shares_purchased_total stays at 0.
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.still_claimed(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:40:00-04:00", retry.claim_token
    ) is True


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


# --- execute_claimed(): placement-intent marker + token-aware ownership ------


def test_execute_claimed_persists_placing_before_calling_the_broker(tmp_path):
    """Codex review discussion_r3818217230 ("Persist placement intent before
    invoking the broker"): the "placing" marker must land on disk BEFORE the
    irreversible broker call runs, and clear (with "placed" set) only after
    it succeeds -- so a crash mid-call always leaves durable evidence a
    submission was attempted.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert decision.shares_to_buy == 1

    observed = {}

    def operation():
        # A separate, lock-free read of the ledger's own on-disk state from
        # INSIDE operation() -- exactly what a process that crashed right
        # here would leave behind for a later reconciliation pass to find.
        raw = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())
        cached = raw["recorded_idempotency_keys"]["2026-08-16:slot-1"]
        observed["placing"] = cached.get("placing")
        observed["placed"] = cached.get("placed")
        return "order-123"

    owns_claim, result = ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", recorded_at, operation, claim_token=decision.claim_token
    )
    assert owns_claim is True
    assert result == "order-123"
    assert observed["placing"] is True
    assert not observed["placed"]

    raw_after = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())
    cached_after = raw_after["recorded_idempotency_keys"]["2026-08-16:slot-1"]
    assert cached_after["placing"] is False
    assert cached_after["placed"] is True


def test_a_claim_left_placing_after_a_crash_is_not_immediately_reclaimable(tmp_path, monkeypatch):
    """Simulates the SIGKILL-between-acceptance-and-persistence scenario
    from Codex review discussion_r3818217230: the broker call ("operation")
    succeeds, but the write that would flip "placing" -> "placed" fails (in
    real life: the process dies first). The claim must be left durably
    marked "placing" -- not silently look like a fresh, reclaimable claim --
    while staying visible to reconciliation via unresolved_claim().
    """
    import dca_whole_share_accumulator as wsa_module

    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )

    real_atomic_write_text = wsa_module.atomic_write_text
    call_count = {"n": 0}

    def flaky_write(path, content):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # The FIRST write (the pre-operation "placing" marker) lands
            # normally; the SECOND write (the post-operation "placed"
            # marker) fails -- mirroring a crash that happens after the
            # broker accepted the order but before this ledger could record
            # that fact.
            raise OSError("simulated crash before the placed write lands")
        real_atomic_write_text(path, content)

    monkeypatch.setattr(wsa_module, "atomic_write_text", flaky_write)
    with pytest.raises(WholeShareClaimPersistenceError):
        ledger.execute_claimed(
            "BYDDY", "2026-08-16", "slot-1", recorded_at, lambda: "order-123",
            claim_token=decision.claim_token,
        )
    monkeypatch.setattr(wsa_module, "atomic_write_text", real_atomic_write_text)

    raw = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())
    cached = raw["recorded_idempotency_keys"]["2026-08-16:slot-1"]
    assert cached["placing"] is True
    assert not cached.get("placed")

    # A retry (even a zero-contribution redispatch attempt) must NOT be
    # treated as freely actionable -- the true broker outcome is unknown.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )
    assert retry.shares_to_buy == 0

    # But it stays discoverable for reconciliation against broker history.
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "slot-1")
    assert unresolved is not None
    assert unresolved["placing"] is True


def test_execute_claimed_rejects_a_stale_token_sharing_the_same_recorded_at(tmp_path):
    """Codex review discussion_r3818217238 ("Validate the claim token before
    placing the order"): when a later record_contribution() reclaims the
    SAME recorded_at (overlapping scheduler attempts sharing a timestamp),
    the fenced-out caller's own (now-stale) token must be rejected by
    execute_claimed() even though its recorded_at still matches what's
    stored.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    shared_recorded_at = "2026-08-16T15:30:00-04:00"
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=shared_recorded_at
    )
    second = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=shared_recorded_at
    )
    assert second.claim_token != first.claim_token

    owns_claim, result = ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", shared_recorded_at, lambda: "should-not-run",
        claim_token=first.claim_token,
    )
    assert owns_claim is False
    assert result is None

    owns_claim2, result2 = ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", shared_recorded_at, lambda: "order-456",
        claim_token=second.claim_token,
    )
    assert owns_claim2 is True
    assert result2 == "order-456"


def test_preserve_pending_if_claimed_clears_placed_to_allow_zero_fill_redispatch(tmp_path):
    """Codex review discussion_r3818217243 ("Clear placed after releasing a
    terminal zero-fill order"): once a confirmed zero-fill order is released
    via preserve_pending_if_claimed(), the restored balance must remain
    redispatchable by a later same-day zero-contribution retry -- a stale
    "placed" marker left over from the dead order must not suppress that
    path forever.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert decision.shares_to_buy == 1

    owns_claim, order_id = ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", recorded_at, lambda: "order-789",
        claim_token=decision.claim_token,
    )
    assert owns_claim is True
    assert order_id == "order-789"

    resolved = ledger.preserve_pending_if_claimed(
        "BYDDY", "2026-08-16", decision.order_notional_usd,
        idempotency_key="slot-1", recorded_at=recorded_at, claim_token=decision.claim_token,
        expected_placed=True, expected_placing=False,
    )
    assert resolved is True
    assert ledger.pending_usd("BYDDY") == 10.0

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:45:00-04:00"
    )
    assert retry.shares_to_buy == 1
    assert retry.order_notional_usd == 10.0


def test_execute_claimed_rejects_a_path_traversal_shaped_ticker(tmp_path):
    """Codex review discussion_r3821705216 ("Validate ticker before acquiring
    the execution lock"): unlike every other public method on this class,
    execute_claimed() used to only reject an EMPTY symbol, so a caller
    passing an unvalidated value (e.g. idempotency_key=None, which skips the
    dedup_key-scoped ownership checks entirely) could reach
    ``self.root / symbol / "ledger.lock"`` with a path-traversal-shaped
    ticker and have cross_process_lock create that parent path OUTSIDE this
    ledger's own whole-share stream tree, running operation() with no claim
    validation at all. Must now raise before any path is touched or the
    lock is acquired -- operation() must never even be called.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    called = []
    with pytest.raises(ValueError):
        ledger.execute_claimed(
            "../other-stream",
            "2026-08-16",
            None,
            "2026-08-16T15:30:00-04:00",
            lambda: called.append("ran") or "should-not-run",
        )
    assert called == []
    # No stray ledger/lock artifact was created outside this ledger's own
    # per-symbol directories.
    assert not (tmp_path / ".." / "other-stream").resolve().exists()


def test_preserve_pending_refuses_to_restore_a_placement_that_won_the_race(tmp_path):
    """Codex review discussion_r3821705222 ("Preserve a placement that races
    stale-claim restoration"): a stale-claim recovery pass may decide, from a
    broker-history lookup that ran BEFORE it calls preserve_pending(), that a
    claim is safe to restore. But execute_claimed() shares the SAME
    per-ticker lock and can complete a real placement -- including its
    durable ``placed`` write -- entirely in the gap between that lookup and
    this call acquiring the lock. preserve_pending() must re-check
    ``placed`` under its own lock and refuse to restore/clear it once that
    has happened, or a same-day retry could redispatch the same money a
    second time despite the accepted order.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert decision.shares_to_buy == 1

    # Simulate the race: between a recovery pass's broker-history lookup
    # (which found nothing) and its preserve_pending() call, the original
    # owner's execute_claimed() call runs to completion and durably marks
    # the claim "placed".
    owns_claim, order_id = ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", recorded_at, lambda: "order-999",
        claim_token=decision.claim_token,
    )
    assert owns_claim is True
    assert order_id == "order-999"

    # The recovery pass's belated preserve_pending() call, using the SAME
    # (still-matching) claim_token/recorded_at it read before the race --
    # it must be refused, not silently restore/clear a real placement.
    restored_balance = ledger.preserve_pending(
        "BYDDY", "2026-08-16", decision.order_notional_usd,
        idempotency_key="slot-1", recorded_at=recorded_at, claim_token=decision.claim_token,
    )
    # Nothing was restored -- the deduction stays reflected in pending_usd
    # exactly as record_contribution left it (the one whole share's worth
    # already deducted), not credited back a second time.
    assert restored_balance == decision.pending_after_usd
    assert ledger.pending_usd("BYDDY") == decision.pending_after_usd

    # The claim is left exactly as execute_claimed() completed it -- still
    # placed, not resolved -- for normal settle() reconciliation, NOT
    # reopened for redispatch.
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "slot-1")
    assert unresolved is not None
    assert unresolved["placed"] is True

    # A same-day retry must not redispatch: the claim is still unresolved
    # (so still reserved), and even a later settle() confirms the real
    # order rather than record_contribution() suggesting a fresh fire.
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:45:00-04:00"
    )
    assert retry.shares_to_buy == 0


def test_preserve_pending_if_claimed_still_releases_a_confirmed_placed_zero_fill(tmp_path):
    """preserve_pending_if_claimed() is NOT subject to the same ``placed``
    guard as plain preserve_pending() (see the race test above) -- it exists
    specifically for a caller holding DIRECT broker evidence (an exact
    ref_id match on a genuinely terminal, zero-fill order) that THIS
    SPECIFIC placed order bought nothing, not a staleness guess from an
    absence of evidence. A ``placed=True`` claim must remain releasable
    through this method, exactly as
    test_preserve_pending_if_claimed_clears_placed_to_allow_zero_fill_redispatch
    already covers -- this test only pins the contrast against the new
    plain-preserve_pending() guard so a future change cannot accidentally
    widen that guard onto this method too.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    owns_claim, _ = ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", recorded_at, lambda: "order-111",
        claim_token=decision.claim_token,
    )
    assert owns_claim is True

    resolved = ledger.preserve_pending_if_claimed(
        "BYDDY", "2026-08-16", decision.order_notional_usd,
        idempotency_key="slot-1", recorded_at=recorded_at, claim_token=decision.claim_token,
        expected_placed=True, expected_placing=False,
    )
    assert resolved is True
    assert ledger.pending_usd("BYDDY") == decision.pending_after_usd + decision.order_notional_usd


def test_preserve_pending_if_claimed_declines_when_a_placement_races_the_snapshot(tmp_path):
    """Codex review discussion_r3825818000 ("Preserve orders placed during
    zero-fill restoration"): a reconciliation caller snapshots this claim
    via unresolved_claim() BEFORE doing its (network round-trip) broker
    lookup, observing placed=False/placing=False. If a concurrent
    execute_claimed() call for this SAME still-current claim_token places a
    genuinely NEW live order in the gap before this call acquires its lock,
    preserve_pending_if_claimed() must decline (return False) rather than
    restore the deduction and clear the now-stale ``placed`` marker -- doing
    so would make that live order's credit spendable again.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert decision.shares_to_buy == 1

    # The caller's snapshot, taken BEFORE the broker lookup that will
    # eventually justify a restoration -- at this point nothing has been
    # placed yet.
    snapshot = ledger.unresolved_claim("BYDDY", "2026-08-16", "slot-1")
    assert snapshot is not None
    assert not snapshot.get("placed")
    assert not snapshot.get("placing")

    # In the gap, a concurrent execute_claimed() call for this SAME
    # claim_token places a genuinely new, real order.
    owns_claim, order_id = ledger.execute_claimed(
        "BYDDY", "2026-08-16", "slot-1", recorded_at, lambda: "order-race-1",
        claim_token=decision.claim_token,
    )
    assert owns_claim is True
    assert order_id == "order-race-1"

    # The caller now (belatedly) tries to release its stale "confirmed
    # zero-fill" evidence, passing what its OWN snapshot showed for
    # ``placed`` (False).
    resolved = ledger.preserve_pending_if_claimed(
        "BYDDY", "2026-08-16", decision.order_notional_usd,
        idempotency_key="slot-1", recorded_at=recorded_at, claim_token=decision.claim_token,
        expected_placed=bool(snapshot.get("placed")),
        expected_placing=bool(snapshot.get("placing")),
    )
    assert resolved is False
    # Nothing was restored, and the live order's ``placed`` marker survives
    # untouched -- the deduction stays reflected in pending_usd exactly as
    # record_contribution left it.
    assert ledger.pending_usd("BYDDY") == decision.pending_after_usd
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "slot-1")
    assert unresolved is not None
    assert unresolved["placed"] is True


def test_preserve_pending_if_claimed_declines_when_placing_starts_after_the_snapshot(tmp_path):
    """Same race as the sibling test above, but for the ``placing`` (not yet
    durably ``placed``) case: the caller's reconciliation snapshot shows no
    placement in flight at all, but a concurrent execute_claimed() call for
    this SAME claim_token has since started (``placing=True``) and raised
    before completing -- e.g. an OrderConfirmationRequiredError-shaped
    failure. That in-flight/failed attempt is a real, unresolved live
    concern this detached reconciliation caller has no evidence about, so
    it must decline exactly like the ``placed`` case, not restore/clear it.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    recorded_at = "2026-08-16T15:30:00-04:00"
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="slot-1", recorded_at=recorded_at
    )
    assert decision.shares_to_buy == 1

    snapshot = ledger.unresolved_claim("BYDDY", "2026-08-16", "slot-1")
    assert snapshot is not None
    assert not snapshot.get("placed")
    assert not snapshot.get("placing")

    def _raise():
        raise RuntimeError("simulated broker placement failure")

    with pytest.raises(RuntimeError):
        ledger.execute_claimed(
            "BYDDY", "2026-08-16", "slot-1", recorded_at, _raise,
            claim_token=decision.claim_token,
        )
    mid_flight = ledger.unresolved_claim("BYDDY", "2026-08-16", "slot-1")
    assert mid_flight is not None
    assert not mid_flight.get("placed")
    assert mid_flight["placing"] is True

    resolved = ledger.preserve_pending_if_claimed(
        "BYDDY", "2026-08-16", decision.order_notional_usd,
        idempotency_key="slot-1", recorded_at=recorded_at, claim_token=decision.claim_token,
        expected_placed=bool(snapshot.get("placed")),
        expected_placing=bool(snapshot.get("placing")),
    )
    assert resolved is False
    assert ledger.pending_usd("BYDDY") == decision.pending_after_usd
    still_unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "slot-1")
    assert still_unresolved is not None
    assert still_unresolved["placing"] is True


def test_settle_rejects_nan_and_infinite_share_quantities(tmp_path):
    """Codex review discussion_r3825818002 ("Reject non-finite settled
    share quantities"): NaN/inf both slip past the existing
    ``shares_purchased < 0`` guard (every comparison against NaN is False,
    and inf is not negative) and would otherwise permanently corrupt
    ``shares_purchased_total`` -- every later settlement then propagates
    the non-finite total. Both must raise ValueError, leaving the ledger's
    state completely unchanged.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution("BYDDY", "2026-08-16", 10.0, 10.0)
    before_total = ledger.shares_purchased_total("BYDDY")
    before_pending = ledger.pending_usd("BYDDY")

    for bad_quantity in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            ledger.settle("BYDDY", "2026-08-16", bad_quantity, 10.0, stream="morning")

    assert ledger.shares_purchased_total("BYDDY") == before_total
    assert ledger.pending_usd("BYDDY") == before_pending


def test_settle_from_a_fenced_out_caller_does_not_block_the_new_owners_own_credit(tmp_path):
    """Codex review discussion_r3825818004 ("Do not mark the new owner's
    order as already credited"): a fenced-out caller settling a REAL,
    already-executed order it placed must still get credited for it (that
    is settle()'s documented behavior for a genuine in-line placement caller
    -- see its own docstring) -- but must NOT stamp ``shares_credited=True``
    onto whatever DIFFERENT claim currently occupies the slot. If it did,
    that different, still-live claim's own later, legitimate settlement
    would be wrongly suppressed by ``already_credited``, and only the stale
    caller's purchase would ever land in ``shares_purchased_total`` even
    though BOTH physical orders actually executed.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    original = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert original.shares_to_buy == 2

    # A retry reclaims the fire (a new claim_token, distinct physical
    # attempt) before the original caller settles.
    retry_recorded_at = "2026-08-16T15:40:00-04:00"
    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at=retry_recorded_at
    )
    assert retry.claim_token != original.claim_token

    # The ORIGINAL (now fenced-out) caller settles the REAL order it placed
    # for its own (stale) claim_token.
    resolved = ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1",
        recorded_at=original_recorded_at, claim_token=original.claim_token,
    )
    assert resolved is False  # fenced out -- did not resolve the (now retry's) claim
    # Its real shares are still credited.
    assert ledger.shares_purchased_total("BYDDY") == 2

    # The RETRY (the current, still-live claim owner) now places its own,
    # DIFFERENT physical order and settles it for ITS OWN claim_token. This
    # must land its own credit -- the fenced caller's earlier settlement
    # must not have poisoned the current slot's ``shares_credited`` marker.
    retry_resolved = ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1",
        recorded_at=retry_recorded_at, claim_token=retry.claim_token,
    )
    assert retry_resolved is True
    # BOTH physical orders' shares are now reflected -- 2 + 2 = 4, not just
    # the fenced caller's 2.
    assert ledger.shares_purchased_total("BYDDY") == 4


def test_settle_is_still_idempotent_for_two_concurrent_fenced_callers_of_the_same_order(tmp_path):
    """Pre-existing idempotency guarantee (Codex review: "Handle a fenced
    post-placement settlement idempotently") that the fix above must NOT
    regress: two concurrent RECONCILING callers can both read the SAME
    unresolved claim before either settles it, and both then call settle()
    for the SAME physical broker order using the SAME (now fenced-out)
    claim_token. Crediting must happen only once for that one physical
    order, even though neither caller currently owns the slot.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    original_recorded_at = "2026-08-16T15:30:00-04:00"
    original = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at=original_recorded_at
    )
    assert original.shares_to_buy == 2

    # A retry reclaims the fire, fencing out the original claim_token.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )

    # Two concurrent reconciling callers both discover the SAME physical
    # order (the original's) and both settle it with the SAME, now-stale
    # claim_token.
    first_resolved = ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1",
        recorded_at=original_recorded_at, claim_token=original.claim_token,
    )
    second_resolved = ledger.settle(
        "BYDDY", "2026-08-16", 2, 20.0, stream="morning", idempotency_key="slot-1",
        recorded_at=original_recorded_at, claim_token=original.claim_token,
    )
    assert first_resolved is False
    assert second_resolved is False
    # Only ONE of the two calls actually credited the physical order --
    # not double-counted to 4.
    assert ledger.shares_purchased_total("BYDDY") == 2


def test_restored_credit_is_aged_by_intervening_withdrawals(tmp_path):
    """Codex review discussion_r3867623114 ("Age restored credit before
    redispatching it").

    ``restored_credit_usd`` was clamped to the CURRENT pending balance and
    nothing else, and a balance clamp cannot tell "never spent" from "spent and
    refilled". Claim A restores $10; claim B withdraws exactly that $10; claim C
    then contributes a fresh $10. The pool reads $10 again, so A's cap survived
    intact and a retry of A could submit an order funded entirely by C's
    contribution -- the very hazard the cap exists to prevent, reached by a
    different route.

    ``claim_credit_remainder_usd`` already aged against ``withdrawn_usd_total``
    for exactly this reason; its sibling now does too.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert first.shares_to_buy == 1
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    # B draws A's restored $10 straight back out of the shared pool.
    drain = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert drain.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 0.0

    # C refills the pool to $10 with money that is entirely its own. Priced
    # above the contribution so it accumulates instead of firing.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "A's restored credit was withdrawn by B; the $10 now in the pool is C's "
        "contribution and A may not spend it"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_a_null_restored_credit_snapshot_is_written_off_not_unaged(tmp_path):
    """Codex review ("Reject null restored-credit snapshots").

    ``restored_credit_as_of_withdrawn_usd`` is PRESENT-but-null on a
    hand-edited or corrupted cache entry -- a different thing from the field
    being ABSENT (a pre-counter entry that never had a snapshot). The old
    check (``as_of is not None``) treated both the same and skipped aging
    entirely, so a null snapshot's ``restored_credit_usd`` reached the plain
    balance clamp un-aged. Claim A restores $10; claim B withdraws it; claim C
    refills the pool with its own $10. With a genuine snapshot A's retry
    would be aged to $0 (see test_restored_credit_is_aged_by_intervening_
    withdrawals); with the snapshot corrupted to null it must fail closed to
    $0 as well, not fall through to the un-aged $10 balance-clamp figure.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:A"]
    assert entry["restored_credit_usd"] == 10.0
    entry["restored_credit_as_of_withdrawn_usd"] = None
    state_path.write_text(json.dumps(state, indent=2))

    drain = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert drain.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 0.0

    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "a null snapshot must write the credit off, not fall through to the "
        "un-aged balance-clamp figure that lets A spend C's contribution"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_a_zero_share_reprice_records_the_credit_it_released(tmp_path):
    """Codex review discussion_r3867623112 ("Preserve credit when a reclaim
    reprices to zero shares").

    A stale $10 fire reclaimed after the quote rises above $10 reprices to zero
    shares: the whole committed amount goes back into the pool and the entry is
    marked resolved. It was resolved WITHOUT ever writing
    ``restored_credit_usd`` -- no preserve_pending() ran on this path -- so a
    later zero-contribution retry fell through _redispatchable_credit_usd()'s
    missing-field branch to ``order_notional_usd``, which this very reprice had
    just overwritten with 0. The cap read $0 and the released credit was
    stranded permanently, even though the ledger still held it.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert first.shares_to_buy == 1
    assert ledger.pending_usd("BYDDY") == 0.0

    # Never settled or preserved -- the owner crashed. A later retry reclaims
    # it, and the quote has risen past what the committed total buys.
    reclaim = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="A",
        recorded_at="2026-08-16T15:40:00-04:00",
    )
    assert reclaim.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 10.0

    entry = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())[
        "recorded_idempotency_keys"
    ]["2026-08-16:A"]
    assert entry["resolved"] is True
    assert entry["order_notional_usd"] == 0.0
    assert entry["restored_credit_usd"] == 10.0, (
        "the released entitlement has to be recorded explicitly -- the "
        "missing-field fallback now reads a zeroed notional"
    )

    later = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:50:00-04:00",
    )
    assert later.shares_to_buy == 1, "the $10 the reprice released must stay reachable"
    assert later.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 0.0


def test_a_reclaim_keeps_the_remainder_aged_against_an_intervening_withdrawal(tmp_path):
    """Codex review discussion_r3867623115 ("Retain remainder aging when
    reclaiming a claim").

    The reclaim rebuilt ``claim_credit_remainder_usd`` from the FROZEN
    pending_before_usd/contribution_usd captured when the claim was first
    recorded, then re-snapshotted the withdrawal counter. Rebuilding resurrects
    a remainder another claim has since withdrawn, and re-snapshotting makes
    that withdrawal look older than the remainder, so the aging can never see
    it. If unrelated credit then lands before this claim is preserved, the
    preserve fold counts that fresh money as the claim's own change.

    $20 at $8 buys two shares for $16 and leaves $4 of A's own credit. B takes
    that $4. A is reclaimed at the same price, C then contributes an unrelated
    $4, and A is preserved -- the fold must return $16, not $20.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 8.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert (first.shares_to_buy, first.order_notional_usd) == (2, 16.0)
    assert ledger.pending_usd("BYDDY") == 4.0

    drain = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 4.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert drain.order_notional_usd == 4.0
    assert ledger.pending_usd("BYDDY") == 0.0

    reclaim = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 8.0, idempotency_key="A",
        recorded_at="2026-08-16T15:41:00-04:00",
    )
    assert (reclaim.shares_to_buy, reclaim.order_notional_usd) == (2, 16.0)
    entry = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())[
        "recorded_idempotency_keys"
    ]["2026-08-16:A"]
    assert entry["claim_credit_remainder_usd"] == 0.0, (
        "B withdrew the $4; the reclaim must not rebuild it from the frozen basis"
    )

    # Unrelated money lands before the preserve.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 50.0, idempotency_key="C",
        recorded_at="2026-08-16T15:42:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 4.0

    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 16.0, idempotency_key="A",
        recorded_at="2026-08-16T15:43:00-04:00", claim_token=reclaim.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 20.0
    entry = json.loads((tmp_path / "BYDDY" / "ledger.json").read_bytes())[
        "recorded_idempotency_keys"
    ]["2026-08-16:A"]
    assert entry["restored_credit_usd"] == 16.0, (
        "only what A itself put back -- C's $4 is not A's change"
    )

    # End to end: the pool holds $20 but A may only reach its own $16.
    redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 20.0, idempotency_key="A",
        recorded_at="2026-08-16T15:44:00-04:00",
    )
    assert redispatch.shares_to_buy == 0, (
        "a $20 share is out of reach on a $16 entitlement, however full the pool looks"
    )
    assert ledger.pending_usd("BYDDY") == 20.0


def test_a_non_finite_restored_credit_cannot_redispatch_the_whole_pool(tmp_path):
    """Codex review ("Reject non-finite restored credits before redispatch").

    ``float()`` accepts the JSON strings "NaN" and "Infinity", and a ledger is
    arbitrary JSON that _read_state() admits on shape alone. Neither survives a
    comparison the way a number does: ``min(pending_usd, nan)`` returns
    ``pending_usd``, because every comparison against NaN is False and ``min``
    keeps its first argument. So a corrupt entitlement did not merely fail to
    cap the redispatch -- it handed it the entire shared pool, which is exactly
    the ownership breach the cap exists to prevent.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    # An unrelated claim's contribution shares the pool.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 15.0, 40.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 25.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:A"]
    entry["restored_credit_usd"] = "NaN"
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 25.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "an unreadable entitlement is no entitlement, never an unbounded one -- "
        "a $25 share must not be bought on B's contribution"
    )
    assert ledger.pending_usd("BYDDY") == 25.0


def test_an_unusable_withdrawal_counter_does_not_abort_the_ticker(tmp_path):
    """Codex review ("Validate the withdrawal counter when loading ledgers").

    ``setdefault`` only fills the field in when it is ABSENT, so an existing
    entry carrying a nonnumeric value survived untouched -- and every later
    reservation calls ``float()`` on it in ``_record_withdrawal()``, which then
    raises and takes down processing for the whole ticker on the next fire.
    Failing the malformed entitlement CLOSED is what the counter is for; taking
    the ticker with it is not.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 5.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    state["withdrawn_usd_total"] = "bad"
    state_path.write_text(json.dumps(state, indent=2))

    # The next fire must still work rather than raise.
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert decision.shares_to_buy == 1
    assert ledger.pending_usd("BYDDY") == 5.0

    # Normalized on the way in, so the counter is a usable number again and
    # this fire's own withdrawal was recorded against it.
    persisted = json.loads(state_path.read_bytes())
    assert isinstance(persisted["withdrawn_usd_total"], (int, float))
    assert persisted["withdrawn_usd_total"] >= 0.0


def test_an_absent_withdrawal_counter_does_not_invalidate_legacy_entitlements(tmp_path):
    """Codex review ("Preserve entitlements when the counter is absent").

    A ledger written before withdrawn_usd_total existed at all has ABSENT is
    not the same thing as PRESENT-and-malformed: initializing it to 0.0 is
    the intentionally supported upgrade path, not a corruption to write off
    every cached restored_credit_usd/claim remainder over. Routing an absent
    field through the same invalidation as a genuinely unreadable one made a
    resolved claim with $10 restored into a $10 pending pool return zero
    shares on its zero-contribution retry instead of redispatching, silently
    suppressing a purchase the entitlement legitimately owned.
    """
    ledger = _preserved_fire_with_unrelated_pending(tmp_path)
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:slot-1"]
    assert entry["restored_credit_usd"] == 10.0
    # Simulate a legacy ledger that predates this field entirely.
    del state["withdrawn_usd_total"]
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="slot-1",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 1, (
        "an absent withdrawal counter must initialize to 0.0 without "
        "writing off the claim's real restored credit"
    )
    assert retry.order_notional_usd == 10.0

    persisted = json.loads(state_path.read_bytes())
    assert isinstance(persisted["withdrawn_usd_total"], (int, float))
    assert persisted["withdrawn_usd_total"] >= 0.0


def test_a_legacy_restored_credit_is_snapshotted_before_future_withdrawals(tmp_path):
    """Regression for "Snapshot legacy restored credits before future
    withdrawals".

    An entry written by a PARENT version that had restored_credit_usd but
    not yet its _AS_OF snapshot field carries the value with no snapshot at
    all. Reading an absent snapshot as "charged no withdrawal, fall back to
    the balance clamp" is correct for draws that happened BEFORE the
    upgrade (unknowable), but nothing else backfills the snapshot
    afterward, so the old code exempted the entry from aging FOREVER --
    including withdrawals that happen strictly after this load. A restores
    $10, B later withdraws it, C replenishes the pool, and retrying A must
    not spend C's contribution.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:A"]
    assert entry["restored_credit_usd"] == 10.0
    # Simulate a legacy write: restored_credit_usd present, but no snapshot
    # field at all (as if written before that field existed).
    del entry["restored_credit_as_of_withdrawn_usd"]
    state_path.write_text(json.dumps(state, indent=2))

    # B drains A's restored credit back out of the shared pool. This is the
    # first ledger read/write since the simulated legacy write above, so it
    # is where the backfill must snapshot A's entry -- BEFORE this draw.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    # C refills the pool with money that is entirely its own.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "A's restored credit must be aged by B's withdrawal even though it "
        "predates the as_of snapshot -- retrying A must not reach C's money"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_two_legacy_restored_credits_sharing_a_backfilled_snapshot_are_staggered(tmp_path):
    """Codex review ("Stagger tie offsets when loading legacy credits"): the
    migration that backfills a missing restored_credit_as_of_withdrawn_usd
    snapshot (_initialize_restored_credit_aging) used to give every migrated
    entry a flat $0 tie offset. Two legacy $10 credits with NO snapshot at
    all (as if written before that field existed) both land on the SAME
    backfilled as-of value in one migration pass -- with the old flat-$0
    behavior, a single later $10 withdrawal would age BOTH of them to zero,
    stranding whichever one's money wasn't actually spent. The fix stagger
    them against each other (never against the live global tie-batch
    tracker, to avoid the documented self-collision hazard), matching the
    ordinary (non-legacy) tie-offset guarantee.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    # All three fires happen BEFORE any preserve, so each independently buys
    # one $10 share from an empty pool -- none of them draws on another's
    # restored money (same setup as
    # test_two_restored_credits_at_the_same_snapshot_are_not_both_charged_
    # one_withdrawal above).
    fires = {}
    for key, at in (
        ("A", "2026-08-16T15:30:00-04:00"),
        ("B", "2026-08-16T15:31:00-04:00"),
        ("C", "2026-08-16T15:32:00-04:00"),
    ):
        fires[key] = ledger.record_contribution(
            "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key=key, recorded_at=at
        )
        assert fires[key].order_notional_usd == 10.0

    for key, at in (
        ("A", "2026-08-16T15:33:00-04:00"),
        ("B", "2026-08-16T15:34:00-04:00"),
        ("C", "2026-08-16T15:35:00-04:00"),
    ):
        ledger.preserve_pending(
            "BYDDY", "2026-08-16", 10.0, idempotency_key=key, recorded_at=at,
            claim_token=fires[key].claim_token,
        )
    assert ledger.pending_usd("BYDDY") == 30.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    # Simulate A and B (but not C) as legacy writes: restored_credit_usd
    # present, but no snapshot field at all.
    for key in ("2026-08-16:A", "2026-08-16:B"):
        del entries[key]["restored_credit_as_of_withdrawn_usd"]
        del entries[key]["restored_credit_tie_offset_usd"]
    state_path.write_text(json.dumps(state, indent=2))

    # C redispatches its own $10 -- a single, cleanly-sized $10 withdrawal
    # out of the shared $30 pool (a redispatch is capped at the
    # redispatching claim's OWN restored credit, so C's $10 caps its own
    # order exactly, regardless of the combined pool total). This is also
    # the first ledger read/write since the simulated legacy write above,
    # so it is where the migration backfills A's and B's snapshots in the
    # SAME pass, landing both on the same as-of value this withdrawal is
    # about to age against.
    c_redispatch = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="C",
        recorded_at="2026-08-16T15:36:00-04:00",
    )
    assert c_redispatch.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 20.0

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    withdrawn_total = state["withdrawn_usd_total"]
    pool = state["pending_usd"]
    credit_a = _redispatchable_credit_usd(entries["2026-08-16:A"], pool, withdrawn_total)
    credit_b = _redispatchable_credit_usd(entries["2026-08-16:B"], pool, withdrawn_total)
    zeroed = [credit for credit in (credit_a, credit_b) if credit == 0.0]
    survived = [credit for credit in (credit_a, credit_b) if credit == 10.0]
    assert len(zeroed) == 1 and len(survived) == 1, (
        f"exactly one of A's (${credit_a}) / B's (${credit_b}) legacy $10 credits "
        "must be charged for the $10 withdrawal -- not both (the bug) and not neither"
    )


def test_legacy_migration_seeds_the_live_tie_batch_so_new_registrations_continue_after_it(tmp_path):
    """Codex review ("Seed the live tie batch after migrating legacy
    credits"): the legacy-credit migration in _read_state() assigns
    staggered per-entry offsets to the migrated entries themselves, but
    used to never seed the LIVE state[_CREDIT_TIE_BATCH_ANCHOR_FIELD]/
    [_CREDIT_TIE_BATCH_WIDTH_FIELD] tracker _allocate_credit_tie_offset()
    reads. A fresh, ordinary (non-legacy) registration at the SAME
    withdrawn_total instant -- e.g. a new sub-share contribution recorded
    before any further withdrawal moves the counter -- would then see no
    live batch at all, reset to offset $0, and collide with the FIRST
    migrated legacy entry's own $0 offset.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    fires = {}
    for key, at in (
        ("A", "2026-08-16T15:30:00-04:00"),
        ("B", "2026-08-16T15:31:00-04:00"),
    ):
        fires[key] = ledger.record_contribution(
            "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key=key, recorded_at=at
        )
        assert fires[key].order_notional_usd == 10.0

    for key, at in (
        ("A", "2026-08-16T15:33:00-04:00"),
        ("B", "2026-08-16T15:34:00-04:00"),
    ):
        ledger.preserve_pending(
            "BYDDY", "2026-08-16", 10.0, idempotency_key=key, recorded_at=at,
            claim_token=fires[key].claim_token,
        )
    assert ledger.pending_usd("BYDDY") == 20.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    # Simulate A and B as legacy writes: restored_credit_usd present, but
    # no snapshot field at all.
    for key in ("2026-08-16:A", "2026-08-16:B"):
        del entries[key]["restored_credit_as_of_withdrawn_usd"]
        del entries[key]["restored_credit_tie_offset_usd"]
    state_path.write_text(json.dumps(state, indent=2))

    # A fresh sub-share contribution, priced so it can never fire, at the
    # SAME withdrawn_total instant A's/B's backfilled snapshot lands on.
    # This is the FIRST ledger read/write since the simulated legacy edit
    # above, so it is where the migration runs AND, in this same call,
    # this claim's own remainder registration happens right after it.
    d_decision = ledger.record_contribution(
        "BYDDY", "2026-08-16", 5.0, 1000.0, idempotency_key="D",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert d_decision.shares_to_buy == 0

    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    d_offset = entries["2026-08-16:D"]["claim_credit_remainder_tie_offset_usd"]
    assert d_offset == 20.0, (
        "D's registration must continue AFTER the two migrated $10 legacy "
        f"credits (offset 20.0), not collide with A's own $0 offset (got {d_offset})"
    )
    assert state["credit_tie_batch_width_usd"] == 25.0


def test_a_legacy_restored_credit_and_a_legacy_remainder_are_staggered_against_each_other(tmp_path):
    """Codex review ("Stagger tie offsets when loading legacy credits"): the
    same finding as the sibling test above, but across the TWO DIFFERENT
    migrated field types rather than two entries of the SAME type.

    restored_credit_usd and claim_credit_remainder_usd are slices of the
    exact same fungible pending_usd pool -- the LIVE (non-migration) write
    sites already tie them against each other through one shared
    credit_tie_batch_width_usd/credit_tie_batch_anchor_withdrawn_usd axis
    (_allocate_credit_tie_offset() is called from both
    record_contribution()'s remainder registration and
    preserve_pending()'s restored-credit registration). A migration fix
    that staggers restored-credit entries only against OTHER restored-
    credit entries, and remainder entries only against OTHER remainder
    entries, would let a resolved claim (restored credit) and a still-open
    claim (live remainder) that share a backfilled snapshot both read tie
    offset $0 despite drawing on the same pool -- reopening the exact
    collision this fix exists to close, just between the two field types
    instead of between two same-type entries.

    X fires a clean $10-for-$10 order (no remainder) and is preserved,
    restoring $10. Y then contributes $5 at a price too high to fire,
    leaving a live $5 remainder -- both at the SAME withdrawn_usd_total
    snapshot (no withdrawal moves the counter in between). Both are rolled
    back to the legacy shape (no snapshot at all) and migrated together in
    one pass. If they were NOT staggered against each other, both would
    read offset $0 and a single $10 withdrawal would zero out X's credit
    without touching Y's live $5 remainder captured in the SAME pool slice
    it should have already covered.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"
    ticker = "BYDDY"

    x = ledger.record_contribution(
        ticker, day, 10.0, 10.0, idempotency_key="X", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert x.order_notional_usd == 10.0
    ledger.preserve_pending(
        ticker, day, 10.0, idempotency_key="X", recorded_at="2026-08-16T15:30:00-04:00", claim_token=x.claim_token,
    )
    y = ledger.record_contribution(
        ticker, day, 5.0, 1_000.0, idempotency_key="Y", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert y.shares_to_buy == 0
    assert ledger.pending_usd(ticker) == 15.0

    state_path = tmp_path / ticker / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    x_entry = entries[f"{day}:X"]
    y_entry = entries[f"{day}:Y"]
    assert x_entry["restored_credit_usd"] == 10.0
    assert y_entry["claim_credit_remainder_usd"] == 5.0
    # Simulate both as legacy writes: the value is present, but neither the
    # snapshot nor the tie-offset field exists at all.
    del x_entry["restored_credit_as_of_withdrawn_usd"]
    del x_entry["restored_credit_tie_offset_usd"]
    del y_entry["claim_credit_remainder_as_of_withdrawn_usd"]
    del y_entry["claim_credit_remainder_tie_offset_usd"]
    state_path.write_text(json.dumps(state, indent=2))

    # A fresh, unrelated $0 probe migrates both legacy entries in this one
    # call, without itself firing or withdrawing anything.
    probe = ledger.record_contribution(
        ticker, day, 0.0, 1_000_000.0, idempotency_key="probe", recorded_at="2026-08-16T15:30:30-04:00"
    )
    assert probe.shares_to_buy == 0
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    x_offset = entries[f"{day}:X"]["restored_credit_tie_offset_usd"]
    y_offset = entries[f"{day}:Y"]["claim_credit_remainder_tie_offset_usd"]
    assert {x_offset, y_offset} == {0.0, 10.0}, (
        f"X's restored credit (${x_offset}) and Y's live remainder (${y_offset}) "
        "share the same pool and the same backfilled snapshot -- they must be "
        "staggered against EACH OTHER (one at $0, the other at $10), not each "
        "independently reset to $0 against their own field type"
    )


@pytest.mark.parametrize("malformed_shape", [None, []])
def test_a_malformed_idempotency_key_map_fails_the_ledger_closed(tmp_path, malformed_shape):
    """Regression for "Validate the idempotency-key map before iterating" and
    "Fail closed when the claim cache map is malformed".

    setdefault() only fills recorded_idempotency_keys in when it is ABSENT,
    so a PRESENT but malformed value (null, a list) survived untouched --
    and every read site in _read_state(), including the legacy-snapshot
    backfill, unconditionally called .values()/.items()/.get() on it,
    raising AttributeError out of _read_state() itself and breaking even
    read-only operations like pending_usd() and events().

    An earlier fix reset the malformed field to {} instead. That map is the
    ONLY durable record that a given (trading_day, idempotency_key)
    contribution already happened -- pending_usd already reflects the
    original contribution, so silently forgetting the map lets a scheduler
    retry of the exact same claim look brand new and double the credit. The
    ledger must fail closed (raise) instead, like every other malformed
    core field this same block already checks.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 5.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    state["recorded_idempotency_keys"] = malformed_shape
    state_path.write_text(json.dumps(state, indent=2))

    # A read-only call must not crash with AttributeError, but must not
    # silently forget A's claim either -- fail the ticker closed instead.
    with pytest.raises(ValueError):
        ledger.pending_usd("BYDDY")
    with pytest.raises(ValueError):
        ledger.events("BYDDY")


def test_a_corrupted_withdrawal_counter_writes_off_the_entitlements_it_aged(tmp_path):
    """Codex review ("Fail corrupted withdrawal counters closed").

    Resetting the counter alone is not conservative, it is the hazard. Every
    per-claim snapshot was taken against the old, LARGER counter, so a reset to
    0.0 makes ``withdrawn_now - as_of`` negative for all of them and the aging
    subtraction becomes zero -- each claim then reads its full recorded
    entitlement, bounded only by the pool clamp, which is exactly what cannot
    tell "never spent" from "spent and refilled".

    The sequence is the one r3867623114's aging closed, reopened by the repair:
    A restores $10, B withdraws it, C contributes a fresh $10, the counter is
    corrupted, and A's retry spends C's money.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    # B drains A's restored credit back out of the shared pool.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    # C refills it with money that is entirely its own.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    assert state["withdrawn_usd_total"] > 0.0
    state["withdrawn_usd_total"] = "bad"
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "a lost counter cannot prove A's entitlement survived B's withdrawal, "
        "so A must not reach the $10 C put in"
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    # Applied on every READ, not once at repair time, so a retry that writes
    # nothing back (this one placed no order) cannot leave the write-off behind
    # for the next caller to miss.
    again = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:40:00-04:00",
    )
    assert again.shares_to_buy == 0
    assert ledger.pending_usd("BYDDY") == 10.0

    # Written off rather than deleted, so the cap stays honored: a MISSING
    # restored_credit_usd falls back to order_notional_usd, while one PRESENT
    # and 0.0 is read as a genuine "restored nothing".
    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    entry = repaired["recorded_idempotency_keys"]["2026-08-16:A"]
    assert entry["restored_credit_usd"] == 0.0
    assert entry["restored_credit_as_of_withdrawn_usd"] == 0.0
    assert _redispatchable_credit_usd(entry, 10.0, repaired["withdrawn_usd_total"]) == 0.0


def test_a_corrupted_tie_offset_larger_than_the_reconstructable_bound_writes_off_the_credit(tmp_path):
    """Codex review
    (https://github.com/badMade/all-stock-dca/pull/1168#discussion_r3900047284,
    also raised on PR #1164): ``_read_state()`` validated a persisted
    ``restored_credit_tie_offset_usd``/``claim_credit_remainder_tie_offset_usd``
    for being finite and non-negative (``_finite_non_negative()``), but never
    against the reconstructable bound implied by the OTHER claims tied at that
    exact same snapshot instant -- the only thing a legitimate tie offset can
    ever represent (see ``_allocate_credit_tie_offset()``).

    Same setup as ``test_a_corrupted_withdrawal_counter_writes_off_the_entitlements_it_aged``
    just above: A restores $10, B (a fresh, unrelated claim) withdraws that
    $10 back out of the shared pool, and C then refills the pool with $10 of
    its own money the price is too high to spend. Without any corruption, A's
    restored credit correctly ages to $0 -- the pool cannot prove A's $10
    survived B's withdrawal. Corrupting A's tie offset to a wildly oversized
    but still finite, non-negative figure (bug report's example: an offset of
    1000 against a $10 credit) used to suppress that aging entirely
    (``max(0, drawn_since_snapshot - tie_offset)`` collapses to 0 for ANY
    ``drawn_since_snapshot``), leaving A fully redispatchable from the pool
    C's unrelated money refilled -- exactly the ownership breach the tie
    offset exists to prevent.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    # B is a fresh, unrelated claim that drains A's restored credit back out
    # of the shared pool -- a genuine withdrawal, not a redispatch of A.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    # C refills the pool with money that is entirely its own (priced too high
    # to fire, so it just sits in pending_usd).
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    a_entry = entries["2026-08-16:A"]
    assert a_entry["restored_credit_usd"] == 10.0
    assert a_entry["restored_credit_tie_offset_usd"] == 0.0
    # A is the only claim tied at its snapshot, so the legitimate offset is
    # $0 -- corrupt it to a wildly oversized but still finite, non-negative
    # figure, matching the bug report's example.
    a_entry["restored_credit_tie_offset_usd"] = 1000.0
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "an unprovable tie offset cannot excuse A's credit from B's "
        "withdrawal, so A must not reach the $10 C put in"
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    entry = repaired["recorded_idempotency_keys"]["2026-08-16:A"]
    assert entry["restored_credit_usd"] == 0.0
    assert entry["restored_credit_tie_offset_usd"] == 0.0
    assert _redispatchable_credit_usd(entry, 10.0, repaired["withdrawn_usd_total"]) == 0.0


def test_a_corrupted_offset_matching_the_claims_own_amount_is_still_caught(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Exclude the
    current registration from the offset bound" and "Record the bound
    before later same-anchor registrations"): two DERIVED-bound attempts in
    a row both let a corrupted offset rise as far as some other amount
    present in the ledger (first the claim's own contribution, then a
    later-registered sibling's) before being caught. The durable, write-once
    ``credit_tie_offset_audit`` record sidesteps deriving a bound at all --
    it stores the EXACT offset ``_allocate_credit_tie_offset()`` computed
    for this exact (claim, field, snapshot), so any deviation, by any
    amount, in any direction, is caught by a plain equality check.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    a_entry = entries["2026-08-16:A"]
    assert a_entry["restored_credit_usd"] == 10.0
    assert a_entry["restored_credit_tie_offset_usd"] == 0.0
    audit_key = f"2026-08-16:A:restored_credit_usd:{_snapshot_key(a_entry['restored_credit_as_of_withdrawn_usd'])}"
    assert state["credit_tie_offset_audit"][audit_key] == 0.0
    # Corrupt the offset to exactly A's own $10 -- a value a purely derived
    # bound (this PR's two earlier, rejected attempts) would have accepted,
    # but which does not match A's own audited $0.
    a_entry["restored_credit_tie_offset_usd"] = 10.0
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "an offset equal to the claim's own contribution is still corrupted -- "
        "nothing preceded a sole claim at its anchor, so the true bound is $0"
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    entry = repaired["recorded_idempotency_keys"]["2026-08-16:A"]
    assert entry["restored_credit_usd"] == 0.0
    assert _redispatchable_credit_usd(entry, 10.0, repaired["withdrawn_usd_total"]) == 0.0


def test_a_corrupted_offset_matching_a_later_registered_siblings_amount_is_still_caught(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Record the bound
    before later same-anchor registrations"): the durable per-anchor
    high-water mark, even with this entry's own current amount excluded,
    still includes whatever a DIFFERENT claim registered into the SAME
    anchor AFTER this one. A registers $10 at offset $0; B then registers
    $10 at the same anchor, offset $10 (history $20). A's true offset is
    $0 -- nothing preceded it -- but a bound of (history $20 - A's own $10
    = $10) still accepted a corrupted offset of $10, which is really B's
    slice, not A's: if a subsequent $10 withdrawal genuinely consumes A's
    interval, the corrupted offset would let A redispatch B's money. The
    write-once audit record fixes this exactly: A's audited offset is $0,
    so ANY other value -- including one that happens to equal a sibling's
    own amount -- fails the equality check.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    fires = {}
    for key, at in (
        ("A", "2026-08-16T15:30:00-04:00"),
        ("B", "2026-08-16T15:31:00-04:00"),
    ):
        fires[key] = ledger.record_contribution(
            "BYDDY", day, 10.0, 10.0, idempotency_key=key, recorded_at=at
        )
        assert fires[key].order_notional_usd == 10.0

    for key, at in (
        ("A", "2026-08-16T15:32:00-04:00"),
        ("B", "2026-08-16T15:33:00-04:00"),
    ):
        ledger.preserve_pending(
            "BYDDY", day, 10.0, idempotency_key=key, recorded_at=at, claim_token=fires[key].claim_token
        )
    assert ledger.pending_usd("BYDDY") == 20.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    a_entry = entries[f"{day}:A"]
    assert a_entry["restored_credit_tie_offset_usd"] == 0.0
    assert entries[f"{day}:B"]["restored_credit_tie_offset_usd"] == 10.0
    # Corrupt A's offset to exactly B's own $10 -- a value the previous
    # (rejected) bound of (history - A's own amount) would have accepted.
    a_entry["restored_credit_tie_offset_usd"] = 10.0
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", day, 0.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert retry.shares_to_buy == 0, (
        "A's offset must equal exactly what was audited for it ($0), not a "
        "value that merely happens to match a sibling's own contribution"
    )
    assert ledger.pending_usd("BYDDY") == 20.0


def test_a_present_but_unreadable_audit_value_fails_the_entitlement_closed(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Reject
    malformed present audit records"): a genuinely MISSING audit key is
    trusted (cannot be disproven), but a key that IS present with an
    unreadable value (``null``, ``"NaN"``, a JSON boolean, ...) is a
    different thing -- an audit record was written for this exact
    registration and has since been corrupted. Routing that through the
    same "no audit, trust it" path as a genuinely absent key would let a
    corrupted live offset stand simply by ALSO corrupting its own audit
    entry, reopening the exact ownership breach this audit trail exists to
    close.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    first = ledger.record_contribution(
        "BYDDY", day, 10.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    ledger.record_contribution(
        "BYDDY", day, 0.0, 10.0, idempotency_key="B", recorded_at="2026-08-16T15:31:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", day, 10.0, 25.0, idempotency_key="C", recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    audit_key = f"{day}:A:restored_credit_usd:{_snapshot_key(a_entry['restored_credit_as_of_withdrawn_usd'])}"
    assert audit_key in state["credit_tie_offset_audit"]
    # Corrupt BOTH the live offset AND its own audit entry to something
    # oversized-but-unreadable -- without the fix, the malformed audit
    # value is treated exactly like a missing key and the corrupted live
    # offset is trusted outright.
    a_entry["restored_credit_tie_offset_usd"] = 1000.0
    state["credit_tie_offset_audit"][audit_key] = "NaN"
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", day, 0.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:35:00-04:00"
    )
    assert retry.shares_to_buy == 0, (
        "a present-but-unreadable audit entry must fail the entitlement "
        "closed, not be trusted like a genuinely absent one"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_writing_off_a_mismatch_retires_the_stale_audit_entry(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Persist or
    retire the audit after writing off an entitlement"): when a mismatch
    write-off zeroes a claim's live field, the stale audit entry that
    caused the mismatch must be retired too. Left behind, a caller that
    goes on to persist this corrected state (every mutating method reads
    via ``_read_state()`` and later writes the same dict back) still
    carries the stale audit forward, and the audit/live pair mismatches
    again on every future load even after the live value is durably $0.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    first = ledger.record_contribution(
        "BYDDY", day, 10.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    as_of = a_entry["restored_credit_as_of_withdrawn_usd"]
    audit_key = f"{day}:A:restored_credit_usd:{_snapshot_key(as_of)}"
    assert state["credit_tie_offset_audit"][audit_key] == 0.0
    # Corrupt the live offset so the next load writes it off.
    a_entry["restored_credit_tie_offset_usd"] = 1000.0
    state_path.write_text(json.dumps(state, indent=2))

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_a = repaired["recorded_idempotency_keys"][f"{day}:A"]
    assert repaired_a["restored_credit_usd"] == 0.0
    assert audit_key not in repaired.get("credit_tie_offset_audit", {}), (
        "the stale audit entry that caused this mismatch must be retired "
        "alongside the write-off, not left behind to mismatch again"
    )


def test_a_legacy_initialized_credit_is_not_written_off_by_an_unrelated_same_counter_claim(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Exempt
    legacy-initialized credits from unrelated history"): a credit upgraded
    by ``_initialize_restored_credit_aging()`` gets its snapshot/offset
    assigned directly, without going through ``_allocate_credit_tie_offset()``
    -- so it carries no ``credit_tie_offset_audit`` entry at all. A fresh,
    unrelated, CURRENT-format claim later registering at the exact same
    withdrawal-counter instant must not retroactively make the legacy
    credit's un-audited offset look wrong: with no audit record for it,
    validation must trust it rather than write it off.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    # A legacy restored credit: present, but with no snapshot/offset/audit
    # fields at all, as if written before any of them existed.
    first = ledger.record_contribution(
        "BYDDY", day, 10.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    del a_entry["restored_credit_as_of_withdrawn_usd"]
    del a_entry["restored_credit_tie_offset_usd"]
    state.pop("credit_tie_offset_audit", None)
    state_path.write_text(json.dumps(state, indent=2))

    # A fresh, unrelated CURRENT-format claim (B) registers at the exact
    # same withdrawn_usd_total instant this load's migration backfills A's
    # snapshot to -- exercising _allocate_credit_tie_offset() for B (which
    # DOES write an audit entry) immediately after A's un-audited migration.
    b = ledger.record_contribution(
        "BYDDY", day, 5.0, 100.0, idempotency_key="B", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert b.shares_to_buy == 0

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_a = repaired["recorded_idempotency_keys"][f"{day}:A"]
    assert repaired_a["restored_credit_usd"] == 10.0, (
        "the legacy credit has no audit record to fail, so it must be trusted, "
        "not written off because of an unrelated claim registering afterward"
    )
    withdrawn_total = repaired["withdrawn_usd_total"]
    pool = repaired["pending_usd"]
    assert _redispatchable_credit_usd(repaired_a, pool, withdrawn_total) == 10.0


def test_re_anchoring_after_a_missing_counter_forgets_stale_audits_first(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Synchronize
    audits when migration rewrites offsets"): the "counter field is
    entirely ABSENT" upgrade-boundary path re-anchors every existing
    _AS_OF snapshot to the current (usually $0) counter and forcibly resets
    every offset to $0 -- without forgetting the old audit entry that
    offset supersedes. Two credits already tied at withdrawn_usd_total==0.0
    (A offset $0, B offset $10, both audited) are UNCHANGED by the
    re-anchor's own as_of rewrite (0.0 -> 0.0), so B's forced offset reset
    to $0 collides with its own still-present $10 audit entry the moment
    this same load's validation pass runs -- writing off B's perfectly
    legitimate $10 credit, not merely logging a stale warning.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    # Built through the real API so both entries have a realistic shape,
    # then hand-edited to the as_of==0.0 tie this migration path can't
    # itself distinguish from a coincidental re-anchor collision.
    first = ledger.record_contribution(
        "BYDDY", day, 10.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    second = ledger.record_contribution(
        "BYDDY", day, 10.0, 10.0, idempotency_key="B", recorded_at="2026-08-16T15:31:00-04:00"
    )
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="B", recorded_at="2026-08-16T15:31:00-04:00", claim_token=second.claim_token,
    )

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    a_entry = entries[f"{day}:A"]
    b_entry = entries[f"{day}:B"]
    audit = state["credit_tie_offset_audit"]
    # Move both credits (and their audit entries) down to as_of==0.0,
    # tied at offsets $0 and $10 -- exactly the state the re-anchor path
    # cannot tell apart from "was already there before the counter went
    # missing".
    for entry, offset in ((a_entry, 0.0), (b_entry, 10.0)):
        old_key = f"{day}:{'A' if entry is a_entry else 'B'}:restored_credit_usd:{_snapshot_key(entry['restored_credit_as_of_withdrawn_usd'])}"
        audit.pop(old_key, None)
        entry["restored_credit_as_of_withdrawn_usd"] = 0.0
        entry["restored_credit_tie_offset_usd"] = offset
        audit[f"{day}:{'A' if entry is a_entry else 'B'}:restored_credit_usd:0.00"] = offset
    del state["withdrawn_usd_total"]
    state_path.write_text(json.dumps(state, indent=2))

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_b = repaired["recorded_idempotency_keys"][f"{day}:B"]
    assert repaired_b["restored_credit_usd"] == 10.0, (
        "B's legitimate $10 credit must survive the re-anchor -- the forced "
        "offset reset to $0 must not collide with B's own stale $10 audit "
        "entry from before the counter went missing"
    )
    withdrawn_total = repaired["withdrawn_usd_total"]
    pool = repaired["pending_usd"]
    assert _redispatchable_credit_usd(repaired_b, pool, withdrawn_total) == 10.0


def test_the_as_of_backfill_migration_forgets_a_stale_audit_at_its_target_key(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, second example:
    "backfilling a missing as-of field can re-stagger an audited offset and
    trigger the same loss"): the as_of-backfill loop (for an entry whose
    ``restored_credit_as_of_withdrawn_usd`` is entirely ABSENT) assigns a
    fresh as_of/offset pair directly, not through
    ``_allocate_credit_tie_offset()``, so it never writes its own audit
    entry -- but if a stale audit entry already happens to sit at the exact
    (claim, field, counter) key this backfill is about to target, the
    freshly-assigned offset must not be judged against it.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    first = ledger.record_contribution(
        "BYDDY", day, 10.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    withdrawn_total = state["withdrawn_usd_total"]
    assert a_entry["restored_credit_as_of_withdrawn_usd"] == withdrawn_total

    # Simulate a legacy/partially-edited entry: as_of entirely absent, so
    # the next load's backfill loop will re-anchor it fresh at the CURRENT
    # counter (unchanged here) with a freshly-staggered $0 offset.
    del a_entry["restored_credit_as_of_withdrawn_usd"]
    del a_entry["restored_credit_tie_offset_usd"]
    # Plant a stale, WRONG audit entry at the exact key the backfill is
    # about to target -- simulating leftover garbage from some unrelated
    # earlier registration at this same counter value.
    state["credit_tie_offset_audit"][f"{day}:A:restored_credit_usd:{_snapshot_key(withdrawn_total)}"] = 999.0
    state_path.write_text(json.dumps(state, indent=2))

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_a = repaired["recorded_idempotency_keys"][f"{day}:A"]
    assert repaired_a["restored_credit_usd"] == 10.0, (
        "the fresh backfilled offset must not be judged against a stale "
        "audit entry planted at the same target key"
    )
    pool = repaired["pending_usd"]
    assert _redispatchable_credit_usd(repaired_a, pool, repaired["withdrawn_usd_total"]) == 10.0


def test_retiring_an_interval_keeps_the_shifted_siblings_audit_in_sync(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Keep audits
    synchronized when shifting tie offsets"): ``_retire_credit_tie_interval()``
    shifts a LATER-registered sibling's live offset down to close the gap a
    retired registration leaves behind, but this PR's audit trail only wrote
    an audit entry the moment ``_allocate_credit_tie_offset()`` first
    computed a value -- not when a legitimate shift changes it afterward. A
    fires $10 leaving a $4 remainder (offset $0); B then adds $4 at the same
    withdrawal-counter instant (offset $4, audited as $4). Preserving A's
    order retires A's $4 registration, shifting B's LIVE offset down to $0
    -- but without syncing the audit too, B's now-stale $4 audit entry would
    mismatch its own correctly-shifted $0 live value on the very next load,
    writing off a perfectly legitimate credit.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    first = ledger.record_contribution(
        "BYDDY", day, 14.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.shares_to_buy == 1
    assert first.order_notional_usd == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    assert a_entry["claim_credit_remainder_usd"] == 4.0
    assert a_entry["claim_credit_remainder_tie_offset_usd"] == 0.0

    # B ties at the exact same withdrawal-counter instant (no fire, no
    # withdrawal in between) and is offset by A's own $4 registration.
    b = ledger.record_contribution(
        "BYDDY", day, 4.0, 100.0, idempotency_key="B", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert b.shares_to_buy == 0
    state = json.loads(state_path.read_bytes())
    b_entry = state["recorded_idempotency_keys"][f"{day}:B"]
    assert b_entry["claim_credit_remainder_usd"] == 4.0
    assert b_entry["claim_credit_remainder_tie_offset_usd"] == 4.0

    # Preserving A's order retires A's $4 registration and shifts B's LIVE
    # offset down to close the gap.
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:32:00-04:00", claim_token=first.claim_token,
    )
    state = json.loads(state_path.read_bytes())
    b_entry = state["recorded_idempotency_keys"][f"{day}:B"]
    assert b_entry["claim_credit_remainder_tie_offset_usd"] == 0.0, "B's live offset must be shifted"
    audit_key = f"{day}:B:claim_credit_remainder_usd:{_snapshot_key(b_entry['claim_credit_remainder_as_of_withdrawn_usd'])}"
    assert state["credit_tie_offset_audit"][audit_key] == 0.0, (
        "B's audit entry must move with its shifted live offset"
    )

    from dca_whole_share_accumulator import _claim_credit_remainder_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_b = repaired["recorded_idempotency_keys"][f"{day}:B"]
    assert repaired_b["claim_credit_remainder_usd"] == 4.0, (
        "B's legitimately-shifted credit must survive reload, not be written "
        "off by a stale audit entry that never moved with it"
    )
    withdrawn_total = repaired["withdrawn_usd_total"]
    pool = repaired["pending_usd"]
    assert _claim_credit_remainder_usd(repaired_b, pool, withdrawn_total) == 4.0


def test_a_relabeled_restored_credit_gets_its_own_audit_entry(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Record an audit
    for relabeled restored credits"): when a stale claim reprices to zero
    shares, its ``claim_credit_remainder_usd`` (already audited) is
    relabeled onto ``restored_credit_usd`` reusing the SAME offset value --
    but that reuse only copied the live field, not a matching audit entry
    under ``restored_credit_usd``'s own audit key. Without one,
    ``_read_state()`` finds no audit record for (claim, restored_credit_usd,
    snapshot) and trusts ANY value stored there, including a later
    corruption.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    first = ledger.record_contribution(
        "BYDDY", day, 14.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.shares_to_buy == 1
    assert first.order_notional_usd == 10.0

    # Reclaim A at a price high enough that its frozen $14 total no longer
    # buys even one share -- the "resolved: repriced to zero shares" branch
    # that relabels the remainder as restored credit.
    repriced = ledger.record_contribution(
        "BYDDY", day, 0.0, 1000.0, idempotency_key="A", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert repriced.shares_to_buy == 0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    # The reclaim releases A's WHOLE frozen commitment (its original $10
    # order plus the $4 remainder) back to pending, not just the remainder.
    assert a_entry["restored_credit_usd"] == 14.0
    assert a_entry["restored_credit_tie_offset_usd"] == 0.0
    audit_key = f"{day}:A:restored_credit_usd:{_snapshot_key(a_entry['restored_credit_as_of_withdrawn_usd'])}"
    assert state["credit_tie_offset_audit"][audit_key] == 0.0, (
        "the relabeled restored credit must have its OWN audit entry, not "
        "rely solely on the remainder's now-superseded one"
    )

    # Corrupt the relabeled offset -- without the fix, no audit entry exists
    # under restored_credit_usd's own key, so this would be trusted outright.
    a_entry["restored_credit_tie_offset_usd"] = 5.0
    state_path.write_text(json.dumps(state, indent=2))

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_a = repaired["recorded_idempotency_keys"][f"{day}:A"]
    assert repaired_a["restored_credit_usd"] == 0.0, (
        "a corrupted offset on the relabeled field must be caught against "
        "its own audit entry, not silently trusted"
    )
    withdrawn_total = repaired["withdrawn_usd_total"]
    pool = repaired["pending_usd"]
    assert _redispatchable_credit_usd(repaired_a, pool, withdrawn_total) == 0.0


def test_relabeling_a_remainder_with_a_nonzero_offset_forgets_its_own_stale_audit(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Retire the
    remainder audit when relabeling credit"): the previous fix wrote a NEW
    audit entry for the relabeled ``restored_credit_usd`` field, but never
    cleared the remainder's own now-superseded entry before resetting the
    live ``claim_credit_remainder_tie_offset_usd`` field to $0. When that
    remainder's offset was $0 to begin with, resetting it to $0 trivially
    matches its own audit and nothing is exposed -- so this needs a claim
    whose remainder ties AFTER another claim's own interval, giving it a
    NONZERO offset, to actually surface the stale mismatch.

    B first registers a $4 interval at the same withdrawal-counter instant.
    A's own $4 remainder (from a fire that already happened) then reclaims
    at a price too high to buy anything: this retires A's OLD remainder
    registration, shifting B's offset down to $0, and re-registers A's
    WHOLE frozen $14 commitment fresh -- landing AFTER B's now-$0 interval,
    at a NONZERO offset. Relabeling that onto restored_credit_usd resets
    the live remainder offset to $0 while its own audit entry (just written
    moments earlier by that same fresh registration) still holds the
    nonzero value -- exactly the mismatch this fix must forget.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    first = ledger.record_contribution(
        "BYDDY", day, 14.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert first.shares_to_buy == 1
    assert first.order_notional_usd == 10.0

    # B ties at the exact same withdrawal-counter instant as A's remainder
    # (no fire, no withdrawal in between).
    b = ledger.record_contribution(
        "BYDDY", day, 4.0, 100.0, idempotency_key="B", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert b.shares_to_buy == 0

    # Reclaim A at a price high enough that its frozen $14 total no longer
    # buys even one share. This retires A's old $4 remainder registration
    # (shifting B's offset down to $0) and re-registers A's whole $14 fresh,
    # landing after B's interval at a nonzero offset.
    repriced = ledger.record_contribution(
        "BYDDY", day, 0.0, 1000.0, idempotency_key="A", recorded_at="2026-08-16T15:32:00-04:00"
    )
    assert repriced.shares_to_buy == 0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    assert a_entry["restored_credit_usd"] == 14.0
    assert a_entry["restored_credit_tie_offset_usd"] > 0.0, (
        "the setup must give A's relabeled offset a NONZERO value -- "
        "otherwise resetting the remainder to $0 trivially matches it"
    )
    assert a_entry["claim_credit_remainder_usd"] == 0.0
    assert a_entry["claim_credit_remainder_tie_offset_usd"] == 0.0
    remainder_audit_key = (
        f"{day}:A:claim_credit_remainder_usd:{_snapshot_key(a_entry['claim_credit_remainder_as_of_withdrawn_usd'])}"
    )
    assert remainder_audit_key not in state["credit_tie_offset_audit"], (
        "A's superseded remainder audit entry must be forgotten, not left "
        "behind holding a stale nonzero value that mismatches the reset $0"
    )

    # A fresh, ordinary load must not treat A's own legitimate reset as
    # corruption -- no warning-driven write-off, and the relabeled restored
    # credit must survive untouched.
    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_a = repaired["recorded_idempotency_keys"][f"{day}:A"]
    assert repaired_a["restored_credit_usd"] == 14.0


def test_a_claim_moving_to_a_new_snapshot_does_not_write_off_a_tied_siblings_offset(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Preserve offsets
    after preceding claims move snapshots"): the reconstructable-bound check
    above must not group claims strictly by their CURRENT ``_AS_OF``
    snapshot value. A and B tie at the same snapshot (A offset $0, B offset
    $10). A is later redispatched -- a real withdrawal, advancing the
    counter -- and preserved again, which overwrites A's OWN snapshot/offset
    fields in place with no historical trace left behind at the OLD
    snapshot (see the "prior_*_as_of == withdrawn_total" guards around every
    _retire_credit_tie_interval() call site: a claim's own real withdrawal
    deliberately does NOT retire its old interval, precisely so it keeps
    protecting whichever claim was tied after it). A bound scoped to "still
    at this exact snapshot" would then see nothing left at A's old snapshot
    to justify B's $10 offset and write off B's still-legitimate
    entitlement -- even though the withdrawal that happened came entirely
    out of A's own slice, not B's.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    fires = {}
    for key, at in (
        ("A", "2026-08-16T15:30:00-04:00"),
        ("B", "2026-08-16T15:31:00-04:00"),
    ):
        fires[key] = ledger.record_contribution(
            "BYDDY", day, 10.0, 10.0, idempotency_key=key, recorded_at=at
        )
        assert fires[key].order_notional_usd == 10.0

    for key, at in (
        ("A", "2026-08-16T15:32:00-04:00"),
        ("B", "2026-08-16T15:33:00-04:00"),
    ):
        ledger.preserve_pending(
            "BYDDY", day, 10.0, idempotency_key=key, recorded_at=at, claim_token=fires[key].claim_token
        )
    assert ledger.pending_usd("BYDDY") == 20.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    assert entries[f"{day}:A"]["restored_credit_tie_offset_usd"] == 0.0
    assert entries[f"{day}:B"]["restored_credit_tie_offset_usd"] == 10.0

    # A redispatches its own $10 -- a real, cleanly-sized withdrawal funded
    # entirely by A's own restored credit -- and that fire is then preserved
    # again, moving A on to a brand new snapshot.
    a_redispatch = ledger.record_contribution(
        "BYDDY", day, 0.0, 10.0, idempotency_key="A", recorded_at="2026-08-16T15:34:00-04:00"
    )
    assert a_redispatch.order_notional_usd == 10.0
    ledger.preserve_pending(
        "BYDDY", day, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00", claim_token=a_redispatch.claim_token,
    )
    assert ledger.pending_usd("BYDDY") == 20.0

    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    a_entry = entries[f"{day}:A"]
    b_entry = entries[f"{day}:B"]
    assert a_entry["restored_credit_as_of_withdrawn_usd"] != b_entry["restored_credit_as_of_withdrawn_usd"], (
        "A must have moved on to a snapshot distinct from B's original one"
    )
    assert b_entry["restored_credit_tie_offset_usd"] == 10.0, (
        "B's own offset/snapshot are untouched by A's move"
    )

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_b = repaired["recorded_idempotency_keys"][f"{day}:B"]
    assert repaired_b["restored_credit_usd"] == 10.0, (
        "B's entitlement must survive reload -- the withdrawal since B's snapshot "
        "came entirely out of A's own slice, which B's untouched offset already "
        "accounts for, so there is nothing here to write off"
    )
    withdrawn_total = repaired["withdrawn_usd_total"]
    pool = repaired["pending_usd"]
    assert _redispatchable_credit_usd(repaired_b, pool, withdrawn_total) == 10.0


def test_a_shrinking_sibling_credit_does_not_write_off_a_tied_claims_offset(tmp_path):
    """Codex review (PR #1170 discussion on this same fix, "Preserve
    historical width after preceding credits shrink" / "Exclude the claim's
    sibling credit from its offset bound"): validating a tie offset against
    the SUM of every other entry's CURRENT recorded amount is unsound in a
    second way beyond the snapshot-move case above -- a claim's current
    amount can legitimately shrink below what it was when a SIBLING's offset
    was originally earned against it. A partial redispatch draws down only
    part of a claim's credit; preserving that partial fire re-registers the
    claim at its new, smaller remaining balance (and can leave BOTH a stale
    ``restored_credit_usd`` and a fresh ``claim_credit_remainder_usd`` on the
    SAME entry at once, from the two different registrations). A bound based
    on current amounts would then read less total money in the ledger than
    what legitimately justified the sibling's offset, and write off a still-
    valid entitlement.

    A and B tie $10 each at the same snapshot (A offset $0, B offset $10).
    A is then partially redispatched -- firing on only part of its own
    credit, leaving a remainder -- which shrinks what A currently holds
    without touching B at all. B's offset must still validate against the
    durable historical batch width recorded when both A and B originally
    registered, not against A's now-smaller current balance.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    fires = {}
    for key, at in (
        ("A", "2026-08-16T15:30:00-04:00"),
        ("B", "2026-08-16T15:31:00-04:00"),
    ):
        fires[key] = ledger.record_contribution(
            "BYDDY", day, 10.0, 10.0, idempotency_key=key, recorded_at=at
        )
        assert fires[key].order_notional_usd == 10.0

    for key, at in (
        ("A", "2026-08-16T15:32:00-04:00"),
        ("B", "2026-08-16T15:33:00-04:00"),
    ):
        ledger.preserve_pending(
            "BYDDY", day, 10.0, idempotency_key=key, recorded_at=at, claim_token=fires[key].claim_token
        )
    assert ledger.pending_usd("BYDDY") == 20.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    assert entries[f"{day}:A"]["restored_credit_tie_offset_usd"] == 0.0
    assert entries[f"{day}:B"]["restored_credit_tie_offset_usd"] == 10.0

    # A redispatches at a price that only partially spends its own $10
    # credit ($10 // $9 = 1 share, $9 notional, $1 left over) -- a real,
    # smaller withdrawal funded entirely by A's own money, leaving A's
    # entry with a stale restored_credit_usd (from the original preserve
    # above) alongside a fresh claim_credit_remainder_usd (from this fire).
    a_redispatch = ledger.record_contribution(
        "BYDDY", day, 0.0, 9.0, idempotency_key="A", recorded_at="2026-08-16T15:34:00-04:00"
    )
    assert a_redispatch.order_notional_usd == 9.0

    state = json.loads(state_path.read_bytes())
    a_entry = state["recorded_idempotency_keys"][f"{day}:A"]
    assert "restored_credit_usd" in a_entry and "claim_credit_remainder_usd" in a_entry, (
        "A must carry both a stale restored credit and a fresh remainder at once"
    )

    from dca_whole_share_accumulator import _redispatchable_credit_usd

    repaired = ledger._read_state(state_path, "BYDDY")
    repaired_b = repaired["recorded_idempotency_keys"][f"{day}:B"]
    assert repaired_b["restored_credit_usd"] == 10.0, (
        "B's entitlement must survive reload -- A's own current balance shrinking "
        "does not shrink the historical batch width B's offset was validated "
        "against when both A and B originally tied"
    )
    assert repaired_b["restored_credit_tie_offset_usd"] == 10.0
    withdrawn_total = repaired["withdrawn_usd_total"]
    pool = repaired["pending_usd"]
    assert _redispatchable_credit_usd(repaired_b, pool, withdrawn_total) > 0.0


def test_a_corrupted_withdrawal_counter_writes_off_a_legacy_entry_too(tmp_path):
    """Regression for "Invalidate legacy fallback credits after counter
    corruption".

    A resolved claim written BEFORE restored_credit_usd existed carries no
    such field at all, so the corrupted-counter write-off loop (which only
    zeroed an entry where the field was ALREADY present) left it untouched.
    _redispatchable_credit_usd() then takes its legacy order_notional_usd
    fallback with NO withdrawal aging applied -- the counter that aging
    depends on was just lost -- so a zero-contribution retry of the legacy
    claim could still reserve its full original notional from a pool
    replenished entirely by an unrelated later claim.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    # B drains A's restored credit back out of the shared pool.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    # C refills it with money that is entirely its own.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    # Simulate a legacy write: A's entry predates restored_credit_usd
    # entirely -- no field, not even a null one -- but still carries the
    # positive order_notional_usd its preserve_pending() call recorded.
    entry = state["recorded_idempotency_keys"]["2026-08-16:A"]
    assert entry["order_notional_usd"] == 10.0
    del entry["restored_credit_usd"]
    del entry["restored_credit_as_of_withdrawn_usd"]
    state["withdrawn_usd_total"] = "bad"
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "a lost counter cannot prove the legacy entry's entitlement survived "
        "B's withdrawal, so A must not reach the $10 C put in"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_finite_non_negative_rejects_integer_overflow():
    """Codex review ("Handle integer overflow while scanning cached
    claims"): a JSON integer too large to convert to a float (e.g. a
    persisted ``claim_credit_remainder_usd: 10**400``) raises
    ``OverflowError`` from ``float()``, not ``TypeError``/``ValueError`` --
    and every caller of ``_finite_non_negative()`` relies on it to fail
    closed on exactly this kind of corrupted numeric field.
    """
    from dca_whole_share_accumulator import _finite_non_negative

    assert _finite_non_negative(10**400) is None
    assert _finite_non_negative(-(10**400)) is None


def test_a_ledger_wide_integer_overflow_does_not_abort_unrelated_reads(tmp_path):
    """Regression for "Handle integer overflow while scanning cached
    claims": left uncaught, a single oversized JSON integer anywhere in
    ``recorded_idempotency_keys`` raised ``OverflowError`` out of the new
    tie-offset validation pass in ``_read_state()`` -- which runs on every
    load -- taking the WHOLE ticker down (even read-only callers like
    ``pending_usd()``) over one unrelated corrupted field, instead of
    writing off just that one entitlement.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    entries["2026-08-16:A"]["restored_credit_usd"] = 10**400
    state_path.write_text(json.dumps(state, indent=2))

    # Must not raise OverflowError -- the corrupted entry is written off,
    # not fatal to the whole ticker. pending_usd itself is untouched by the
    # corruption (a separate field); this call merely proves _read_state()
    # completes instead of raising.
    assert ledger.pending_usd("BYDDY") == 10.0
    assert ledger.events("BYDDY") is not None


def test_finite_non_negative_rejects_json_booleans():
    """Regression for "Reject booleans in persisted dollar fields".

    bool is a subclass of int, so float(False)/float(True) succeed as
    0.0/1.0 with no TypeError/ValueError to catch -- a persisted dollar
    field corrupted to a JSON boolean was read as a "valid" number instead
    of the unusable value it actually is. These fields are numeric, never
    truth-valued.
    """
    from dca_whole_share_accumulator import _finite_non_negative

    assert _finite_non_negative(False) is None
    assert _finite_non_negative(True) is None
    assert _finite_non_negative(0.0) == 0.0
    assert _finite_non_negative(1) == 1.0


def test_a_boolean_withdrawal_counter_writes_off_the_entitlements_it_aged(tmp_path):
    """Regression for "Reject booleans in persisted dollar fields".

    A corrupted withdrawn_usd_total: false was read as a valid 0.0 by
    _finite_non_negative()'s old float()-only conversion, so it never
    triggered the entitlement invalidation _read_state() relies on for a
    lost counter. A restored credit whose snapshot is above zero then
    received no aging, letting a claim redispatch against a pool an
    unrelated later contribution replenished.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    # B drains A's restored credit back out of the shared pool.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    # C refills it with money that is entirely its own.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    state["withdrawn_usd_total"] = False
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "a boolean-corrupted counter cannot prove A's entitlement survived "
        "B's withdrawal, so A must not reach the $10 C put in"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_a_reclaim_cannot_reprice_into_another_claims_contribution(tmp_path):
    """Codex review ("Cap repricing by the claim's surviving remainder").

    The reprice was sized from the frozen basis + contribution, which assumes
    the claim's original remainder is still exclusively its own, and the
    balance check downstream only asks whether the POOL can cover the increase
    -- which it can, out of somebody else's money. A claim can only command
    what it still OWNS: the money reserved in its outstanding order, plus
    whatever of its own remainder survives.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    # A fires $16 of a $20 basis at $8/share, leaving $4 of its own.
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 8.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    assert (first.shares_to_buy, first.order_notional_usd) == (2, 16.0)
    assert ledger.pending_usd("BYDDY") == 4.0

    # B consumes exactly that $4.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 4.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 0.0

    # C contributes an unrelated $4, priced too high to fire.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 4.0, 99.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 4.0

    # A goes stale and is reclaimed at a price that would raise its order to
    # $20 off the frozen basis -- $4 more than it reserved, and the only $4 in
    # the pool is C's.
    reclaim = ledger.record_contribution(
        "BYDDY", "2026-08-16", 20.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:42:00-04:00",
    )
    assert reclaim.order_notional_usd <= 16.0, (
        "A reserved $16 and B took its $4 remainder; repricing past $16 spends C's money"
    )
    assert (reclaim.shares_to_buy, reclaim.order_notional_usd) == (1, 10.0)
    # C's $4 is still there, alongside the $6 A released by repricing down.
    assert ledger.pending_usd("BYDDY") == 10.0


def test_a_malformed_restored_credit_does_not_inherit_the_legacy_entitlement(tmp_path):
    """Codex review ("Write off malformed restored credit instead of falling
    back").

    The legacy fallback exists for an entry written before restored_credit_usd
    existed -- one where the field is ABSENT -- and it hands back
    order_notional_usd with no withdrawal aging applied. A field that is
    PRESENT and unreadable is a different thing: the entry is new enough to
    have had a snapshot, so granting it the un-aged legacy figure lets a claim
    whose restored money another claim already withdrew spend an unrelated
    later contribution up to that old notional.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:A"]
    # PRESENT but unreadable, and the entry still carries its order_notional_usd.
    entry["restored_credit_usd"] = "Infinity"
    assert entry["order_notional_usd"] > 0
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "a present-but-unreadable credit must fail closed, not inherit the "
        "un-aged legacy figure and reach C's contribution"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_a_null_restored_credit_does_not_inherit_the_legacy_entitlement(tmp_path):
    """Codex review ("Treat null restored credits as invalid").

    JSON ``null`` deserializes to Python ``None``, which the previous presence
    check (``cached.get(field) is not None``) treated as equivalent to the
    field being ABSENT -- routing a claim that HAS a (blanked-out) snapshot
    onto the legacy, un-aged ``order_notional_usd`` fallback meant only for
    entries written before this field existed at all. ``_finite_non_negative``
    already rejects ``None`` the same way it rejects "NaN"/"Infinity"; the
    fix is recognizing the field as PRESENT so that rejection fails the claim
    closed instead of falling through to the legacy path.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:A"]
    # PRESENT as JSON null, and the entry still carries its order_notional_usd.
    entry["restored_credit_usd"] = None
    assert entry["order_notional_usd"] > 0
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "a present-but-null credit must fail closed, not inherit the un-aged "
        "legacy figure and reach C's contribution"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


def test_a_non_finite_legacy_order_notional_fails_closed(tmp_path):
    """Codex review ("Reject non-finite legacy fallback notionals").

    The legacy fallback path (restored_credit_usd genuinely ABSENT) reads
    order_notional_usd directly. A malformed "NaN"/"Infinity" there must not
    pass through float() and hand back the whole pending pool.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00",
    )
    ledger.preserve_pending(
        "BYDDY", "2026-08-16", 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:30:00-04:00", claim_token=first.claim_token,
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="B",
        recorded_at="2026-08-16T15:31:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 25.0, idempotency_key="C",
        recorded_at="2026-08-16T15:32:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entry = state["recorded_idempotency_keys"]["2026-08-16:A"]
    # restored_credit_usd genuinely ABSENT (a pre-upgrade entry), but its
    # order_notional_usd is corrupted.
    del entry["restored_credit_usd"]
    del entry["restored_credit_as_of_withdrawn_usd"]
    entry["order_notional_usd"] = "Infinity"
    state_path.write_text(json.dumps(state, indent=2))

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="A",
        recorded_at="2026-08-16T15:35:00-04:00",
    )
    assert retry.shares_to_buy == 0, (
        "a non-finite legacy order_notional_usd must fail closed rather than "
        "hand back the entire pending pool, including C's contribution"
    )
    assert ledger.pending_usd("BYDDY") == 10.0


# --- "Avoid charging one withdrawal to every restored claim" -----------------
#
# Two claims restoring/leaving credit at the SAME withdrawn_usd_total snapshot
# used to age BOTH of them by the FULL withdrawal delta of any later draw,
# independently -- not a proportional or FIFO share of it, the whole thing,
# for every claim that happened to tie. A single $10 withdrawal out of a $20
# pool (two $10 credits, tied at the same snapshot) charged $10 to EACH,
# stranding one of them at $0 even though only $10 actually left the pool.


def test_two_restored_credits_at_the_same_snapshot_are_not_both_charged_one_withdrawal(tmp_path):
    """Codex review ("Avoid charging one withdrawal to every restored
    claim"): A and B each restore $10 into the pool at the SAME
    withdrawn_usd_total snapshot (pool = $20). A single later $10 withdrawal
    must drain only whichever claim's money it could actually have come
    from -- not $10 from BOTH independently, which would strand the
    innocent claim at $0 for no reason tied to its own money ever having
    been spent.

    C and D are a separate pair of claims used only to produce two clean,
    independently-sized $10 withdrawals against the shared pool (a
    redispatch is capped at the redispatching claim's OWN restored credit,
    so C's/D's $10 caps size their own order exactly, regardless of the
    combined pool total) -- the withdrawals under test are C's and D's, A
    and B never redispatch themselves.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"

    # Four claims, one fire each: $10 contribution at $10/share buys exactly
    # one share and reserves the whole $10, leaving nothing pending -- purely
    # a vehicle to get each claim its own $10 order to later preserve.
    fires = {}
    for key, at in (
        ("A", "2026-08-16T15:30:00-04:00"),
        ("B", "2026-08-16T15:31:00-04:00"),
        ("C", "2026-08-16T15:32:00-04:00"),
        ("D", "2026-08-16T15:33:00-04:00"),
    ):
        fires[key] = ledger.record_contribution(
            "BYDDY", day, 10.0, 10.0, idempotency_key=key, recorded_at=at
        )
        assert fires[key].order_notional_usd == 10.0

    # Preserve all four -- as if each fire's order failed -- back to back,
    # with no other pool activity between them, so every restored credit
    # lands at the exact same withdrawn_usd_total. This is the tie the
    # Codex finding is about.
    for key, at in (
        ("A", "2026-08-16T15:34:00-04:00"),
        ("B", "2026-08-16T15:35:00-04:00"),
        ("C", "2026-08-16T15:36:00-04:00"),
        ("D", "2026-08-16T15:37:00-04:00"),
    ):
        ledger.preserve_pending(
            "BYDDY", day, 10.0, idempotency_key=key, recorded_at=at, claim_token=fires[key].claim_token
        )
    assert ledger.pending_usd("BYDDY") == 40.0

    # C redispatches its own $10 -- a single, cleanly-sized $10 withdrawal
    # out of the shared $40 pool.
    c_redispatch = ledger.record_contribution(
        "BYDDY", day, 0.0, 10.0, idempotency_key="C", recorded_at="2026-08-16T15:38:00-04:00"
    )
    assert c_redispatch.order_notional_usd == 10.0
    assert ledger.pending_usd("BYDDY") == 30.0

    # Inspect A's and B's redispatchable credit directly (the same pattern
    # the existing counter-corruption regression test above uses) rather
    # than retrying them for real through record_contribution() -- a real
    # retry that turns out actionable also FIRES, which would spend money
    # and re-snapshot state, muddying what this test is isolating: whether
    # aging attributed C's withdrawal to the right claim, not what a live
    # caller would go on to do about it.
    from dca_whole_share_accumulator import _redispatchable_credit_usd

    state_path = tmp_path / "BYDDY" / "ledger.json"
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    withdrawn_total = state["withdrawn_usd_total"]
    pool = state["pending_usd"]
    credit_a = _redispatchable_credit_usd(entries["2026-08-16:A"], pool, withdrawn_total)
    credit_b = _redispatchable_credit_usd(entries["2026-08-16:B"], pool, withdrawn_total)
    zeroed = [credit for credit in (credit_a, credit_b) if credit == 0.0]
    survived = [credit for credit in (credit_a, credit_b) if credit == 10.0]
    assert len(zeroed) == 1 and len(survived) == 1, (
        f"exactly one of A's (${credit_a}) / B's (${credit_b}) tied $10 credits "
        "must be charged for C's $10 withdrawal -- not both (the bug) and not "
        "neither"
    )

    # D redispatches its own $10 next -- the second, independent withdrawal,
    # advancing the SAME shared withdrawal counter another $10.
    d_redispatch = ledger.record_contribution(
        "BYDDY", day, 0.0, 10.0, idempotency_key="D", recorded_at="2026-08-16T15:39:00-04:00"
    )
    assert d_redispatch.order_notional_usd == 10.0

    # Both A and B must now be fully aged out -- no double-spend: across the
    # two $10 withdrawals (C's and D's, $20 total), A and B were each
    # charged for exactly one, never more than the $20 actually drawn from
    # the pool between them.
    state = json.loads(state_path.read_bytes())
    entries = state["recorded_idempotency_keys"]
    withdrawn_total = state["withdrawn_usd_total"]
    pool = state["pending_usd"]
    assert _redispatchable_credit_usd(entries["2026-08-16:A"], pool, withdrawn_total) == 0.0
    assert _redispatchable_credit_usd(entries["2026-08-16:B"], pool, withdrawn_total) == 0.0, (
        "the second $10 withdrawal (D's) must zero out the one remaining tied "
        "claim -- no claim may survive more withdrawals than the pool "
        "actually took from it"
    )


def test_retire_credit_tie_interval_shifts_later_offsets_instead_of_colliding():
    """Codex review ("Keep later tie offsets valid when moving a
    remainder"): retiring a non-tail interval must not corrupt a claim
    registered AFTER it.

    A registers a $4 interval at offset 0 (batch width becomes 4). B then
    registers its own $4 interval at offset 4 (batch width becomes 8). If
    A's old interval is later "taken back" (e.g. to re-register a larger
    folded credit), naively decrementing the batch width by A's $4 would
    compact the freed space from the WRONG position -- A's next fresh
    allocation would land at offset 4, exactly overlapping B's still-live
    interval. The fix instead shifts B's offset down by the retired width,
    closing the gap left behind.
    """
    from dca_whole_share_accumulator import (
        _CLAIM_REMAINDER_AS_OF_FIELD,
        _CLAIM_REMAINDER_TIE_OFFSET_FIELD,
        _CREDIT_TIE_BATCH_ANCHOR_FIELD,
        _CREDIT_TIE_BATCH_WIDTH_FIELD,
        _allocate_credit_tie_offset,
        _retire_credit_tie_interval,
    )

    state = {_CREDIT_TIE_BATCH_ANCHOR_FIELD: 0.0, _CREDIT_TIE_BATCH_WIDTH_FIELD: 0.0}
    a_entry = {_CLAIM_REMAINDER_AS_OF_FIELD: 0.0}
    b_entry = {_CLAIM_REMAINDER_AS_OF_FIELD: 0.0}
    recorded_keys = {"2026-01-01:A": a_entry, "2026-01-01:B": b_entry}

    a_entry[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = _allocate_credit_tie_offset(
        state, 4.0, dedup_key="2026-01-01:A", field="claim_credit_remainder_usd"
    )
    b_entry[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = _allocate_credit_tie_offset(
        state, 4.0, dedup_key="2026-01-01:B", field="claim_credit_remainder_usd"
    )
    assert a_entry[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] == 0.0
    assert b_entry[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] == 4.0
    assert state[_CREDIT_TIE_BATCH_WIDTH_FIELD] == 8.0

    # Retire A's registration (as preserve_pending()/record_contribution()'s
    # reclaim path do before re-allocating fresh for a larger folded total).
    _retire_credit_tie_interval(
        state, recorded_keys, 0.0, a_entry[_CLAIM_REMAINDER_TIE_OFFSET_FIELD], 4.0
    )
    assert state[_CREDIT_TIE_BATCH_WIDTH_FIELD] == 4.0
    # B's own interval must be shifted down to close the gap, not left
    # sitting where a fresh allocation would collide with it.
    assert b_entry[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] == 0.0

    # A re-registers its larger folded total ($14): must land AFTER B's
    # now-shifted interval, never overlapping it.
    a_new_offset = _allocate_credit_tie_offset(
        state, 14.0, dedup_key="2026-01-01:A", field="claim_credit_remainder_usd"
    )
    assert a_new_offset == 4.0
    assert state[_CREDIT_TIE_BATCH_WIDTH_FIELD] == 18.0
    # A's interval [4, 18) and B's interval [0, 4) are disjoint.
    assert a_new_offset >= b_entry[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] + 4.0


def test_two_claim_remainders_at_the_same_snapshot_are_not_both_charged_one_withdrawal():
    """Parallel case for claim_credit_remainder_usd (Codex review: "Avoid
    charging one withdrawal to every restored claim") -- the same tie, but
    through _claim_credit_remainder_usd() rather than
    _redispatchable_credit_usd(). Exercised directly against constructed
    cache entries (the same pattern as
    test_a_non_finite_legacy_order_notional_fails_closed's sibling checks
    above), since claim_credit_remainder_usd's tie can only arise between
    two DIFFERENT sub-share contributions recorded back to back with no
    order (and therefore no withdrawal) between them -- awkward to stage
    through the public API alone, trivial to construct directly.
    """
    from dca_whole_share_accumulator import _claim_credit_remainder_usd

    # A and B each left a $10 remainder recorded at the SAME
    # claim_credit_remainder_as_of_withdrawn_usd snapshot (10 and 20 in the
    # withdrawal-counter axis respectively -- staggered by
    # _allocate_credit_interval_start() the way two zero-order fresh
    # contributions recorded back to back actually would be, NOT the shared
    # raw counter value the pre-fix code used for both).
    claim_a = {
        "claim_credit_remainder_usd": 10.0,
        "claim_credit_remainder_as_of_withdrawn_usd": 0.0,
    }
    claim_b = {
        "claim_credit_remainder_usd": 10.0,
        "claim_credit_remainder_as_of_withdrawn_usd": 10.0,
    }
    pool = 20.0

    # A single $10 withdrawal (the counter moves from 0 to 10) falls
    # entirely inside A's [0, 10) slice and none of B's [10, 20) slice.
    withdrawn_after_first = 10.0
    a_after_first = _claim_credit_remainder_usd(claim_a, pool, withdrawn_after_first)
    b_after_first = _claim_credit_remainder_usd(claim_b, pool, withdrawn_after_first)
    assert a_after_first == 0.0, "A's slice was fully consumed by the $10 withdrawal"
    assert b_after_first == 10.0, "B's slice starts at 10 -- the withdrawal never reached it"

    # A second $10 withdrawal (counter moves from 10 to 20) falls entirely
    # inside B's [10, 20) slice.
    withdrawn_after_second = 20.0
    a_after_second = _claim_credit_remainder_usd(claim_a, pool, withdrawn_after_second)
    b_after_second = _claim_credit_remainder_usd(claim_b, pool, withdrawn_after_second)
    assert a_after_second == 0.0, "already-exhausted A stays at 0, not negative or replenished"
    assert b_after_second == 0.0, "the second withdrawal now fully consumes B's own slice too"
    # No double-spend: across both withdrawals, A and B were charged $10
    # each -- $20 total -- matching exactly the $20 actually withdrawn.


def test_allocate_credit_tie_offset_is_backward_compatible_with_no_batch_fields(tmp_path):
    """Backward compatibility: a ledger state dict written before the
    credit-tie-batch fields existed carries neither of them at all.
    _allocate_credit_tie_offset() must treat that exactly like a
    freshly-started batch -- anchored at the current withdrawal counter,
    offset $0 -- rather than raising or misbehaving, so every pre-fix ledger
    keeps working the first time it is written to under the fixed code.
    """
    from dca_whole_share_accumulator import _allocate_credit_tie_offset

    legacy_state = {"withdrawn_usd_total": 15.0}
    assert "credit_tie_batch_anchor_withdrawn_usd" not in legacy_state
    assert "credit_tie_batch_width_usd" not in legacy_state
    offset = _allocate_credit_tie_offset(
        legacy_state, 5.0, dedup_key="2026-01-01:A", field="claim_credit_remainder_usd"
    )
    assert offset == 0.0, "the first claim in a freshly-started batch is offset by nothing"
    assert legacy_state["credit_tie_batch_anchor_withdrawn_usd"] == 15.0
    assert legacy_state["credit_tie_batch_width_usd"] == 5.0

    # A second allocation at the SAME counter value extends the batch --
    # offset by the first claim's width -- proving the batch state persists
    # and is actually read on the next call, matching how a real ledger
    # round-trips it through disk between record_contribution() calls.
    second_offset = _allocate_credit_tie_offset(
        legacy_state, 3.0, dedup_key="2026-01-01:B", field="claim_credit_remainder_usd"
    )
    assert second_offset == 5.0
    assert legacy_state["credit_tie_batch_width_usd"] == 8.0

    # A third allocation after the counter has moved starts a BRAND NEW
    # batch at offset $0 again, rather than extending the stale one.
    legacy_state["withdrawn_usd_total"] = 25.0
    third_offset = _allocate_credit_tie_offset(
        legacy_state, 2.0, dedup_key="2026-01-01:C", field="claim_credit_remainder_usd"
    )
    assert third_offset == 0.0
    assert legacy_state["credit_tie_batch_anchor_withdrawn_usd"] == 25.0
    assert legacy_state["credit_tie_batch_width_usd"] == 2.0


def test_tied_sub_share_contributions_track_only_their_own_credit(tmp_path):
    """Codex review ("Track only newly owned credit in the tie batch"): two
    consecutive sub-share contributions tied at the same withdrawal-counter
    instant used to record the CUMULATIVE pool total (decision.pending_after_usd)
    as both the tie-batch width contribution and the claim's own remainder,
    instead of just the claim's own marginal addition.

    A and B each leave $10 pending with nothing bought (price is well above
    either contribution) -- the pool holds $20, tied at the same
    withdrawal-counter instant since neither fired. Before the fix, B's
    remainder was recorded as $20 (the whole pool) and its tie-batch
    interval extended to $30 even though the pool never held more than $20.
    A third claim (C) then fires off the WHOLE pool -- a fresh claim draws
    on the whole pool by right -- withdrawing the entire $20 in one
    reservation. A fourth, unrelated claim (D) then contributes its own
    fresh $10. B's zero-contribution retry must see its own credit fully
    consumed by C's withdrawal ($0), not the un-fixed $10 that would let it
    submit an order funded by D's contribution instead of its own.
    """
    ledger = WholeShareAccumulationLedger(tmp_path)
    day = "2026-08-16"
    ticker = "BYDDY"

    # A and B: $10 each, priced so nothing buys (sub-share, stays pending).
    # Recorded back to back with no withdrawal between them, so both tie at
    # the same withdrawn_usd_total snapshot.
    a = ledger.record_contribution(
        ticker, day, 10.0, 100.0, idempotency_key="A", recorded_at="2026-08-16T15:30:00-04:00"
    )
    assert a.shares_to_buy == 0
    b = ledger.record_contribution(
        ticker, day, 10.0, 100.0, idempotency_key="B", recorded_at="2026-08-16T15:31:00-04:00"
    )
    assert b.shares_to_buy == 0
    assert ledger.pending_usd(ticker) == 20.0

    # C: a FRESH claim (its own key, never seen before) at $20/share draws on
    # the whole existing $20 pool by right and fires immediately, reserving
    # all of it in one withdrawal.
    c = ledger.record_contribution(
        ticker, day, 0.0, 20.0, idempotency_key="C", recorded_at="2026-08-16T15:32:00-04:00"
    )
    assert c.shares_to_buy == 1
    assert c.order_notional_usd == 20.0
    assert ledger.pending_usd(ticker) == 0.0

    # D: an unrelated fresh $10 contribution, entirely independent of A/B/C.
    d = ledger.record_contribution(
        ticker, day, 10.0, 100.0, idempotency_key="D", recorded_at="2026-08-16T15:33:00-04:00"
    )
    assert d.shares_to_buy == 0
    assert ledger.pending_usd(ticker) == 10.0

    # B's zero-contribution retry: its own $10 share of the pool was fully
    # consumed by C's $20 withdrawal (which drained both A's and B's
    # tied $10s), so it must have no actionable credit left. Priced at
    # $8/share -- low enough that the UN-FIXED $10 credit the bug would
    # report (B's cumulative pending_after_usd, aged by only C's tie-offset
    # share) buys a real share and spends $8 of D's untouched $10, while the
    # correct $0 credit buys nothing at all.
    b_retry = ledger.record_contribution(
        ticker, day, 0.0, 8.0, idempotency_key="B", recorded_at="2026-08-16T15:34:00-04:00"
    )
    assert b_retry.shares_to_buy == 0, (
        "B's own tied credit was already fully consumed by C's withdrawal -- "
        "it must not fire using D's untouched contribution"
    )
    assert ledger.pending_usd(ticker) == 10.0, (
        "B's retry must not spend D's contribution -- B's own tied credit "
        "was already fully consumed by C's withdrawal"
    )


def test_compute_virtual_contribution_usd_keeps_its_strategy_config_annotation():
    """2026-09-07 diagnostic X:OWN-04: PR #1312 replaced the TYPE_CHECKING-guarded
    ``StrategyConfig`` import with ``config: Any`` to silence an unused-import
    lint, erasing the type on this module's one public entry point. ruff never
    flags a TYPE_CHECKING import that an annotation uses, so the guard costs
    nothing at runtime (``near_close_dca`` still imports this module first, so
    the import must stay behind the guard to avoid the cycle) and keeps a wrong
    object passed as ``config`` visible to static checkers."""
    import inspect

    annotation = inspect.signature(compute_virtual_contribution_usd).parameters["config"].annotation
    assert annotation == "StrategyConfig", (
        f"config must be annotated as StrategyConfig behind a TYPE_CHECKING import, got {annotation!r}"
    )
