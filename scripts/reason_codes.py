#!/usr/bin/env python3
"""Unified structured decision/reason codes for the daily DCA streams.

The reliability plan calls for *structured reason codes* -- a single vocabulary
of machine-readable strings that explain why a run, a per-symbol decision, or a
safety gate produced the outcome it did (``NO_LOWER_PRICE``, ``ALREADY_FILLED``,
``STALE_POINTER``, ``BROKER_HISTORY_INCOMPLETE``, ``SAFETY_CAP`` ...). Today
those reasons are scattered across several modules under different spellings:

  * :mod:`scripts.same_day_pointer` -- ``STATUS_STALE`` / ``STATUS_PENDING`` ...
    (pointer freshness).
  * :mod:`scripts.run_classification` -- the ``top_up_guard_result`` codes
    (``no_prior_run``, ``price_not_below_prior_run``, ``incremental_top_up``,
    ``no_incremental_difference``, ``missing_prior_price``).
  * ``schemas/run_state.schema.json`` -- the ``order_submit_status`` enum.

This module does NOT replace those (they remain the on-disk contract); it is the
one place that *names* the plan's canonical reason codes, validates them, and
bridges the existing lower-level codes onto the canonical set so ledger/metrics
consumers can group heterogeneous outcomes without re-learning five vocabularies.

Standard library only. No trading logic, no network, no secrets.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

# --- Allocation / top-up decision outcomes -----------------------------------

#: No prior same-day allocation for the symbol -> the full desired amount is a
#: first allocation (not a top-up).
FIRST_ALLOCATION = "FIRST_ALLOCATION"
#: Prior allocation exists and the current price is strictly below the earlier
#: reference price -> buy the incremental ``desired - effective_prior``.
EFFECTIVE_TOP_UP = "EFFECTIVE_TOP_UP"
#: Prior allocation exists but the current price is NOT below the earlier
#: reference price -> no incremental buy (the top-up invariant blocked it).
NO_LOWER_PRICE = "NO_LOWER_PRICE"
#: The symbol's effective prior allocation already meets or exceeds the desired
#: amount -> nothing left to buy.
ALREADY_MET_OR_OVERFILLED = "ALREADY_MET_OR_OVERFILLED"
#: The incremental top-up was computed but is below the broker minimum.
BELOW_BROKER_MINIMUM_ORDER_SIZE = "BELOW_BROKER_MINIMUM_ORDER_SIZE"
#: A same-day order/fill already exists for the symbol (duplicate guard).
ALREADY_FILLED = "ALREADY_FILLED"
#: Prior allocation exists but no comparable reference price is available ->
#: conservatively buy $0 and flag for manual review.
MISSING_PRIOR_PRICE = "MISSING_PRIOR_PRICE"
#: No current decision price available -> cannot evaluate the top-up invariant.
MISSING_CURRENT_PRICE = "MISSING_CURRENT_PRICE"

# --- Safety / fail-closed blocks ---------------------------------------------

#: Same-day broker order history could not be proven complete (pagination did
#: not terminate cleanly, or an artifact order id was never resolved). The
#: fail-closed outcome: do NOT buy rather than assume zero prior allocation.
BROKER_HISTORY_INCOMPLETE = "BROKER_HISTORY_INCOMPLETE"
#: A latest_* pointer was stale/prior-date/malformed and cannot be trusted as
#: same-day scheduled evidence.
STALE_POINTER = "STALE_POINTER"
#: A per-symbol / daily / weekly exposure cap would be exceeded.
SAFETY_CAP = "SAFETY_CAP"
#: The configured broker account was not pinned/validated before reconciliation
#: or submission.
ACCOUNT_UNPINNED = "ACCOUNT_UNPINNED"
#: Same-day broker coverage cannot be proven complete, so trading is locked out
#: for the window (the incomplete-history lockout).
INCOMPLETE_HISTORY_LOCKOUT = "INCOMPLETE_HISTORY_LOCKOUT"

# --- Terminal / status outcomes ----------------------------------------------

SUBMITTED = "SUBMITTED"
SKIPPED = "SKIPPED"
BLOCKED = "BLOCKED"
DRY_RUN = "DRY_RUN"
NO_CANDIDATES = "NO_CANDIDATES"

#: Every canonical reason code, for validation and enumeration.
ALL_REASONS: FrozenSet[str] = frozenset(
    {
        FIRST_ALLOCATION,
        EFFECTIVE_TOP_UP,
        NO_LOWER_PRICE,
        ALREADY_MET_OR_OVERFILLED,
        BELOW_BROKER_MINIMUM_ORDER_SIZE,
        ALREADY_FILLED,
        MISSING_PRIOR_PRICE,
        MISSING_CURRENT_PRICE,
        BROKER_HISTORY_INCOMPLETE,
        STALE_POINTER,
        SAFETY_CAP,
        ACCOUNT_UNPINNED,
        INCOMPLETE_HISTORY_LOCKOUT,
        SUBMITTED,
        SKIPPED,
        BLOCKED,
        DRY_RUN,
        NO_CANDIDATES,
    }
)

#: Reason codes that mean "no buy happened / no buy should happen". Useful for
#: ledger and metrics consumers that need to separate no-buy runs from buys.
NO_BUY_REASONS: FrozenSet[str] = frozenset(
    {
        NO_LOWER_PRICE,
        ALREADY_MET_OR_OVERFILLED,
        BELOW_BROKER_MINIMUM_ORDER_SIZE,
        ALREADY_FILLED,
        MISSING_PRIOR_PRICE,
        MISSING_CURRENT_PRICE,
        BROKER_HISTORY_INCOMPLETE,
        STALE_POINTER,
        SAFETY_CAP,
        ACCOUNT_UNPINNED,
        INCOMPLETE_HISTORY_LOCKOUT,
        SKIPPED,
        BLOCKED,
        NO_CANDIDATES,
        # A dry run places no real order, so it is a no-buy outcome for any
        # ledger/metrics consumer separating no-buy runs from actual purchases.
        DRY_RUN,
    }
)

#: Reason codes that represent a fail-closed safety block (as opposed to a
#: benign no-buy such as NO_LOWER_PRICE). A window ending on one of these should
#: be surfaced as blocked, not as a successful no-buy run.
FAIL_CLOSED_REASONS: FrozenSet[str] = frozenset(
    {
        BROKER_HISTORY_INCOMPLETE,
        STALE_POINTER,
        SAFETY_CAP,
        ACCOUNT_UNPINNED,
        INCOMPLETE_HISTORY_LOCKOUT,
    }
)

#: Bridge from :mod:`scripts.run_classification`'s ``top_up_guard_result`` codes
#: (the on-disk ``top_up_guard_result`` enum in schemas/run_state.schema.json)
#: onto the canonical vocabulary above, so a consumer reading either spelling
#: can normalize to one set. ``incremental_top_up_amount`` returns the left-hand
#: keys; :func:`from_top_up_guard` maps them to the right-hand canonical codes.
_TOP_UP_GUARD_TO_CANONICAL: Dict[str, str] = {
    "no_prior_run": FIRST_ALLOCATION,
    "incremental_top_up": EFFECTIVE_TOP_UP,
    "price_not_below_prior_run": NO_LOWER_PRICE,
    "no_incremental_difference": ALREADY_MET_OR_OVERFILLED,
    "below_broker_minimum_order_size": BELOW_BROKER_MINIMUM_ORDER_SIZE,
    "missing_prior_price": MISSING_PRIOR_PRICE,
    "missing_prior_base": MISSING_PRIOR_PRICE,
}


def is_valid_reason(code: str) -> bool:
    """True when ``code`` is a canonical reason code."""
    return code in ALL_REASONS


def from_top_up_guard(guard_result: str) -> str:
    """Map a ``top_up_guard_result`` code to the canonical reason vocabulary.

    Unknown inputs are returned unchanged so a newly-added lower-level code is
    surfaced verbatim rather than silently coerced to a wrong canonical value.
    """
    return _TOP_UP_GUARD_TO_CANONICAL.get(guard_result, guard_result)


def is_fail_closed(code: str) -> bool:
    """True when ``code`` is a fail-closed safety block (not a benign no-buy)."""
    return code in FAIL_CLOSED_REASONS


__all__ = [
    "FIRST_ALLOCATION",
    "EFFECTIVE_TOP_UP",
    "NO_LOWER_PRICE",
    "ALREADY_MET_OR_OVERFILLED",
    "BELOW_BROKER_MINIMUM_ORDER_SIZE",
    "ALREADY_FILLED",
    "MISSING_PRIOR_PRICE",
    "MISSING_CURRENT_PRICE",
    "BROKER_HISTORY_INCOMPLETE",
    "STALE_POINTER",
    "SAFETY_CAP",
    "ACCOUNT_UNPINNED",
    "INCOMPLETE_HISTORY_LOCKOUT",
    "SUBMITTED",
    "SKIPPED",
    "BLOCKED",
    "DRY_RUN",
    "NO_CANDIDATES",
    "ALL_REASONS",
    "NO_BUY_REASONS",
    "FAIL_CLOSED_REASONS",
    "is_valid_reason",
    "from_top_up_guard",
    "is_fail_closed",
]
