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
    build_latest_run_summary,
    build_missing_fills,
    compute_slippage_rows,
    generate_reports,
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


def test_missing_fills_reported_when_no_fill():
    run_state = _run_state()
    fills = [f for f in _fills() if f["ticker"] != "NVDA"]
    missing = build_missing_fills(run_state["buy_candidates"], fills)
    tickers = {m["ticker"] for m in missing}
    assert tickers == {"NVDA"}
    assert missing[0]["skip_reason"]


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


def test_weekly_graded_vs_fixed_delta():
    run_states = [_run_state()]
    md = build_graded_vs_fixed(run_states)
    # Graded total 6.5 vs fixed 3x base * 3 candidates = 9.0 -> delta -2.50.
    assert "-2.50" in md


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

    summary = json.loads(Path(paths["latest_run_summary"]).read_text())
    assert summary["execution_quality"]["target_price_missing"] == 1

    report = Path(paths["slippage_and_coverage_report"]).read_text()
    assert "Slippage & Coverage Report" in report
    assert "target_price_missing" in report


def test_generate_reports_excludes_manual_order_ids(tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = tmp_path or Path(tempfile.mkdtemp())
    # O2 (NVDA) is a manual-run order that leaked into same-day broker fills.
    paths = generate_reports(_run_state(), _fills(), directory, exclude_order_ids={"O2"})
    summary = json.loads(Path(paths["latest_run_summary"]).read_text())
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
    summary = json.loads(Path(paths["latest_run_summary"]).read_text())
    assert summary["coverage"]["fills"] == 1  # only AAPL; NVDA's O2 fill excluded as manual
    assert "NVDA" not in Path(paths["slippage_ranked"]).read_text()


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
    assert artifacts["budget"]["base_buy_amount_usd"] == 3.0

    from scripts.run_classification import validate_classification

    rc = artifacts["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["manual_run"] is False
    assert validate_classification(rc).ok


def test_build_run_artifacts_manual_classification():
    from datetime import date, datetime, time
    from zoneinfo import ZoneInfo

    from near_close_dca import StrategyConfig, StrategyState, build_run_artifacts
    from scripts.run_classification import validate_classification

    eastern = ZoneInfo("America/New_York")
    now = datetime.combine(date(2026, 6, 29), time(15, 45), tzinfo=eastern)

    artifacts = build_run_artifacts(now, StrategyConfig(), StrategyState(), [], {}, manual=True)
    rc = artifacts["run_classification"]
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert validate_classification(rc).ok


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
