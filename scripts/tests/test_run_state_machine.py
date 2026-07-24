"""Tests for the per-window run state machine."""

from __future__ import annotations

import pytest

from scripts import run_state_machine as sm
from scripts.run_state_machine import IllegalTransitionError, RunStateMachine


def test_happy_path_no_buy_run():
    machine = RunStateMachine()
    for state in (sm.PREFLIGHT, sm.RECONCILED, sm.DECIDED, sm.VERIFIED, sm.PUBLISHED):
        machine.advance(state)
    assert machine.is_clean_publish
    assert machine.is_done
    assert not machine.is_abnormal_end


def test_happy_path_with_submission():
    machine = RunStateMachine()
    for state in (sm.PREFLIGHT, sm.RECONCILED, sm.DECIDED, sm.SUBMITTED, sm.VERIFIED, sm.PUBLISHED):
        machine.advance(state)
    assert machine.state == sm.PUBLISHED


def test_illegal_skip_ahead_raises():
    machine = RunStateMachine()
    with pytest.raises(IllegalTransitionError):
        machine.advance(sm.PUBLISHED)  # cannot publish straight from pending


def test_cannot_transition_out_of_terminal():
    machine = RunStateMachine()
    machine.advance(sm.SKIPPED)
    assert machine.is_done
    with pytest.raises(IllegalTransitionError):
        machine.advance(sm.PREFLIGHT)


def test_partially_reconciled_must_resolve_to_decided_or_block():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.PARTIALLY_RECONCILED)
    assert machine.can_advance(sm.DECIDED)
    assert machine.can_advance(sm.BLOCKED)
    assert not machine.can_advance(sm.SUBMITTED)


def test_abort_to_failed_from_any_nonterminal():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.FAILED, reason="broker outage")
    assert machine.is_abnormal_end
    assert not machine.is_clean_publish


def test_last_safe_state_resumes_from_last_nonterminal():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.TIMED_OUT)
    # A recovery controller resumes at reconciled, not from pending.
    assert machine.last_safe_state() == sm.RECONCILED


def test_last_safe_state_of_live_window_is_current():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    assert machine.last_safe_state() == sm.PREFLIGHT


def test_history_records_transitions_and_reasons():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT, reason="fired")
    machine.advance(sm.BLOCKED, reason="stale pointer")
    assert [t.to_state for t in machine.history] == [sm.PREFLIGHT, sm.BLOCKED]
    assert machine.history[-1].reason == "stale pointer"


def test_unknown_state_rejected():
    with pytest.raises(ValueError):
        RunStateMachine(state="banana")
    machine = RunStateMachine()
    with pytest.raises(ValueError):
        machine.advance("banana")


def test_partial_reconciliation_blocks_submission_via_lineage():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.PARTIALLY_RECONCILED)
    machine.advance(sm.DECIDED)  # fail-closed no-buy decision is legal
    assert machine.entered_partial_reconciliation
    with pytest.raises(IllegalTransitionError):
        machine.advance(sm.SUBMITTED)
    # It may still publish the no-buy outcome.
    machine.advance(sm.VERIFIED)
    machine.advance(sm.PUBLISHED)
    assert machine.is_clean_publish


def test_can_advance_honors_partial_lineage_like_advance():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.PARTIALLY_RECONCILED)
    machine.advance(sm.DECIDED)
    # can_advance must agree with advance: submission is not advisable once the
    # window entered partially_reconciled, even though the static table allows it.
    assert not machine.can_advance(sm.SUBMITTED)
    assert machine.can_advance(sm.VERIFIED)
    with pytest.raises(IllegalTransitionError):
        machine.advance(sm.SUBMITTED)


def test_verified_can_time_out():
    machine = RunStateMachine()
    for state in (sm.PREFLIGHT, sm.RECONCILED, sm.DECIDED, sm.SUBMITTED, sm.VERIFIED):
        machine.advance(state)
    machine.advance(sm.TIMED_OUT)
    assert machine.is_abnormal_end


def test_to_dict_from_dict_preserves_partial_lineage():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.PARTIALLY_RECONCILED)
    machine.advance(sm.DECIDED)
    restored = RunStateMachine.from_dict(machine.to_dict())
    assert restored.state == sm.DECIDED
    assert restored.entered_partial_reconciliation
    # The fail-closed submission block survives serialization across a crash.
    with pytest.raises(IllegalTransitionError):
        restored.advance(sm.SUBMITTED)


def test_from_dict_rejects_state_without_consistent_history():
    # A persisted record with an advanced state but missing history would drop the
    # partial-reconciliation lineage (can_advance("submitted") -> True). from_dict
    # must reject it -- the history must legally reach the persisted state.
    with pytest.raises(ValueError):
        RunStateMachine.from_dict({"state": "decided"})
    with pytest.raises(ValueError):
        RunStateMachine.from_dict({"state": "decided", "history": []})
    # An inconsistent history (a transition that does not start where the machine
    # currently is) is also rejected.
    with pytest.raises(ValueError):
        RunStateMachine.from_dict({
            "state": "submitted",
            "history": [{"from_state": "reconciled", "to_state": "submitted"}],
        })


def test_from_dict_round_trip_of_partial_lineage_stays_non_submittable():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.PARTIALLY_RECONCILED)
    machine.advance(sm.DECIDED)
    restored = RunStateMachine.from_dict(machine.to_dict())
    assert restored.state == sm.DECIDED
    assert restored.entered_partial_reconciliation
    assert not restored.can_advance(sm.SUBMITTED)
    with pytest.raises(IllegalTransitionError):
        restored.advance(sm.SUBMITTED)


def test_last_safe_state_does_not_resume_blocked_window():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.PARTIALLY_RECONCILED)
    machine.advance(sm.DECIDED)
    machine.advance(sm.BLOCKED)
    # A deliberate block must not be reopened into a submittable state.
    resume = machine.last_safe_state()
    assert resume == sm.BLOCKED
    assert sm.is_terminal(resume)


def test_skip_allowed_from_reconciled_and_partially_reconciled():
    m = RunStateMachine()
    m.advance(sm.PREFLIGHT)
    m.advance(sm.RECONCILED)
    assert m.can_advance(sm.SKIPPED)  # e.g. no eligible symbols post-reconciliation
    m.advance(sm.SKIPPED)
    assert m.is_done and m.is_abnormal_end

    m2 = RunStateMachine()
    m2.advance(sm.PREFLIGHT)
    m2.advance(sm.RECONCILED)
    m2.advance(sm.PARTIALLY_RECONCILED)
    assert m2.can_advance(sm.SKIPPED)
    m2.advance(sm.SKIPPED)
    assert m2.is_done


def test_decided_abort_resumes_at_reconciled_not_decided():
    m = RunStateMachine()
    for state in (sm.PREFLIGHT, sm.RECONCILED, sm.DECIDED):
        m.advance(state)
    m.advance(sm.FAILED)
    # A crashed decision resumes at reconciled (re-decide), never at decided --
    # so a recovery can't be walked decided->submitted, bypassing a no-buy call.
    assert m.last_safe_state() == sm.RECONCILED


def test_last_safe_state_resumes_failed_window():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.FAILED)
    assert machine.last_safe_state() == sm.RECONCILED


def test_last_safe_state_of_partial_then_failed_resumes_at_partial():
    machine = RunStateMachine()
    machine.advance(sm.PREFLIGHT)
    machine.advance(sm.RECONCILED)
    machine.advance(sm.PARTIALLY_RECONCILED)
    machine.advance(sm.DECIDED)
    machine.advance(sm.FAILED)
    resume = machine.last_safe_state()
    assert resume == sm.PARTIALLY_RECONCILED
    # A recovery reconstructed from the resume state alone stays non-submittable.
    recovered = RunStateMachine(state=resume)
    assert recovered.entered_partial_reconciliation
    recovered.advance(sm.DECIDED)
    with pytest.raises(IllegalTransitionError):
        recovered.advance(sm.SUBMITTED)
