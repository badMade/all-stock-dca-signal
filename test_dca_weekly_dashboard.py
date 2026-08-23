"""Tests for the DCA weekly dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dca_weekly_dashboard import (
    _has_submitted_evidence,
    _is_manual_run_state,
    build_graded_vs_fixed,
    build_performance_snapshot,
    load_json_files,
    load_scheduled_run_states,
)


def test_build_graded_vs_fixed_uses_per_candidate_dynamic_base():
    # Codex review ("Apply the dynamic base to the analytics baseline"): the fixed
    # 1x baseline must use each candidate's OWN selected_base_amount_usd ($1 or $2),
    # not a single flat base for every candidate that day.
    run_states = [
        {
            "trading_day": "2026-07-24",
            "budget": {"base_amount_usd": 2.0},
            "buy_candidates": [
                {"planned_amount_usd": 2.0, "selected_base_amount_usd": 1.0},  # $1 base
                {"planned_amount_usd": 4.0, "selected_base_amount_usd": 2.0},  # $2 base
                {"planned_amount_usd": 3.0},  # no recorded base -> falls back to the run's flat $2 base
            ],
        }
    ]
    report = build_graded_vs_fixed(run_states)
    assert "$9.00" in report  # graded: 2 + 4 + 3
    assert "$5.00" in report  # fixed: 1 (own base) + 2 (own base) + 2 (fallback), not 2 * 3 = 6


def test_build_performance_snapshot_counts_partial_submission_artifact():
    # Codex review, PR #538 discussion_r3837489854 ("Normalize retained
    # partial runs before dashboard aggregation"): morning/midday's
    # _blocked_live_result()/_apply_blocked_before_placement_fields() record
    # a genuine submitted order's counts under the NEWER top-level
    # orders_submitted_count and budget.hypothetical_day_spend_usd fields,
    # not the ordinary completed-run counts.submitted / budget.day_notional_usd
    # this function originally read. Before the fix, load_json_files() let
    # such a blocked-but-submitted artifact through (via
    # _has_submitted_evidence()), but build_performance_snapshot() then
    # aggregated it as zero orders and zero notional.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "counts": {"buy_candidates": 2},
            "budget": {
                "hypothetical_day_spend_usd": 3.5,
                "week_notional_usd": 3.5,
                "weekly_cap_usd": 100.0,
            },
            "orders_submitted_count": 1,
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 3.5},
                {"ticker": "BBB", "status": "not_submitted"},
            ],
            "broker_outcomes": {"AAA": {"status": "submitted"}},
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $3.50 |" in report


def test_build_performance_snapshot_prefers_ordinary_completed_run_fields():
    # A normal, fully completed run still reports through the ordinary
    # counts.submitted / budget.day_notional_usd fields -- the fallback to
    # orders_submitted_count/hypothetical_day_spend_usd must only kick in
    # when the ordinary field is actually absent, not override it.
    run_states = [
        {
            "trading_day": "2026-07-24",
            "counts": {"buy_candidates": 1, "submitted": 1},
            "budget": {
                "day_notional_usd": 5.0,
                "hypothetical_day_spend_usd": 999.0,
                "week_notional_usd": 5.0,
                "weekly_cap_usd": 100.0,
            },
            "orders_submitted_count": 999,
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 5.0}],
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $5.00 |" in report


def test_is_manual_run_state_no_classification():
    """Test when run_classification is missing."""
    assert _is_manual_run_state({}) is False
    assert _is_manual_run_state({"other_key": "value"}) is False


def test_is_manual_run_state_not_dict():
    """Test when data is not a dict."""
    # Test defensive handling of non-dict inputs despite type hints.
    assert _is_manual_run_state(None) is False  # type: ignore[arg-type]
    assert _is_manual_run_state([]) is False  # type: ignore[arg-type]


def test_is_manual_run_state_rc_not_dict():
    """Test when run_classification is not a dict."""
    assert _is_manual_run_state({"run_classification": "not_a_dict"}) is False
    assert _is_manual_run_state({"run_classification": None}) is False


def test_is_manual_run_state_not_manual():
    """Test when run_classification is present but not manual."""
    assert _is_manual_run_state({"run_classification": {}}) is False
    assert _is_manual_run_state({"run_classification": {"run_origin": "scheduled"}}) is False
    assert _is_manual_run_state({"run_classification": {"manual_run": False}}) is False
    assert _is_manual_run_state({"run_classification": {"run_origin": "scheduled", "manual_run": False}}) is False


def test_is_manual_run_state_manual_origin():
    """Test when run_origin is 'manual'."""
    assert _is_manual_run_state({"run_classification": {"run_origin": "manual"}}) is True
    assert _is_manual_run_state({"run_classification": {"run_origin": "manual", "manual_run": False}}) is True


def test_is_manual_run_state_manual_run_true():
    """Test when manual_run is True."""
    assert _is_manual_run_state({"run_classification": {"manual_run": True}}) is True
    assert _is_manual_run_state({"run_classification": {"run_origin": "scheduled", "manual_run": True}}) is True


def test_is_manual_run_state_both_manual():
    """Test when both run_origin and manual_run indicate manual."""
    assert _is_manual_run_state({"run_classification": {"run_origin": "manual", "manual_run": True}}) is True


# Codex review on PR #420 ("Retain blocked artifacts that contain submitted
# orders"): morning/midday's _blocked_live_result() can now write
# status="blocked" while buy_candidates/broker_outcomes still carry a real
# earlier submission -- that evidence must not disappear from the weekly
# order/notional metrics just because the run as a whole is flagged blocked.
def test_has_submitted_evidence_empty_blocked_stub():
    assert _has_submitted_evidence({"status": "blocked"}) is False
    assert _has_submitted_evidence({"status": "blocked", "buy_candidates": [], "broker_outcomes": {}}) is False


def test_has_submitted_evidence_from_buy_candidates():
    data = {"status": "blocked", "buy_candidates": [{"ticker": "AAA", "status": "submitted"}]}
    assert _has_submitted_evidence(data) is True


def test_has_submitted_evidence_from_broker_outcomes():
    data = {"status": "blocked", "broker_outcomes": {"AAA": {"status": "submitted"}}}
    assert _has_submitted_evidence(data) is True


def test_load_json_files_keeps_blocked_run_with_submitted_evidence(tmp_path):
    empty_stub = tmp_path / "empty_stub.json"
    empty_stub.write_text(json.dumps({"status": "blocked", "trading_day": "2026-08-10"}), encoding="utf-8")
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted"}],
            }
        ),
        encoding="utf-8",
    )
    clean_run = tmp_path / "clean_run.json"
    clean_run.write_text(json.dumps({"status": "ok", "trading_day": "2026-08-12"}), encoding="utf-8")

    loaded = load_json_files([empty_stub, partial_block, clean_run])
    trading_days = {d["trading_day"] for d in loaded}
    assert trading_days == {"2026-08-11", "2026-08-12"}


def test_load_json_files_filters_manual_run_flag_and_accepts_str_paths(tmp_path: Path):
    """Test that load_json_files filters manual_run-only states and accepts str paths."""
    valid_file = tmp_path / "valid.json"
    valid_file.write_text(json.dumps({"status": "completed", "id": "valid1"}))

    manual_file = tmp_path / "manual.json"
    manual_file.write_text(json.dumps({"run_classification": {"manual_run": True}, "id": "manual1"}))

    loaded = load_json_files([str(valid_file), str(manual_file)])

    assert len(loaded) == 1
    assert loaded[0]["id"] == "valid1"

def test_load_scheduled_run_states_missing_dir(tmp_path: Path):
    """Test when scheduled_runs directory does not exist."""
    states = load_scheduled_run_states(tmp_path)
    assert states == []


def test_load_scheduled_run_states_empty_dir(tmp_path: Path):
    """Test when scheduled_runs directory exists but is empty."""
    (tmp_path / "scheduled_runs").mkdir()
    states = load_scheduled_run_states(tmp_path)
    assert states == []


def test_load_scheduled_run_states_valid(tmp_path: Path):
    """Test loading valid scheduled run states."""
    scheduled_dir = tmp_path / "scheduled_runs"
    scheduled_dir.mkdir()

    # Valid run state
    run1 = {"id": "1", "status": "completed"}
    (scheduled_dir / "run_state_1.json").write_text(json.dumps(run1))

    # Another valid run state in a subdirectory
    sub_dir = scheduled_dir / "2023-10-01"
    sub_dir.mkdir()
    run2 = {"id": "2", "status": "completed", "run_classification": {"run_origin": "scheduled"}}
    (sub_dir / "run_state_2.json").write_text(json.dumps(run2))

    states = load_scheduled_run_states(tmp_path)

    # Should load both; load_scheduled_run_states sorts paths so 2023-10-01/run_state_2.json
    # comes before run_state_1.json.
    assert len(states) == 2
    assert states[0]["id"] == "2"
    assert states[1]["id"] == "1"


def test_load_scheduled_run_states_filtering(tmp_path: Path):
    """Test that blocked and manual run states are filtered out."""
    scheduled_dir = tmp_path / "scheduled_runs"
    scheduled_dir.mkdir()

    # Blocked run state
    blocked_run = {"id": "1", "status": "blocked"}
    (scheduled_dir / "run_state_1.json").write_text(json.dumps(blocked_run))

    # Manual run state
    manual_run = {"id": "2", "run_classification": {"run_origin": "manual"}}
    (scheduled_dir / "run_state_2.json").write_text(json.dumps(manual_run))

    # Valid run state
    valid_run = {"id": "3", "status": "completed"}
    (scheduled_dir / "run_state_3.json").write_text(json.dumps(valid_run))

    states = load_scheduled_run_states(tmp_path)
    assert len(states) == 1
    assert states[0]["id"] == "3"


def test_load_scheduled_run_states_ignores_manual_runs_dir(tmp_path: Path):
    """Test that files in manual_runs directory are ignored."""
    scheduled_dir = tmp_path / "scheduled_runs"
    scheduled_dir.mkdir()
    manual_dir = tmp_path / "manual_runs"
    manual_dir.mkdir()

    valid_scheduled = {"id": "1", "status": "completed"}
    (scheduled_dir / "run_state_1.json").write_text(json.dumps(valid_scheduled))

    valid_manual_file = {"id": "2", "status": "completed"}
    (manual_dir / "run_state_2.json").write_text(json.dumps(valid_manual_file))

    states = load_scheduled_run_states(tmp_path)
    assert len(states) == 1
    assert states[0]["id"] == "1"


def test_load_scheduled_run_states_ignores_nested_manual_runs_dir(tmp_path: Path):
    """Test that manual_runs is ignored even when nested under a date directory
    alongside a sibling scheduled_runs tree."""
    manual_dir = tmp_path / "manual_runs" / "some_date"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state_1.json").write_text(json.dumps({"status": "success", "id": 1}))

    sched_dir = tmp_path / "scheduled_runs" / "some_date"
    sched_dir.mkdir(parents=True)
    (sched_dir / "run_state_2.json").write_text(json.dumps({"status": "success", "id": 2}))

    results = load_scheduled_run_states(tmp_path)
    assert len(results) == 1
    assert results[0]["id"] == 2


def test_load_scheduled_run_states_filters_blocked(tmp_path: Path):
    """Test that blocked run states are filtered out."""
    sched_dir = tmp_path / "scheduled_runs" / "some_date"
    sched_dir.mkdir(parents=True)

    (sched_dir / "run_state_1.json").write_text(json.dumps({"status": "blocked", "id": 1}))
    (sched_dir / "run_state_2.json").write_text(json.dumps({"status": "success", "id": 2}))

    results = load_scheduled_run_states(tmp_path)
    assert len(results) == 1
    assert results[0]["id"] == 2


def test_load_scheduled_run_states_filters_manual_classification(tmp_path: Path):
    """Test that manually classified run states in scheduled_runs are filtered out."""
    sched_dir = tmp_path / "scheduled_runs" / "some_date"
    sched_dir.mkdir(parents=True)

    (sched_dir / "run_state_1.json").write_text(json.dumps({
        "status": "success",
        "id": 1,
        "run_classification": {"manual_run": True}
    }))
    (sched_dir / "run_state_2.json").write_text(json.dumps({
        "status": "success",
        "id": 2
    }))

    results = load_scheduled_run_states(tmp_path)
    assert len(results) == 1
    assert results[0]["id"] == 2


def test_load_scheduled_run_states_valid_files(tmp_path: Path):
    """Test loading multiple valid run states from scheduled_runs tree."""
    sched_dir_1 = tmp_path / "scheduled_runs" / "date1"
    sched_dir_1.mkdir(parents=True)
    sched_dir_2 = tmp_path / "scheduled_runs" / "date2"
    sched_dir_2.mkdir(parents=True)

    (sched_dir_1 / "run_state_abc.json").write_text(json.dumps({"id": 1, "status": "success"}))
    (sched_dir_1 / "run_state_def.json").write_text(json.dumps({"id": 2, "status": "success"}))
    (sched_dir_2 / "run_state_xyz.json").write_text(json.dumps({"id": 3, "status": "success"}))

    # Also add a file that shouldn't match the glob (e.g. latest_run_summary.json)
    (sched_dir_1 / "latest_run_summary.json").write_text(json.dumps({"id": 999}))

    results = load_scheduled_run_states(tmp_path)

    # Files should be sorted by path
    assert len(results) == 3
    assert [r["id"] for r in results] == [1, 2, 3]


def test_load_scheduled_run_states_corrupt_json(tmp_path: Path):
    """Test that a corrupt JSON file raises an exception."""
    scheduled_dir = tmp_path / "scheduled_runs"
    scheduled_dir.mkdir()
    (scheduled_dir / "run_state_corrupt.json").write_text("{invalid json")

    with pytest.raises(json.JSONDecodeError):
        load_scheduled_run_states(tmp_path)


def test_load_scheduled_run_states_unreadable_file(tmp_path: Path):
    """Test that an unreadable file raises an OSError.

    A directory is created where a file is expected: unlike os.chmod(path, 0),
    this deterministically raises OSError on read regardless of platform or
    whether the test runs as root (both make chmod-based denial a no-op).
    """
    scheduled_dir = tmp_path / "scheduled_runs"
    scheduled_dir.mkdir()
    (scheduled_dir / "run_state_unreadable.json").mkdir()

    with pytest.raises(OSError):
        load_scheduled_run_states(tmp_path)
