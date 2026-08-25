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


def test_build_performance_snapshot_preserves_prior_week_notional_when_later_value_malformed():
    # Codex review, PR #654 ("Preserve prior totals when the latest weekly
    # value is malformed"): _num()'s 0.0 fallback for a non-numeric
    # week_notional_usd is indistinguishable from a genuine $0 report, so
    # the latest-value-of-the-week reducer treated a malformed later state
    # as "the latest reported total is $0" and overwrote Monday's valid
    # $100 with it, instead of skipping the unusable value and keeping the
    # last state that actually reports a real number.
    run_states = [
        {
            "trading_day": "2026-07-20",  # Monday
            "budget": {"week_notional_usd": 100.0, "weekly_cap_usd": 500.0},
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 100.0}],
        },
        {
            "trading_day": "2026-07-21",  # Tuesday, same ISO week
            "budget": {"week_notional_usd": "not-a-number", "weekly_cap_usd": 500.0},
            "buy_candidates": [{"ticker": "BBB", "status": "submitted", "planned_amount_usd": 5.0}],
        },
    ]
    report = build_performance_snapshot(run_states)
    assert "| Week notional / cap | $100.00 / $500.00 |" in report


def test_build_performance_snapshot_preserves_prior_weekly_cap_when_later_value_malformed():
    # Codex review, PR #654 ("Skip malformed latest weekly caps"): the same
    # 0.0-fallback bug as the notional case above, but for the weekly cap --
    # a non-numeric/non-finite later weekly_cap_usd must not overwrite an
    # earlier valid cap with $0.00.
    run_states = [
        {
            "trading_day": "2026-07-20",  # Monday
            "budget": {"week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 10.0}],
        },
        {
            "trading_day": "2026-07-21",  # Tuesday, same ISO week
            "budget": {"week_notional_usd": 10.0, "weekly_cap_usd": float("nan")},
            "buy_candidates": [{"ticker": "BBB", "status": "submitted", "planned_amount_usd": 0.0}],
        },
    ]
    report = build_performance_snapshot(run_states)
    assert "| Week notional / cap | $10.00 / $100.00 |" in report


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


def test_this_run_submitted_notional_counts_distinct_order_on_same_ticker():
    # Codex review, PR #654 ("Reconcile same-ticker outcomes before skipping
    # them"): a retry can carry a bare-ticker-keyed broker_outcomes entry for
    # a DIFFERENT order on the same ticker already counted from
    # buy_candidates. Skipping the broker_outcomes fallback by ticker alone
    # dropped that second, distinct order's notional entirely instead of
    # reconciling by order identity.
    run_state = {
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 1.5, "order_id": "order-A"},
        ],
        "broker_outcomes": {
            "AAA": {"status": "submitted", "submitted_amount_usd": 2.5, "order_id": "order-B"},
        },
    }
    assert _this_run_submitted_notional(run_state) == 4.0


def test_this_run_submitted_notional_does_not_double_count_same_order_id():
    # The other half of the same fix: a broker_outcomes entry describing the
    # SAME order already summed from buy_candidates (matched by order_id, not
    # by its dict key) must not be added again.
    run_state = {
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 1.5, "order_id": "order-A"},
        ],
        "broker_outcomes": {
            "AAA:order-A": {"status": "submitted", "submitted_amount_usd": 9.99, "order_id": "order-A"},
        },
    }
    assert _this_run_submitted_notional(run_state) == 1.5


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


def test_load_json_files_coerces_malformed_buy_candidates_before_appending_synthetic_row(tmp_path):
    # Codex review, PR #654 ("Coerce malformed candidate containers before
    # appending"): a structurally rejected artifact can carry a truthy
    # non-list buy_candidates (e.g. from a partially-written/corrupted
    # run_state) while broker_outcomes still has real submitted evidence.
    # _has_submitted_evidence() admits it into `kept`, and the synthetic-row
    # normalization pass then tried `buy_candidates + [synthetic_row]` --
    # `or []` doesn't catch a truthy dict, so that raised TypeError and
    # aborted the entire weekly report instead of retaining the real
    # broker_outcomes evidence.
    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-13",
                "buy_candidates": {"bad": "shape"},
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 5.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([malformed])

    assert len(loaded) == 1
    assert isinstance(loaded[0]["buy_candidates"], list)
    assert any(row.get("ticker") == "AAA" for row in loaded[0]["buy_candidates"])


def test_load_json_files_rejects_non_finite_retained_order_amount(tmp_path):
    # Codex review, PR #654 ("Reject non-finite retained order amounts"): a
    # standard-JSON-safe "NaN" string converts cleanly via float() and would
    # otherwise flow into amounts_by_ticker, the synthetic candidate row's
    # planned_amount_usd, and every downstream weekly total as an actual
    # NaN -- poisoning the whole report with "$nan" instead of being treated
    # as absent, like a non-numeric amount already is.
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-14",
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": "NaN"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([nonfinite])

    assert len(loaded) == 1
    synthetic_row = next(row for row in loaded[0]["buy_candidates"] if row.get("ticker") == "AAA")
    assert synthetic_row["planned_amount_usd"] is None


def test_load_json_files_rejects_non_finite_candidate_amount(tmp_path):
    # Codex review, PR #654 ("Reject non-finite candidate amounts before
    # aggregation"): the candidate-row pass (buy_candidates, not
    # broker_outcomes) has the same float()-succeeds-on-"NaN" hole the
    # broker_outcomes pass was just fixed for -- a non-finite
    # submitted_amount_usd/planned_amount_usd must not flow into
    # amounts_by_ticker or get backfilled onto the row itself.
    nonfinite = tmp_path / "nonfinite_candidate.json"
    nonfinite.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-15",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "submitted_amount_usd": "NaN"}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([nonfinite])

    assert len(loaded) == 1
    row = next(r for r in loaded[0]["buy_candidates"] if r.get("ticker") == "AAA")
    assert row.get("planned_amount_usd") is None
    report = build_performance_snapshot(loaded)
    assert "nan" not in report.lower()
    assert "inf" not in report.lower()


def test_load_json_files_ignores_unhashable_order_id(tmp_path):
    # Codex review, PR #654 ("Validate order IDs before using them as
    # mapping keys"): load_json_files() doesn't schema-validate retained
    # states and explicitly supports structurally rejected ones -- a truthy
    # but unhashable order_id (e.g. a JSON array, valid JSON but never a
    # real order id) reached candidate_identity_by_order_id.setdefault(
    # order_id, ...) and raised TypeError: unhashable type: 'list',
    # aborting the entire weekly report instead of just ignoring the
    # unusable id.
    malformed = tmp_path / "unhashable_order_id.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-16",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 5.0, "order_id": ["not", "a", "str"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    # Must not raise -- this is the regression.
    loaded = load_json_files([malformed])

    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "$5.00" in report


def test_load_json_files_ignores_unhashable_ticker(tmp_path):
    # Codex review, PR #654 ("Normalize malformed candidate tickers before
    # set lookup"): a truthy but unhashable ticker (e.g. a JSON array)
    # reaches `ticker in seen_tickers` immediately, raising TypeError and
    # aborting the entire weekly report before the order-id guard even
    # runs -- one line earlier than the order_id hazard already fixed.
    malformed = tmp_path / "unhashable_ticker.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-18",
                "buy_candidates": [
                    {"ticker": ["not", "a", "str"], "status": "submitted", "planned_amount_usd": 9.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    # Must not raise -- this is the regression.
    loaded = load_json_files([malformed])

    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "$9.00" in report


def test_load_json_files_ignores_unhashable_broker_outcome_order_id(tmp_path):
    # Codex review, PR #654 ("Validate broker outcome IDs before
    # reconciliation"): a broker_outcomes entry whose truthy order_id is a
    # JSON array reaches `outcome_order_id in candidate_identity_by_
    # order_id` and raises TypeError, aborting the entire weekly report --
    # the same class of bug as the candidate-row order_id fix, but on the
    # broker_outcomes side of the reconciliation.
    malformed = tmp_path / "unhashable_outcome_order_id.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-19",
                "broker_outcomes": {
                    "AAA": {
                        "status": "submitted",
                        "submitted_amount_usd": 6.0,
                        "order_id": ["not", "a", "str"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # Must not raise -- this is the regression.
    loaded = load_json_files([malformed])

    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "$6.00" in report


def test_backfill_planned_amount_replaces_non_finite_existing_value(tmp_path):
    # Codex review, PR #654 ("Replace non-finite planned amounts during
    # backfill"): a retained row's existing planned_amount_usd of "NaN"/
    # "Infinity" converted cleanly via float() in _backfill_planned_amount(),
    # so it was treated as "already has a valid amount" and never replaced
    # by a finite derived amount -- downstream _num() preserves the
    # non-finite value, rendering totals as $nan/$inf.
    nonfinite = tmp_path / "nonfinite_existing_planned.json"
    nonfinite.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-17",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 7.0,
                        "planned_amount_usd": "NaN",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([nonfinite])

    assert len(loaded) == 1
    row = next(r for r in loaded[0]["buy_candidates"] if r.get("ticker") == "AAA")
    assert row["planned_amount_usd"] == 7.0
    report = build_performance_snapshot(loaded)
    assert "nan" not in report.lower()
    assert "inf" not in report.lower()


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
