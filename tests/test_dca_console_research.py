"""The Research page catalog: run directories and the analysis archive, listed without research imports."""

import io
import json
import sys
from datetime import date

import pytest
from test_dca_console import request, running_server

import dca_console
from dca_console import REPO, AuthFailureLog, ConsoleAccess, ConsoleApp, Settings
from dca_console_reader import RootSpec
from dca_console_research import analysis_archive, build_research_snapshot, octopus_runs


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings("local", tmp_path / "cron_tracking", tmp_path / "output", date(2026, 9, 8),
                                      sys.executable, sys.executable))
    yield application
    application.jobs.close()


def test_octopus_runs_list_metadata_without_reading_models(tmp_path):
    root = tmp_path / "cron_tracking"
    run = root / "wondrous_octopus" / "runs" / "quick-2026-09-01"
    (run / "checkpoint").mkdir(parents=True)
    (run / "run_meta.json").write_text(json.dumps({"model_selection": "octopus", "universe": {"tickers": ["AAPL", "MSFT"]},
                                                   "archive": str(tmp_path / "drive")}))
    (run / "checkpoint" / "harness_state.json").write_text(json.dumps({"last_processed_date": "2026-09-05", "outstanding": {}}))
    (run / "checkpoint" / "model.pt").write_bytes(b"\x00" * 10)
    bare = root / "wondrous_octopus" / "runs" / "bare"
    bare.mkdir()
    demo = tmp_path / "output" / "demo" / "octopus" / "job-1"
    demo.mkdir(parents=True)
    (demo / "run_meta.json").write_text("{}")
    view = octopus_runs((RootSpec.of(root, "primary"),), tmp_path / "output")
    rows = {row["name"]: row for row in view["runs"]}
    assert rows["quick-2026-09-01"]["updatable"] is True and rows["quick-2026-09-01"]["ticker_count"] == 2
    assert rows["quick-2026-09-01"]["last_processed_date"] == "2026-09-05" and rows["quick-2026-09-01"]["report_present"] is False
    assert rows["bare"]["updatable"] is False and rows["job-1"]["demo"] is True
    assert str(tmp_path) not in json.dumps(view)


def test_analysis_archive_lists_entries_and_advisory_outputs(tmp_path):
    (tmp_path / "analysis" / "2026-09-05-ticker-advisory" / "20260905_101010").mkdir(parents=True)
    (tmp_path / "analysis" / "2026-09-05-ticker-advisory" / "proposed_additions.json").write_text("{}")
    (tmp_path / "analysis" / "2026-06-29-revised-ladder").mkdir()
    (tmp_path / "analysis" / "2026-06-29-revised-ladder" / "README.md").write_text("# ladder")
    (tmp_path / "analysis" / "loose.md").write_text("not a directory")
    view = analysis_archive(tmp_path)
    rows = {row["name"]: row for row in view["entries"]}
    assert set(rows) == {"2026-09-05-ticker-advisory", "2026-06-29-revised-ladder"}
    assert rows["2026-09-05-ticker-advisory"]["ticker_advisory"] is True
    assert rows["2026-09-05-ticker-advisory"]["outputs"] == ["proposed_additions.json"]
    assert rows["2026-06-29-revised-ladder"]["readme"] is True
    assert analysis_archive(tmp_path / "missing")["count"] == 0


def test_snapshot_and_route_are_closed(app, tmp_path):
    snapshot = build_research_snapshot(app, REPO)
    assert set(snapshot) == {"octopus", "analysis", "launches", "boundary"}
    assert snapshot["analysis"]["count"] > 0
    with running_server(app, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        status, _, body = request(server, "/api/research")
        assert status == 200 and str(tmp_path) not in body.decode() and str(REPO) not in body.decode()
        assert request(server, "/api/research?x=1")[0] == 400
