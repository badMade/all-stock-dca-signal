"""Focused regressions for the follow-up fixes requested on PR 612."""

from datetime import date

from dca_risk import deployed_usd_by_claim_date
from dca_whole_share_accumulator import WholeShareAccumulationLedger


def test_recovered_broker_statuses_count_as_deployed_capital():
    state = {
        "trading_day": "2026-08-28",
        "buy_candidates": [
            {"status": status, "planned_amount_usd": 10}
            for status in ("submitted", "filled", "pending", "unconfirmed")
        ],
    }

    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 28): 40.0}


def test_redispatch_cannot_spend_an_unrelated_claims_pending_credit(tmp_path):
    ledger = WholeShareAccumulationLedger(tmp_path)
    first = ledger.record_contribution(
        "BYDDY", "2026-08-28", 10.0, 10.0, idempotency_key="claim-a"
    )
    ledger.preserve_pending(
        "BYDDY",
        "2026-08-28",
        10.0,
        idempotency_key="claim-a",
        claim_token=first.claim_token,
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-28", 10.0, 100.0, idempotency_key="claim-b"
    )

    retry = ledger.record_contribution(
        "BYDDY", "2026-08-28", 0.0, 15.0, idempotency_key="claim-a"
    )

    assert retry.shares_to_buy == 0
    assert retry.order_notional_usd == 0.0
    assert ledger.pending_usd("BYDDY") == 20.0
