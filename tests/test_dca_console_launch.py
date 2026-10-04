"""The launcher registry: closed argv templates, server-resolved inputs, manifest-trusting artifact routes."""

import io
import json
import os
import sys
from datetime import date
from typing import Any, cast

import pytest
from test_dca_console import request, running_server

import dca_console
import dca_console_launch as launch
from dca_console import REPO, AuthFailureLog, ConsoleAccess, ConsoleApp, Settings
from dca_console_launch import (
    FORBIDDEN_ARGV_TOKENS,
    GATE5_SLOTS,
    LAUNCH_SPECS,
    artifact_bytes,
    junit_summary,
    latest_gate5_payloads,
    launch_actions,
    read_manifest,
    run_launch,
)
from dca_console_reader import RootSpec, root_label
from dca_console_routes import DEFAULT_CSP, SANDBOX_CSP


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {"numpy": True, "pandas": True, "sklearn": True, "pytest": True})
    root = tmp_path / "cron_tracking"
    root.mkdir()
    extra = tmp_path / "artifacts"
    extra.mkdir()
    application = ConsoleApp(Settings(
        "local", root, tmp_path / "output", date(2026, 9, 8), sys.executable, sys.executable,
        secondary_roots=(extra,),
    ))
    yield application
    application.jobs.close()


def seed_payloads(root, extra):
    (root / "intraday_dca" / "20260916_1330ET").mkdir(parents=True)
    (root / "intraday_dca" / "20260916_1330ET" / "market_data_2026-09-16_1330.json").write_text("{}")
    (root / "morning_dca" / "20260914_1030ET").mkdir(parents=True)
    (root / "morning_dca" / "20260914_1030ET" / "market_data_1030ET.json").write_text("{}")
    (extra / "market_data_1530_20260916.json").write_text("{}")
    (extra / "market_data_1430ET_live.json").write_text("{}")
    (extra / "market_data_2026-09-11_1230.json").write_text("{}")
    (extra / "market_data_2026-09-04_1230.json").write_text("{}")
    (extra / "not_a_payload.json").write_text("{}")
    (extra / "market_data_0935ET.json").write_text("{}")
    (extra / "market_data_1130ET.json").write_text("{}")


def test_payload_discovery_picks_the_newest_per_slot_without_paths(app, tmp_path):
    seed_payloads(app.settings.root, tmp_path / "artifacts")
    roots = (RootSpec.of(app.settings.root, "primary"), RootSpec.of(tmp_path / "artifacts", "secondary"))
    payloads = cast(dict[str, dict[str, Any]], latest_gate5_payloads(roots))
    assert set(payloads) == set(GATE5_SLOTS)
    assert payloads["1330"]["trading_day"] == "2026-09-16" and payloads["1330"]["day_source"] == "filename"
    assert payloads["1030"]["trading_day"] == "2026-09-14" and payloads["1030"]["day_source"] == "run directory"
    assert payloads["1530"]["trading_day"] == "2026-09-16" and payloads["1530"]["at_risk"] is True
    assert payloads["1430"]["day_source"] == "file modification time"
    assert payloads["1230"]["relative_path"] == "market_data_2026-09-11_1230.json"
    assert payloads["0935"]["day_source"] == "file modification time" and payloads["1130"] is not None
    serialized = json.dumps(payloads)
    assert str(tmp_path) not in serialized
    assert all("_path" not in row and "_sort" not in row for row in payloads.values() if row)
    assert payloads["1530"]["root_label"] == root_label(tmp_path / "artifacts")


def test_every_template_is_closed_and_forbidden_tokens_never_appear(app, tmp_path, monkeypatch):
    seed_payloads(app.settings.root, tmp_path / "artifacts")
    monkeypatch.setattr(launch, "_deployed_figure", lambda application, day, log: 42.5)
    (app.settings.root / "x" / "20260908_1530ET").mkdir(parents=True)
    (app.settings.root / "x" / "20260908_1530ET" / "run_state.json").write_text(json.dumps({"trading_day": "2026-09-08"}))
    app.settings = app.settings.__class__(**{**app.settings.__dict__, "octopus_run": tmp_path / "octopus"})
    templates = launch.argv_templates(app, REPO)
    assert set(templates) == set(LAUNCH_SPECS)
    launch_root = str(app.settings.output / "launches")
    for action, argv in templates.items():
        assert argv, action
        joined = " ".join(argv)
        for token in FORBIDDEN_ARGV_TOKENS:
            assert token not in argv, f"{action} carries {token}"
        for argument in argv:
            if argument.startswith(str(app.settings.output)):
                assert argument.startswith(launch_root), f"{action} writes outside its job directory: {argument}"
        assert "config/" not in joined or action == "config-invariants"
    assert templates["ml-train-dry-run"][:2] == ["ml-forecaster/train.py", "--dry-run"]
    assert "--output" in templates["ml-train-dry-run"]
    assert templates["gate5-replay-1530"][:4] == ["-m", "scripts.generate_buy_candidates", "--slot", "1530ET"]
    assert "--deployed-last-7d-usd" in templates["gate5-replay-1530"] and "42.50" in templates["gate5-replay-1530"]
    assert "--deployed-last-7d-usd" not in templates["gate5-replay-1330"]
    assert templates["gate5-replay-1130"][:4] == ["-m", "scripts.generate_intraday_candidates", "--slot", "1130"]
    assert "--start" in templates["market-graph-archive"] and "--start" not in templates["market-graph-csv"]
    assert templates["drift-check"][0] == "scripts/drift_detection.py" and "--slack" not in templates["drift-check"]


def test_gate5_replay_refuses_without_usable_risk_evidence(app, tmp_path):
    seed_payloads(app.settings.root, tmp_path / "artifacts")
    calls = []

    def fake_command(argv, log, *, research=False, timeout=600, capture_to=None, demo=False):
        calls.append(argv)
        return 0

    lines = []
    code = run_launch(app, "gate5-replay-1230", "job-a", lines.append, repo=REPO, run_command=fake_command, today=date(2026, 9, 8))
    assert code == 1 and calls == []
    assert any("Refusing the replay" in line for line in lines)
    manifest = read_manifest(app, "job-a")
    assert manifest is not None
    assert manifest["action"] == "gate5-replay-1230" and manifest["artifacts"] == []
    assert "payload" in manifest["inputs"] and str(tmp_path) not in json.dumps(manifest)


def test_run_launch_captures_documents_and_lists_them_in_the_manifest(app, tmp_path):
    seed_payloads(app.settings.root, tmp_path / "artifacts")

    def fake_command(argv, log, *, research=False, timeout=600, capture_to=None, demo=False):
        if capture_to is not None:
            capture_to.parent.mkdir(parents=True, exist_ok=True)
            capture_to.write_text(json.dumps({"candidates": [{"ticker": "AAPL"}], "account": "999000111"}))
        return 0

    lines = []
    code = run_launch(app, "gate5-replay-1330", "job-b", lines.append, repo=REPO, run_command=fake_command, today=date(2026, 9, 8))
    assert code == 0
    manifest = read_manifest(app, "job-b")
    assert manifest is not None
    assert [item["name"] for item in manifest["artifacts"]] == ["document.json"]
    assert manifest["inputs"]["payload"]["trading_day"] == "2026-09-16"
    assert any("Replay of saved inputs" in line for line in lines)
    found = artifact_bytes(app, "job-b", "document.json")
    assert found is not None and found[1] == "json"
    assert artifact_bytes(app, "job-b", "other.json") is None
    assert artifact_bytes(app, "job-b", "../manifest.json") is None
    assert artifact_bytes(app, "missing-job", "document.json") is None
    assert read_manifest(app, "bad id") is None


def test_launch_actions_report_readiness_reasons(app, tmp_path, monkeypatch):
    rows = {row["id"]: row for row in launch_actions(app, REPO)}
    assert rows["config-invariants"]["available"] is True
    assert rows["gate5-replay-0935"]["available"] is False and "No saved market_data payload" in rows["gate5-replay-0935"]["reason"]
    assert rows["gate5-replay-0935"]["payload"] is None
    assert rows["gate5-replay-1230"]["needs_deployed_figure"] is True
    assert rows["octopus-inspect-html"]["available"] is False and "--octopus-run" in rows["octopus-inspect-html"]["reason"]
    assert rows["market-graph-csv"]["available"] is (REPO / "historical_stock_data.csv").is_file()
    monkeypatch.setattr(app, "dev_packages", {})
    assert {row["id"]: row for row in launch_actions(app, REPO)}["config-invariants"]["available"] is False
    ids = {row["id"] for row in app.actions()}
    assert set(LAUNCH_SPECS) <= ids and "demo-dca" in ids


def test_junit_summary_counts_skips_as_not_validated(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite><testcase name="a"/><testcase name="b"><skipped message="jsonschema missing"/></testcase>'
        '<testcase name="c"><failure message="boom"/></testcase></testsuite></testsuites>'
    )
    summary = junit_summary(report)
    assert summary["parsed"] is True and (summary["passed"], summary["failed"], summary["skipped"]) == (1, 1, 1)
    assert summary["cases"][1] == {"name": "b", "outcome": "skipped", "message": "jsonschema missing"}
    assert junit_summary(tmp_path / "missing.xml")["parsed"] is False


def test_artifact_route_serves_manifest_listed_files_with_the_right_policy(app, tmp_path):
    directory = app.settings.output / "launches" / "job-c"
    directory.mkdir(parents=True)
    (directory / "ocean.html").write_text("<html><script>1</script></html>")
    (directory / "risk.json").write_text(json.dumps({"note": "account 999000111", "n": 1}))
    (directory / "secret.txt").write_text("not listed")
    (directory / "manifest.json").write_text(json.dumps({
        "action": "risk-report", "job": "job-c", "kind": "report", "label": "x", "inputs": {},
        "artifacts": [{"name": "ocean.html", "kind": "html", "bytes": 1}, {"name": "risk.json", "kind": "json", "bytes": 1}],
    }))
    with running_server(app, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        status, headers, body = request(server, "/api/artifact?job=job-c&name=ocean.html")
        assert status == 200 and headers["Content-Security-Policy"] == SANDBOX_CSP and b"<script>" in body
        status, headers, body = request(server, "/api/artifact?job=job-c&name=risk.json")
        assert status == 200 and headers["Content-Security-Policy"] == DEFAULT_CSP
        assert "999000111" not in body.decode() and json.loads(body)["n"] == 1
        for bad in ("/api/artifact?job=job-c&name=secret.txt", "/api/artifact?job=other&name=risk.json"):
            assert request(server, bad)[0] == 404, bad
        # A name with a path separator never reaches the manifest lookup.
        assert request(server, "/api/artifact?job=job-c&name=../manifest.json")[0] == 400
        assert request(server, "/api/artifact?job=job-c")[0] == 400
        assert request(server, "/api/artifact?job=job-c&name=risk.json&x=1")[0] == 400
        status, headers, _ = request(server, "/api/snapshot")
        assert status == 200 and headers["Content-Security-Policy"] == DEFAULT_CSP
        status, _, body = request(server, "/api/launch")
        assert status == 200
        bench = json.loads(body)
        assert set(bench["payloads"]) == set(GATE5_SLOTS) and bench["launches"][0]["job"] == "job-c"
        assert str(tmp_path) not in body.decode()


@pytest.mark.skipif(os.name != "posix", reason="the drift launcher resolves a run directory with POSIX paths")
def test_drift_check_targets_the_newest_run_directory(app):
    for name, stamp in (("20260907_1530ET", "2026-09-07T15:45:00-04:00"), ("20260908_1530ET", "2026-09-08T15:45:00-04:00")):
        directory = app.settings.root / "scheduled_runs" / name
        directory.mkdir(parents=True)
        (directory / "run_state.json").write_text(json.dumps({"trading_day": stamp[:10], "run_started_at_et": stamp}))
    seen = []

    def fake_command(argv, log, *, research=False, timeout=600, capture_to=None, demo=False):
        seen.append(argv)
        return 0

    assert run_launch(app, "drift-check", "job-d", lambda _: None, repo=REPO, run_command=fake_command, today=date(2026, 9, 8)) == 0
    argv = seen[0]
    assert argv[argv.index("--run-dir") + 1].endswith("20260908_1530ET")
    manifest = read_manifest(app, "job-d")
    assert manifest is not None
    assert manifest["inputs"]["run_dir"] == "scheduled_runs/20260908_1530ET"


def test_payload_discovery_is_cached_briefly_per_root_set(app, tmp_path, monkeypatch):
    launch._PAYLOAD_CACHE.clear()
    seed_payloads(app.settings.root, tmp_path / "artifacts")
    roots = (RootSpec.of(app.settings.root, "primary"), RootSpec.of(tmp_path / "artifacts", "secondary"))
    first = latest_gate5_payloads(roots, now=100.0)
    import os
    from datetime import datetime, timezone

    # Touch a new payload with a specific timestamp to ensure the test does not depend on host machine time
    new_file = tmp_path / "artifacts" / "market_data_2026-09-20_1130.json"
    new_file.write_text("{}")

    # The existing files from `seed_payloads` might be evaluated as "newer" depending on the host time
    # and fallback logic because they don't have explicit timestamps or specific names matching the regex exactly to the day.
    # We utime everything in artifacts back into the past so this test doesn't randomly fail across days.
    old_ts = datetime(2020, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    for child in (tmp_path / "artifacts").iterdir():
        os.utime(child, (old_ts, old_ts))

    target_ts = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(new_file, (target_ts, target_ts))

    # Within the window the cached listing is returned unchanged...
    assert latest_gate5_payloads(roots, now=110.0)["1130"] == first["1130"]
    # ...and a fresh scan follows once it expires.
    refreshed = latest_gate5_payloads(roots, now=100.0 + launch.PAYLOAD_CACHE_SECONDS + 1)
    assert refreshed["1130"] is not None
    assert refreshed["1130"]["trading_day"] == "2026-09-20"
    # A launch never trusts the cache: it re-discovers the newest payload itself.
    resolved_payload, _ = launch._payload_path(roots, "1130")
    assert resolved_payload is not None
    assert resolved_payload["trading_day"] == "2026-09-20"
    launch._PAYLOAD_CACHE.clear()

def test_junit_summary_rejects_xxe_and_doctype_entities(tmp_path):
    from dca_console_launch import junit_summary
    report = tmp_path / "xxe.xml"
    report.write_text('<?xml version="1.0"?><!DOCTYPE root [<!ENTITY foo "bar">]><root>&foo;</root>')
    summary = junit_summary(report)
    assert summary["parsed"] is False
    assert summary["passed"] == 0

    report2 = tmp_path / "xxe2.xml"
    report2.write_text('<?xml version="1.0"?><!DOCTYPE root SYSTEM "http://example.com/evil.dtd"><root></root>')
    summary2 = junit_summary(report2)
    assert summary2["parsed"] is False
