"""Tests for the hybrid Notion reporting pipeline.

Covers target_price_missing handling, latest_run_summary generation, the compact
Notion markdown builder, and weekly dashboard section generation. Runnable with
``pytest`` or ``python test_dca_reporting.py``.
"""

import json

from dca_notion_sync import (
    COMPACT_SYNC_TITLE,
    build_compact_sync_markdown,
    build_notion_sync_payload,
)
from dca_reporting import (
    _drop_excluded_fills,
    _is_manual_run_state,
    _to_float,
    build_coverage_report_md,
    build_latest_run_summary,
    build_missing_fills,
    compute_slippage_rows,
    fills_from_order_history_payload,
    generate_reports,
    load_run_state,
    rank_slippage,
)
from dca_weekly_dashboard import (
    build_execution_quality,
    build_graded_vs_fixed,
    build_sector_matrix,
    build_weekly_dashboard,
    load_json_files,
)


def _run_state():
    return {
        "run_id": "2026-06-29-154500",
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T15:45:00-04:00",
        "broker": "robinhood",
        "dry_run": False,
        "counts": {"buy_candidates": 3, "submitted": 3, "manual_review": 1},
        "manual_review_tickers": ["MSFT"],
        "budget": {
            "base_buy_amount_usd": 3.0,
            "budget_tier": "normal",
            "daily_cap_usd": 180.0,
            "day_notional_usd": 6.5,
            "weekly_rollover_reserve_usd": 893.5,
            "remaining_spend_today_usd": 173.5,
            "week_notional_usd": 6.5,
            "weekly_cap_usd": 900.0,
        },
        "buy_candidates": [
            {"ticker": "AAPL", "planned_amount_usd": 2.0, "buy_multiplier": 2.0,
             "target_price": 100.0, "target_price_missing": False, "status": "submitted", "order_id": "O1"},
            {"ticker": "NVDA", "planned_amount_usd": 3.0, "buy_multiplier": 3.0,
             "target_price": 50.0, "target_price_missing": False, "status": "submitted", "order_id": "O2"},
            {"ticker": "AMD", "planned_amount_usd": 1.5, "buy_multiplier": 1.5,
             "target_price": None, "target_price_missing": True, "status": "submitted", "order_id": "O3"},
        ],
    }


def _fills():
    return [
        {"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.0198, "order_id": "O1"},
        {"ticker": "NVDA", "fill_price": 49.5, "filled_qty": 0.0606, "order_id": "O2"},
        {"ticker": "AMD", "fill_price": 30.0, "filled_qty": 0.05, "order_id": "O3"},
    ]


def test_fills_from_order_history_payload_shapes():
    # Bare list, {"orders": [...]}, and normalized {"data": ...} pointer all work.
    orders = [
        {
            "ticker": "AAPL",
            "id": "O1",
            "executions": [
                {"quantity": "0.01", "price": "100.0"},
                {"quantity": "0.01", "price": "102.0"},
            ],
        },
        {"symbol": "NVDA", "order_id": "O2", "filled_qty": "0.06", "filled_avg_price": "49.5"},
        {"ticker": "SKIP", "id": "O3", "quantity": "0"},  # no executed qty -> skipped
    ]
    for payload in (orders, {"orders": orders}, {"results": orders}, {"output_path": "x", "data": orders}):
        fills = fills_from_order_history_payload(payload)
        by_ticker = {f["ticker"]: f for f in fills}
        assert set(by_ticker) == {"AAPL", "NVDA"}
        # AAPL avg = weighted mean of executions = 101.0 across 0.02 shares.
        assert abs(by_ticker["AAPL"]["filled_avg_price"] - 101.0) < 1e-9
        assert abs(by_ticker["AAPL"]["filled_qty"] - 0.02) < 1e-9
        assert by_ticker["AAPL"]["executions"] == 2
        assert by_ticker["AAPL"]["order_id"] == "O1"
        # NVDA falls back to filled_avg_price when no executions list is present.
        assert abs(by_ticker["NVDA"]["fill_price"] - 49.5) < 1e-9


def test_fills_from_order_history_payload_feeds_slippage():
    payload = {"orders": [
        {"ticker": "AAPL", "id": "O1", "filled_qty": "0.0198", "filled_avg_price": "101.0"},
        {"ticker": "NVDA", "id": "O2", "filled_qty": "0.0606", "filled_avg_price": "49.5"},
    ]}
    fills = fills_from_order_history_payload(payload)
    rows = compute_slippage_rows(_run_state()["buy_candidates"], fills)
    by_ticker = {r["ticker"]: r for r in rows}
    assert abs(by_ticker["AAPL"]["slippage_pct"] - 1.0) < 1e-6
    assert by_ticker["NVDA"]["slippage_pct"] < 0


def test_fills_from_order_history_payload_empty():
    assert fills_from_order_history_payload({}) == []
    assert fills_from_order_history_payload([]) == []
    assert fills_from_order_history_payload({"orders": []}) == []


def test_target_price_missing_excludes_slippage():
    rows = compute_slippage_rows(_run_state()["buy_candidates"], _fills())
    by_ticker = {r["ticker"]: r for r in rows}

    # AMD has no target price -> flagged and slippage stays null.
    assert by_ticker["AMD"]["target_price_missing"] is True
    assert by_ticker["AMD"]["slippage_pct"] is None
    assert by_ticker["AMD"]["slippage_abs"] is None

    # AAPL filled above target -> positive slippage.
    assert by_ticker["AAPL"]["target_price_missing"] is False
    assert abs(by_ticker["AAPL"]["slippage_pct"] - 1.0) < 1e-6

    # NVDA filled below target -> negative (favorable) slippage.
    assert by_ticker["NVDA"]["slippage_pct"] < 0


def test_rank_slippage_pushes_missing_target_last():
    rows = compute_slippage_rows(_run_state()["buy_candidates"], _fills())
    ranked = rank_slippage(rows)
    assert ranked[0]["ticker"] == "AAPL"  # worst (highest positive) first
    assert ranked[-1]["target_price_missing"] is True  # missing target last


def test_latest_run_summary_generation():
    run_state = _run_state()
    rows = compute_slippage_rows(run_state["buy_candidates"], _fills())
    missing = build_missing_fills(run_state["buy_candidates"], _fills())
    summary = build_latest_run_summary(run_state, rows, missing)

    assert summary["run_id"] == "2026-06-29-154500"
    assert summary["coverage"]["buy_candidates"] == 3
    assert summary["coverage"]["fills"] == 3
    assert summary["coverage"]["missing_fills"] == 0
    assert summary["coverage"]["coverage_pct"] == 100.0
    # Only two of three fills are priced (AMD missing target).
    assert summary["execution_quality"]["priced_fills"] == 2
    assert summary["execution_quality"]["target_price_missing"] == 1
    assert summary["execution_quality"]["worst_ticker"] == "AAPL"
    assert summary["budget"]["planned_notional_usd"] == 6.5


def test_latest_run_summary_mirrors_run_classification():
    run_state = _run_state()
    run_state["run_classification"] = {
        "run_origin": "scheduled",
        "manual_run": False,
        "stream": "afternoon",
        "cron_id": "10ce362c",
        "supersedes_cron_id": "ce16ba6b",
        "run_id": "scheduled_20260722_1520ET",
    }
    rows = compute_slippage_rows(run_state["buy_candidates"], _fills())
    missing = build_missing_fills(run_state["buy_candidates"], _fills())
    summary = build_latest_run_summary(run_state, rows, missing)

    # run_summary MUST carry the SAME non-null run_classification as run_state.
    assert summary["run_classification"] == run_state["run_classification"]
    assert "artifact_contract_failure" not in summary


def test_latest_run_summary_marks_contract_failure_when_classification_null():
    run_state = _run_state()  # no run_classification
    rows = compute_slippage_rows(run_state["buy_candidates"], _fills())
    missing = build_missing_fills(run_state["buy_candidates"], _fills())
    summary = build_latest_run_summary(run_state, rows, missing)

    assert summary["run_classification"] is None
    assert summary["artifact_contract_failure"]["failure"] == "artifact_contract_failure"


def test_missing_fills_reported_when_no_fill():
    run_state = _run_state()
    fills = [f for f in _fills() if f["ticker"] != "NVDA"]
    missing = build_missing_fills(run_state["buy_candidates"], fills)
    tickers = {m["ticker"] for m in missing}
    assert tickers == {"NVDA"}
    assert missing[0]["skip_reason"]


def test_missing_fills_excludes_top_up_skipped_candidates():
    run_state = _run_state()
    # NVDA's afternoon buy was fully offset by an earlier same-day run; the guard
    # intentionally ordered $0 for it, so it must not surface as a missing fill.
    run_state["buy_candidates"][1]["top_up_skipped"] = True
    fills = [f for f in _fills() if f["ticker"] != "NVDA"]
    missing = build_missing_fills(run_state["buy_candidates"], fills)
    assert {m["ticker"] for m in missing} == set()



def test_latest_run_summary_excludes_top_up_skipped_from_coverage():
    run_state = _run_state()
    run_state["buy_candidates"][1]["top_up_skipped"] = True
    fills = [f for f in _fills() if f["ticker"] != "NVDA"]
    rows = compute_slippage_rows(run_state["buy_candidates"], fills)
    missing = build_missing_fills(run_state["buy_candidates"], fills)
    summary = build_latest_run_summary(run_state, rows, missing)

    # Only AAPL and AMD count toward the coverage universe; both filled.
    assert summary["coverage"]["buy_candidates"] == 2
    assert summary["coverage"]["missing_fills"] == 0
    assert summary["coverage"]["coverage_pct"] == 100.0


def test_build_coverage_report_md_populated_tables():
    run_state = _run_state()
    rows = compute_slippage_rows(run_state["buy_candidates"], _fills())
    missing = build_missing_fills(run_state["buy_candidates"], _fills())
    summary = build_latest_run_summary(run_state, rows, missing)

    md = build_coverage_report_md(summary, rows, missing)

    # Header and metadata
    assert "# Slippage & Coverage Report" in md
    assert "2026-06-29" in md
    assert "2026-06-29-154500" in md
    assert "robinhood" in md

    # Slippage table rendered (not the empty placeholder)
    assert "| Ticker | Target | Fill | Slippage % | Note |" in md
    assert "AAPL" in md
    assert "_No fills to rank._" not in md

    # Missing-fills table should be absent (all filled)
    assert "_No missing fills._" in md
    assert "| Ticker | Planned $ | Status | Reason |" not in md


def test_build_coverage_report_md_empty_tables():
    run_state = _run_state()
    # No fills at all → all candidates become missing fills; slippage rows are empty.
    rows = compute_slippage_rows(run_state["buy_candidates"], [])
    missing = build_missing_fills(run_state["buy_candidates"], [])
    summary = build_latest_run_summary(run_state, rows, missing)

    md = build_coverage_report_md(summary, rows, missing)

    # Slippage table replaced by empty-state sentinel
    assert "_No fills to rank._" in md
    assert "| Ticker | Target | Fill | Slippage % | Note |" not in md

    # Missing-fills table rendered with all three candidates
    assert "| Ticker | Planned $ | Status | Reason |" in md
    assert "AAPL" in md
    assert "NVDA" in md
    assert "AMD" in md
    assert "_No missing fills._" not in md


def test_compact_notion_markdown_generation():
    run_state = _run_state()
    rows = compute_slippage_rows(run_state["buy_candidates"], _fills())
    missing = build_missing_fills(run_state["buy_candidates"], _fills())
    summary = build_latest_run_summary(run_state, rows, missing)

    md = build_compact_sync_markdown(summary)
    assert md.startswith(f"## {COMPACT_SYNC_TITLE}")
    assert "2026-06-29" in md
    assert "Buy candidates" in md
    assert "target_price_missing" in md
    assert "| Metric | Value |" in md

    payload = build_notion_sync_payload(summary, page_id="PAGE123")
    assert payload["title"] == COMPACT_SYNC_TITLE
    assert payload["page_id"] == "PAGE123"
    assert payload["markdown"] == md
    assert payload["metrics"]["coverage"]["coverage_pct"] == 100.0


def test_no_secrets_in_notion_module():
    # The compact sync must remain connector-agnostic: no hard-coded page id/token.
    import dca_notion_sync

    # Default payload carries no page id and no credential.
    assert build_notion_sync_payload(_summary_fixture())["page_id"] is None
    assert not hasattr(dca_notion_sync, "NOTION_TOKEN")


def _summary_fixture():
    run_state = _run_state()
    rows = compute_slippage_rows(run_state["buy_candidates"], _fills())
    missing = build_missing_fills(run_state["buy_candidates"], _fills())
    return build_latest_run_summary(run_state, rows, missing)


def test_weekly_dashboard_section_generation():
    run_states = [_run_state()]
    summaries = [_summary_fixture()]

    dashboard = build_weekly_dashboard(run_states, summaries, sector_map={"AAPL": "Tech", "NVDA": "Semis"})
    for heading in [
        "## Performance snapshot",
        "## Graded DCA vs fixed daily DCA",
        "## Sector performance matrix",
        "## Budget and reserve effectiveness",
        "## Execution quality",
        "## Coverage and missed opportunities",
        "## Weekly review notes",
    ]:
        assert heading in dashboard, f"missing section: {heading}"


def test_weekly_dashboard_snapshot_and_review_notes_values():
    run_states = [_run_state()]
    summaries = [_summary_fixture()]

    dashboard = build_weekly_dashboard(run_states, summaries, sector_map={"AAPL": "Tech", "NVDA": "Semis"})

    assert "| Run days | 1 |" in dashboard
    assert "| Buy candidates | 3 |" in dashboard
    assert "| Orders submitted | 3 |" in dashboard
    assert "| Planned notional | $6.50 |" in dashboard
    assert "| Executed notional | $6.50 |" in dashboard
    assert "| Week notional / cap | $6.50 / $900.00 |" in dashboard

    assert "- Runs analyzed: 1 day(s)." in dashboard
    assert "- All buy candidates were covered by fills." in dashboard
    assert "- 1 fill(s) lacked a target price; slippage excluded — check signal capture." in dashboard
    assert "- 1 manual-review flag(s) across the week — reconcile finance vs Robinhood." in dashboard
    assert "- Add qualitative notes here before the Notion dashboard refresh." in dashboard


def test_weekly_graded_vs_fixed_delta():
    run_states = [_run_state()]
    md = build_graded_vs_fixed(run_states)
    # Graded total 6.5 vs fixed 3x base * 3 candidates = 9.0 -> delta -2.50.
    assert "-2.50" in md


def test_weekly_graded_vs_fixed_missing_base_amount_uses_pre_change_default():
    # Codex review: a legacy run_state dated before the Jul 19 2026 base-amount
    # change may predate this field being tracked at all; every such artifact
    # used $3, so build_graded_vs_fixed must use that known value instead of
    # raising and blocking the whole weekly report.
    run_states = [{
        "trading_day": "2026-07-10",
        "budget": {},
        "buy_candidates": [{"ticker": "AAPL", "planned_amount_usd": 3.0}],
    }]
    md = build_graded_vs_fixed(run_states)
    # Fixed baseline: 1 candidate * pre-change $3 base = $3.00.
    assert "$3.00" in md


def test_weekly_graded_vs_fixed_missing_base_amount_raises_on_or_after_change():
    # A run_state dated on/after the change is expected to always carry the
    # field; a missing value there is a genuine anomaly, not a legacy gap.
    run_states = [{
        "trading_day": "2026-07-19",
        "budget": {},
        "buy_candidates": [{"ticker": "AAPL", "planned_amount_usd": 2.0}],
    }]
    try:
        build_graded_vs_fixed(run_states)
    except ValueError as exc:
        assert "base_amount_usd" in str(exc)
    else:
        raise AssertionError("expected ValueError for a missing base amount on/after the change")


def test_weekly_sector_matrix_buckets_unclassified():
    md = build_sector_matrix([_run_state()], sector_map={"AAPL": "Tech"})
    assert "Tech" in md
    assert "Unclassified" in md  # NVDA and AMD are unmapped


def test_weekly_execution_quality_week_avg():
    md = build_execution_quality([_summary_fixture()])
    assert "Week avg" in md
    assert "Priced fills" in md


def test_generate_reports_writes_all_files(tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = tmp_path or Path(tempfile.mkdtemp())
    paths = generate_reports(_run_state(), _fills(), directory)
    for key in ["slippage_ranked", "missing_fills_report", "slippage_and_coverage_report", "latest_run_summary"]:
        assert Path(paths[key]).exists(), f"missing output: {key}"

    summary = json.loads(Path(paths["latest_run_summary"]).read_bytes())
    assert summary["execution_quality"]["target_price_missing"] == 1

    report = Path(paths["slippage_and_coverage_report"]).read_text()
    assert "Slippage & Coverage Report" in report
    assert "target_price_missing" in report


def test_generate_reports_excludes_fills_by_ticker(tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = tmp_path or Path(tempfile.mkdtemp())
    # NVDA is a manual-run ticker that leaked into same-day broker fills.
    paths = generate_reports(_run_state(), _fills(), directory, excluded_tickers=frozenset(["NVDA"]))
    summary = json.loads(Path(paths["latest_run_summary"]).read_bytes())
    # AAPL + AMD remain filled; NVDA's excluded fill becomes a missing fill.
    assert summary["coverage"]["fills"] == 2
    assert "NVDA" not in Path(paths["slippage_ranked"]).read_text()


def test_generate_reports_rejects_manual_state_when_scheduled_required(tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = tmp_path or Path(tempfile.mkdtemp())
    manual = _run_state()
    manual["run_classification"] = {"run_origin": "manual", "manual_run": True}
    try:
        generate_reports(manual, _fills(), directory, require_scheduled=True)
    except ValueError as exc:
        assert "manual run_state" in str(exc)
    else:
        raise AssertionError("expected ValueError for a manual run_state")


def _classification_helpers():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
    from run_classification import (  # noqa: E402
        build_manual_classification,
        build_scheduled_classification,
        write_run_artifacts,
    )
    return build_scheduled_classification, build_manual_classification, write_run_artifacts


def _sched_state(day, order_ids, rc):
    return {
        "run_id": f"{day}-152001", "generated_at": f"{day}T15:20:01",
        "trading_day": day, "broker": "robinhood",
        "budget": {"budget_tier": "normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
        "counts": {"submitted": len(order_ids)},
        "run_classification": rc,
        "buy_candidates": [
            {"ticker": t, "buy_multiplier": 2.0, "planned_amount_usd": 3.0,
             "target_price": 100.0, "target_price_missing": False,
             "status": "submitted", "order_id": oid}
            for t, oid in order_ids
        ],
    }


def test_generate_scheduled_reports_routes_through_classification(tmp_path=None):
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports

    build_sched, build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"

    write_run_artifacts(
        _sched_state(day, [("AAPL", "O1"), ("NVDA", "O2")], build_sched(day, f"{day}T19:20:01Z")),
        base=base,
    )
    # A manual run that also touched O2 -> that fill must be excluded.
    manual = _sched_state(day, [("NVDA", "O2")], build_manual(f"{day}T15:00:00Z"))
    manual["run_id"] = f"{day}-110000"
    write_run_artifacts(manual, base=base)

    fills = [
        {"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.03, "order_id": "O1"},
        {"ticker": "NVDA", "fill_price": 49.5, "filled_qty": 0.06, "order_id": "O2"},
    ]
    paths = generate_scheduled_reports(day, fills, base / "reports", base=base)
    summary = json.loads(Path(paths["latest_run_summary"]).read_bytes())
    assert summary["coverage"]["fills"] == 1  # only AAPL; NVDA's O2 fill excluded as manual
    assert "NVDA" not in Path(paths["slippage_ranked"]).read_text()


def test_generate_scheduled_reports_retains_scheduled_fill_when_manual_order_shares_ticker(tmp_path=None):
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports

    build_sched, build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"

    write_run_artifacts(
        _sched_state(day, [("AAPL", "O1"), ("NVDA", "O2")], build_sched(day, f"{day}T19:20:01Z")),
        base=base,
    )
    # A manual run that ALSO traded NVDA, but under a distinct order id (O3).
    # Excluding by ticker would wrongly drop the scheduled NVDA fill (O2) too;
    # exclusion must stay scoped to the manual order id.
    manual = _sched_state(day, [("NVDA", "O3")], build_manual(f"{day}T15:00:00Z"))
    manual["run_id"] = f"{day}-110000"
    write_run_artifacts(manual, base=base)

    fills = [
        {"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.03, "order_id": "O1"},
        {"ticker": "NVDA", "fill_price": 49.5, "filled_qty": 0.06, "order_id": "O2"},
        {"ticker": "NVDA", "fill_price": 50.0, "filled_qty": 0.01, "order_id": "O3"},
    ]
    paths = generate_scheduled_reports(day, fills, base / "reports", base=base)
    summary = json.loads(Path(paths["latest_run_summary"]).read_bytes())
    # AAPL (O1) and the scheduled NVDA fill (O2) both survive; only the
    # manual NVDA order (O3) is excluded.
    assert summary["coverage"]["fills"] == 2
    ranked = Path(paths["slippage_ranked"]).read_text()
    assert "49.5" in ranked


def test_generate_scheduled_reports_does_not_write_model_usage_log_by_default(tmp_path=None):
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports

    build_sched, _build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"
    write_run_artifacts(
        _sched_state(day, [("AAPL", "O1")], build_sched(day, f"{day}T19:20:01Z")), base=base,
    )
    fills = [{"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.03, "order_id": "O1"}]
    generate_scheduled_reports(day, fills, base / "reports", base=base)
    assert not (base / "model_usage").exists()


def test_generate_scheduled_reports_appends_model_usage_log_entry_when_root_configured(tmp_path=None):
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports
    from scripts.model_usage_log import resolve_log_path

    build_sched, _build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"
    write_run_artifacts(
        _sched_state(day, [("AAPL", "O1")], build_sched(day, f"{day}T19:20:01Z")), base=base,
    )
    fills = [{"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.03, "order_id": "O1"}]
    log_root = base / "model_usage"
    report_paths = generate_scheduled_reports(
        day, fills, base / "reports", base=base, model_usage_log_root=log_root
    )

    from datetime import datetime, timezone

    log_path = resolve_log_path(datetime.now(timezone.utc), root=log_root)
    assert log_path.exists()
    entry = json.loads(log_path.read_bytes().splitlines()[0])
    assert entry["cron_id"] == "67aa802b"
    # A distinct run_id/artifact for THIS reporting task, not the 15:20 live
    # run's own run_id/run_state.json -- the live run's run_id is still
    # referenced in notes.
    assert entry["run_id"] == f"reporting_{day}_1700ET"
    assert entry["artifact_path"] == str(base / "reports")
    assert f"{day}-152001" in entry["notes"]
    assert entry["run_origin"] == "scheduled"
    assert Path(report_paths["latest_run_summary"]).exists()


def test_generate_scheduled_reports_records_premium_routing_metadata_when_supplied(tmp_path=None):
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports
    from scripts.model_usage_log import resolve_log_path

    build_sched, _build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"
    write_run_artifacts(
        _sched_state(day, [("AAPL", "O1")], build_sched(day, f"{day}T19:20:01Z")), base=base,
    )
    fills = [{"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.03, "order_id": "O1"}]
    log_root = base / "model_usage"
    generate_scheduled_reports(
        day, fills, base / "reports", base=base,
        model_usage_log_root=log_root,
        model_usage_model_routing_mode="premium",
        model_usage_reason_for_premium_model="explicit_user_request",
    )
    log_path = resolve_log_path(datetime.now(timezone.utc), root=log_root)
    entry = json.loads(log_path.read_bytes().splitlines()[0])
    assert entry["model_routing_mode"] == "premium"
    assert entry["reason_for_premium_model"] == "explicit_user_request"


def test_generate_scheduled_reports_logs_even_when_report_generation_fails(tmp_path, monkeypatch):
    # If generate_reports() raises (e.g. a report artifact can't be written),
    # the append still happens -- the one-entry-per-task-run contract holds
    # even for a failed run -- and the original exception still propagates.
    from datetime import datetime, timezone

    import dca_reporting
    from scripts.model_usage_log import resolve_log_path

    build_sched, _build_manual, write_run_artifacts = _classification_helpers()
    base = tmp_path / "root"
    day = "2026-07-06"
    write_run_artifacts(
        _sched_state(day, [("AAPL", "O1")], build_sched(day, f"{day}T19:20:01Z")), base=base,
    )
    fills = [{"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.03, "order_id": "O1"}]

    def _boom(*args, **kwargs):
        raise RuntimeError("report artifact write failed")

    monkeypatch.setattr(dca_reporting, "generate_reports", _boom)

    log_root = base / "model_usage"
    try:
        dca_reporting.generate_scheduled_reports(
            day, fills, base / "reports", base=base, model_usage_log_root=log_root
        )
        raise AssertionError("expected RuntimeError to propagate")
    except RuntimeError as exc:
        assert "report artifact write failed" in str(exc)

    log_path = resolve_log_path(datetime.now(timezone.utc), root=log_root)
    assert log_path.exists()
    entry = json.loads(log_path.read_bytes().splitlines()[0])
    assert entry["run_id"] == f"reporting_{day}_1700ET"


def test_generate_scheduled_reports_excludes_fills_for_confirmation_blocked_run(tmp_path=None):
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports

    build_sched, _build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"

    state = _sched_state(day, [], build_sched(day, f"{day}T19:20:01Z"))
    state["order_submit_status"] = "not_submitted_confirmation_required"
    state["buy_candidates"] = [
        {"ticker": "AAPL", "buy_multiplier": 2.0, "planned_amount_usd": 3.0,
         "target_price": 100.0, "target_price_missing": False,
         "status": "not_submitted_confirmation_required", "order_id": None},
    ]
    write_run_artifacts(state, base=base)

    # A same-ticker broker fill unrelated to this run must never be attributed
    # to a confirmation-blocked run, which placed zero orders by contract.
    fills = [{"ticker": "AAPL", "fill_price": 101.0, "filled_qty": 0.03, "order_id": "UNRELATED-1"}]
    paths = generate_scheduled_reports(day, fills, base / "reports", base=base)
    summary = json.loads(Path(paths["latest_run_summary"]).read_bytes())
    assert summary["coverage"]["fills"] == 0


def test_generate_scheduled_reports_raw_fallback_survives_null_broker_outcomes(tmp_path=None):
    # Codex review (PR #88): "Guard malformed broker outcomes in raw-fill
    # fallback". (selection.state or {}).get("broker_outcomes", {}).items()
    # only substitutes the {} default when the key is ABSENT -- a parseable
    # but structurally corrupted scheduled state with "broker_outcomes": null
    # (or a list) returns None/a list from .get() and raises
    # AttributeError/TypeError from .items(), crashing the whole reporting
    # job instead of falling back to the caller-supplied fills. The same gap
    # existed for "buy_candidates": null two lines above.
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports

    build_sched, _build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"

    state = _sched_state(day, [("AAPL", "O1")], build_sched(day, f"{day}T19:20:01Z"))
    # Structurally corrupt but still a valid scheduled state otherwise.
    state["broker_outcomes"] = None
    written = write_run_artifacts(state, base=base)

    # No caller-supplied fill for O1 -> missing_ids triggers the raw-fill
    # fallback, which reads orders_after_place.json next to the state file.
    raw_dir = Path(written["run_state"]).parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "orders_after_place.json").write_text(
        json.dumps({"orders": [
            {"id": "O1", "symbol": "AAPL", "state": "filled",
             "cumulative_quantity": "0.03", "average_price": "101.0"},
        ]}),
        encoding="utf-8",
    )

    # Must not raise AttributeError/TypeError from the null broker_outcomes.
    paths = generate_scheduled_reports(day, [], base / "reports", base=base)
    summary = json.loads(Path(paths["latest_run_summary"]).read_bytes())
    assert summary["coverage"]["fills"] == 1, (
        "raw-fill fallback should have resolved AAPL's O1 fill despite null broker_outcomes"
    )


def test_generate_scheduled_reports_raw_fallback_survives_non_utf8_orders_file(tmp_path=None):
    # Codex review (PR #88): "Handle non-UTF-8 persisted raw orders as a
    # fallback miss". orders_after_place.json containing invalid UTF-8 bytes
    # (e.g. truncated mid-write) raises UnicodeDecodeError from
    # read_text(encoding="utf-8"), which the except clause didn't cover
    # (UnicodeDecodeError is a ValueError subclass, not an OSError). The
    # fallback already treats unreadable/malformed JSON as non-fatal; this
    # must degrade the same way instead of crashing the whole reporting job.
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports

    build_sched, _build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"

    state = _sched_state(day, [("AAPL", "O1")], build_sched(day, f"{day}T19:20:01Z"))
    written = write_run_artifacts(state, base=base)

    raw_dir = Path(written["run_state"]).parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "orders_after_place.json").write_bytes(b"\xff\xfe\x00invalid-utf8")

    # Must not raise UnicodeDecodeError -- degrades to the normal missing-fill
    # report instead (no caller-supplied fill for O1, and the raw fallback
    # can't be read either).
    paths = generate_scheduled_reports(day, [], base / "reports", base=base)
    summary = json.loads(Path(paths["latest_run_summary"]).read_bytes())
    assert summary["coverage"]["fills"] == 0


def test_generate_scheduled_reports_requires_scheduled_state(tmp_path=None):
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports

    build_sched, build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"
    # Only a manual run exists -> no scheduled inference allowed.
    write_run_artifacts(_sched_state(day, [("NVDA", "O2")], build_manual(f"{day}T15:00:00Z")), base=base)
    try:
        generate_scheduled_reports(day, [], base / "reports", base=base)
    except ValueError as exc:
        assert "no valid scheduled run_state" in str(exc)
    else:
        raise AssertionError("expected ValueError when no scheduled state exists")


def test_load_scheduled_run_states_only_scheduled(tmp_path=None):
    import tempfile
    from pathlib import Path

    from dca_weekly_dashboard import load_scheduled_run_states

    build_sched, build_manual, write_run_artifacts = _classification_helpers()
    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-06"
    write_run_artifacts(_sched_state(day, [("AAPL", "O1")], build_sched(day, f"{day}T19:20:01Z")), base=base)
    manual = _sched_state(day, [("NVDA", "O2")], build_manual(f"{day}T15:00:00Z"))
    manual["run_id"] = f"{day}-110000"
    write_run_artifacts(manual, base=base)

    loaded = load_scheduled_run_states(base)
    assert len(loaded) == 1  # manual_runs/ never traversed
    assert loaded[0]["run_classification"]["run_origin"] == "scheduled"


def test_build_run_artifacts_captures_target_price():
    from datetime import date, datetime, time
    from zoneinfo import ZoneInfo

    from near_close_dca import (
        Action,
        ReconciledDecision,
        SourceName,
        SourceSignal,
        StrategyConfig,
        StrategyState,
        build_run_artifacts,
    )

    eastern = ZoneInfo("America/New_York")
    now = datetime.combine(date(2026, 6, 29), time(15, 45), tzinfo=eastern)

    finance = SourceSignal(
        ticker="AAPL", source=SourceName.FINANCE, action=Action.BUY, reason="buy",
        decision_price=100.0, previous_close=104.0, buy_multiplier=2.0, suggested_amount_usd=2.0,
    )
    robinhood = SourceSignal(
        ticker="AAPL", source=SourceName.ROBINHOOD, action=Action.BUY, reason="buy",
        decision_price=100.5, previous_close=104.0, buy_multiplier=2.0, suggested_amount_usd=2.0,
    )
    priced = ReconciledDecision(
        ticker="AAPL", action=Action.BUY, reason="agree", finance_signal=finance,
        robinhood_signal=robinhood, buy_multiplier=2.0, suggested_amount_usd=2.0,
    )
    # No decision price on either source -> target_price missing.
    blank_fin = SourceSignal(ticker="AMD", source=SourceName.FINANCE, action=Action.BUY, reason="buy")
    unpriced = ReconciledDecision(
        ticker="AMD", action=Action.BUY, reason="agree", finance_signal=blank_fin,
        robinhood_signal=None, buy_multiplier=1.5, suggested_amount_usd=1.5,
    )

    artifacts = build_run_artifacts(
        now, StrategyConfig(), StrategyState(), [priced, unpriced],
        {"AAPL": {"status": "submitted", "order_id": "O1"}, "AMD": {"status": "submitted", "order_id": "O2"}},
    )
    by_ticker = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert by_ticker["AAPL"]["target_price"] == 100.0  # finance preferred
    assert by_ticker["AAPL"]["target_price_missing"] is False
    assert by_ticker["AAPL"]["planned_amount_usd"] == 2.0
    assert by_ticker["AAPL"]["order_id"] == "O1"
    assert by_ticker["AMD"]["target_price"] is None
    assert by_ticker["AMD"]["target_price_missing"] is True
    assert artifacts["budget"]["weekly_cap_usd"] == 900.0
    assert artifacts["budget"]["budget_tier"] == "normal"
    assert artifacts["budget"]["base_buy_amount_usd"] == 2.0

    from scripts.run_classification import validate_classification

    rc = artifacts["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["manual_run"] is False
    assert validate_classification(rc).ok


def test_build_run_artifacts_manual_classification():
    from datetime import date, datetime, time
    from zoneinfo import ZoneInfo

    from near_close_dca import ArtifactRunContext, StrategyConfig, StrategyState, build_run_artifacts
    from scripts.run_classification import validate_classification

    eastern = ZoneInfo("America/New_York")
    now = datetime.combine(date(2026, 6, 29), time(15, 45), tzinfo=eastern)

    artifacts = build_run_artifacts(now, StrategyConfig(), StrategyState(), [], {}, run_context=ArtifactRunContext(manual=True))
    rc = artifacts["run_classification"]
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert validate_classification(rc).ok


def test_build_run_artifacts_surfaces_top_up_manual_review():
    from datetime import date, datetime, time
    from zoneinfo import ZoneInfo

    from near_close_dca import (
        Action,
        ArtifactRunContext,
        ReconciledDecision,
        SourceName,
        SourceSignal,
        StrategyConfig,
        StrategyState,
        TopUpGuardOutcome,
        build_run_artifacts,
    )

    eastern = ZoneInfo("America/New_York")
    now = datetime.combine(date(2026, 6, 29), time(15, 45), tzinfo=eastern)

    finance = SourceSignal(
        ticker="AAPL", source=SourceName.FINANCE, action=Action.BUY, reason="buy",
        decision_price=100.0, previous_close=104.0, buy_multiplier=1.0, suggested_amount_usd=6.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL", action=Action.BUY, reason="agree", finance_signal=finance,
        robinhood_signal=None, buy_multiplier=1.0, suggested_amount_usd=6.0,
    )
    # Known prior amount but no comparable reference price -> guard buys $0 and
    # flags manual review, even though the decision itself was a plain BUY.
    guard_outcome = TopUpGuardOutcome(
        ticker="AAPL", afternoon_desired_amount=6.0, afternoon_decision_price=100.0,
        prior_same_day_amount=3.0, prior_same_day_reference_price=None,
        prior_same_day_run_id="manual_am", prior_same_day_source="manual",
        incremental_order_amount=0.0, top_up_guard_result="missing_prior_price",
        skipped=True, manual_review=True,
    )

    artifacts = build_run_artifacts(
        now, StrategyConfig(), StrategyState(), [decision],
        {"AAPL": {"status": "not_submitted", "skip_reason": "missing_prior_price"}},
        run_context=ArtifactRunContext(top_up_outcomes={"AAPL": guard_outcome}),
    )
    assert artifacts["manual_review_tickers"] == ["AAPL"]
    assert artifacts["usage_proxy"]["manual_recovery_required"] is True


def test_load_json_files_skips_blocked_stubs(tmp_path):
    real = tmp_path / "run_state.json"
    real.write_text(json.dumps(_run_state()), encoding="utf-8")
    blocked = tmp_path / "blocked_run_state.json"
    blocked.write_text(
        json.dumps({"trading_day": "2026-06-30", "status": "blocked"}), encoding="utf-8"
    )

    loaded = load_json_files([real, blocked])
    # Date-gated stubs must not count as run days in the weekly dashboard.
    assert len(loaded) == 1
    assert loaded[0]["trading_day"] == "2026-06-29"


def test_load_json_files_skips_manual_states(tmp_path):
    real = tmp_path / "run_state.json"
    real.write_text(json.dumps(_run_state()), encoding="utf-8")
    manual_state = _run_state()
    manual_state["trading_day"] = "2026-06-30"
    manual_state["run_classification"] = {"run_origin": "manual", "manual_run": True}
    manual = tmp_path / "manual_run_state.json"
    manual.write_text(json.dumps(manual_state), encoding="utf-8")

    loaded = load_json_files([real, manual])
    # The Friday dashboard is scheduled-live only: manual runs are dropped.
    assert len(loaded) == 1
    assert loaded[0]["trading_day"] == "2026-06-29"


# --- PR #22 bot-review regression coverage (batch: order-history parsing) ----
# Each test below reproduces one unresolved chatgpt-codex-connector review
# comment on PR #22 against the current fills_from_order_history_payload /
# _order_list_from_payload implementation, before any fix is applied.


def test_fills_from_order_history_payload_ignores_unfilled_order_quantity():
    # Codex review: "Do not treat order quantity as executed fills." An
    # unfilled/open/rejected order carries only the original requested
    # "quantity" with no executions/filled_qty/cumulative_quantity -- that
    # must not be reported as an executed fill (it would falsely show the
    # candidate as filled with coverage/slippage numbers).
    payload = {"orders": [
        {"ticker": "AAPL", "id": "O1", "quantity": "5", "status": "rejected"},
    ]}
    fills = fills_from_order_history_payload(payload)
    assert fills == []


def test_fills_from_order_history_payload_maps_ticker_by_order_id():
    # Codex review: "Preserve ticker context when parsing order history." The
    # live-adapter order shape (id/cumulative_quantity/average_price) often
    # carries no ticker/symbol field at all because the request itself was
    # symbol- or order-id-scoped. The caller must be able to supply an
    # order_id -> ticker map so the fill isn't dropped for lack of an inline
    # ticker field.
    payload = {"orders": [
        {"id": "O1", "cumulative_quantity": "0.02", "average_price": "101.0"},
    ]}
    fills = fills_from_order_history_payload(payload, order_id_to_ticker={"O1": "AAPL"})
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["order_id"] == "O1"


def test_fills_from_order_history_payload_handles_order_wrapped_shape():
    # Codex review on PR #50: orders_after_place.json can be an order-ID-scoped
    # Robinhood response shaped like {"data": {"order": {...}}} (the single-order
    # shape RobinhoodBroker.get_order_fill() accepts). Without unwrapping the
    # nested "order" member, this fallback would report zero fills even though
    # a persisted filled scheduled order with cumulative_quantity/average_price
    # is present.
    payload = {
        "order": {
            "id": "O1",
            "cumulative_quantity": "0.02",
            "average_price": "101.0",
            "state": "filled",
        }
    }
    fills = fills_from_order_history_payload(payload, order_id_to_ticker={"O1": "AAPL"})
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["order_id"] == "O1"
    assert abs(fills[0]["filled_qty"] - 0.02) < 1e-9


def test_fills_from_order_history_payload_handles_bare_single_order_shape():
    # Codex review on PR #50: orders_after_place.json can also be a bare single
    # Robinhood order object with no "order" wrapper key at all, e.g.
    # {"id": ..., "cumulative_quantity": ...} directly at the top level (the
    # same single-order shape RobinhoodBroker.get_order_fill() accepts when
    # data.get("id") is present). This must be detected as a one-element order
    # list rather than reporting the fill as missing.
    payload = {
        "id": "O1",
        "cumulative_quantity": "0.02",
        "average_price": "101.0",
        "state": "filled",
    }
    fills = fills_from_order_history_payload(payload, order_id_to_ticker={"O1": "AAPL"})
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["order_id"] == "O1"
    assert abs(fills[0]["filled_qty"] - 0.02) < 1e-9


def test_fills_from_order_history_payload_unwraps_nested_data_envelopes():
    # Codex review: "Unwrap normalized order-history envelopes recursively."
    # save_raw_input() normalizing a raw Robinhood envelope that is itself
    # {"data": {"orders": [...]}} produces a doubly-nested pointer:
    # {"output_path": ..., "data": {"data": {"orders": [...]}}}. The parser
    # must keep unwrapping "data" layers until it finds an orders/results list.
    orders = [{"ticker": "AAPL", "id": "O1", "filled_qty": "0.02", "filled_avg_price": "101.0"}]
    payload = {"output_path": "x", "data": {"data": {"orders": orders}}}
    fills = fills_from_order_history_payload(payload)
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"


def test_fills_from_order_history_payload_skips_fills_with_no_price():
    # Codex review: "Require fill prices before reporting order-history
    # fills." An executed quantity with no price anywhere (no executions, no
    # filled_avg_price/average_price/price) must not be reported as a priced
    # fill -- compute_slippage_rows coerces a missing price to 0.0, which
    # would show false coverage and a false -100% slippage number.
    payload = {"orders": [
        {"ticker": "AAPL", "id": "O1", "filled_qty": "0.02"},
    ]}
    fills = fills_from_order_history_payload(payload)
    assert fills == []


def test_fills_from_order_history_payload_rejects_zero_priced_executions():
    # Codex review: "Reject unpriced executions instead of zeroing them." An
    # execution with a quantity but a blank/missing price must not be coerced
    # to price=0.0 and folded into the weighted average -- that would drag an
    # otherwise-valid fill toward a false, understated price (or, if every
    # execution is unpriced, a false $0 fill showing -100% slippage) instead
    # of the fill being reported as missing/unpriced.
    payload = {"orders": [
        # Every execution lacks a price -> no resolvable price anywhere -> skipped.
        {"ticker": "AAPL", "id": "O1", "executions": [
            {"quantity": "0.02", "price": None},
        ]},
        # One priced execution and one unpriced execution -> average must come
        # from the priced execution alone (100.0), not diluted by the unpriced
        # quantity toward a lower blended price.
        {"ticker": "NVDA", "id": "O2", "executions": [
            {"quantity": "0.01", "price": "100.0"},
            {"quantity": "0.01", "price": None},
        ]},
    ]}
    fills = fills_from_order_history_payload(payload)
    by_ticker = {f["ticker"]: f for f in fills}
    assert set(by_ticker) == {"NVDA"}
    assert abs(by_ticker["NVDA"]["fill_price"] - 100.0) < 1e-9
    assert abs(by_ticker["NVDA"]["filled_qty"] - 0.01) < 1e-9


def test_fills_from_order_history_payload_rejects_non_finite_prices_and_quantities():
    # Codex review (PR #88): "Reject non-finite broker fill prices". Infinity
    # and NaN are neither None nor <= 0 (NaN compares False to every
    # comparison, Infinity is > 0), so a corrupted broker record carrying
    # either value used to slip past the existing checks and be reported as
    # a real fill -- feeding non-finite slippage/JSON into downstream
    # reports instead of being treated as unpriced and missing.
    payload = {"orders": [
        # Order-level average_price is Infinity.
        {"ticker": "AAPL", "id": "O1", "state": "filled",
         "filled_qty": "0.02", "average_price": "Infinity"},
        # Order-level average_price is NaN.
        {"ticker": "NVDA", "id": "O2", "state": "filled",
         "filled_qty": "0.01", "average_price": "NaN"},
        # Per-execution price is Infinity.
        {"ticker": "GOOG", "id": "O3", "executions": [
            {"quantity": "0.01", "price": "Infinity"},
        ]},
        # Bare-quantity fallback: quantity itself is NaN.
        {"ticker": "MSFT", "id": "O4", "state": "filled",
         "quantity": "NaN", "average_price": "100.0"},
        # A genuinely valid order must still be reported.
        {"ticker": "AMZN", "id": "O5", "state": "filled",
         "filled_qty": "0.02", "average_price": "100.0"},
    ]}
    fills = fills_from_order_history_payload(payload)
    by_ticker = {f["ticker"]: f for f in fills}
    assert set(by_ticker) == {"AMZN"}, (
        f"non-finite price/quantity orders must be rejected, got {sorted(by_ticker)}"
    )


def test_drop_excluded_fills_empty_excludes():
    fills = [
        {"ticker": "AAPL"},
        {"ticker": "NVDA"},
    ]
    # Empty frozenset passed for excluded_tickers should return original fills unaltered
    assert _drop_excluded_fills(fills, frozenset()) == fills


def test_drop_excluded_fills_by_ticker():
    fills = [
        {"ticker": "AAPL"},
        {"ticker": "NVDA"},
        {"ticker": "GOOG"},
    ]
    excluded = frozenset(["AAPL", "GOOG"])
    filtered = _drop_excluded_fills(fills, excluded)
    assert len(filtered) == 1
    assert filtered[0]["ticker"] == "NVDA"


def test_drop_excluded_fills_missing_ticker_key():
    fills = [
        {"order_id": "O1"},
        {"ticker": "NVDA"},
        {"ticker": "GOOG"},
    ]
    excluded = frozenset(["AAPL", "GOOG"])
    filtered = _drop_excluded_fills(fills, excluded)
    assert len(filtered) == 2
    assert filtered[0] == {"order_id": "O1"}
    assert filtered[1] == {"ticker": "NVDA"}


def test_drop_excluded_fills_matches_symbol_keyed_fills():
    # compute_slippage_rows/build_missing_fills resolve identity via
    # ticker-or-symbol; _drop_excluded_fills must use the same fallback so a
    # broker fill shaped with "symbol" instead of "ticker" is still excluded.
    fills = [
        {"symbol": "AAPL"},
        {"symbol": "NVDA"},
        {"ticker": "GOOG"},
    ]
    excluded = frozenset(["AAPL", "GOOG"])
    filtered = _drop_excluded_fills(fills, excluded)
    assert filtered == [{"symbol": "NVDA"}]


def test_to_float():
    """Test _to_float safely converts values to float or returns None."""
    cases = [
        (10.5, 10.5),
        ("10.5", 10.5),
        (10, 10.0),
        ("10", 10.0),
        ("-5.2", -5.2),
        (0, 0.0),
        ("0", 0.0),
        (None, None),
        ("", None),
        ("abc", None),
        ("10.5a", None),
        ([], None),
        ({}, None),
    ]
    for value, expected in cases:
        assert _to_float(value) == expected


def test_write_csv_happy_path(tmp_path):
    import csv

    from dca_reporting import _write_csv

    path = tmp_path / "test.csv"
    rows = [{"A": 1, "B": "foo"}, {"A": 2, "B": "bar"}]
    fieldnames = ["A", "B"]

    _write_csv(path, rows, fieldnames)

    assert path.exists()
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["A", "B"]
        data = list(reader)
        assert len(data) == 2
        assert data[0] == {"A": "1", "B": "foo"}
        assert data[1] == {"A": "2", "B": "bar"}


def test_write_csv_empty_rows(tmp_path):
    import csv

    from dca_reporting import _write_csv

    path = tmp_path / "test.csv"
    _write_csv(path, [{"A": 1, "B": "foo"}], ["A", "B"])
    assert path.exists()

    # Re-writing with an empty row set must still overwrite the file with a
    # header-only CSV (per generate_reports' four-file contract) rather than
    # leaving the previous run's stale row behind.
    _write_csv(path, [], ["A", "B"])
    assert path.exists()
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["A", "B"]
        assert list(reader) == []


def test_run_classification_helpers():
    from dca_reporting import _run_classification_helpers

    collect_manual_order_ids, select_scheduled_state_for_reporting = _run_classification_helpers()

    assert callable(collect_manual_order_ids)
    assert callable(select_scheduled_state_for_reporting)
    assert collect_manual_order_ids.__name__ == "collect_manual_order_ids"
    assert select_scheduled_state_for_reporting.__name__ == "select_scheduled_state_for_reporting"


def test_is_manual_run_state():
    cases = [
        ({"run_classification": {"run_origin": "manual"}}, True),
        ({"run_classification": {"manual_run": True}}, True),
        ({"run_classification": {"run_origin": "manual", "manual_run": True}}, True),
        ({"run_classification": {"run_origin": "scheduled"}}, False),
        ({"run_classification": {"manual_run": False}}, False),
        ({"run_classification": {}}, False),
        ({"run_classification": None}, False),
        ({"run_classification": "invalid_type"}, False),
        ({}, False),
        (None, False),
        ([], False),
        ("not_a_dict", False),
    ]
    for run_state, expected in cases:
        assert _is_manual_run_state(run_state) is expected


def test_load_run_state(tmp_path):
    expected_data = {"run_id": "test-run", "dry_run": True}
    state_file = tmp_path / "run_state.json"
    state_file.write_text(json.dumps(expected_data), encoding="utf-8")

    loaded_data = load_run_state(state_file)
    assert loaded_data == expected_data


def test_generate_scheduled_reports_escalates_missing_scheduled_evidence_by_default(tmp_path=None):
    import tempfile
    from pathlib import Path

    import pytest

    from dca_reporting import generate_scheduled_reports

    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-30"
    with pytest.raises(ValueError, match="no valid scheduled run_state"):
        generate_scheduled_reports(day, [], base / "reports", base=base)


def test_generate_scheduled_reports_escalates_when_foreground_primary_is_not_active(tmp_path=None):
    """A same-day foreground artifact with NO marker does not suppress the alert."""
    import tempfile
    from pathlib import Path

    import pytest

    from dca_reporting import generate_scheduled_reports

    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-30"
    unmarked = {"trading_day": day, "run_classification": {"run_origin": "manual"}}
    with pytest.raises(ValueError, match="no valid scheduled run_state"):
        generate_scheduled_reports(
            day,
            [],
            base / "reports",
            base=base,
            foreground_primary_artifacts=[unmarked],
        )


def test_generate_scheduled_reports_reports_foreground_primary_mode_instead_of_escalating(tmp_path=None):
    """Jul 30 2026 patch: a marked same-day foreground artifact is reported, not alerted."""
    import tempfile
    from pathlib import Path

    from dca_reporting import generate_scheduled_reports
    from scripts.close_window_signal import build_close_window_summary

    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-30"
    foreground = base / "foreground" / "run_state.json"
    foreground.parent.mkdir(parents=True, exist_ok=True)
    foreground.write_text(
        json.dumps(
            {
                "trading_day": day,
                "run_classification": {"run_origin": "manual_foreground_primary"},
            }
        )
    )

    summary = build_close_window_summary(
        {
            "NVDA": {
                "decision_price": 98.0,
                "prior_close": 100.0,
                "lookback_price": 99.0,
                "data_source": "finance",
            }
        },
        tiers=((0.001, 1.0), (0.02, 2.0)),
        trading_day=day,
        background_mode_active=False,
        foreground_primary_mode_active=True,
    )

    paths = generate_scheduled_reports(
        day,
        [],
        base / "reports",
        base=base,
        foreground_primary_artifacts=[foreground],
        close_window_summary=summary,
    )
    report = json.loads(Path(paths["foreground_primary_context"]).read_bytes())

    context = report["foreground_primary_context"]
    assert context["foreground_primary_mode_active"] is True
    assert context["foreground_artifact_paths"] == [str(foreground)]
    # Evidence separation survives the round trip into the artifact.
    assert context["valid_scheduled_evidence"] is False
    assert context["valid_afternoon_scheduled_evidence"] is False
    assert context["top_up_context_only"] is True

    alert = report["scheduled_evidence_alert"]
    assert alert["escalate"] is False
    assert alert["suppressed_reasons"]

    assert report["what_would_have_fired"]["analytics_only"] is True
    assert report["what_would_have_fired"]["would_have_fired_tickers"] == ["NVDA"]


def test_generate_scheduled_reports_keeps_malformed_scheduled_state_blocking_in_foreground_mode(tmp_path=None):
    """A malformed/unreadable scheduled run_state.json must keep escalating even
    with a valid same-day foreground marker present (Codex review: "Keep
    malformed scheduled artifacts blocking in foreground mode") -- unlike a
    genuinely absent run_state, which foreground primary context may explain."""
    import tempfile
    from pathlib import Path

    import pytest

    from dca_reporting import generate_scheduled_reports
    from scripts.run_classification import RUN_STATE_FILENAME, scheduled_run_dir

    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-30"

    state_path = scheduled_run_dir(day, base) / RUN_STATE_FILENAME
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not valid json", encoding="utf-8")

    foreground = {
        "trading_day": day,
        "run_classification": {"run_origin": "manual_foreground_primary"},
    }
    with pytest.raises(ValueError, match="no valid scheduled run_state"):
        generate_scheduled_reports(
            day,
            [],
            base / "reports",
            base=base,
            foreground_primary_artifacts=[foreground],
        )


def test_generate_scheduled_reports_forwards_blocking_findings_to_foreground_report(tmp_path=None):
    """blocking_findings passed into the entry point must escalate even when a
    valid foreground marker is present (Codex review: "Thread validator
    blocking findings through the entry point")."""
    import tempfile
    from pathlib import Path

    import pytest

    from dca_reporting import generate_scheduled_reports

    base = Path(tmp_path or Path(tempfile.mkdtemp())) / "root"
    day = "2026-07-30"
    foreground = {
        "trading_day": day,
        "run_classification": {"run_origin": "manual_foreground_primary"},
    }
    with pytest.raises(ValueError, match="no valid scheduled run_state"):
        generate_scheduled_reports(
            day,
            [],
            base / "reports",
            base=base,
            foreground_primary_artifacts=[foreground],
            blocking_findings=["safety_failure"],
        )


def test_generate_scheduled_reports_logs_model_usage_when_foreground_report_raises(tmp_path, monkeypatch):
    """The per-run model-usage audit entry must still be appended when
    write_foreground_primary_report itself raises (e.g. a JSON serialization
    or atomic-write failure), matching the scheduled branch's own
    try/finally (Codex review: "Log model usage when foreground report
    writing fails")."""
    import pytest

    import dca_reporting
    from dca_reporting import generate_scheduled_reports

    base = tmp_path / "root"
    day = "2026-07-30"
    foreground = {
        "trading_day": day,
        "run_classification": {"run_origin": "manual_foreground_primary"},
    }

    calls = []

    def fake_log_model_usage(**kwargs):
        calls.append(kwargs)
        return None

    def raising_write(*args, **kwargs):
        raise RuntimeError("boom: simulated atomic write failure")

    monkeypatch.setattr(dca_reporting, "_model_usage_log_helper", lambda: fake_log_model_usage)
    monkeypatch.setattr(dca_reporting, "write_foreground_primary_report", raising_write)

    with pytest.raises(RuntimeError, match="boom"):
        generate_scheduled_reports(
            day,
            [],
            base / "reports",
            base=base,
            foreground_primary_artifacts=[foreground],
            model_usage_log_root=str(base / "model_usage"),
        )

    assert len(calls) == 1
    assert calls[0]["run_id"] == f"reporting_{day}_1700ET"


def _run_all():
    import inspect
    import tempfile
    from pathlib import Path

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                # tmp_path-style tests get a throwaway directory outside pytest.
                with tempfile.TemporaryDirectory() as tmp:
                    fn(Path(tmp))
            else:
                fn()
            print(f"PASS {name}")
    print("All reporting self-tests passed.")


if __name__ == "__main__":
    _run_all()
