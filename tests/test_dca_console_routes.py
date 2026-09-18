"""The console route table: closed query sets, structured redaction, and one gate order for every route."""

import io
import json
import sys
from datetime import date

import pytest
from test_dca_console import AUTH_CREDENTIAL, auth_headers, request, running_server

import dca_console
from dca_console import ASSETS, AuthFailureLog, ConsoleAccess, ConsoleApp, Settings
from dca_console_jobs import register_secret
from dca_console_routes import (
    CREDENTIAL_FREE_ROUTES,
    DEFAULT_CSP,
    GET_ROUTES,
    SANDBOX_CSP,
    Response,
    query,
    walk_redact,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings(
        "local", tmp_path / "artifacts", tmp_path / "output", date(2026, 9, 8), sys.executable, sys.executable,
    ))
    yield application
    application.jobs.close()


def test_query_strict_rejects_unknown_and_excess_fields():
    assert query("date=2026-09-08&ticker=AAPL", ("date", "ticker"), max_fields=2) == {
        "date": "2026-09-08", "ticker": "AAPL",
    }
    with pytest.raises(ValueError, match="Unknown query field: extra"):
        query("date=2026-09-08&extra=1", ("date",), max_fields=8)
    with pytest.raises(ValueError):
        query("&".join(f"key{i}=value" for i in range(9)), ("key0",), max_fields=8)


def test_query_lenient_ignores_unknown_fields_and_drops_blank_values():
    fields = query("date=&ticker=AAPL&unknown=1", ("date", "ticker"), max_fields=8, strict=False)
    assert fields == {"ticker": "AAPL"}
    assert "unknown" not in fields


def test_walk_redact_removes_secret_values_but_keeps_ids_and_prose(monkeypatch):
    register_secret("console-private-value-xyz")
    monkeypatch.setenv("DCA_TEST_ROUTE_WEBHOOK", "hook-value-123456")
    value = {
        "id": "20260917T120000-abcdef12",
        "note": "The password must have 12 characters; the console token rotates.",
        "command": "python3 -m scripts.same_day_top_up_gate --account <acct> --trading-day <YYYY-MM-DD>",
        "leak": "see console-private-value-xyz and hook-value-123456",
        "nested": [1, None, {"account": "account number 999000111 seen"}],
        "number": 20260917,
    }
    redacted = walk_redact(value)
    assert redacted["id"] == value["id"]
    assert redacted["note"] == value["note"]
    assert redacted["command"] == value["command"]
    assert redacted["number"] == 20260917
    serialized = json.dumps(redacted)
    assert "console-private-value-xyz" not in serialized
    assert "hook-value-123456" not in serialized
    assert "999000111" not in serialized
    assert redacted["nested"][0] == 1 and redacted["nested"][1] is None


def test_response_json_defaults_to_the_strict_csp_and_redacts_unless_told_not_to():
    register_secret("route-secret-value-abc")
    response = Response.json(200, {"value": "route-secret-value-abc"})
    assert response.csp == DEFAULT_CSP
    assert b"route-secret-value-abc" not in response.body
    unredacted = Response.json(200, {"token": "route-secret-value-abc"}, redacted=False)
    assert b"route-secret-value-abc" in unredacted.body
    assert "sandbox" in SANDBOX_CSP and "frame-ancestors 'self'" in SANDBOX_CSP
    assert "frame-ancestors 'none'" in DEFAULT_CSP


@pytest.mark.parametrize("route", sorted(GET_ROUTES) + sorted(ASSETS))
def test_every_get_route_sits_behind_trust_then_basic(app, route):
    access = ConsoleAccess(username="dca", credential=AUTH_CREDENTIAL)
    with running_server(app, access, AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        status, _, body = request(server, route, headers={"Host": "untrusted.example"})
        assert status == 403, route
        assert json.loads(body)["error"]
        status, headers, body = request(server, route)
        if route in CREDENTIAL_FREE_ROUTES:
            assert status == 200, route
        else:
            assert status == 401, route
            assert headers["WWW-Authenticate"].startswith('Basic realm="DCA console"')
        status, headers, _ = request(server, route, headers=auth_headers())
        assert status in {200, 404}, route
        assert headers["Content-Security-Policy"] == DEFAULT_CSP, route
        assert headers["Cache-Control"] == "no-store"


def test_unknown_get_route_is_json_404_and_never_reaches_a_handler(app, monkeypatch):
    monkeypatch.setattr(app, "snapshot", lambda *args, **kwargs: pytest.fail("Unknown route reached the snapshot"))
    with running_server(app, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        for route in ("/api/unknown", "/api/snapshot/extra", "/api", "/pages.js/../app.js"):
            status, _, body = request(server, route)
            assert status == 404, route
            assert json.loads(body) == {"error": "Not found"}


def test_snapshot_roots_are_labels_and_secondary_roots_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    extra = tmp_path / "artifacts-copy"
    extra.mkdir()
    settings = Settings("local", tmp_path / "artifacts", tmp_path / "output", date(2026, 9, 8),
                        sys.executable, sys.executable, secondary_roots=(extra,))
    application = ConsoleApp(settings)
    try:
        with running_server(application, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
            status, _, body = request(server, "/api/snapshot")
        assert status == 200
        data = json.loads(body)
        assert [root["kind"] for root in data["roots"]] == ["primary", "secondary"]
        assert all("(" in root["label"] and "/" not in root["label"] for root in data["roots"])
        assert str(tmp_path) not in body.decode()
        assert data["paths"]["artifacts"] == data["roots"][0]["label"]
    finally:
        application.jobs.close()
