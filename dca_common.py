"""Shared helpers used by more than one live-execution stream.

Currently holds ``build_top_up_outcome``, the same-day top-up outcome
constructor that both ``near_close_dca`` (afternoon top-up guard) and
``morning_dca`` (morning top-up) call so the two streams build
``TopUpOutcome``/``TopUpGuardOutcome`` from one implementation instead of
two independently-maintained copies.
"""

from typing import Any, Optional, TypeVar

T = TypeVar("T")

def build_top_up_outcome(
    outcome_cls: Any,
    ticker: str,
    desired_amount: float,
    decision_price: Optional[float],
    prior: Any,
    incremental: float,
    result: str,
    skipped: bool,
    effective_base: float,
    manual_review: bool = False,
    cumulative_credit_decline_usd: Optional[float] = None,
    discarded_pending_credit_usd: Optional[float] = None,
) -> T:
    """Construct a TopUpOutcome or TopUpGuardOutcome with common fields."""

    kwargs = {
        "ticker": ticker,
        "prior_same_day_amount": round(prior.amount_usd, 2) if prior else 0.0,
        "prior_same_day_reference_price": prior.reference_price if prior else None,
        "prior_same_day_run_id": prior.run_id if prior else None,
        "prior_same_day_source": prior.source if prior else None,
        "incremental_order_amount": round(max(0.0, incremental), 2),
        "effective_base_amount_usd": effective_base,
        "cumulative_credit_decline_usd": cumulative_credit_decline_usd,
        "top_up_guard_result": result,
    }

    if outcome_cls.__name__ == "TopUpGuardOutcome":
        kwargs["afternoon_desired_amount"] = desired_amount
        kwargs["afternoon_decision_price"] = decision_price
        kwargs["skipped"] = skipped
        kwargs["manual_review"] = manual_review
        # Only TopUpGuardOutcome (near_close_dca's 15:30 final session) ever
        # calls apply_cumulative_credit with final_session=True, so only it
        # carries this field -- TopUpOutcome (morning_dca's regular-session
        # streams) never produces a discard decision to report.
        kwargs["discarded_pending_credit_usd"] = discarded_pending_credit_usd
    else:
        kwargs["desired_amount"] = desired_amount
        kwargs["decision_price"] = decision_price
        kwargs["top_up_skipped"] = skipped
        kwargs["top_up_manual_review"] = manual_review

    return outcome_cls(**kwargs)
