#!/usr/bin/env python3
"""Run state machine for a single scheduled/recovery DCA window.

The reliability plan models each execution window as a progression:

    pending -> preflight -> reconciled -> decided -> submitted -> verified -> published

with off-happy-path terminal outcomes that the window-level status panel must be
able to distinguish (a successful no-buy ``published`` run is NOT the same as a
``skipped``, ``blocked``, ``timed_out``, ``partially_reconciled``, or ``failed``
run). Today the tracked code only records discrete ``order_submit_status`` /
``pointer_status`` values with no notion of *which transitions are legal*, so an
inconsistent progression (e.g. publishing before reconciliation, or resuming a
window that already terminated) cannot be caught.

This module is that missing guard: a pure, side-effect-free state machine that
knows the legal transitions, refuses illegal ones, records the transition
history, and lets a recovery controller resume from the last safe state instead
of blindly rerunning the whole window.

Standard library only. No trading logic, no network, no secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

# --- Happy-path states (in order) --------------------------------------------

PENDING = "pending"
PREFLIGHT = "preflight"
RECONCILED = "reconciled"
DECIDED = "decided"
SUBMITTED = "submitted"
VERIFIED = "verified"
PUBLISHED = "published"

# --- Off-happy-path states ----------------------------------------------------

#: Reconciliation completed but could not be proven complete (e.g. broker
#: history incomplete). Not terminal: the window must still resolve to ``decided``
#: (fail-closed, no buy) or a terminal block.
PARTIALLY_RECONCILED = "partially_reconciled"
BLOCKED = "blocked"
SKIPPED = "skipped"
TIMED_OUT = "timed_out"
FAILED = "failed"

HAPPY_PATH = (PENDING, PREFLIGHT, RECONCILED, DECIDED, SUBMITTED, VERIFIED, PUBLISHED)

#: States from which no further transition is allowed.
TERMINAL_STATES: FrozenSet[str] = frozenset({PUBLISHED, BLOCKED, SKIPPED, TIMED_OUT, FAILED})

#: An abnormal end that is NOT a clean published run -- the window-level status
#: panel surfaces these distinctly from a successful (possibly no-buy) publish.
ABNORMAL_TERMINALS: FrozenSet[str] = frozenset({BLOCKED, SKIPPED, TIMED_OUT, FAILED})

# From any non-terminal state a window may also abort to failed/timed_out; those
# are folded into each state's allowed set below so the map is explicit.
_ABORTS = frozenset({FAILED, TIMED_OUT})

_LEGAL_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    PENDING: frozenset({PREFLIGHT, SKIPPED, BLOCKED}) | _ABORTS,
    PREFLIGHT: frozenset({RECONCILED, SKIPPED, BLOCKED}) | _ABORTS,
    RECONCILED: frozenset({DECIDED, PARTIALLY_RECONCILED, SKIPPED, BLOCKED}) | _ABORTS,
    PARTIALLY_RECONCILED: frozenset({DECIDED, SKIPPED, BLOCKED}) | _ABORTS,
    # A no-buy decision goes straight to verified (nothing to submit); a buy
    # decision goes to submitted. A decision can also be skipped/blocked.
    DECIDED: frozenset({SUBMITTED, VERIFIED, SKIPPED, BLOCKED}) | _ABORTS,
    SUBMITTED: frozenset({VERIFIED}) | _ABORTS,
    # Verification can itself time out or fail (e.g. a broker fill-confirmation
    # lookup hangs), so aborts are permitted here too -- consistent with the
    # module contract that any non-terminal state may abort to failed/timed_out.
    VERIFIED: frozenset({PUBLISHED}) | _ABORTS,
    # Terminal states have no outgoing transitions.
    PUBLISHED: frozenset(),
    BLOCKED: frozenset(),
    SKIPPED: frozenset(),
    TIMED_OUT: frozenset(),
    FAILED: frozenset(),
}

ALL_STATES: FrozenSet[str] = frozenset(_LEGAL_TRANSITIONS)


class IllegalTransitionError(RuntimeError):
    """Raised when an illegal or post-terminal state transition is attempted."""


def is_terminal(state: str) -> bool:
    """True when ``state`` allows no further transitions."""
    return state in TERMINAL_STATES


def can_transition(from_state: str, to_state: str) -> bool:
    """True when ``from_state`` -> ``to_state`` is a legal transition."""
    return to_state in _LEGAL_TRANSITIONS.get(from_state, frozenset())


@dataclass
class Transition:
    """One recorded state transition."""

    from_state: str
    to_state: str
    reason: Optional[str] = None
    at: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Transition":
        return cls(
            from_state=data["from_state"],
            to_state=data["to_state"],
            reason=data.get("reason"),
            at=data.get("at"),
        )


@dataclass
class RunStateMachine:
    """Track a window's state with legal-transition enforcement and history.

    ``state`` starts at ``pending``. :meth:`advance` validates each transition
    against :data:`_LEGAL_TRANSITIONS` and records it; an illegal transition (or
    any transition out of a terminal state) raises :class:`IllegalTransitionError`
    rather than silently corrupting the progression.
    """

    state: str = PENDING
    history: List[Transition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state not in ALL_STATES:
            raise ValueError(f"unknown initial state {self.state!r}")

    def can_advance(self, to_state: str) -> bool:
        """True when the window may legally advance to ``to_state`` now.

        Applies the same fail-closed lineage guard as :meth:`advance`, so a
        controller using this predicate to decide whether to submit agrees with
        enforcement: submission is never advisable once the window entered
        ``partially_reconciled``, even though the static table would allow the
        ``decided -> submitted`` edge.
        """
        if to_state == SUBMITTED and self.entered_partial_reconciliation:
            return False
        return can_transition(self.state, to_state)

    def advance(self, to_state: str, *, reason: Optional[str] = None, at: Optional[str] = None) -> "RunStateMachine":
        """Advance to ``to_state``, recording the transition. Raises if illegal.

        ``at`` is an optional caller-supplied timestamp (kept out of the core so
        the machine stays deterministic and wall-clock-free); the run ledger is
        where timestamps are authoritative.
        """
        if to_state not in ALL_STATES:
            raise ValueError(f"unknown target state {to_state!r}")
        if is_terminal(self.state):
            raise IllegalTransitionError(
                f"cannot transition out of terminal state {self.state!r} (to {to_state!r})"
            )
        if not can_transition(self.state, to_state):
            raise IllegalTransitionError(
                f"illegal transition {self.state!r} -> {to_state!r}; "
                f"allowed: {sorted(_LEGAL_TRANSITIONS[self.state])}"
            )
        # Fail-closed lineage guard: once a window entered partially_reconciled
        # (broker history could not be proven complete), submission is
        # unreachable for the rest of its life. Without this, a caller could
        # legally walk partially_reconciled -> decided -> submitted and place an
        # order on an incomplete history -- exactly what partial reconciliation
        # must prevent. Such a window may only fail closed to a no-buy
        # (decided -> verified -> published) or a terminal block.
        if to_state == SUBMITTED and self.entered_partial_reconciliation:
            raise IllegalTransitionError(
                "submission is unreachable once the window entered "
                "partially_reconciled; an incomplete broker history must fail "
                "closed to a no-buy decision, never a submission"
            )
        self.history.append(Transition(self.state, to_state, reason=reason, at=at))
        self.state = to_state
        return self

    @property
    def entered_partial_reconciliation(self) -> bool:
        """True when the window ever entered ``partially_reconciled``.

        Sticky across the window's whole lifetime (checks history, not just the
        current state), so the fail-closed submission guard holds even after the
        window has moved on to ``decided``.
        """
        return self.state == PARTIALLY_RECONCILED or any(
            PARTIALLY_RECONCILED in (transition.from_state, transition.to_state)
            for transition in self.history
        )

    @property
    def is_done(self) -> bool:
        """True when the window has reached a terminal state."""
        return is_terminal(self.state)

    @property
    def is_clean_publish(self) -> bool:
        """True for a window that completed the happy path to ``published``."""
        return self.state == PUBLISHED

    @property
    def is_abnormal_end(self) -> bool:
        """True for a blocked/skipped/timed_out/failed terminal (not a clean publish)."""
        return self.state in ABNORMAL_TERMINALS

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the machine (state + full history) for cross-process persistence.

        Persisting the history -- not just the current state -- is required so a
        runner that crashes and resumes preserves ``entered_partial_reconciliation``
        and therefore the fail-closed submission block. Reconstructing from the
        current state alone would silently drop that guard.
        """
        return {"state": self.state, "history": [t.to_dict() for t in self.history]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunStateMachine":
        """Rebuild a machine from :meth:`to_dict` output, validating the history.

        The recorded history is REPLAYED from ``pending`` and must legally reach
        the persisted ``state``. A record with an advanced state but missing or
        corrupt ``history`` (e.g. ``{"state": "decided"}``) is rejected: restoring
        it would drop the partial-reconciliation lineage and let a recovery submit
        after an incomplete reconciliation. Fail closed (raise ``ValueError``)
        rather than expose an unsafe machine.
        """
        state = data.get("state") or PENDING
        if state not in ALL_STATES:
            raise ValueError(f"unknown persisted state {state!r}")
        try:
            transitions = [Transition.from_dict(item) for item in data.get("history") or []]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"corrupt persisted transition history: {exc}") from exc
        rebuilt = cls()  # starts at pending with empty history
        for transition in transitions:
            if transition.from_state != rebuilt.state:
                raise ValueError(
                    f"inconsistent persisted history: transition from "
                    f"{transition.from_state!r} but machine is at {rebuilt.state!r}"
                )
            # advance() re-validates legality (incl. the partial-lineage submission
            # guard) and rebuilds entered_partial_reconciliation from the history.
            rebuilt.advance(transition.to_state, reason=transition.reason, at=transition.at)
        if rebuilt.state != state:
            raise ValueError(
                f"persisted state {state!r} is not reached by its history "
                f"(history ends at {rebuilt.state!r})"
            )
        return rebuilt

    def last_safe_state(self) -> str:
        """The state a recovery controller should resume from.

        Recovery resumes from the last non-terminal state the window actually
        reached rather than rerunning from ``pending``: if the window aborted
        (failed/timed_out) after reconciling, the resumed run re-enters at
        ``reconciled``. A window that only reached ``pending`` resumes there.

        Only unexpected aborts (``failed``/``timed_out``) are resumable. A
        deliberate terminal -- a successful ``published`` run, or a
        ``blocked``/``skipped`` safety stop -- is NOT reopened: this returns the
        terminal state itself, so a recovery controller that builds a machine at
        it stays terminal and cannot advance a blocked window back into the
        submission path (bypassing the original safety block). A window that
        aborted at ``decided`` resumes at ``reconciled`` (re-decide), never at
        ``decided`` itself, so a crashed no-buy decision cannot be resumed
        straight into a submission.
        """
        if not is_terminal(self.state):
            return self.state
        if self.state not in (FAILED, TIMED_OUT):
            return self.state
        # A window that ever entered partial reconciliation must resume AT
        # ``partially_reconciled`` -- never at a later ``decided`` -- so a recovery
        # controller that reconstructs ``RunStateMachine(state=resume_state)``
        # (losing the history) still cannot advance into the submission path: the
        # partial state itself keeps ``entered_partial_reconciliation`` True and
        # submission unreachable. See the fail-closed submission guard in advance().
        if self.entered_partial_reconciliation:
            return PARTIALLY_RECONCILED
        # Never resume AT ``decided``. ``decided`` is a computed decision, not a
        # checkpoint: whether it was a buy or an explicit no-buy is not encoded in
        # the state, and ``decided -> submitted`` is a legal edge. A recovery
        # controller that reconstructs ``RunStateMachine(state=decided)`` could
        # therefore submit an order the original run had declined. Resuming at the
        # state BEFORE the decision (``reconciled``) forces the decision to be
        # re-derived from fresh exposure/prices rather than skipped straight to a
        # submission -- a legitimate buy is still reachable, a stale no-buy is not
        # silently converted into one.
        for transition in reversed(self.history):
            if not is_terminal(transition.from_state) and transition.from_state != DECIDED:
                return transition.from_state
        return PENDING


__all__ = [
    "PENDING",
    "PREFLIGHT",
    "RECONCILED",
    "DECIDED",
    "SUBMITTED",
    "VERIFIED",
    "PUBLISHED",
    "PARTIALLY_RECONCILED",
    "BLOCKED",
    "SKIPPED",
    "TIMED_OUT",
    "FAILED",
    "HAPPY_PATH",
    "TERMINAL_STATES",
    "ABNORMAL_TERMINALS",
    "ALL_STATES",
    "IllegalTransitionError",
    "is_terminal",
    "can_transition",
    "Transition",
    "RunStateMachine",
]
