"""Structured admission and refusal reason codes for self-healing.

The self-heal coordinator records one stable, machine-readable reason for every
admission decision.  Keeping that vocabulary here prevents the observation,
admission, state, and reporting layers from inventing subtly different names
for the same safety gate.

Standard library only. No admission logic, subprocesses, network, or secrets.
"""

from __future__ import annotations

from typing import FrozenSet

# --- Operator and concurrency gates -----------------------------------------

#: The operator-controlled repair switch is off. Observation deliberately
#: remains independent so disabling mutation can never make incidents invisible.
DISABLED = "DISABLED"
#: Another supervised tick owns the single-run lock. Refusing a second worker
#: prevents concurrent state transactions and duplicate repair publication.
ALREADY_RUNNING = "ALREADY_RUNNING"

# --- Hard lifetime, breaker, and frequency bounds ---------------------------

#: The monotonic epoch counter reached its finite ceiling. Only the human
#: ``--new-epoch`` action may re-arm this lifetime bound; pipeline code may not.
LIFETIME_BOUND_REACHED = "LIFETIME_BOUND_REACHED"
#: Enough repairs have failed in the current epoch to latch the breaker. There
#: is no automatic cooldown because retrying without review could create a loop.
BREAKER_LATCHED = "BREAKER_LATCHED"
#: The explicit UTC-day repair allowance is exhausted. This gives the operator
#: a direct, independently enforced once-per-day limit in the shipped policy.
RATE_LIMITED_DAILY = "RATE_LIMITED_DAILY"

# --- Time and spend trust boundaries ----------------------------------------

#: Wall time falls outside the durable anchor's accepted forward and cumulative
#: skew bands. Repair refuses so clock jumps cannot manufacture fresh budgets.
CLOCK_IMPLAUSIBLE = "CLOCK_IMPLAUSIBLE"
#: Reserving the repair's local and CI worst case would exceed the current
#: monotonic-period allowance, so dispatch must not begin work it cannot fund.
BUDGET_EXHAUSTED_MINUTES = "BUDGET_EXHAUSTED_MINUTES"
#: The supervised repair used its elapsed-time allowance. Recording this reason
#: distinguishes a bounded termination from an adapter-specific repair failure.
WALL_CLOCK_EXCEEDED = "WALL_CLOCK_EXCEEDED"

# --- Incident eligibility and provenance ------------------------------------

#: This fingerprint consumed its complete attempt allowance. Quarantine keeps a
#: recurring incident from monopolizing repair capacity indefinitely.
FINGERPRINT_QUARANTINED = "FINGERPRINT_QUARANTINED"
#: The incident has no narrowly matched repair-and-verification adapter. It is
#: still reported, but generic mutation is forbidden because it is not safe.
REPORT_ONLY_NO_ADAPTER = "REPORT_ONLY_NO_ADAPTER"
#: Observation found no newly dispatchable fingerprint. This explicit no-work
#: result keeps an uneventful tick distinguishable from a failed coordinator.
NO_NOVEL_INCIDENTS = "NO_NOVEL_INCIDENTS"
#: The keyed fingerprint identity cannot be trusted because its key is missing
#: or unreadable. Automatic replacement would silently re-mint every attempt.
FINGERPRINT_KEY_UNAVAILABLE = "FINGERPRINT_KEY_UNAVAILABLE"
#: Durable composite provenance identifies an incident produced by this repair
#: pipeline. Dropping it before admission breaks feedback loops; archival stays.
SELF_ORIGIN_DROPPED = "SELF_ORIGIN_DROPPED"
#: Reconciliation cannot yet classify an incident's originating commit. Holding
#: it for a later tick avoids admitting it before self-origin checks complete.
PROVENANCE_PENDING = "PROVENANCE_PENDING"
#: The incident came from an untrusted non-main ref while repair targets
#: ``origin/main``. Spending main's bounded capacity on that mismatch is refused.
NOT_MAIN_REF = "NOT_MAIN_REF"

# --- Fail-closed foundation failures ----------------------------------------

#: Configuration is absent or malformed, so the coordinator cannot prove its
#: safety ceilings. Repair refuses instead of synthesizing permissive defaults.
CONFIG_INVALID = "CONFIG_INVALID"
#: The authoritative state cannot be parsed or validated. Guessing from the
#: non-authoritative journal could bypass a latch, reservation, or epoch bound.
STATE_UNREADABLE = "STATE_UNREADABLE"

# --- Successful admission outcome -------------------------------------------

#: Every admission gate passed and the authoritative reservation transaction
#: committed. This is the sole code that authorizes repair dispatch.
ADMITTED = "ADMITTED"

#: Every canonical admission-decision code, for validation and enumeration.
ALL_REASONS: FrozenSet[str] = frozenset(
    {
        DISABLED,
        ALREADY_RUNNING,
        LIFETIME_BOUND_REACHED,
        BREAKER_LATCHED,
        RATE_LIMITED_DAILY,
        CLOCK_IMPLAUSIBLE,
        BUDGET_EXHAUSTED_MINUTES,
        FINGERPRINT_QUARANTINED,
        REPORT_ONLY_NO_ADAPTER,
        WALL_CLOCK_EXCEEDED,
        NO_NOVEL_INCIDENTS,
        FINGERPRINT_KEY_UNAVAILABLE,
        SELF_ORIGIN_DROPPED,
        PROVENANCE_PENDING,
        NOT_MAIN_REF,
        CONFIG_INVALID,
        STATE_UNREADABLE,
        ADMITTED,
    }
)

#: Every canonical code that withholds repair admission. This is explicit rather
#: than implemented as ``code != ADMITTED`` so an unknown value is not silently
#: misclassified as a recognized refusal.
REFUSAL_REASONS: FrozenSet[str] = frozenset(
    {
        DISABLED,
        ALREADY_RUNNING,
        LIFETIME_BOUND_REACHED,
        BREAKER_LATCHED,
        RATE_LIMITED_DAILY,
        CLOCK_IMPLAUSIBLE,
        BUDGET_EXHAUSTED_MINUTES,
        FINGERPRINT_QUARANTINED,
        REPORT_ONLY_NO_ADAPTER,
        WALL_CLOCK_EXCEEDED,
        NO_NOVEL_INCIDENTS,
        FINGERPRINT_KEY_UNAVAILABLE,
        SELF_ORIGIN_DROPPED,
        PROVENANCE_PENDING,
        NOT_MAIN_REF,
        CONFIG_INVALID,
        STATE_UNREADABLE,
    }
)


def is_valid_reason(code: str) -> bool:
    """Return whether ``code`` belongs to the canonical reason vocabulary.

    Args:
        code: Candidate machine-readable reason code.

    Returns:
        ``True`` when ``code`` is a member of :data:`ALL_REASONS`.
    """
    return code in ALL_REASONS


def is_refusal(code: str) -> bool:
    """Return whether ``code`` is a recognized refusal of repair admission.

    Args:
        code: Candidate machine-readable reason code.

    Returns:
        ``True`` only for a member of :data:`REFUSAL_REASONS`; unknown strings
        and :data:`ADMITTED` both return ``False``.
    """
    return code in REFUSAL_REASONS


__all__ = [
    "DISABLED",
    "ALREADY_RUNNING",
    "LIFETIME_BOUND_REACHED",
    "BREAKER_LATCHED",
    "RATE_LIMITED_DAILY",
    "CLOCK_IMPLAUSIBLE",
    "BUDGET_EXHAUSTED_MINUTES",
    "FINGERPRINT_QUARANTINED",
    "REPORT_ONLY_NO_ADAPTER",
    "WALL_CLOCK_EXCEEDED",
    "NO_NOVEL_INCIDENTS",
    "FINGERPRINT_KEY_UNAVAILABLE",
    "SELF_ORIGIN_DROPPED",
    "PROVENANCE_PENDING",
    "NOT_MAIN_REF",
    "CONFIG_INVALID",
    "STATE_UNREADABLE",
    "ADMITTED",
    "ALL_REASONS",
    "REFUSAL_REASONS",
    "is_valid_reason",
    "is_refusal",
]
