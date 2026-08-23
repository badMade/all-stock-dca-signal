"""Tests for the DCA weekly dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dca_weekly_dashboard import (
    _has_submitted_evidence,
    _is_manual_run_state,
    _this_run_submitted_notional,
    build_budget_reserve_effectiveness,
    build_graded_vs_fixed,
    build_performance_snapshot,
    build_weekly_dashboard,
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


def test_build_budget_reserve_effectiveness_normalizes_partial_submission_artifact():
    # Codex review, PR #538 discussion_r3837630719 ("Normalize partial runs
    # for the budget table"): build_performance_snapshot() was already fixed
    # to read a retained blocked/partial-submission artifact's real spend
    # via orders_submitted_count/hypothetical_day_spend_usd (see
    # test_build_performance_snapshot_counts_partial_submission_artifact),
    # but build_budget_reserve_effectiveness() -- the BUDGET TABLE section --
    # still read only the ordinary budget.day_notional_usd, which such an
    # artifact never populates, and reported $0.00 spent for a day with a
    # real submitted order.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "budget": {
                "hypothetical_day_spend_usd": 3.5,
                "daily_cap_usd": 10.0,
                "remaining_spend_today_usd": 6.5,
            },
            "orders_submitted_count": 1,
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 3.5},
                {"ticker": "BBB", "status": "not_submitted"},
            ],
            "broker_outcomes": {"AAA": {"status": "submitted"}},
        }
    ]
    report = build_budget_reserve_effectiveness(run_states)
    row = next(line for line in report.splitlines() if line.startswith("| 2026-07-24"))
    spent_cell = row.split("|")[3].strip()
    assert spent_cell == "$3.50"


def test_build_budget_reserve_effectiveness_prefers_ordinary_completed_run_field():
    # A normal, fully completed run still reports through the ordinary
    # budget.day_notional_usd field -- the submitted-rows fallback must only
    # kick in when that field is actually absent, not override it.
    run_states = [
        {
            "trading_day": "2026-07-24",
            "budget": {"day_notional_usd": 5.0, "daily_cap_usd": 10.0},
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 999.0}],
        }
    ]
    report = build_budget_reserve_effectiveness(run_states)
    row = next(line for line in report.splitlines() if line.startswith("| 2026-07-24"))
    spent_cell = row.split("|")[3].strip()
    assert spent_cell == "$5.00"


def test_build_budget_reserve_effectiveness_normalizes_morning_stream_cap_fields():
    # Codex review, PR #538 ("Normalize morning and midday cap fields in
    # retained runs"): a retained morning/midday/market_open/intraday
    # artifact carries its cap and remaining balance under its OWN
    # stream-specific keys (morning_daily_cap_usd, remaining_daily_usd),
    # never the generic daily_cap_usd/remaining_spend_today_usd -- without
    # falling back to those, the budget table rendered $0.00 daily cap and
    # $0.00 remaining for a real partial run.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "budget": {
                "hypothetical_day_spend_usd": 3.5,
                "morning_daily_cap_usd": 12.0,
                "remaining_daily_usd": 8.5,
            },
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 3.5}],
        }
    ]
    report = build_budget_reserve_effectiveness(run_states)
    row = next(line for line in report.splitlines() if line.startswith("| 2026-07-24"))
    cells = [c.strip() for c in row.split("|")]
    assert cells[4] == "$12.00"  # Daily cap $
    assert cells[6] == "$8.50"  # Remaining $


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


def test_build_performance_snapshot_normalizes_morning_stream_weekly_fields():
    # Codex review, PR #538 ("Normalize morning and midday cap fields in
    # retained runs"): a retained morning/midday/market_open/intraday
    # artifact's cumulative weekly planned spend and weekly cap live under
    # hypothetical_week_spend_usd / morning_weekly_cap_usd (or the sibling
    # midday/market_open/intraday key), never the generic
    # week_notional_usd/weekly_cap_usd -- without falling back to those, the
    # snapshot rendered "$0.00 / $0.00" for a real partial run.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "budget": {
                "hypothetical_day_spend_usd": 3.5,
                "hypothetical_week_spend_usd": 17.5,
                "morning_weekly_cap_usd": 60.0,
            },
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 3.5}],
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Week notional / cap | $17.50 / $60.00 |" in report


def test_build_weekly_dashboard_succeeds_with_ladder_only_base_amount():
    # Codex review, PR #538 discussion_r3837514831 ("Normalize the base amount
    # for retained partial runs"): a retained blocked/partial-submission
    # morning/midday artifact records the ladder base amount at
    # ladder.base_amount_usd, not budget.base_amount_usd/base_buy_amount_usd.
    # Before the fix, _require_base_amount() raised ValueError for such an
    # artifact and build_weekly_dashboard()/build_graded_vs_fixed() aborted
    # the entire weekly dashboard build instead of just this one section.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "counts": {"buy_candidates": 1},
            "ladder": {"base_amount_usd": 2.0},
            "budget": {
                "hypothetical_day_spend_usd": 4.0,
                "week_notional_usd": 4.0,
                "weekly_cap_usd": 100.0,
            },
            "orders_submitted_count": 1,
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0},
            ],
            "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 4.0}},
        }
    ]
    # Must not raise -- this is the regression: previously ValueError aborted
    # the whole dashboard build.
    report = build_weekly_dashboard(run_states)
    assert "$4.00" in report
    graded_section = build_graded_vs_fixed(run_states)
    assert "$4.00" in graded_section


def test_build_performance_snapshot_sums_own_submitted_amount_not_cumulative():
    # Codex review, PR #538 discussion_r3837514830 ("Derive partial-run
    # notional from submitted rows"): budget.hypothetical_day_spend_usd is
    # ledger.day_planned(trading_day) -- a CUMULATIVE running total for the
    # whole trading day, seeded from prior same-day allocations by
    # _compute_top_up(). Simulate an earlier and a later same-day partial run,
    # each with their own submitted amount; the later run's
    # hypothetical_day_spend_usd includes the earlier run's amount too. The
    # aggregated notional must equal the sum of each run's OWN submitted
    # amount ($2 + $3 = $5), not double-count the earlier run's $2 by also
    # reading the later run's cumulative $5 on top of the earlier run's own $2
    # (which would total $7).
    earlier_run = {
        "status": "blocked",
        "trading_day": "2026-07-24",
        "counts": {"buy_candidates": 1},
        "budget": {
            "hypothetical_day_spend_usd": 2.0,
            "week_notional_usd": 2.0,
            "weekly_cap_usd": 100.0,
        },
        "orders_submitted_count": 1,
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 2.0},
        ],
        "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 2.0}},
    }
    later_run = {
        "status": "blocked",
        "trading_day": "2026-07-24",
        "counts": {"buy_candidates": 1},
        "budget": {
            # Cumulative day total: the earlier run's $2 plus this run's own $3.
            "hypothetical_day_spend_usd": 5.0,
            "week_notional_usd": 5.0,
            "weekly_cap_usd": 100.0,
        },
        "orders_submitted_count": 1,
        "buy_candidates": [
            {"ticker": "BBB", "status": "submitted", "planned_amount_usd": 3.0},
        ],
        "broker_outcomes": {"BBB": {"status": "submitted", "submitted_amount_usd": 3.0}},
    }
    report = build_performance_snapshot([earlier_run, later_run])
    assert "| Orders submitted | 2 |" in report
    assert "| Executed notional | $5.00 |" in report


def test_build_performance_snapshot_normalizes_weekly_fields_for_morning_artifact():
    # Codex review, PR #654 discussion_r3837678475 ("Normalize weekly fields
    # for retained morning artifacts"): a retained blocked/partial-submission
    # morning/midday artifact never populates budget.week_notional_usd/
    # weekly_cap_usd -- MorningDCAExecutor.build_artifacts() instead writes
    # the week's cumulative spend at budget.hypothetical_week_spend_usd and
    # the cap under a stream-specific key (morning_weekly_cap_usd here), so
    # the snapshot previously reported "$0.00 / $0.00" for the week on
    # exactly these partial runs.
    run_state = {
        "status": "blocked",
        "trading_day": "2026-07-24",
        "counts": {"buy_candidates": 1},
        "budget": {
            "hypothetical_day_spend_usd": 2.0,
            "hypothetical_week_spend_usd": 12.5,
            "morning_weekly_cap_usd": 50.0,
        },
        "orders_submitted_count": 1,
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 2.0},
        ],
        "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 2.0}},
    }
    report = build_performance_snapshot([run_state])
    assert "| Week notional / cap | $12.50 / $50.00 |" in report


def test_build_performance_snapshot_prefers_ordinary_weekly_fields_when_present():
    # The ordinary week_notional_usd/weekly_cap_usd fields must still take
    # precedence over the stream-specific fallback when both are present.
    run_state = {
        "status": "completed",
        "trading_day": "2026-07-24",
        "counts": {"buy_candidates": 1, "submitted": 1},
        "budget": {
            "day_notional_usd": 2.0,
            "week_notional_usd": 8.0,
            "weekly_cap_usd": 40.0,
            # If the fallback were consulted instead, it would report a
            # different (wrong) value than the ordinary fields above.
            "hypothetical_week_spend_usd": 99.0,
            "morning_weekly_cap_usd": 999.0,
        },
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 2.0},
        ],
    }
    report = build_performance_snapshot([run_state])
    assert "| Week notional / cap | $8.00 / $40.00 |" in report


def test_this_run_submitted_notional_falls_back_when_candidate_amount_is_absent():
    # Codex review, PR #538 ("Fall back when the candidate notional is
    # absent"): a submitted buy_candidates row with a missing/non-numeric
    # planned_amount_usd made _num() add 0.0 AND marked the ticker "seen",
    # skipping the broker_outcomes fallback below even though THAT entry
    # carries the real submitted_amount_usd -- silently reporting a real
    # order as zero executed notional.
    run_state = {
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted"},  # planned_amount_usd absent
        ],
        "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 2.5}},
    }
    assert _this_run_submitted_notional(run_state) == 2.5


def test_this_run_submitted_notional_prefers_candidate_amount_when_present():
    # A genuinely present (even zero) planned_amount_usd on the candidate row
    # must still take precedence over broker_outcomes -- the fallback exists
    # only for a MISSING/invalid candidate amount, not to override a valid one.
    run_state = {
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 1.5},
        ],
        "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 9.99}},
    }
    assert _this_run_submitted_notional(run_state) == 1.5


def test_this_run_submitted_notional_recognizes_every_placed_order_status():
    # Codex review, PR #538 ("Include every placed status in submitted
    # notional"): _has_submitted_evidence() now admits "filled"/"pending"/
    # "unconfirmed" rows, but this strict status == "submitted" check still
    # ignored their amount -- reporting $0 executed/spent for a real order.
    for status in ("filled", "pending", "unconfirmed"):
        candidate_run_state = {
            "buy_candidates": [{"ticker": "AAA", "status": status, "planned_amount_usd": 3.0}],
        }
        assert _this_run_submitted_notional(candidate_run_state) == 3.0, status
        outcome_run_state = {
            "broker_outcomes": {"AAA": {"status": status, "submitted_amount_usd": 4.0}},
        }
        assert _this_run_submitted_notional(outcome_run_state) == 4.0, status


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


# Codex review on PR #538 ("Retain submitted rows in compute-only partial
# failures"): a compute-only terminal state can be TRUE for a run that placed
# one candidate but left a different positive-planned candidate unplaced --
# compute_only describes overall completion validity, not whether a specific
# row's placement is trustworthy. The placed row's real capital deployment
# must still surface here regardless of the artifact_contract_failure marker
# (compute_only True or False, or the key absent entirely on a legacy
# artifact).
def test_has_submitted_evidence_survives_compute_only_contract_failure():
    data = {
        "status": "blocked",
        "buy_candidates": [{"ticker": "AAA", "status": "submitted"}],
        "artifact_contract_failure": {"failure": "artifact_contract_failure", "compute_only": True},
    }
    assert _has_submitted_evidence(data) is True


def test_has_submitted_evidence_survives_companion_artifact_failure():
    data = {
        "status": "blocked",
        "buy_candidates": [{"ticker": "AAA", "status": "submitted"}],
        "artifact_contract_failure": {"failure": "artifact_contract_failure", "compute_only": False},
    }
    assert _has_submitted_evidence(data) is True


def test_has_submitted_evidence_recognizes_every_placed_order_status():
    # Codex review, PR #538 ("Recognize every placed-order status as
    # submission evidence"): a restored same-slot retry row can carry
    # "filled"/"pending"/"unconfirmed" instead of the exact "submitted"
    # string -- all real evidence an order reached the broker, per
    # near_close_dca._derive_order_submit_status's own status set.
    for status in ("filled", "pending", "unconfirmed"):
        candidate_data = {"status": "blocked", "buy_candidates": [{"ticker": "AAA", "status": status}]}
        assert _has_submitted_evidence(candidate_data) is True, status
        outcome_data = {"status": "blocked", "broker_outcomes": {"AAA": {"status": status}}}
        assert _has_submitted_evidence(outcome_data) is True, status


def test_has_submitted_evidence_survives_legacy_contract_failure_without_flag():
    # A legacy artifact written before the compute_only flag existed carries
    # no such key at all -- a genuinely submitted row must still surface.
    data = {
        "status": "blocked",
        "buy_candidates": [{"ticker": "AAA", "status": "submitted"}],
        "artifact_contract_failure": {"failure": "artifact_contract_failure", "reason": "legacy"},
    }
    assert _has_submitted_evidence(data) is True


def test_has_submitted_evidence_still_false_without_any_placed_row():
    # A contract failure with no genuinely placed row anywhere (the actual
    # compute-only-with-nothing-submitted case) still reports no evidence --
    # not because of the contract_failure marker, but because there is
    # nothing in buy_candidates/broker_outcomes to find.
    data = {
        "status": "blocked",
        "buy_candidates": [{"ticker": "AAA", "status": "not_submitted"}],
        "artifact_contract_failure": {"failure": "artifact_contract_failure", "compute_only": True},
    }
    assert _has_submitted_evidence(data) is False


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
