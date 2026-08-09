"""Tests for same-day sub-dollar DCA order-credit handling."""

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


def test_final_330_pm_session_rounds_every_positive_pending_balance_up():
    decision = apply_cumulative_credit(3.30, 0.0, final_session=True)

    assert decision.order_amount_usd == 4.0
    assert decision.pending_after_usd == 0.0


def test_final_330_pm_session_flushes_even_a_tiny_positive_credit():
    decision = apply_cumulative_credit(0.01, 0.0, final_session=True)

    assert decision.order_amount_usd == 1.0
    assert decision.pending_after_usd == 0.0


def test_ledger_persists_pending_balance_and_auditable_events(tmp_path):
    ledger = CumulativeCreditLedger(tmp_path)

    first = ledger.record("abc", "2026-08-03", 0.49, stream="morning", recorded_at="10:30")
    final = ledger.record(
        "ABC", "2026-08-03", 0.0, final_session=True, stream="afternoon", recorded_at="15:30"
    )

    assert first.pending_after_usd == 0.49
    assert final.order_amount_usd == 1.0
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
            "order_amount_usd": 1.0,
            "pending_after_usd": 0.0,
            "decision": "order",
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
        "pending_after_usd": 0.0,
        "decision": "settled",
    }
    # The pending balance settle() resolved must be gone -- a fresh credit
    # afterward starts from zero, not from the pre-settlement balance.
    fresh = ledger.record("xyz", "2026-08-03", 0.10, stream="morning")
    assert fresh.pending_before_usd == 0.0


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


def test_record_with_idempotency_rebases_from_locked_state_not_caller_baseline(tmp_path):
    # With an idempotency key, record() must recompute from the CURRENT locked
    # ledger state (minus this slot's own unconsumed preserve entry, if any),
    # not trust a caller-supplied pre-lock baseline that may now be stale.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.record("xyz", "2026-08-03", 0.40, stream="morning")

    decision = ledger.record(
        "xyz",
        "2026-08-03",
        0.10,
        idempotency_key="slot-1",
        pending_baseline_usd=0.0,
    )

    assert decision.pending_before_usd == 0.40
    assert decision.order_amount_usd == 1.0
    assert ledger.pending_usd("xyz", "2026-08-03") == 0.0


def test_recorded_decision_returns_none_for_a_preserve_only_slot(tmp_path):
    # A blocked placement must still be free to retry (a later gate check
    # might now pass), so recorded_decision() -- which short-circuits a
    # caller's re-evaluation entirely -- must only ever match a record()
    # call, never a preserve_pending() one.
    ledger = CumulativeCreditLedger(tmp_path)
    ledger.preserve_pending("xyz", "2026-08-03", 0.40, idempotency_key="slot-1")

    assert ledger.recorded_decision("xyz", "2026-08-03", "slot-1") is None


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
    # bought); the second entry is partially covered and must be reduced to
    # its real $0.60 remainder, while the third stays at $0.80.
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
    # midday's slot was preserved SECOND and must remain retriable with only
    # its genuine uncovered remainder.
    assert ledger.preserved_credit_for_slot("xyz", "2026-08-03", "midday_run_1230") == 0.60
    assert ledger.recorded_decision("xyz", "2026-08-03", "midday_run_1230") is None


def test_settle_consumes_nothing_when_the_first_entry_alone_exceeds_submission(tmp_path):
    # A submission smaller than the first preserved entry must not mark it
    # consumed, but it does reduce that entry to the uncovered remainder.
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
