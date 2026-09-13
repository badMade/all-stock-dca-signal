"""Shared helpers used by more than one live-execution stream.

Currently holds ``build_top_up_outcome``, the same-day top-up outcome
constructor that both ``near_close_dca`` (afternoon top-up guard) and
``morning_dca`` (morning top-up) call so the two streams build
``TopUpOutcome``/``TopUpGuardOutcome`` from one implementation instead of
two independently-maintained copies; and ``_num``/``USAGE_PROXY_FIELDS``,
the usage-proxy casting helper and field list shared by the cost tracker
and the weekly dashboard.
"""

from dataclasses import dataclass
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")

# Lightweight usage-proxy fields logged to each live run's run_state so the monthly
# cost tracker can aggregate automation load without any exact credit accounting.
# All numbers are activity proxies (call/row/byte counts), never real credit spend.
USAGE_PROXY_NUMERIC_FIELDS = (
    "tickers_scanned",
    "finance_calls_attempted",
    "robinhood_calls_attempted",
    "signal_rows_written",
    "buy_candidates_written",
    "order_reviews_attempted",
    "orders_placed_count",
    "state_file_bytes",
    "csv_artifact_count",
    "total_artifact_bytes",
    "notifications_sent",
    "errors_count",
)

USAGE_PROXY_FIELDS = (
    *USAGE_PROXY_NUMERIC_FIELDS,
    "manual_recovery_required",
    "cost_control_notes",
)


def _num(value: Any) -> float:
    """Safely cast a mixed-type JSON scalar to float (returning 0.0 on failure)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class TopUpContext(Generic[T]):
    """Context holding parameters for building top-up outcomes."""

    outcome_cls: Any
    ticker: str
    desired_amount: float
    decision_price: Optional[float]
    prior: Any
    incremental: float
    result: str
    skipped: bool
    effective_base: float
    manual_review: bool = False
    cumulative_credit_decline_usd: Optional[float] = None
    discarded_pending_credit_usd: Optional[float] = None


def build_top_up_outcome(ctx: TopUpContext[T]) -> T:
    """Construct a TopUpOutcome or TopUpGuardOutcome with common fields."""

    kwargs = {
        "ticker": ctx.ticker,
        "prior_same_day_amount": round(ctx.prior.amount_usd, 2) if ctx.prior else 0.0,
        "prior_same_day_reference_price": ctx.prior.reference_price if ctx.prior else None,
        "prior_same_day_run_id": ctx.prior.run_id if ctx.prior else None,
        "prior_same_day_source": ctx.prior.source if ctx.prior else None,
        "incremental_order_amount": round(max(0.0, ctx.incremental), 2),
        "effective_base_amount_usd": ctx.effective_base,
        "cumulative_credit_decline_usd": ctx.cumulative_credit_decline_usd,
        "top_up_guard_result": ctx.result,
    }

    if ctx.outcome_cls.__name__ == "TopUpGuardOutcome":
        kwargs["afternoon_desired_amount"] = ctx.desired_amount
        kwargs["afternoon_decision_price"] = ctx.decision_price
        kwargs["skipped"] = ctx.skipped
        kwargs["manual_review"] = ctx.manual_review
        # Only TopUpGuardOutcome (near_close_dca's 15:30 final session) ever
        # calls apply_cumulative_credit with final_session=True, so only it
        # carries this field -- TopUpOutcome (morning_dca's regular-session
        # streams) never produces a discard decision to report.
        kwargs["discarded_pending_credit_usd"] = ctx.discarded_pending_credit_usd
    else:
        kwargs["desired_amount"] = ctx.desired_amount
        kwargs["decision_price"] = ctx.decision_price
        kwargs["top_up_skipped"] = ctx.skipped
        kwargs["top_up_manual_review"] = ctx.manual_review

    return ctx.outcome_cls(**kwargs)
