"""The Reports page: in-memory renders of the weekly dashboard, cost tracker, latest summary and digests."""

import io
import json
import sys
from datetime import date

import pytest
from test_dca_console import request, running_server

import dca_console
from dca_console import AuthFailureLog, ConsoleAccess, ConsoleApp, Settings
from dca_console_reader import RootSpec
from dca_console_reports import (
    average_cost,
    build_reports_snapshot,
    console_digest,
    console_digests,
    cost_tracker,
    latest_summary,
    weekly_dashboard,
)


def run_state(day, **extra):
    return {"trading_day": day, "status": "completed", "dry_run": False, "generated_at": f"{day}T15:45:00-04:00",
            "run_classification": {"stream": "afternoon", "run_origin": "scheduled", "manual_run": False},
            "daily_cap_usd": 180.0, "weekly_cap_usd": 900.0, "buy_candidates": [{"ticker": "AAPL", "amount_usd": 2.0}],
            "budget": {"base_amount_usd": 2.0, "daily_cap_usd": 180.0, "weekly_cap_usd": 900.0},
            "usage_proxy": {"tool_calls": 12, "tokens_in": 1000}, **extra}


@pytest.fixture
def roots(tmp_path):
    primary = tmp_path / "cron_tracking"
    (primary / "scheduled_runs" / "20260915_1530ET").mkdir(parents=True)
    (primary / "scheduled_runs" / "20260915_1530ET" / "run_state.json").write_text(json.dumps(run_state("2026-09-15")))
    (primary / "scheduled_runs" / "20260901_1530ET").mkdir(parents=True)
    (primary / "scheduled_runs" / "20260901_1530ET" / "run_state.json").write_text(json.dumps(run_state("2026-09-01")))
    (primary / "scheduled_runs" / "20260915_1530ET" / "latest_run_summary.json").write_text(json.dumps({
        "trading_day": "2026-09-15", "coverage": {"planned": 3, "filled": 3}, "execution_quality": {}, "budget": {},
        "manual_review_tickers": [], "account": "999000111",
    }))
    (primary / "manual_runs" / "20260916_1530ET").mkdir(parents=True)
    (primary / "manual_runs" / "20260916_1530ET" / "run_state.json").write_text(json.dumps(run_state("2026-09-16")))
    extra = tmp_path / "artifacts"
    (extra / "2026-09-14").mkdir(parents=True)
    (extra / "2026-09-14" / "run_state.json").write_text(json.dumps(run_state("2026-09-14")))
    return (RootSpec.of(primary, "primary"), RootSpec.of(extra, "secondary"))


def test_weekly_dashboard_covers_only_the_iso_week_and_scheduled_runs(roots, tmp_path):
    view = weekly_dashboard(roots, date(2026, 9, 17))
    assert view["week"] == "2026-09-14 to 2026-09-20" and view["run_count"] == 2
    assert view["markdown"].startswith("# Weekly Performance Dashboard")
    # The date-named layout of the repo-root artifacts tree counts too.
    assert view["sources"] == [{"root_label": roots[0].label, "at_risk": False, "runs": 1},
                               {"root_label": roots[1].label, "at_risk": True, "runs": 1}]
    assert str(tmp_path) not in view["markdown"]
    empty = weekly_dashboard(roots, date(2026, 8, 3))
    assert empty["markdown"] is None and empty["run_count"] == 0 and empty["warnings"]
    # A run_state the builder refuses is reported, never guessed around.
    stripped = run_state("2026-09-16")
    del stripped["budget"]
    (roots[0].path / "scheduled_runs" / "20260916_1530ET").mkdir(parents=True)
    (roots[0].path / "scheduled_runs" / "20260916_1530ET" / "run_state.json").write_text(json.dumps(stripped))
    refused = weekly_dashboard(roots, date(2026, 9, 17))
    assert refused["markdown"] is None and any("refused" in warning for warning in refused["warnings"])


def test_cost_tracker_reports_proxy_units_with_the_disclaimer(roots):
    view = cost_tracker(roots, 2026, 9)
    assert view["month"] == "2026-09" and view["run_count"] >= 2
    assert "proxy" in view["disclaimer"].lower()
    assert view["aggregate"]["month_label"] == "2026-09"
    assert isinstance(view["markdown"], str) and view["markdown"]
    assert cost_tracker((), 2026, 9)["aggregate"] is None
    assert cost_tracker(roots, 2025, 1)["run_count"] == 0


def test_latest_summary_builds_the_notion_preview_without_the_account(roots, tmp_path):
    view = latest_summary(roots)
    assert view["found"] is True and view["trading_day"] == "2026-09-15"
    assert view["relative_path"] == "scheduled_runs/20260915_1530ET/latest_run_summary.json"
    assert view["page_id"] is None and "Compact Sync" in view["markdown"]
    assert "999000111" not in json.dumps({key: value for key, value in view.items() if key != "markdown"})
    assert str(tmp_path) not in json.dumps(view)
    assert latest_summary((roots[1],))["found"] is False


def test_console_digests_list_and_read_saved_days(tmp_path):
    output = tmp_path / "output"
    folder = output / "summaries" / "local"
    folder.mkdir(parents=True)
    (folder / "2026-09-17.json").write_text("{}")
    (folder / "2026-09-17.md").write_text("# DCA daily summary — 2026-09-17\n")
    (folder / "2026-09-16.md").write_text("older")
    (folder / "notes.txt").write_text("ignored")
    listing = console_digests(output, "local")
    assert [row["day"] for row in listing["days"]] == ["2026-09-17", "2026-09-16"]
    assert listing["days"][0] == {"day": "2026-09-17", "json": True, "md": True}
    assert str(tmp_path) not in json.dumps(listing)
    assert console_digest(output, "local", date(2026, 9, 17))["markdown"].startswith("# DCA daily summary")
    assert console_digest(output, "local", date(2026, 9, 1))["found"] is False
    assert console_digest(output, "demo", date(2026, 9, 17))["found"] is False


def test_average_cost_hashes_account_identities(roots, tmp_path):
    assert average_cost(roots)["found"] is False
    (roots[0].path / "scheduled_runs" / "20260915_1530ET" / "average_cost_snapshot.json").write_text(json.dumps({
        "generated_at": "2026-09-15T17:00:00-04:00",
        "accounts": {"999000111": {"AAPL": {"average_cost_usd": 190.5}}},
        "combined": {"AAPL": {"average_cost_usd": 190.5, "total_quantity": 0.25}, "bad ticker": {}},
    }))
    view = average_cost(roots)
    assert view["found"] is True and view["account_count"] == 1
    assert view["combined"] == [{"ticker": "AAPL", "average_cost_usd": 190.5, "total_quantity": 0.25}]
    assert "999000111" not in json.dumps(view)


def test_snapshot_and_routes_are_cheap_and_closed(roots, tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    output = tmp_path / "output"
    app = ConsoleApp(Settings("local", roots[0].path, output, date(2026, 9, 17), sys.executable, sys.executable,
                              secondary_roots=(roots[1].path,)))
    try:
        snapshot = build_reports_snapshot(roots, output, "local", date(2026, 9, 17))
        assert snapshot["weekly"]["run_count"] == 2 and "markdown" not in snapshot["weekly"]
        assert snapshot["latest"]["found"] is True and snapshot["digests"]["count"] == 0
        with running_server(app, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
            for route in ("/api/reports", "/api/reports/weekly?date=2026-09-17", "/api/reports/cost?month=2026-09",
                          "/api/reports/latest", "/api/reports/summary?date=2026-09-17"):
                status, _, body = request(server, route)
                assert status == 200, route
                assert str(tmp_path) not in body.decode() and "999000111" not in body.decode()
            assert request(server, "/api/reports/cost?month=2026-13")[0] == 400
            assert request(server, "/api/reports/weekly?date=nope")[0] == 400
            assert request(server, "/api/reports/latest?x=1")[0] == 400
    finally:
        app.jobs.close()
