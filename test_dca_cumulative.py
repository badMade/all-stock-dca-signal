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
