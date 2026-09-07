"""Tests for same-day sub-dollar DCA order-credit handling."""

import json

import pytest

from dca_cumulative import CumulativeCreditLedger, apply_cumulative_credit


def test_sub_fifty_cent_credit_stays_pending():
    decision = apply_cumulative_credit(0.0, 0.49)

    assert decision.order_amount_usd == 0.0
    assert decision.pending_after_usd == 0.49


def test_fifty_cents_rounds_to_a_one_dollar_order_without_negative_carry():
    decision = apply_cumulative_credit(0.0, 0.50)

    assert decision.order_amount_usd == 1.0
    assert decision.pending_after_usd == 0.0


def test_multiple_credits_accumulate_until_they_clear_the_minimum_order():
    first = apply_cumulative_credit(0.0, 0.49)
    second = apply_cumulative_credit(first.pending_after_usd, 0.49)

    assert first.order_amount_usd == 0.0
    assert second.order_amount_usd == 1.0
    assert second.pending_after_usd == 0.0


@pytest.mark.parametrize("credit", [1.01, 2.99, 4.50, 4.99])
def test_qualified_orders_keep_cent_precision_above_the_minimum(credit):
    decision = apply_cumulative_credit(0.0, credit)

    assert decision.order_amount_usd == credit
    assert decision.pending_after_usd == 0.0


def test_final_330_pm_session_keeps_exact_amount_above_minimum():
    decision = apply_cumulative_credit(3.30, 0.0, final_session=True)

    assert decision.order_amount_usd == 3.30
    assert decision.pending_after_usd == 0.0


@pytest.mark.parametrize("credit", [0.01, 0.30, 0.49])
def test_final_330_pm_session_discards_sub_fifty_cent_credit(credit):
    decision = apply_cumulative_credit(credit, 0.0, final_session=True)

    assert decision.order_amount_usd == 0.0
    assert decision.pending_after_usd == 0.0


@pytest.mark.parametrize("credit", [0.50, 0.80, 0.99])
def test_final_330_pm_session_rounds_only_sub_dollar_qualified_credit(credit):
    decision = apply_cumulative_credit(credit, 0.0, final_session=True)

    assert decision.order_amount_usd == 1.0
    assert decision.pending_after_usd == 0.0


def test_minimum_order_pending_view_excludes_unrelated_placement_blocks(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("minimum", "2026-08-03", 0.30, idempotency_key="slot-1")
    ledger.preserve_pending("spread", "2026-08-03", 0.80, idempotency_key="slot-1")

    assert ledger.pending_tickers("2026-08-03") == {"MINIMUM": 0.30, "SPREAD": 0.80}
    assert ledger.minimum_order_pending_tickers("2026-08-03") == {"MINIMUM": 0.30}


def test_discard_minimum_order_pending_clears_a_genuinely_sub_threshold_balance(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-12", 0.20, stream="morning")

    discarded = ledger.discard_minimum_order_pending("XYZ", "2026-08-12", 0.20)

    assert discarded is True
    assert ledger.minimum_order_pending_tickers("2026-08-12") == {}
    assert ledger.pending_usd("XYZ", "2026-08-12") == 0.0
    assert ledger.events("2026-08-12")[-1]["decision"] == "discarded"


def test_discard_minimum_order_pending_refuses_when_a_concurrent_update_qualifies_it(
    tmp_path,
):
    # Codex review discussion on commit 0af4768 ("Guard final discards
    # against concurrent credit updates"): the caller (the 15:30 final
    # pass's pre-scan) can decide to discard a ticker based on a balance it
    # read BEFORE acquiring this call's own lock. If a concurrent stream's
    # record() call raises the CURRENT balance to or above the discard
    # threshold in between, blindly popping whatever is present under the
    # lock would silently discard real money that should have produced an
    # order instead. This must re-check the current balance under its own
    # lock and refuse to discard when it no longer qualifies.
    # Reach a $0.50 minimum-pending balance the SAME way
    # test_record_rebase_preserves_minimum_order_provenance_after_threshold_crossing
    # does: a rebased record() call that crosses the order threshold but gets
    # overridden back to "stays fully pending" because its caller already
    # committed, from a staler baseline, to placing no order this call. That
    # is precisely the kind of concurrent update that can land between the
    # 15:30 final pass's pre-lock scan (which would have observed a smaller,
    # sub-threshold amount) and this call's own lock.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-13", 0.30, stream="s0")
    stale_baseline = ledger.pending_usd("XYZ", "2026-08-13")
    ledger.record("XYZ", "2026-08-13", 0.10, stream="s-concurrent")
    ledger.record(
        "XYZ",
        "2026-08-13",
        0.10,
        idempotency_key="slot-b",
        pending_baseline_usd=stale_baseline,
        stream="s-b",
    )
    assert ledger.minimum_order_pending_tickers("2026-08-13") == {"XYZ": 0.50}

    discarded = ledger.discard_minimum_order_pending("XYZ", "2026-08-13", 0.30)

    assert discarded is False
    # Nothing was touched -- the now-qualifying $0.50 balance survives.
    assert ledger.minimum_order_pending_tickers("2026-08-13") == {"XYZ": 0.50}
    assert ledger.pending_usd("XYZ", "2026-08-13") == 0.50


def test_discard_still_clears_a_balance_that_changed_but_stayed_sub_threshold(tmp_path):
    # Codex review ("Retry discards when the balance remains sub-threshold"):
    # the observed-amount guard refused on ANY change, so a concurrent
    # contribution that moved a credit without making it actionable
    # ($0.10 -> $0.20) returned False. Both callers read False as "the balance
    # now qualifies for an order" -- _select_final_session_credit_flush_tickers
    # has no decision to route for a sub-threshold amount, and the
    # per-candidate path re-evaluates but declines a second discard -- so the
    # credit was stranded after the final pass with nothing left to clear it.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-14", 0.10, stream="scan")
    scanned = ledger.pending_usd("XYZ", "2026-08-14")
    assert scanned == 0.10
    # A concurrent stream lands between the caller's scan and this call.
    ledger.record("XYZ", "2026-08-14", 0.10, stream="s-concurrent")
    assert ledger.minimum_order_pending_tickers("2026-08-14") == {"XYZ": 0.20}

    discarded = ledger.discard_minimum_order_pending("XYZ", "2026-08-14", scanned)

    assert discarded is True, (
        "a balance that merely changed while staying below the threshold must "
        "still be discarded, at its current value"
    )
    assert ledger.minimum_order_pending_tickers("2026-08-14") == {}
    assert ledger.pending_usd("XYZ", "2026-08-14") == 0.0
    # The FRESH amount is what left the pool, not the caller's stale reading.
    event = ledger.events("2026-08-14")[-1]
    assert event["decision"] == "discarded"
    assert event["pending_before_usd"] == 0.20
    assert event["pending_after_usd"] == 0.0


def test_discard_with_a_stale_snapshot_still_refuses_a_now_qualifying_balance(tmp_path):
    # The other side of the same change: dropping the blanket
    # changed-amount veto must NOT weaken the guard that actually matters.
    # When the concurrent update pushes the balance to the order threshold,
    # the discard is still refused so the caller can route it into evaluation.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-15", 0.30, stream="s0")
    scanned = ledger.pending_usd("XYZ", "2026-08-15")
    stale_baseline = scanned
    ledger.record("XYZ", "2026-08-15", 0.10, stream="s-concurrent")
    ledger.record(
        "XYZ",
        "2026-08-15",
        0.10,
        idempotency_key="slot-b",
        pending_baseline_usd=stale_baseline,
        stream="s-b",
    )
    assert ledger.minimum_order_pending_tickers("2026-08-15") == {"XYZ": 0.50}

    discarded = ledger.discard_minimum_order_pending("XYZ", "2026-08-15", scanned)

    assert discarded is False
    assert ledger.minimum_order_pending_tickers("2026-08-15") == {"XYZ": 0.50}
    assert ledger.pending_usd("XYZ", "2026-08-15") == 0.50


def test_settle_is_idempotent_for_the_same_broker_order(tmp_path):
    # Codex review ("Make cumulative-credit settlement idempotent by order
    # ID"): a placement can succeed at the broker and the process then crash
    # before publishing its run artifact. The retry's duplicate-recovery path
    # finds that same live order and settles it again -- and without a dedup
    # key the second call subtracts the notional a second time, so a $1
    # settlement against a $2 balance erases the remaining $1 and consumes its
    # provenance twice.
    ledger = CumulativeCreditLedger(tmp_path)
    # Sub-minimum credits accumulate rather than firing, so this builds a real
    # $0.90 pending balance.
    ledger.record("XYZ", "2026-08-20", 0.20, stream="morning")
    ledger.record("XYZ", "2026-08-20", 0.15, stream="midday")
    assert ledger.pending_usd("XYZ", "2026-08-20") == 0.35

    ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon", order_id="ORD-1")
    assert ledger.pending_usd("XYZ", "2026-08-20") == 0.15

    # The recovery re-settles the SAME order.
    ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon", order_id="ORD-1")
    assert ledger.pending_usd("XYZ", "2026-08-20") == 0.15, (
        "re-settling the same broker order must not subtract its notional twice"
    )

    # The ignored duplicate is auditable rather than silent.
    decisions = [e["decision"] for e in ledger.events("2026-08-20")]
    assert decisions.count("settled") == 1
    assert decisions.count("settle_ignored_duplicate_order") == 1


def test_settle_still_applies_for_a_different_order_id(tmp_path):
    # The dedup is per ORDER, not per ticker: a genuinely separate order must
    # still settle its own notional.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-20", 0.20, stream="morning")
    ledger.record("XYZ", "2026-08-20", 0.15, stream="midday")

    ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon", order_id="ORD-1")
    ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon", order_id="ORD-2")

    assert ledger.pending_usd("XYZ", "2026-08-20") == 0.0


def test_settle_without_an_order_id_keeps_the_previous_behavior(tmp_path):
    # Callers that cannot supply an id (older wiring, or a path with no
    # broker id in hand) keep at-least-once settlement rather than being
    # silently deduped against each other.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-20", 0.20, stream="morning")
    ledger.record("XYZ", "2026-08-20", 0.15, stream="midday")

    ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon")
    ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon")

    assert ledger.pending_usd("XYZ", "2026-08-20") == 0.0


def test_settle_fails_closed_on_a_malformed_settled_order_ids_field(tmp_path):
    # Codex review ("Validate the settled-order index before mutating
    # credits"): settled_order_ids is optional and added after the base
    # ledger shape, so a malformed but PRESENT value (e.g. a list, from a
    # hand-edited or corrupted ledger) passed the base validation untouched.
    # state.setdefault("settled_order_ids", {}) only substitutes the default
    # when the key is ABSENT, so settle()'s own dict-style write further down
    # raised on that malformed value AFTER the broker had already accepted
    # the order -- past the point where a caller could fail closed before
    # committing anything.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-20", 0.20, stream="morning")

    day_dir = tmp_path / "2026-08-20"
    state_path = day_dir / "ledger.json"
    state = json.loads(state_path.read_text())
    state["settled_order_ids"] = []
    state_path.write_text(json.dumps(state))

    with pytest.raises(ValueError):
        ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon", order_id="ORD-1")


def test_settle_fails_closed_on_a_null_settled_order_ids_field(tmp_path):
    # Codex review ("Reject a null settled-order index"): ABSENT is not the
    # same thing as PRESENT-but-null. A legacy ledger that predates this
    # field never had the key at all -- the supported upgrade path. A
    # ledger that DOES have the key but carries an explicit JSON null is a
    # field that existed and was corrupted (e.g. a partial write), not one
    # that was never written. The prior fix's `is None` check conflated the
    # two and silently re-initialized the durable order-ID index to {}
    # either way, so duplicate recovery of an already-settled broker order
    # treated it as new and subtracted its notional from the remaining
    # pending balance and provenance a second time.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-20", 0.20, stream="morning")
    ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon", order_id="ORD-1")

    day_dir = tmp_path / "2026-08-20"
    state_path = day_dir / "ledger.json"
    state = json.loads(state_path.read_text())
    assert isinstance(state["settled_order_ids"], dict) and state["settled_order_ids"]
    state["settled_order_ids"] = None
    state_path.write_text(json.dumps(state))

    with pytest.raises(ValueError):
        ledger.settle("XYZ", "2026-08-20", 0.20, stream="afternoon", order_id="ORD-1")


def test_settle_is_idempotent_for_a_preserved_credit(tmp_path):
    # The same dedup, reached through preserve_pending() rather than record():
    # the settled-order map is keyed off the broker order alone, so it protects
    # a preserved credit's balance exactly as it protects an accumulated one.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-14", 2.0, idempotency_key="slot-1")

    ledger.settle("XYZ", "2026-08-14", 1.0, order_id="order-123")
    ledger.settle("XYZ", "2026-08-14", 1.0, order_id="order-123")

    assert ledger.pending_usd("XYZ", "2026-08-14") == 1.0
    decisions = [event["decision"] for event in ledger.events("2026-08-14")]
    assert decisions.count("settled") == 1
    assert decisions.count("settle_ignored_duplicate_order") == 1


def test_settle_normalizes_a_whitespace_padded_order_id(tmp_path):
    # Ids are stripped before the dedup lookup and before persistence, so an
    # adapter that pads the same broker id cannot settle it twice.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-14", 2.0, idempotency_key="slot-1")

    ledger.settle("XYZ", "2026-08-14", 1.0, order_id="order-123")
    ledger.settle("XYZ", "2026-08-14", 1.0, order_id="  order-123  ")

    assert ledger.pending_usd("XYZ", "2026-08-14") == 1.0


def test_ledger_persists_pending_balance_and_auditable_events(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)

    first = ledger.record("abc", "2026-08-03", 0.49, stream="morning", recorded_at="10:30")
    final = ledger.record(
        "ABC", "2026-08-03", 0.0, final_session=True, stream="afternoon", recorded_at="15:30"
    )

    assert first.pending_after_usd == 0.49
    assert final.order_amount_usd == 0.0
    assert final.pending_after_usd == 0.0
    assert ledger.events("2026-08-03") == [
        {
            "ticker": "ABC",
            "stream": "morning",
            "recorded_at": "10:30",
            "final_session": False,
            "pending_before_usd": 0.0,
            "incremental_credit_usd": 0.49,
            "order_amount_usd": 0.0,
            "pending_after_usd": 0.49,
            "decision": "pending",
        },
        {
            "ticker": "ABC",
            "stream": "afternoon",
            "recorded_at": "15:30",
            "final_session": True,
            "pending_before_usd": 0.49,
            "incremental_credit_usd": 0.0,
            "order_amount_usd": 0.0,
            "pending_after_usd": 0.0,
            "decision": "discarded",
        },
    ]


def test_settle_clears_pending_and_records_the_confirmed_submitted_amount(tmp_path):
    # Codex review ("Defer ledger updates until the submitted amount is
    # known"): settle() is the ONLY write a caller should make once a
    # credit-derived order is confirmed placed -- it makes no sizing
    # decision of its own (the caller already decided, purely, before
    # placement), just durably confirms what actually happened using the
    # real submitted notional and clears the pending balance it resolved.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("xyz", "2026-08-03", 0.20, stream="morning")

    ledger.settle("xyz", "2026-08-03", 1.0, stream="afternoon", recorded_at="15:30")

    events = ledger.events("2026-08-03")
    assert events[-1] == {
        "ticker": "XYZ",
        "stream": "afternoon",
        "recorded_at": "15:30",
        "final_session": True,
        "pending_before_usd": 0.20,
        "submitted_amount_usd": 1.0,
        "order_id": None,
        "pending_after_usd": 0.0,
        "decision": "settled",
    }
    # The pending balance settle() resolved must be gone -- a fresh credit
    # afterward starts from zero, not from the pre-settlement balance.
    fresh = ledger.record("xyz", "2026-08-03", 0.10, stream="morning")
    assert fresh.pending_before_usd == 0.0


def test_settle_with_an_order_id_is_idempotent_against_a_repeat_call(tmp_path):
    # Codex review: "Make duplicate-recovery settlement idempotent". A
    # settled order's own broker order id, once passed, lets a SECOND
    # settle() call for the exact same order (e.g. morning_dca's scheduled
    # duplicate-recovery path re-settling an order whose original placement
    # already settled it before the process crashed) become a no-op instead
    # of subtracting the same dollars from the shared balance twice.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 2.0, idempotency_key="slot-1")

    # The original placement may return surrounding whitespace that duplicate
    # recovery strips. Both forms must still identify the same broker order.
    ledger.settle("xyz", "2026-08-03", 1.0, stream="morning", order_id=" ORDER-1 ")
    assert ledger.pending_usd("xyz", "2026-08-03") == 1.0

    # A repeat call with the SAME order id must change nothing further.
    ledger.settle("xyz", "2026-08-03", 1.0, stream="morning", order_id="ORDER-1")
    assert ledger.pending_usd("xyz", "2026-08-03") == 1.0
    events = ledger.events("2026-08-03")
    assert sum(1 for e in events if e["decision"] == "settled") == 1
    state = json.loads((tmp_path / "2026-08-03" / "ledger.json").read_text(encoding="utf-8"))
    assert state["settled_order_ids"] == {
        "ORDER-1": {"ticker": "XYZ", "submitted_amount_usd": 1.0}
    }

    # A DIFFERENT order id still settles normally.
    ledger.settle("xyz", "2026-08-03", 1.0, stream="morning", order_id="ORDER-2")
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.0


def test_settle_normalizes_persisted_order_ids_before_deduplication(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 2.0, idempotency_key="slot-1")
    ledger.settle("xyz", "2026-08-03", 1.0, stream="morning", order_id="ORDER-1")

    state_path = tmp_path / "2026-08-03" / "ledger.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["settled_order_ids"] = {
        " ORDER-1 ": {"ticker": "XYZ", "submitted_amount_usd": 1.0},
        " 123 ": {"ticker": "XYZ", "submitted_amount_usd": 1.0},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    ledger.preserve_pending("xyz", "2026-08-03", 1.0, idempotency_key="slot-2")

    ledger.settle("xyz", "2026-08-03", 1.0, stream="morning", order_id="ORDER-1")

    assert ledger.pending_usd("xyz", "2026-08-03") == 2.0
    migrated = json.loads(state_path.read_text(encoding="utf-8"))
    assert migrated["settled_order_ids"] == {
        "ORDER-1": {"ticker": "XYZ", "submitted_amount_usd": 1.0},
        "123": {"ticker": "XYZ", "submitted_amount_usd": 1.0},
    }


def test_settle_requires_a_ticker():
    ledger = CumulativeCreditLedger(root=None)
    with pytest.raises(ValueError):
        ledger.settle("", "2026-08-03", 1.0)


# --- Idempotency: record() / preserve_pending() / recorded_decision() -------


def test_record_and_preserve_pending_idempotency_keys_do_not_collide(tmp_path):
    # Antigravity review ("Potential TypeError on Idempotency Lookup
    # Collision"): record() and preserve_pending() used to share the same
    # f"{symbol}:{key}" dedup-key format despite caching differently-shaped
    # dicts, so a preserve_pending() call followed by a record() retry for
    # the SAME key would try to build a CumulativeCreditDecision from the
    # preserve entry's single-field dict and raise TypeError.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    # Must not raise, and must apply normally since record()'s OWN
    # idempotency key ("record:XYZ:slot-1") was never written.
    decision = ledger.record("xyz", "2026-08-03", 0.10, idempotency_key="slot-1")
    assert decision.incremental_credit_usd == 0.10


def test_record_retry_with_the_same_idempotency_key_is_a_no_op(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    first = ledger.record("xyz", "2026-08-03", 0.30, idempotency_key="slot-1")

    retry = ledger.record("xyz", "2026-08-03", 0.30, idempotency_key="slot-1")

    assert retry == first
    # The credit was applied exactly once, not twice.
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.30


def test_preserve_pending_retry_with_the_same_idempotency_key_is_a_no_op(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    first = ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    retry = ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    assert retry == first == 0.40
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.40


def test_preserve_pending_reconciles_a_fully_settled_entry_instead_of_dropping_the_new_credit(
    tmp_path,
):
    # Codex review discussion_r3838839294: "Reconcile zeroed preserve entries
    # before re-preserving". A slot preserves $0.80 (blocked placement); a
    # LATER order for the same ticker settles the whole $0.80 via settle()'s
    # chronological preserve consumption, marking this slot's own preserve
    # entry consumed_by_settlement with zero outstanding. If that same slot
    # is retried later THE SAME DAY and computes a brand new $0.30 decline
    # (unrelated to the already-paid $0.80), preserve_pending() must not
    # treat the still-present (but fully consumed) dedup key as "already
    # handled" and silently drop the new $0.30 -- it must add it to the
    # pending balance like a fresh preserve.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-1")
    ledger.settle("xyz", "2026-08-03", 0.80, final_session=False, stream="midday")

    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.0

    pending_after = ledger.preserve_pending(
        "xyz", "2026-08-03", 0.30, idempotency_key="slot-1"
    )

    assert pending_after == 0.30
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.30
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.30
    # The new cycle's own settled history starts fresh at $0.0 -- the
    # PREVIOUS cycle's $0.80 was already fully paid and is unrelated
    # provenance for this new, unconsumed $0.30 contribution.
    assert ledger.settled_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0


def test_record_with_a_netted_baseline_does_not_double_count_this_slots_own_preserve(tmp_path):
    # Codex review ("Do not re-record adjusted credits against the full
    # ledger"): a slot that earlier preserve_pending()'d $0.80 (a blocked
    # placement attempt) retries and recomputes a SMALLER $0.20 decline this
    # time (e.g. the price partially recovered). The caller nets its own
    # stale $0.80 out of the ledger's raw pending balance before deciding
    # this stays pending (existing_pending_credit_usd = pending_usd() -
    # preserved_credit_for_slot() = $0.80 - $0.80 = $0.00), and must pass
    # that SAME netted baseline into record() via pending_baseline_usd.
    # Without it, record() re-reads the ledger's raw $0.80 balance and
    # combines it with the fresh $0.20 into a bogus $1.00 order decision,
    # clearing the pending balance even though no broker order was
    # submitted this run.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-1")

    existing_pending_credit_usd = max(
        0.0,
        ledger.pending_usd("xyz", "2026-08-03")
        - ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1"),
    )
    assert existing_pending_credit_usd == 0.0

    decision = ledger.record(
        "xyz",
        "2026-08-03",
        0.20,
        idempotency_key="slot-1",
        pending_baseline_usd=existing_pending_credit_usd,
    )

    assert decision.order_amount_usd == 0.0
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.20


def test_recorded_decision_returns_none_for_a_preserve_only_slot(tmp_path):
    # A blocked placement must still be free to retry (a later gate check
    # might now pass), so recorded_decision() -- which short-circuits a
    # caller's re-evaluation entirely -- must only ever match a record()
    # call, never a preserve_pending() one.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-1") is None


def test_reconcile_to_zero_leaves_the_slot_retriable_not_settled(tmp_path):
    # Codex review: "Keep zeroed reconciliations retriable". A price that
    # temporarily recovers back to (or above) the reference makes a retry
    # recompute an outstanding remainder of $0 -- that is NOT a settlement,
    # so reconcile_preserved_credit must not mark the entry
    # consumed_by_settlement. Doing so would make recorded_decision() treat
    # the slot as permanently, terminally resolved at $0, silently losing any
    # later decline once the price falls again.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    ledger.reconcile_preserved_credit(
        "xyz", "2026-08-03", "slot-1", 0.0, expected_outstanding_credit_usd=0.40
    )

    # Zeroed out, but not treated as a resolved/settled decision -- a later
    # retry must still be free to recompute a fresh, positive remainder.
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-1") is None
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.0

    # The price falls again -- the slot must still accept a fresh, non-zero
    # reconciliation rather than being stuck at the zeroed remainder.
    ledger.reconcile_preserved_credit(
        "xyz", "2026-08-03", "slot-1", 0.50, expected_outstanding_credit_usd=0.0
    )
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.50
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.50


def test_recorded_decision_returns_the_cached_stays_pending_decision(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    original = ledger.record("xyz", "2026-08-03", 0.30, idempotency_key="slot-1")

    cached = ledger.recorded_decision("xyz", "2026-08-03", "slot-1")

    assert cached == original


def test_preserved_credit_for_slot_reports_this_slots_own_contribution(tmp_path):
    # Codex review ("Recognize preserved credits before recomputing the
    # slot"): a retry that reads pending_usd() and recomputes a fresh
    # decline for the SAME slot must first subtract out whatever this exact
    # slot already contributed via preserve_pending(), or the combined total
    # double-counts that slot's own decline -- e.g. $0.80 pending (which
    # already includes a $0.40 preserved contribution from this slot) plus a
    # freshly recomputed $0.40 becomes $1.20 instead of the correct $0.80.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("xyz", "2026-08-03", 0.40, stream="morning")  # unrelated earlier pending
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    assert ledger.pending_usd("xyz", "2026-08-03") == 0.80
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.40
    baseline = ledger.pending_usd("xyz", "2026-08-03") - ledger.preserved_credit_for_slot(
        "xyz", "2026-08-03", "slot-1"
    )
    assert baseline == 0.40


def test_preserved_credit_for_slot_is_zero_when_never_preserved(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    ledger.record("xyz", "2026-08-03", 0.30, idempotency_key="slot-1")
    # A record() (stays-pending) entry is not a preserve() entry.
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0


def test_settle_marks_preserved_credits_for_the_ticker_consumed(tmp_path):
    # Codex review ("Prevent settled credits from reviving on old-slot
    # retries"): once a LATER slot's settle() pays out a ticker's combined
    # balance, an EARLIER slot's own preserve() contribution to that balance
    # is fully resolved, not merely still pending. Both idempotency lookups
    # must recognize that -- preserved_credit_for_slot no longer reports it
    # as outstanding, and recorded_decision short-circuits the slot entirely
    # instead of letting a caller recompute a fresh decline for it.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.40

    ledger.settle("xyz", "2026-08-03", 1.0, stream="midday")

    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    decision = ledger.recorded_decision("xyz", "2026-08-03", "slot-1")
    assert decision is not None
    assert decision.order_amount_usd == 0.0
    assert decision.pending_after_usd == 0.0


def test_settle_retains_the_balance_a_clamped_order_did_not_cover(tmp_path):
    # Codex review ("Retain credits not covered by a clamped order"): three
    # blocked $0.80 preserved credits total $2.40 pending, but a downstream
    # kill-switch/per-order cap can clamp the actual submitted notional to
    # $1 -- settle() must retain the uncovered $1.40 as still pending rather
    # than destroying it. Codex review ("Mark settled preserve keys on
    # partial settlement"): the FIRST $0.80 contribution IS fully covered by
    # the $1 submitted and must be marked consumed (else a delayed retry of
    # that exact slot could recompute and resubmit for credit already
    # bought). The SECOND is only partially covered ($0.20 of its $0.80): it
    # stays unconsumed (still retriable) but its own recorded contribution is
    # reduced to the genuinely outstanding $0.60, not left at the stale $0.80
    # (Codex review: "Track partially consumed preserve entries" -- else a
    # delayed retry of that slot could recompute and resubmit the $0.20
    # portion that this settlement already paid). The THIRD, never reached,
    # is untouched.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-1")
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-2")
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-3")
    assert ledger.pending_usd("xyz", "2026-08-03") == 2.40

    ledger.settle("xyz", "2026-08-03", 1.0, stream="afternoon")

    assert ledger.pending_usd("xyz", "2026-08-03") == 1.40
    decision = ledger.recorded_decision("xyz", "2026-08-03", "slot-1")
    assert decision is not None and decision.order_amount_usd == 0.0
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-2") is None
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-2") == 0.60
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-3") is None
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-3") == 0.80


def test_settle_still_fully_clears_a_balance_the_submission_covers(tmp_path):
    # The common case (a confirmed order for at least the pending balance)
    # must behave exactly as before: fully cleared and consumed.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    ledger.settle("xyz", "2026-08-03", 1.0, stream="afternoon")

    assert ledger.pending_usd("xyz", "2026-08-03") == 0.0
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    decision = ledger.recorded_decision("xyz", "2026-08-03", "slot-1")
    assert decision is not None
    assert decision.order_amount_usd == 0.0


def test_settle_covers_exactly_one_of_two_equal_preserved_credits(tmp_path):
    # The exact scenario from the Codex review ("Mark settled preserve keys
    # on partial settlement"): two blocked $0.80 credits followed by a
    # later $1 clamped final order. $0.60 genuinely remains outstanding, but
    # the first slot's own $0.80 IS fully paid and must not be revivable.
    # The second slot's own contribution is only partially paid ($0.20 of
    # its $0.80): preserved_credit_for_slot must report the genuinely
    # outstanding $0.60, not the stale original $0.80 (Codex review: "Track
    # partially consumed preserve entries").
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-1")
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-2")

    ledger.settle("xyz", "2026-08-03", 1.0, stream="afternoon")

    assert ledger.pending_usd("xyz", "2026-08-03") == 0.60
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-1") is not None
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-2") == 0.60
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-2") is None


def test_settle_consumes_the_earliest_preserved_credit_first_despite_key_sort_order(tmp_path):
    # Codex review ("Preserve chronological credit settlement order"): the
    # ledger is durably written with sort_keys=True and re-read on every
    # call, so after any write recorded_idempotency_keys' iteration order is
    # lexicographic by idempotency key, not the order entries were actually
    # preserved. A 10:30 morning preserve followed by a 12:30 midday preserve
    # sorts "midday_run..." before "morning_run..." purely by text, even
    # though morning was preserved FIRST -- a partial settlement must still
    # consume morning's contribution first, not midday's.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="morning_run_1030")
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="midday_run_1230")

    ledger.settle("xyz", "2026-08-03", 1.0, stream="afternoon")

    assert ledger.pending_usd("xyz", "2026-08-03") == 0.60
    # morning's slot was preserved FIRST and must be the one fully consumed.
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "morning_run_1030") == 0.0
    assert ledger.recorded_decision("xyz", "2026-08-03", "morning_run_1030") is not None
    # midday's slot was preserved SECOND and must remain retriable; only
    # $0.20 of its $0.80 was paid, so $0.60 is genuinely still outstanding.
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "midday_run_1230") == 0.60
    assert ledger.recorded_decision("xyz", "2026-08-03", "midday_run_1230") is None


def test_settle_consumes_nothing_when_the_first_entry_alone_exceeds_submission(tmp_path):
    # A submission smaller than even the first preserved entry must not mark
    # anything consumed -- there is nothing it fully covers. But $0.50 of the
    # $0.80 WAS paid, so the slot's own recorded contribution must shrink to
    # the genuinely outstanding $0.30, not stay at the stale $0.80 (Codex
    # review: "Track partially consumed preserve entries").
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-1")

    ledger.settle("xyz", "2026-08-03", 0.50, stream="afternoon")

    assert ledger.pending_usd("xyz", "2026-08-03") == 0.30
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-1") is None
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.30


def test_settle_does_not_affect_a_different_tickers_preserved_credit(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("abc", "2026-08-03", 0.40, idempotency_key="slot-1")

    ledger.settle("xyz", "2026-08-03", 1.0, stream="midday")

    assert ledger.preserved_credit_for_slot("abc", "2026-08-03", "slot-1") == 0.40
    assert ledger.recorded_decision("abc", "2026-08-03", "slot-1") is None


def test_unconsumed_preserved_credit_still_allows_a_fresh_retry(tmp_path):
    # The opposite of the consumed case: a blocked placement with no
    # subsequent settlement must still be free to retry from scratch (a
    # later gate check might now pass), so recorded_decision must NOT
    # short-circuit it.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-1") is None
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.40


def test_settle_does_not_let_a_partially_paid_entry_be_resubmitted_in_full(tmp_path):
    # Codex review ("Track partially consumed preserve entries"): preserved
    # credits of $0.60 and $1.40 followed by a $1.00 settlement leave $1.00
    # genuinely pending ($0.60 fully paid off the first entry, $0.40 paid
    # off the second). Before the fix, the second entry's own recorded
    # contribution stayed at the stale full $1.40 -- a retry of that exact
    # slot would recompute and resubmit $1.40, restoring the $0.40 that was
    # already bought.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.60, idempotency_key="slot-1")
    ledger.preserve_pending("xyz", "2026-08-03", 1.40, idempotency_key="slot-2")
    assert ledger.pending_usd("xyz", "2026-08-03") == 2.00

    ledger.settle("xyz", "2026-08-03", 1.0, stream="afternoon")

    assert ledger.pending_usd("xyz", "2026-08-03") == 1.00
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1") == 0.0
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-1") is not None
    # slot-2 is still retriable (not fully paid) but must report only the
    # genuinely outstanding $1.00, not the stale original $1.40.
    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-2") is None
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-2") == 1.00


def test_record_rebases_a_netted_baseline_against_a_concurrent_ledger_update(tmp_path):
    # Codex review ("Rebase retry adjustments inside the ledger lock"): a
    # caller normally computes pending_baseline_usd from pending_usd() minus
    # preserved_credit_for_slot() BEFORE calling record(), which acquires
    # its own separate lock. A concurrent stream can preserve/record/settle
    # the same ticker in between. record() must rebase this slot's own
    # preserved contribution against the ledger's CURRENT balance -- read
    # atomically under its own lock -- rather than blindly trusting the
    # caller's now-stale snapshot, which would silently discard the
    # concurrent stream's update despite the cross-process lock.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.80, idempotency_key="slot-1")

    # Caller's pre-lock snapshot: this slot's own $0.80 nets out to $0.00.
    stale_baseline = ledger.pending_usd(
        "xyz", "2026-08-03"
    ) - ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1")
    assert stale_baseline == 0.0

    # A DIFFERENT, concurrent slot preserves its own unrelated $0.10 blocked
    # credit for the same ticker after the caller's snapshot but before
    # slot-1's record() call.
    ledger.preserve_pending("xyz", "2026-08-03", 0.10, idempotency_key="slot-2")
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.90

    # slot-1 now recomputes a fresh, smaller $0.05 decline and calls
    # record() with its now-stale pre-lock baseline.
    decision = ledger.record(
        "xyz",
        "2026-08-03",
        0.05,
        idempotency_key="slot-1",
        pending_baseline_usd=stale_baseline,
    )

    # Correct atomic rebase: current balance ($0.90) minus slot-1's own
    # unconsumed preserved contribution ($0.80) = $0.10, not the stale
    # $0.00 the caller read before slot-2's concurrent preserve.
    assert decision.pending_before_usd == 0.10
    assert decision.order_amount_usd == 0.0
    assert decision.pending_after_usd == 0.15
    # slot-2's concurrent $0.10 contribution must survive in the final
    # balance ($0.10 + the fresh $0.05 = $0.15), not be silently discarded
    # by overwriting the ledger with a decision derived from the stale
    # $0.00 baseline (which would have left only $0.05 pending).
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.15
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-2") == 0.10


def test_record_rebase_never_crosses_the_order_threshold_on_its_own(tmp_path):
    # Codex review ("Prevent rebasing from creating an unsubmitted order"):
    # morning_dca.py's only production call site for record()'s rebase
    # branch is reached ONLY after the caller already decided, from its own
    # (staler) baseline, to place no order this run -- it never inspects
    # the returned decision, relying on the invariant that this call always
    # yields order_amount_usd == 0. If the atomic rebase sees a fresher,
    # larger balance (e.g. a concurrent stream's contribution) that alone
    # crosses the regular-session threshold, honoring that here would clear
    # the pending balance as though an order were submitted -- but no order
    # actually gets placed (the caller ignores the return value), silently
    # losing the credit. The rebase must instead keep the full combined
    # total pending, exactly like preserve_pending, and defer the
    # order-or-not decision to a later evaluation.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.30, idempotency_key="slot-1")

    stale_baseline = ledger.pending_usd(
        "xyz", "2026-08-03"
    ) - ledger.preserved_credit_for_slot("xyz", "2026-08-03", "slot-1")
    assert stale_baseline == 0.0

    # A concurrent stream preserves its own unrelated $0.30 for the same
    # ticker after the caller's snapshot but before slot-1's record() call.
    ledger.preserve_pending("xyz", "2026-08-03", 0.30, idempotency_key="slot-2")
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.60

    # slot-1 retries with a fresh $0.30 decline. Rebased atomically, this
    # would total $0.60 ($0.30 netted baseline + $0.30 fresh) -- enough to
    # cross the $0.50 threshold on its own -- but nobody will place an
    # order for it.
    decision = ledger.record(
        "xyz",
        "2026-08-03",
        0.30,
        idempotency_key="slot-1",
        pending_baseline_usd=stale_baseline,
    )

    assert decision.order_amount_usd == 0.0
    assert decision.pending_after_usd == 0.60
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.60


def test_record_rebase_preserves_minimum_order_provenance_after_threshold_crossing(
    tmp_path,
):
    # Codex review discussion_r3793071617 ("Preserve minimum provenance after
    # a rebased threshold crossing"): the broker-minimum-provenance pool
    # (minimum_order_pending_tickers, the ONLY balance the 15:30 aggregate
    # top-up rule may flush) must be rebased exactly like the main pending
    # balance above it. Before the fix, the un-rebased qualifying_decision was
    # computed against the SAME threshold-crossing baseline as the original
    # (soon-to-be-overridden) decision, so it popped the ticker from
    # minimum_pending as though an order had consumed it -- even though the
    # override just decided no order will actually happen this call.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-10", 0.30, stream="s0")
    assert ledger.minimum_order_pending_tickers("2026-08-10") == {"XYZ": 0.30}

    # slot-b's pre-lock snapshot of the balance, captured before the
    # concurrent $0.10 update just below.
    stale_baseline = ledger.pending_usd("XYZ", "2026-08-10")
    assert stale_baseline == 0.30

    # A concurrent, unrelated $0.10 broker-minimum-blocked credit lands for
    # the same ticker after slot-b's snapshot but before slot-b's record()
    # call below.
    ledger.record("XYZ", "2026-08-10", 0.10, stream="s-concurrent")
    assert ledger.pending_usd("XYZ", "2026-08-10") == 0.40
    assert ledger.minimum_order_pending_tickers("2026-08-10") == {"XYZ": 0.40}

    # slot-b retries with its own fresh $0.10 decline, using the now-stale
    # pre-lock baseline. Rebased atomically inside the lock, the combined
    # total ($0.40 + $0.10 = $0.50) crosses the round-up-to-$1 threshold on
    # its own -- but slot-b's caller (morning_dca.py's stays-pending write)
    # never inspects the return value, so record() must keep the full total
    # pending instead of silently creating an order nobody will submit.
    decision = ledger.record(
        "XYZ",
        "2026-08-10",
        0.10,
        idempotency_key="slot-b",
        pending_baseline_usd=stale_baseline,
        stream="s-b",
    )

    assert decision.order_amount_usd == 0.0
    assert decision.pending_after_usd == 0.50
    assert ledger.pending_usd("XYZ", "2026-08-10") == 0.50
    # The $0.50 that stayed pending must still be visible through the
    # broker-minimum-provenance pool -- not silently dropped as though an
    # order had already consumed it -- since it is the ONLY balance the
    # 15:30 final pass's aggregate top-up rule may flush.
    assert ledger.minimum_order_pending_tickers("2026-08-10") == {"XYZ": 0.50}


def test_record_with_idempotency_key_but_nothing_to_rebase_still_places_an_order(
    tmp_path,
):
    # Codex review discussion_r3904287096 ("Rebase only when a matching
    # preserved credit exists"): a bare idempotency_key alone used to be
    # enough to set `rebased = True`, so a plain record() call that has
    # neither a matching preserve_pending() entry NOR an explicit
    # pending_baseline_usd had every threshold-crossing decision silently
    # overridden into pending credit, even though there was nothing stale
    # to rebase against. An existing $0.40 pending plus this $0.20 credit
    # must still return the policy's normal $1.00 rounded-up order, exactly
    # as it would without an idempotency_key at all.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-10", 0.40, stream="s0")
    assert ledger.pending_usd("XYZ", "2026-08-10") == 0.40

    decision = ledger.record(
        "XYZ",
        "2026-08-10",
        0.20,
        idempotency_key="slot",
        stream="s1",
    )

    assert decision.order_amount_usd == 1.0
    assert decision.pending_after_usd == 0.0
    assert ledger.pending_usd("XYZ", "2026-08-10") == 0.0


def test_settle_partitions_the_submitted_amount_across_provenance_pools(tmp_path):
    # Codex review discussion on commit 0af4768 ("Allocate settlement once
    # across provenance pools"): minimum_pending and a preserve: entry are
    # DISJOINT provenance views of the SAME pending_usd_by_ticker balance.
    # A $0.40 minimum-order credit plus an unrelated $0.80 preserved credit
    # ($1.20 total pending) settled by a single clamped $1.00 order must
    # partition that $1.00 between the two views, not let each independently
    # subtract the full $1.00 from itself -- otherwise $1.20 of credit gets
    # accounted for against only $1.00 actually submitted, and the genuinely
    # outstanding $0.20 remainder becomes invisible to both provenance views
    # even though it is still sitting in the main pending balance.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-11", 0.40, stream="morning")
    ledger.preserve_pending("XYZ", "2026-08-11", 0.80, idempotency_key="slot-1")
    assert ledger.pending_usd("XYZ", "2026-08-11") == 1.20
    assert ledger.minimum_order_pending_tickers("2026-08-11") == {"XYZ": 0.40}

    ledger.settle("XYZ", "2026-08-11", 1.0, stream="afternoon")

    # The main balance correctly retains the genuine $0.20 remainder.
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.20
    # The minimum pool is fully paid ($0.40 < the $1.00 budget) -- correct.
    assert ledger.minimum_order_pending_tickers("2026-08-11") == {}
    # Only $0.60 of the $1.00 budget remains after the minimum pool's $0.40,
    # so the $0.80 preserved entry is only PARTIALLY consumed: $0.20 of it
    # must remain genuinely outstanding and retriable, not fully consumed.
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.20
    assert ledger.settled_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.60
    assert ledger.recorded_decision("XYZ", "2026-08-11", "slot-1") is None


def test_partial_settlement_does_not_rebuild_paid_preserved_credit(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-11", 0.40, stream="morning")
    ledger.preserve_pending("XYZ", "2026-08-11", 0.80, idempotency_key="slot-1")

    ledger.settle("XYZ", "2026-08-11", 1.0, stream="afternoon")

    outstanding = ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1")
    paid = ledger.settled_credit_for_slot("XYZ", "2026-08-11", "slot-1")
    baseline = ledger.pending_usd("XYZ", "2026-08-11") - outstanding
    retry = apply_cumulative_credit(baseline, max(0.0, 0.80 - paid), final_session=False)
    assert retry.order_amount_usd == 0.0
    assert retry.pending_after_usd == 0.20


def test_provenance_snapshot_returns_partially_settled_values_atomically(tmp_path, monkeypatch):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-11", 0.40, stream="morning")
    ledger.preserve_pending("XYZ", "2026-08-11", 0.80, idempotency_key="slot-1")
    ledger.settle("XYZ", "2026-08-11", 1.0, stream="afternoon")
    reads = 0
    original_read_state = ledger._read_state

    def counting_read_state(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original_read_state(*args, **kwargs)

    monkeypatch.setattr(ledger, "_read_state", counting_read_state)

    snapshot = ledger.provenance_snapshot("XYZ", "2026-08-11", "slot-1")

    assert reads == 1
    assert snapshot.pending_usd == 0.20
    assert snapshot.preserved_credit_usd == 0.20
    assert snapshot.settled_credit_usd == 0.60


def test_reconcile_preserved_credit_replaces_its_pending_contribution(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("XYZ", "2026-08-11", 0.40, stream="morning")
    ledger.preserve_pending("XYZ", "2026-08-11", 0.20, idempotency_key="slot-1")

    ledger.reconcile_preserved_credit(
        "XYZ", "2026-08-11", "slot-1", 0.10, expected_outstanding_credit_usd=0.20
    )
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.50
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.10
    assert ledger.minimum_order_pending_tickers("2026-08-11") == {"XYZ": 0.40}

    ledger.reconcile_preserved_credit(
        "XYZ", "2026-08-11", "slot-1", 0.30, expected_outstanding_credit_usd=0.10
    )
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.70
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.30
    assert ledger.minimum_order_pending_tickers("2026-08-11") == {"XYZ": 0.40}


def test_reconcile_applies_a_revived_decline_after_full_settlement(tmp_path):
    # Codex review, PR #656 follow-up: "Retry reconciliation when the
    # preserve was consumed". If another stream fully settles this preserve
    # entry AFTER the caller's initial recorded_decision()/preserved_
    # credit_for_slot() reads but BEFORE this call, reconcile_preserved_
    # credit must not just silently return the current balance -- a fresh,
    # genuinely NEW decline (outstanding_credit_usd) must still be applied,
    # or it is lost entirely.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-11", 0.50, idempotency_key="slot-1")
    # A later stream fully settles this exact $0.50 preserve.
    ledger.settle("XYZ", "2026-08-11", 0.50, final_session=False, stream="midday")
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.0
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.0

    # This retry's own price fell further, recomputing a fresh $0.30 decline
    # for the SAME slot -- unrelated to the $0.50 that was just paid.
    pending_after = ledger.reconcile_preserved_credit(
        "XYZ",
        "2026-08-11",
        "slot-1",
        0.30,
        expected_settled_credit_usd=0.50,
        expected_outstanding_credit_usd=0.0,
    )

    assert pending_after == 0.30
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.30
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.30
    # The new cycle's own settled history starts fresh -- the previous
    # $0.50 was already fully paid and is unrelated provenance for this
    # new, unconsumed $0.30 contribution.
    assert ledger.settled_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.0


def test_reconcile_restamps_a_revived_preserve_with_a_fresh_sequence(tmp_path):
    # Codex review, PR #656 follow-up: "Restamp revived preserves with a new
    # sequence". settle() consumes preserve entries in `sequence` order.
    # slotA is preserved and fully consumed; slotB is then preserved
    # (strictly later, so it must be OLDER in settlement priority than
    # anything revived after it) and stays outstanding; slotA is then
    # revived with a genuinely new decline. A later PARTIAL settlement must
    # still prioritize slotB's older outstanding balance over slotA's
    # revived one -- retaining slotA's original (already-paid) sequence
    # would wrongly let it jump the queue ahead of slotB.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-11", 0.60, idempotency_key="slotA")
    ledger.settle("XYZ", "2026-08-11", 0.60, final_session=False, stream="morning")
    ledger.preserve_pending("XYZ", "2026-08-11", 0.70, idempotency_key="slotB")

    ledger.reconcile_preserved_credit(
        "XYZ",
        "2026-08-11",
        "slotA",
        0.40,
        expected_settled_credit_usd=0.60,
        expected_outstanding_credit_usd=0.0,
    )

    # A partial $0.40 settlement should be attributed to slotB (the older
    # outstanding entry), leaving slotA's freshly revived credit untouched --
    # not fully consume slotA just because it happens to sort first.
    ledger.settle("XYZ", "2026-08-11", 0.40, final_session=False, stream="midday")

    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slotA") == 0.40
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slotB") == 0.30


def test_reconcile_rejects_a_concurrent_settlement(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-11", 0.80, idempotency_key="slot-1")
    ledger.settle("XYZ", "2026-08-11", 0.20, final_session=False)

    with pytest.raises(RuntimeError, match="changed during reconciliation"):
        ledger.reconcile_preserved_credit(
            "XYZ",
            "2026-08-11",
            "slot-1",
            0.80,
            expected_settled_credit_usd=0.0,
            expected_outstanding_credit_usd=0.60,
        )

    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.60
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.60


def test_reconcile_rejects_a_concurrent_preserve_reconciliation(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-11", 0.20, idempotency_key="slot-1")

    ledger.reconcile_preserved_credit(
        "XYZ", "2026-08-11", "slot-1", 0.0, expected_outstanding_credit_usd=0.20
    )

    with pytest.raises(RuntimeError, match="changed during reconciliation"):
        ledger.reconcile_preserved_credit(
            "XYZ", "2026-08-11", "slot-1", 0.60, expected_outstanding_credit_usd=0.20
        )
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.0


def test_reconcile_rejects_a_concurrent_unrelated_provenance_change(tmp_path):
    # A retry's order-vs-stays-pending decision is made from a pending_usd()
    # snapshot read BEFORE reconcile_preserved_credit's lock is acquired. If a
    # concurrent stream settles or records an unrelated contribution for the
    # SAME ticker in between, neither the settled-credit nor outstanding-
    # credit CAS checks above notice -- both compare only THIS preserve
    # entry's own fields, which never changed. expected_pending_usd closes
    # that gap by additionally comparing the ticker's total shared balance.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-11", 0.20, idempotency_key="slot-1")
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.20

    # An unrelated (sub-threshold, so it stays pending rather than rounding
    # up to an order and clearing the balance) minimum-order credit for the
    # same ticker lands after the caller's pending_usd() snapshot but before
    # its reconciliation call.
    ledger.record("XYZ", "2026-08-11", 0.05, stream="midday")
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.25

    with pytest.raises(RuntimeError, match="changed during reconciliation"):
        ledger.reconcile_preserved_credit(
            "XYZ",
            "2026-08-11",
            "slot-1",
            0.10,
            expected_outstanding_credit_usd=0.20,
            expected_pending_usd=0.20,
        )

    # Rejected: neither this preserve entry nor the unrelated minimum credit
    # changed as a result of the failed reconciliation attempt.
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.20
    assert ledger.minimum_order_pending_tickers("2026-08-11") == {"XYZ": 0.05}
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.25

    # A fresh attempt using the CURRENT total succeeds normally.
    ledger.reconcile_preserved_credit(
        "XYZ",
        "2026-08-11",
        "slot-1",
        0.10,
        expected_outstanding_credit_usd=0.20,
        expected_pending_usd=0.25,
    )
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", "slot-1") == 0.10
    assert ledger.pending_usd("XYZ", "2026-08-11") == 0.15


def test_legacy_preserve_without_settlement_accounting_fails_closed(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-11", 0.20, idempotency_key="slot-1")
    path = tmp_path / "2026-08-11" / "ledger.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["recorded_idempotency_keys"]["preserve:XYZ:slot-1"]["settled_credit_usd"]
    path.write_text(json.dumps(state), encoding="utf-8")

    decision = ledger.recorded_decision("XYZ", "2026-08-11", "slot-1")
    assert decision is not None
    assert decision.order_amount_usd == 0.0
    with pytest.raises(ValueError, match="legacy preserved credit"):
        ledger.reconcile_preserved_credit(
            "XYZ", "2026-08-11", "slot-1", 0.80, expected_outstanding_credit_usd=0.20
        )


def test_partial_settlement_keeps_legacy_preserve_fail_closed(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("XYZ", "2026-08-11", 0.40, idempotency_key="slot-1")
    path = tmp_path / "2026-08-11" / "ledger.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state["recorded_idempotency_keys"]["preserve:XYZ:slot-1"]
    del entry["settled_credit_usd"]
    path.write_text(json.dumps(state), encoding="utf-8")

    ledger.settle("XYZ", "2026-08-11", 0.10)

    updated = json.loads(path.read_text(encoding="utf-8"))
    entry = updated["recorded_idempotency_keys"]["preserve:XYZ:slot-1"]
    assert entry["incremental_credit_usd"] == 0.30
    assert "settled_credit_usd" not in entry
    assert ledger.recorded_decision("XYZ", "2026-08-11", "slot-1") is not None


def test_cumulative_credit_edge_cases(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)

    # 1. Empty ticker validations and None idempotency keys
    with pytest.raises(ValueError, match="ticker is required"):
        ledger.record("", "2026-08-11", 0.50)
    with pytest.raises(ValueError, match="ticker is required"):
        ledger.preserve_pending("  ", "2026-08-11", 0.50)
    with pytest.raises(ValueError, match="ticker is required"):
        ledger.reconcile_preserved_credit("", "2026-08-11", "slot-1", 0.50, expected_outstanding_credit_usd=0.0)
    assert ledger.recorded_decision("", "2026-08-11", "slot-1") is None
    assert ledger.recorded_decision("XYZ", "2026-08-11", None) is None
    assert ledger.preserved_credit_for_slot("", "2026-08-11", "slot-1") == 0.0
    assert ledger.preserved_credit_for_slot("XYZ", "2026-08-11", None) == 0.0
    assert ledger.has_preserved_entry_for_slot("", "2026-08-11", "slot-1") is False
    assert ledger.has_preserved_entry_for_slot("XYZ", "2026-08-11", None) is False
    assert ledger.settled_credit_for_slot("", "2026-08-11", "slot-1") == 0.0
    assert ledger.settled_credit_for_slot("XYZ", "2026-08-11", None) == 0.0
    assert ledger.settled_credit_for_slot("XYZ", "2026-08-11", "slot-unknown") == 0.0
    assert ledger.pending_usd("", "2026-08-11") == 0.0

    # 2. record with pending_baseline_usd and no idempotency_key
    decision = ledger.record("XYZ", "2026-08-11", 0.10, pending_baseline_usd=0.40)
    assert decision.order_amount_usd == 1.0

    # 3. preserve_pending with 0 additional credit when pending is 0
    res = ledger.preserve_pending("ABC", "2026-08-11", 0.0)
    assert res == 0.0

    # 4. reconcile_preserved_credit when cached is None
    res = ledger.reconcile_preserved_credit(
        "ABC", "2026-08-11", "nonexistent-key", 0.20, expected_outstanding_credit_usd=0.0
    )
    assert res == 0.0

    # 5. reconcile_preserved_credit when already consumed and outstanding_credit <= 0
    ledger.preserve_pending("XYZ", "2026-08-11", 0.20, idempotency_key="slot-consumed")
    assert ledger.has_preserved_entry_for_slot("XYZ", "2026-08-11", "slot-consumed") is True
    ledger.settle("XYZ", "2026-08-11", 1.0)
    res = ledger.reconcile_preserved_credit(
        "XYZ",
        "2026-08-11",
        "slot-consumed",
        0.0,
        expected_settled_credit_usd=0.20,
        expected_outstanding_credit_usd=0.0,
    )
    assert res == ledger.pending_usd("XYZ", "2026-08-11")

    # 6. discard_minimum_order_pending when amount <= 0
    assert ledger.discard_minimum_order_pending("NONEXISTENT", "2026-08-11") is True

    # 7. discard_minimum_order_pending when pending_after > 0
    ledger.preserve_pending("DUAL", "2026-08-11", 0.20)
    ledger.record("DUAL", "2026-08-11", 0.10)
    assert ledger.discard_minimum_order_pending("DUAL", "2026-08-11") is True
    assert ledger.pending_usd("DUAL", "2026-08-11") == 0.20

    # 8. Settle loop break when available <= 0, and legacy entry without settled_credit_usd
    ledger.preserve_pending("MULTI", "2026-08-11", 0.20, idempotency_key="m1")
    ledger.preserve_pending("MULTI", "2026-08-11", 0.20, idempotency_key="m2")
    # Remove settled_credit_usd from m1 to test branch where key is absent
    m_state_path = tmp_path / "2026-08-11" / "ledger.json"
    m_state = json.loads(m_state_path.read_text(encoding="utf-8"))
    m_state["recorded_idempotency_keys"]["preserve:MULTI:m1"].pop("settled_credit_usd", None)
    m_state_path.write_text(json.dumps(m_state), encoding="utf-8")
    ledger.settle("MULTI", "2026-08-11", 0.20)

    # 9. _read_state corrupt / invalid json / invalid structure
    day_dir = tmp_path / "2026-08-99"
    day_dir.mkdir(parents=True, exist_ok=True)
    state_file = day_dir / "ledger.json"
    state_file.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not read cumulative credit ledger"):
        CumulativeCreditLedger(tmp_path).pending_usd("XYZ", "2026-08-99")

    state_file.write_text(json.dumps({"version": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid cumulative credit ledger"):
        CumulativeCreditLedger(tmp_path).pending_usd("XYZ", "2026-08-99")

    # 10. _money numeric & finite checks
    from dca_cumulative import _money

    with pytest.raises(ValueError, match="credit amounts must be numeric"):
        _money("invalid_numeric")
    with pytest.raises(ValueError, match="credit amounts must be finite"):
        _money(float("nan"))
    with pytest.raises(ValueError, match="credit amounts must be finite"):
        _money(float("inf"))

    # 11. apply_cumulative_credit total == 0
    zero_dec = apply_cumulative_credit(0.0, 0.0)
    assert zero_dec.order_amount_usd == 0.0
    assert zero_dec.pending_after_usd == 0.0

