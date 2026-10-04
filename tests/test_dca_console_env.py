"""The Environment page: names not values, labels not paths, lstat not scans."""

import io
import json
import os
import sys
from datetime import date

import pytest
from test_dca_console import request, running_server

import dca_console
from dca_console import REPO, AuthFailureLog, ConsoleAccess, ConsoleApp, Settings
from dca_console_env import ENV_VARS, access_policy, build_env_snapshot, environment_variables, roots_and_mounts


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {"numpy": True, "torch": False})
    extra = tmp_path / "artifacts"
    extra.mkdir()
    application = ConsoleApp(Settings(
        "local", tmp_path / "cron_tracking", tmp_path / "output", date(2026, 9, 8), sys.executable, sys.executable,
        secondary_roots=(extra,),
    ))
    yield application
    application.jobs.close()


def test_environment_variables_report_names_and_presence_never_values(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    monkeypatch.setenv("DCA_DRIFT_SLACK_WEBHOOK", "https://hooks.example/private-hook-value")
    monkeypatch.setenv("DCA_CONFIG_DIR", "/somewhere/config")
    monkeypatch.setenv("DCA_UNLISTED_EXTRA", "1")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    rows = {row["name"]: row for row in environment_variables()}
    assert rows["ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"] == {
        "name": "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "set": True, "sensitive": True,
        "purpose": ENV_VARS[0].purpose,
    }
    assert rows["DCA_DRIFT_SLACK_WEBHOOK"]["set"] is True and rows["DCA_DRIFT_SLACK_WEBHOOK"]["sensitive"] is True
    assert rows["DCA_CONFIG_DIR"]["set"] is True and rows["DCA_CONFIG_DIR"]["sensitive"] is False
    assert rows["NOTION_TOKEN"]["set"] is False
    assert rows["DCA_UNLISTED_EXTRA"]["purpose"].startswith("Set in this environment")
    serialized = json.dumps(rows)
    assert "999000111" not in serialized and "private-hook-value" not in serialized and "/somewhere" not in serialized


def test_roots_and_mounts_use_labels_and_lstat_only(app, tmp_path, monkeypatch):
    archive = tmp_path / "drive-archive"
    archive.mkdir()
    link = tmp_path / "ticker_data"
    if os.name == "posix":
        link.symlink_to(archive)
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(link if os.name == "posix" else archive))
    calls = []
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda path: calls.append(str(path)) or real_exists(path))
    rows = {row["name"]: row for row in roots_and_mounts(app.settings)}
    assert rows["primary artifact root"]["present"] is False
    assert rows["secondary artifact root"]["present"] is True
    assert rows["ticker archive"]["present"] is True
    if os.name == "posix":
        assert rows["ticker archive"]["symlink"] is True
    # The archive symlink is never followed by the page reader.
    assert all("ticker_data" not in call for call in calls)
    assert rows["octopus run"]["present"] is None
    serialized = json.dumps(rows)
    assert str(tmp_path) not in serialized


def test_access_policy_reports_the_banner_fields_without_the_credential():
    policy = access_policy(ConsoleAccess(username="dca", credential=b"dca:s3cret-pw-abcdefg"), 8765)
    assert policy["auth"] == "basic user=dca" and policy["bind"] == "127.0.0.1" and policy["plaintext_warning"] is False
    assert "s3cret" not in json.dumps(policy)
    assert access_policy(None, None)["auth"] == "unknown"


def test_build_env_snapshot_composes_labels_gates_and_stray_files(app, monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    app.access = ConsoleAccess()
    snapshot = build_env_snapshot(app, REPO, port=1234)
    assert snapshot["session"]["mode"] == "local" and snapshot["session"]["day"] == "2026-09-08"
    assert [root["kind"] for root in snapshot["session"]["roots"]] == ["primary", "secondary"]
    assert snapshot["interpreters"][1]["packages"] == {"numpy": True, "torch": False}
    assert snapshot["access"]["port"] == 1234
    assert any(gate["command"].startswith("ruff") for gate in snapshot["gates"])
    assert {row["name"] for row in snapshot["stray_files"]} >= {"midday_run.log", "snap.py"}
    serialized = json.dumps(snapshot)
    assert "999000111" not in serialized
    assert str(app.settings.root) not in serialized and str(REPO) not in serialized
    assert "/Users/" not in serialized and "/home/" not in serialized


def test_env_route_is_strict_and_redacted(app, monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    with running_server(app, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        status, _, body = request(server, "/api/env")
        assert status == 200
        data = json.loads(body)
        assert data["access"]["port"] == server.server_address[1]
        assert data["access"]["auth"] == "none"
        assert "999000111" not in body.decode()
        assert request(server, "/api/env?x=1")[0] == 400
