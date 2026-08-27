"""Tests for the monthly automation cost/usage tracker.

Covers month filtering of run_state artifacts, per-component aggregation of the
usage proxies, the estimate/proxy labeling in the rendered markdown, and the
connector-agnostic payload. Runnable with ``pytest`` or ``python
test_dca_cost_tracker.py``.
"""

import json
import tempfile
from pathlib import Path

from dca_cost_tracker import (
    COST_TRACKER_TITLE,
    _month_prefix,
    _num,
    aggregate_usage,
    build_cost_tracker_markdown,
    build_cost_tracker_payload,
    collect_month_run_states,
    find_run_state_files,
    generate_cost_tracker,
)


def _usage(**overrides):
    base = {
        "tickers_scanned": 3,
        "finance_calls_attempted": 3,
        "robinhood_calls_attempted": 3,
        "signal_rows_written": 6,
        "buy_candidates_written": 2,
        "order_reviews_attempted": 2,
        "orders_placed_count": 2,
        "state_file_bytes": 4000,
        "csv_artifact_count": 2,
        "total_artifact_bytes": 5000,
        "notifications_sent": 1,
        "errors_count": 0,
        "manual_recovery_required": False,
        "cost_control_notes": ["daily compact mode"],
    }
    base.update(overrides)
    return base


def _run_state(trading_day, **usage_overrides):
    return {
        "run_id": f"{trading_day}-154500",
        "trading_day": trading_day,
        "broker": "robinhood",
        "usage_proxy": _usage(**usage_overrides),
    }


def _write_runs(root, run_states):
    for i, rs in enumerate(run_states):
        run_dir = Path(root) / f"runs/{rs['trading_day']}-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_state.json").write_text(json.dumps(rs))


def test_month_prefix():
    assert _month_prefix(2026, 6) == "2026-06"
    assert _month_prefix(2026, 12) == "2026-12"
    assert _month_prefix(99, 1) == "0099-01"


def test_collect_filters_by_month(tmp_path):
    _write_runs(
        tmp_path,
        [
            _run_state("2026-06-29"),
            _run_state("2026-06-30"),
            _run_state("2026-07-01"),  # different month, excluded
        ],
    )
    june = collect_month_run_states(tmp_path, 2026, 6)
    days = sorted(rs["trading_day"] for rs in june)
    assert days == ["2026-06-29", "2026-06-30"]

    july = collect_month_run_states(tmp_path, 2026, 7)
    assert [rs["trading_day"] for rs in july] == ["2026-07-01"]


def test_collect_skips_blocked_stubs(tmp_path):
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-blocked"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps({"trading_day": "2026-06-30", "status": "blocked"}), encoding="utf-8"
    )

    states = collect_month_run_states(tmp_path, 2026, 6)
    assert len(states) == 1  # the date-gated stub is not an analyzed run
    agg = aggregate_usage(states, month_label="2026-06")
    assert agg["run_count"] == 1


def test_collect_retains_a_blocked_run_that_submitted_an_order(tmp_path):
    # Codex review ("Retain blocked runs that contain submitted orders"): a run
    # can be marked blocked AFTER an earlier candidate was successfully
    # submitted -- raw persistence failing on a LATER candidate is the concrete
    # case -- while still carrying real API activity, an order count, artifact
    # bytes and a recovery signal. Skipping every blocked state dropped all of
    # that from the monthly rollup, understating cost for exactly the runs that
    # went wrong.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-blocked-with-order"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "orders_submitted_count": 1,
                "buy_candidates": [
                    {"ticker": "AAA", "status": "submitted", "order_id": "ORD-1"},
                ],
                "usage_proxy": {"manual_recovery_required": True},
            }
        ),
        encoding="utf-8",
    )

    states = collect_month_run_states(tmp_path, 2026, 6)
    assert len(states) == 2, "a blocked run carrying a real order is still an analyzed run"
    agg = aggregate_usage(states, month_label="2026-06")
    assert agg["run_count"] == 2
    assert agg["manual_recovery_runs"] == 1


def test_collect_retains_a_blocked_run_with_outcome_only_order_evidence(tmp_path):
    # Codex review discussion_r3868141060 ("Retain blocked runs with
    # outcome-only order evidence"): buy_candidates and the top-level
    # orders_submitted_count are both OPTIONAL, and a valid blocked artifact can
    # record its accepted order in broker_outcomes alone -- which is exactly why
    # MorningDCAExecutor._existing_submitted_run_state() reads outcome-only
    # evidence too. Checking only the other two classified such a run as an empty
    # date-gate stub and dropped its API/recovery cost from the rollup.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-outcome-only"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "ORD-1"}},
            }
        ),
        encoding="utf-8",
    )

    assert len(collect_month_run_states(tmp_path, 2026, 6)) == 2


def test_collect_retains_a_blocked_run_whose_count_lives_only_in_counts(tmp_path):
    # The nested counts object is the other place the evidence can live on its
    # own -- morning_dca's blocked-retry carry-forward writes counts.submitted
    # alongside the top-level field, and an artifact carrying only the nested
    # one is the same analyzed run.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-nested-count"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "counts": {"submitted": 2, "buy_candidates": 2},
            }
        ),
        encoding="utf-8",
    )

    assert len(collect_month_run_states(tmp_path, 2026, 6)) == 2


def test_collect_still_skips_a_stub_whose_outcomes_and_counts_are_empty(tmp_path):
    # The other side of both additions: present-but-empty broker_outcomes and a
    # zeroed counts object are still just a date-gate stub.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-empty-outcomes"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "broker_outcomes": {},
                "counts": {"submitted": 0, "buy_candidates": 0},
            }
        ),
        encoding="utf-8",
    )

    assert len(collect_month_run_states(tmp_path, 2026, 6)) == 1


def test_collect_retains_a_blocked_run_that_evaluated_signals_before_blocking(tmp_path):
    # Codex review ("Retain evaluated blocked runs without usage proxies"): a
    # scheduled run can block AFTER it evaluated every signal but BEFORE it
    # ever reached placement -- a raw-persistence failure or an account-
    # pinning mismatch are the concrete cases -- carrying real connector/API
    # cost (a quote and a signal computation per ticker) with no order-bearing
    # evidence anywhere, since none was ever attempted. build_artifacts()
    # always populates counts.evaluated/signal_rows before any such failure is
    # classified, so this must count as an analyzed run even with zero orders
    # and no usage_proxy.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-evaluated-then-blocked"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "orders_submitted_count": 0,
                "buy_candidates": [],
                "counts": {"evaluated": 3, "submitted": 0},
                "signal_rows": [
                    {"ticker": "AAA", "action": "NO_ACTION"},
                    {"ticker": "BBB", "action": "NO_ACTION"},
                    {"ticker": "CCC", "action": "NO_ACTION"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert len(collect_month_run_states(tmp_path, 2026, 6)) == 2


def test_aggregate_usage_derives_a_fallback_for_evaluated_runs_with_no_proxy(tmp_path):
    # Codex review ("Derive usage for evaluated blocked runs"): retaining a
    # run via counts.evaluated only changes run_count -- aggregate_usage()
    # still skips every state with no usage_proxy block entirely, so
    # tickers_scanned/signal_rows_written and every other totaled field stay
    # zero for the exact runs the earlier fix was meant to surface. A
    # conservative fallback derived from signal_rows/counts.evaluated fills
    # in the one thing provably true (one row per evaluated ticker).
    evaluated_no_proxy = {
        "trading_day": "2026-06-30",
        "status": "blocked",
        "counts": {"evaluated": 3},
        "signal_rows": [
            {"ticker": "AAA", "action": "NO_ACTION"},
            {"ticker": "BBB", "action": "NO_ACTION"},
            {"ticker": "CCC", "action": "NO_ACTION"},
        ],
    }
    agg = aggregate_usage([evaluated_no_proxy], month_label="2026-06")
    assert agg["totals"]["tickers_scanned"] == 3.0
    assert agg["totals"]["signal_rows_written"] == 3.0
    # Everything this run genuinely has no evidence for stays at zero rather
    # than guessing.
    assert agg["totals"]["finance_calls_attempted"] == 0.0
    assert agg["totals"]["orders_placed_count"] == 0.0
    assert agg["manual_recovery_runs"] == 0


def test_aggregate_usage_derives_nothing_for_a_genuine_date_gate_stub(tmp_path):
    # The other side: no usage_proxy AND no evaluated signals at all is a
    # genuine empty stub and must still contribute nothing.
    stub = {"trading_day": "2026-06-30", "status": "blocked"}
    agg = aggregate_usage([stub], month_label="2026-06")
    assert agg["totals"]["tickers_scanned"] == 0.0
    assert agg["totals"]["signal_rows_written"] == 0.0


def test_aggregate_usage_real_proxy_takes_priority_over_the_fallback(tmp_path):
    # A run with a real usage_proxy must never be touched by the fallback
    # path, even if it also happens to carry evaluated signal_rows.
    real_proxy = {
        "trading_day": "2026-06-30",
        "status": "blocked",
        "usage_proxy": {"tickers_scanned": 9, "signal_rows_written": 9},
        "counts": {"evaluated": 3},
        "signal_rows": [{"ticker": "AAA"}],
    }
    agg = aggregate_usage([real_proxy], month_label="2026-06")
    assert agg["totals"]["tickers_scanned"] == 9.0
    assert agg["totals"]["signal_rows_written"] == 9.0


def test_collect_still_skips_a_stub_with_zero_evaluated_and_empty_signal_rows(tmp_path):
    # The other side: counts.evaluated present but zero, and signal_rows
    # present but empty, is still just a date-gate stub.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-zero-evaluated"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "counts": {"evaluated": 0},
                "signal_rows": [],
            }
        ),
        encoding="utf-8",
    )

    assert len(collect_month_run_states(tmp_path, 2026, 6)) == 1


def test_collect_retains_a_blocked_run_with_only_a_usage_proxy(tmp_path):
    # Order evidence is not the only signal worth keeping: a run that made API
    # calls and then blocked still has cost to account for.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-blocked-usage"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "usage_proxy": {"manual_recovery_required": False},
            }
        ),
        encoding="utf-8",
    )

    assert len(collect_month_run_states(tmp_path, 2026, 6)) == 2


def test_collect_skips_a_blocked_stub_with_an_empty_candidate_list(tmp_path):
    # The other side: buy_candidates present but empty, and no orders/usage, is
    # still just a date-gate stub.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-empty"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps(
            {
                "trading_day": "2026-06-30",
                "status": "blocked",
                "orders_submitted_count": 0,
                "buy_candidates": [],
            }
        ),
        encoding="utf-8",
    )

    assert len(collect_month_run_states(tmp_path, 2026, 6)) == 1


def test_collect_skips_unparseable_files(tmp_path):
    # Keep a valid run alongside the malformed artifact so the test verifies
    # parse failures are skipped without discarding successfully parsed runs.
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    bad_dir = tmp_path / "runs/bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "run_state.json").write_text('{"trading_day": "2026-06-30", not valid json')

    states = collect_month_run_states(tmp_path, 2026, 6)
    assert [state["trading_day"] for state in states] == ["2026-06-29"]


def test_aggregate_sums_and_component_breakdown():
    run_states = [
        _run_state("2026-06-29", finance_calls_attempted=3, orders_placed_count=2),
        _run_state("2026-06-30", finance_calls_attempted=3, orders_placed_count=1,
                   manual_recovery_required=True, errors_count=2),
    ]
    agg = aggregate_usage(run_states, month_label="2026-06")

    assert agg["run_count"] == 2
    assert agg["manual_recovery_runs"] == 1
    assert agg["totals"]["finance_calls_attempted"] == 6
    assert agg["totals"]["orders_placed_count"] == 3
    assert agg["totals"]["errors_count"] == 2

    by_component = {c["component"]: c for c in agg["components"]}
    # order_execution count = reviews(2+2) + orders(2+1) = 7, weight 2.0 -> 14.0
    assert by_component["order_execution"]["count"] == 7
    assert by_component["order_execution"]["estimated_proxy_units"] == 14.0
    # finance_data count 6, weight 1.0 -> 6.0
    assert by_component["finance_data"]["estimated_proxy_units"] == 6.0
    assert agg["estimated_total_proxy_units"] > 0


def test_aggregate_handles_missing_usage_block():
    run_states = [{"trading_day": "2026-06-29"}]  # no usage_proxy
    agg = aggregate_usage(run_states, month_label="2026-06")
    assert agg["run_count"] == 1
    assert agg["totals"]["finance_calls_attempted"] == 0
    assert agg["estimated_total_proxy_units"] == 0


def test_markdown_labels_estimates_not_credits():
    agg = aggregate_usage([_run_state("2026-06-29")], month_label="2026-06")
    md = build_cost_tracker_markdown(agg)
    assert md.startswith(f"## {COST_TRACKER_TITLE} — 2026-06")
    assert "Estimates only" in md
    assert "not exact credit" in md
    assert "not credits" in md  # component table header
    assert "| Component | Count | Proxy weight | Est. proxy units |" in md
    # a couple of components should be present
    assert "finance_data" in md
    assert "order_execution" in md
    # raw metric table present
    assert "Raw monthly usage proxies" in md


def test_payload_is_connector_agnostic():
    agg = aggregate_usage([_run_state("2026-06-29")], month_label="2026-06")
    payload = build_cost_tracker_payload(agg)
    assert payload["title"] == COST_TRACKER_TITLE
    assert payload["page_id"] is None  # supplied by caller, never embedded
    assert payload["value_type"] == "estimate_proxy"
    assert payload["markdown"] == build_cost_tracker_markdown(agg)

    with_page = build_cost_tracker_payload(agg, page_id="PAGE123")
    assert with_page["page_id"] == "PAGE123"


def test_build_cost_tracker_payload():
    agg = {"month_label": "2026-06", "totals": {}, "components": []}
    payload = build_cost_tracker_payload(agg, page_id="page_123")
    assert payload["title"] == COST_TRACKER_TITLE
    assert payload["page_id"] == "page_123"
    assert payload["month_label"] == "2026-06"
    assert payload["aggregate"] == agg
    assert payload["value_type"] == "estimate_proxy"
    assert isinstance(payload["markdown"], str)


def test_collect_skips_unreadable_files(tmp_path):
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    bad_dir = tmp_path / "runs/bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "run_state.json").write_bytes(b"\x80abc")

    states = collect_month_run_states(tmp_path, 2026, 6)
    assert len(states) == 1  # bad file skipped, good one kept


def test_markdown_no_components():
    md = build_cost_tracker_markdown({})
    assert "| _no runs_ | 0 | - | 0 |" in md


def test_generate_cost_tracker_writes_files(tmp_path):
    _write_runs(tmp_path, [_run_state("2026-06-29"), _run_state("2026-06-30")])
    out_dir = tmp_path / "cost"
    paths = generate_cost_tracker(tmp_path, 2026, 6, out_dir, page_id="NOTION_PAGE")

    assert Path(paths["markdown"]).exists()
    assert Path(paths["payload"]).exists()
    payload = json.loads(Path(paths["payload"]).read_bytes())
    assert payload["month_label"] == "2026-06"
    assert payload["aggregate"]["run_count"] == 2
    assert payload["page_id"] == "NOTION_PAGE"


def test_num():
    assert _num(10) == 10.0
    assert _num("42.5") == 42.5
    assert _num("-3") == -3.0
    assert _num(0) == 0.0
    assert _num(None) == 0.0
    assert _num("invalid") == 0.0
    assert _num([]) == 0.0
    assert _num({}) == 0.0


def test_find_run_state_files(tmp_path):
    # Setup some test files
    dir_b = tmp_path / "runs" / "b"
    dir_b.mkdir(parents=True, exist_ok=True)
    file_b = dir_b / "run_state.json"
    file_b.write_text("{}")

    dir_a = tmp_path / "runs" / "a"
    dir_a.mkdir(parents=True, exist_ok=True)
    file_a = dir_a / "run_state.json"
    file_a.write_text("{}")

    dir_c = tmp_path / "runs" / "c"
    dir_c.mkdir(parents=True, exist_ok=True)
    (dir_c / "other.json").write_text("{}")

    file_root = tmp_path / "run_state.json"
    file_root.write_text("{}")

    # Test directory searching and sorting
    found_files = find_run_state_files(tmp_path)

    # We expect file_root, file_a, file_b in some sorted order
    # rglob returns path objects that sort alphabetically by their full path
    # 'a' comes before 'b', and 'run_state.json' might come before 'runs' depending on sorting
    # Path sorting is consistent, let's just assert on the set first and then order

    expected_paths = {file_root, file_a, file_b}
    assert set(found_files) == expected_paths
    assert len(found_files) == 3

    # check that sorting is deterministic (path sorting)
    assert found_files == sorted([file_root, file_a, file_b])

    # Test direct file path
    found_single = find_run_state_files(file_a)
    assert found_single == [file_a]

    # Test non-matching file path
    other_file = tmp_path / "other.json"
    other_file.write_text("{}")
    assert find_run_state_files(other_file) == []

    # Test empty directory
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir(parents=True, exist_ok=True)
    assert find_run_state_files(empty_dir) == []


def _run_all():
    import inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                # tmp_path-style tests get a throwaway directory outside pytest.
                with tempfile.TemporaryDirectory() as tmp:
                    fn(Path(tmp))
            else:
                fn()
            print(f"PASS {name}")
    print("All cost-tracker self-tests passed.")


if __name__ == "__main__":
    _run_all()
