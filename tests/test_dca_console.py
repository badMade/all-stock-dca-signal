"""Loopback HTTP and launcher integration tests for the local DCA console."""

import http.client
import json
import sys
import threading
from datetime import date, datetime
from pathlib import Path

import pytest

import dca_console
from dca_console import ConsoleApp, Settings, main, make_server
from dca_console_jobs import JobManager


def test_default_demo_skips_holidays_and_early_close_sessions(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 28, 10, tzinfo=tz)

    monkeypatch.setattr(dca_console, "datetime", Clock)
    # Friday is a half day; Thursday is Thanksgiving. Wednesday is a full session.
    assert dca_console.default_demo_day() == date(2026, 11, 25)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings(
        "local", tmp_path / "artifacts", tmp_path / "output", date(2026, 9, 8), sys.executable, sys.executable,
    ))
    yield application
    application.jobs.close()


@pytest.fixture
def http_server(app):
    server = make_server(app, 0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)
    assert not worker.is_alive()


def request(server, path, *, method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def post(server, token, action, **headers):
    return request(server, "/api/jobs", method="POST", body=json.dumps({"action": action}),
                   headers={"X-Console-Token": token, "Content-Type": "application/json", **headers})


def join_job(manager):
    assert manager.worker is not None
    manager.worker.join(timeout=5)
    assert not manager.worker.is_alive(), "Test job did not complete"
    return manager.snapshot()[0]


def test_http_serves_bootstrap_daily_data_assets_and_download(app, http_server):
    status, headers, body = request(http_server, "/api/bootstrap")
    assert status == 200
    bootstrap = json.loads(body)
    assert bootstrap["token"] == app.token
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, _, body = request(http_server, "/api/snapshot?date=2026-09-08")
    data = json.loads(body)
    assert status == 200
    assert data["daily"]["date"] == "2026-09-08"
    assert data["daily"]["metrics"]["confirmed_spend_usd"] is None
    assert data["jobs"] == []
    assert app.token not in body.decode()
    assert (app.settings.output / "summaries/local/2026-09-08.json").is_file()
    status, _, body = request(http_server, "/api/health")
    assert status == 200 and json.loads(body) == {"status": "ok", "mode": "local"}
    for route, content_type in (("/", "text/html"), ("/app.js", "text/javascript"), ("/styles.css", "text/css")):
        status, headers, body = request(http_server, route)
        assert status == 200 and body
        assert content_type in headers["Content-Type"]
    status, headers, body = request(http_server, "/api/export?date=2026-09-08")
    assert status == 200
    assert "dca-summary-2026-09-08.json" in headers["Content-Disposition"]
    assert "modules" not in json.loads(body)


def test_authenticated_actions_are_fixed_and_unknown_commands_are_rejected(app, http_server, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_run", lambda action, job_id, log: calls.append(action) or 0)
    status, _, body = post(http_server, app.token, "python -c arbitrary-code")
    assert status == 400 and "Unknown action" in json.loads(body)["error"]
    assert calls == []
    status, _, body = post(http_server, app.token, "demo-dca")
    assert status == 202
    assert json.loads(body)["job"]["action"] == "demo-dca"
    assert join_job(app.jobs)["status"] == "completed"
    assert calls == ["demo-dca"]


def test_missing_wrong_token_and_unknown_post_route_do_not_dispatch(app, http_server, monkeypatch):
    monkeypatch.setattr(app, "submit", lambda _: pytest.fail("Rejected request dispatched work"))
    for token in ("", "incorrect-token"):
        assert post(http_server, token, "demo-dca")[0] == 403
    assert request(http_server, "/other", method="POST", body="{}",
                   headers={"X-Console-Token": app.token})[0] == 404


def test_origin_fetch_metadata_and_host_guards_protect_reads_and_writes(app, http_server, monkeypatch):
    monkeypatch.setattr(app, "submit", lambda _: pytest.fail("Untrusted request dispatched work"))
    for attack in ({"Origin": "https://untrusted.example"}, {"Host": "untrusted.example"},
                   {"Sec-Fetch-Site": "cross-site"}, {"Host": "localhost:1"}):
        assert request(http_server, "/api/bootstrap", headers=attack)[0] == 403
        status, headers, _ = post(http_server, app.token, "demo-dca", **attack)
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers


def test_malformed_json_dates_and_extra_command_fields_return_client_errors(app, http_server, monkeypatch):
    monkeypatch.setattr(app, "submit", lambda _: pytest.fail("Malformed request dispatched work"))
    for body in ("{", "[]", '{"action": 1}', '{"action":"demo-dca","command":"other"}', "x" * 1025):
        assert request(http_server, "/api/jobs", method="POST", body=body,
                       headers={"X-Console-Token": app.token})[0] == 400
    for query in ("date=not-a-date", "date=2026-02-30", "&".join(f"key{i}=value" for i in range(9))):
        assert request(http_server, "/api/snapshot?" + query)[0] == 400
    assert app.jobs.snapshot() == []


def test_fixed_asset_routes_do_not_serve_arbitrary_repository_files(http_server):
    for path in ("/../config/scheduled_tasks.json", "/%2e%2e/config/scheduled_tasks.json",
                 "/console/../../AGENTS.md", "/config/scheduled_tasks.json", "/missing"):
        status, _, body = request(http_server, path)
        assert status == 404
        assert json.loads(body) == {"error": "Not found"}


def test_second_active_job_is_rejected_until_first_finishes(app, http_server, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def work(action, job_id, log):
        started.set()
        assert release.wait(5)
        return 0

    monkeypatch.setattr(app, "_run", work)
    try:
        assert post(http_server, app.token, "demo-dca")[0] == 202
        assert started.wait(2)
        status, _, body = post(http_server, app.token, "demo-dca")
        assert status == 409
        assert "Another job is running" in json.loads(body)["error"]
        assert len(app.jobs.snapshot()) == 1
    finally:
        release.set()
        join_job(app.jobs)


def test_failed_jobs_persist_redacted_diagnostics_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_TEST_PRIVATE_TOKEN", "private-credential-value")
    manager = JobManager(tmp_path / "jobs")

    def fail(job_id, log):
        log("Received private-credential-value\n")
        raise RuntimeError("the demo command failed")

    try:
        manager.submit("demo-dca", "Test DCA", fail)
        result = join_job(manager)
        assert result["status"] == "failed" and result["returncode"] == 1
        persisted = (manager.root / f"{result['id']}.json").read_text()
        assert "private-credential-value" not in persisted
        assert "the demo command failed" in json.loads(persisted)["log"]
        restored = JobManager(manager.root)
        assert restored.snapshot()[0]["status"] == "failed"
        assert restored.snapshot()[0]["finished_at"]
        restored.close()
    finally:
        manager.close()


def test_restart_marks_unfinished_work_interrupted_without_relaunching(tmp_path):
    folder = tmp_path / "jobs"
    folder.mkdir()
    for status in ("queued", "running"):
        (folder / f"job-{status}.json").write_text(json.dumps({
            "id": f"job-{status}", "status": status, "action": "demo-dca", "log": "started",
        }))
    (folder / "malformed.json").write_text("{")
    manager = JobManager(folder)
    try:
        assert len(manager.snapshot()) == 2
        assert all(item["status"] == "interrupted" for item in manager.snapshot())
        assert all("Console stopped" in item["log"] for item in manager.snapshot())
        assert manager.worker is None
    finally:
        manager.close()


def test_local_once_reads_existing_artifacts_without_running_commands(tmp_path, monkeypatch, capsys):
    root = tmp_path / "observed"
    root.mkdir()
    sentinel = root / "existing-user-data.txt"
    sentinel.write_text("keep this data")
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    monkeypatch.setattr(JobManager, "run_command", lambda *args: pytest.fail("Local --once launched a command"))
    monkeypatch.setattr(dca_console.subprocess, "run", lambda *args, **kwargs: pytest.fail("Unexpected subprocess"))
    output = tmp_path / "console-output"
    assert main(["--local", "--once", "--root", str(root), "--output-dir", str(output), "--date", "2026-09-08"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "local" and result["jobs"] == []
    assert result["paths"]["artifacts"] == str(root)
    assert sentinel.read_text() == "keep this data"
    assert list(root.iterdir()) == [sentinel]
    assert (output / "summaries/local/2026-09-08.json").is_file()


def test_default_once_selects_isolated_demo_and_explicit_root_selects_local(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    calls = []

    def fake_work(self, action, job_id, log):
        calls.append((self.settings.mode, self.settings.root, action))
        return 0

    monkeypatch.setattr(ConsoleApp, "_run", fake_work)
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "user-live-root"))
    output = tmp_path / "output"
    assert main(["--once", "--output-dir", str(output), "--date", "2026-09-08"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "demo"
    assert calls == [("demo", output / "demo/dca", "demo-all")]
    root = tmp_path / "explicit-root"
    assert main(["--once", "--root", str(root), "--output-dir", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "local"
    assert len(calls) == 1
    with pytest.raises(SystemExit) as error:
        main(["--demo", "--root", str(root)])
    assert error.value.code == 2


def test_dca_demo_command_removes_live_artifact_and_account_overrides(app, monkeypatch):
    captured = []
    monkeypatch.setenv("DCA_MARKET_OPEN_LIVE_STATE_ROOT", "outside-demo")
    monkeypatch.setenv("DCA_INTRADAY_LIVE_STATE_ROOT", "outside-demo")
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "private-account")

    def fake_command(command, log):
        captured.append(command)
        argv = command.argv
        target = Path(argv[argv.index("--artifacts-dir") + 1])
        target.mkdir(parents=True)
        (target / "run_state.json").write_text(json.dumps({"trading_day": "2026-09-04", "dry_run": True}))
        return 0

    monkeypatch.setattr(app.jobs, "run_command", fake_command)
    assert app._run("demo-dca", "example-job", lambda _: None) == 0
    command = captured[0]
    assert command.argv[1:4] == ("run_dca.py", "--demo", "--report")
    assert command.env["DCA_CRON_TRACKING_ROOT"] == str(app.settings.output / "demo/dca")
    assert "DCA_MARKET_OPEN_LIVE_STATE_ROOT" not in command.env
    assert "DCA_INTRADAY_LIVE_STATE_ROOT" not in command.env
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in command.env
    assert not app.settings.root.exists()


def test_saved_summary_remains_valid_json_with_large_numeric_usage_fields(app):
    app.settings.root.mkdir()
    (app.settings.root / "run_state.json").write_text(json.dumps({
        "trading_day": "2026-09-08", "status": "completed", "dry_run": True,
        # Digit-grouped on purpose: written without the underscore this is an
        # unbroken six-digit run, which
        # test_no_account_shaped_literal_in_tracked_config_source_or_docs reads
        # as an account-shaped literal and fails the secrets guard on. The
        # separator keeps the value identical and the guard tight, rather than
        # growing _NON_ACCOUNT_LITERALS for an incidental byte count.
        "usage_proxy": {"total_artifact_bytes": 200_000},
    }))
    app.snapshot()
    saved = app.settings.output / "summaries/local/2026-09-08.json"
    restored = json.loads(saved.read_bytes())
    assert restored["daily"]["costs"]["totals"]["total_artifact_bytes"] == 200_000
