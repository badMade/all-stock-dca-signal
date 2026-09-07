"""Tests for the DCA weekly dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dca_weekly_dashboard import (
    _current_week_claim_amount,
    _has_submitted_evidence,
    _is_manual_run_state,
    _iso_week_of_state,
    _merge_partial_state_into_keeper,
    _require_base_amount,
    _this_run_submitted_notional,
    build_budget_reserve_effectiveness,
    build_coverage_missed,
    build_graded_vs_fixed,
    build_performance_snapshot,
    build_sector_matrix,
    build_weekly_dashboard,
    build_weekly_review_notes,
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
def test_build_performance_snapshot_empty_states():
    report = build_performance_snapshot([])
    assert "| Run days | 0 |" in report
    assert "| Buy candidates | 0 |" in report
    assert "| Orders submitted | 0 |" in report
    assert "| Planned notional | $0.00 |" in report
    assert "| Executed notional | $0.00 |" in report
    assert "| Week notional / cap | $0.00 / $0.00 |" in report


def test_build_performance_snapshot_missing_fields():
    # Test fallback mechanisms when expected budget and counts fields are missing
    run_states = [
        {
            "trading_day": "2026-07-24",
            # Missing "counts" entirely
            # Missing "budget" entirely
            # These will fallback to {}
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 15.0},
            ]
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Run days | 1 |" in report
    assert "| Buy candidates | 0 |" in report
    assert "| Orders submitted | 0 |" in report
    assert "| Planned notional | $15.00 |" in report
    # Note: _this_run_submitted_notional fallback parses buy_candidates and extracts planned_amount_usd for executed notional fallback
    assert "| Executed notional | $15.00 |" in report
    assert "| Week notional / cap | $0.00 / $0.00 |" in report


def test_build_performance_snapshot_renders_happy_path():
    run_states = [
        {
            "trading_day": "2026-07-24",
            "counts": {"buy_candidates": 2, "submitted": 1},
            "budget": {
                "day_notional_usd": 15.0,
                "week_notional_usd": 15.0,
                "weekly_cap_usd": 100.0,
            },
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 15.0},
                {"ticker": "BBB", "status": "not_submitted", "planned_amount_usd": 10.0}
            ]
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Run days | 1 |" in report
    assert "| Buy candidates | 2 |" in report
    assert "| Orders submitted | 1 |" in report
    assert "| Planned notional | $25.00 |" in report
    assert "| Executed notional | $15.00 |" in report
    assert "| Week notional / cap | $15.00 / $100.00 |" in report


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


def test_load_json_files_preserves_unplaced_candidates_for_coverage(tmp_path):
    # Codex review, PR #654 ("Preserve unplaced candidates in retained
    # partial runs"): a compute-only partial failure with A submitted and
    # B unplaced (a real planned amount, never placed) previously had B
    # deleted entirely by load_json_files(), reporting one candidate with
    # only A's planned notional instead of two candidates with B visible
    # as the missed opportunity. B must still show up in "Buy candidates"
    # and "Planned notional" (coverage/planned analytics), while
    # "Orders submitted"/"Executed notional" (submission totals) and
    # build_graded_vs_fixed()'s graded/fixed columns (executed-capital
    # analytics) must still reflect only A.
    run_state = tmp_path / "unplaced_candidate.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "budget": {
                    "base_amount_usd": 2.0,
                    "hypothetical_day_spend_usd": 3.5,
                    "week_notional_usd": 3.5,
                    "weekly_cap_usd": 100.0,
                },
                "orders_submitted_count": 1,
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 3.5},
                    {"ticker": "BBB", "status": "not_submitted", "planned_amount_usd": 10.0},
                ],
                "broker_outcomes": {"AAA": {"status": "submitted"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    tickers = {row.get("ticker") for row in loaded[0]["buy_candidates"]}
    assert tickers == {"AAA", "BBB"}  # B is retained, not deleted

    report = build_performance_snapshot(loaded)
    assert "| Buy candidates | 2 |" in report  # coverage: both A and B
    assert "| Planned notional | $13.50 |" in report  # coverage: 3.5 + 10.0
    assert "| Orders submitted | 1 |" in report  # submission total: A only
    assert "| Executed notional | $3.50 |" in report  # submission total: A only

    graded_report = build_graded_vs_fixed(loaded)
    row = next(line for line in graded_report.splitlines() if line.startswith("| 2026-07-24"))
    assert row.split("|")[3].strip() == "$3.50"  # Graded $: A only, not B's $10


def test_load_json_files_skips_a_malformed_file_and_keeps_the_rest(tmp_path, capsys):
    # Codex review ("Isolate malformed run states during loading"): a
    # truncated or unreadable run_state.json (an interrupted write, a
    # crash) previously aborted the WHOLE weekly dashboard via an
    # all-or-nothing list comprehension -- every other, healthy day's real
    # order/notional evidence discarded along with the one damaged file.
    # The malformed file must be skipped (with a stderr warning), not
    # fatal to the rest of the week.
    healthy = tmp_path / "healthy.json"
    healthy.write_text(
        json.dumps({"status": "ok", "trading_day": "2026-07-24", "buy_candidates": []}),
        encoding="utf-8",
    )
    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"status": "ok", "trading_day": "2026-07-2', encoding="utf-8")

    loaded = load_json_files([healthy, truncated])

    assert len(loaded) == 1
    assert loaded[0]["trading_day"] == "2026-07-24"
    assert "truncated.json" in capsys.readouterr().err


@pytest.mark.parametrize("root", ["null", "[]", '"just a string"', "42"])
def test_load_json_files_skips_a_non_object_json_root(tmp_path, capsys, root):
    # Codex review ("Validate decoded run states before retaining them"):
    # syntactically valid JSON with a non-object root (null, a bare list,
    # a string, a number) decodes without error, but every consumer --
    # starting with this same function's own status/manual-run filter --
    # assumes a dict and raises AttributeError on .get(). That escaped the
    # per-path guard meant to isolate exactly this kind of malformed
    # artifact, aborting the whole weekly dashboard again.
    healthy = tmp_path / "healthy.json"
    healthy.write_text(
        json.dumps({"status": "ok", "trading_day": "2026-07-24", "buy_candidates": []}),
        encoding="utf-8",
    )
    bad_root = tmp_path / "bad_root.json"
    bad_root.write_text(root, encoding="utf-8")

    loaded = load_json_files([healthy, bad_root])

    assert len(loaded) == 1
    assert loaded[0]["trading_day"] == "2026-07-24"
    assert "bad_root.json" in capsys.readouterr().err


def test_load_json_files_deduplicates_outcome_only_submission_by_order_id(tmp_path):
    # Codex review, PR #654 ("Deduplicate outcome-only submissions by order
    # ID"): the same physical order (order_id "B") appears in
    # broker_outcomes under both a synthetic recovery key ("Y#recovery")
    # and the bare-ticker key ("Y"), with NO buy_candidates row at all --
    # every candidate-matching fold only looks for the id among CANDIDATE
    # identities, which stay empty here, so both outcomes previously became
    # distinct submissions ($14 / 2 orders) instead of one ($7 / 1 order).
    run_state = tmp_path / "duplicate_outcome_order_id.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "budget": {"weekly_cap_usd": 100.0},
                "broker_outcomes": {
                    "Y#recovery": {
                        "status": "submitted",
                        "submitted_amount_usd": 7.0,
                        "order_id": "B",
                        "ticker": "Y",
                    },
                    "Y": {"status": "submitted", "submitted_amount_usd": 7.0, "order_id": "B"},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert len(loaded[0]["buy_candidates"]) == 1  # one synthetic row, not two
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report


def test_load_json_files_deduplicates_adjustment_only_outcome_by_order_id(tmp_path):
    # Codex review, PR #654 ("Deduplicate adjustment-only outcomes by
    # order ID"): the same settlement correction (order_id "B") appears
    # in broker_outcomes under two mapping keys, both explicitly flagged
    # whole_share_settlement_adjustment -- the ordinary outcome-only
    # dedup (outcome_identity_by_order_id) deliberately excludes
    # adjustments, so this shape bypassed it entirely and each mapping
    # key minted its own synthetic row, doubling the $2 correction's
    # contribution to day/week notional to $4.
    run_state = tmp_path / "duplicate_adjustment_only_outcome.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "budget": {"weekly_cap_usd": 100.0},
                "broker_outcomes": {
                    "A#recovery": {
                        "status": "submitted",
                        "submitted_amount_usd": 2.0,
                        "order_id": "B",
                        "ticker": "A",
                        "whole_share_settlement_adjustment": True,
                    },
                    "A": {
                        "status": "submitted",
                        "submitted_amount_usd": 2.0,
                        "order_id": "B",
                        "whole_share_settlement_adjustment": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert len(loaded[0]["buy_candidates"]) == 1  # one synthetic row, not two
    report = build_performance_snapshot(loaded)
    assert "$4.00" not in report
    assert "| Executed notional | $2.00 |" in report


def test_load_json_files_prefers_usable_broker_outcome_claim_breakdown(tmp_path):
    # Codex review, PR #654 ("Prefer usable broker-outcome claim
    # breakdowns"): two broker_outcomes entries fold onto the same identity
    # (same order_id "B", no candidate row -- see the dedup test above).
    # The FIRST one processed carries a malformed whole_share_claim_
    # breakdown (a string); the SECOND carries a valid one attributing $15
    # of this $20 order to the prior week (a Friday claim surfaced this
    # Monday). Registering claim_sources on bare truthiness let the
    # malformed first entry reserve the slot via setdefault(), so the
    # valid second entry's breakdown was never used and the full $20 was
    # attributed to the current week instead of the real $5 share.
    run_state = tmp_path / "outcome_claim_breakdown.json"
    run_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",  # Monday, ISO week (2026, 30)
                "status": "blocked",
                "budget": {"weekly_cap_usd": 100.0},
                "broker_outcomes": {
                    "Y#recovery": {
                        "status": "submitted",
                        "submitted_amount_usd": 20.0,
                        "order_id": "B",
                        "ticker": "Y",
                        "whole_share_claim_breakdown": "not-a-list",
                    },
                    "Y": {
                        "status": "submitted",
                        "submitted_amount_usd": 20.0,
                        "order_id": "B",
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-07-17", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_sanitizes_order_id_on_unplaced_candidate_row(tmp_path):
    # Codex review, PR #654 ("Sanitize order IDs on unplaced candidate
    # rows"): an unplaced/statusless candidate row's order_id is only
    # sanitized inside the real-order-status loop, which such a row never
    # reaches. A truthy but unhashable order_id (a JSON array) on that row
    # then hit the `existing_order_ids` set comprehension -- now built from
    # every retained row, not just real-order-status ones -- and raised
    # TypeError, aborting the report despite the valid AAA submission.
    run_state = tmp_path / "unhashable_order_id.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [{"ticker": "BBB", "order_id": ["not", "hashable"], "planned_amount_usd": 4.0}],
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 9.0}},
            }
        ),
        encoding="utf-8",
    )

    # Must not raise -- this is the regression.
    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "$9.00" in report


def test_load_json_files_folds_duplicate_candidate_rows_by_order_id(tmp_path):
    # Codex review, PR #654 ("Fold duplicate candidate rows by order ID"):
    # two retained candidate rows share the same ticker AND the same
    # order_id -- a genuine duplicate, not a same-day retry's distinct
    # second order. The composite "<ticker>:<order_id>" identity used to be
    # minted unconditionally for a repeated ticker, giving the duplicate a
    # SECOND identity instead of folding onto the one this order_id already
    # claimed, so two $7 rows for order "X" produced $14 of submitted
    # notional for one physical order.
    run_state = tmp_path / "duplicate_candidate_same_order_id.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "B", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0},
                    {"ticker": "B", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "$7.00" in report
    assert "$14.00" not in report


def test_load_json_files_backfills_matched_stale_candidate_row(tmp_path):
    # Codex review, PR #654 ("Backfill matched stale candidate rows"): a
    # retained candidate row for order "X" has a "not_submitted" status
    # (stale -- the artifact was written before the fill confirmation) and
    # no usable planned_amount_usd, but its matching broker outcome (same
    # order_id) proves a real $7 submission. The outcome-reconciliation
    # pass recognized the order_id as "already represented" (via
    # existing_order_ids) and skipped past it without ever backfilling the
    # amount or correcting the stale status onto the original row, so the
    # order counted as $7 executed in the performance snapshot but $0.00
    # graded/sector spend, and stayed excluded from build_graded_vs_fixed()
    # entirely because of its stale non-placed status.
    run_state = tmp_path / "backfill_stale_candidate_row.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "budget": {"weekly_cap_usd": 100.0, "base_amount_usd": 3.0},
                "buy_candidates": [{"ticker": "B", "order_id": "X", "status": "not_submitted"}],
                "broker_outcomes": {"B": {"status": "submitted", "submitted_amount_usd": 7.0, "order_id": "X"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert len(loaded[0]["buy_candidates"]) == 1
    row = loaded[0]["buy_candidates"][0]
    assert row["planned_amount_usd"] == 7.0
    assert row["status"] == "submitted"
    graded_report = build_graded_vs_fixed(loaded)
    assert "$7.00" in graded_report


def test_load_json_files_removes_malformed_rows_from_retained_candidate_list(tmp_path):
    # Codex review, PR #654 ("Remove malformed rows from retained candidate
    # lists"): a blocked artifact has valid submission evidence in
    # broker_outcomes but buy_candidates is a list containing a non-dict
    # element (e.g. a bare `1`). Downstream dashboard builders call
    # `.get()` on every row unconditionally, so that stray element aborted
    # the report with AttributeError despite the valid order evidence.
    run_state = tmp_path / "malformed_candidate_row.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [1],
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 9.0}},
            }
        ),
        encoding="utf-8",
    )

    # Must not raise -- this is the regression.
    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert all(isinstance(row, dict) for row in loaded[0]["buy_candidates"])
    report = build_performance_snapshot(loaded)
    assert "$9.00" in report


def test_load_json_files_backfills_synthetic_row_from_later_duplicate_outcome(tmp_path):
    # Codex review, PR #654 ("Backfill the synthetic row from later
    # duplicate outcomes"): two broker_outcomes entries fold onto the same
    # identity (same order_id "B", no candidate row). The FIRST one
    # processed omits submitted_amount_usd entirely; the SECOND supplies
    # $7.00. amounts_by_ticker correctly ends up $7.00 either way, but the
    # synthetic candidate row created for the FIRST (amount-less) entry
    # previously stayed stuck at planned_amount_usd=None -- the folded
    # second entry's `continue` skipped both creating its own row AND
    # updating the existing one -- so graded/sector/planned totals
    # reported $0.00 for an order the performance snapshot already counts
    # as $7.00 executed.
    run_state = tmp_path / "backfill_synthetic_row.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "budget": {"weekly_cap_usd": 100.0},
                "broker_outcomes": {
                    "Y#recovery": {"status": "submitted", "order_id": "B", "ticker": "Y"},
                    "Y": {"status": "submitted", "submitted_amount_usd": 7.0, "order_id": "B"},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert len(loaded[0]["buy_candidates"]) == 1
    row = loaded[0]["buy_candidates"][0]
    assert row["planned_amount_usd"] == 7.0
    assert row["submitted_amount_usd"] == 7.0


def test_load_json_files_recomputes_candidate_count_after_folding_duplicates(tmp_path):
    # Codex review, PR #654 ("Recompute the candidate count after folding
    # duplicates"): counts["buy_candidates"] is computed from the FULL
    # pre-dedup candidate list before the duplicate-order-ID fold removes a
    # row -- so a retained state with two identical submitted rows sharing
    # ticker "B" and order_id "X" correctly ends up with one row and one
    # $7 order, but the stored count still reported 2 buy candidates
    # instead of being recomputed from the deduplicated list.
    run_state = tmp_path / "duplicate_candidate_count.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "B", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0},
                    {"ticker": "B", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert loaded[0]["counts"]["buy_candidates"] == 1
    report = build_performance_snapshot(loaded)
    assert "| Buy candidates | 1 |" in report


def test_load_json_files_rejects_negative_ordinary_order_amount(tmp_path):
    # Codex review, PR #654 ("Reject negative ordinary-order amounts during
    # retention"): schemas/run_state.schema.json requires planned_amount_usd
    # to be nonnegative for an ordinary order, but _finite_float() accepts a
    # negative value because it IS finite. A structurally rejected blocked
    # artifact with planned_amount_usd=-5 on an ordinary submitted row
    # therefore reported "Planned notional | $-5.00" and "Executed notional
    # | $-5.00" -- reducing rather than growing the dashboard's financial
    # totals for what should be a real, positive submission.
    run_state = tmp_path / "negative_ordinary_amount.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": -5.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert loaded[0]["buy_candidates"][0]["planned_amount_usd"] is None
    report = build_performance_snapshot(loaded)
    assert "$-5.00" not in report


def test_load_json_files_rejects_negative_submitted_amount_for_ordinary_order(tmp_path):
    # Codex review, PR #654 ("Reject negative submitted amounts for
    # ordinary orders"): fresh evidence after the negative-planned-amount
    # fix is that a finite negative submitted_amount_usd still won in the
    # reconciliation loop's amt_value derivation without the same
    # adjustment exemption check. An ordinary row with a valid
    # planned_amount_usd=5 but submitted_amount_usd=-7 recorded
    # day_notional_usd=-7 instead of falling back to the equally real
    # planned amount, reducing executed totals.
    run_state = tmp_path / "negative_submitted_amount.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "planned_amount_usd": 5.0,
                        "submitted_amount_usd": -7.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "$-7.00" not in report
    assert "$5.00" in report


def test_load_json_files_backfills_retained_row_before_dropping_duplicate(tmp_path):
    # Codex review, PR #654 ("Backfill the retained row before dropping its
    # duplicate"): two candidate rows share order_id "X" -- the FIRST has
    # no usable amount of its own, the SECOND (later removed as a
    # duplicate) carries $7. The duplicate's amount was only backfilled
    # onto the row about to be discarded, never onto the row that survives,
    # so the performance snapshot reported $7 executed while planned/
    # graded/sector totals stayed at $0 for the same order.
    run_state = tmp_path / "backfill_before_drop.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "budget": {"weekly_cap_usd": 100.0, "base_amount_usd": 3.0},
                "buy_candidates": [
                    {"ticker": "B", "order_id": "X", "status": "submitted"},
                    {"ticker": "B", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded[0]["buy_candidates"]) == 1
    assert loaded[0]["buy_candidates"][0]["planned_amount_usd"] == 7.0
    graded_report = build_graded_vs_fixed(loaded)
    assert "$7.00" in graded_report


def test_load_json_files_deduplicates_stale_candidates_by_order_id(tmp_path):
    # Codex review, PR #654 ("Deduplicate stale candidates before outcome
    # reconciliation"): two candidate rows share order_id "X" but NEITHER
    # carries a real status -- the earlier order-ID fold only operates on
    # `_candidate_rows` (the real-order-status subset), so neither row ever
    # entered that loop and neither was folded or removed. Both survived in
    # buy_candidates, doubling the candidate count and planned notional for
    # one physical $7 order.
    run_state = tmp_path / "stale_duplicate_candidates.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "B", "order_id": "X", "planned_amount_usd": 7.0},
                    {"ticker": "B", "order_id": "X", "planned_amount_usd": 7.0},
                ],
                "broker_outcomes": {"B": {"status": "submitted", "order_id": "X", "submitted_amount_usd": 7.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded[0]["buy_candidates"]) == 1
    report = build_performance_snapshot(loaded)
    assert "| Buy candidates | 1 |" in report
    assert "$14.00" not in report


def test_load_json_files_rejects_negative_broker_outcome_notional(tmp_path):
    # Codex review, PR #654 ("Reject negative broker-outcome notionals"): a
    # retained blocked artifact has NO candidate row, only an ordinary
    # submitted broker outcome with submitted_amount_usd=-7. The
    # broker-outcome-only synthesis path checked only finiteness (a
    # separate check from the candidate-side negative-amount fix), so it
    # accepted the negative value and synthesized a -$7.00 candidate.
    run_state = tmp_path / "negative_broker_outcome.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": -7.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "$-7.00" not in report


def test_load_json_files_uses_stale_candidate_notional_when_outcome_omits_it(tmp_path):
    # Codex review, PR #654 ("Use stale candidate notional when its outcome
    # omits it"): a statusless candidate row carries planned_amount_usd=7,
    # and its order-ID-matched broker outcome proves submission but omits
    # submitted_amount_usd. The candidate-row pass only derives amounts for
    # real-order-status rows, so this statusless row's own valid amount was
    # never considered, and the amount fallback defaulted to $0 instead of
    # the real $7.
    run_state = tmp_path / "stale_candidate_amount_fallback.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [{"ticker": "B", "order_id": "X", "planned_amount_usd": 7.0}],
                "broker_outcomes": {"B": {"status": "submitted", "order_id": "X"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $7.00 |" in report


def test_load_json_files_applies_strict_adjustment_check_to_stale_amounts(tmp_path):
    # Codex review, PR #654 ("Apply the strict adjustment check to stale
    # amounts"): fresh evidence after _is_settlement_adjustment() was
    # introduced is that the stale-candidate amount fallback (the outcome-
    # reconciliation "use stale candidate notional when its outcome omits
    # it" path) still tested the raw whole_share_settlement_adjustment
    # marker's bare truthiness. A statusless candidate row carrying a
    # false-looking-but-truthy "false" marker and a negative
    # submitted_amount_usd=-7 was therefore exempted from the negative-
    # amount rejection (treated as a genuine adjustment) even though it's
    # classified as an ORDINARY order everywhere else in this module now.
    run_state = tmp_path / "stale_amount_false_string_adjustment.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {
                        "ticker": "B",
                        "order_id": "X",
                        "submitted_amount_usd": -7.0,
                        "whole_share_settlement_adjustment": "false",
                    }
                ],
                "broker_outcomes": {"B": {"status": "submitted", "order_id": "X"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "$-7.00" not in report


def test_load_json_files_refreshes_executed_total_after_merging_stale_duplicate(tmp_path):
    # Codex review, PR #654 ("Refresh executed totals after merging stale
    # duplicates"): a submitted keeper row has no usable amount of its own,
    # and a stale (statusless) duplicate sharing its order_id supplies $7.
    # The general all-row dedup pass copies $7 onto the retained row's
    # planned_amount_usd, but amounts_by_ticker[identity] -- what
    # budget.day_notional_usd is later summed from -- was already set to
    # 0.0 by the earlier real-order-status aggregation pass, before this
    # dedup pass ever ran, and was never refreshed. With no broker outcome
    # to revisit that identity, day_notional_usd stayed 0 while planned
    # notional correctly showed $7.
    run_state = tmp_path / "refresh_executed_after_stale_dedup.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "B", "order_id": "X", "status": "submitted"},
                    {"ticker": "B", "order_id": "X", "submitted_amount_usd": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $7.00 |" in report


def test_load_json_files_registers_claim_metadata_from_matched_stale_candidate(tmp_path):
    # Codex review, PR #654 ("Register claim metadata from matched stale
    # candidates"): a statusless candidate row has a valid
    # whole_share_claim_breakdown naming the prior week, and its matching
    # broker outcome proves submission but omits that breakdown entirely.
    # Without registering the candidate's own breakdown as the claim
    # source, the order's full amount is attributed to the artifact's
    # current week instead of the real claim week the candidate names.
    run_state = tmp_path / "claim_metadata_from_stale_candidate.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-24",  # Monday
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "Y",
                        "order_id": "R",
                        "planned_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-21", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    }
                ],
                "broker_outcomes": {"Y": {"status": "submitted", "order_id": "R", "submitted_amount_usd": 20.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_registers_claim_metadata_copied_during_all_row_dedup(tmp_path):
    # Codex review, PR #654 ("Register copied claim metadata before weekly
    # aggregation"): this is the candidate-to-candidate twin of the
    # broker-outcome-side fix above -- a real-status keeper row with NO
    # claim breakdown and a stale (statusless) duplicate sharing its
    # order_id that DOES carry a valid prior-week breakdown, with no
    # broker outcome at all to re-trigger claim_sources registration. The
    # general dedup pass copies the breakdown onto the keeper's own
    # whole_share_claim_breakdown field, but without also registering
    # claim_sources[identity], build_performance_snapshot()'s week split
    # never sees it and attributes the full $20 to the current week.
    run_state = tmp_path / "claim_metadata_copied_during_dedup.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-24",  # Monday
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "Y", "order_id": "R", "status": "submitted", "submitted_amount_usd": 20.0},
                    {
                        "ticker": "Y",
                        "order_id": "R",
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-08-21", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_separates_ordinary_outcome_from_adjustment_identity(tmp_path):
    # Codex review, PR #654 ("Separate ordinary outcomes from
    # adjustment-owned identities"): a settlement-adjustment candidate for
    # ticker "AAA" already owns the bare "AAA" identity (-$2 correction),
    # but adjustment rows are deliberately excluded from
    # candidate_order_id_by_identity's own registration -- so a SEPARATE
    # ordinary broker-outcome-only order under that same bare ticker key
    # ($5) had no order_id to compare against and never split into its own
    # composite identity. It landed on the SAME "AAA" identity and
    # OVERWROTE the adjustment's amount in amounts_by_ticker entirely,
    # reporting $5 executed instead of the real $3 net (-$2 + $5).
    run_state = tmp_path / "ordinary_outcome_vs_adjustment_identity.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": -2.0,
                        "whole_share_settlement_adjustment": True,
                    }
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "Z", "submitted_amount_usd": 5.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $3.00 |" in report


def test_load_json_files_removes_discarded_duplicate_identity_from_totals(tmp_path):
    # Codex review, PR #654 ("Remove discarded duplicate identities from
    # execution totals"): two retained real-status rows share order_id
    # "X" but have DIFFERENT tickers (A and B) -- the earlier real-order-
    # status pass's own fold only merges same-TICKER repeats, so each got
    # its own separate aggregation identity ("A" and "B") there, both
    # counted. The general all-row dedup pass then groups them by
    # order_id alone (ignoring ticker) and removes one ROW as a
    # duplicate, but left its identity's entries in amounts_by_ticker and
    # counted_identities untouched -- one surviving candidate row still
    # reported two submitted orders and $10 executed for what should be
    # a single $5 order.
    run_state = tmp_path / "duplicate_order_id_different_tickers.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "A", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "B", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded[0]["buy_candidates"]) == 1
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $5.00 |" in report


def test_load_json_files_rejects_boolean_planned_amount(tmp_path):
    # Codex review, PR #654 ("Reject booleans before converting retained
    # amounts"): a structurally rejected blocked artifact carries
    # `planned_amount_usd: true` -- bool is a subclass of int in Python,
    # so a bare float(True) succeeds as 1.0 even though the run_state
    # schema requires a JSON number and never accepts a boolean. The
    # newly retained order would otherwise be reported as a real $1
    # deployment.
    run_state = tmp_path / "boolean_planned_amount.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": True}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert loaded[0]["buy_candidates"][0]["planned_amount_usd"] is None
    report = build_performance_snapshot(loaded)
    assert "$1.00" not in report


def test_load_json_files_rejects_boolean_broker_outcome_amount(tmp_path):
    # Codex review, PR #654 ("Reject booleans before converting retained
    # amounts"): the broker-outcome-only amount path (no matching
    # candidate row) has its own separate conversion from
    # submitted_amount_usd -- a boolean there survived the same way.
    run_state = tmp_path / "boolean_broker_outcome_amount.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": True}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "$1.00" not in report


def test_require_base_amount_rejects_nonpositive_ladder_base(tmp_path):
    # Codex review, PR #654 ("Reject nonpositive ladder base amounts"): a
    # retained blocked morning/midday artifact omits the budget-level base
    # but carries ladder.base_amount_usd=-2 -- finite, so it survived the
    # existing non-numeric/non-finite guards, but the sizing-ladder schema
    # requires this value to be strictly positive.
    run_state = tmp_path / "nonpositive_ladder_base.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "ladder": {"base_amount_usd": -2.0},
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    with pytest.raises(ValueError, match="nonpositive"):
        _require_base_amount(loaded[0])


def test_require_base_amount_falls_through_null_primary_base(tmp_path):
    # Codex review, PR #654 ("Fall through null primary base amounts"): a
    # retained blocked morning-family artifact has budget.base_amount_usd
    # explicitly set to null (JSON has no way to distinguish "field
    # omitted" from "field explicitly nulled"), alongside a valid
    # ladder.base_amount_usd=2. dict.get()'s default argument only fires
    # when the KEY is absent, not when it's present with an explicit null,
    # so the old nested `.get()` chain returned None directly and never
    # consulted the ladder fallback, raising instead of using the usable
    # $2 base.
    run_state = tmp_path / "null_primary_base.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "budget": {"base_amount_usd": None},
                "ladder": {"base_amount_usd": 2.0},
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert _require_base_amount(loaded[0]) == 2.0


def test_require_base_amount_rejects_falsey_invalid_primary_base(tmp_path):
    # Codex review, PR #654 ("Reject falsey invalid primary base
    # amounts"): fresh evidence after the null-fallback fix is that
    # budget.base_amount_usd="" (falsy but explicitly present, not null)
    # alongside a valid ladder.base_amount_usd=2 must NOT silently fall
    # through to the ladder value -- an `or`-based chain would have
    # treated "" the same as None and masked the genuinely corrupted
    # primary field instead of failing fast as this function's docstring
    # promises.
    run_state = tmp_path / "falsey_invalid_primary_base.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "budget": {"base_amount_usd": ""},
                "ladder": {"base_amount_usd": 2.0},
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    with pytest.raises(ValueError, match="non-numeric"):
        _require_base_amount(loaded[0])


def test_require_base_amount_rejects_boolean_run_level_base(tmp_path):
    # Codex review, PR #654 ("Reject boolean run-level base amounts"): a
    # newly retained blocked artifact carries budget.base_amount_usd:
    # true -- bool is a subclass of int in Python, so a bare float(True)
    # succeeds as 1.0 instead of raising, applying a $1 fixed-DCA baseline
    # to every candidate in the run instead of failing fast the way this
    # function's fail-fast contract requires for a genuinely invalid
    # value.
    run_state = tmp_path / "boolean_run_level_base.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "budget": {"base_amount_usd": True},
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    with pytest.raises(ValueError, match="non-numeric"):
        _require_base_amount(loaded[0])


def test_require_base_amount_coerces_a_non_mapping_budget(tmp_path):
    # Codex review, PR #1300 discussion_r3920988296 ("Normalize malformed
    # budget containers before accumulation"): a truthy non-dict budget
    # (e.g. "budget": ["bad"], from a structurally rejected retained
    # artifact this function deliberately admits) survived the bare
    # `rs.get("budget") or {}` fallback -- a non-empty list is truthy --
    # so budget.get("base_amount_usd") raised AttributeError before ever
    # reaching the usable ladder.base_amount_usd fallback.
    run_state = tmp_path / "list_budget.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "budget": ["bad"],
                "ladder": {"base_amount_usd": 2.0},
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert _require_base_amount(loaded[0]) == 2.0


def test_load_json_files_survives_a_non_mapping_budget(tmp_path):
    # Codex review, PR #1300 discussion_r3920988296 ("Normalize malformed
    # budget containers before accumulation"): a schema-rejected artifact
    # carrying "budget": ["bad"] (a truthy non-dict) survived the bare
    # `data.get("budget") or {}` fallback -- a non-empty list is truthy --
    # so the immediately following .get() call raised AttributeError and
    # aborted the whole weekly dashboard, even though load_json_files()
    # deliberately admits artifacts without validating them first.
    malformed = tmp_path / "malformed_budget.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "completed",
                "trading_day": "2026-08-21",
                "budget": ["bad"],
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}
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



def test_build_budget_reserve_effectiveness_basic_rendering():
    from dca_weekly_dashboard import build_budget_reserve_effectiveness

    run_states = [
        {
            "trading_day": "2026-07-20",
            "budget": {
                "budget_tier": "conservative",
                "day_notional_usd": 150.50,
                "daily_cap_usd": 200.00,
                "weekly_rollover_reserve_usd": 50.25,
                "remaining_spend_today_usd": 49.50,
            },
        },
        {
            "trading_day": "2026-07-21",
            "budget": {
                "budget_tier": "aggressive",
                "day_notional_usd": 300.00,
                "daily_cap_usd": 500.00,
                "weekly_rollover_reserve_usd": 100.00,
                "remaining_spend_today_usd": 200.00,
            },
        },
        {
            # Missing budget tier, should render 'n/a'
            "trading_day": "2026-07-22",
            "budget": {
                "day_notional_usd": 50.00,
                "daily_cap_usd": 100.00,
                "weekly_rollover_reserve_usd": 0.00,
                "remaining_spend_today_usd": 50.00,
            },
        },
        {
            # Fallback legacy fields
            "trading_day": "2026-07-23",
            "budget": {
                "budget_tier": "legacy",
                "day_notional_usd": 100.00,
                "daily_base_cap_usd": 150.00,
                "carry_forward_reserve_usd": 25.00,
                "remaining_spend_today_usd": 50.00,
            },
        },
        {
            # Missing fields should default to 0.00
            "trading_day": "2026-07-24",
        }
    ]

    report = build_budget_reserve_effectiveness(run_states)

    assert "## Budget and reserve effectiveness" in report
    assert "| Day | Tier | Spent $ | Daily cap $ | Rollover reserve $ | Remaining $ |" in report
    assert "| 2026-07-20 | conservative | $150.50 | $200.00 | $50.25 | $49.50 |" in report
    assert "| 2026-07-21 | aggressive | $300.00 | $500.00 | $100.00 | $200.00 |" in report
    assert "| 2026-07-22 | n/a | $50.00 | $100.00 | $0.00 | $50.00 |" in report
    assert "| 2026-07-23 | legacy | $100.00 | $150.00 | $25.00 | $50.00 |" in report
    assert "| 2026-07-24 | n/a | $0.00 | $0.00 | $0.00 | $0.00 |" in report


def test_build_budget_reserve_effectiveness_survives_a_non_mapping_budget():
    # Codex review, PR #1300 discussion_r3920988296 ("Normalize malformed
    # budget containers before accumulation"): a truthy non-dict budget
    # survived the bare `rs.get("budget", {})` default (only fires when the
    # key is ABSENT, not when present with a non-dict value) and crashed
    # the very next budget.get(...) call with AttributeError.
    run_states = [{"trading_day": "2026-07-24", "budget": ["bad"]}]

    # Must not raise -- this is the regression.
    report = build_budget_reserve_effectiveness(run_states)
    assert "2026-07-24" in report


def test_build_graded_vs_fixed_survives_a_non_mapping_budget_on_a_blocked_state():
    # Same fix, applied to the blocked-state precheck inside
    # build_graded_vs_fixed(): only "ladder" was previously coerced there,
    # not "budget" itself, leaving budget.get("base_amount_usd") just below
    # unguarded against a truthy non-dict value.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-08-23",
            "budget": ["bad"],
            "ladder": {"base_amount_usd": 2.0},
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
        }
    ]

    # Must not raise -- this is the regression.
    report = build_graded_vs_fixed(run_states)
    assert "2026-08-23" in report


def test_load_json_files_requires_boolean_settlement_adjustment_marker(tmp_path):
    # Codex review, PR #654 ("Require a boolean settlement-adjustment
    # marker"): a structurally rejected blocked artifact's ordinary
    # submitted order carries a false-looking but truthy string,
    # "false", in whole_share_settlement_adjustment -- a bare truthiness
    # check classified it as an adjustment (a bookkeeping correction to
    # an ALREADY-counted order), which excludes it from
    # counts.buy_candidates/counts.submitted/the fixed baseline. A real,
    # standalone $5 submission therefore rendered zero candidates and zero
    # submitted orders even though it contributed its dollars.
    run_state = tmp_path / "false_string_adjustment_marker.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_settlement_adjustment": "false",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert loaded[0]["counts"]["buy_candidates"] == 1
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $5.00 |" in report


def test_load_json_files_keeps_settlement_adjustment_separate_during_fold(tmp_path):
    # Codex review, PR #654 ("Keep settlement adjustments separate during
    # order-ID folding"): a retained blocked artifact has a submitted $100
    # base row and a whole_share_settlement_adjustment row sharing the
    # SAME ticker and order_id, correcting it by -$2. The duplicate-fold
    # fix (for a genuine duplicate order) lacked the adjustment exclusion
    # already applied elsewhere in this normalization, so it folded the
    # adjustment onto the base row's identity and OVERWROTE the base
    # amount in amounts_by_ticker instead of summing the correction
    # separately -- reporting $-2.00 executed notional instead of $98.00.
    run_state = tmp_path / "adjustment_shares_order_id.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 100.0},
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "submitted_amount_usd": -2.0,
                        "whole_share_settlement_adjustment": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "$98.00" in report
    assert "$-2.00" not in report


def test_load_json_files_keeps_adjustment_row_out_of_all_row_dedup(tmp_path):
    # Codex review, PR #654 ("Keep adjustment rows out of the all-row
    # deduplication"): a statusless base row and a statusless
    # whole_share_settlement_adjustment row share the same order_id --
    # neither carries a real status, so neither enters the earlier
    # real-order-status fold (already exempts adjustments). The LATER,
    # general all-row dedup pass grouped them by order_id anyway and had
    # no adjustment exclusion of its own, so it treated the adjustment as
    # a duplicate of the base row and physically REMOVED it from
    # buy_candidates -- discarding real deployed capital rather than
    # merely not double-counting it.
    run_state = tmp_path / "adjustment_in_all_row_dedup.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "submitted_amount_usd": 100.0},
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "submitted_amount_usd": -2.0,
                        "whole_share_settlement_adjustment": True,
                    },
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X", "submitted_amount_usd": 100.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded[0]["buy_candidates"]) == 2
    assert any(row.get("whole_share_settlement_adjustment") is True for row in loaded[0]["buy_candidates"])


def test_build_graded_vs_fixed_excludes_statusless_blocked_row_without_evidence():
    # Codex review, PR #654 ("Require placement evidence for statusless
    # blocked rows"): a retained blocked state has one genuinely submitted
    # $7 row (A) plus a statusless $5 row (B) with no matching broker
    # outcome -- B never got its status backfilled to a real one because
    # there's no evidence for it, so the ordinary-run "missing status means
    # placed" exemption must NOT apply inside a blocked retained state.
    # Without the fix, B's status is None, which slipped past the
    # exclusion check the same way a normal run's real candidates do,
    # rendering $12.00 graded across two "executed" candidates instead of
    # the $7.00 A alone actually supplied evidence for.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-08-23",
            "budget": {"base_amount_usd": 3.0},
            "buy_candidates": [
                {"ticker": "A", "status": "submitted", "planned_amount_usd": 7.0},
                {"ticker": "B", "planned_amount_usd": 5.0},
            ],
        }
    ]
    report = build_graded_vs_fixed(run_states)
    assert "$7.00" in report
    assert "$12.00" not in report


def test_build_performance_snapshot_falls_through_zero_generic_weekly_cap():
    # Codex review, PR #654 ("Preserve zero-cap fallback semantics"): fresh
    # evidence after the malformed-cap fallthrough fix is that
    # _finite_float(0.0) legitimately returns 0.0, so a state with
    # weekly_cap_usd=0 and a valid legacy weekly_notional_cap_usd=100 now
    # reported a $0 cap instead of falling through to the legacy field the
    # way the original `or`-chain (and this function's own documented
    # behavior) always did for a genuine zero primary value.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "budget": {
                "hypothetical_day_spend_usd": 5.0,
                "hypothetical_week_spend_usd": 5.0,
                "weekly_cap_usd": 0.0,
                "weekly_notional_cap_usd": 100.0,
            },
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 5.0}],
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_reconciles_outcome_against_statusless_candidate_row(tmp_path):
    # Codex review, PR #654 ("Reconcile outcome-backed candidates despite
    # stale statuses"): a retained candidate row for order "X" carries no
    # status field (or a still-non-placed one), so it's invisible to the
    # real-order-status filter that used to also gate which rows counted as
    # "already represented" for the outcome-reconciliation pass. Its
    # matching broker_outcomes entry (same order_id "X") therefore couldn't
    # find it and materialized a SECOND synthetic candidate row for the
    # same physical $7 order, doubling planned/graded/sector spend to $14
    # for one order.
    run_state = tmp_path / "statusless_candidate_reconciliation.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [{"ticker": "B", "order_id": "X", "planned_amount_usd": 7.0}],
                "broker_outcomes": {"B": {"status": "submitted", "submitted_amount_usd": 7.0, "order_id": "X"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert len(loaded[0]["buy_candidates"]) == 1
    report = build_performance_snapshot(loaded)
    assert "$7.00" in report
    assert "$14.00" not in report


def test_load_json_files_sanitizes_unhashable_trading_day(tmp_path):
    # Codex review, PR #654 ("Sanitize trading days on newly retained
    # blocked states"): a structurally rejected blocked artifact admitted
    # for its real broker submission evidence can carry a list-valued
    # trading_day -- build_coverage_missed() later uses it as a dict
    # lookup key (summary_by_day.get(day)) and raises TypeError:
    # unhashable type, aborting the entire weekly report.
    run_state = tmp_path / "unhashable_trading_day.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": ["not", "a", "str"],
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 4.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert loaded[0]["trading_day"] is None
    # Must not raise -- this is the regression.
    build_coverage_missed(loaded, [])


def test_load_json_files_treats_unparsable_trading_day_as_absent(tmp_path):
    # Codex review, PR #654 ("Validate retained trading-day strings before
    # historical fallback"): a type-valid but unparsable trading_day (e.g.
    # "2025-not-a-date") compares lexically BELOW _BASE_AMOUNT_CHANGE_DATE
    # ("2026-07-19"), so the blocked-state precheck and _require_base_amount()
    # both silently treated it as known pre-change history and assigned the
    # $3 base -- corrupting the fixed-DCA comparison instead of skipping the
    # row as unsupported.
    run_state = tmp_path / "unparsable_trading_day.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2025-not-a-date",
                "counts": {"buy_candidates": 1},
                "orders_submitted_count": 1,
                "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 4.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert loaded[0]["trading_day"] is None
    # Must not raise, and must not silently assign the $3 pre-change base --
    # both budget.base_amount_usd and ladder.base_amount_usd are absent, and
    # trading_day is no longer a usable pre-change date, so this row is
    # skipped from the comparison rather than corrupted with a guessed base.
    report = build_graded_vs_fixed(loaded)
    assert "$4.00" not in report


def test_load_json_files_normalizes_malformed_manual_review_tickers(tmp_path):
    # Codex review, PR #654 ("Normalize manual-review containers on
    # retained states"): a structurally rejected blocked artifact with real
    # submission evidence but a truthy non-list manual_review_tickers (e.g.
    # an integer) reaches build_coverage_missed()'s/
    # build_weekly_review_notes()'s unconditional len() call and raises
    # TypeError, aborting the entire weekly report.
    run_state = tmp_path / "malformed_manual_review.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-24",
                "manual_review_tickers": 5,
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 3.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    assert loaded[0]["manual_review_tickers"] == []
    # Must not raise -- this is the regression.
    build_coverage_missed(loaded, [])
    build_weekly_review_notes(loaded, [])


def test_load_json_files_sanitizes_unplaced_candidate_ticker(tmp_path):
    # Codex review, PR #654 ("Sanitize unplaced candidate tickers too"): a
    # retained partial run has one valid submitted row (AAA) and an
    # unplaced candidate whose ticker is a JSON array. The placed-status
    # filter that sanitizes tickers for the reconciliation loop excludes
    # this unplaced row, but the full row is still retained in
    # buy_candidates for coverage -- build_sector_matrix() then calls
    # sector_map.get(ticker) unconditionally on every retained row and
    # raises TypeError for the unhashable ticker.
    run_state = tmp_path / "unplaced_unhashable_ticker.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-24",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 5.0},
                    {"ticker": ["not", "a", "str"], "status": "not_submitted", "planned_amount_usd": 3.0},
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 5.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    # Must not raise -- this is the regression.
    build_sector_matrix(loaded)


def test_load_json_files_sanitizes_non_finite_planned_amount_with_no_replacement(tmp_path):
    # Codex review, PR #654 ("Sanitize retained planned amounts before
    # aggregation"): a dict candidate whose planned_amount_usd is "NaN" and
    # has no finite submitted/outcome amount to replace it previously
    # flowed straight into _num() (which has no finite check of its own)
    # in build_performance_snapshot()'s "Planned notional" and
    # build_sector_matrix()'s notional sum, rendering $nan.
    run_state = tmp_path / "nonfinite_no_replacement.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-24",
                "buy_candidates": [
                    {"ticker": "BBB", "status": "not_submitted", "planned_amount_usd": "NaN"}
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 5.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])

    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "nan" not in report.lower()
    assert "inf" not in report.lower()


def test_this_run_submitted_notional_deduplicates_outcome_only_by_order_id():
    # Codex review, PR #654 ("Record broker-only identities after summing
    # them"): _this_run_submitted_notional()'s own broker_outcomes fallback
    # never added a summed identity to seen_order_identities -- only the
    # buy_candidates pass did -- so two broker_outcomes entries for the
    # SAME order_id (a synthetic recovery key and the bare-ticker key,
    # neither represented by a candidate row) both passed the membership
    # check and were both summed: $14 for one $7 order.
    run_state = {
        "broker_outcomes": {
            "Y#recovery": {"status": "submitted", "submitted_amount_usd": 7.0, "order_id": "B"},
            "Y": {"status": "submitted", "submitted_amount_usd": 7.0, "order_id": "B"},
        },
    }
    assert _this_run_submitted_notional(run_state) == 7.0


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


def test_build_budget_reserve_effectiveness_rejects_non_finite_budget_fields():
    # Codex review, PR #654: "Reject non-finite retained budget fields". A
    # retained blocked artifact's budget fields are as untrusted as its
    # buy_candidates -- _num()'s float() conversion happily accepts "NaN"/
    # "Infinity" strings, so without sanitizing them first, this table
    # rendered literal $nan/$inf cells despite every other retained
    # financial field in this module already being guarded against the same
    # non-finite hazard.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "budget": {
                "daily_cap_usd": "NaN",
                "weekly_rollover_reserve_usd": "Infinity",
                "remaining_spend_today_usd": "-Infinity",
                "day_notional_usd": "NaN",
            },
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 3.5}],
        }
    ]
    report = build_budget_reserve_effectiveness(run_states)
    row = next(line for line in report.splitlines() if line.startswith("| 2026-07-24"))
    assert "nan" not in row.lower()
    assert "inf" not in row.lower()


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


def test_build_performance_snapshot_falls_through_malformed_generic_weekly_cap():
    # Codex review, PR #654 ("Fall through malformed generic weekly caps"):
    # a retained morning-family state carries a truthy but malformed
    # weekly_cap_usd ("bad") alongside a valid stream-specific
    # morning_weekly_cap_usd -- the old `budget.get("weekly_cap_usd") or
    # budget.get("weekly_notional_cap_usd")` chain short-circuited on the
    # malformed-but-truthy string and never reached the stream-specific
    # fallback, so the real $5 week notional rendered against a $0.00 cap
    # instead of the actual $60.00 one.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "budget": {
                "hypothetical_day_spend_usd": 5.0,
                "hypothetical_week_spend_usd": 5.0,
                "weekly_cap_usd": "bad",
                "morning_weekly_cap_usd": 60.0,
            },
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 5.0}],
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Week notional / cap | $5.00 / $60.00 |" in report


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


def test_iso_week_of_state_rejects_iso_week_disagreeing_with_trading_day():
    # Codex review, PR #654 ("Validate ISO weeks against the trading day"):
    # a syntactically valid iso_week that disagrees with a parseable
    # trading_day is a stale/mistyped field, not ground truth -- the
    # accumulator sorts by trading_day but resets/selects the latest week
    # from THIS return value, so trusting a wrong-but-valid iso_week could
    # reset to a fictitious earlier week and let a later, smaller retained
    # partial win as "the latest week" over the real current week's total.
    state = {
        "trading_day": "2026-07-21",  # a real Tuesday, ISO week (2026, 30)
        "iso_week": [2026, 1],  # syntactically valid, but the wrong week
    }
    assert _iso_week_of_state(state) == (2026, 30)


def test_iso_week_of_state_trusts_iso_week_when_trading_day_absent():
    # The cross-check only applies when trading_day itself parses to a
    # usable week -- iso_week alone is still trusted otherwise, exactly as
    # before this fix.
    state = {"iso_week": [2026, 1]}
    assert _iso_week_of_state(state) == (2026, 1)


def test_load_json_files_prefers_usable_claim_breakdown_over_malformed_one(tmp_path):
    # Codex review, PR #654 ("Prefer usable claim breakdowns during
    # reconciliation"): the candidate row carries a truthy but malformed
    # whole_share_claim_breakdown (a string, not the expected list), while
    # the matching broker outcome (same order_id) carries a VALID one
    # attributing $15 of this $20 order to the prior week (a Friday claim
    # surfaced on this Monday). Reserving claim_sources for the malformed
    # candidate breakdown first blocked the outcome's valid one from ever
    # being used via setdefault(), so the full $20 -- not just the
    # current week's real $5 share -- was booked into this week's
    # notional.
    run_state = tmp_path / "claim_breakdown.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",  # Monday, ISO week (2026, 30)
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": "not-a-list",
                    }
                ],
                "broker_outcomes": {
                    "AAA": {
                        "status": "submitted",
                        "submitted_amount_usd": 20.0,
                        "order_id": "X",
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-07-17", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_requires_usable_entry_in_claim_breakdown(tmp_path):
    # Codex review, PR #654 ("Require a usable entry in claim breakdowns"):
    # the candidate row carries a VALID whole_share_claim_breakdown
    # attributing $15 of this $20 order to the prior Friday, while the
    # matching broker outcome (same order_id) carries a nonempty but
    # malformed list ([1], no dict entries) -- _has_usable_claim_breakdown()
    # used to accept any nonempty list, so the outcome's registration
    # (a direct assignment, not setdefault) overwrote the candidate's
    # valid source. _current_week_claim_amount() then skips every non-dict
    # entry in [1] and attributes the FULL $20 to the current week instead
    # of the real $5 share.
    run_state = tmp_path / "malformed_outcome_breakdown_entries.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",  # Monday, ISO week (2026, 30)
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-07-17", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    }
                ],
                "broker_outcomes": {
                    "AAA": {
                        "status": "submitted",
                        "submitted_amount_usd": 20.0,
                        "order_id": "X",
                        "whole_share_claim_breakdown": [1],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_validates_claim_breakdown_entry_contents(tmp_path):
    # Codex review, PR #654 ("Validate claim-breakdown contents before
    # preferring them"): fresh evidence after the [1] fix is that a
    # malformed list such as [{}] still passes the predicate because it
    # contains a dict, even though that dict has no usable amount_usd.
    # _current_week_claim_amount() defaults a missing amount_usd to 0.0
    # via `float(item.get("amount_usd") or 0.0)`, so [{}] contributes $0
    # to accounted_usd while still being "processed" -- the whole
    # aggregate then falls into the unaccounted-remainder term and lands
    # on the current week anyway, discarding the candidate's own valid
    # prior-week breakdown that this malformed outcome breakdown
    # overwrote via direct assignment.
    run_state = tmp_path / "empty_dict_outcome_breakdown_entry.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",  # Monday, ISO week (2026, 30)
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-07-17", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    }
                ],
                "broker_outcomes": {
                    "AAA": {
                        "status": "submitted",
                        "submitted_amount_usd": 20.0,
                        "order_id": "X",
                        "whole_share_claim_breakdown": [{}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_rejects_boolean_claim_breakdown_amount(tmp_path):
    # Codex review, PR #654 ("Reject boolean claim-breakdown amounts"):
    # fresh evidence after the boolean-amount and claim-content fixes is
    # that _has_usable_claim_breakdown() bypassed _finite_float()'s
    # explicit boolean guard by calling a bare float() directly -- an
    # outcome breakdown entry with amount_usd: true converts cleanly to
    # 1.0, so a $20 order whose candidate assigns the full $20 to the
    # prior Friday is instead overwritten by the outcome's malformed
    # breakdown, attributing $19 (not $0) to the current week instead of
    # the real $5 share the valid candidate breakdown would have given.
    run_state = tmp_path / "boolean_outcome_breakdown_amount.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",  # Monday, ISO week (2026, 30)
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-07-17", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    }
                ],
                "broker_outcomes": {
                    "AAA": {
                        "status": "submitted",
                        "submitted_amount_usd": 20.0,
                        "order_id": "X",
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": True}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


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


def test_build_graded_vs_fixed_honors_null_primary_base_in_precheck():
    # Codex review, PR #654 ("Honor the secondary base in the blocked-state
    # precheck"): a post-change retained blocked state has
    # budget.base_amount_usd explicitly null (JSON has no way to
    # distinguish "field omitted" from "field explicitly nulled") but a
    # valid budget.base_buy_amount_usd=2. This earlier precheck still used
    # `dict.get(key, default)`, whose default is ignored for a present-but-
    # null key, so it wrongly `continue`d past this state even though
    # _require_base_amount() below now correctly resolves the fallback.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-08-23",
            "budget": {"base_amount_usd": None, "base_buy_amount_usd": 2.0},
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
        }
    ]
    report = build_graded_vs_fixed(run_states)
    assert "$4.00" in report


def test_build_graded_vs_fixed_coerces_malformed_ladder_container():
    # Codex review, PR #654 ("Coerce malformed ladder containers before
    # base lookup"): a truthy non-dict ladder value (e.g. a JSON array)
    # survives `rs.get("ladder") or {}` and reaches `ladder.get(...)`,
    # raising AttributeError -- and because dict.get()'s default argument
    # is evaluated eagerly, that AttributeError fired even though
    # budget.base_amount_usd was already present and valid, so the
    # malformed ladder never needed consulting at all.
    run_states = [
        {
            "trading_day": "2026-07-24",
            "budget": {"base_amount_usd": 2.0},
            "ladder": ["not", "a", "dict"],
            "buy_candidates": [{"planned_amount_usd": 2.0}],
        }
    ]
    # Must not raise -- this is the regression.
    report = build_graded_vs_fixed(run_states)
    assert "$2.00" in report


def test_build_weekly_dashboard_rejects_non_finite_base_amount_in_blocked_state():
    # Codex review, PR #654 ("Reject non-finite bases in retained blocked
    # states"): float("NaN")/float("Infinity") succeed, so a non-finite
    # budget.base_amount_usd on a retained blocked artifact with real
    # submission evidence silently rendered $nan/$inf totals instead of
    # failing fast the same way a genuinely non-numeric base amount
    # already does.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "counts": {"buy_candidates": 1},
            "budget": {
                "base_amount_usd": "NaN",
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
    with pytest.raises(ValueError, match="non-finite"):
        build_graded_vs_fixed(run_states)


def test_build_graded_vs_fixed_coerces_malformed_ladder_in_blocked_precheck():
    # Codex review, PR #654 ("Sanitize ladder before the blocked-state
    # precheck"): the earlier guard fixed inside _require_base_amount()
    # itself cannot help here -- THIS call site's own `rs.get("ladder") or
    # {}` runs first, on a blocked state missing budget.base_amount_usd
    # entirely, and `ladder.get(...)` on a truthy non-dict ladder raised
    # AttributeError and aborted the whole dashboard before
    # _require_base_amount() was ever reached.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "counts": {"buy_candidates": 1},
            "budget": {"hypothetical_day_spend_usd": 4.0, "week_notional_usd": 4.0, "weekly_cap_usd": 100.0},
            "ladder": ["not", "a", "dict"],
            "orders_submitted_count": 1,
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 4.0}],
            "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 4.0}},
        }
    ]
    # Must not raise -- this is the regression. (base_amount_usd is
    # genuinely absent everywhere, so this row is legitimately skipped
    # from the graded/fixed comparison -- the assertion is just that
    # skipping it doesn't crash the whole report.)
    build_graded_vs_fixed(run_states)


def test_build_graded_vs_fixed_preserves_pre_change_base_for_blocked_state():
    # Codex review, PR #654 ("Preserve the known pre-change base for
    # blocked states"): a retained blocked artifact predating
    # _BASE_AMOUNT_CHANGE_DATE ("2026-07-19") and missing both
    # budget.base_amount_usd and ladder.base_amount_usd is exactly the
    # case _require_base_amount() itself resolves via the documented $3
    # historical fallback -- the precheck's blanket `continue` dropped
    # this state's real submitted row from the comparison entirely instead
    # of ever reaching that fallback.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-10",  # predates the base-amount change
            "counts": {"buy_candidates": 1},
            "budget": {"hypothetical_day_spend_usd": 6.0, "week_notional_usd": 6.0, "weekly_cap_usd": 100.0},
            "orders_submitted_count": 1,
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 6.0}],
            "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 6.0}},
        }
    ]
    report = build_graded_vs_fixed(run_states)
    assert "$6.00" in report  # graded: the real submitted amount, not dropped


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


def test_load_json_files_falls_back_to_planned_amount_when_submitted_unusable(tmp_path):
    # Codex review, PR #654 ("Fall back when the submitted amount is
    # unusable"): the planned_amount_usd fallback only fired when
    # submitted_amount_usd was entirely ABSENT -- a present-but-malformed
    # value (e.g. "bad", not just non-finite) survived the previous
    # `float()` conversion attempt on itself, converting to $0.00 instead
    # of falling back to a perfectly valid planned_amount_usd.
    malformed = tmp_path / "malformed_submitted_amount.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-22",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": "bad",
                        "planned_amount_usd": 5.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([malformed])

    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $5.00 |" in report


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

    # Codex review, PR #1300 discussion_r3921082069 ("Drop malformed
    # candidate tickers before aggregation"), superseding this test's
    # original "$9.00 in report" assertion: merely NOT crashing was not
    # enough -- the PR #654 fix above stopped short of the crash but still
    # let the row aggregate under the ticker=None it was normalized to,
    # reporting a real $9 executed/planned notional for a candidate with no
    # usable ticker identity at all. It must now be dropped entirely
    # instead of counted.
    # Must not raise -- this is the regression.
    loaded = load_json_files([malformed])

    assert len(loaded) == 1
    assert loaded[0]["buy_candidates"] == []
    report = build_performance_snapshot(loaded)
    assert "$9.00" not in report
    assert "| Buy candidates | 0 |" in report


def test_load_json_files_skips_a_candidate_with_a_malformed_ticker(tmp_path):
    # Codex review, PR #1300 discussion_r3921082069 ("Drop malformed
    # candidate tickers before aggregation"): a retained blocked artifact
    # with a submitted-shaped candidate whose ticker is missing/non-string
    # (e.g. a JSON array) was normalized to ticker=None but still processed
    # -- the later identity/count/notional passes then counted None as a
    # real candidate and submission and added its full amount to the
    # dashboard, e.g. a "planned_amount_usd": 100 row with an array-valued
    # ticker reported as a genuine $100 order.
    malformed = tmp_path / "malformed_ticker.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-21",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": ["not", "a", "string"], "status": "submitted", "planned_amount_usd": 100.0},
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
    assert "$105.00" not in report
    assert "100.00" not in report


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


def test_load_json_files_ignores_unhashable_broker_outcome_ticker(tmp_path):
    # Codex review, PR #654 ("Normalize broker-outcome tickers before map
    # lookups"): a broker_outcomes entry whose truthy ticker is a JSON
    # array reaches id_less_candidate_identities.get(_ticker) (the
    # id-less-outcome pre-pass) and raises TypeError, aborting the entire
    # weekly report -- falls back to the JSON mapping key, matching the
    # existing "ticker absent" behavior.
    malformed = tmp_path / "unhashable_outcome_ticker.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-20",
                "broker_outcomes": {
                    "AAA": {
                        "status": "submitted",
                        "submitted_amount_usd": 8.0,
                        "ticker": ["not", "a", "str"],
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
    assert "$8.00" in report


def test_load_json_files_coerces_non_iterable_buy_candidates(tmp_path):
    # Codex review, PR #654 ("Coerce candidate containers before
    # normalization"): a structurally rejected artifact has real
    # submission evidence in broker_outcomes but a truthy non-iterable
    # buy_candidates value (e.g. an integer) -- `for row in ...` raises
    # TypeError before the container guard on the later synthetic-row
    # append site ever runs, aborting the entire weekly report.
    malformed = tmp_path / "noniterable_buy_candidates.json"
    malformed.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-21",
                "buy_candidates": 5,
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 12.0}},
            }
        ),
        encoding="utf-8",
    )

    # Must not raise -- this is the regression.
    loaded = load_json_files([malformed])

    assert len(loaded) == 1
    assert isinstance(loaded[0]["buy_candidates"], list)
    report = build_performance_snapshot(loaded)
    assert "$12.00" in report


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


def test_backfill_planned_amount_replaces_a_boolean_existing_value(tmp_path):
    # Codex review, PR #1300 discussion_r3920765651 ("Reject boolean amounts
    # during planned-notional backfill"): bool is a subclass of int in
    # Python, so float(True) succeeds as 1.0 -- a retained row's existing
    # planned_amount_usd of `true` was therefore treated as "already has a
    # valid amount" and never replaced by the real submitted amount,
    # rendering that order as $1 of planned spend instead of $7, exactly
    # like the non-finite-string case the sibling test above already
    # covers.
    boolean_existing = tmp_path / "boolean_existing_planned.json"
    boolean_existing.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-17",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 7.0,
                        "planned_amount_usd": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([boolean_existing])

    assert len(loaded) == 1
    row = next(r for r in loaded[0]["buy_candidates"] if r.get("ticker") == "AAA")
    assert row["planned_amount_usd"] == 7.0
    report = build_performance_snapshot(loaded)
    assert "$1.00" not in report


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


def test_load_scheduled_run_states_corrupt_json(tmp_path: Path, capsys):
    # Codex review ("Isolate malformed run states during loading"): a
    # single truncated/unreadable run_state.json previously aborted the
    # WHOLE weekly dashboard via load_json_files()'s all-or-nothing list
    # comprehension. It must now be skipped (with a stderr warning) so the
    # rest of the historical tree still produces a report -- not raised.
    scheduled_dir = tmp_path / "scheduled_runs"
    scheduled_dir.mkdir()
    (scheduled_dir / "run_state_corrupt.json").write_text("{invalid json")

    results = load_scheduled_run_states(tmp_path)

    assert results == []
    assert "run_state_corrupt.json" in capsys.readouterr().err


def test_load_scheduled_run_states_unreadable_file(tmp_path: Path, capsys):
    """A directory is created where a file is expected: unlike os.chmod(path, 0),
    this deterministically raises OSError on read regardless of platform or
    whether the test runs as root (both make chmod-based denial a no-op).

    Codex review ("Isolate malformed run states during loading"): this must
    be skipped like any other unreadable artifact, not abort the whole
    weekly dashboard build.
    """
    scheduled_dir = tmp_path / "scheduled_runs"
    scheduled_dir.mkdir()
    (scheduled_dir / "run_state_unreadable.json").mkdir()

    results = load_scheduled_run_states(tmp_path)

    assert results == []
    assert "run_state_unreadable.json" in capsys.readouterr().err


def test_load_json_files_rejects_boolean_claim_breakdown_entry_amount(tmp_path):
    # Codex review, PR #654 ("Reject booleans in every claim-breakdown
    # entry"): _current_week_claim_amount()'s per-entry loop accepted a
    # boolean amount_usd (bool is a subclass of int, so float(True)==1.0)
    # sitting alongside a genuinely valid entry, counting it into
    # accounted_usd instead of skipping it into the unaccounted remainder
    # the way every other unusable entry already is. A $20 order with a
    # valid $15 Friday (prior week) entry plus a boolean-amount Thursday
    # (also prior week) entry used to account $16 (15 + 1) and report
    # $4.00 for the current week instead of the real $5.00 (20 - 15).
    run_state = tmp_path / "boolean_claim_breakdown_entry.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",  # Monday, ISO week (2026, 30)
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-07-17", "amount_usd": 15.0},  # Friday, prior week
                            {"trading_day": "2026-07-16", "amount_usd": True},  # Thursday, prior week
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_keeps_deduplicated_row_amount_consistent(tmp_path):
    # Codex review, PR #654 ("Keep deduplicated row amounts consistent"): a
    # row folded onto an earlier duplicate's identity in the real-status
    # candidate loop (same ticker, same order_id) used to let its OWN
    # amount unconditionally overwrite amounts_by_ticker, even though the
    # retained keeper row's planned_amount_usd is left untouched -- the
    # candidate row and the notional total disagreed on which amount won.
    # Two rows share order_id "X" and the SAME "submitted" status rank; per
    # "Prefer later candidate corrections when status ranks tie" (a later
    # fix in this same series), an equal-rank tie now keeps the LATER row
    # ($7, treated as the correction) -- but candidate and notional must
    # still agree with EACH OTHER on which one that is.
    run_state = tmp_path / "duplicate_row_amount_consistency.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "planned_amount_usd": 5.0},
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "planned_amount_usd": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    assert len(loaded[0]["buy_candidates"]) == 1
    assert loaded[0]["buy_candidates"][0]["planned_amount_usd"] == 7.0
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $7.00 |" in report
    assert "$12.00" not in report


def test_load_json_files_deduplicates_adjustment_rows_separately(tmp_path):
    # Codex review, PR #654 ("Deduplicate adjustment candidate rows
    # separately"): two statusless adjustment-only rows sharing order_id
    # "Y" (no ordinary row involved at all) used to be routed entirely out
    # of the all-row dedup pass, since adjustment rows are deliberately
    # excluded from folding onto ordinary rows -- but that also meant two
    # adjustment rows for the SAME settlement correction never folded
    # together either, each surviving as its own candidate and doubling
    # the correction's contribution to day/week notional.
    run_state = tmp_path / "adjustment_rows_deduplicated_separately.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "Y",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    },
                    {
                        "ticker": "AAA",
                        "order_id": "Y",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    assert len(loaded[0]["buy_candidates"]) == 1


def test_load_json_files_materializes_ordinary_outcome_masked_by_adjustment_row(tmp_path):
    # Codex review, PR #654 ("Materialize ordinary outcomes masked by
    # adjustment rows"): the only retained candidate row for order_id "X"
    # is a settlement ADJUSTMENT, but the matching broker outcome for that
    # same order_id is an ORDINARY submission (e.g. the base order's own
    # placement never got a buy_candidates row of its own, only its later
    # settlement correction did). Unconditionally suppressing synthetic-row
    # creation whenever the order_id was "already represented" left
    # buy_candidates containing only the adjustment, so
    # build_graded_vs_fixed()/build_sector_matrix() (which read
    # buy_candidates directly) reported the adjustment's negative delta
    # and zero physical candidates instead of the real $100 ordinary order.
    run_state = tmp_path / "ordinary_outcome_masked_by_adjustment.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    }
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X", "submitted_amount_usd": 100.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    candidates = loaded[0]["buy_candidates"]
    assert len(candidates) == 2
    ordinary_rows = [row for row in candidates if not row.get("whole_share_settlement_adjustment")]
    assert len(ordinary_rows) == 1
    assert ordinary_rows[0]["ticker"] == "AAA"
    assert ordinary_rows[0]["submitted_amount_usd"] == 100.0


def test_build_performance_snapshot_rejects_negative_weekly_cap():
    # Codex review, PR #654 ("Reject negative weekly caps before selecting
    # them"): a malformed negative weekly_cap_usd (-100) is finite, so
    # _finite_float() alone accepts it, and being truthy it stops the `or`
    # chain immediately -- masking a valid weekly_notional_cap_usd fallback
    # the same way a non-finite value used to.
    run_states = [
        {
            "status": "blocked",
            "trading_day": "2026-07-24",
            "budget": {
                "hypothetical_day_spend_usd": 5.0,
                "hypothetical_week_spend_usd": 5.0,
                "weekly_cap_usd": -100.0,
                "weekly_notional_cap_usd": 50.0,
            },
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 5.0}],
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Week notional / cap | $5.00 / $50.00 |" in report


def test_load_json_files_sanitizes_boolean_candidate_fixed_base(tmp_path):
    # Codex review, PR #654 ("Sanitize candidate-level fixed bases in
    # retained rows"): _candidate_fixed_base_usd() already rejects a
    # nonpositive selected_base_amount_usd, but its own float(raw) call
    # accepts a JSON boolean as 1.0/0.0 -- a positive `true` survived that
    # check and rendered a $1 fixed baseline (masking the run's real $2
    # default) instead of falling back to it.
    run_state = tmp_path / "boolean_candidate_fixed_base.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "budget": {"base_amount_usd": 2.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "planned_amount_usd": 4.0,
                        "selected_base_amount_usd": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_graded_vs_fixed(loaded)
    assert "$2.00" in report  # falls back to the run's flat $2 base, not $1
    assert "$1.00" not in report


def test_load_json_files_recovers_blank_candidate_ticker_from_outcome(tmp_path):
    # Codex review, PR #654 ("Recover blank candidate tickers from matching
    # outcomes"): a retained candidate row carries ticker="" (empty, not
    # missing) alongside order_id "X". Its order-ID-matched broker outcome
    # (same order_id "X") carries the real ticker "AAA". Before the fix,
    # `not isinstance(ticker, str)` treated the empty string as already
    # valid, so the row's own blank ticker survived every downstream
    # ticker-keyed lookup -- reported under "Unclassified" instead of the
    # real AAA sector -- even though the outcome that proves its submission
    # names the real ticker right there.
    run_state = tmp_path / "blank_candidate_ticker.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [{"ticker": "", "order_id": "X", "planned_amount_usd": 5.0}],
                "broker_outcomes": {
                    "AAA": {"status": "submitted", "order_id": "X", "ticker": "AAA", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    candidates = loaded[0]["buy_candidates"]
    assert any(row.get("ticker") == "AAA" for row in candidates)
    assert not any(row.get("ticker") == "" for row in candidates)


def test_load_json_files_backfills_only_candidates_of_the_same_order_kind(tmp_path):
    # Codex review, PR #654 ("Backfill only candidates of the same order
    # kind"): the only retained candidate row for order_id "X" is a
    # settlement ADJUSTMENT (planned_amount_usd=-2.0), but the matching
    # broker outcome for that same order_id is an ORDINARY submission for
    # $100. Before the fix, the stale-row backfill ran unconditionally,
    # copying the $100 ordinary amount/status onto the adjustment row --
    # so once the ordinary outcome's own synthetic row was ALSO
    # materialized (a $100 order alongside a now-$100 "adjustment"), the
    # row-based graded/sector totals summed to $200 for a $98 net order.
    run_state = tmp_path / "backfill_same_order_kind.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    }
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X", "submitted_amount_usd": 100.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    candidates = loaded[0]["buy_candidates"]
    adjustment_rows = [row for row in candidates if row.get("whole_share_settlement_adjustment")]
    assert len(adjustment_rows) == 1
    assert adjustment_rows[0]["planned_amount_usd"] == -2.0
    assert adjustment_rows[0].get("submitted_amount_usd") != 100.0


def test_load_json_files_reconciles_adjustment_candidate_by_order_id(tmp_path):
    # Codex review, PR #654 ("Reconcile adjustment candidates with outcomes
    # by order ID"): a settlement-adjustment candidate row for order_id "X"
    # (a real-status row, no ordinary base order alongside it) plus a
    # composite-keyed adjustment outcome (mapping key "AAA#recovery", not
    # the bare ticker) for the SAME order_id "X" and the SAME -$2 amount.
    # The old adjustment order-ID fold only tracked identities established
    # by EARLIER adjustment outcomes, and the ticker-based fallback only
    # fires once amounts_by_ticker already holds a bare-ticker entry (which
    # only an ordinary base order populates) -- absent here, so the
    # outcome's -$2 landed on a FRESH bare-ticker identity instead of the
    # candidate row's own composite one, doubling the correction to -$4.
    run_state = tmp_path / "adjustment_candidate_reconciled_by_order_id.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    }
                ],
                "broker_outcomes": {
                    "AAA#recovery": {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "submitted_amount_usd": -2.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    assert loaded[0]["budget"]["day_notional_usd"] == -2.0


def test_load_json_files_gates_stale_amount_fallback_by_order_kind(tmp_path):
    # Codex review, PR #654 ("Gate stale amount fallback by order kind"):
    # the only retained candidate row for order_id "X" is a statusless
    # settlement ADJUSTMENT (planned_amount_usd=-2.0). Its order-ID-matched
    # broker outcome is an ORDINARY submission that OMITS its own amount.
    # Before the fix, the stale-amount fallback used the adjustment row's
    # own -$2 as the ordinary outcome's amount (exempted from the
    # negative-amount rejection because the FALLBACK CHECKED THE STALE
    # ROW'S OWN adjustment-ness, not the outcome's), so the ordinary
    # outcome's identity wrongly reported -$2 instead of $0/unresolved.
    run_state = tmp_path / "stale_amount_fallback_order_kind.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    }
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    assert loaded[0]["budget"]["day_notional_usd"] == 0.0


def test_load_json_files_deduplicates_orders_across_run_state_files(tmp_path):
    # Codex review, PR #654 ("Deduplicate retained orders across run-state
    # files"): two separate retained run_state files (e.g. a retained
    # blocked/preserved state and a later completed state for the same
    # run -- a supported artifact shape elsewhere in the repository) both
    # carry a candidate row for the SAME physical broker order_id "X".
    # Every dedup pass in this module operates on one file's own data at a
    # time, so both files' own amount/counts computation counted the same
    # $100 order independently, doubling the executed/day total and order
    # count for one physical order.
    earlier_state = tmp_path / "run_state_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "run_state_later.json"
    later_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-21",
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $100.00 |" in report
    assert "$200.00" not in report


def test_load_json_files_folds_reconciled_id_into_its_ambiguous_predecessor(tmp_path):
    # Codex review, PR #1300 discussion_r3920642635 ("Fold reconciled IDs
    # into their ambiguous predecessor"): an earlier variant of an explicit
    # run records a "potentially_submitted" row with NO order_id -- the
    # placement connector raised before returning one -- and a LATER variant
    # of the SAME run (same run_classification.run_id) reconciles that same
    # request to a filled order that finally HAS an id. Cross-state dedup is
    # keyed on order_id, so the ambiguous row was never registered and the
    # identified row matched nothing: both states survived and the dashboard
    # reported two run days, two candidates, two submissions and double the
    # notional for ONE physical order.
    earlier_state = tmp_path / "run_state_ambiguous.json"
    earlier_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1530ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "potentially_submitted", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "run_state_reconciled.json"
    later_state.write_text(
        json.dumps(
            {
                "status": "completed",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1530ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "filled", "order_id": "X", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $100.00 |" in report
    assert "$200.00" not in report


def test_load_json_files_counts_matching_candidate_and_outcome_as_one_placement(tmp_path):
    # Codex review, PR #1300 discussion_r3920155073 ("Count matching
    # candidate and outcome as one placement"): the earlier state below
    # carries BOTH an id-less "potentially_submitted" buy_candidates row for
    # AAA AND its matching broker_outcomes["AAA"] entry -- the ordinary shape
    # for one physical order (a candidate half plus the broker's own outcome
    # half). _note_ambiguous_placement() used to be invoked once per
    # representation, bumping the per-ticker count in
    # _ambiguous_placements_seen_this_state to 2 even though there is only
    # ONE order here. Promotion into
    # _ambiguous_placements_claimed_across_states only accepts count == 1,
    # so the double-count silently disabled the very correlation
    # test_load_json_files_folds_reconciled_id_into_its_ambiguous_predecessor
    # exercises above: the later reconciled state's identified row would
    # match nothing, and both states would survive, double-counting one
    # order's notional.
    earlier_state = tmp_path / "run_state_ambiguous_pair.json"
    earlier_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1530ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "potentially_submitted", "submitted_amount_usd": 100.0}
                ],
                "broker_outcomes": {
                    "AAA": {"status": "potentially_submitted", "submitted_amount_usd": 100.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "run_state_ambiguous_pair_reconciled.json"
    later_state.write_text(
        json.dumps(
            {
                "status": "completed",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1530ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "filled", "order_id": "X", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $100.00 |" in report
    assert "$200.00" not in report


def test_load_json_files_keeps_ambiguous_rows_of_different_runs_separate(tmp_path):
    # The correlation above must demand a SHARED explicit run identity: two
    # genuinely different runs that each placed an AAA order -- one still
    # ambiguous, one reconciled -- are two physical orders, and folding them
    # would erase real capital from the dashboard. Same ticker, same day,
    # DIFFERENT run_classification.run_id, so both must survive.
    ambiguous_state = tmp_path / "run_state_other_run_ambiguous.json"
    ambiguous_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1030ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "potentially_submitted", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    identified_state = tmp_path / "run_state_other_run_identified.json"
    identified_state.write_text(
        json.dumps(
            {
                "status": "completed",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1530ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "filled", "order_id": "X", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([ambiguous_state, identified_state])
    report = build_performance_snapshot(loaded)
    # Executed notional is the capital-preservation signal: both physical
    # orders must still be counted. (The candidate/submission COUNTS for an
    # id-less row follow this module's own pre-existing id-less accounting
    # and are deliberately not asserted here.)
    assert "| Executed notional | $200.00 |" in report


def test_load_json_files_keeps_two_ambiguous_placements_for_one_ticker_separate(tmp_path):
    # When ONE state holds two id-less ambiguous placements for the same
    # ticker, there is no safe way to tell which one a later identified row
    # reconciles, so no correlation may be registered at all -- the same "no
    # competing candidate it could be mistaken for" rule the id-less
    # matching pass already applies. Both ambiguous rows plus the later
    # identified order must survive rather than one being silently folded.
    ambiguous_state = tmp_path / "run_state_two_ambiguous.json"
    ambiguous_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1530ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "potentially_submitted", "submitted_amount_usd": 100.0},
                    {"ticker": "AAA", "status": "potentially_submitted", "submitted_amount_usd": 250.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    identified_state = tmp_path / "run_state_two_ambiguous_later.json"
    identified_state.write_text(
        json.dumps(
            {
                "status": "completed",
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1530ET"},
                "budget": {"weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "filled", "order_id": "X", "submitted_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([ambiguous_state, identified_state])
    report = build_performance_snapshot(loaded)
    # $100 + $250 ambiguous + $100 identified: every dollar still reported,
    # so nothing was folded into an ambiguous predecessor it may not belong
    # to. Distinct amounts make the sum unambiguous.
    assert "| Executed notional | $450.00 |" in report


def test_load_json_files_prefers_dated_claim_metadata_over_undated(tmp_path):
    # Codex review, PR #654 ("Preserve dated claim metadata over undated
    # replacements"): the candidate row for a $20 Monday order carries a
    # valid DATED prior-week claim breakdown (Friday, $15), but its
    # order-ID-matched broker outcome carries a usable but UNDATED
    # breakdown ($15, no trading_day). _has_usable_claim_breakdown()
    # accepts both, and the old direct assignment let the outcome's
    # undated entry unconditionally overwrite the candidate's dated one --
    # an undated entry is attributed to the artifact's OWN week by
    # _current_week_claim_amount(), so the dashboard reported the full $20
    # in the current week instead of the real $5 (20 - 15).
    run_state = tmp_path / "dated_vs_undated_claim_breakdown.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",  # Monday, ISO week (2026, 30)
                "budget": {"weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [
                            {"trading_day": "2026-07-17", "amount_usd": 15.0}  # Friday, prior week
                        ],
                    }
                ],
                "broker_outcomes": {
                    "AAA": {
                        "status": "submitted",
                        "order_id": "X",
                        "submitted_amount_usd": 20.0,
                        "whole_share_claim_breakdown": [{"amount_usd": 15.0}],  # no trading_day
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Week notional / cap | $5.00 / $100.00 |" in report


def test_load_json_files_deduplicates_adjustment_rows_across_run_state_files(tmp_path):
    # Codex review, PR #654 ("Deduplicate adjustment rows across state
    # variants"): two separate retained blocked run_state files both carry
    # the SAME settlement adjustment (ticker AAA, order_id "X", -$2). The
    # cross-file dedup pass exempted settlement adjustments from the
    # ordinary order_id namespace entirely (so an adjustment never folds
    # onto -- or is folded onto by -- the ordinary order it corrects), but
    # that also meant two files repeating the SAME adjustment never folded
    # onto EACH OTHER either, doubling the correction to -$4.
    earlier_state = tmp_path / "adjustment_run_state_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "adjustment_run_state_later.json"
    later_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-21",
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "status": "submitted",
                        "order_id": "X",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    total_day_notional = sum(
        state.get("budget", {}).get("day_notional_usd", 0.0) or 0.0 for state in loaded
    )
    assert total_day_notional == -2.0


def test_load_json_files_recomputes_totals_after_cross_state_dedup(tmp_path):
    # Codex review, PR #654 ("Recompute state totals after dropping
    # cross-file duplicates"): two NORMAL (non-blocked) completed run_state
    # files both carry a candidate row for the SAME $5 order (ticker AAA,
    # order_id "X"), each with its own pre-existing counts/budget already
    # baked in (as a real executor would write them). The cross-file
    # row-level dedup drops the later file's duplicate row, but the
    # counts.submitted/counts.buy_candidates/budget.day_notional_usd
    # recompute further below only runs for status == "blocked" -- a
    # normal completed state's pre-existing aggregates were left
    # untouched, so the later file still reported 1 submission and $5
    # executed for a row that no longer exists in its own buy_candidates.
    earlier_state = tmp_path / "recompute_run_state_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "recompute_run_state_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    # The later state's ENTIRE evidence (its one candidate row) was a
    # cross-file duplicate of a SAME-trading_day keeper -- a genuine variant
    # of the same physical run -- so it's dropped from the returned list
    # entirely rather than surviving with its totals zeroed out (Codex
    # review, PR #654: "Collapse state variants before counting run days";
    # "Keep distinct run days after order deduplication" -- collapsing is
    # still correct here since both states share trading_day "2026-07-20").
    assert len(loaded) == 1
    assert loaded[0]["trading_day"] == "2026-07-20"
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $5.00 |" in report


def test_load_json_files_adjusts_totals_for_outcome_only_duplicates(tmp_path):
    # Codex review, PR #654 ("Adjust totals for outcome-only duplicates"):
    # a later completed state represents the duplicate $5 order ONLY in
    # broker_outcomes, with no buy_candidates row of its own for it. The
    # cross-file dedup pass dropped that outcome without crediting the
    # removal (only candidate-row removals were tracked), so the later
    # state's own pre-baked counts.submitted/budget.day_notional_usd stayed
    # unchanged, still reporting a second submission and $10 executed for
    # one physical $5 order.
    earlier_state = tmp_path / "outcome_only_dup_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "outcome_only_dup_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 0, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    # The later state's ENTIRE evidence (its one outcome entry) was a
    # cross-file duplicate of a SAME-trading_day keeper -- a genuine variant
    # of the same physical run -- so it's dropped from the returned list
    # entirely (Codex review, PR #654: "Collapse state variants before
    # counting run days"; "Keep distinct run days after order
    # deduplication" -- collapsing is still correct here since both states
    # share trading_day "2026-07-20") rather than surviving with its totals
    # zeroed.
    assert len(loaded) == 1
    assert loaded[0]["trading_day"] == "2026-07-20"
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 1 |" in report
    assert "| Executed notional | $5.00 |" in report


def test_load_json_files_preserves_weekly_totals_across_same_day_state_variants(tmp_path):
    # Codex review, PR #654 ("Preserve weekly totals across same-day state
    # variants"): two run_state variants for the SAME trading day both
    # report order "X" (a $5 duplicate) and the same $5 week-to-date total
    # (week_notional_usd is cumulative, not row-local). The later variant
    # ALSO carries its own genuinely unique order "Y" ($3), so it survives
    # the cross-file dedup pass (a fully-duplicate variant is now dropped
    # entirely -- see "Collapse state variants before counting run days")
    # with only X removed. Subtracting X's own $5 from the already-correct
    # $5 cumulative week-to-date total would corrupt it to $0, even though
    # the retained earlier variant still supplies that real $5 to the week.
    earlier_state = tmp_path / "same_day_variant_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "same_day_variant_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "status": "submitted", "order_id": "Y", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["budget"]["day_notional_usd"] == 3.0
    assert later_loaded["budget"]["week_notional_usd"] == 5.0


def test_load_json_files_excludes_adjustment_rows_from_removed_submission_counts(tmp_path):
    # Codex review, PR #654 ("Exclude adjustment rows from removed-submission
    # counts"): a later completed state contains a genuine ordinary order Z
    # (its state's only real submission) plus a settlement adjustment X that
    # duplicates an earlier state's own adjustment. The stored
    # counts.submitted already excludes adjustments by design, but the
    # cross-file dedup pass incremented _removed_submitted_count for the
    # removed adjustment anyway, decrementing the later state's submitted
    # count from 1 (Z alone) to 0 even though Z itself was never touched.
    earlier_state = tmp_path / "adjustment_submitted_count_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 0, "submitted": 0},
                "budget": {"day_notional_usd": -2.0, "week_notional_usd": -2.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "adjustment_submitted_count_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 3.0, "week_notional_usd": 3.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "BBB", "order_id": "Z", "status": "submitted", "submitted_amount_usd": 5.0},
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    later_loaded = loaded[1]
    assert later_loaded["counts"]["submitted"] == 1
    assert later_loaded["counts"]["buy_candidates"] == 1
    assert later_loaded["budget"]["day_notional_usd"] == 5.0


def test_load_json_files_subtracts_amounts_only_for_placed_duplicate_rows(tmp_path):
    # Codex review, PR #654 ("Subtract amounts only for placed duplicate
    # rows"): a dry_run candidate's deterministic synthetic order_id repeats
    # across two same-day state variants, but its amount was never included
    # in either state's own executed notional (dry_run is not a real order
    # status). Crediting its amount toward _removed_amount anyway subtracted
    # a real dollar figure from the later state's $0.00 day_notional_usd,
    # rendering a negative executed total.
    earlier_state = tmp_path / "dry_run_dup_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 0, "submitted": 0},
                "budget": {"day_notional_usd": 0.0, "week_notional_usd": 0.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "DRY1", "status": "dry_run", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "dry_run_dup_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 3.0, "week_notional_usd": 3.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "DRY1", "status": "dry_run", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["budget"]["day_notional_usd"] == 3.0
    assert later_loaded["counts"]["submitted"] == 1


def test_load_json_files_credits_missing_outcome_contribution_after_candidate_removal(tmp_path):
    # Codex review, PR #654 ("Credit missing outcome contributions after
    # candidate removal"): a later state's duplicate candidate row for order
    # "X" has a stale, non-placement status ("selected") and no amount --
    # neither is credited when it's removed. But that same state's matching
    # broker_outcomes entry for "X" IS submitted and carries the real $5
    # amount. The outcome-side removal must still be free to credit that
    # missing count/amount, not be blocked just because a (non-contributing)
    # candidate row for the same order_id was already processed.
    earlier_state = tmp_path / "outcome_credits_missing_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "outcome_credits_missing_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "selected"},
                    {"ticker": "CCC", "order_id": "Z", "status": "submitted", "submitted_amount_usd": 2.0},
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["counts"]["submitted"] == 1
    assert later_loaded["budget"]["day_notional_usd"] == 2.0


def test_load_json_files_namespaces_cross_file_order_ids_by_broker(tmp_path):
    # Codex review, PR #654 ("Namespace cross-file order identities by
    # broker"): two states carry the SAME order_id "X" but for DIFFERENT
    # brokers -- order_id is only unique within one broker's own order
    # history, since this repo's adapters are broker-agnostic. Treating
    # order_id as globally unique removed the later broker's real, unrelated
    # $7 order as if it were a duplicate of the first broker's $5 order.
    earlier_state = tmp_path / "broker_namespace_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "broker_namespace_later.json"
    later_state.write_text(
        json.dumps(
            {
                "broker": "otherbroker",
                "trading_day": "2026-07-21",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    later_loaded = loaded[1]
    assert len(later_loaded["buy_candidates"]) == 1
    assert later_loaded["counts"]["submitted"] == 1
    assert later_loaded["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_corrects_duplicate_weekly_totals_across_iso_week_boundaries(tmp_path):
    # Codex review, PR #654 ("Correct duplicate weekly totals across
    # ISO-week boundaries"): a $5 order from Friday (ISO week 29) is
    # repeated in a Monday state (a NEW ISO week, 30) whose own baked
    # week_notional_usd is also $5 -- mistakenly attributing Friday's order
    # to Monday's fresh week-to-date total. Since the duplicate was claimed
    # by an EARLIER state in a DIFFERENT (not the same) ISO week, Monday's
    # week_notional_usd must have that contribution removed too, or
    # _latest_week_notional() reports $5 for a week whose only order was
    # just dropped as a duplicate.
    earlier_state = tmp_path / "iso_week_boundary_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-17",  # Friday, ISO week 29
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "iso_week_boundary_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",  # Monday, ISO week 30 -- a NEW week
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["budget"]["day_notional_usd"] == 3.0
    assert later_loaded["budget"]["week_notional_usd"] == 3.0


def test_state_week_notional_rejects_boolean_value():
    # Codex review, PR #654 ("Reject boolean weekly-notional values"): a
    # malformed week_notional_usd: true is accepted by a bare float() as
    # 1.0 (bool is an int subclass), so the chronologically latest state's
    # boolean value replaced an earlier real cumulative $100 total with $1
    # instead of being skipped like every other unusable latest value.
    run_states = [
        {
            "trading_day": "2026-07-20",  # Monday
            "budget": {"week_notional_usd": 100.0, "weekly_cap_usd": 500.0},
            "buy_candidates": [{"ticker": "AAA", "status": "submitted", "planned_amount_usd": 100.0}],
        },
        {
            "trading_day": "2026-07-21",  # Tuesday, same ISO week
            "budget": {"week_notional_usd": True, "weekly_cap_usd": 500.0},
            "buy_candidates": [{"ticker": "BBB", "status": "submitted", "planned_amount_usd": 5.0}],
        },
    ]
    report = build_performance_snapshot(run_states)
    assert "| Week notional / cap | $100.00 / $500.00 |" in report


def test_load_json_files_collapses_fully_duplicate_state_variants(tmp_path):
    # Codex review, PR #654 ("Collapse state variants before counting run
    # days"): a preserved blocked state and a later completed run_state are
    # both variants of the SAME physical run and share the same $5 order.
    # Row-level cross-file dedup removes the duplicate row from the later
    # file, but the later state dict itself used to survive in the
    # returned list -- still counting as a SECOND run day via
    # len(run_states) and rendering a duplicate per-run row in
    # build_graded_vs_fixed(), even though every one of its rows was just
    # proven to be a duplicate of the earlier state's own evidence.
    earlier_state = tmp_path / "fully_duplicate_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "fully_duplicate_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    assert loaded[0]["trading_day"] == "2026-07-20"
    report = build_performance_snapshot(loaded)
    assert "| Run days | 1 |" in report


def test_load_json_files_preserves_distinct_run_when_only_one_side_has_a_run_id(tmp_path):
    # Codex review, PR #1300 discussion_r3920988299 ("Preserve distinct
    # same-stream runs after order deduplication"): the identified state
    # below carries a real run_classification.run_id -- positive evidence of
    # a SPECIFIC scheduled execution -- while the later, coincidentally
    # same-day, same-ticker state carries NONE at all. _states_share_run_
    # identity()'s trading_day fallback only compares two PRESENT ids (or
    # falls back to trading_day when BOTH are absent); it silently ignores
    # an id present on only one side, so these two states -- one identified,
    # one not -- used to read as "one run," and once row-level dedup removed
    # every row from the run_id-less state (it duplicated the identified
    # state's own $5 order), that whole state was dropped from the returned
    # list entirely instead of surviving as its own distinct zero-order
    # execution, losing whatever coverage/budget data it carried and
    # understating the true run count.
    identified_state = tmp_path / "asymmetric_run_id_identified.json"
    identified_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "scheduled_20260720_1030ET"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    unidentified_state = tmp_path / "asymmetric_run_id_unidentified.json"
    unidentified_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([identified_state, unidentified_state])
    # Both states survive: the run_id-less one is a distinct (now
    # zero-order) execution, not an empty husk of the identified one.
    assert len(loaded) == 2


def test_load_json_files_preserves_state_with_no_orders_at_all(tmp_path):
    # A state that genuinely never had any candidate/outcome evidence (a
    # real "nothing to buy today" run) must NOT be collapsed by the
    # cross-file dedup pass -- it never contributed a row to dedup against
    # in the first place, so it's not a "duplicate variant" of anything.
    empty_state = tmp_path / "genuinely_empty.json"
    empty_state.write_text(
        json.dumps({"trading_day": "2026-07-20", "counts": {"buy_candidates": 0, "submitted": 0}}),
        encoding="utf-8",
    )
    other_state = tmp_path / "other_day.json"
    other_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-21",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "X", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([empty_state, other_state])
    assert len(loaded) == 2


def test_load_json_files_namespaces_removal_credits_by_adjustment_kind(tmp_path):
    # Codex review, PR #654 ("Namespace removal credits by adjustment
    # kind"): two same-day state variants each carry BOTH a submitted $100
    # ordinary order and its own -$2 settlement adjustment under the SAME
    # order_id. Removal of the later ordinary row credits $100, and the
    # distinct adjustment sharing that same bare order_id must credit its
    # own -$2 too -- crediting only $100 left the -$2 correction still
    # baked into the later state's own $98 day total, uncounted.
    earlier_state = tmp_path / "shared_order_id_adjustment_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 98.0, "week_notional_usd": 98.0, "weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 100.0},
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "shared_order_id_adjustment_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 101.0, "week_notional_usd": 101.0, "weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 100.0},
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    },
                    {"ticker": "CCC", "order_id": "Z", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["budget"]["day_notional_usd"] == 3.0


def test_load_json_files_gates_removed_outcome_amounts_on_placement_status(tmp_path):
    # Codex review, PR #654 ("Gate removed outcome amounts on placement
    # status"): two state variants contain the SAME generated dry_run
    # candidate AND its matching dry_run broker outcome. The candidate-side
    # removal correctly credits nothing (dry_run isn't a real order
    # status), but without this fix the OUTCOME-side removal still added
    # its own submitted_amount_usd to _removed_amount, turning a zero
    # day_notional_usd into a negative one.
    earlier_state = tmp_path / "dry_run_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 0, "submitted": 0},
                "budget": {"day_notional_usd": 0.0, "week_notional_usd": 0.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "DRY1", "status": "dry_run", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {"status": "dry_run", "order_id": "DRY1", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "dry_run_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 3.0, "week_notional_usd": 3.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "DRY1", "status": "dry_run", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
                "broker_outcomes": {
                    "AAA": {"status": "dry_run", "order_id": "DRY1", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["budget"]["day_notional_usd"] == 3.0


def test_load_json_files_avoids_reapplying_positive_settlement_adjustments(tmp_path):
    # Codex review, PR #654 ("Avoid reapplying positive settlement
    # adjustments"): a retained blocked state's own reported
    # week_notional_usd ($102) already includes its +$2 settlement
    # correction on top of an earlier $100 cumulative total. Flooring
    # against that already-corrected $102 and then adding the +$2
    # correction again normalized it to $104 instead of the real $102.
    earlier_state = tmp_path / "positive_adjustment_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",  # Monday
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 100.0, "week_notional_usd": 100.0, "weekly_cap_usd": 500.0},
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 100.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "positive_adjustment_later.json"
    later_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-21",  # Tuesday, same ISO week
                "budget": {"weekly_cap_usd": 500.0, "week_notional_usd": 102.0},
                "buy_candidates": [
                    {
                        "ticker": "BBB",
                        "order_id": "ADJ1",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": 2.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    later_loaded = next(state for state in loaded if state["trading_day"] == "2026-07-21")
    assert later_loaded["budget"]["week_notional_usd"] == 102.0


def test_load_json_files_reconciles_outcome_amounts_before_crediting_removals(tmp_path):
    # Codex review, PR #654 ("Reconcile outcome amounts before crediting
    # removals"): a later state's duplicate order "X" is represented by
    # BOTH a candidate row (planned at $5) and a broker_outcomes entry
    # (confirmed submitted at $7) -- the two disagree. Crediting from the
    # candidate row (processed first) used the $5 estimate and marked the
    # order as already credited, permanently blocking the outcome branch
    # from ever supplying the real $7 actually baked into the state's own
    # day_notional_usd. With a unique $3 order "Y" alongside and a stored
    # day_notional_usd of $10 (7 + 3), removing only $5 left the retained
    # state reporting $5 instead of the correct $3 (Y's own amount).
    earlier_state = tmp_path / "reconcile_outcome_amount_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "reconcile_outcome_amount_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 10.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "planned_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X", "submitted_amount_usd": 7.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["budget"]["day_notional_usd"] == 3.0


def test_merge_partial_state_into_keeper_rebuilds_weekly_total_without_source_week_fields(tmp_path):
    # Codex review, PR #1300 discussion_r3921019928 ("Rebuild weekly totals
    # when merging a partial variant"): source has surviving unique rows
    # (this call moves them) and a real day_notional_usd, but reports
    # NEITHER week_notional_usd NOR hypothetical_week_spend_usd of its own,
    # so _state_week_notional(source) returns None. The keeper's OLDER
    # cumulative weekly total used to be left completely unchanged in this
    # case, even though the source's own day-notional contribution (the
    # same figure the function adds to the keeper's DAY total a few lines
    # later) is genuinely new capital for the week -- understating the
    # week's true spend by exactly that contribution.
    keeper = {
        "trading_day": "2026-07-20",
        "counts": {"buy_candidates": 1, "submitted": 1},
        "budget": {"day_notional_usd": 5.0, "week_notional_usd": 20.0, "weekly_cap_usd": 100.0},
        "buy_candidates": [
            {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
        ],
    }
    source = {
        "trading_day": "2026-07-20",
        "counts": {"buy_candidates": 1, "submitted": 1},
        # Deliberately NO week_notional_usd/hypothetical_week_spend_usd.
        "budget": {"day_notional_usd": 3.0, "weekly_cap_usd": 100.0},
        "buy_candidates": [
            {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0}
        ],
    }

    _merge_partial_state_into_keeper(source, keeper)

    # Day total still merges as before (sanity check this is the same call).
    assert keeper["budget"]["day_notional_usd"] == pytest.approx(8.0)
    # Weekly total must now reflect the moved $3 order too: 20.0 (prior
    # cumulative) + 3.0 (source's surviving row-local contribution), not
    # the stale 20.0 the missing-week-fields fallback used to leave behind.
    assert keeper["budget"]["week_notional_usd"] == pytest.approx(23.0)


def test_load_json_files_merges_later_fill_amount_into_keeper(tmp_path):
    # Codex review, PR #654 ("Merge later fill amounts into the retained
    # order"): an earlier retained blocked state has provisional evidence
    # for order "X" at $5. A later completed state carries the SAME order
    # "X" with the authoritative $7 fill and no other evidence, so it's
    # fully absorbed as a cross-file duplicate and dropped entirely.
    # Simply discarding the later state after adjusting only its own
    # totals never propagated the $7 correction back onto the retained
    # keeper row, so the snapshot kept reporting the stale $5 forever.
    earlier_state = tmp_path / "merge_fill_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "merge_fill_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0
    assert keeper["budget"]["week_notional_usd"] == 7.0


def test_load_json_files_includes_account_in_cross_state_order_identities(tmp_path):
    # Codex review, PR #654 ("Include the account in cross-state order
    # identities"): two states share the SAME broker but carry DIFFERENT
    # account_number values, and both report order_id "X" -- order_id is
    # only unique WITHIN one account's own order history, since two
    # accounts at the same broker can independently issue the same
    # account-local order_id. Treating order_id as unique per broker alone
    # removed the second account's real, unrelated $7 order as if it were
    # a duplicate of the first account's $5 order.
    earlier_state = tmp_path / "account_scope_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "111",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "account_scope_later.json"
    later_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "222",
                "trading_day": "2026-07-21",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert len(later_loaded["buy_candidates"]) == 1
    assert later_loaded["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_falls_back_to_candidate_when_outcome_lacks_amount(tmp_path):
    # Codex review, PR #654 ("Fall back to the candidate when the outcome
    # lacks an amount"): a later state's duplicate candidate for order "X"
    # has a usable $5 amount, and its matching broker outcome IS placed
    # but omits submitted_amount_usd entirely. Deferring unconditionally
    # to the outcome (the prior round's fix) left NEITHER side crediting
    # anything, so the duplicate's $5 stayed baked into the later state's
    # day_notional_usd alongside a unique $3 order "Y".
    earlier_state = tmp_path / "outcome_no_amount_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "outcome_no_amount_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "X"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["budget"]["day_notional_usd"] == 3.0


def test_load_json_files_does_not_merge_planned_fallback_into_keeper(tmp_path):
    # Codex review, PR #654 ("Do not merge planned fallbacks as authoritative
    # fills"): an earlier keeper has a CONFIRMED $7 fill for order "X". A
    # later, fully-duplicate state's only representation of "X" is a
    # candidate row with no submitted_amount_usd of its own -- only a $5
    # planned_amount_usd estimate. That estimate is the right figure to
    # subtract from the later state's own (now-dropped) totals, but it must
    # never overwrite the keeper's confirmed $7 with a stale pre-placement
    # guess.
    earlier_state = tmp_path / "planned_fallback_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "planned_fallback_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "planned_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0
    assert keeper["budget"]["week_notional_usd"] == 7.0


def test_load_json_files_updates_keeper_outcome_when_merging_later_fill(tmp_path):
    # Codex review, PR #654 ("Update the keeper outcome when merging a later
    # fill"): the retained blocked keeper carries BOTH a $5 candidate row
    # and its own matching $5 broker_outcomes entry for order "X". A later
    # duplicate confirms the real $7 fill and is fully absorbed. Updating
    # only the keeper's candidate row left its own stale $5 outcome entry
    # behind, and the blocked-state amount reconciliation further down
    # treats broker_outcomes as authoritative -- recomputing day_notional_usd
    # back down to $5 despite the merged $7 candidate.
    earlier_state = tmp_path / "keeper_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "budget": {"weekly_cap_usd": 100.0},
                "orders_submitted_count": 1,
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {"order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "keeper_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["broker_outcomes"]["AAA"]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_folds_id_less_outcomes_with_removed_candidate(tmp_path):
    # Codex review, PR #654 ("Fold ID-less outcomes with their removed
    # candidate"): the later state's duplicate candidate for order "X" is
    # removed by cross-file dedup, but its matching same-state broker_outcomes
    # entry has NO order_id of its own -- so it bypasses cross-file dedup
    # entirely and would otherwise survive to be synthesized back into a
    # fresh candidate, double-counting the same $5 order.
    earlier_state = tmp_path / "id_less_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "id_less_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-20",
                "budget": {"weekly_cap_usd": 100.0},
                "orders_submitted_count": 1,
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {"AAA": {"status": "submitted", "submitted_amount_usd": 5.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert len(keeper["buy_candidates"]) == 1
    assert keeper["budget"]["day_notional_usd"] == 5.0


def test_load_json_files_applies_cross_week_fill_correction_to_keeper_week_total(tmp_path):
    # Codex review, PR #654 ("Apply cross-week fill corrections to the keeper
    # total"): the keeper is retained in one ISO week (Friday 2026-07-17) at
    # a provisional $5, and a later duplicate landing in the NEXT ISO week
    # (Monday 2026-07-20) confirms the real $7 fill before being fully
    # absorbed. The correction belongs to the keeper's own week regardless of
    # which week it was learned in, so the keeper's week_notional_usd must
    # also move from $5 to $7, not just its day_notional_usd.
    earlier_state = tmp_path / "cross_week_fill_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-17",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "cross_week_fill_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    # The later (Monday) state is NOT collapsed away even though every one
    # of its rows was removed as a cross-file duplicate -- it belongs to a
    # DIFFERENT trading_day than its keeper, so it's still its own distinct
    # run day, just with zero orders after the correction (Codex review, PR
    # #654: "Keep distinct run days after order deduplication").
    assert len(loaded) == 2
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0
    assert keeper["budget"]["week_notional_usd"] == 7.0
    later_loaded = loaded[1]
    assert later_loaded["trading_day"] == "2026-07-20"
    assert later_loaded["buy_candidates"] == []


def test_load_json_files_prefers_authoritative_duplicate_outcome_amount(tmp_path):
    # Codex review, PR #654 ("Prefer the authoritative duplicate outcome
    # amount"): the later state has TWO broker_outcomes entries for the same
    # order "X" -- a provisional $5 "submitted" entry followed by the
    # authoritative $7 "filled" entry. A first-writer-wins lookup would defer
    # the candidate's removal credit and the keeper merge to whichever entry
    # dict iteration happens to reach first, which can be the stale $5 one.
    earlier_state = tmp_path / "authoritative_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 3.0, "week_notional_usd": 3.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 3.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "authoritative_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA_submitted": {"order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    "AAA_filled": {"order_id": "X", "status": "filled", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_merges_later_claim_metadata_into_keeper(tmp_path):
    # Codex review, PR #654 ("Merge later claim metadata into the retained
    # order"): a later duplicate reports the SAME $5 amount as the keeper
    # (so the amount-equality early return would previously skip everything)
    # but carries a dated whole_share_claim_breakdown the keeper's own row
    # lacks. The claim metadata must still propagate even though the dollar
    # amount itself doesn't change.
    earlier_state = tmp_path / "claim_metadata_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "claim_metadata_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 5.0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["whole_share_claim_breakdown"] == [
        {"trading_day": "2026-07-17", "amount_usd": 5.0}
    ]


def test_load_json_files_prefers_later_outcome_on_status_rank_tie(tmp_path):
    # Codex review, PR #654 ("Prefer the later outcome when status ranks
    # tie"): the later state has TWO "filled" broker_outcomes entries for
    # order "X" -- an earlier-written $5 one and a corrected $7 one. A
    # strict `>` rank comparison keeps whichever entry is encountered first
    # since neither outranks the other; the later, corrected entry must win.
    earlier_state = tmp_path / "tie_break_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 3.0, "week_notional_usd": 3.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 3.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "tie_break_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA_first": {"order_id": "X", "status": "filled", "submitted_amount_usd": 5.0},
                    "AAA_second": {"order_id": "X", "status": "filled", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_bases_keeper_delta_on_confirmed_outcome(tmp_path):
    # Codex review, PR #654 ("Base keeper deltas on its confirmed outcome"):
    # the keeper candidate row only has a stale planned_amount_usd ($5), but
    # its own matching broker_outcomes entry already confirms $7 -- and the
    # keeper's budget.day_notional_usd already reflects that confirmed $7.
    # A later duplicate corrects the fill to $8. The delta must be computed
    # against the ALREADY-CONFIRMED $7 (yielding a $1 delta -> $8 total),
    # not against the stale $5 planned estimate (which would wrongly yield
    # a $3 delta -> $10 total).
    earlier_state = tmp_path / "confirmed_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "planned_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {"order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "confirmed_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 8.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["budget"]["day_notional_usd"] == 8.0


def test_load_json_files_preserves_unrelated_id_less_same_ticker_outcome(tmp_path):
    # Codex review, PR #654 ("Preserve unrelated ID-less same-ticker
    # outcomes"): the later state has TWO candidates for ticker "AAA" -- one
    # duplicate order "X" (removed by cross-file dedup) and a genuine second,
    # still-unplaced order for the SAME ticker whose only placement evidence
    # is its own ID-less broker_outcomes entry. Both broker_outcomes entries
    # share the "AAA" ticker; a ticker-only membership check (rather than a
    # one-to-one association with the single removed duplicate) drops BOTH,
    # discarding the genuine second order's only evidence.
    earlier_state = tmp_path / "unrelated_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "unrelated_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 14.0, "week_notional_usd": 14.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "AAA", "status": "selected"},
                ],
                "broker_outcomes": {
                    "AAA_1": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0},
                    "AAA_2": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 9.0},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert len(later_loaded["buy_candidates"]) == 1
    assert later_loaded["buy_candidates"][0].get("order_id") is None
    assert list(later_loaded["broker_outcomes"].keys()) == ["AAA_2"]
    assert later_loaded["budget"]["day_notional_usd"] == 9.0


def test_load_json_files_preserves_dated_keeper_claim_metadata(tmp_path):
    # Codex review, PR #654 ("Preserve dated keeper claim metadata"): the
    # keeper already carries a DATED whole_share_claim_breakdown. A later
    # duplicate has a usable but UNDATED breakdown. An unconditional
    # overwrite would replace the keeper's precise claim-week attribution
    # with the less precise undated one; the dated breakdown must survive.
    earlier_state = tmp_path / "dated_claim_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 5.0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "dated_claim_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_claim_breakdown": [{"amount_usd": 5.0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["whole_share_claim_breakdown"] == [
        {"trading_day": "2026-07-17", "amount_usd": 5.0}
    ]


def test_load_json_files_matches_composite_id_less_outcome_to_sequenced_candidate(tmp_path):
    # Codex review, PR #654 ("Match composite ID-less outcomes to sequenced
    # candidates"): a single state holds TWO id-less candidate rows for the
    # same ticker "AAA" ($5, sequenced as the bare "AAA" identity, and $7,
    # sequenced as "AAA:candidate#1"). Their matching broker_outcomes
    # entries are stored under a BARE key ("AAA") and a COMPOSITE retry key
    # ("AAA#retry") -- the bare-keyed outcome resolves directly, but the
    # composite-keyed one previously fell through the pre-pass unmatched
    # (its identity can never literally equal a sequenced pool entry) and
    # minted a brand-new THIRD identity/synthetic row, reporting 3
    # submissions worth $19 instead of the true 2 orders worth $12.
    state_path = tmp_path / "composite_id_less_outcome.json"
    state_path.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "status": "blocked",
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "AAA", "status": "submitted"},
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0},
                    "AAA#retry": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([state_path])
    assert len(loaded) == 1
    state = loaded[0]
    assert len(state["buy_candidates"]) == 2
    assert state["counts"]["submitted"] == 2
    assert state["budget"]["day_notional_usd"] == 12.0


def test_load_json_files_matches_id_less_outcome_by_amount_not_dict_order(tmp_path):
    # Codex review, PR #654 ("Match ID-less outcomes to the removed
    # candidate"): the later state's broker_outcomes dict lists the GENUINE
    # second order's ID-less outcome ($9) BEFORE the removed duplicate's own
    # ID-less outcome ($5) -- the opposite of insertion order in the earlier
    # "preserves unrelated" test. A positional/count-only match would
    # consume the first entry encountered (the genuine $9 one) instead of
    # the one that actually belongs to the removed $5 duplicate. Matching by
    # amount must pick the correct one regardless of dict order.
    earlier_state = tmp_path / "amount_match_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "amount_match_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 14.0, "week_notional_usd": 14.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "AAA", "status": "selected"},
                ],
                "broker_outcomes": {
                    "AAA_genuine": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 9.0},
                    "AAA_duplicate": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert list(later_loaded["broker_outcomes"].keys()) == ["AAA_genuine"]
    assert later_loaded["budget"]["day_notional_usd"] == 9.0


def test_load_json_files_credits_removed_id_less_outcomes_to_state_totals(tmp_path):
    # Codex review, PR #654 ("Credit removed ID-less outcomes to state
    # totals"): the removed duplicate candidate for order "X" has a
    # NON-real status ("selected") and so never credited anything itself,
    # but its matching ID-less broker_outcomes entry IS submitted with a
    # real $5 amount -- the only placement evidence for that order. Dropping
    # it without crediting the removal left the later state's baked-in
    # counts.submitted/day_notional_usd still including that $5 alongside
    # a genuine $3 order "Y".
    earlier_state = tmp_path / "credit_id_less_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "credit_id_less_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "selected", "planned_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert len(later_loaded["buy_candidates"]) == 1
    assert later_loaded["broker_outcomes"] == {}
    assert later_loaded["counts"]["submitted"] == 1
    assert later_loaded["budget"]["day_notional_usd"] == 3.0


def test_load_json_files_recomputes_keeper_week_attribution_on_metadata_change(tmp_path):
    # Codex review, PR #654 ("Recompute keeper week attribution when claim
    # metadata changes"): the keeper is a completed Monday state reporting a
    # $5 order fully attributed to Monday's own week (no breakdown). A later,
    # fully-duplicate variant reports the SAME $5 amount but adds a DATED
    # claim_breakdown attributing it entirely to the prior Friday -- a
    # DIFFERENT ISO week. Even though the dollar amount is unchanged, the
    # keeper's week_notional_usd must drop to $0 (the claim no longer
    # belongs to Monday's week), while day_notional_usd -- real capital
    # deployed on Monday regardless of which week it's attributed to --
    # stays at $5.
    earlier_state = tmp_path / "week_attribution_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "week_attribution_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 5.0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["budget"]["day_notional_usd"] == 5.0
    assert keeper["budget"]["week_notional_usd"] == 0.0


def test_load_json_files_rejects_negative_ordinary_fill_correction(tmp_path):
    # Codex review, PR #654 ("Reject negative ordinary fill corrections"):
    # a corrupted later variant reports submitted_amount_usd: -2 for
    # duplicate ORDINARY order "X" (not a settlement adjustment), while the
    # keeper has a confirmed $5 fill. Crediting/merging that negative amount
    # would rewrite the keeper's retained order and cumulative totals to
    # -$2. The negative amount must be treated as unusable for an ordinary
    # order, leaving the keeper's real $5 fill untouched.
    earlier_state = tmp_path / "negative_fill_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "negative_fill_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 1.0, "week_notional_usd": 1.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": -2.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 5.0
    assert keeper["budget"]["day_notional_usd"] == 5.0
    assert keeper["budget"]["week_notional_usd"] == 5.0


def test_load_json_files_preserves_stronger_keeper_fill(tmp_path):
    # Codex review, PR #654 ("Preserve stronger keeper fills across state
    # variants"): the keeper already has a CONFIRMED "filled" $7 amount
    # (both candidate and outcome). A later duplicate carries only a
    # lower-ranked "submitted" $5 -- a stale, provisional confirmation that
    # hasn't caught up. It must not overwrite the keeper's stronger fill.
    earlier_state = tmp_path / "stronger_fill_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 7.0}
                ],
                "broker_outcomes": {
                    "AAA": {"order_id": "X", "status": "filled", "submitted_amount_usd": 7.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "stronger_fill_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["broker_outcomes"]["AAA"]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_excludes_id_less_adjustment_from_submission_credit(tmp_path):
    # Codex review, PR #654 ("Exclude ID-less settlement adjustments from
    # submission credits"): the matched ID-less outcome is a PLACED
    # settlement adjustment, and its removed candidate never credited
    # anything (non-real "selected" status). The state also has one genuine
    # new order "Y". A settlement adjustment is never counted toward
    # counts.submitted by any other reconciliation path in this module --
    # crediting it here too would wrongly reduce the submitted count from 1
    # (Y only) to 0.
    earlier_state = tmp_path / "id_less_adjustment_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 0},
                "budget": {"day_notional_usd": -2.0, "week_notional_usd": -2.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "whole_share_settlement_adjustment": True,
                        "submitted_amount_usd": -2.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "id_less_adjustment_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 1},
                "budget": {"day_notional_usd": 1.0, "week_notional_usd": 1.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "selected",
                        "whole_share_settlement_adjustment": True,
                        "planned_amount_usd": -2.0,
                    },
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
                "broker_outcomes": {
                    "AAA_adj": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": -2.0,
                        "whole_share_settlement_adjustment": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["counts"]["submitted"] == 1
    assert later_loaded["budget"]["day_notional_usd"] == 3.0


def test_load_json_files_merges_claim_metadata_from_matched_id_less_outcome(tmp_path):
    # Codex review, PR #654 ("Merge claim metadata from matched ID-less
    # outcomes"): the removed duplicate candidate for order "X" has a
    # non-real "selected" status, so its own row never credited or merged
    # anything. Its matched ID-less outcome IS submitted with the SAME $5
    # amount but carries a DATED claim_breakdown attributing it to the
    # prior Friday -- a different ISO week than the keeper's own Monday.
    # That claim metadata must reach the keeper (and recompute its week
    # attribution), not just get silently dropped with the outcome.
    earlier_state = tmp_path / "id_less_claim_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "id_less_claim_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 0},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "selected", "planned_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 5.0}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["budget"]["day_notional_usd"] == 5.0
    assert keeper["budget"]["week_notional_usd"] == 0.0


def test_load_json_files_credits_id_less_removal_against_cross_week_total(tmp_path):
    # Codex review, PR #654 ("Credit ID-less removals against cross-week
    # totals"): the keeper is a Friday order; the later Monday state's
    # duplicate candidate is "selected" (never credited itself) and its
    # matched ID-less outcome supplies the ONLY placement evidence ($5,
    # submitted). Monday's own day_notional_usd correctly zeroes, but its
    # week_notional_usd must ALSO drop -- the removed order belongs to
    # Friday's week, not Monday's -- or Monday's week total keeps reporting
    # prior-week capital as its own.
    earlier_state = tmp_path / "id_less_cross_week_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-17",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "id_less_cross_week_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 0},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "selected", "planned_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["trading_day"] == "2026-07-20"
    assert later_loaded["budget"]["day_notional_usd"] == 0.0
    assert later_loaded["budget"]["week_notional_usd"] == 0.0


def test_load_json_files_prefers_filled_candidate_row_when_folding_duplicates(tmp_path):
    # Codex review, PR #654 ("Prefer filled candidate rows when folding
    # duplicates"): a SINGLE retained blocked state has two candidate rows
    # sharing order_id "X" -- a provisional "submitted" $5 row followed by a
    # corrected "filled" $7 row -- and no broker_outcomes entry to supply
    # the correction from elsewhere. Picking the FIRST real-status row
    # (rather than the strongest-ranked one) kept the stale $5 and
    # discarded the corrected $7.
    run_state = tmp_path / "filled_over_submitted_duplicate.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-08-23",
                "buy_candidates": [
                    {"ticker": "B", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "B", "order_id": "X", "status": "filled", "submitted_amount_usd": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    assert len(loaded[0]["buy_candidates"]) == 1
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $7.00 |" in report


def test_load_json_files_matches_sole_id_less_fill_despite_amount_correction(tmp_path):
    # Codex review, PR #654 ("Match sole ID-less fills despite amount
    # corrections"): the later state's removed duplicate candidate for "X"
    # estimated $5 (never itself credited -- non-real "selected" status),
    # and its SOLE ID-less broker_outcomes entry (the only one for ticker
    # "AAA" in this state) confirms a DIFFERENT, corrected $7 fill. Since
    # there's no competing same-ticker outcome it could be confused with,
    # it must still match and merge despite the amount disagreeing with the
    # candidate's own estimate.
    earlier_state = tmp_path / "sole_id_less_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "sole_id_less_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 0},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "selected", "planned_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "status": "submitted", "submitted_amount_usd": 7.0}
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_bases_keeper_delta_on_confirmed_outcome_over_stale_row(tmp_path):
    # Codex review, PR #654 ("Base keeper deltas on the confirmed outcome
    # amount"): the keeper candidate row itself still says $5 (a stale
    # figure it never caught up to), but its matching outcome already
    # confirms $7 -- and the keeper's own day_notional_usd already reflects
    # that confirmed $7. A later duplicate corrects the fill to $8. The
    # delta must be computed against the ALREADY-CONFIRMED $7 (a $1 delta
    # -> $8), not the stale $5 row value (which would wrongly yield a $3
    # delta -> $10).
    earlier_state = tmp_path / "stale_row_confirmed_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {"order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "stale_row_confirmed_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 8.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["budget"]["day_notional_usd"] == 8.0


def test_load_json_files_prefers_stronger_candidate_fill_over_stale_outcome(tmp_path):
    # Codex review, PR #654 ("Prefer a stronger candidate fill over a stale
    # outcome"): the later duplicate's candidate row for "X" is "filled" at
    # $8, but its SAME-state matching outcome is only "submitted" at $5 (a
    # stale confirmation that hasn't caught up). Deferring to any placed
    # outcome merely because it has a usable amount discarded the stronger,
    # authoritative $8 candidate figure.
    earlier_state = tmp_path / "stronger_candidate_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 3.0, "week_notional_usd": 3.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 3.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "stronger_candidate_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 8.0}
                ],
                "broker_outcomes": {
                    "AAA": {"order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 8.0
    assert keeper["budget"]["day_notional_usd"] == 8.0


def test_load_json_files_deduplicates_legacy_and_account_scoped_variants(tmp_path):
    # Codex review, PR #654 ("Deduplicate mixed legacy and account-scoped
    # variants"): an earlier LEGACY state has no account_number at all (an
    # older writer, or an artifact-format transition), while a later
    # variant of the SAME physical order carries account_number "222". No
    # OTHER real account has ever claimed this (broker, order_id), so the
    # unscoped legacy claim and the newly-scoped one are unambiguously the
    # same order and must fold together, not survive as two submissions.
    earlier_state = tmp_path / "legacy_account_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "legacy_account_later.json"
    later_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "222",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_bridges_legacy_and_broker_scoped_variants(tmp_path):
    # Codex review, PR #654 ("Bridge missing broker scopes to unambiguous
    # variants"): an earlier LEGACY state has no "broker" field at all (an
    # older writer, or a retained blocked/legacy variant of one physical
    # run), while a later variant of the SAME order carries broker
    # "robinhood". No OTHER real broker has ever claimed this order_id, so
    # the unscoped legacy claim and the newly-scoped one are unambiguously
    # the same order and must fold together, not survive as two
    # submissions.
    earlier_state = tmp_path / "legacy_broker_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "legacy_broker_later.json"
    later_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_bridges_broker_and_account_scopes_jointly(tmp_path):
    # Codex review, PR #654 ("Bridge broker and account scopes together"):
    # an earlier LEGACY state omits BOTH "broker" and "account_number"
    # entirely, while a later variant of the SAME order supplies both
    # ("robinhood" / "A"). Neither one-dimensional fallback alone can find
    # the legacy claim's actual key of (None, None, "X"): the account
    # fallback looks under the row's own (new) broker "robinhood", and the
    # broker fallback keeps the row's own (new) account "A" -- both miss
    # the fully-unscoped legacy entry. Only jointly bridging both missing
    # scopes together resolves it.
    earlier_state = tmp_path / "legacy_both_scopes_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "legacy_both_scopes_later.json"
    later_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "A",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_keeps_distinct_scheduled_runs_on_same_day(tmp_path):
    # Codex review, PR #654 ("Use run identity before collapsing same-day
    # states"): the keeper is a MORNING scheduled run; the later state is a
    # genuinely DIFFERENT AFTERNOON scheduled run on the SAME trading_day
    # (different run_classification.run_id), whose only row happens to
    # repeat the morning run's order "X". trading_day equality alone would
    # treat the afternoon run as a mere variant of the morning one and
    # collapse it away entirely -- but it's a real, distinct scheduled run
    # that must survive (with zero orders after the correction), not
    # disappear from run-day denominators.
    earlier_state = tmp_path / "distinct_run_morning.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "distinct_run_afternoon.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "run_classification": {"run_id": "afternoon-run"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    later_loaded = loaded[1]
    assert later_loaded["run_classification"]["run_id"] == "afternoon-run"
    assert later_loaded["buy_candidates"] == []


def test_load_json_files_preserves_metadata_from_amount_less_matching_outcome(tmp_path):
    # Codex review, PR #654 ("Preserve metadata from amount-less matching
    # outcomes"): the later duplicate's candidate row for "X" supplies the
    # amount ($5, via the candidate fallback), but its matching same-state
    # outcome -- which lacks submitted_amount_usd entirely -- carries a
    # DATED claim_breakdown attributing the claim to the prior Friday. The
    # outcome branch can never invoke the keeper merge without an amount of
    # its own, so its claim metadata must be merged here, alongside the
    # candidate's fallback amount, or it's lost when the outcome is dropped.
    earlier_state = tmp_path / "amount_less_outcome_metadata_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "amount_less_outcome_metadata_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {
                        "order_id": "X",
                        "status": "filled",
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 5.0}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["budget"]["day_notional_usd"] == 5.0
    assert keeper["budget"]["week_notional_usd"] == 0.0


def test_load_json_files_records_bridged_account_to_stop_later_collisions(tmp_path):
    # Codex review, PR #654 ("Record scoped accounts bridged to a legacy
    # claim"): a legacy state (no account_number) claims order "X", a
    # later account-"A" variant of the SAME order is bridged onto it via
    # the unscoped-legacy fallback. A THIRD state under a genuinely
    # DIFFERENT concrete account "B" then reports its own order "X" ($9).
    # If the bridged account "A" is never recorded, the claimed-accounts
    # set for (broker, "X") stays {None} forever, and account "B"'s real,
    # unrelated order also looks "unambiguous" and gets wrongly bridged
    # too -- overwriting its own $9 with whatever the legacy/A claim holds.
    legacy_state = tmp_path / "bridged_account_legacy.json"
    legacy_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    account_a_state = tmp_path / "bridged_account_a.json"
    account_a_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "A",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    account_b_state = tmp_path / "bridged_account_b.json"
    account_b_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "B",
                "trading_day": "2026-07-21",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 9.0, "week_notional_usd": 9.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 9.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([legacy_state, account_a_state, account_b_state])
    assert len(loaded) == 2
    account_b_loaded = loaded[1]
    assert len(account_b_loaded["buy_candidates"]) == 1
    assert account_b_loaded["buy_candidates"][0]["submitted_amount_usd"] == 9.0


def test_load_json_files_preserves_stronger_outcome_amount_during_same_state_fold(tmp_path):
    # Codex review, PR #654 ("Preserve stronger outcome amounts during
    # same-state folding"): a single retained blocked state has TWO
    # broker_outcomes entries for the SAME order_id "X" -- a "filled" $7
    # entry followed by a stale "submitted" $5 one. Both fold onto the same
    # identity; the second, weaker entry must not overwrite the first,
    # stronger one's amount.
    run_state = tmp_path / "same_state_outcome_fold_priority.json"
    run_state.write_text(
        json.dumps(
            {
                "status": "blocked",
                "trading_day": "2026-07-24",
                "broker_outcomes": {
                    "AAA_filled": {"order_id": "X", "ticker": "AAA", "status": "filled", "submitted_amount_usd": 7.0},
                    "AAA_submitted": {
                        "order_id": "X",
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([run_state])
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $7.00 |" in report


def test_load_json_files_ignores_non_placement_outcome_when_matching_id_less(tmp_path):
    # Codex review, PR #654 ("Ignore non-placement outcomes when consuming
    # ID-less matches"): the later state's removed duplicate candidate for
    # "X" has TWO same-ticker ID-less broker_outcomes -- an earlier "failed"
    # entry with no amount, and a later "filled" $7 correction. A
    # non-placement outcome consuming the sole pending match via the
    # unknown-amount fallback would leave the genuine $7 fill unmatched and
    # reported as a brand-new submission instead of merging into the keeper.
    earlier_state = tmp_path / "ignore_failed_outcome_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "ignore_failed_outcome_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 0},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "selected", "planned_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA_failed": {"ticker": "AAA", "status": "failed"},
                    "AAA_filled": {"ticker": "AAA", "status": "filled", "submitted_amount_usd": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    keeper = loaded[0]
    assert keeper["budget"]["day_notional_usd"] == 7.0
    later_loaded = loaded[1]
    assert later_loaded["buy_candidates"] == []
    assert list(later_loaded["broker_outcomes"].keys()) == ["AAA_failed"]


def test_load_json_files_ranks_both_keeper_representations_before_merging(tmp_path):
    # Codex review, PR #654 ("Rank both keeper representations before
    # merging"): the keeper's candidate row is "filled" at $7 (the
    # stronger representation) while its matching outcome is still
    # "submitted" at $5 (weaker). A later duplicate at $6, "submitted",
    # ties the WEAKER outcome's own rank -- but must still lose to the
    # keeper's overall strongest representation (the "filled" $7 row), not
    # be judged only against whichever representation happens to be
    # checked first.
    earlier_state = tmp_path / "rank_both_representations_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 7.0}
                ],
                "broker_outcomes": {
                    "AAA": {"order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "rank_both_representations_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 6.0, "week_notional_usd": 6.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 6.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0
    assert keeper["budget"]["day_notional_usd"] == 7.0


def test_load_json_files_deduplicates_aggregated_whole_share_order_ids(tmp_path):
    # Codex review, PR #654 ("Deduplicate every order ID in aggregated
    # rows"): near_close_dca's whole-share merge can aggregate several
    # physical orders into one row/outcome, with the scalar order_id
    # holding only the NEWEST of them and the complete set living in
    # whole_share_order_ids. An earlier state independently claims BOTH
    # "X" ($5) and "Y" ($6) as their own separate orders. A later state's
    # row aggregates them under scalar order_id "Y" ($11, the combined
    # total) -- a PROVABLE full duplicate, since every one of its
    # constituent IDs is already claimed elsewhere -- and must be folded
    # away rather than double-counted a third time.
    earlier_state = tmp_path / "aggregated_order_ids_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 11.0, "week_notional_usd": 11.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "AAA", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 6.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "aggregated_order_ids_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 11.0, "week_notional_usd": 11.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "Y",
                        "whole_share_order_ids": ["X", "Y"],
                        "status": "submitted",
                        "submitted_amount_usd": 11.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "$22.00" not in report
    assert "| Orders submitted | 2 |" in report
    # Codex review, PR #654 ("Do not merge aggregate totals into one
    # constituent"): the later aggregate's identity resolves via the
    # multi-constituent fallback onto whichever of X/Y's own keeper it
    # names first -- merging its full $11 in as that ONE keeper's
    # "authoritative" amount on top of the OTHER keeper's own unmodified
    # amount previously reported $16 ($11 + $6), not the correct $11.
    assert "| Executed notional | $11.00 |" in report


def test_load_json_files_does_not_overwrite_aggregate_keeper_with_one_constituent(tmp_path):
    # Codex review, PR #654 ("Do not merge aggregate totals into one
    # constituent"): the REVERSE order from the sibling test above. An
    # earlier state's row already aggregates "X" and "Y" under scalar
    # order_id "Y" ($10 combined) -- _register_seen_claim aliases BOTH
    # "X" and "Y" directly onto this SAME keeper entry. A later state then
    # submits a plain STANDALONE row for constituent "X" alone ($5) --
    # its bare identity resolves via a DIRECT exact match (not the
    # multi-constituent fallback, since "X" was aliased straight into
    # _claimed_map), so treating its own $5 as an authoritative
    # correction for the aggregate keeper overwrote the correctly
    # combined $10 down to $5, silently dropping "Y"'s $5 share.
    earlier_state = tmp_path / "aggregate_keeper_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 10.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "Y",
                        "whole_share_order_ids": ["X", "Y"],
                        "status": "submitted",
                        "submitted_amount_usd": 10.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "aggregate_keeper_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 10.0
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $10.00 |" in report


def test_load_json_files_preserves_unclaimed_order_in_partial_aggregate(tmp_path):
    # Codex review, PR #654 ("Preserve unclaimed orders in aggregate
    # rows"): an earlier state claims ONLY order "X" ($5). A later state's
    # row aggregates "X" and a genuinely NEW order "Y" under scalar
    # order_id "Y" ($10 combined) -- a PARTIAL overlap, not a full
    # duplicate. This module has no per-ID breakdown of the aggregate's
    # own total to split between the already-claimed "X" portion and the
    # new "Y" portion, so folding the whole row away as a duplicate of "X"
    # would silently discard "Y"'s evidence entirely. Both states must
    # survive rather than being merged.
    earlier_state = tmp_path / "partial_aggregate_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "partial_aggregate_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 10.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "Y",
                        "whole_share_order_ids": ["X", "Y"],
                        "status": "submitted",
                        "submitted_amount_usd": 10.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    assert loaded[0]["buy_candidates"][0]["submitted_amount_usd"] == 5.0
    assert loaded[1]["buy_candidates"][0]["submitted_amount_usd"] == 10.0


def test_load_json_files_leaves_ambiguous_equal_amount_id_less_outcomes_unmatched(tmp_path):
    # Codex review, PR #654 ("Disambiguate equal-amount ID-less
    # outcomes"): the later state has TWO removed duplicate candidates
    # (order "X" and order "Y", both ticker "AAA", both $5) plus TWO
    # ID-less broker_outcomes entries -- also both $5 -- each carrying a
    # DIFFERENT claim_breakdown date. Matching whichever pending entry is
    # encountered first would attribute one order's claim metadata to the
    # WRONG keeper. Neither outcome can be unambiguously matched, so both
    # must be left alone rather than guessed at.
    keeper_x_state = tmp_path / "ambiguous_id_less_keeper_x.json"
    keeper_x_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    keeper_y_state = tmp_path / "ambiguous_id_less_keeper_y.json"
    keeper_y_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "ambiguous_id_less_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 2, "submitted": 0},
                "budget": {"day_notional_usd": 10.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "selected", "planned_amount_usd": 5.0},
                    {"ticker": "AAA", "order_id": "Y", "status": "selected", "planned_amount_usd": 5.0},
                ],
                "broker_outcomes": {
                    "AAA_1": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 5.0}],
                    },
                    "AAA_2": {
                        "ticker": "AAA",
                        "status": "submitted",
                        "submitted_amount_usd": 5.0,
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-18", "amount_usd": 5.0}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([keeper_x_state, keeper_y_state, later_state])
    assert len(loaded) == 3
    keeper_x = loaded[0]
    keeper_y = loaded[1]
    later_loaded = loaded[2]
    assert "whole_share_claim_breakdown" not in keeper_x["buy_candidates"][0]
    assert "whole_share_claim_breakdown" not in keeper_y["buy_candidates"][0]
    assert later_loaded["buy_candidates"] == []
    assert set(later_loaded["broker_outcomes"].keys()) == {"AAA_1", "AAA_2"}


def test_load_json_files_aliases_bridged_account_scope_for_later_matches(tmp_path):
    # Codex review, PR #654 ("Alias bridged account scopes in the claimed
    # map"): a legacy order "X" (no account) is bridged to an account-A
    # variant. Recording account "A" into the claimed-accounts set (the
    # prior fix) makes that set {None, "A"} -- no longer a singleton -- so
    # a THIRD state under that SAME account "A" would find no exact
    # _claimed_map entry and no longer qualify for the fallback either,
    # surviving unmatched. The bridged account must be aliased directly
    # onto the keeper entry in _claimed_map so a later exact lookup under
    # it still succeeds.
    legacy_state = tmp_path / "alias_bridged_legacy.json"
    legacy_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    account_a_first_state = tmp_path / "alias_bridged_account_a_first.json"
    account_a_first_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "A",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 6.0, "week_notional_usd": 6.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 6.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    account_a_second_state = tmp_path / "alias_bridged_account_a_second.json"
    account_a_second_state.write_text(
        json.dumps(
            {
                "broker": "robinhood",
                "account_number": "A",
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 7.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([legacy_state, account_a_first_state, account_a_second_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["buy_candidates"][0]["submitted_amount_usd"] == 7.0


def test_load_json_files_registers_every_constituent_id_from_aggregate_keeper(tmp_path):
    # Codex review, PR #654 ("Register every constituent ID from aggregate
    # keepers"): the EARLIEST (keeper) state's own row is itself an
    # aggregated whole-share row -- scalar order_id "Y" (the newest),
    # whole_share_order_ids ["X", "Y"], $10 combined. Only registering the
    # scalar "Y" left "X" unclaimed, so a later standalone $5 row for "X"
    # alone found no claim at all and survived as a brand-new order.
    earlier_state = tmp_path / "aggregate_keeper_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 10.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "Y",
                        "whole_share_order_ids": ["X", "Y"],
                        "status": "submitted",
                        "submitted_amount_usd": 10.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "aggregate_keeper_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    report = build_performance_snapshot(loaded)
    assert "$15.00" not in report
    assert "| Orders submitted | 1 |" in report


def test_this_run_submitted_notional_prefers_corrected_submitted_amount():
    # Codex review, PR #654 ("Prefer corrected submitted amounts in
    # executed totals"): a completed keeper (no budget.day_notional_usd
    # of its own) has a candidate row whose original planned_amount_usd
    # ($5) was never updated by the cross-file dedup merge -- only its
    # submitted_amount_usd ($7, the authoritative correction) was.
    # Reading planned_amount_usd unconditionally reported the stale $5.
    run_states = [
        {
            "trading_day": "2026-07-20",
            "counts": {"submitted": 1},
            "budget": {"weekly_cap_usd": 100.0},
            "buy_candidates": [
                {
                    "ticker": "AAA",
                    "order_id": "X",
                    "status": "submitted",
                    "planned_amount_usd": 5.0,
                    "submitted_amount_usd": 7.0,
                }
            ],
        }
    ]
    report = build_performance_snapshot(run_states)
    assert "| Executed notional | $7.00 |" in report


def test_load_json_files_clamps_negative_current_week_claim_remainder(tmp_path):
    # Codex review, PR #654 ("Prevent negative current-week claim
    # remainders"): the keeper's own breakdown (all $7 dated to the prior
    # Friday) is never replaced because the later duplicate supplies no
    # claim metadata of its own -- only a corrected, LOWER $5 amount. The
    # breakdown's own accounted total ($7) now exceeds the corrected
    # amount ($5), and an unclamped remainder would subtract $2 from the
    # keeper's week -- which never received any of this order in the
    # first place (the whole $7 was already attributed to Friday's week).
    earlier_state = tmp_path / "negative_remainder_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 7.0, "week_notional_usd": 0.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {
                        "ticker": "AAA",
                        "order_id": "X",
                        "status": "submitted",
                        "submitted_amount_usd": 7.0,
                        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 7.0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "negative_remainder_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    keeper = loaded[0]
    assert keeper["budget"]["week_notional_usd"] == 0.0


def test_current_week_claim_amount_preserves_negative_unaccounted_remainder():
    # Codex review, PR #654 ("Preserve negative unaccounted adjustment
    # remainders"): a -$3 settlement-adjustment aggregate whose breakdown
    # only dates -$2 to a PRIOR week genuinely owes the remaining -$1 to
    # the artifact's own current week. The zero-floor added to guard the
    # ORDINARY (non-negative) over-accounted case must not also swallow a
    # legitimate negative remainder for an adjustment's own negative
    # aggregate.
    source = {
        "whole_share_claim_breakdown": [
            {"trading_day": "2026-07-17", "amount_usd": -2.0},
        ]
    }
    # week_key = the ISO week of 2026-07-20 (a Monday, same week as the
    # 07-17 Friday claim entry) -- so the -$2 IS attributed to a "prior"
    # week only relative to the RUNNING week the aggregate applies to.
    running_week_key = (2026, 31)  # a later week than 2026-07-17's (2026, 29)
    result = _current_week_claim_amount(source, -3.0, running_week_key)
    assert result == -1.0


def test_current_week_claim_amount_still_floors_ordinary_over_accounted_remainder():
    # Companion to the negative-remainder preservation above: the existing
    # floor-at-zero guard for an ORDINARY (non-negative) over-accounted
    # breakdown must still apply unchanged.
    source = {
        "whole_share_claim_breakdown": [
            {"trading_day": "2026-07-17", "amount_usd": 7.0},
        ]
    }
    running_week_key = (2026, 31)
    result = _current_week_claim_amount(source, 5.0, running_week_key)
    assert result == 0.0


def test_load_json_files_merges_partially_overlapping_variants_of_one_run(tmp_path):
    # Codex review, PR #654 discussion_r3853577540 ("Merge partial variants
    # before counting run days"): an earlier BLOCKED variant of one scheduled
    # run holds order X ($5); its later COMPLETED variant of the SAME
    # run_classification.run_id holds X plus a newly placed Y ($3). Row-level
    # dedup correctly leaves X in the first state and Y in the second, so
    # NEITHER is emptied and the fully-absorbed husk collapse cannot touch
    # either -- both survived, and one physical run reported TWO run days (and
    # two per-run rows in every builder that emits one row per state). The
    # later variant's remainder is now folded into the keeper so the run
    # reports the UNION of X and Y under a single run day.
    earlier_state = tmp_path / "partial_merge_blocked.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:30:00",
                "status": "blocked",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "partial_merge_completed.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:35:00",
                "status": "completed",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    survivor = loaded[0]
    # The union of the two variants' DISTINCT orders, each exactly once.
    assert [row["order_id"] for row in survivor["buy_candidates"]] == ["X", "Y"]
    assert survivor["counts"]["buy_candidates"] == 2
    assert survivor["counts"]["submitted"] == 2
    # Row-local day totals ADD (5 + the later variant's post-dedup 3);
    # cumulative week-to-date totals do NOT (both variants report the same
    # week), so the survivor takes the larger, not the sum.
    assert survivor["budget"]["day_notional_usd"] == 8.0
    assert survivor["budget"]["week_notional_usd"] == 8.0
    report = build_performance_snapshot(loaded)
    assert "| Run days | 1 |" in report
    assert "| Orders submitted | 2 |" in report
    assert "| Executed notional | $8.00 |" in report
    assert "| Week notional / cap | $8.00 / $100.00 |" in report


def test_load_json_files_merges_partial_variant_broker_outcomes_without_collision(tmp_path):
    # Companion to the merge above, for the broker_outcomes side: both
    # variants of one run carry a ticker-keyed "AAA" outcome, but for
    # DIFFERENT orders (the keeper's X, the later variant's newly placed Z).
    # A naive dict update would silently drop one of the two; the merge falls
    # back to the "<ticker>:<order_id>" key near_close_dca's own
    # preserved-state merge already uses, and carries the real ticker inside
    # the outcome so submitted_order_rows() never writes the synthetic key as
    # the symbol.
    earlier_state = tmp_path / "outcome_merge_blocked.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:30:00",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                },
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "outcome_merge_completed.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:35:00",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 9.0, "week_notional_usd": 9.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "AAA", "order_id": "Z", "status": "submitted", "submitted_amount_usd": 4.0},
                ],
                "broker_outcomes": {
                    "AAA": {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    "AAA:Z": {"ticker": "AAA", "order_id": "Z", "status": "submitted", "submitted_amount_usd": 4.0},
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    survivor = loaded[0]
    outcomes = survivor["broker_outcomes"]
    # The keeper's own "AAA" entry is untouched; the later variant's distinct
    # order arrives under its own non-colliding key with its ticker intact.
    assert outcomes["AAA"]["order_id"] == "X"
    assert outcomes["AAA:Z"]["order_id"] == "Z"
    assert outcomes["AAA:Z"]["ticker"] == "AAA"
    assert sorted(row["order_id"] for row in survivor["buy_candidates"]) == ["X", "Z"]
    report = build_performance_snapshot(loaded)
    assert "| Run days | 1 |" in report
    assert "| Orders submitted | 2 |" in report
    assert "| Executed notional | $9.00 |" in report


def test_load_json_files_does_not_merge_partial_variants_without_explicit_run_id(tmp_path):
    # The merge moves live orders out of one state and reports them under
    # another, so it demands POSITIVE proof the two are variants of one
    # physical run: a matching run_classification.run_id on both sides. A
    # shared trading_day alone -- enough to drop a state already emptied of
    # every row -- is NOT enough here, since two genuinely different
    # scheduled runs share a calendar day all the time.
    earlier_state = tmp_path / "no_run_id_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:30:00",
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "no_run_id_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:35:00",
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    assert [row["order_id"] for row in loaded[1]["buy_candidates"]] == ["Y"]


def test_load_json_files_does_not_merge_partial_variants_of_different_runs(tmp_path):
    # The keeper is a MORNING run; the later state is a genuinely different
    # AFTERNOON run on the same trading_day that repeats the morning's order
    # X alongside its own newly placed Y. Both carry a run_id, but they
    # DISAGREE -- the afternoon run keeps its own run day and its own Y.
    earlier_state = tmp_path / "different_run_morning.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:30:00",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "different_run_afternoon.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T15:30:00",
                "run_classification": {"run_id": "afternoon-run"},
                "counts": {"buy_candidates": 2, "submitted": 2},
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    assert loaded[1]["run_classification"]["run_id"] == "afternoon-run"
    assert [row["order_id"] for row in loaded[1]["buy_candidates"]] == ["Y"]


def test_load_json_files_does_not_merge_when_duplicates_span_several_keepers(tmp_path):
    # A state whose removals funnel into MORE THAN ONE sibling has no single
    # defensible destination for its remaining rows -- guessing one would
    # attribute a real order to a run that never placed it -- so it is left
    # alone, exactly as before. Two earlier variants of one run each hold a
    # distinct order; the third holds both of theirs plus its own new Z.
    first_state = tmp_path / "multi_keeper_first.json"
    first_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:30:00",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    second_state = tmp_path / "multi_keeper_second.json"
    second_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:32:00",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 3.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    third_state = tmp_path / "multi_keeper_third.json"
    third_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:35:00",
                "run_classification": {"run_id": "morning-run"},
                "counts": {"buy_candidates": 3, "submitted": 3},
                "budget": {"day_notional_usd": 10.0, "week_notional_usd": 10.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                    {"ticker": "CCC", "order_id": "Z", "status": "submitted", "submitted_amount_usd": 2.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([first_state, second_state, third_state])
    assert len(loaded) == 3
    assert [row["order_id"] for row in loaded[2]["buy_candidates"]] == ["Z"]


def _partial_merge_state(**overrides):
    """A minimal run_state for the partial-variant merge tests.

    Every merge test needs the same scaffolding -- a shared trading_day and a
    matching run_classification.run_id (the merge's explicit-identity bar),
    plus a generated_at so the accumulation order is deterministic.
    """
    state = {
        "trading_day": "2026-07-20",
        "run_classification": {"run_id": "morning-run"},
        "budget": {"weekly_cap_usd": 100.0},
    }
    budget = state["budget"]
    budget.update(overrides.pop("budget", {}))
    state.update(overrides)
    state["budget"] = budget
    return state


def test_load_json_files_preserves_downward_weekly_correction_when_merging(tmp_path):
    # Codex review, PR #654 ("Preserve downward corrections when folding
    # weekly totals"): the later variant of one run legitimately reports a
    # LOWER cumulative week total ($8) than the earlier one ($10) because it
    # carries a -$2 settlement adjustment against the same order. Taking the
    # larger of the two weekly figures restored the stale pre-adjustment $10
    # on the survivor -- the blocked-state reconciliation that produced the
    # $8 does not rerun for it. A cumulative total's LATEST report wins,
    # which is also exactly what _latest_week_value() selected from the two
    # states before they were merged.
    earlier_state = tmp_path / "weekly_correction_earlier.json"
    earlier_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 10.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "weekly_correction_later.json"
    later_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                counts={"buy_candidates": 2, "submitted": 1},
                budget={"day_notional_usd": 3.0, "week_notional_usd": 8.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {
                        "ticker": "AAA",
                        "order_id": "ADJ",
                        "status": "filled",
                        "whole_share_settlement_adjustment": True,
                        "submitted_amount_usd": -2.0,
                    },
                ],
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    survivor = loaded[0]
    assert survivor["budget"]["week_notional_usd"] == 8.0
    # $5 order less the $2 correction -- the day total is row-local, so it
    # still adds across the two variants.
    assert survivor["budget"]["day_notional_usd"] == 3.0


def test_load_json_files_unions_manual_review_flags_when_merging_variants(tmp_path):
    # Codex review, PR #654 ("Merge manual-review flags before dropping
    # partial variants"): manual_review_tickers is neither a row nor a total,
    # so the merge moved the later variant's rows and dollars but left its
    # flags behind -- and build_coverage_missed()/build_weekly_review_notes()
    # read that list straight off the state, so both reported zero flags for
    # the run.
    earlier_state = tmp_path / "manual_flags_earlier.json"
    earlier_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0},
                manual_review_tickers=["AAA"],
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "manual_flags_later.json"
    later_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                counts={"buy_candidates": 2, "submitted": 2},
                budget={"day_notional_usd": 8.0, "week_notional_usd": 8.0},
                # "AAA" repeated by both variants must not be double-counted.
                manual_review_tickers=["AAA", "BBB"],
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    assert loaded[0]["manual_review_tickers"] == ["AAA", "BBB"]


def test_load_json_files_corrects_credited_removal_to_matched_id_less_fill(tmp_path):
    # Codex review, PR #654 ("Replace candidate removal credit with the
    # matched fill amount"): the later variant's duplicate X is estimated at
    # $5 on its candidate row but confirmed at $7 by an ID-less outcome, and
    # its own $11 day total was baked from the confirmed figure. The
    # candidate side credited the removal at the $5 estimate and marked the
    # order handled, so the $2 difference stayed behind -- and the partial
    # merge then carried that phantom remainder into a keeper the keeper
    # merge had already corrected to $7, reporting $13 for $11 of orders.
    earlier_state = tmp_path / "credited_removal_earlier.json"
    earlier_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "credited_removal_later.json"
    later_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                counts={"buy_candidates": 2, "submitted": 2},
                budget={"day_notional_usd": 11.0, "week_notional_usd": 11.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 4.0},
                ],
                broker_outcomes={"AAA": {"ticker": "AAA", "status": "filled", "submitted_amount_usd": 7.0}},
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    survivor = loaded[0]
    # $7 confirmed for X plus $4 for Y -- never the $13 the uncorrected
    # removal credit produced.
    assert survivor["budget"]["day_notional_usd"] == 11.0
    report = build_performance_snapshot(loaded)
    assert "| Executed notional | $11.00 |" in report


def test_load_json_files_does_not_match_id_less_adjustment_to_ordinary_removal(tmp_path):
    # Codex review, PR #654 ("Match ID-less outcomes only to the same order
    # kind"): the later variant's only ID-less same-ticker outcome is a -$2
    # SETTLEMENT ADJUSTMENT, while its only pending removal is the ordinary
    # duplicate X. The sole-pending-entry fallback read the whole per-ticker
    # list without comparing kinds, so the adjustment was consumed as X's
    # match -- dropped from broker_outcomes and merged onto the ordinary
    # keeper, rewriting X's own $5 fill to -$2.
    earlier_state = tmp_path / "kind_mismatch_earlier.json"
    earlier_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "kind_mismatch_later.json"
    later_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 3.0, "week_notional_usd": 3.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
                broker_outcomes={
                    "AAA_adjustment": {
                        "ticker": "AAA",
                        "status": "filled",
                        "whole_share_settlement_adjustment": True,
                        "submitted_amount_usd": -2.0,
                    }
                },
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    survivor = loaded[0]
    # X keeps its own $5 fill; the adjustment survives as a separate entry.
    assert survivor["buy_candidates"][0]["submitted_amount_usd"] == 5.0
    assert survivor["broker_outcomes"]["AAA_adjustment"]["submitted_amount_usd"] == -2.0
    # $5 order less the genuine -$2 correction.
    assert survivor["budget"]["day_notional_usd"] == 3.0


def test_load_json_files_retargets_claims_through_the_merge_chain(tmp_path):
    # Codex review, PR #654 ("Retarget order claims after merging their
    # keeper state"): three variants of one run. A holds X ($5); B holds X
    # plus Y ($3) and is merged into A; C corrects Y to a $4 fill and adds Z
    # ($2). Y's claimed keeper entry still pins the now-dropped B, so C's +$1
    # correction landed on B's budget -- out of every report -- while Y's row
    # itself (living in A now) was updated to $4, leaving A reporting $10 for
    # rows totalling $11.
    first_state = tmp_path / "chain_a.json"
    first_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    second_state = tmp_path / "chain_b.json"
    second_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:32:00",
                counts={"buy_candidates": 2, "submitted": 2},
                budget={"day_notional_usd": 8.0, "week_notional_usd": 8.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            )
        ),
        encoding="utf-8",
    )
    third_state = tmp_path / "chain_c.json"
    third_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                counts={"buy_candidates": 2, "submitted": 2},
                budget={"day_notional_usd": 6.0, "week_notional_usd": 6.0},
                buy_candidates=[
                    {"ticker": "BBB", "order_id": "Y", "status": "filled", "submitted_amount_usd": 4.0},
                    {"ticker": "CCC", "order_id": "Z", "status": "submitted", "submitted_amount_usd": 2.0},
                ],
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([first_state, second_state, third_state])
    assert len(loaded) == 1
    survivor = loaded[0]
    rows = {row["order_id"]: row["submitted_amount_usd"] for row in survivor["buy_candidates"]}
    assert rows == {"X": 5.0, "Y": 4.0, "Z": 2.0}
    # The reported total must agree with the rows the survivor actually holds.
    assert survivor["budget"]["day_notional_usd"] == sum(rows.values()) == 11.0
    report = build_performance_snapshot(loaded)
    assert "| Run days | 1 |" in report
    assert "| Executed notional | $11.00 |" in report


def test_load_json_files_adjusts_top_level_submitted_count_after_dedup(tmp_path):
    # Codex review, PR #654 ("Adjust the top-level submitted count after
    # deduplication"): morning/midday blocked-partial artifacts carry no
    # counts.submitted at all -- the real figure lives under the top-level
    # orders_submitted_count, which is exactly the fallback
    # build_performance_snapshot() reads. Only the nested field was
    # decremented, so an earlier state holding X plus a later state holding
    # duplicate X and new Y still reported THREE submissions for the two
    # orders actually retained.
    earlier_state = tmp_path / "top_level_count_earlier.json"
    earlier_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:30:00",
                "status": "blocked",
                "orders_submitted_count": 1,
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "top_level_count_later.json"
    later_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:35:00",
                "status": "blocked",
                "orders_submitted_count": 2,
                "budget": {"day_notional_usd": 8.0, "week_notional_usd": 8.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    # No run_classification.run_id on either side, so these stay two states
    # (the partial-variant merge needs explicit run identity) -- which is
    # exactly the shape that exposed the unadjusted fallback count.
    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 2
    assert loaded[1]["orders_submitted_count"] == 1
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 2 |" in report
    assert "| Executed notional | $8.00 |" in report


def test_load_json_files_keeps_distinct_same_day_streams_without_run_ids(tmp_path):
    # Codex review, PR #654 ("Preserve distinct same-day streams without run
    # IDs"): neither state carries a run_classification.run_id, so the
    # identity check fell back to trading_day alone and treated a MORNING
    # run and an AFTERNOON run on the same day as variants of each other.
    # Once dedup emptied the afternoon state (its only row was recovery
    # evidence for the morning order), the husk collapse dropped that run
    # entirely -- losing a real run day and its coverage/status data.
    # run_classification.stream is the same field the executors use to tell
    # one slot's artifacts from another's.
    morning_state = tmp_path / "stream_morning.json"
    morning_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T10:30:00",
                "run_classification": {"stream": "morning"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    afternoon_state = tmp_path / "stream_afternoon.json"
    afternoon_state.write_text(
        json.dumps(
            {
                "trading_day": "2026-07-20",
                "generated_at": "2026-07-20T15:30:00",
                "run_classification": {"stream": "afternoon"},
                "counts": {"buy_candidates": 1, "submitted": 1},
                "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0, "weekly_cap_usd": 100.0},
                "buy_candidates": [
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([morning_state, afternoon_state])
    assert len(loaded) == 2
    assert loaded[1]["run_classification"]["stream"] == "afternoon"
    # Its duplicate row is still removed -- only the run day survives.
    assert loaded[1]["buy_candidates"] == []
    report = build_performance_snapshot(loaded)
    assert "| Run days | 2 |" in report
    assert "| Orders submitted | 1 |" in report


def test_states_share_run_identity_still_matches_on_shared_or_absent_stream():
    # The stream check only ever REJECTS: one intraday stream fires three
    # times a day, so a shared stream on one trading day is not proof of a
    # shared run and must fall through to the historical trading_day-only
    # behaviour, as must a stream present on only one side.
    from dca_weekly_dashboard import _states_share_run_identity

    same_stream_a = {"trading_day": "2026-07-20", "run_classification": {"stream": "intraday"}}
    same_stream_b = {"trading_day": "2026-07-20", "run_classification": {"stream": "intraday"}}
    assert _states_share_run_identity(same_stream_a, same_stream_b) is True

    one_sided = {"trading_day": "2026-07-20", "run_classification": {}}
    assert _states_share_run_identity(same_stream_a, one_sided) is True

    # An explicit run_id still wins outright over the stream fallback.
    explicit_a = {"trading_day": "2026-07-20", "run_classification": {"run_id": "R", "stream": "morning"}}
    explicit_b = {"trading_day": "2026-07-20", "run_classification": {"run_id": "R", "stream": "afternoon"}}
    assert _states_share_run_identity(explicit_a, explicit_b) is True


def test_merge_authoritative_amount_preserves_metadata_from_lower_ranked_duplicate():
    # Codex review, PR #654 ("Preserve metadata from lower-ranked duplicate
    # states"): the status-rank guard used to `return` outright, which
    # protected the keeper's stronger fill amount but also skipped the
    # claim-metadata merge -- contradicting the function's own contract that
    # metadata propagates unconditionally. A `filled` $7 keeper with no
    # breakdown, followed by a stale `submitted` $5 variant dating the claim
    # to the prior Friday, lost those dates and kept the $7 on its own week.
    from dca_weekly_dashboard import _merge_authoritative_amount_into_keeper

    keeper_row = {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 7.0}
    keeper_state = {
        "trading_day": "2026-07-20",
        "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0},
    }
    # (claiming_state_iso_week, keeper_row, keeper_outcome, keeper_state) --
    # 2026-07-20 is a Monday in ISO week (2026, 30).
    keeper_entry = ((2026, 30), keeper_row, None, keeper_state)
    stale_variant = {
        "ticker": "AAA",
        "order_id": "X",
        "status": "submitted",
        "submitted_amount_usd": 5.0,
        "whole_share_claim_breakdown": [{"trading_day": "2026-07-17", "amount_usd": 7.0}],
    }

    _merge_authoritative_amount_into_keeper(keeper_entry, 5.0, stale_variant)

    # The stale $5 must NOT overwrite the confirmed $7 fill...
    assert keeper_row["submitted_amount_usd"] == 7.0
    assert keeper_state["budget"]["day_notional_usd"] == 7.0
    # ...but its dated breakdown still moves the $7 off the keeper's own week.
    assert keeper_row["whole_share_claim_breakdown"] == [{"trading_day": "2026-07-17", "amount_usd": 7.0}]
    assert keeper_state["budget"]["week_notional_usd"] == 0.0


def test_load_json_files_derives_merged_counts_from_surviving_source_rows(tmp_path):
    # Codex review, PR #654 ("Derive merged counts from surviving source
    # rows"): the later variant reports NEITHER counts nor
    # orders_submitted_count, but its rows still move into the keeper. The
    # keeper here is a completed state, so the blocked-state normalization
    # never recomputes it -- crediting nothing for the moved rows left a
    # survivor holding X and Y while reporting one candidate and one
    # submission.
    earlier_state = tmp_path / "derived_counts_earlier.json"
    earlier_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                status="completed",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "derived_counts_later.json"
    later_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                status="completed",
                budget={"day_notional_usd": 8.0, "week_notional_usd": 8.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    survivor = loaded[0]
    assert [row["order_id"] for row in survivor["buy_candidates"]] == ["X", "Y"]
    # X was deduped out of the later variant, so only Y is derived from what
    # actually survived there -- 1 + 1, not 1 + 2.
    assert survivor["counts"]["buy_candidates"] == 2
    assert survivor["counts"]["submitted"] == 2
    report = build_performance_snapshot(loaded)
    assert "| Buy candidates | 2 |" in report
    assert "| Orders submitted | 2 |" in report


def test_load_json_files_carries_latest_remaining_budget_through_partial_merge(tmp_path):
    # Codex review, PR #654 ("Carry the latest remaining budget through
    # partial merges"): budget.remaining_spend_today_usd and the rollover
    # reserve are cumulative as-of-this-state snapshots that SHRINK as the
    # run places orders. Adding the merged rows to the keeper's executed
    # total while leaving its stale headroom rendered "$8.00 spent /
    # $95.00 remaining" for a run that had spent $8 of $100.
    earlier_state = tmp_path / "remaining_budget_earlier.json"
    earlier_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={
                    "day_notional_usd": 5.0,
                    "week_notional_usd": 5.0,
                    "daily_cap_usd": 100.0,
                    "remaining_spend_today_usd": 95.0,
                    "weekly_rollover_reserve_usd": 20.0,
                },
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    later_state = tmp_path / "remaining_budget_later.json"
    later_state.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                counts={"buy_candidates": 2, "submitted": 2},
                budget={
                    "day_notional_usd": 8.0,
                    "week_notional_usd": 8.0,
                    "daily_cap_usd": 100.0,
                    "remaining_spend_today_usd": 92.0,
                    "weekly_rollover_reserve_usd": 17.0,
                },
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    {"ticker": "BBB", "order_id": "Y", "status": "submitted", "submitted_amount_usd": 3.0},
                ],
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([earlier_state, later_state])
    assert len(loaded) == 1
    survivor_budget = loaded[0]["budget"]
    assert survivor_budget["day_notional_usd"] == 8.0
    assert survivor_budget["remaining_spend_today_usd"] == 92.0
    assert survivor_budget["weekly_rollover_reserve_usd"] == 17.0
    # The day's policy constant is identical in both variants and untouched.
    assert survivor_budget["daily_cap_usd"] == 100.0
    report = build_budget_reserve_effectiveness(loaded)
    assert "| $8.00 | $100.00 | $17.00 | $92.00 |" in report


def test_this_run_submitted_notional_rejects_non_finite_corrected_amount():
    # Codex review, PR #654 ("Reject non-finite corrected submitted
    # amounts"): the corrected submitted_amount_usd the keeper merge writes
    # was read with a bare float(), and float("NaN") succeeds -- so a
    # malformed correction poisoned the whole executed total as $nan AND
    # marked the order seen, blocking the broker_outcomes fallback that
    # carried the real figure, even though the row's own planned_amount_usd
    # was perfectly usable.
    poisoned = {
        "buy_candidates": [
            {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": "NaN",
             "planned_amount_usd": 5.0},
        ]
    }
    assert _this_run_submitted_notional(poisoned) == 5.0

    # With no usable planned amount either, the identity must stay unseen so
    # the outcome fallback can still supply the real figure.
    outcome_backed = {
        "buy_candidates": [
            {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": "Infinity"},
        ],
        "broker_outcomes": {
            "AAA": {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 7.0},
        },
    }
    assert _this_run_submitted_notional(outcome_backed) == 7.0


def test_load_json_files_preserves_completed_placement_semantics_when_merging(tmp_path):
    # Codex review, PR #654 ("Preserve completed placement semantics during
    # partial merges"): whether a STATUSLESS candidate row counts as placed
    # is a property of the state it lives in. An ordinary completed run's
    # rows are placed by construction; a blocked artifact's are not, and
    # build_graded_vs_fixed()/the blocked reconciliation exclude them.
    # Moving a completed variant's statusless row into a BLOCKED keeper
    # silently demoted a real order to unplaced -- a blocked X=$5 plus a
    # completed statusless Y=$3 rendered one order and $5 executed against
    # an $8 weekly total.
    blocked_keeper = tmp_path / "placement_semantics_blocked.json"
    blocked_keeper.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                status="blocked",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0, "base_amount_usd": 1.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    completed_source = tmp_path / "placement_semantics_completed.json"
    completed_source.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                status="completed",
                counts={"buy_candidates": 2, "submitted": 2},
                budget={"day_notional_usd": 8.0, "week_notional_usd": 8.0, "base_amount_usd": 1.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    # Statusless: placed by construction in a completed run.
                    {"ticker": "BBB", "order_id": "Y", "submitted_amount_usd": 3.0, "planned_amount_usd": 3.0},
                ],
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([blocked_keeper, completed_source])
    assert len(loaded) == 1
    survivor = loaded[0]
    moved = [row for row in survivor["buy_candidates"] if row["order_id"] == "Y"][0]
    # Y carries its provenance explicitly now that it lives in a blocked state.
    assert moved["status"] in ("submitted", "filled", "pending", "unconfirmed")
    # The keeper's own rows are untouched -- promoting the survivor's status
    # instead would have extended the completed run's construction guarantee
    # to rows a blocked artifact never made that promise for.
    assert survivor["status"] == "blocked"
    report = build_performance_snapshot(loaded)
    assert "| Orders submitted | 2 |" in report
    assert "| Executed notional | $8.00 |" in report


def test_load_json_files_does_not_promote_statusless_rows_from_a_non_completed_source(tmp_path):
    # Codex review, PR #1300 discussion_r3920546935 ("Restrict statusless-row
    # promotion to completed states"): the sibling test above
    # (test_load_json_files_preserves_completed_placement_semantics_when_
    # merging) covers a genuinely "completed" source correctly promoting its
    # statusless row -- this covers the gap the old `!= "blocked"` check let
    # through: a "failed" (or any other non-"blocked", non-affirmatively-
    # "completed") source provides no placement guarantee for its own
    # statusless rows either, so its statusless row must stay statusless
    # (excluded from counts.submitted / executed notional) once merged into
    # the blocked keeper, exactly like the keeper's own statusless rows
    # already are.
    blocked_keeper = tmp_path / "non_completed_source_blocked.json"
    blocked_keeper.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:30:00",
                status="blocked",
                counts={"buy_candidates": 1, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0, "base_amount_usd": 1.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0}
                ],
            )
        ),
        encoding="utf-8",
    )
    failed_source = tmp_path / "non_completed_source_failed.json"
    failed_source.write_text(
        json.dumps(
            _partial_merge_state(
                generated_at="2026-07-20T10:35:00",
                status="failed",
                counts={"buy_candidates": 2, "submitted": 1},
                budget={"day_notional_usd": 5.0, "week_notional_usd": 5.0, "base_amount_usd": 1.0},
                buy_candidates=[
                    {"ticker": "AAA", "order_id": "X", "status": "submitted", "submitted_amount_usd": 5.0},
                    # Statusless: this "failed" variant provides no placement
                    # evidence for it, unlike a genuinely completed run's row.
                    {"ticker": "BBB", "order_id": "Y", "submitted_amount_usd": 3.0, "planned_amount_usd": 3.0},
                ],
            )
        ),
        encoding="utf-8",
    )

    loaded = load_json_files([blocked_keeper, failed_source])
    assert len(loaded) == 1
    survivor = loaded[0]
    moved = [row for row in survivor["buy_candidates"] if row["order_id"] == "Y"][0]
    # Y must NOT be stamped "submitted"/"filled"/etc -- the failed source
    # gave it no placement evidence.
    assert moved.get("status") is None
    assert survivor["status"] == "blocked"


def test_merge_authoritative_amount_promotes_keeper_status_to_the_source():
    # Codex review, PR #1300 discussion_r3920466853 ("Promote retained
    # duplicates to the authoritative fill status"): an earlier state
    # records the order as "pending" and a later duplicate records the SAME
    # order as "filled" -- the merge already copies the later fill amount
    # into the keeper, but must ALSO promote the keeper's own row/outcome
    # status, or the returned normalized state reports the corrected filled
    # notional while both representations still read "pending", losing the
    # only terminal reconciliation evidence.
    from dca_weekly_dashboard import _merge_authoritative_amount_into_keeper

    keeper_row = {"ticker": "AAA", "order_id": "X", "status": "pending", "submitted_amount_usd": 5.0}
    keeper_outcome = {"order_id": "X", "status": "pending", "submitted_amount_usd": 5.0}
    keeper_state = {
        "trading_day": "2026-07-20",
        "budget": {"day_notional_usd": 5.0, "week_notional_usd": 5.0},
    }
    keeper_entry = ((2026, 30), keeper_row, keeper_outcome, keeper_state)
    filled_duplicate = {
        "ticker": "AAA",
        "order_id": "X",
        "status": "filled",
        "submitted_amount_usd": 7.0,
    }

    _merge_authoritative_amount_into_keeper(keeper_entry, 7.0, filled_duplicate)

    assert keeper_row["submitted_amount_usd"] == 7.0
    assert keeper_row["status"] == "filled"
    assert keeper_outcome["status"] == "filled"


def test_merge_authoritative_amount_never_downgrades_a_stronger_keeper_status():
    # The no-downgrade half of the same fix: when the source does NOT
    # outrank the keeper overall (both representations already at least as
    # strong), neither representation's status is touched, matching the
    # existing rule that already protects the amount in this case.
    from dca_weekly_dashboard import _merge_authoritative_amount_into_keeper

    keeper_row = {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 7.0}
    keeper_outcome = {"order_id": "X", "status": "filled", "submitted_amount_usd": 7.0}
    keeper_state = {
        "trading_day": "2026-07-20",
        "budget": {"day_notional_usd": 7.0, "week_notional_usd": 7.0},
    }
    keeper_entry = ((2026, 30), keeper_row, keeper_outcome, keeper_state)
    stale_submitted_duplicate = {
        "ticker": "AAA",
        "order_id": "X",
        "status": "submitted",
        "submitted_amount_usd": 5.0,
    }

    _merge_authoritative_amount_into_keeper(keeper_entry, 5.0, stale_submitted_duplicate)

    assert keeper_row["status"] == "filled"
    assert keeper_outcome["status"] == "filled"
    assert keeper_row["submitted_amount_usd"] == 7.0


def test_states_share_run_identity_rejects_distinct_intraday_slots_without_run_ids():
    # Codex review, PR #1300 discussion_r3920257833 ("Keep distinct
    # same-stream intraday slots separate"): "intraday" is one stream shared
    # across three distinct daily slots (11:30/13:30/14:30). Two same-day
    # intraday states with no usable run_id and the SAME stream previously
    # fell all the way through to the historical trading_day-only "same run"
    # verdict -- so if a later slot's state held only recovery evidence for
    # an order already present in an earlier slot's state, the husk collapse
    # absorbed the later slot as a duplicate of the earlier one, dropping a
    # real run day and its coverage/status data.
    # run_classification.scheduled_for_et is the slot identity available
    # even without a run_id, and disagreeing is provable proof these are
    # different runs.
    from dca_weekly_dashboard import _states_share_run_identity

    slot_1130 = {
        "trading_day": "2026-07-20",
        "run_classification": {"stream": "intraday", "scheduled_for_et": "11:30"},
    }
    slot_1330 = {
        "trading_day": "2026-07-20",
        "run_classification": {"stream": "intraday", "scheduled_for_et": "13:30"},
    }
    assert _states_share_run_identity(slot_1130, slot_1330) is False

    # A matching scheduled_for_et still falls through to the historical
    # trading_day-only verdict.
    slot_1130_again = {
        "trading_day": "2026-07-20",
        "run_classification": {"stream": "intraday", "scheduled_for_et": "11:30"},
    }
    assert _states_share_run_identity(slot_1130, slot_1130_again) is True

    # scheduled_for_et present on only one side is not proof either way --
    # falls through to the historical behaviour, same as an absent stream.
    no_slot = {"trading_day": "2026-07-20", "run_classification": {"stream": "intraday"}}
    assert _states_share_run_identity(slot_1130, no_slot) is True


def test_this_run_submitted_notional_rejects_non_finite_broker_only_amount():
    # Codex review, PR #1300 discussion_r3920642631 ("Validate broker-only
    # notionals before totaling them"): the broker_outcomes-only fallback
    # (reached when a completed/legacy state has placement evidence ONLY in
    # broker_outcomes, with no matching candidate row) previously read
    # submitted_amount_usd with the bare _num(), which accepts negative
    # values, booleans, NaN, and infinities -- unlike the candidate-row pass
    # just above it, which already validates with _finite_ordinary_amount().
    # A malformed outcome-only amount could poison the whole executed total
    # as $nan/$inf, or silently reduce it via a bogus negative.
    nan_outcome_only = {
        "broker_outcomes": {
            "AAA": {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": "NaN"},
        },
    }
    assert _this_run_submitted_notional(nan_outcome_only) == 0.0

    negative_outcome_only = {
        "broker_outcomes": {
            "AAA": {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": -5.0},
        },
    }
    assert _this_run_submitted_notional(negative_outcome_only) == 0.0

    # A genuinely usable outcome-only figure still counts normally.
    usable_outcome_only = {
        "broker_outcomes": {
            "AAA": {"ticker": "AAA", "order_id": "X", "status": "filled", "submitted_amount_usd": 7.0},
        },
    }
    assert _this_run_submitted_notional(usable_outcome_only) == 7.0

def test_build_weekly_dashboard_renders_all_sections_correctly():
    # Covers the top-level composition function, which lacked direct tests.
    run_states = [
        {
            "status": "completed",
            "trading_day": "2026-07-24",
            "counts": {"buy_candidates": 1},
            "budget": {
                "base_amount_usd": 2.0,
                "hypothetical_day_spend_usd": 4.0,
                "week_notional_usd": 4.0,
                "weekly_cap_usd": 100.0,
            },
            "orders_submitted_count": 1,
            "buy_candidates": [
                {"ticker": "AAA", "status": "filled", "planned_amount_usd": 4.0},
            ],
            "broker_outcomes": {"AAA": {"status": "filled", "submitted_amount_usd": 4.0}},
        }
    ]
    summaries = [
        {
            "trading_day": "2026-07-24",
            "coverage": {"buy_candidates": 1, "fills": 1, "coverage_pct": 100.0, "missing_fills": 0},
            "execution_quality": {"priced_fills": 1, "target_price_missing": 0, "avg_slippage_pct": 0.1, "max_slippage_pct": 0.1, "worst_ticker": "AAA"},
        }
    ]
    sector_map = {"AAA": "Tech"}

    report = build_weekly_dashboard(run_states, summaries, sector_map, week_label="2026-W30")

    # Expected headers
    assert "# Weekly Performance Dashboard — 2026-W30" in report
    assert "## Performance snapshot" in report
    assert "## Graded DCA vs fixed daily DCA" in report
    assert "## Sector performance matrix" in report
    assert "## Budget and reserve effectiveness" in report
    assert "## Execution quality" in report
    assert "## Coverage and missed opportunities" in report
    assert "## Weekly review notes" in report

    # Check some content from the components
    assert "Tech" in report  # Sector matrix
    assert "100.0%" in report # Coverage
    assert "0.1%" in report # Execution quality


def test_build_sector_matrix():
    run_states = [
        {
            "buy_candidates": [
                {"ticker": "AAPL", "planned_amount_usd": 150.0},
                {"ticker": "MSFT", "planned_amount_usd": 250.0},
                {"ticker": "V", "planned_amount_usd": 100.0},
                {"ticker": "AAPL", "planned_amount_usd": 50.0, "reason": "settlement_adjustment"}
            ]
        },
        {
            "buy_candidates": [
                {"ticker": "JPM", "planned_amount_usd": 200.0},
                {"ticker": "UNKNOWN", "planned_amount_usd": 50.0}
            ]
        }
    ]

    sector_map = {
        "AAPL": "Technology",
        "MSFT": "Technology",
        "V": "Financials",
        "JPM": "Financials"
    }

    report = build_sector_matrix(run_states, sector_map)

    assert "| Financials | 2 | $300.00 |" in report
    assert "| Technology | 3 | $450.00 |" in report
    assert "| Unclassified | 1 | $50.00 |" in report

def test_build_sector_matrix_empty():
    run_states = [
        {
            "buy_candidates": []
        }
    ]
    report = build_sector_matrix(run_states)
    assert "| _no candidates_ | 0 | $0.00 |" in report
def test_build_weekly_review_notes_empty():
    from dca_weekly_dashboard import build_weekly_review_notes
    res = build_weekly_review_notes([], [])
    assert "- Runs analyzed: 0 day(s)." in res
    assert "- All buy candidates were covered by fills." in res
    assert "- Add qualitative notes here before the Notion dashboard refresh." in res

def test_build_weekly_review_notes_all_zeros():
    from dca_weekly_dashboard import build_weekly_review_notes
    run_states = [{"manual_review_tickers": []}]
    summaries = [
        {"coverage": {"missing_fills": 0}, "execution_quality": {"target_price_missing": 0}}
    ]
    res = build_weekly_review_notes(run_states, summaries)
    assert "- Runs analyzed: 1 day(s)." in res
    assert "- All buy candidates were covered by fills." in res

def test_build_weekly_review_notes_with_issues():
    from dca_weekly_dashboard import build_weekly_review_notes
    run_states = [
        {"manual_review_tickers": ["AAA", "BBB"]},
        {"manual_review_tickers": ["CCC"]}
    ]
    summaries = [
        {"coverage": {"missing_fills": 2}, "execution_quality": {"target_price_missing": 1}},
        {"coverage": {"missing_fills": 3}, "execution_quality": {"target_price_missing": 2}}
    ]
    res = build_weekly_review_notes(run_states, summaries)
    assert "- Runs analyzed: 2 day(s)." in res
    assert "- 5 buy candidate(s) had no fill — review broker rejects / caps." in res
    assert "- 3 fill(s) lacked a target price; slippage excluded — check signal capture." in res
    assert "- 3 manual-review flag(s) across the week — reconcile finance vs Robinhood." in res

def test_build_execution_quality():
    from dca_weekly_dashboard import build_execution_quality
    summaries = [
        {
            "trading_day": "2026-07-20",
            "execution_quality": {
                "priced_fills": 5,
                "target_price_missing": 1,
                "avg_slippage_pct": 0.15,
                "max_slippage_pct": 0.42,
                "worst_ticker": "AAPL"
            }
        },
        {
            "trading_day": "2026-07-21",
            "execution_quality": {
                "priced_fills": 3,
                "target_price_missing": 0,
                "avg_slippage_pct": 0.25,
                "max_slippage_pct": 0.50,
                "worst_ticker": "TSLA"
            }
        },
        {
            "trading_day": "2026-07-22",
        },
        {
            "trading_day": "2026-07-23",
            "execution_quality": {
                "priced_fills": 2
            }
        }
    ]
    report = build_execution_quality(summaries)
    assert "| 2026-07-20 | 5 | 1 | 0.15% | 0.42% | AAPL |" in report
    assert "| 2026-07-21 | 3 | 0 | 0.25% | 0.5% | TSLA |" in report
    assert "| 2026-07-22 | 0 | 0 | n/a | n/a | n/a |" in report
    assert "| 2026-07-23 | 2 | 0 | n/a | n/a | n/a |" in report
    assert "| **Week avg** | | | **0.2%** | | |" in report


# The two tests below come from PR #1332, which was opened in parallel with
# #1313 (merged first) under the same two test names. They are kept under
# distinct names because they pin different things: the section and table
# headers, a settlement-adjustment row counting as an ordinary candidate,
# and the empty-input case with NO run states at all (#1313 covers a run
# state whose buy_candidates list is empty).
def test_build_sector_matrix_renders_header_and_unclassified_rows():
    run_states = [
        {
            "buy_candidates": [
                {"ticker": "AAPL", "planned_amount_usd": 100.50},
                {"ticker": "GOOGL", "planned_amount_usd": 200.00},
                {"ticker": "UNKNOWN", "planned_amount_usd": 50.00},
                {"ticker": "SETTLE", "planned_amount_usd": 10.00, "description": "Settlement Adjustment (10.00)"},
            ]
        },
        {
            "buy_candidates": [
                {"ticker": "AAPL", "planned_amount_usd": 50.00},
            ]
        }
    ]
    sector_map = {"AAPL": "Technology", "GOOGL": "Technology"}

    result = build_sector_matrix(run_states, sector_map)

    assert "## Sector performance matrix" in result
    assert "| Sector | Candidates | Planned $ |" in result
    assert "| Technology | 3 | $350.50 |" in result
    assert "| Unclassified | 2 | $60.00 |" in result


def test_build_sector_matrix_with_no_run_states():
    run_states = []

    result = build_sector_matrix(run_states)

    assert "## Sector performance matrix" in result
    assert "| _no candidates_ | 0 | $0.00 |" in result


def test_build_execution_quality_empty():
    from dca_weekly_dashboard import build_execution_quality
    report = build_execution_quality([])
    assert "## Execution quality" in report
    assert "| **Week avg** | | | **n/a** | | |" in report


def test_write_weekly_dashboard_creates_file(tmp_path):
    import os

    from dca_weekly_dashboard import write_weekly_dashboard

    run_states = [
        {
            "trading_day": "2026-07-20",
            "status": "completed",
            "buy_candidates": [],
            "budget": {
                "base_amount_usd": 3.0,
                "day_notional_usd": 3.0,
                "weekly_rollover_reserve_usd": 0.0,
                "remaining_spend_today_usd": 3.0,
            },
        }
    ]

    # Use tmp_path from pytest
    result_path = write_weekly_dashboard(run_states=run_states, output_dir=tmp_path, week_label="Week 42")

    assert result_path == str(tmp_path / "weekly_performance_dashboard.md")
    assert os.path.exists(result_path)

    with open(result_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "Weekly Performance Dashboard — Week 42" in content
    assert "2026-07-20" in content


def test_write_weekly_dashboard_creates_file_and_directories(tmp_path):
    import os

    from dca_weekly_dashboard import write_weekly_dashboard

    run_states = [
        {
            "trading_day": "2026-07-20",
            "status": "completed",
            "buy_candidates": [],
            "budget": {
                "base_amount_usd": 3.0,
                "day_notional_usd": 3.0,
                "weekly_rollover_reserve_usd": 0.0,
                "remaining_spend_today_usd": 3.0,
            },
        }
    ]

    # Path with non-existent subdirectories
    output_dir = tmp_path / "deep" / "nested" / "dir"

    result_path = write_weekly_dashboard(run_states=run_states, output_dir=output_dir, week_label="Week 42")

    assert result_path == str(output_dir / "weekly_performance_dashboard.md")
    assert os.path.exists(result_path)

    with open(result_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "Weekly Performance Dashboard — Week 42" in content
    assert "2026-07-20" in content
def test_build_coverage_missed_happy_path():
    run_states = [
        {
            "trading_day": "2023-10-01",
            "counts": {"buy_candidates": 5},
            "manual_review_tickers": ["AAPL", "GOOG"],
        },
    ]
    summaries = [
        {
            "trading_day": "2023-10-01",
            "coverage": {
                "buy_candidates": 5,
                "fills": 4,
                "coverage_pct": 80.0,
                "missing_fills": 1,
            }
        }
    ]
    result = build_coverage_missed(run_states, summaries)
    assert "## Coverage and missed opportunities" in result
    assert "| 2023-10-01 | 5 | 4 | 80.0% | 1 | 2 |" in result


def test_build_coverage_missed_missing_data():
    run_states = [
        {
            # No trading_day
            "counts": {"buy_candidates": 3},
            "manual_review_tickers": [],
        },
        {
            "trading_day": "2023-10-03",
            # missing counts and manual review
        }
    ]
    summaries = []

    result = build_coverage_missed(run_states, summaries)
    assert "| ? | 3 | n/a | n/a | n/a | 0 |" in result
    assert "| 2023-10-03 | 0 | n/a | n/a | n/a | 0 |" in result

