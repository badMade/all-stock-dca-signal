"""Tests for the DCA weekly dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dca_weekly_dashboard import (
    _has_submitted_evidence,
    _is_manual_run_state,
    _iso_week_of_state,
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
    assert "| Executed notional | $7.00 |" in report


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
