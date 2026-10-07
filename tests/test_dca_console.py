"""Loopback HTTP and launcher integration tests for the local DCA console."""

import base64
import builtins
import http.client
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

import dca_console
from dca_console import AuthFailureLog, ConsoleAccess, ConsoleApp, Settings, load_auth_file, main, make_server
from dca_console_jobs import JobManager, redact
from dca_console_reader import root_label

AUTH_CREDENTIAL = b"dca:s3cret-pw-abcdefg"
AUTH_PASSWORD = AUTH_CREDENTIAL.partition(b":")[2].decode()


def auth_headers(credential: bytes = AUTH_CREDENTIAL, *, scheme: str = "Basic") -> dict[str, str]:
    return {"Authorization": f"{scheme} {base64.b64encode(credential).decode()}"}


def secure_auth_file(path: Path, content: bytes = AUTH_CREDENTIAL + b"\n") -> Path:
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def auth_file_error(path: Path) -> str:
    with pytest.raises(ValueError) as error:
        load_auth_file(path)
    return str(error.value)


@contextmanager
def running_server(app, access, failure_log):
    server = make_server(app, 0, access=access, failure_log=failure_log)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


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


@pytest.fixture
def auth_server(app):
    stream = io.StringIO()
    access = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL)
    failure_log = AuthFailureLog(interval=0, stream=stream)
    with running_server(app, access, failure_log) as server:
        yield server, stream, access


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
    health = json.loads(body)
    assert status == 200 and health["status"] == "ok" and health["mode"] == "local"
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
    # Roots are labelled, never echoed: the snapshot is a browser response and
    # a saved summary (GUI expansion Phase 0 leak fix).
    assert result["paths"]["artifacts"] == root_label(root)
    assert result["roots"] == [{"label": root_label(root), "kind": "primary"}]
    assert str(root) not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert sentinel.read_text() == "keep this data"
    assert list(root.iterdir()) == [sentinel]
    assert (output / "summaries/local/2026-09-08.json").is_file()


def test_default_once_selects_local_and_explicit_demo_selects_isolated_demo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    calls = []

    def fake_work(self, action, job_id, log):
        calls.append((self.settings.mode, self.settings.root, action))
        return 0

    monkeypatch.setattr(ConsoleApp, "_run", fake_work)
    live_root = tmp_path / "user-live-root"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(live_root))
    output = tmp_path / "output"
    # No mode flag: the console must default to observing real local artifacts,
    # never a synthetic demo, and must not auto-submit any job on its own.
    assert main(["--once", "--output-dir", str(output), "--date", "2026-09-08"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "local"
    assert calls == []
    # --demo remains an explicit, isolated opt-in sandbox.
    assert main(["--demo", "--once", "--output-dir", str(output), "--date", "2026-09-08"]) == 0
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
    assert command.env["NO_COLOR"] == "1" and command.env["PY_COLORS"] == "0"
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


def test_auth_file_rules_fail_closed(tmp_path, monkeypatch):
    good = secure_auth_file(tmp_path / "good-auth")
    messages = []

    missing_message = auth_file_error(tmp_path / "missing-auth")
    messages.append(missing_message)
    assert "open" in missing_message.lower() or "exist" in missing_message.lower()

    permissive = secure_auth_file(tmp_path / "permissive-auth")
    permissive.chmod(0o644)
    mode_message = auth_file_error(permissive)
    messages.append(mode_message)
    assert "mode" in mode_message.lower() or "permission" in mode_message.lower()

    symlink = tmp_path / "linked-auth"
    symlink.symlink_to(good)
    symlink_message = auth_file_error(symlink)
    messages.append(symlink_message)
    assert "symlink" in symlink_message.lower() or "no-follow" in symlink_message.lower()

    fifo = tmp_path / "fifo-auth"
    os.mkfifo(fifo, mode=0o600)
    fifo_result = []

    def read_fifo():
        try:
            fifo_result.append(load_auth_file(fifo))
        except Exception as error:  # noqa: BLE001 - the assertion below diagnoses any wrong exception type
            fifo_result.append(error)

    fifo_worker = threading.Thread(target=read_fifo, daemon=True)
    fifo_worker.start()
    fifo_worker.join(timeout=5)
    assert not fifo_worker.is_alive(), "A writerless auth FIFO blocked instead of failing closed"
    assert len(fifo_result) == 1 and isinstance(fifo_result[0], ValueError)
    fifo_message = str(fifo_result[0])
    messages.append(fifo_message)
    assert "regular" in fifo_message.lower()

    directory = tmp_path / "auth-directory"
    directory.mkdir()
    directory_message = auth_file_error(directory)
    messages.append(directory_message)
    assert "regular" in directory_message.lower()

    oversized_marker = b"oversized-private-content"
    oversized = secure_auth_file(tmp_path / "oversized-auth", oversized_marker + b"x" * (4096 + 1))
    size_message = auth_file_error(oversized)
    messages.append(size_message)
    assert "size" in size_message.lower() or "4096" in size_message
    assert oversized_marker.decode() not in size_message

    actual_uid = good.stat().st_uid
    with monkeypatch.context() as patch:
        patch.setattr(dca_console.os, "getuid", lambda: actual_uid + 1)
        owner_message = auth_file_error(good)
    messages.append(owner_message)
    assert "own" in owner_message.lower()

    with monkeypatch.context() as patch:
        patch.setattr(dca_console.os, "name", "not-posix")
        platform_message = auth_file_error(good)
    messages.append(platform_message)
    assert "posix" in platform_message.lower()

    no_colon_content = b"no-colon-private-value"
    no_colon = secure_auth_file(tmp_path / "no-colon-auth", no_colon_content)
    colon_message = auth_file_error(no_colon)
    messages.append(colon_message)
    assert "colon" in colon_message.lower() or "user:password" in colon_message.lower()
    assert no_colon_content.decode() not in colon_message

    empty_message = auth_file_error(secure_auth_file(tmp_path / "empty-auth", b""))
    messages.append(empty_message)
    assert "line" in empty_message.lower() or "empty" in empty_message.lower()

    two_line_content = AUTH_CREDENTIAL + b"\nsecond-private-line\n"
    two_line_message = auth_file_error(secure_auth_file(tmp_path / "two-line-auth", two_line_content))
    messages.append(two_line_message)
    assert "line" in two_line_message.lower()
    assert two_line_content.decode() not in two_line_message

    short_content = b"dca:short-pw-xx\n"
    short_message = auth_file_error(secure_auth_file(tmp_path / "short-auth", short_content))
    messages.append(short_message)
    assert "12" in short_message or "password" in short_message.lower()
    assert short_content.decode() not in short_message

    with pytest.raises(SystemExit) as cli_error:
        main(["--check", "--auth-file", str(no_colon)])
    assert cli_error.value.code == 2

    assert all(AUTH_PASSWORD not in message for message in messages)
    assert load_auth_file(good) == ("dca", AUTH_CREDENTIAL)


def test_auth_file_is_read_from_the_checked_descriptor(tmp_path, monkeypatch):
    path = secure_auth_file(tmp_path / "descriptor-auth")
    real_open = os.open
    real_fstat = os.fstat
    real_read = os.read
    opened = []
    checked = []
    read_from = []

    def tracked_open(opened_path, flags):
        descriptor = real_open(opened_path, flags)
        opened.append((opened_path, flags, descriptor))
        return descriptor

    def tracked_fstat(descriptor):
        checked.append(descriptor)
        return real_fstat(descriptor)

    def tracked_read(descriptor, size):
        read_from.append((descriptor, size))
        return real_read(descriptor, size)

    def reject_path_open(*args, **kwargs):
        pytest.fail("Auth content was reopened by path after descriptor validation")

    with monkeypatch.context() as patch:
        patch.setattr(dca_console.os, "open", tracked_open)
        patch.setattr(dca_console.os, "fstat", tracked_fstat)
        patch.setattr(dca_console.os, "read", tracked_read)
        patch.setattr(builtins, "open", reject_path_open)
        patch.setattr(Path, "open", reject_path_open)
        assert load_auth_file(path) == ("dca", AUTH_CREDENTIAL)

    assert len(opened) == 1
    _, flags, descriptor = opened[0]
    assert flags & os.O_NOFOLLOW
    assert flags & os.O_NONBLOCK
    assert flags & os.O_CLOEXEC
    assert checked and set(checked) == {descriptor}
    assert read_from and {item[0] for item in read_from} == {descriptor}


def test_basic_auth_required_on_every_route_except_health(app, auth_server, monkeypatch):
    server, _, _ = auth_server
    protected_routes = (
        "/",
        "/app.js",
        "/styles.css",
        "/api/bootstrap",
        "/api/snapshot",
        "/api/export",
        "/api/docs?path=docs/live_wiring.md",
    )
    for route in protected_routes:
        status, headers, body = request(server, route)
        assert status == 401, route
        assert headers["WWW-Authenticate"].startswith('Basic realm="DCA console"')
        assert json.loads(body)["error"]

    status, _, body = request(server, "/api/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"

    monkeypatch.setattr(app, "submit", lambda _: pytest.fail("A request without Basic auth dispatched work"))
    status, headers, _ = post(server, app.token, "demo-dca")
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic realm=")


def test_basic_auth_wrong_credentials_are_rejected(auth_server):
    server, _, _ = auth_server
    rejected_headers = (
        auth_headers(b"dca:wrong-private-password"),
        auth_headers(b"other-user:s3cret-pw-abcdefg"),
        {"Authorization": "Basic not-valid-base64!"},
        auth_headers(scheme="Bearer"),
    )
    for headers in rejected_headers:
        status, response_headers, body = request(server, "/api/bootstrap", headers=headers)
        assert status == 401
        assert response_headers["WWW-Authenticate"].startswith("Basic realm=")
        assert json.loads(body)["error"]


def test_basic_auth_correct_credentials_pass_and_token_still_required(app, auth_server, monkeypatch):
    server, _, _ = auth_server
    headers = auth_headers(scheme="bAsIc")
    status, _, body = request(server, "/api/bootstrap", headers=headers)
    assert status == 200
    assert json.loads(body)["token"] == app.token

    submitted = []
    monkeypatch.setattr(app, "submit", lambda action: submitted.append(action) or {"id": "example-job"})
    assert post(server, "", "demo-dca", **headers)[0] == 403
    assert submitted == []
    status, _, body = post(server, app.token, "demo-dca", **headers)
    assert status == 202
    assert json.loads(body)["job"]["id"] == "example-job"
    assert submitted == ["demo-dca"]


def test_gate_order_is_trusted_then_basic_then_token(app, auth_server):
    server, stream, _ = auth_server
    credentials = auth_headers()

    status, headers, _ = request(
        server, "/api/bootstrap", headers={"Host": "untrusted.example", **credentials},
    )
    assert status == 403
    assert "WWW-Authenticate" not in headers

    status, headers, _ = request(server, "/api/bootstrap")
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic realm=")

    status, headers, _ = post(server, "", "demo-dca", **credentials)
    assert status == 403
    assert "WWW-Authenticate" not in headers

    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert "method=GET" in lines[0] and "route=/api/bootstrap" in lines[0]
    assert "method=GET" in lines[1] and "route=/api/bootstrap" in lines[1]
    assert "method=POST" in lines[2] and "route=/api/jobs" in lines[2]


def test_password_never_appears_in_responses_or_redaction(auth_server):
    server, _, access = auth_server
    responses = [
        request(server, f"/private/{AUTH_PASSWORD}"),
        request(server, "/api/bootstrap", headers=auth_headers()),
        request(server, "/api/bootstrap", headers=auth_headers(b"dca:wrong-private-password")),
    ]
    for _, headers, body in responses:
        rendered_response = json.dumps(headers) + body.decode(errors="replace")
        assert AUTH_PASSWORD not in rendered_response
        assert AUTH_CREDENTIAL.decode() not in rendered_response

    rendered_log = redact(f"password fragments: {AUTH_PASSWORD} and {AUTH_CREDENTIAL.decode()}")
    assert AUTH_PASSWORD not in rendered_log
    assert AUTH_CREDENTIAL.decode() not in rendered_log
    assert "[redacted]" in rendered_log.lower()
    assert AUTH_PASSWORD not in repr(access)
    assert AUTH_CREDENTIAL.decode() not in repr(access)


def test_auth_failure_is_logged_without_credentials(auth_server):
    server, stream, _ = auth_server
    wrong_header = auth_headers(b"other-user:s3cret-pw-abcdefg")
    status, _, _ = request(server, f"/api/bootstrap?attempt={AUTH_PASSWORD}", headers=wrong_header)
    assert status == 401

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "127.0.0.1" in line
    assert "GET" in line
    assert "/api/bootstrap" in line
    assert "attempt" not in line
    assert "Authorization" not in line and "Basic" not in line
    assert AUTH_PASSWORD not in line
    assert base64.b64encode(b"other-user:s3cret-pw-abcdefg").decode() not in line


def test_auth_failure_log_line_is_bounded_and_redacted(app):
    # Constructing access registers the credential with the shared redactor. This
    # covers direct logger calls as well as the handler path that the prior test uses.
    ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL)
    client = ("127.0.0.1", 123)

    def render_direct(target):
        stream = io.StringIO()
        failure_log = AuthFailureLog(interval=0, stream=stream)
        failure_log.record(client, "GET", target)
        lines = stream.getvalue().splitlines()
        assert len(lines) == 1
        assert len(lines[0]) < 256
        return lines[0], stream.getvalue()

    def render_http(target):
        stream = io.StringIO()
        failure_log = AuthFailureLog(interval=0, stream=stream)
        access = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL)
        with running_server(app, access, failure_log) as server:
            assert request(server, target)[0] == 401
        lines = stream.getvalue().splitlines()
        assert len(lines) == 1
        assert len(lines[0]) < 256
        return lines[0], stream.getvalue()

    # Exercise the injected per-server object for targets an HTTP client can
    # represent; direct calls below are reserved for parser-hostile controls.
    long_line, _ = render_http("/" + "x" * (60 * 1024) + "?ignored-query=yes")
    assert "ignored-query" not in long_line

    secret_line, _ = render_http(f"/private/{AUTH_PASSWORD}?ignored={AUTH_PASSWORD}")
    assert AUTH_PASSWORD not in secret_line
    assert "ignored" not in secret_line
    assert "[redacted]" in secret_line.lower()

    # Same Page round 2 requires redaction before the route is truncated. Put
    # half the password on each side of that boundary so either leaked fragment
    # catches a truncate-first regression even though the complete value is gone.
    split = len(AUTH_PASSWORD) // 2
    padding = "x" * (128 - 1 - split)
    cutoff_line, _ = render_http("/" + padding + AUTH_PASSWORD)
    assert AUTH_PASSWORD[:split] not in cutoff_line
    assert AUTH_PASSWORD[split:] not in cutoff_line
    assert "[red" in cutoff_line.lower()

    # Controls split the registered value on input. They must be removed before
    # redaction, and none may manufacture a second physical log line.
    controlled_target = "/control/" + AUTH_PASSWORD[:split] + "\r\n\x00\x1b" + AUTH_PASSWORD[split:]
    controlled_line, controlled_output = render_direct(controlled_target)
    assert controlled_output.count("\n") == 1
    assert "\r" not in controlled_output and "\x00" not in controlled_output and "\x1b" not in controlled_output
    assert AUTH_PASSWORD[:split] not in controlled_line
    assert AUTH_PASSWORD[split:] not in controlled_line
    assert "non-ascii-target" in controlled_line

    # An auth-file password may itself contain characters the fixed-shape
    # logger must strip or parse as URL delimiters. The raw pre-parse redaction
    # keeps normalization from exposing the remaining characters (panel,
    # round 3).
    transformed_password = "control?private-\x00secret"
    transformed_access = ConsoleAccess("dca", f"dca:{transformed_password}".encode())
    assert transformed_access.authorized(auth_headers(f"dca:{transformed_password}".encode()))
    transformed = transformed_password.replace("\x00", "")
    transformed_line, _ = render_direct(f"/control/{transformed_password}")
    assert transformed not in transformed_line
    assert "[redacted]" in transformed_line.lower()

    # A raw socket preserves the UTF-8 request-target bytes needed to exercise
    # http.server's ISO-8859-1 decoding, which caused the reviewed printable-
    # projection leak; a normal HTTP client cannot represent that path (panel
    # finding, round 1).
    non_ascii_password = "pässwörd-abcdefg"
    non_ascii_credential = f"dca:{non_ascii_password}".encode()
    non_ascii_stream = io.StringIO()
    non_ascii_log = AuthFailureLog(interval=0, stream=non_ascii_stream)
    non_ascii_access = ConsoleAccess("dca", non_ascii_credential)
    wire_spelling = non_ascii_password.encode("utf-8").decode("iso-8859-1")
    percent_spelling = quote(non_ascii_password, safe="")
    printable_projection = "".join(
        character for character in non_ascii_password if 0x20 <= ord(character) <= 0x7e
    )
    # Mixed raw/percent encoding defeats finite spelling enumeration, so the
    # route logger must reject its residual non-ASCII structure instead
    # (Visionary review, Rock 1 fix round 2).
    mixed_path = b"/x/p%C3%A4ssw" + "örd-abcdefg".encode("utf-8")

    with running_server(app, non_ascii_access, non_ascii_log) as server:
        authority = f"127.0.0.1:{server.server_address[1]}".encode("ascii")
        password_paths = (
            b"/x/" + non_ascii_password.encode("utf-8"),
            b"/x/" + percent_spelling.encode("ascii"),
            mixed_path,
        )
        for password_path in password_paths:
            payload = b"GET " + password_path + b" HTTP/1.1\r\nHost: " + authority + b"\r\nConnection: close\r\n\r\n"
            with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as connection:
                connection.sendall(payload)
                response = b""
                while chunk := connection.recv(4096):
                    response += chunk
            assert b" 401 " in response.partition(b"\r\n")[0]

    non_ascii_output = non_ascii_stream.getvalue()
    for exposed in (non_ascii_password, wire_spelling, printable_projection, percent_spelling):
        assert exposed not in non_ascii_output
    assert non_ascii_output.count("[redacted]") == 2
    assert non_ascii_output.count("non-ascii-target") == 1

    # A mostly non-ASCII password has a tiny printable projection. Registering
    # that projection globally would destroy unrelated job diagnostics.
    sparse_password = "☃" * 11 + "a"
    ConsoleAccess("dca", f"dca:{sparse_password}".encode())
    ordinary_diagnostic = "available actions remain readable"
    assert redact(ordinary_diagnostic) == ordinary_diagnostic

    malformed_line, malformed_output = render_direct("http://[")
    assert malformed_output.count("\n") == 1
    assert "malformed-target" in malformed_line
    assert "traceback" not in malformed_line.lower()

    # The injected clock exposes the suppression total without sleeping. One
    # initial line and one post-interval line are the only allowed writes.
    clock = FakeClock()
    burst_stream = io.StringIO()
    burst_log = AuthFailureLog(interval=5.0, clock=clock, stream=burst_stream)
    for _ in range(50):
        burst_log.record(client, "GET", "/api/bootstrap")
    assert len(burst_stream.getvalue().splitlines()) == 1
    clock.advance(6)
    burst_log.record(client, "GET", "/api/bootstrap")
    burst_lines = burst_stream.getvalue().splitlines()
    assert len(burst_lines) <= 2
    assert re.search(r"(?:suppressed\D*49|49\D*suppressed)", burst_lines[-1], flags=re.IGNORECASE)


def test_auth_failure_limiter_is_thread_safe():
    ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL)
    clock = FakeClock()
    stream = io.StringIO()
    failure_log = AuthFailureLog(interval=5.0, clock=clock, stream=stream)
    worker_count = 20
    barrier = threading.Barrier(worker_count + 1)
    errors = []
    errors_lock = threading.Lock()

    def fail_together(worker):
        try:
            barrier.wait(timeout=5)
            failure_log.record(("127.0.0.1", worker), "GET", f"/failure-{worker}")
        except Exception as error:  # noqa: BLE001 - every worker failure must reach the main assertion
            with errors_lock:
                errors.append(error)

    workers = [threading.Thread(target=fail_together, args=(worker,)) for worker in range(worker_count)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(stream.getvalue().splitlines()) == 1

    clock.advance(6)
    failure_log.record(("127.0.0.1", 321), "GET", "/after-burst")
    lines = stream.getvalue().splitlines()
    assert len(lines) <= 2
    assert re.search(r"(?:suppressed\D*19|19\D*suppressed)", lines[-1], flags=re.IGNORECASE)


def test_console_access_construction_fails_closed():
    authenticated = {"username": "dca", "credential": AUTH_CREDENTIAL}
    invalid_policies = (
        {"bind": "192.0.2.10"},
        {"trusted_hosts": ("mac.tailnet.ts.net",)},
        {**authenticated, "behind_proxy": True},
        {
            **authenticated,
            "bind": "192.0.2.10",
            "trusted_hosts": ("mac.tailnet.ts.net",),
            "behind_proxy": True,
        },
        {**authenticated, "bind": "0.0.0.0"},
        {"bind": "not-an-ip"},
        {"bind": "::1"},
    )
    for policy in invalid_policies:
        with pytest.raises(ValueError):
            ConsoleAccess(**policy)

    for trusted_host in ("http://x", "x/y", "x:99999", "x y", "*.ts.net"):
        with pytest.raises(ValueError):
            ConsoleAccess(**authenticated, trusted_hosts=(trusted_host,))


def test_invalid_access_never_constructs_a_server(app, monkeypatch):
    monkeypatch.setattr(
        dca_console,
        "ThreadingHTTPServer",
        lambda *args, **kwargs: pytest.fail("An invalid access policy reached server construction"),
    )
    with pytest.raises(ValueError):
        make_server(app, 0, ConsoleAccess(bind="192.0.2.10"))


def test_exposure_flags_fail_closed_from_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    auth_file = secure_auth_file(tmp_path / "console-auth")
    invalid_argv = (
        ["--check", "--bind", "192.0.2.10"],
        ["--check", "--trusted-host", "x"],
        ["--check", "--behind-proxy", "--auth-file", str(auth_file)],
        [
            "--check",
            "--behind-proxy",
            "--bind",
            "192.0.2.10",
            "--trusted-host",
            "x",
            "--auth-file",
            str(auth_file),
        ],
        ["--check", "--bind", "0.0.0.0", "--auth-file", str(auth_file)],
    )
    for argv in invalid_argv:
        with pytest.raises(SystemExit) as error:
            main(argv)
        assert error.value.code == 2


def test_trusted_hosts_accept_configured_names_and_https_origin(app):
    access = ConsoleAccess(
        username="dca",
        credential=AUTH_CREDENTIAL,
        trusted_hosts=("mac.tailnet.ts.net",),
    )
    failure_log = AuthFailureLog(interval=0, stream=io.StringIO())
    credentials = auth_headers()
    with running_server(app, access, failure_log) as server:
        trusted = {"Host": "mac.tailnet.ts.net", "Origin": "https://mac.tailnet.ts.net", **credentials}
        assert request(server, "/api/bootstrap", headers=trusted)[0] == 200
        uppercase = {"Host": "MAC.TAILNET.TS.NET", "Origin": "HTTPS://MAC.TAILNET.TS.NET", **credentials}
        assert request(server, "/api/bootstrap", headers=uppercase)[0] == 200

        status, _, body = request(
            server,
            "/api/bootstrap",
            headers={"Host": "other.ts.net", **credentials},
        )
        assert status == 403
        assert json.loads(body)["received_host"] == "other.ts.net"
        assert request(server, "/api/bootstrap", headers=credentials)[0] == 200


def test_origin_must_match_effective_authority(app):
    trusted_host = "mac.tailnet.ts.net"
    credentials = auth_headers()
    access = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL, trusted_hosts=(trusted_host,))
    with running_server(app, access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        loopback = f"127.0.0.1:{server.server_address[1]}"
        assert request(
            server,
            "/api/bootstrap",
            headers={"Host": loopback, "Origin": f"https://{trusted_host}", **credentials},
        )[0] == 403
        assert request(
            server,
            "/api/bootstrap",
            headers={"Host": trusted_host, "Origin": f"http://{loopback}", **credentials},
        )[0] == 403

    proxy_access = ConsoleAccess(
        username="dca",
        credential=AUTH_CREDENTIAL,
        trusted_hosts=(trusted_host,),
        behind_proxy=True,
    )
    with running_server(app, proxy_access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        headers = {
            "X-Forwarded-Host": trusted_host,
            "Origin": f"https://{trusted_host}",
            **credentials,
        }
        assert request(server, "/api/bootstrap", headers=headers)[0] == 200


def test_forwarded_host_is_ignored_unless_behind_proxy(app):
    trusted_host = "mac.tailnet.ts.net"
    credentials = auth_headers()
    direct_access = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL, trusted_hosts=(trusted_host,))
    proxy_access = ConsoleAccess(
        username="dca",
        credential=AUTH_CREDENTIAL,
        trusted_hosts=(trusted_host,),
        behind_proxy=True,
    )
    untrusted_base = {"Host": "untrusted.example", "X-Forwarded-Host": trusted_host, **credentials}
    with running_server(app, direct_access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        assert request(server, "/api/bootstrap", headers=untrusted_base)[0] == 403

    with running_server(app, proxy_access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        assert request(server, "/api/bootstrap", headers=untrusted_base)[0] == 403
        loopback = f"127.0.0.1:{server.server_address[1]}"
        assert request(
            server,
            "/api/bootstrap",
            headers={"X-Forwarded-Host": loopback, **credentials},
        )[0] == 403
        status, _, body = request(
            server,
            "/api/bootstrap",
            headers={"X-Forwarded-Host": "other.ts.net", **credentials},
        )
        assert status == 403
        diagnostic = json.loads(body)
        assert diagnostic["received_host"] == loopback
        assert diagnostic["forwarded_host"] == "other.ts.net"


def test_forwarded_host_takes_precedence_as_effective_authority(app):
    trusted_host = "mac.tailnet.ts.net"
    headers = {
        "X-Forwarded-Host": trusted_host,
        "Origin": f"https://{trusted_host}",
        **auth_headers(),
    }
    proxy_access = ConsoleAccess(
        username="dca",
        credential=AUTH_CREDENTIAL,
        trusted_hosts=(trusted_host,),
        behind_proxy=True,
    )
    with running_server(app, proxy_access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        assert request(server, "/api/bootstrap", headers=headers)[0] == 200

    direct_access = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL, trusted_hosts=(trusted_host,))
    with running_server(app, direct_access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        assert request(server, "/api/bootstrap", headers=headers)[0] == 403


def test_health_reports_received_and_forwarded_host(http_server):
    port = http_server.server_address[1]
    authority = f"127.0.0.1:{port}"
    status, _, body = request(http_server, "/api/health")
    assert status == 200
    assert json.loads(body) == {
        "status": "ok",
        "mode": "local",
        "received_host": authority,
        "forwarded_host": None,
    }

    status, _, body = request(
        http_server,
        "/api/health",
        headers={"X-Forwarded-Host": "mac.tailnet.ts.net"},
    )
    assert status == 200
    assert json.loads(body)["forwarded_host"] == "mac.tailnet.ts.net"


def test_hosts_for_bind_addresses():
    port = 8765
    loopback_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    assert ConsoleAccess().hosts(port) == loopback_hosts

    exposed = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL, bind="192.0.2.10")
    assert exposed.hosts(port) == {*loopback_hosts, f"192.0.2.10:{port}"}

    wildcard = ConsoleAccess(
        username="dca",
        credential=AUTH_CREDENTIAL,
        bind="0.0.0.0",
        trusted_hosts=("MAC.TAILNET.TS.NET",),
    )
    assert wildcard.trusted_hosts == ("mac.tailnet.ts.net",)
    assert wildcard.hosts(port) == {*loopback_hosts, "mac.tailnet.ts.net"}
    assert f"0.0.0.0:{port}" not in wildcard.hosts(port)


def test_non_loopback_bind_banner_warns_about_plaintext():
    exposed = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL, bind="192.0.2.10")
    exposed_banner = dca_console.startup_banner(exposed, 8765)
    loopback_banner = dca_console.startup_banner(
        ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL),
        8765,
    )

    warning = "plain HTTP: the Basic credential and console token are visible to anyone on this network"
    assert warning in exposed_banner
    assert warning not in loopback_banner
    assert "bind: 192.0.2.10" in exposed_banner
    assert "port: 8765" in exposed_banner
    assert "trusted hosts: (none)" in exposed_banner
    assert "auth: basic user=dca" in exposed_banner
    for banner in (exposed_banner, loopback_banner):
        assert AUTH_PASSWORD not in banner
        assert AUTH_CREDENTIAL.decode() not in banner

    echoed_password = "banner-secret.example"
    collision = ConsoleAccess(
        username="dca",
        credential=f"dca:{echoed_password}".encode(),
        trusted_hosts=(echoed_password,),
    )
    assert echoed_password not in dca_console.startup_banner(collision, 8765)


def test_cross_site_navigation_allowed_only_for_document_route(app, monkeypatch):
    document_headers = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    failure_log = AuthFailureLog(interval=0, stream=io.StringIO())
    with running_server(app, ConsoleAccess(), failure_log) as server:
        assert request(server, "/", headers=document_headers)[0] == 200
        monkeypatch.setattr(app, "snapshot", lambda *args, **kwargs: pytest.fail("Cross-site read reached snapshot"))
        monkeypatch.setattr(app, "submit", lambda *args, **kwargs: pytest.fail("Cross-site POST dispatched work"))
        for route in ("/api/bootstrap", "/api/snapshot", "/api/export"):
            assert request(server, route, headers=document_headers)[0] == 403
        assert request(
            server,
            "/api/bootstrap",
            headers={**document_headers, "Sec-Fetch-Site": "cross-site "},
        )[0] == 403
        assert request(
            server,
            "/",
            headers={**document_headers, "Sec-Fetch-Mode": "cors"},
        )[0] == 403
        assert post(server, app.token, "demo-dca", **document_headers)[0] == 403

    assert not (app.settings.output / "summaries").exists()


def test_idle_connection_is_closed_by_handler_timeout(app, monkeypatch):
    original_server = dca_console.ThreadingHTTPServer
    def mock_server(addr, handler_cls):
        handler_cls.timeout = 0.2
        return original_server(addr, handler_cls)
    monkeypatch.setattr(dca_console, "ThreadingHTTPServer", mock_server)

    with running_server(
        app,
        ConsoleAccess(),
        AuthFailureLog(interval=0, stream=io.StringIO()),
    ) as server:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
            connection.settimeout(2)
            assert connection.recv(1) == b""


def test_server_header_does_not_leak_python_version(auth_server):
    server, _, _ = auth_server
    status, headers, _ = request(server, "/api/health")
    assert status == 200
    assert headers["Server"] == "DCA-Console"
    assert "Python" not in headers["Server"]
    assert sys.version.split()[0] not in headers["Server"]

    # BaseHTTPRequestHandler normally reflects an unsupported method in its
    # own HTML error before the console's route guards run. Exercise that
    # hardened pre-handler path with the registered credential spelling.
    authority = f"127.0.0.1:{server.server_address[1]}".encode()
    payload = AUTH_PASSWORD.encode() + b" / HTTP/1.1\r\nHost: " + authority + b"\r\nConnection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
        connection.sendall(payload)
        response = b""
        while chunk := connection.recv(4096):
            response += chunk
    assert b" 501 " in response.partition(b"\r\n")[0]
    assert AUTH_PASSWORD.encode() not in response
    assert AUTH_CREDENTIAL not in response


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM lifecycle coverage requires POSIX signals")
def test_sigterm_stops_console_cleanly(tmp_path):
    child_env = dict(os.environ)
    child_env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "from.py",
            "--local",
            "--no-browser",
            "--port",
            "0",
            "--root",
            str(tmp_path / "observed"),
            "--output-dir",
            str(tmp_path / "console-output"),
        ],
        cwd=dca_console.REPO,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    output_lines = []
    ready = threading.Event()
    output_closed = threading.Event()

    def read_output():
        # Send SIGTERM as soon as the readiness line is observable. This keeps
        # the regression on the Same Page round 2 race: the handler must be in
        # place before an operator or supervisor can react to that banner.
        try:
            for line in process.stdout:
                output_lines.append(line)
                if "DCA console (local)" in line:
                    ready.set()
        finally:
            output_closed.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        for _ in range(100):
            if ready.wait(timeout=0.1) or output_closed.is_set():
                break
        if not ready.is_set():
            pytest.fail("Console did not print its readiness banner:\n" + "".join(output_lines))
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=2)
    stdout = "".join(output_lines)
    assert "DCA console (local)" in stdout
    assert "Stopping local console." in stdout


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM handler coverage requires POSIX signals")
def test_sigterm_handler_raises_and_restores_previous_handler():
    original_handler = signal.getsignal(signal.SIGTERM)

    def previous_handler(signum, frame):
        return None

    signal.signal(signal.SIGTERM, previous_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            with dca_console._sigterm_as_keyboard_interrupt():
                installed_handler = signal.getsignal(signal.SIGTERM)
                assert installed_handler is not previous_handler
                installed_handler(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) is previous_handler

        worker_observation = []

        def enter_from_worker():
            # signal.signal() itself is illegal off the main thread. The
            # context therefore has to be a no-op there (Same Page round 3),
            # while preserving the process's already-installed handler.
            with dca_console._sigterm_as_keyboard_interrupt():
                worker_observation.append(signal.getsignal(signal.SIGTERM))

        worker = threading.Thread(target=enter_from_worker)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert worker_observation == [previous_handler]
    finally:
        signal.signal(signal.SIGTERM, original_handler)


def test_unpinned_local_default_day_rolls_over_midnight(tmp_path, monkeypatch):
    moments = [datetime(2026, 9, 10, 23, 59)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = moments[0]
            return cls(value.year, value.month, value.day, value.hour, value.minute, tzinfo=tz)

    monkeypatch.setattr(dca_console, "datetime", Clock)
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings(
        "local", tmp_path / "artifacts", tmp_path / "output", None, sys.executable, sys.executable,
    ))
    try:
        with running_server(
            application,
            ConsoleAccess(),
            AuthFailureLog(interval=0, stream=io.StringIO()),
        ) as server:
            first_status, _, first_body = request(server, "/api/bootstrap")
            first_bootstrap = json.loads(first_body)
            first_snapshot = application.snapshot()

            moments[0] = datetime(2026, 9, 11, 0, 1)
            second_status, _, second_body = request(server, "/api/bootstrap")
            second_bootstrap = json.loads(second_body)
            second_snapshot = application.snapshot()

        assert first_status == second_status == 200
        assert first_bootstrap["default_date"] == first_snapshot["daily"]["date"] == "2026-09-10"
        assert second_bootstrap["default_date"] == second_snapshot["daily"]["date"] == "2026-09-11"
        assert first_snapshot["default_date"] == first_bootstrap["default_date"]
        assert second_snapshot["default_date"] == second_bootstrap["default_date"]
    finally:
        application.jobs.close()


def test_explicit_date_stays_pinned(tmp_path, monkeypatch, capsys):
    moments = [datetime(2026, 9, 10, 23, 59)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = moments[0]
            return cls(value.year, value.month, value.day, value.hour, value.minute, tzinfo=tz)

    monkeypatch.setattr(dca_console, "datetime", Clock)
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    pinned_day = date(2026, 9, 8)

    # Existing fixtures pass a concrete day positionally. Both modes must keep
    # that meaning even though None now denotes a rolling local service.
    for mode in ("local", "demo"):
        application = ConsoleApp(Settings(
            mode,
            tmp_path / f"{mode}-artifacts",
            tmp_path / f"{mode}-output",
            pinned_day,
            sys.executable,
            sys.executable,
        ))
        try:
            with running_server(
                application,
                ConsoleAccess(),
                AuthFailureLog(interval=0, stream=io.StringIO()),
            ) as server:
                first_status, _, first_body = request(server, "/api/bootstrap")
                first_snapshot = application.snapshot()
                moments[0] = datetime(2026, 9, 11, 0, 1)
                second_status, _, second_body = request(server, "/api/bootstrap")
                second_snapshot = application.snapshot()
            assert first_status == second_status == 200
            for bootstrap in (json.loads(first_body), json.loads(second_body)):
                assert bootstrap["default_date"] == pinned_day.isoformat()
            for snapshot in (first_snapshot, second_snapshot):
                assert snapshot["default_date"] == pinned_day.isoformat()
                assert snapshot["daily"]["date"] == pinned_day.isoformat()
        finally:
            application.jobs.close()
        moments[0] = datetime(2026, 9, 10, 23, 59)

    # Exercise the CLI construction boundary too: only local mode without an
    # explicit --date is allowed to carry None into Settings (Same Page round
    # 2). --check keeps this assertion outside the serving-only signal scope.
    created_apps = []
    real_console_app = ConsoleApp

    def capture_app(settings):
        application = real_console_app(settings)
        created_apps.append(application)
        return application

    monkeypatch.setattr(dca_console, "ConsoleApp", capture_app)
    monkeypatch.setattr(dca_console, "module_catalog", lambda _: [])
    common = [
        "--local",
        "--check",
        "--root",
        str(tmp_path / "cli-artifacts"),
        "--output-dir",
        str(tmp_path / "cli-output"),
    ]
    try:
        assert main([*common, "--date", pinned_day.isoformat()]) == 0
        assert created_apps[-1].settings.day == pinned_day
        assert main(common) == 0
        assert created_apps[-1].settings.day is None
        capsys.readouterr()
    finally:
        for application in created_apps:
            application.jobs.close()


def test_demo_pipeline_day_is_not_the_calendar_day(tmp_path, monkeypatch):
    class SaturdayClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 12, 12, tzinfo=tz)

    monkeypatch.setattr(dca_console, "datetime", SaturdayClock)
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings(
        "local", tmp_path / "artifacts", tmp_path / "output", None, sys.executable, sys.executable,
    ))
    commands = []

    def capture_command(argv, log, **kwargs):
        commands.append(argv)
        return 1

    monkeypatch.setattr(application, "_command", capture_command)
    monkeypatch.setattr(application, "snapshot", lambda: {"daily": {"summary": []}})
    try:
        # A local page may default to Saturday, but its demo action still needs
        # the previous complete market session (Same Page round 3).
        expected_day = dca_console.default_demo_day()
        assert expected_day != SaturdayClock.now(dca_console.ET).date()
        assert application._run("demo-dca", "demo-job", lambda _: None) == 1
        assert len(commands) == 1
        now_index = commands[0].index("--now")
        assert commands[0][now_index + 1] == f"{expected_day}T15:45"
    finally:
        application.jobs.close()


def test_summary_signature_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings(
        "local", tmp_path / "artifacts", tmp_path / "output", date(2026, 9, 8),
        sys.executable, sys.executable,
    ))
    days = [date(2026, 1, 1) + timedelta(days=offset) for offset in range(100)]
    try:
        # Insert newest-first so this proves "most recent days" means calendar
        # recency, not merely the most recent UI insertion order.
        for day in reversed(days):
            application._save_summary(
                {"date": day.isoformat(), "summary": [], "runs": [], "warnings": []},
                {"status": "unavailable", "summary": [], "warnings": []},
            )
        # One signature per day would otherwise grow for the lifetime of a
        # launchd service. Retaining the newest window preserves write-skipping
        # without an unbounded process-local history (Rock 3 review finding).
        assert len(application.signatures) <= 32
        assert set(application.signatures) == {day.isoformat() for day in days[-32:]}
    finally:
        application.jobs.close()


@contextmanager
def probed_console(tmp_path, monkeypatch, packages, octopus_run=None):
    """Build a console whose research probe reports exactly ``packages``."""
    monkeypatch.setattr(dca_console, "python_packages", lambda _: packages)
    application = ConsoleApp(Settings(
        "local", tmp_path / "artifacts", tmp_path / "output", date(2026, 9, 8),
        sys.executable, sys.executable, octopus_run,
    ))
    try:
        yield application
    finally:
        application.jobs.close()


def action_state(application, action_id: str) -> tuple[bool, str]:
    action = next(item for item in application.actions() if item["id"] == action_id)
    return action["available"], action["reason"]


def test_research_action_reason_names_only_the_missing_packages(tmp_path, monkeypatch):
    """One shared "install the requirements" line could not say which package was
    missing, so an operator with NumPy already installed had nothing to act on."""
    packages = {"numpy": True, "pandas": False, "sklearn": False, "torch": False}
    with probed_console(tmp_path, monkeypatch, packages) as application:
        for action_id in ("demo-octopus", "octopus-estimate"):
            available, reason = action_state(application, action_id)
            assert available is False
            # scikit-learn, not "sklearn": the reason has to name what pip takes.
            assert "pandas" in reason and "scikit-learn" in reason
            assert "numpy" not in reason
            assert "--research-python" in reason and "wondrous-octopus/requirements.txt" in reason


def test_neural_action_reason_names_torch_alone_when_the_rest_is_installed(tmp_path, monkeypatch):
    packages = {"numpy": True, "pandas": True, "sklearn": True, "torch": False}
    with probed_console(tmp_path, monkeypatch, packages) as application:
        assert action_state(application, "demo-octopus") == (True, "")
        available, reason = action_state(application, "demo-neural")
        assert available is False
        assert "torch" in reason
        assert "pandas" not in reason and "scikit-learn" not in reason


def test_failed_research_probe_is_reported_instead_of_a_pip_instruction(tmp_path, monkeypatch):
    """A probe that never answered is not evidence of a missing package; sending
    the operator to pip against that interpreter is the wrong next step."""
    with probed_console(tmp_path, monkeypatch, {"error": "Python runtime is unavailable"}) as application:
        available, reason = action_state(application, "demo-octopus")
        assert available is False
        assert "Python runtime is unavailable" in reason
        assert "--research-python" in reason
        assert "Install" not in reason and "requirements.txt" not in reason


def test_octopus_update_reason_separates_the_run_directory_from_dependencies(tmp_path, monkeypatch):
    installed = {"numpy": True, "pandas": True, "sklearn": True, "torch": True}
    missing = {"numpy": True, "pandas": True, "sklearn": False, "torch": False}
    run_dir = tmp_path / "octopus-run"
    run_dir.mkdir()

    with probed_console(tmp_path, monkeypatch, installed) as application:
        available, reason = action_state(application, "octopus-update")
        assert available is False
        assert "--octopus-run" in reason
        assert "Install" not in reason and "scikit-learn" not in reason

    with probed_console(tmp_path, monkeypatch, missing, octopus_run=run_dir) as application:
        available, reason = action_state(application, "octopus-update")
        assert available is False
        assert "scikit-learn" in reason
        assert "--octopus-run" not in reason

    with probed_console(tmp_path, monkeypatch, missing) as application:
        available, reason = action_state(application, "octopus-update")
        assert available is False
        assert "--octopus-run" in reason and "scikit-learn" in reason

    with probed_console(tmp_path, monkeypatch, installed, octopus_run=run_dir) as application:
        assert action_state(application, "octopus-update") == (True, "")


def test_actions_include_last_updated_metadata(tmp_path, monkeypatch):
    """Every workflow action row includes dynamic last_updated metadata for launchpad cards."""
    packages = {"numpy": True, "pandas": True, "sklearn": True, "torch": True}
    with probed_console(tmp_path, monkeypatch, packages) as application:
        actions = application.actions()
        assert len(actions) >= 9
        for item in actions:
            assert "last_updated" in item
            lu = item["last_updated"]
            assert isinstance(lu, dict)
            assert "status" in lu and "timestamp" in lu and "label" in lu



def test_snapshot_defaults_research_to_the_primary_universe_and_ticker_overrides_it(tmp_path, monkeypatch):
    """Wiring check for the research page: the default view is tickers.json's active
    list (never the whole-share BYDDY/RVII), an explicit ticker reaches any symbol in
    the run, and the filter's choices cover every ticker in the run."""
    packages = {"numpy": True, "pandas": True, "sklearn": True, "torch": True}
    run_dir = tmp_path / "octopus"
    pending = lambda ticker: {  # noqa: E731
        "ticker": ticker, "model": "octopus", "origin_date": "2026-09-04", "ref_close": 100,
        "point": {name: 0.01 for name in ("open", "high", "low", "close")},
        "quantiles": {name: {"p10": -0.02, "p50": 0.01, "p90": 0.03} for name in ("open", "high", "low", "close")},
    }
    checkpoint = run_dir / "checkpoint" / "harness_state.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({
        "last_processed_date": "2026-09-04",
        "outstanding": {"2026-09-08": [pending(t) for t in ("AAPL", "BYDDY", "ZZZZ")]},
    }), encoding="utf-8")
    with probed_console(tmp_path, monkeypatch, packages, octopus_run=run_dir) as application:
        default = application.snapshot(date(2026, 9, 8))["octopus"]
        assert {row["ticker"] for row in default["forecasts"]} == {"AAPL"}
        assert default["available_tickers"] == ["AAPL", "BYDDY", "ZZZZ"]
        override = application.snapshot(date(2026, 9, 8), ticker="ZZZZ")["octopus"]
        assert {row["ticker"] for row in override["forecasts"]} == {"ZZZZ"}
