"""Tests for the opt-in whole-share accumulation module (see docs/whole_share_accumulation.md)."""

import json

import pytest

from dca_whole_share_accumulator import (
    WholeShareAccumulationLedger,
    WholeShareClaimPersistenceError,
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
    state = json.loads(state_path.read_text(encoding="utf-8"))
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
    state = json.loads(state_path.read_text())
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
    state = json.loads(state_path.read_text())
    entry = state["recorded_idempotency_keys"]["2026-08-16:a"]
    del entry["restored_credit_usd"]
    del entry["restored_credit_as_of_withdrawn_usd"]
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 10.0, idempotency_key="b", recorded_at="2026-08-16T15:31:00-04:00"
    )
    migrated = json.loads(state_path.read_text())["recorded_idempotency_keys"]["2026-08-16:a"]
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
    state = json.loads(state_path.read_text())
    del state["recorded_idempotency_keys"]["2026-08-16:slot-1"]["restored_credit_usd"]
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    preview = ledger.cached_fire_decision("BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 12.0)
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
    state = json.loads(state_path.read_text())
    state["recorded_idempotency_keys"]["2026-08-16:slot-1"]["restored_credit_usd"] = 0.0
    state_path.write_text(json.dumps(state, indent=2))

    preview = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:35:00-04:00", 10.0
    )
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
    entry = json.loads((tmp_path / "BYDDY" / "ledger.json").read_text())[
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

    entry = json.loads((tmp_path / "XYZ" / "ledger.json").read_text())[
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
    (pool now $2, entirely C's). A is reclaimed at $12/share: still one
    share, but the new $12 reservation draws $2 MORE than A's old $10 one.
    The pool-level guard alone would pass (pool $2 + draw -$2 = $0, not
    negative) and let A spend C's $2 -- even though A's own surviving
    remainder, per `_claim_credit_remainder_usd()`, is $0 (B already spent
    it). The reclaim must defer instead, leaving the pool and A's own claim
    untouched.
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
    assert ledger.pending_usd("XYZ") == 2.0, "C's $2 must be untouched by A's reclaim"

    entry = json.loads((tmp_path / "XYZ" / "ledger.json").read_text())[
        "recorded_idempotency_keys"
    ]["2026-08-16:claim-a"]
    assert entry["order_notional_usd"] == 10.0, "A's own claim must stay exactly as it was"
    assert entry["resolved"] is False


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
    assert preview.shares_to_buy == 0, (
        "the preview must defer exactly as record_contribution() itself now "
        "does -- not report a phantom order spending C's $2"
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

    entry = json.loads((tmp_path / "XYZ" / "ledger.json").read_text())[
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
    state = json.loads((tmp_path / "BYDDY" / "ledger.json").read_text())
    assert state["withdrawn_usd_total"] == 14.0, (
        "the $10 original reservation plus the $4 the upward reprice drew"
    )


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
    live = ledger.record_contribution(
        "BYDDY", "2026-08-16", 0.0, 12.0, idempotency_key="slot-1", recorded_at="2026-08-16T15:40:00-04:00"
    )
    assert preview.shares_to_buy == live.shares_to_buy == 1
    assert preview.order_notional_usd == live.order_notional_usd == 12.0
    assert preview.pending_after_usd == live.pending_after_usd == 13.0


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
    assert at_entitlement.shares_to_buy == 1
    # ...but not a cent of slot-2's $6.
    beyond = ledger.cached_fire_decision(
        "BYDDY", "2026-08-16", "slot-1", "2026-08-16T15:45:00-04:00", 26.0
    )
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
        account_number = "test-account"

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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # some OTHER, unrelated order exists -- not this claim's

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-QUEUED-1", "state": "queued", "cumulative_quantity": "0", "quantity": "1"}

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-FILLED-1", "state": "filled", "cumulative_quantity": "1"}

    assert unresolved is not None
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
        account_number = "test-account"

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
        ledger, _PartialFillThenCanceledBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup ran before the broker indexed the order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # The SAME order is now visible, still working -- not terminal.
            return {"id": "RH-RACE-1", "state": "confirmed", "cumulative_quantity": "0"}

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup missed the (eventually consistent) order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Filled, but the response only carries "quantity", not
            # "cumulative_quantity".
            return {"id": "RH-FILLED-ANY-STATE-1", "state": "filled", "quantity": "1"}

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-CANCELED-ANY-STATE-1", "state": "cancelled", "quantity": "1"}

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # No cumulative_quantity at all -- only the REQUESTED quantity,
            # which must not be mistaken for what actually executed.
            return {"id": "RH-REJECTED-NO-FILL-INFO-1", "state": "rejected", "quantity": "2"}

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-REJECTED-1", "state": "rejected", "cumulative_quantity": "0"}

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Terminal, but no cumulative_quantity field at all.
            return {"id": "RH-CANCELLED-1", "state": "cancelled"}

    assert unresolved is not None
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
        account_number = "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # cleanly no live match

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            raise RuntimeError("order history temporarily unavailable")

    assert unresolved is not None
    reconciled = _reconcile_unresolved_whole_share_claim(
        ledger, _TerminalLookupRaisesBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-16", unresolved,
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
