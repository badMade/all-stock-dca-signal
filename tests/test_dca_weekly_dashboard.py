"""Tests for the DCA weekly dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dca_weekly_dashboard import (
    _accumulation_sort_key,
    _has_submitted_evidence,
    _is_manual_run_state,
    _this_run_submitted_notional,
    build_budget_reserve_effectiveness,
    build_graded_vs_fixed,
    build_performance_snapshot,
    build_sector_matrix,
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


def test_build_graded_vs_fixed_excludes_adjustment_rows_from_candidate_count_and_fixed_baseline():
    # Codex review ("Exclude adjustment rows from candidate analytics"): a
    # whole_share_settlement_adjustment row corrects an ALREADY-counted
    # physical candidate, not a second one -- counting it here (and giving
    # it its own fixed-baseline 1x allocation) inflates both the candidate
    # count and the fixed baseline for one physical order. Its dollar delta
    # ($2) is still real deployed capital and stays in the graded total.
    run_states = [
        {
            "trading_day": "2026-07-24",
            "budget": {"base_amount_usd": 2.0},
            "buy_candidates": [
                {"ticker": "BYDDY", "planned_amount_usd": 10.0, "selected_base_amount_usd": 2.0},
                {
                    "ticker": "BYDDY",
                    "planned_amount_usd": 2.0,
                    "whole_share_settlement_adjustment": True,
                },
            ],
        }
    ]
    report = build_graded_vs_fixed(run_states)
    assert "$12.00" in report  # graded: 10 + 2 (the adjustment's real delta)
    assert "$2.00" in report  # fixed: only the ORIGINAL candidate's own $2 base
    assert "| 2026-07-24 | 1 |" in report  # candidate count: 1, not 2


def test_build_sector_matrix_excludes_adjustment_rows_from_candidate_count():
    # Codex review ("Exclude adjustment rows from candidate analytics"):
    # same exclusion as build_graded_vs_fixed's sibling test, applied to
    # the sector matrix's own candidate/notional aggregation.
    run_states = [
        {
            "trading_day": "2026-07-24",
            "buy_candidates": [
                {"ticker": "BYDDY", "planned_amount_usd": 10.0},
                {
                    "ticker": "BYDDY",
                    "planned_amount_usd": 2.0,
                    "whole_share_settlement_adjustment": True,
                },
            ],
        }
    ]
    report = build_sector_matrix(run_states, {"BYDDY": "Consumer"})
    assert "| Consumer | 1 | $12.00 |" in report


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


def test_load_json_files_sums_distinct_orders_for_same_ticker(tmp_path: Path):
    """Codex review ("Preserve multiple same-ticker orders in retained metrics"):
    a retained blocked state can carry two DISTINCT placed orders for the same
    ticker -- a same-day retry's second order, keyed under a composite
    "<ticker>:<order-id>" broker_outcomes key per
    scripts.scheduled_artifacts.submitted_order_rows(). Deduping by bare
    ticker alone would collapse both into one and undercount both the order
    count and the notional; the primary order (bare-ticker key) must still
    reconcile with its matching buy_candidates row rather than double-count.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0}],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0},
                    "AAA:retry-order-2": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 2.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    assert len(loaded) == 1
    data = loaded[0]
    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)


def test_load_json_files_raises_stale_week_notional_to_day_floor(tmp_path: Path):
    """Codex review ("Include retained submissions in weekly notional"):
    build_performance_snapshot() reports the MAX week_notional_usd seen across
    the week's artifacts as the week-to-date executed total. A retained
    partial state can leave that field absent/stale even though real capital
    was just deployed -- a day's week-to-date total can never be less than
    that day's own executed notional, so normalization must raise (never
    lower) it to that floor.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}],
                "budget": {"week_notional_usd": 1.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    assert loaded[0]["budget"]["week_notional_usd"] == pytest.approx(5.0)


def test_load_json_files_materializes_outcome_only_submission_as_candidate(tmp_path: Path):
    """Codex review ("Materialize outcome-only submissions for dashboard
    sections"): build_graded_vs_fixed()/build_sector_matrix() consume only
    buy_candidates. A retained blocked state whose sole submission evidence
    lives in broker_outcomes (no buy_candidates row at all) would otherwise
    report executed notional in the snapshot while showing zero graded spend
    and no sector allocation for that same real order.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "broker_outcomes": {"AAA": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 4.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    assert data["buy_candidates"] == [
        {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 4.0, "planned_amount_usd": 4.0}
    ]
    assert data["counts"]["buy_candidates"] == 1
    assert data["counts"]["submitted"] == 1
    assert data["budget"]["day_notional_usd"] == pytest.approx(4.0)


def test_load_json_files_does_not_duplicate_candidate_already_present(tmp_path: Path):
    """The primary outcome must NOT get a synthetic row when a matching
    buy_candidates row for that ticker already exists (it's the same order,
    just represented twice -- adding a second row would double-count it in
    graded spend / sector notional)."""
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0}],
                "broker_outcomes": {"AAA": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    assert len(data["buy_candidates"]) == 1
    assert data["counts"]["buy_candidates"] == 1


def test_load_json_files_matches_composite_outcome_by_order_id_not_just_ticker(tmp_path: Path):
    """Codex review, PR #593 discussion_r3837437492 ("Match synthetic
    candidates by order ID"): a retained retry's recovery merge can already
    append its OWN buy_candidates row for a same-ticker composite-keyed
    outcome (e.g. _failure_candidate_row in near_close_dca.py) -- not just for
    the primary order. Matching only by ticker never covers the composite
    case (identity != ticker), so that already-represented second order got a
    THIRD synthetic row appended, inflating counts.buy_candidates and spend.
    Matching by order_id first must recognize the existing row and skip.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0, "order_id": "o-1"},
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 2.0, "order_id": "o-2"},
                ],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 1.0,
                        "order_id": "o-1",
                    },
                    "AAA:o-2": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 2.0,
                        "order_id": "o-2",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    # No synthetic THIRD row -- both orders already have a real buy_candidates
    # row, matched by order_id even though the composite outcome key differs
    # from the bare ticker.
    assert len(data["buy_candidates"]) == 2
    assert data["counts"]["buy_candidates"] == 2
    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)


def test_load_json_files_reconciles_synthetic_recovery_outcome_by_order_id(tmp_path: Path):
    """Codex review, PR #593 ("Reconcile synthetic recovery outcomes by order
    ID"): for an inverse-Martingale recovery, near_close_dca stores the order
    under a SYNTHETIC "<ticker>#imart-recovery" broker_outcomes key while
    _build_buy_candidates emits the very same order as an ordinary candidate
    row with the REAL ticker and order id. Identity came from the mapping key
    for outcomes and from the ticker for candidates, so one $5 recovery
    aggregated as TWO identities: two submitted orders and $10 of day/week
    notional. The earlier order-id check only suppressed the duplicate
    synthetic CANDIDATE ROW; the duplicate amount/count entries survived it.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "order_id": "o-imart",
                        "reason": "recovered_inverse_martingale_lease",
                    },
                ],
                "broker_outcomes": {
                    # The synthetic key near_close_dca really writes; the real
                    # ticker travels IN the outcome, as does the real order id.
                    "AAA#imart-recovery": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "order_id": "o-imart",
                        "recovered_inverse_martingale_lease": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    # One order, counted once, worth $5 -- not two orders worth $10.
    assert len(data["buy_candidates"]) == 1
    assert data["counts"]["buy_candidates"] == 1
    assert data["counts"]["submitted"] == 1
    assert data["budget"]["day_notional_usd"] == pytest.approx(5.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(5.0)


@pytest.mark.parametrize("blank", [None, ""])
def test_load_json_files_falls_back_from_a_blank_outcome_ticker(tmp_path: Path, blank):
    """Codex review, PR #593 ("Fall back from blank outcome tickers during
    normalization"): the synthetic-candidate path read the symbol with
    ``outcome.get("ticker", key)``, whose default only applies when the key is
    ABSENT. A placed outcome carrying an EXPLICIT None/"" ticker therefore
    emitted a synthetic candidate with a blank symbol, so graded spend and the
    sector matrix attributed a real AAA order to an unknown ticker -- while
    scripts.scheduled_artifacts.submitted_order_rows() canonicalised the very
    same outcome as AAA via its truthy ``outcome.get("ticker") or key``.
    """
    partial_block = tmp_path / "blank_ticker.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                # Outcome-only evidence, so the synthetic row is what carries
                # this order into the graded/sector tables.
                "buy_candidates": [],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": blank,
                        "status": "submitted",
                        "submitted_amount_usd": 6.0,
                        "order_id": "o-blank",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]

    assert data["counts"]["submitted"] == 1
    assert data["budget"]["day_notional_usd"] == pytest.approx(6.0)
    # The synthetic row names the mapping key, not a blank -- so the order is
    # attributable in graded-spend and sector reporting.
    assert len(data["buy_candidates"]) == 1
    assert data["buy_candidates"][0]["ticker"] == "AAA"


def test_load_json_files_still_separates_a_same_ticker_retry_from_its_primary(tmp_path: Path):
    """Guard for the fix above: folding by order id must apply ONLY when the
    ids actually match. A same-ticker retry's composite-keyed outcome carries
    a DIFFERENT order id from the primary, so it must still aggregate as its
    own distinct identity rather than being collapsed into the primary's.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0, "order_id": "o-1"},
                ],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 1.0,
                        "order_id": "o-1",
                    },
                    "AAA:o-2": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 2.0,
                        "order_id": "o-2",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)


def test_load_json_files_falls_back_to_ticker_when_candidate_lacks_order_id(tmp_path: Path):
    """Codex review, PR #593 discussion_r3840206567 ("Fall back to ticker
    when candidate order IDs are absent"): a retained candidate row can lack
    order_id entirely while its matching PRIMARY broker outcome carries one.
    existing_order_ids never contains the outcome's id in that case, so
    without a ticker fallback the outcome always looks unmatched and a
    duplicate synthetic candidate row gets appended, inflating counts and
    spend for one real order.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    # No order_id on this retained candidate row.
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 3.0,
                        "order_id": "o-1",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    # No duplicate synthetic row -- the existing ticker-matched candidate
    # already covers this outcome even though it carries no order_id.
    assert len(data["buy_candidates"]) == 1
    assert data["counts"]["buy_candidates"] == 1
    assert data["counts"]["submitted"] == 1
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)


def test_load_json_files_preserves_candidate_amount_when_outcome_omits_it(tmp_path: Path):
    """Codex review, PR #593 discussion_r3840206570 ("Preserve candidate
    notional when an outcome omits it"): a retained candidate already has a
    valid planned_amount_usd/submitted_amount_usd, but its matching broker
    outcome OMITS submitted_amount_usd. The amounts_by_ticker conversion must
    not overwrite the already-derived amount with 0.0 in that case.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "pending", "planned_amount_usd": 5.0},
                ],
                "broker_outcomes": {
                    # No submitted_amount_usd on the matching outcome.
                    "AAA": {"ticker": "AAA", "status": "pending"},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    # The candidate-derived $5 must survive, not get zeroed by the outcome's
    # missing submitted_amount_usd.
    assert data["budget"]["day_notional_usd"] == pytest.approx(5.0)


def test_load_json_files_sums_two_same_ticker_candidates_when_outcomes_omit_amount(tmp_path: Path):
    """Codex review, PR #593 discussion_r3840290875 ("Preserve amounts for
    multiple same-ticker candidates"): a retained retry can carry TWO placed
    buy_candidates rows for the SAME ticker, each with its own distinct
    order_id, whose matching broker_outcomes entries BOTH omit
    submitted_amount_usd. Keying the derived amounts_by_ticker dict by the
    bare ticker let the second candidate's amount silently overwrite the
    first's -- with no outcome-side submitted_amount_usd available to
    re-derive the lost value, the first candidate's notional vanished
    entirely. Both amounts must survive and sum into day/week notional.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0, "order_id": "o-1"},
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 2.0, "order_id": "o-2"},
                ],
                "broker_outcomes": {
                    # Neither outcome carries submitted_amount_usd -- the
                    # candidate-derived amounts are the only evidence.
                    "AAA": {"ticker": "AAA", "status": "submitted", "order_id": "o-1"},
                    "AAA:o-2": {"ticker": "AAA", "status": "submitted", "order_id": "o-2"},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(3.0)


def test_load_json_files_backfills_planned_amount_from_submitted_amount(tmp_path: Path):
    """Codex review, PR #593 discussion_r3840229902 ("Propagate submitted
    amounts into retained candidate rows"): a retained candidate row can carry
    submitted_amount_usd but be MISSING planned_amount_usd entirely.
    build_graded_vs_fixed()/build_sector_matrix() read planned_amount_usd
    directly off the candidate row (not off load_json_files' own derived
    amounts_by_ticker/day_notional_usd), so without backfilling it that same
    real order rendered as $0 graded/sector spend even though the
    performance snapshot shows its full amount.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "budget": {"base_amount_usd": 2.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    assert data["buy_candidates"][0]["planned_amount_usd"] == pytest.approx(7.0)
    # And the aggregate snapshot figure agrees with the now-backfilled row.
    assert data["budget"]["day_notional_usd"] == pytest.approx(7.0)

    report = build_graded_vs_fixed(loaded)
    assert "$7.00" in report


def test_load_json_files_backfills_a_matched_candidate_from_its_outcome_amount(tmp_path: Path):
    """Codex review, PR #593 ("Backfill matched candidates from outcome
    amounts"): a retained blocked candidate row can lack BOTH amount fields
    while its matching broker outcome carries submitted_amount_usd -- the
    id-less pending candidate matched to an ID-bearing $7 outcome. Only
    amounts_by_ticker was updated in that case, and because the matched
    candidate suppresses synthetic-row creation, the row's planned_amount_usd
    stayed absent: the snapshot reported $7 executed while planned notional,
    graded spend and sector spend all reported $0.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "budget": {"base_amount_usd": 2.0},
                "buy_candidates": [
                    # Neither submitted_amount_usd nor planned_amount_usd.
                    {"ticker": "AAA", "status": "pending"},
                ],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": "AAA",
                        "status": "pending",
                        "order_id": "o-1",
                        "submitted_amount_usd": 7.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    # Still one candidate row for one order (the match suppressed a synthetic
    # duplicate) -- and it now carries the outcome's amount.
    assert len(data["buy_candidates"]) == 1
    assert data["buy_candidates"][0]["planned_amount_usd"] == pytest.approx(7.0)
    assert data["budget"]["day_notional_usd"] == pytest.approx(7.0)
    # The row-reading sections agree with the snapshot instead of showing $0.
    assert "$7.00" in build_graded_vs_fixed(loaded)
    snapshot = build_performance_snapshot(loaded)
    assert "$7.00" in snapshot


def test_load_json_files_keeps_a_reported_cumulative_week_total(tmp_path: Path):
    """Codex review, PR #593 ("Preserve the reported cumulative week total
    when normalizing"): a retained morning-family state can carry its own
    cumulative week-to-date under budget.hypothetical_week_spend_usd while the
    caller loaded none of the earlier states that produced it. Deriving
    week_notional_usd from the running total plus this row's orders alone
    wrote a SMALLER number -- and because week_notional_usd is written here,
    that smaller value then shadowed the reported $100 in both the accumulator
    and build_performance_snapshot()'s own fallback.
    """
    blocked = tmp_path / "blocked.json"
    blocked.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-12",
                "budget": {"hypothetical_week_spend_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([blocked])
    data = loaded[0]
    # The known cumulative is the floor; today's own $5 never lowers it.
    assert data["budget"]["week_notional_usd"] == pytest.approx(100.0)
    assert data["budget"]["hypothetical_week_spend_usd"] == pytest.approx(100.0)
    assert "$100.00" in build_performance_snapshot(loaded)

    # ...and a later state in the same week still accumulates ON TOP of it
    # rather than restarting from this state's own day total.
    later = tmp_path / "later.json"
    later.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-13",
                "buy_candidates": [
                    {"ticker": "BBB", "status": "submitted", "submitted_amount_usd": 5.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_json_files([blocked, later])
    thursday = next(d for d in pair if d["trading_day"] == "2026-08-13")
    assert thursday["budget"]["week_notional_usd"] == pytest.approx(105.0)


def test_load_json_files_excludes_settlement_adjustment_from_counts_but_keeps_notional(
    tmp_path: Path,
):
    """Codex review, PR #593 discussion_r3840229906 ("Exclude settlement
    adjustments from normalized order counts"): a whole_share_settlement_
    adjustment row is a bookkeeping-only dollar correction for an order
    ALREADY counted elsewhere, not a fresh distinct placement -- it must not
    inflate counts.buy_candidates/counts.submitted, but its dollar delta must
    still land in day_notional_usd/week_notional_usd.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": 2.0,
                        "submitted_amount_usd": 2.0,
                        "whole_share_settlement_adjustment": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    assert data["counts"]["buy_candidates"] == 0
    assert data["counts"]["submitted"] == 0
    # The dollar correction itself must still be reflected in day notional.
    assert data["budget"]["day_notional_usd"] == pytest.approx(2.0)


def test_load_json_files_resets_running_week_notional_at_week_boundary(tmp_path: Path):
    """Codex review, PR #593 discussion_r3840206566 ("Reset accumulated
    notional at ISO-week boundaries"): load_scheduled_run_states() loads the
    COMPLETE historical scheduled_runs/** tree, so the running weekly-notional
    accumulator can span multiple ISO weeks. A prior week's $100 total must
    not bleed into a new week's own $5 retained partial submission.
    """
    # 2026-08-10 is a Monday in ISO week (2026, 33); 2026-08-17 is a Monday
    # in the NEXT ISO week (2026, 34).
    prior_week = tmp_path / "prior_week.json"
    prior_week.write_text(
        json.dumps(
            {
                "status": "ok",
                "trading_day": "2026-08-10",
                "budget": {"week_notional_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )
    new_week = tmp_path / "new_week.json"
    new_week.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-17",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([prior_week, new_week])
    new = next(d for d in loaded if d["trading_day"] == "2026-08-17")
    # Must be just this new week's own $5, not $100 (prior week) + $5.
    assert new["budget"]["week_notional_usd"] == pytest.approx(5.0)


def test_load_json_files_keeps_higher_existing_week_notional(tmp_path: Path):
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 1.0}],
                "budget": {"week_notional_usd": 20.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    assert loaded[0]["budget"]["week_notional_usd"] == pytest.approx(20.0)


def test_load_json_files_applies_negative_settlement_correction_to_week_notional(tmp_path: Path):
    """Codex review, PR #593 discussion_r3840403547 ("Apply negative
    settlement corrections to weekly notional"): a whole_share_settlement_
    adjustment row's dollar amount is a CORRECTION delta against an order
    already counted elsewhere -- not a fresh independent order total -- and
    that delta can be negative (a settlement correcting an earlier overcount
    downward). The one-way max-based accumulation must not refuse to LOWER a
    stale weekly total to reflect it: a prior $100 week-to-date plus a -$2
    settlement correction must net to $98, not stay at $100.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": -2.0,
                        "submitted_amount_usd": -2.0,
                        "whole_share_settlement_adjustment": True,
                    },
                ],
                "budget": {"week_notional_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    # The correction still lands in day notional...
    assert data["budget"]["day_notional_usd"] == pytest.approx(-2.0)
    # ...and the stale $100 weekly total is actually LOWERED to $98, not left
    # untouched by a one-way max().
    assert data["budget"]["week_notional_usd"] == pytest.approx(98.0)
    # A settlement-adjustment-only row must still not count as a fresh order.
    assert data["counts"]["buy_candidates"] == 0
    assert data["counts"]["submitted"] == 0


def test_load_json_files_excludes_a_prior_week_settlement_delta_from_this_week(tmp_path: Path):
    """Codex review, PR #593 ("Attribute settlement deltas to the claim's ISO
    week"): a Friday provisional whole-share order reconciled with a +$2
    adjustment on the following MONDAY carries the corrected day in
    whole_share_claim_breakdown[].trading_day, but normalization keyed the
    accumulator only off the artifact's own trading_day -- so the delta was
    added to Monday's week_notional_usd, over-reporting the NEW week with
    capital that belongs to the previous one.

    Chosen semantics: exclusion, not retroactive rewriting. The accumulator
    walks states in order and resets at week boundaries, and
    build_performance_snapshot() reduces week_notional_usd with a max() across
    states, so the already-emitted prior week cannot be reached from here. A
    delta belonging to a passed week is simply not applicable to the current
    week's running total. The cash movement is still visible on the day it
    settled, via day_notional_usd.
    """
    # Fri 2026-08-07 is ISO week (2026, 32); Mon 2026-08-10 is (2026, 33).
    monday_block = tmp_path / "monday_block.json"
    monday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": 2.0,
                        "submitted_amount_usd": 2.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-07", "amount_usd": 2.0},
                        ],
                    },
                ],
                "budget": {"week_notional_usd": 40.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday_block])
    data = loaded[0]
    # The correction still shows up as the day's own cash movement...
    assert data["budget"]["day_notional_usd"] == pytest.approx(2.0)
    # ...but Monday's ISO week is NOT credited with capital deployed the prior
    # week: the $40 weekly total is left exactly as it stood.
    assert data["budget"]["week_notional_usd"] == pytest.approx(40.0)
    assert data["counts"]["submitted"] == 0


def test_load_json_files_applies_a_same_week_settlement_delta_to_the_week(tmp_path: Path):
    """Counterpart to the exclusion above: an adjustment whose claim day falls
    in the artifact's OWN ISO week still applies to that week's total, exactly
    as it always did. Only cross-week attribution changed.
    """
    # Mon 2026-08-10 and Tue 2026-08-11 are both ISO week (2026, 33).
    tuesday_block = tmp_path / "tuesday_block.json"
    tuesday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": 2.0,
                        "submitted_amount_usd": 2.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-10", "amount_usd": 2.0},
                        ],
                    },
                ],
                "budget": {"week_notional_usd": 40.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([tuesday_block])
    data = loaded[0]
    assert data["budget"]["day_notional_usd"] == pytest.approx(2.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(42.0)


def test_load_json_files_excludes_a_prior_week_settlement_outcome_from_this_week(tmp_path: Path):
    """Same claim-week attribution, for a retained state whose ONLY evidence
    for the adjustment lives in broker_outcomes -- the outcome-only shape this
    normalization exists to support, which never reaches the candidate pass.
    """
    monday_block = tmp_path / "monday_outcome_block.json"
    monday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [],
                "broker_outcomes": {
                    "BYDDY": {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "submitted_amount_usd": 2.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-07", "amount_usd": 2.0},
                        ],
                    },
                },
                "budget": {"week_notional_usd": 40.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday_block])
    data = loaded[0]
    assert data["budget"]["day_notional_usd"] == pytest.approx(2.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(40.0)
    assert data["counts"]["submitted"] == 0


def test_load_json_files_splits_a_multi_week_settlement_aggregate_by_claim_week(tmp_path: Path):
    """Codex review, PR #593 ("Split aggregated adjustments by claim week"):
    near_close_dca._merge_whole_share_submission_outcomes CONCATENATES
    whole_share_claim_breakdown lists, so one adjustment row can carry a
    single summed submitted_amount_usd covering claims from several trading
    days -- and therefore several ISO weeks.

    The prior exclusion was all-or-nothing per identity: any breakdown entry
    naming the current week let the WHOLE aggregate through. A $2 Friday
    correction merged with a $1 Monday correction therefore reported $3
    against Monday's week instead of $1. Only the current week's share may
    move this week's total.
    """
    # Fri 2026-08-07 is ISO week (2026, 32); Mon 2026-08-10 is (2026, 33).
    monday_block = tmp_path / "monday_split_block.json"
    monday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": 3.0,
                        "submitted_amount_usd": 3.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-07", "amount_usd": 2.0},
                            {"trading_day": "2026-08-10", "amount_usd": 1.0},
                        ],
                    },
                ],
                "budget": {"week_notional_usd": 40.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday_block])
    data = loaded[0]
    # The whole correction is still the day's own cash movement...
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)
    # ...but only the $1 claimed for a day in Monday's OWN ISO week raises the
    # weekly total; the $2 belonging to the prior week is excluded.
    assert data["budget"]["week_notional_usd"] == pytest.approx(41.0)
    assert data["counts"]["submitted"] == 0


def test_load_json_files_splits_a_multi_week_settlement_outcome_by_claim_week(tmp_path: Path):
    """Same apportioning, for the outcome-only retained shape (no matching
    buy_candidates row) -- the split cannot live in the candidate pass alone.
    """
    monday_block = tmp_path / "monday_split_outcome_block.json"
    monday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [],
                "broker_outcomes": {
                    "BYDDY": {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "submitted_amount_usd": 3.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-07", "amount_usd": 2.0},
                            {"trading_day": "2026-08-10", "amount_usd": 1.0},
                        ],
                    },
                },
                "budget": {"week_notional_usd": 40.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday_block])
    data = loaded[0]
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(41.0)


def test_load_json_files_attributes_unaccounted_settlement_dollars_to_this_week(tmp_path: Path):
    """A breakdown that does not sum to the aggregate is the NORMAL shape for a
    row that merged a reconciled prior-day claim with this artifact's own fresh
    contribution (which contributes no breakdown entry of its own).

    Documented rule, mirroring dca_risk.deployed_usd_by_claim_date(): the
    unaccounted remainder belongs to the artifact's own week. Here $2 of the
    $3 aggregate is claimed for the prior Friday, so $1 remains and lands in
    Monday's week. An entry with a non-numeric amount_usd is skipped rather
    than guessed at, so its dollars fall into that same remainder.
    """
    monday_block = tmp_path / "monday_remainder_block.json"
    monday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": 3.0,
                        "submitted_amount_usd": 3.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-07", "amount_usd": 2.0},
                            {"trading_day": "2026-08-07", "amount_usd": "not-a-number"},
                        ],
                    },
                ],
                "budget": {"week_notional_usd": 40.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday_block])
    data = loaded[0]
    assert data["budget"]["day_notional_usd"] == pytest.approx(3.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(41.0)


def test_load_json_files_excludes_a_prior_week_claim_from_an_undated_state(tmp_path: Path):
    """Codex review, PR #593 ("Preserve claim-week attribution for undated
    states"): the claim-week split keyed off the ARTIFACT's own ISO week, and
    a retained blocked state can carry neither trading_day nor iso_week while
    still carrying a DATED whole_share_claim_breakdown. The split then saw a
    None week, took the early return, and applied the whole aggregate to the
    accumulator's last known week -- so a current-week $100 total followed by
    an undated +$2 adjustment claiming the prior Friday normalized to $102 for
    the current week, even though the dated form of that same adjustment is
    correctly excluded.

    The undated state contributes to the week the accumulator is already in,
    so that running week is what a dated claim must be measured against. The
    established semantics are unchanged: the prior week is EXCLUDED, never
    retroactively rewritten (this is a single forward pass), and the day's own
    cash movement still lands on the day.
    """
    # Fri 2026-08-07 is ISO week (2026, 32); Mon 2026-08-10 is (2026, 33).
    monday = tmp_path / "monday.json"
    monday.write_text(
        json.dumps(
            {
                "trading_day": "2026-08-10",
                "generated_at": "2026-08-10T15:30:00",
                "budget": {"week_notional_usd": 100.0, "weekly_cap_usd": 200.0},
            }
        ),
        encoding="utf-8",
    )
    undated = tmp_path / "undated_block.json"
    undated.write_text(
        json.dumps(
            {
                # No trading_day and no iso_week -- the retained shape
                # _accumulation_sort_key deliberately tails and the
                # accumulator deliberately leaves the running week alone for.
                "status": "blocked",
                "generated_at": "2026-08-11T10:30:00",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": 2.0,
                        "submitted_amount_usd": 2.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-07", "amount_usd": 2.0},
                        ],
                    },
                ],
                "budget": {"weekly_cap_usd": 200.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday, undated])
    data = loaded[1]

    # The correction still moved cash on the day it settled...
    assert data["budget"]["day_notional_usd"] == pytest.approx(2.0)
    # ...but it belongs to the prior week, so the current week stays at $100.
    assert data["budget"]["week_notional_usd"] == pytest.approx(100.0)
    assert "| Week notional / cap | $100.00 / $200.00 |" in build_performance_snapshot(loaded)

    # A SAME-week claim on an equally undated state is untouched: the running
    # week is a real comparison, not a blanket exclusion.
    same_week = tmp_path / "undated_same_week_block.json"
    same_week.write_text(
        json.dumps(
            {
                "status": "blocked",
                "generated_at": "2026-08-11T10:30:00",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": 2.0,
                        "submitted_amount_usd": 2.0,
                        "whole_share_settlement_adjustment": True,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-11", "amount_usd": 2.0},
                        ],
                    },
                ],
                "budget": {"weekly_cap_usd": 200.0},
            }
        ),
        encoding="utf-8",
    )

    assert load_json_files([monday, same_week])[1]["budget"]["week_notional_usd"] == pytest.approx(102.0)

    # And with no week known on EITHER side the aggregate still applies, as it
    # always did.
    assert load_json_files([undated])[0]["budget"]["week_notional_usd"] == pytest.approx(2.0)


def test_load_json_files_attributes_a_recovered_order_to_its_claim_week(tmp_path: Path):
    """Codex review, PR #593 ("Attribute recovered orders by their claim
    week"): the claim-week apportioning used to apply to settlement
    ADJUSTMENT identities only. An ordinary, non-adjustment whole-share
    RECOVERY -- the shape near_close_dca._book_reconciled_whole_share_order()
    publishes -- also carries whole_share_claim_breakdown naming the original
    claim day, so a Friday claim first surfaced on Monday dropped its full
    amount into Monday's week_notional_usd even though risk accounting books
    it to Friday.

    It is still a REAL order, unlike an adjustment: counts.submitted and
    day_notional_usd must be unchanged. Only the weekly attribution moves.
    """
    # Fri 2026-08-07 is ISO week (2026, 32); Mon 2026-08-10 is (2026, 33).
    monday_block = tmp_path / "monday_recovery_block.json"
    monday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "order_id": "rec-1",
                        "planned_amount_usd": 12.0,
                        "submitted_amount_usd": 12.0,
                        # No whole_share_settlement_adjustment marker: this is
                        # a plain recovered order.
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-07", "order_id": "rec-1", "amount_usd": 12.0},
                        ],
                    },
                    # Monday's OWN fresh order, so the weekly figure below is
                    # a real number rather than the raise-only floor declining
                    # to touch a zero total.
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "fresh-1",
                        "planned_amount_usd": 5.0,
                        "submitted_amount_usd": 5.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday_block])
    data = loaded[0]
    # Both physical orders are fully counted, and both cash movements stay on
    # the day they actually settled.
    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(17.0)
    # But Monday's ISO week is credited only with Monday's own $5: Friday's
    # $12 belongs to the prior week (it was $17 before this fix).
    assert data["budget"]["week_notional_usd"] == pytest.approx(5.0)


def test_load_json_files_keeps_a_same_week_recovered_order_in_the_week(tmp_path: Path):
    """Counterpart to the exclusion above: a recovery whose claim day falls in
    the artifact's OWN ISO week still raises that week's total, exactly as it
    always did. Only cross-week attribution changed.
    """
    # Mon 2026-08-10 and Tue 2026-08-11 are both ISO week (2026, 33).
    tuesday_block = tmp_path / "tuesday_recovery_block.json"
    tuesday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "order_id": "rec-2",
                        "planned_amount_usd": 12.0,
                        "submitted_amount_usd": 12.0,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-10", "order_id": "rec-2", "amount_usd": 12.0},
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([tuesday_block])
    data = loaded[0]
    assert data["counts"]["submitted"] == 1
    assert data["budget"]["day_notional_usd"] == pytest.approx(12.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(12.0)


def test_load_json_files_attributes_a_recovered_outcome_by_claim_week(tmp_path: Path):
    """The same recovered-order attribution for a retained state whose only
    evidence lives in broker_outcomes -- the outcome-only shape this
    normalization exists to support, which never reaches the candidate pass.
    The partial split also applies: only the portion of an aggregate whose
    breakdown names THIS week counts, and the unaccounted remainder (the
    artifact's own fresh contribution) stays on the artifact's own week.
    """
    monday_block = tmp_path / "monday_recovery_outcome_block.json"
    monday_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [],
                "broker_outcomes": {
                    "BYDDY": {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [
                            # Friday's claim -- prior ISO week, excluded.
                            {"trading_day": "2026-08-07", "amount_usd": 12.0},
                            # Monday's own claim -- this week, kept.
                            {"trading_day": "2026-08-10", "amount_usd": 5.0},
                            # $3 unaccounted remainder -> the artifact's week.
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday_block])
    data = loaded[0]
    assert data["counts"]["submitted"] == 1
    assert data["budget"]["day_notional_usd"] == pytest.approx(20.0)
    # $5 (Monday's own claim) + $3 (unaccounted remainder); the $12 Friday
    # claim is excluded. The whole $20 landed here before this fix.
    assert data["budget"]["week_notional_usd"] == pytest.approx(8.0)


def _idless_settlement_state(tmp_path: Path, name: str, *, adjustment_first: bool) -> Path:
    """A retained blocked artifact carrying an id-less base row AND an id-less
    whole_share_settlement_adjustment row for the SAME ticker, written in
    either physical row order."""
    base_row = {
        "ticker": "BYDDY",
        "status": "submitted",
        "planned_amount_usd": 100.0,
        "submitted_amount_usd": 100.0,
    }
    adjustment_row = {
        "ticker": "BYDDY",
        "status": "submitted",
        "planned_amount_usd": -2.0,
        "submitted_amount_usd": -2.0,
        "whole_share_settlement_adjustment": True,
    }
    rows = [adjustment_row, base_row] if adjustment_first else [base_row, adjustment_row]
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": rows,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("adjustment_first", [False, True])
def test_load_json_files_keeps_idless_settlement_adjustment_separate(tmp_path: Path, adjustment_first: bool):
    """Codex review, PR #593 discussion_r3840429731 ("Keep id-less settlement
    adjustments as separate amounts"): the composite "<ticker>:<order_id>"
    identity requires an order_id, so an id-less whole_share_settlement_
    adjustment row for the same ticker as an id-less BASE row fell back to the
    bare ticker identity and OVERWROTE the base amount -- a $100 provisional
    row followed by a -$2 correction reported day_notional_usd = -2 instead of
    $98. The adjustment must sum with the base amount (in either physical row
    order) while still being excluded from the order counts.
    """
    partial_block = _idless_settlement_state(tmp_path, "partial_block.json", adjustment_first=adjustment_first)

    loaded = load_json_files([partial_block])
    data = loaded[0]
    # $100 base + (-$2) correction, not the bare -$2 that clobbering produced.
    assert data["budget"]["day_notional_usd"] == pytest.approx(98.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(98.0)
    # The adjustment is still bookkeeping-only: exactly one real order.
    assert data["counts"]["submitted"] == 1
    assert data["counts"]["buy_candidates"] == 1


def test_load_json_files_carries_settlement_reduction_into_running_week_total(tmp_path: Path):
    """Codex review, PR #593 discussion_r3840429733 ("Carry settlement
    reductions into the running week total"): lowering the CURRENT artifact's
    week_notional_usd ($100 -> $98 after a -$2 correction) is not enough -- the
    running accumulator feeding SUBSEQUENT states in the same ISO week used a
    one-way max() and stayed at $100, so the next retained partial run's $5
    normalized to $105 instead of the correct $103.

    The Monday state is what seeds the accumulator at $100; the correction on
    Tuesday then has to LOWER it (max($100, $98) is exactly the step that used
    to throw the reduction away).
    """
    monday = tmp_path / "monday.json"
    monday.write_text(
        json.dumps(
            {
                "status": "ok",
                "trading_day": "2026-08-10",
                "budget": {"week_notional_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )
    tuesday = tmp_path / "tuesday.json"
    tuesday.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {
                        "ticker": "BYDDY",
                        "status": "submitted",
                        "planned_amount_usd": -2.0,
                        "submitted_amount_usd": -2.0,
                        "whole_share_settlement_adjustment": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    # Same ISO week (2026, 33) as the Tuesday state above, so the accumulator
    # is carried forward rather than reset.
    wednesday = tmp_path / "wednesday.json"
    wednesday.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-12",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday, tuesday, wednesday])
    tue = next(d for d in loaded if d["trading_day"] == "2026-08-11")
    wed = next(d for d in loaded if d["trading_day"] == "2026-08-12")
    assert tue["budget"]["week_notional_usd"] == pytest.approx(98.0)
    # $98 corrected week-to-date + $5, NOT the pre-correction $100 + $5.
    assert wed["budget"]["week_notional_usd"] == pytest.approx(103.0)


def test_load_json_files_adds_retained_partial_spend_to_prior_weekly_total(tmp_path: Path):
    """Codex review, PR #593 discussion_r3837437494 ("Add retained partial
    spend to the prior weekly total"): when an EARLIER state in the same
    load_json_files() call already reported a cumulative week-to-date total
    (e.g. $100 from Monday), a LATER retained/partial state's own newly
    derived day spend (e.g. $5 on Wednesday, with its own week_notional_usd
    absent) must be ADDED to that prior total -- not just floored against its
    own day_total in isolation, which would report a stale/understated $100
    (or the wrong-things-maxed $5) instead of the correct $105 once
    build_performance_snapshot() takes the max across all states.
    """
    monday = tmp_path / "monday.json"
    monday.write_text(
        json.dumps(
            {
                "status": "ok",
                "trading_day": "2026-08-10",
                "budget": {"week_notional_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )
    wednesday = tmp_path / "wednesday.json"
    wednesday.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-12",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([monday, wednesday])
    wed = next(d for d in loaded if d["trading_day"] == "2026-08-12")
    assert wed["budget"]["week_notional_usd"] == pytest.approx(105.0)


def test_load_json_files_keeps_a_differing_order_id_outcome_separate_from_its_ticker_candidate(
    tmp_path: Path,
):
    """Codex review, PR #593 ("Match ticker-only outcomes only to candidates
    without IDs"): the ticker fallback added for id-less candidate rows was
    too broad in the reverse direction. A retained blocked state can hold a
    candidate row for AAA with order id A alongside an outcome-only placement
    for AAA carrying a DIFFERENT order id B. Both ids prove the two are
    distinct orders, but the outcome reused the bare-ticker identity,
    OVERWROTE A's amount, and the ticker fallback then suppressed the
    synthetic row -- so a $5 candidate plus a $7 outcome reported ONE
    submitted order worth $7 instead of two worth $12.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-11",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0, "order_id": "o-A"},
                ],
                "broker_outcomes": {
                    # Keyed by the bare ticker (not a composite key), but
                    # carrying a different order id from the candidate row.
                    "AAA": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 7.0,
                        "order_id": "o-B",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])
    data = loaded[0]
    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(12.0)
    # The outcome-only order gets its own synthetic candidate row so
    # build_graded_vs_fixed()/build_sector_matrix() see it too.
    assert data["counts"]["buy_candidates"] == 2
    assert {row.get("order_id") for row in data["buy_candidates"]} == {"o-A", "o-B"}


def test_load_json_files_sorts_states_before_accumulating_weekly_notional(tmp_path: Path):
    """Codex review, PR #593 ("Sort states before accumulating weekly
    notional"): load_json_files() is a PUBLIC API accepting arbitrary paths,
    but its running weekly accumulator is order-dependent and only
    load_scheduled_run_states() sorts. Handed Wednesday (already reporting
    $100 week-to-date) BEFORE Monday's retained $5 submission, Monday
    normalized to $105 -- overstating the week for a day that came first.
    """
    monday = tmp_path / "monday.json"
    monday.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}],
            }
        ),
        encoding="utf-8",
    )
    wednesday = tmp_path / "wednesday.json"
    wednesday.write_text(
        json.dumps(
            {
                "status": "ok",
                "trading_day": "2026-08-12",
                "budget": {"week_notional_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )

    # Deliberately non-chronological input order.
    loaded = load_json_files([wednesday, monday])

    # The returned list keeps the caller's own order -- sorting happens on a
    # working copy for accumulation only.
    assert [d["trading_day"] for d in loaded] == ["2026-08-12", "2026-08-10"]
    mon = next(d for d in loaded if d["trading_day"] == "2026-08-10")
    assert mon["budget"]["week_notional_usd"] == pytest.approx(5.0)


def test_load_json_files_keeps_undated_states_when_sorting_for_accumulation(tmp_path: Path):
    """A state with no usable trading_day must never be dropped or crash the
    chronological sort -- it sorts after every dated state and is still
    normalized like any other retained partial run.
    """
    undated = tmp_path / "undated.json"
    undated.write_text(
        json.dumps(
            {
                "status": "blocked",
                "buy_candidates": [{"ticker": "BBB", "status": "submitted", "submitted_amount_usd": 4.0}],
            }
        ),
        encoding="utf-8",
    )
    unparseable = tmp_path / "unparseable.json"
    unparseable.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "not-a-date",
                "buy_candidates": [{"ticker": "CCC", "status": "submitted", "submitted_amount_usd": 6.0}],
            }
        ),
        encoding="utf-8",
    )
    dated = tmp_path / "dated.json"
    dated.write_text(
        json.dumps({"status": "ok", "trading_day": "2026-08-10", "budget": {"week_notional_usd": 100.0}}),
        encoding="utf-8",
    )

    loaded = load_json_files([undated, unparseable, dated])

    assert len(loaded) == 3
    assert loaded[0]["budget"]["day_notional_usd"] == pytest.approx(4.0)
    assert loaded[1]["budget"]["day_notional_usd"] == pytest.approx(6.0)


def test_load_json_files_separates_repeated_idless_candidates(tmp_path: Path):
    """Codex review, PR #593 ("Assign unique identities to repeated id-less
    candidates"): the composite "<ticker>:<order_id>" identity is only minted
    when the repeated row HAS an order_id, and the settlement branch only for
    adjustments -- so two placed rows for the same ticker that are both id-less
    and both ordinary orders (the shape a pending/unconfirmed same-day retry
    legitimately produces before either order id is known) both kept the bare
    ticker identity. The second amount overwrote the first and counts.submitted
    reported one order instead of two.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 3.0},
                    {"ticker": "AAA", "status": "unconfirmed", "submitted_amount_usd": 4.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([partial_block])[0]

    # Both in-flight orders survive: $3 + $4, not the $4 that clobbering left.
    assert data["budget"]["day_notional_usd"] == pytest.approx(7.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(7.0)
    assert data["counts"]["submitted"] == 2


def test_load_json_files_matches_an_outcome_to_a_repeated_idless_candidate(tmp_path: Path):
    """Codex review, PR #593 ("Match outcomes to repeated id-less candidates"):
    the direct consequence of sequencing repeated id-less rows. A blocked retry
    retains a candidate for AAA with order id A alongside a later
    pending/unconfirmed candidate with NO id, whose broker outcome carries id B
    under a composite key. The id-less row now holds "AAA:candidate#1" rather
    than the bare ticker, so the synthetic-row fallback (which only fires for
    identity == ticker) could not suppress B's row: B aggregated as a THIRD
    identity while the id-less row kept its own, and a $5 order plus a $7 order
    reported three submissions and $19 instead of two and $12.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "A", "submitted_amount_usd": 5.0},
                    # The retry's second order, recorded before its id was known.
                    {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                ],
                "broker_outcomes": {
                    "AAA:B": {"ticker": "AAA", "status": "pending", "order_id": "B", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([partial_block])[0]

    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(12.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(12.0)
    # ...and no synthetic row for B either: the id-less candidate row IS B, so
    # a third row would duplicate its graded/sector spend.
    assert len(data["buy_candidates"]) == 2
    assert data["counts"]["buy_candidates"] == 2


def test_load_json_files_matches_a_bare_idless_outcome_to_a_sequenced_candidate(tmp_path: Path):
    """Codex review, PR #593 ("Match bare id-less outcomes to sequenced
    candidates"): the same arithmetic as the id-bearing fold above, reached
    from the other side. The ID-BEARING $5 row is first, so it owns the bare
    "AAA" identity; the later id-less $7 row is sequenced to
    "AAA:candidate#1"; and the matching broker outcome is ALSO id-less, keyed
    bare "AAA". The pre-pass found no "AAA" entry in the pool and consumed
    nothing, so the outcome kept the bare identity and OVERWROTE the
    ID-bearing row's $5 with $7 -- $14 of day/week notional for two orders
    worth $12.
    """
    partial_block = tmp_path / "bare_idless_outcome.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "A", "submitted_amount_usd": 5.0},
                    # The retry's second order, recorded before its id was
                    # known -- and its outcome does not know it either.
                    {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([partial_block])[0]

    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(12.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(12.0)
    # The ID-bearing row keeps its own $5 rather than being rewritten to $7...
    amounts = sorted(row.get("planned_amount_usd") for row in data["buy_candidates"])
    assert amounts == [pytest.approx(5.0), pytest.approx(7.0)]
    # ...and the id-less row IS this outcome, so no synthetic third row.
    assert len(data["buy_candidates"]) == 2
    assert data["counts"]["buy_candidates"] == 2

    # Same shape, but the outcome carries an EXPLICIT blank ticker -- the
    # pre-pass resolved its symbol with a key-ABSENCE fallback, so a null/""
    # ticker reached the pool lookup as a blank, matched no ticker, and
    # consumed nothing: back to $14 (Codex review, PR #593: "Use the mapping
    # key for blank tickers in the pre-pass").
    for blank in (None, ""):
        blank_ticker = tmp_path / f"bare_idless_blank_{blank!r}.json"
        blank_ticker.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "trading_day": "2026-08-10",
                    "buy_candidates": [
                        {"ticker": "AAA", "status": "submitted", "order_id": "A", "submitted_amount_usd": 5.0},
                        {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                    ],
                    "broker_outcomes": {
                        "AAA": {"ticker": blank, "status": "pending", "submitted_amount_usd": 7.0},
                    },
                }
            ),
            encoding="utf-8",
        )
        blanked = load_json_files([blank_ticker])[0]
        assert blanked["counts"]["submitted"] == 2, blank
        assert blanked["budget"]["day_notional_usd"] == pytest.approx(12.0), blank
        assert len(blanked["buy_candidates"]) == 2, blank

    # The consumption is one entry, once: a SECOND id-less row with no
    # outcome of its own stays sequenced and keeps counting as its own order,
    # exactly as the round that sequenced repeated id-less rows made it.
    surplus_rows = tmp_path / "bare_idless_outcome_surplus.json"
    surplus_rows.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "A", "submitted_amount_usd": 5.0},
                    {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                    {"ticker": "AAA", "status": "unconfirmed", "submitted_amount_usd": 9.0},
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([surplus_rows])[0]

    assert data["counts"]["submitted"] == 3
    assert data["budget"]["day_notional_usd"] == pytest.approx(21.0)
    assert len(data["buy_candidates"]) == 3


def test_load_json_files_keeps_surplus_outcomes_and_surplus_idless_candidates(tmp_path: Path):
    """Guards for both lopsided shapes of the reconciliation above (Codex
    review, PR #593): a real order must never be dropped in either direction.

    More id-bearing outcomes than id-less rows: the surplus outcome keeps its
    own identity and gets its own synthetic candidate row. More id-less rows
    than outcomes: the surplus rows stay sequenced exactly as the previous
    round made them, so two distinct in-flight orders are still two orders.
    """
    more_outcomes = tmp_path / "more_outcomes.json"
    more_outcomes.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [{"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0}],
                "broker_outcomes": {
                    "AAA:B": {"ticker": "AAA", "status": "pending", "order_id": "B", "submitted_amount_usd": 7.0},
                    "AAA:C": {"ticker": "AAA", "status": "pending", "order_id": "C", "submitted_amount_usd": 9.0},
                },
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([more_outcomes])[0]

    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(16.0)
    assert {row.get("order_id") for row in data["buy_candidates"]} == {None, "C"}

    more_candidates = tmp_path / "more_candidates.json"
    more_candidates.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                    {"ticker": "AAA", "status": "unconfirmed", "submitted_amount_usd": 9.0},
                ],
                "broker_outcomes": {
                    "AAA:B": {"ticker": "AAA", "status": "pending", "order_id": "B", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([more_candidates])[0]

    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(16.0)
    assert len(data["buy_candidates"]) == 2

    # ...and an id-less OUTCOME still claims the id-less row it reconciles
    # with by ticker, so a composite id-bearing outcome alongside it cannot
    # swallow that same row: two orders, $16, not one.
    idless_outcome = tmp_path / "idless_outcome.json"
    idless_outcome.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [{"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0}],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 7.0},
                    "AAA:C": {"ticker": "AAA", "status": "pending", "order_id": "C", "submitted_amount_usd": 9.0},
                },
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([idless_outcome])[0]

    assert data["counts"]["submitted"] == 2
    assert data["budget"]["day_notional_usd"] == pytest.approx(16.0)


def test_load_json_files_separates_an_adjustment_outcome_from_its_base_candidate(tmp_path: Path):
    """Codex review, PR #593 ("Separate adjustment outcomes from the base order
    identity"): a physical candidate under "AAA" whose settlement correction
    exists ONLY in broker_outcomes["AAA"] gave both the bare-ticker identity,
    and the outcome assignment overwrote the base amount -- a $100 order plus a
    -$2 correction reported day_notional_usd = -2 instead of $98. The candidate
    pass already splits an id-less adjustment ROW this way; the outcome pass
    has to do the same, while keeping the adjustment out of the order counts.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 100.0},
                ],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": "AAA",
                        "status": "filled",
                        "submitted_amount_usd": -2.0,
                        "whole_share_settlement_adjustment": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    data = load_json_files([partial_block])[0]

    assert data["budget"]["day_notional_usd"] == pytest.approx(98.0)
    assert data["budget"]["week_notional_usd"] == pytest.approx(98.0)
    # The correction is bookkeeping-only: still exactly one real order.
    assert data["counts"]["submitted"] == 1
    assert data["counts"]["buy_candidates"] == 1


def test_load_json_files_orders_same_day_states_by_generated_at(tmp_path: Path):
    """Codex review, PR #593 ("Include intra-day ordering in the accumulation
    key"): the accumulation sort key was day-granular, so two states from the
    SAME trading day compared equal and the stable sort simply preserved caller
    order. An afternoon state already reporting $100 week-to-date, handed in
    before that morning's retained $5 partial, still normalized the partial to
    $105.
    """
    morning = tmp_path / "morning.json"
    morning.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "generated_at": "2026-08-10T10:30:00",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}],
            }
        ),
        encoding="utf-8",
    )
    afternoon = tmp_path / "afternoon.json"
    afternoon.write_text(
        json.dumps(
            {
                "status": "ok",
                "trading_day": "2026-08-10",
                "generated_at": "2026-08-10T15:30:00",
                "budget": {"week_notional_usd": 100.0},
            }
        ),
        encoding="utf-8",
    )

    # Deliberately non-chronological input order, same trading day.
    loaded = load_json_files([afternoon, morning])

    morning_state = next(d for d in loaded if d["generated_at"].endswith("10:30:00"))
    assert morning_state["budget"]["week_notional_usd"] == pytest.approx(5.0)


def test_performance_snapshot_reports_the_corrected_latest_weekly_total():
    """Codex review, PR #593 ("Use the corrected latest weekly total in the
    snapshot"): load_json_files() correctly lowers Tuesday's week_notional_usd
    to $98 after a -$2 settlement correction, but build_performance_snapshot()
    reduced week_notional_usd with a max() across states, so Monday's stale
    pre-correction $100 won and the correction never reached the user-visible
    total. week_notional_usd is cumulative week-to-date by construction, so the
    LATEST state of the week is the one that must win.
    """
    run_states = [
        {
            "trading_day": "2026-08-10",
            "generated_at": "2026-08-10T15:30:00",
            "budget": {"week_notional_usd": 100.0, "weekly_cap_usd": 200.0},
        },
        {
            "trading_day": "2026-08-11",
            "generated_at": "2026-08-11T15:30:00",
            "budget": {"week_notional_usd": 98.0, "weekly_cap_usd": 200.0},
        },
    ]

    snapshot = build_performance_snapshot(run_states)

    assert "| Week notional / cap | $98.00 / $200.00 |" in snapshot


def test_performance_snapshot_includes_undated_retained_states_in_the_latest_week():
    """Codex review, PR #593 ("Include undated retained states in the latest
    week"): the latest-week selection discarded a retained submitted state with
    no usable trading_day/iso_week whenever ANY dated state existed -- even
    though load_json_files() deliberately retains such a state and has already
    normalized its cumulative weekly total ($100 plus a $5 blocked submission =
    $105). The snapshot still reported the dated state's stale $100.

    _accumulation_sort_key already sorts an undated state after every dated
    one, stably, and load_json_files()'s accumulator leaves the running ISO
    week untouched for it -- so it belongs to the latest known week, and being
    last, it is the state that wins.
    """
    run_states = [
        {
            "trading_day": "2026-08-10",
            "generated_at": "2026-08-10T15:30:00",
            "budget": {"week_notional_usd": 100.0, "weekly_cap_usd": 200.0},
        },
        {
            # No trading_day and no iso_week: a retained blocked submission
            # normalized to the running $100 + its own $5.
            "status": "blocked",
            "generated_at": "2026-08-11T10:30:00",
            "budget": {"week_notional_usd": 105.0, "weekly_cap_usd": 200.0},
        },
    ]

    snapshot = build_performance_snapshot(run_states)

    assert "| Week notional / cap | $105.00 / $200.00 |" in snapshot


def test_performance_snapshot_reports_the_latest_weeks_own_weekly_cap():
    """Codex review, PR #593 ("Match the weekly cap to the selected week"): the
    weekly total became "the latest ISO week's latest value", but the cap
    stayed the MAXIMUM across every state load_scheduled_run_states() loads --
    and that is the complete historical tree. An older $1,200-cap week followed
    by the current $900-cap week rendered the current week's spend against
    $1,200 and understated utilization.
    """
    run_states = [
        {
            "trading_day": "2026-08-03",
            "generated_at": "2026-08-03T15:30:00",
            "budget": {"week_notional_usd": 900.0, "weekly_cap_usd": 1200.0},
        },
        {
            "trading_day": "2026-08-10",
            "generated_at": "2026-08-10T15:30:00",
            "budget": {"week_notional_usd": 400.0, "weekly_cap_usd": 900.0},
        },
    ]

    snapshot = build_performance_snapshot(run_states)

    assert "| Week notional / cap | $400.00 / $900.00 |" in snapshot


def test_performance_snapshot_weekly_reduction_keeps_absent_and_undated_rules():
    """Guards for the two edges the shared weekly reducer must keep (Codex
    review, PR #593): a state that reports NO weekly figure is skipped rather
    than read as $0, and when not a single state can be placed in an ISO week
    the historic raise-only max() still applies.
    """
    # The LATEST state of the week reports neither total nor cap; the earlier
    # one that does still wins, instead of the absent fields reading as $0.
    partial_week = [
        {
            "trading_day": "2026-08-10",
            "generated_at": "2026-08-10T10:30:00",
            "budget": {"week_notional_usd": 50.0, "weekly_cap_usd": 100.0},
        },
        {
            "trading_day": "2026-08-11",
            "generated_at": "2026-08-11T15:30:00",
            "budget": {"day_notional_usd": 0.0},
        },
    ]
    assert "| Week notional / cap | $50.00 / $100.00 |" in build_performance_snapshot(partial_week)

    # Nothing placeable in a week at all -> the historic max() reduction.
    undated_only = [
        {"budget": {"week_notional_usd": 20.0, "weekly_cap_usd": 40.0}},
        {"budget": {"week_notional_usd": 12.0, "weekly_cap_usd": 90.0}},
    ]
    assert "| Week notional / cap | $20.00 / $90.00 |" in build_performance_snapshot(undated_only)


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


def test_load_json_files_matches_bare_key_outcome_to_its_order_id(tmp_path: Path):
    """Codex review, PR #593 ("Match the bare-key outcome by its order ID"): a
    retained blocked state can hold an EARLIER id-less AAA candidate row worth
    $5 (identity "AAA") alongside a NEWER AAA row carrying order id B worth $7
    (identity "AAA:B") -- exactly what _merge_placements_into_state() produces
    when it appends an ID-bearing placement beside a preserved id-less row --
    while broker_outcomes describes B under the BARE key "AAA".

    The order-ID fold used to be skipped because the bare "AAA" identity was
    already occupied, so the outcome overwrote the id-less row's $5 with $7
    while B stayed aggregated separately: three-way arithmetic reporting $14
    for two orders worth $12. An order-id match outranks a bare-key
    coincidence with a row that claims no id, so the outcome now folds onto
    "AAA:B" and the id-less row keeps its own $5 -- and, because that fold is
    gated on the id matching a candidate row, the outcome does NOT also
    consume the id-less reconciliation pool entry.
    """
    partial_block = tmp_path / "partial_block.json"
    partial_block.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-10",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "pending", "submitted_amount_usd": 5.0},
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "B",
                        "submitted_amount_usd": 7.0,
                    },
                ],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "B",
                        "submitted_amount_usd": 7.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([partial_block])

    assert len(loaded) == 1
    state = loaded[0]
    assert state["budget"]["day_notional_usd"] == pytest.approx(12.0)
    assert state["budget"]["week_notional_usd"] == pytest.approx(12.0)
    assert state["counts"]["submitted"] == 2
    # The outcome reconciled onto the real order-id row, so no synthetic row
    # was appended for it: the two candidate rows are the two orders.
    assert state["counts"]["buy_candidates"] == 2
    assert len(state["buy_candidates"]) == 2


def test_load_json_files_orders_iso_week_only_states_by_their_week(tmp_path: Path):
    """Codex review, PR #593 ("Keep ISO-week-only states in chronological week
    order"): _iso_week_of_state() accepts a retained legacy state with no
    trading_day and a valid iso_week, but _accumulation_sort_key's tail bucket
    sorted every such state after EVERY dated state. An OLDER ISO-week-only
    state therefore landed last, reset the running accumulator back to its own
    week after the current week had already been walked, and
    _latest_week_value() then selected that older week's cap and notional as
    "latest" -- reporting the Aug 3 week's $900/$1,200 instead of the current
    Aug 10 week's $400/$900.

    The week-only state is now imputed to its week's Monday and sorts before
    every dated state of that same week.
    """
    older_week_only = tmp_path / "older_week_only.json"
    older_week_only.write_text(
        json.dumps(
            {
                "status": "ok",
                "iso_week": [2026, 32],  # the week of 2026-08-03
                "generated_at": "2026-08-05T15:30:00",
                "budget": {"week_notional_usd": 900.0, "weekly_cap_usd": 1200.0},
            }
        ),
        encoding="utf-8",
    )
    current_week = tmp_path / "current_week.json"
    current_week.write_text(
        json.dumps(
            {
                "status": "ok",
                "trading_day": "2026-08-10",
                "generated_at": "2026-08-10T15:30:00",
                "budget": {"week_notional_usd": 400.0, "weekly_cap_usd": 900.0},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([current_week, older_week_only])

    snapshot = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $400.00 / $900.00 |" in snapshot


def test_iso_week_of_state_rejects_an_impossible_week_and_falls_back():
    """Codex review, PR #593 ("Validate ISO-week bounds before accepting the
    tuple"): the tuple is now validated with date.fromisocalendar(), not merely
    shape-checked, so an impossible two-int value like [2026, 60] falls through
    to trading_day exactly as a malformed SHAPE already did -- rather than
    being handed back as though it were a real week.

    This is asserted at the helper because that is where the defect was and
    where it is observable. _accumulation_sort_key() already rejected such a
    value independently, so the two disagreed about the same field; the
    accumulator's week-boundary resets and _latest_week_value()'s latest_week
    were keyed on a week that does not exist. Validating in one place makes
    every caller agree by construction.
    """
    from dca_weekly_dashboard import _iso_week_of_state

    # Valid two-int week is still accepted verbatim.
    assert _iso_week_of_state({"iso_week": [2026, 33]}) == (2026, 33)
    # Impossible week with a usable trading_day falls back to the date.
    assert _iso_week_of_state(
        {"iso_week": [2026, 60], "trading_day": "2026-08-10"}
    ) == (2026, 33)
    # Impossible week with nothing to fall back to is "week unknown".
    assert _iso_week_of_state({"iso_week": [2026, 60]}) is None
    assert _iso_week_of_state({"iso_week": [0, 1]}) is None


def test_accumulation_sort_key_places_iso_week_only_state_first_in_its_week():
    """The imputed position is the week's Monday, AHEAD of every dated state of
    that same week (both figures are cumulative week-to-date, so real dated
    evidence must have the last word) and behind every state of an earlier
    week. A state with neither a usable trading_day nor a valid iso_week still
    sorts last, and a malformed two-int iso_week is no more placeable than a
    missing one.
    """
    prior_week = {"trading_day": "2026-08-07", "generated_at": "2026-08-07T15:30:00"}
    week_only = {"iso_week": [2026, 33], "generated_at": "2026-08-12T15:30:00"}
    same_week_monday = {"trading_day": "2026-08-10", "generated_at": "2026-08-10T15:30:00"}
    undated = {"generated_at": "2026-08-11T15:30:00"}
    malformed_week = {"iso_week": [2026, 60], "generated_at": "2026-08-11T16:30:00"}

    ordered = sorted(
        [undated, same_week_monday, malformed_week, week_only, prior_week],
        key=_accumulation_sort_key,
    )

    assert ordered == [prior_week, week_only, same_week_monday, undated, malformed_week]
