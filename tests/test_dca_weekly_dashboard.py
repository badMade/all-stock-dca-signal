"""Tests for the DCA weekly dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dca_weekly_dashboard import (
    _is_manual_run_state,
    build_graded_vs_fixed,
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


def _blocked_state(**overrides):
    """A retained partial run_state: blocked overall, but carrying real
    placed-order evidence in broker_outcomes."""
    state = {
        "status": "blocked",
        "trading_day": "2026-07-24",
        "generated_at": "2026-07-24T15:30:00-04:00",
        "run_classification": {"run_origin": "scheduled", "manual_run": False},
        "budget": {"base_amount_usd": 2.0},
        "counts": {},
        "buy_candidates": [],
        "broker_outcomes": {},
    }
    state.update(overrides)
    return state


def _load_one(tmp_path: Path, state, name="run_state.json"):
    path = tmp_path / name
    path.write_text(json.dumps(state))
    loaded = load_json_files([path])
    assert len(loaded) == 1, "the retained partial run must survive the blocked filter"
    return loaded[0]


def test_blocked_state_derives_metrics_from_broker_outcomes_alone(tmp_path: Path):
    # A retained partial run whose only evidence lives in broker_outcomes --
    # its buy_candidates never got written before the run blocked. counts and
    # budget.day_notional_usd, which build_performance_snapshot() reads
    # directly off the artifact, must be re-derived from that evidence, and a
    # synthetic candidate row published so the graded/sector aggregators (which
    # only iterate buy_candidates) see the real order at all.
    loaded = _load_one(
        tmp_path,
        _blocked_state(
            broker_outcomes={
                "AAPL": {"status": "submitted", "order_id": "O1", "submitted_amount_usd": 4.0},
                # "pending" is a placed/in-flight status too, per
                # scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES.
                "MSFT": {"status": "pending", "order_id": "O2", "submitted_amount_usd": 2.0},
                # Never reached placement -- must contribute nothing.
                "NVDA": {"status": "not_submitted_confirmation_required", "submitted_amount_usd": 9.0},
            }
        ),
    )

    assert loaded["counts"]["submitted"] == 2
    assert loaded["budget"]["day_notional_usd"] == pytest.approx(6.0)
    rows = {row["ticker"]: row for row in loaded["buy_candidates"]}
    assert set(rows) == {"AAPL", "MSFT"}
    assert rows["AAPL"]["planned_amount_usd"] == pytest.approx(4.0)
    assert rows["MSFT"]["order_id"] == "O2"


def test_blocked_state_keeps_both_orders_of_a_same_ticker_retry(tmp_path: Path):
    # Codex review, PR #593 discussion_r3840290875 ("Preserve amounts for
    # multiple same-ticker candidates"): a same-day retry can leave TWO placed
    # orders for one ticker. Keying every amount by the bare ticker let the
    # second silently overwrite the first; the first row keeps the bare ticker
    # and the second takes a "<ticker>:<order_id>" composite identity instead,
    # so both notionals survive into day_notional_usd.
    loaded = _load_one(
        tmp_path,
        _blocked_state(
            broker_outcomes={
                "AAPL": {"status": "submitted", "order_id": "O1", "submitted_amount_usd": 4.0},
                "AAPL:O2": {
                    "ticker": "AAPL",
                    "status": "submitted",
                    "order_id": "O2",
                    "submitted_amount_usd": 3.0,
                },
            }
        ),
    )

    assert loaded["counts"]["submitted"] == 2
    assert loaded["budget"]["day_notional_usd"] == pytest.approx(7.0)


def test_blocked_state_does_not_count_a_settlement_adjustment_as_an_order(tmp_path: Path):
    # Codex review, PR #593 discussion_r3840229906 ("Exclude settlement
    # adjustments from normalized order counts"): a
    # whole_share_settlement_adjustment is a dollar correction against an order
    # already counted elsewhere, not a fresh placement. Its delta still moves
    # day notional; it must not add to counts.submitted or counts.buy_candidates.
    loaded = _load_one(
        tmp_path,
        _blocked_state(
            broker_outcomes={
                "BYDDY": {"status": "submitted", "order_id": "O1", "submitted_amount_usd": 100.0},
                "BYDDY:adj": {
                    "ticker": "BYDDY",
                    "status": "submitted",
                    "order_id": "O1",
                    "whole_share_settlement_adjustment": True,
                    "submitted_amount_usd": -2.0,
                },
            }
        ),
    )

    assert loaded["counts"]["submitted"] == 1
    assert loaded["counts"]["buy_candidates"] == 1
    assert loaded["budget"]["day_notional_usd"] == pytest.approx(98.0)


def test_blocked_state_reconciles_an_outcome_with_its_existing_candidate_row(tmp_path: Path):
    # An outcome that matches a candidate row already present must reconcile
    # onto it -- backfilling the notional the row lacks -- rather than being
    # published a SECOND time as a synthetic row and double-counting the order.
    loaded = _load_one(
        tmp_path,
        _blocked_state(
            buy_candidates=[{"ticker": "AAPL", "status": "submitted", "order_id": "O1"}],
            broker_outcomes={
                "AAPL": {"status": "submitted", "order_id": "O1", "submitted_amount_usd": 4.0},
            },
        ),
    )

    assert len(loaded["buy_candidates"]) == 1
    assert loaded["buy_candidates"][0]["planned_amount_usd"] == pytest.approx(4.0)
    assert loaded["counts"]["submitted"] == 1
    assert loaded["budget"]["day_notional_usd"] == pytest.approx(4.0)


def test_blocked_state_folds_an_id_less_outcome_onto_its_id_less_candidate(tmp_path: Path):
    # Neither side carries an order id (the broker response omitted it), so the
    # outcome has to be matched to the candidate row by ticker alone. Folding
    # them together keeps ONE order on the books; treating them as two would
    # report double the capital that actually moved.
    loaded = _load_one(
        tmp_path,
        _blocked_state(
            buy_candidates=[{"ticker": "AAPL", "status": "submitted"}],
            broker_outcomes={"AAPL": {"status": "submitted", "submitted_amount_usd": 4.0}},
        ),
    )

    assert len(loaded["buy_candidates"]) == 1
    assert loaded["counts"]["submitted"] == 1
    assert loaded["budget"]["day_notional_usd"] == pytest.approx(4.0)


def test_blocked_state_without_placed_evidence_is_still_skipped(tmp_path: Path):
    # The retention above applies only to a blocked run with REAL placed
    # evidence. A date-gated blocked stub is not a run day at all and must stay
    # out of the weekly denominators.
    path = tmp_path / "stub.json"
    path.write_text(json.dumps(_blocked_state(broker_outcomes={"AAPL": {"status": "not_submitted"}})))

    assert load_json_files([path]) == []
