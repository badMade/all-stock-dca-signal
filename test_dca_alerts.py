"""Tests for dca_alerts: failure alerting, heartbeats, and the guarded_run wrapper."""

import http.client
import io
import ipaddress
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dca_alerts
import dca_net_utils
from dca_alerts import GuardedRunConfig, alert_failure, guarded_run, heartbeat_url, send_heartbeat

_PUBLIC_IP = ipaddress.ip_address("93.184.216.34")


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type=None, exc_val=None, exc_tb=None) -> None:
        pass


@pytest.fixture
def captured_requests(monkeypatch):
    """Capture urllib.request.urlopen calls made by dca_alerts."""
    calls = []

    def fake_urlopen(url_or_request, timeout=None):
        calls.append(url_or_request)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture(autouse=True)
def _fake_public_dns(monkeypatch):
    """dca_alerts now validates that a URL's host resolves to a public address
    before treating it as safe to use (SSRF guard). Test hostnames like
    ``hc.example`` are a reserved TLD that never resolves in real DNS, so fake a
    public address for every hostname here; tests exercising the SSRF-blocking
    path itself override this per-test.
    """
    monkeypatch.setattr(dca_net_utils, "resolve_ips", lambda hostname: {_PUBLIC_IP})


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    """dca_alerts now special-cases destinations routed through a
    configured forward proxy (see _proxy_configured_for) so proxy-only
    deployments don't need local DNS resolution to succeed. That must not
    depend on whatever proxy happens to be configured in the environment
    the test suite itself runs in -- clear it so every test here exercises
    the direct (non-proxied) path unless it opts into proxy behavior
    explicitly.
    """
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)


def _clear_alert_env(monkeypatch):
    for name in (
        "DCA_HEARTBEAT_URL",
        "DCA_HEARTBEAT_URL_MORNING",
        "DCA_HEARTBEAT_URL_AFTERNOON",
        "DCA_ALERT_SLACK_WEBHOOK",
        "DCA_DRIFT_SLACK_WEBHOOK",
    ):
        monkeypatch.delenv(name, raising=False)


# -- heartbeats -------------------------------------------------------------------


def test_send_heartbeat_noop_without_env(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    assert send_heartbeat("morning") is False
    assert captured_requests == []


def test_send_heartbeat_prefers_stream_specific_env(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/generic")
    monkeypatch.setenv("DCA_HEARTBEAT_URL_MORNING", "https://hc.example/morning")
    assert heartbeat_url("morning") == "https://hc.example/morning"
    assert send_heartbeat("morning") is True
    assert captured_requests == ["https://hc.example/morning"]


def test_send_heartbeat_failure_ping_appends_fail(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")
    assert send_heartbeat("afternoon", ok=False) is True
    assert captured_requests == ["https://hc.example/check/fail"]


def test_send_heartbeat_rejects_non_http_scheme(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "ftp://hc.example/check")
    assert send_heartbeat("morning") is False
    assert captured_requests == []


def test_send_heartbeat_never_raises_on_network_error(monkeypatch):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")

    def boom(url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert send_heartbeat("morning") is False


def test_send_heartbeat_redacts_url_on_delivery_failure(monkeypatch, caplog):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/super-secret-token")

    def boom(url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with caplog.at_level("WARNING"):
        assert send_heartbeat("morning") is False
    assert "super-secret-token" not in caplog.text
    assert "https://hc.example" in caplog.text


@pytest.mark.parametrize("private_ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"])
def test_send_heartbeat_blocks_private_destination(monkeypatch, captured_requests, private_ip):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")
    import dca_net_utils
    monkeypatch.setattr(
        dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address(private_ip)}
    )
    assert send_heartbeat("morning") is False
    assert captured_requests == []


def test_send_heartbeat_blocks_destination_with_any_disallowed_resolved_ip(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")
    import dca_net_utils
    monkeypatch.setattr(
            dca_net_utils,
            "resolve_ips",
        lambda hostname: {_PUBLIC_IP, ipaddress.ip_address("127.0.0.1")},
    )
    assert send_heartbeat("morning") is False
    assert captured_requests == []


def test_send_heartbeat_blocks_unresolvable_host(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")

    def boom(hostname):
        raise OSError("name resolution failed")

    monkeypatch.setattr(dca_net_utils, "resolve_ips", boom)
    assert send_heartbeat("morning") is False
    assert captured_requests == []


# -- SSRF redirect guard ------------------------------------------------------------


def test_safe_redirect_handler_blocks_private_redirect_target(monkeypatch):
    import dca_net_utils
    monkeypatch.setattr(
        dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("127.0.0.1")}
    )
    handler = dca_net_utils.SafeRedirectHandler()
    req = urllib.request.Request("https://hc.example/start")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://internal.example/secret")


def test_safe_redirect_handler_allows_public_redirect_target(monkeypatch):
    handler = dca_net_utils.SafeRedirectHandler()
    req = urllib.request.Request("https://hc.example/start")
    new_req = handler.redirect_request(req, None, 302, "Found", {}, "https://hc.example/elsewhere")
    assert new_req is not None and new_req.full_url == "https://hc.example/elsewhere"


# -- disallowed-address coverage (carrier-grade NAT / deprecated site-local) -------


@pytest.mark.parametrize(
    "disallowed_ip",
    ["100.64.0.1", "100.100.100.1", "fec0::1"],
)
def test_send_heartbeat_blocks_shared_and_site_local_destination(
    monkeypatch, captured_requests, disallowed_ip
):
    """100.64.0.0/10 (carrier-grade NAT) and fec0::/10 (deprecated IPv6
    site-local) are neither private nor reserved but are not publicly
    routable either -- is_private/is_reserved alone let them through.
    """
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")
    import dca_net_utils
    monkeypatch.setattr(
        dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address(disallowed_ip)}
    )
    assert send_heartbeat("morning") is False
    assert captured_requests == []


# -- malformed port ----------------------------------------------------------------


def test_heartbeat_url_rejects_malformed_port(monkeypatch):
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example:notaport/super-secret-token")
    assert heartbeat_url("morning") is None


def test_send_heartbeat_never_raises_on_malformed_port(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example:notaport/super-secret-token")
    assert send_heartbeat("morning") is False
    assert captured_requests == []


def test_redact_url_never_raises_on_malformed_port():
    assert dca_alerts._redact_url("https://hc.example:notaport/super-secret-token") == "https://hc.example"


# -- sanitized exception logging ----------------------------------------------------


def test_send_heartbeat_does_not_log_raw_exception_text_with_token(monkeypatch, caplog):
    """http.client.InvalidURL (raised for a URL path containing a control
    character) embeds the complete offending path -- including a token --
    in its message; the failure log must not reproduce it.
    """
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://hc.example/check")

    def boom(url, timeout=None):
        import http.client

        raise http.client.InvalidURL("URL can't contain control characters. '/super-secret-token\\x00'")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with caplog.at_level("WARNING"):
        assert send_heartbeat("morning") is False
    assert "super-secret-token" not in caplog.text
    assert "InvalidURL" in caplog.text


def test_alert_failure_does_not_log_raw_exception_text_with_token(monkeypatch, caplog):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_ALERT_SLACK_WEBHOOK", "https://hooks.example/super-secret-token")

    def boom(request, timeout=None):
        import http.client

        raise http.client.InvalidURL("URL can't contain control characters. '/super-secret-token\\x00'")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with caplog.at_level("WARNING"):
        assert alert_failure("morning", RuntimeError("boom")) is False
    assert "super-secret-token" not in caplog.text
    assert "InvalidURL" in caplog.text


def test_sanitize_exception_never_stringifies_urlerror_reason():
    """URLError.reason can be a plain string OR an arbitrary exception object;
    stringifying it directly (rather than reporting only its type) could
    reintroduce a URL/token leak (Copilot review on this PR).
    """
    token_leaking_reason = "connection to https://hooks.example/super-secret-token failed"
    rendered = dca_alerts._sanitize_exception(urllib.error.URLError(token_leaking_reason))
    assert "super-secret-token" not in rendered
    assert "URLError" in rendered

    oserror_reason = OSError(111, "connection refused")
    rendered = dca_alerts._sanitize_exception(urllib.error.URLError(oserror_reason))
    assert "URLError" in rendered
    assert "111" in rendered

    # BaseException-typed reason without errno -- only the type names are safe
    # to report; the exception message itself may contain URL/token data.
    value_error_reason = ValueError("unexpected scheme https://hooks.example/secret-token")
    rendered = dca_alerts._sanitize_exception(urllib.error.URLError(value_error_reason))
    assert "secret-token" not in rendered
    assert "URLError" in rendered
    assert "ValueError" in rendered


def test_sanitize_exception_reports_http_error_status_only():
    from email.message import Message

    exc = urllib.error.HTTPError(
        "https://hooks.example/super-secret-token", 503, "blocked", Message(), None
    )
    rendered = dca_alerts._sanitize_exception(exc)
    assert "super-secret-token" not in rendered
    assert "503" in rendered


# -- proxy-only deployments ----------------------------------------------------------


def test_proxy_configured_for_true_when_scheme_proxy_set(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    assert dca_net_utils.proxy_configured_for("https://hc.example/check") is True


def test_proxy_configured_for_false_when_only_other_scheme_proxied(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
    assert dca_net_utils.proxy_configured_for("https://hc.example/check") is False


def test_proxy_configured_for_respects_no_proxy(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    monkeypatch.setenv("no_proxy", "hc.example")
    assert dca_net_utils.proxy_configured_for("https://hc.example/check") is False


def test_send_heartbeat_skips_local_resolution_behind_a_proxy(monkeypatch, captured_requests):
    """A proxy-only deployment's heartbeat host may intentionally not
    resolve on this host (the proxy resolves it); a genuine local
    resolution FAILURE for that hostname must not block the request when a
    proxy is configured (codex review discussion_r3679190507).
    """
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://internal-only.example/check")
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")

    def boom(hostname):
        raise OSError("name resolution failed")

    monkeypatch.setattr(dca_net_utils, "resolve_ips", boom)
    assert send_heartbeat("morning") is True
    assert captured_requests == ["https://internal-only.example/check"]


def test_heartbeat_url_still_blocks_literal_disallowed_ip_behind_a_proxy(monkeypatch):
    """A literal IP host (e.g. the cloud-metadata address) needs no DNS
    resolution at all, so the proxy-bypass branch above must not skip
    validating it -- an attacker-influenced URL naming
    169.254.169.254 directly must still be rejected even when a proxy is
    configured (codex review on this PR).
    """
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://169.254.169.254/latest/meta-data/")
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    assert heartbeat_url("morning") is None


def test_heartbeat_url_allows_literal_public_ip_behind_a_proxy(monkeypatch):
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://93.184.216.34/check")
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    assert heartbeat_url("morning") == "https://93.184.216.34/check"


def test_heartbeat_url_still_blocks_resolvable_disallowed_hostname_behind_a_proxy(monkeypatch):
    """A hostname (not a literal IP) that DOES resolve locally to a
    disallowed address must still be rejected even when a proxy is
    configured for the scheme -- unconditionally skipping validation
    whenever any proxy exists (regardless of whether this particular
    hostname needed it) would let an attacker-controlled hostname resolve
    via the proxy to a private/link-local address completely unchecked
    (codex review on this PR).
    """
    monkeypatch.setenv("DCA_HEARTBEAT_URL", "https://attacker.example/check")
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    import dca_net_utils
    monkeypatch.setattr(
        dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("169.254.169.254")}
    )
    assert heartbeat_url("morning") is None


# -- pinned connection (DNS-rebinding guard) -----------------------------------------


def test_connect_pinned_connects_to_the_validated_address(monkeypatch):
    """The address _connect_pinned validates must be the exact address it
    connects to -- not a hostname handed to a second, independent lookup
    that could answer differently (DNS rebinding).
    """
    validated_ip = ipaddress.ip_address("93.184.216.34")
    monkeypatch.setattr(dca_net_utils, "resolve_ips", lambda hostname: {validated_ip})
    calls = []

    def fake_create_connection(address, timeout, source_address):
        calls.append(address)
        return object()

    import socket
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    class _FakeConn(http.client.HTTPConnection):
        host = str(validated_ip)
        port = 443
        timeout = 10.0
        source_address = None
        sock = None

    conn = _FakeConn("hc.example", port=443)
    dca_net_utils.connect_pinned(conn)
    assert calls == [(str(validated_ip), 443)]
    assert conn.sock is not None


def test_connect_pinned_rejects_disallowed_resolved_address(monkeypatch):
    import dca_net_utils
    monkeypatch.setattr(
        dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("127.0.0.1")}
    )

    class _FakeConn(http.client.HTTPConnection):
        host = "hc.example"
        port = 443
        timeout = 10.0
        source_address = None

    with pytest.raises(dca_net_utils.UnsafeDestinationError):
        dca_net_utils.connect_pinned(_FakeConn("hc.example"))


def test_request_is_proxied_detects_tunnel_and_plain_proxy_forms():
    tunneled = urllib.request.Request("https://hc.example/check")
    tunneled._tunnel_host = "hc.example"  # type: ignore[attr-defined]
    assert dca_net_utils.request_is_proxied(tunneled) is True

    plain_proxied = urllib.request.Request("http://hc.example/check")
    plain_proxied.selector = "http://hc.example/check"
    assert dca_net_utils.request_is_proxied(plain_proxied) is True

    direct = urllib.request.Request("https://hc.example/check")
    direct.selector = "/check"
    assert dca_net_utils.request_is_proxied(direct) is False


# -- handler construction (not exercised by tests that mock urlopen directly) -----


def test_pinned_https_handler_constructs_connection_without_crashing(monkeypatch):
    """https_open() must be able to construct its connection object without
    raising. Every other test in this module mocks urllib.request.urlopen
    directly, which bypasses this handler entirely and let a Python-version-
    specific bug ship undetected: on 3.12+, HTTPSHandler no longer sets
    self._check_hostname at all, and http.client.HTTPSConnection.__init__ no
    longer accepts a check_hostname argument either, so forwarding it here
    raised AttributeError/TypeError on every real HTTPS request (Codex
    review on this PR).
    """

    class _ReachedConnection(Exception):
        pass

    def fake_request(self, *a, **kw):
        raise _ReachedConnection("construction succeeded")

    monkeypatch.setattr(dca_net_utils.PinnedHTTPSConnection, "request", fake_request)
    handler = dca_net_utils.PinnedHTTPSHandler()
    req = urllib.request.Request("https://hc.example/check")
    req.timeout = 10.0  # normally set by OpenerDirector.open() before dispatch
    with pytest.raises(_ReachedConnection):
        handler.https_open(req)


def test_pinned_https_handler_proxied_branch_constructs_connection_without_crashing(monkeypatch):
    class _ReachedConnection(Exception):
        pass

    def fake_request(self, *a, **kw):
        raise _ReachedConnection("construction succeeded")

    monkeypatch.setattr(http.client.HTTPSConnection, "request", fake_request)
    handler = dca_net_utils.PinnedHTTPSHandler()
    req = urllib.request.Request("https://hc.example/check")
    req.timeout = 10.0  # type: ignore[attr-defined]  # normally set by OpenerDirector.open() before dispatch
    req._tunnel_host = "hc.example"  # type: ignore[attr-defined]
    with pytest.raises(_ReachedConnection):
        handler.https_open(req)


def test_pinned_http_handler_constructs_connection_without_crashing(monkeypatch):
    class _ReachedConnection(Exception):
        pass

    def fake_request(self, *a, **kw):
        raise _ReachedConnection("construction succeeded")

    monkeypatch.setattr(dca_net_utils.PinnedHTTPConnection, "request", fake_request)
    handler = dca_net_utils.PinnedHTTPHandler()
    req = urllib.request.Request("http://hc.example/check")
    req.timeout = 10.0  # normally set by OpenerDirector.open() before dispatch
    with pytest.raises(_ReachedConnection):
        handler.http_open(req)


# -- failure alerts ---------------------------------------------------------------


def test_alert_failure_noop_without_env(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    assert alert_failure("morning", RuntimeError("boom")) is False
    assert captured_requests == []


def test_alert_failure_posts_stream_and_exception(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_ALERT_SLACK_WEBHOOK", "https://hooks.example/alert")
    assert alert_failure("afternoon", RuntimeError("boom"), context="ctx-info") is True
    (request,) = captured_requests
    assert request.full_url == "https://hooks.example/alert"
    text = json.loads(request.data.decode("utf-8"))["text"]
    assert "afternoon" in text
    assert "RuntimeError" in text
    assert "boom" in text
    assert "ctx-info" in text


def test_alert_failure_blocks_private_destination(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_ALERT_SLACK_WEBHOOK", "https://hooks.example/alert")
    import dca_net_utils
    monkeypatch.setattr(
        dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("169.254.169.254")}
    )
    assert alert_failure("morning", RuntimeError("boom")) is False
    assert captured_requests == []


def test_alert_failure_falls_back_to_drift_webhook(monkeypatch, captured_requests):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_DRIFT_SLACK_WEBHOOK", "https://hooks.example/drift")
    assert alert_failure("morning", ValueError("bad")) is True
    assert captured_requests[0].full_url == "https://hooks.example/drift"


def test_alert_failure_never_raises_on_network_error(monkeypatch):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("DCA_ALERT_SLACK_WEBHOOK", "https://hooks.example/alert")

    def boom(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert alert_failure("morning", RuntimeError("boom")) is False


# -- guarded_run ------------------------------------------------------------------


def test_guarded_run_success_pings_ok(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    assert guarded_run("morning", lambda: 42) == 42
    assert pings == [("morning", True)]
    assert alerts == []


def test_guarded_run_failure_alerts_pings_fail_and_reraises(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda stream, exc, **kw: alerts.append((stream, exc)))

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run("midday", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pings == [("midday", False)]
    assert len(alerts) == 1 and isinstance(alerts[0][1], RuntimeError)


def test_guarded_run_disabled_never_pings(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: pings.append(a))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    with pytest.raises(RuntimeError):
        guarded_run("morning", lambda: (_ for _ in ()).throw(RuntimeError("boom")), config=GuardedRunConfig(enabled=False))
    assert pings == []
    assert alerts == []


def test_guarded_run_result_ok_false_pings_fail_without_alert(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    result = guarded_run(
        "afternoon",
        lambda: {"raw_persistence_error": "disk full"},
        config=GuardedRunConfig(result_ok=lambda r: not r.get("raw_persistence_error")),
    )
    assert result == {"raw_persistence_error": "disk full"}
    assert pings == [("afternoon", False)]
    # Structured-failure paths emit their own targeted alerts; no duplicate here.
    assert alerts == []


def test_guarded_run_result_ok_predicate_error_fails_closed(monkeypatch):
    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))

    result = guarded_run(
        "morning",
        lambda: {"fine": True},
        config=GuardedRunConfig(result_ok=lambda r: r["missing-key"]),
    )
    assert result == {"fine": True}
    assert pings == [("morning", False)]


def test_guarded_run_alerting_bug_does_not_mask_failure(monkeypatch):
    def broken_alert(*a, **kw):
        raise ValueError("alert plumbing broken")

    monkeypatch.setattr(dca_alerts, "alert_failure", broken_alert)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", broken_alert)

    with pytest.raises(RuntimeError, match="original"):
        guarded_run("morning", lambda: (_ for _ in ()).throw(RuntimeError("original")))


def test_guarded_run_failure_writes_failed_start_marker(monkeypatch):
    markers = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "write_failed_start_marker", lambda stream, exc, **kw: markers.append((stream, exc)))

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run("midday", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert len(markers) == 1
    assert markers[0][0] == "midday"
    assert isinstance(markers[0][1], RuntimeError)


def test_guarded_run_marker_write_bug_does_not_mask_failure(monkeypatch):
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "write_failed_start_marker", lambda *a, **kw: (_ for _ in ()).throw(ValueError("marker plumbing broken")))

    with pytest.raises(RuntimeError, match="original"):
        guarded_run("morning", lambda: (_ for _ in ()).throw(RuntimeError("original")))


def test_guarded_run_alert_failure_raising_does_not_block_heartbeat_or_marker(monkeypatch):
    # alert_failure raising (e.g. malformed webhook URL parsing outside its
    # own try) must not prevent the heartbeat or the marker -- the marker
    # especially is the only durable local evidence this failure happened --
    # codex review discussion_r3658373974.
    pings = []
    markers = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad url")))
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    monkeypatch.setattr(dca_alerts, "write_failed_start_marker", lambda stream, exc, **kw: markers.append(stream))

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run("midday", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pings == [("midday", False)]
    assert markers == ["midday"]


def test_guarded_run_passes_attempt_log_root_to_marker(monkeypatch, tmp_path):
    roots = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda stream, exc, *, root=None, **kw: roots.append(root),
    )

    with pytest.raises(RuntimeError):
        guarded_run(
            "morning",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            config=GuardedRunConfig(attempt_log_root=str(tmp_path)),
        )
    assert roots == [str(tmp_path)]


# -- attempt_log_root_sibling_to ---------------------------------------------------


def test_attempt_log_root_sibling_to_none_when_unset(monkeypatch):
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    assert dca_alerts.attempt_log_root_sibling_to(None) is None
    assert dca_alerts.attempt_log_root_sibling_to("") is None


def test_attempt_log_root_sibling_to_derives_sibling_directory(monkeypatch):
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    result = dca_alerts.attempt_log_root_sibling_to("/custom/base/model_usage")
    assert result == "/custom/base/attempt_log"


def test_attempt_log_root_sibling_to_defers_to_explicit_env_override(monkeypatch):
    # A deployment that intentionally sets DCA_ATTEMPT_LOG_ROOT to its own
    # active artifact tree must have that win over a derived sibling path --
    # codex review discussion_r3659124650.
    monkeypatch.setenv("DCA_ATTEMPT_LOG_ROOT", "/configured/attempt_log")
    assert dca_alerts.attempt_log_root_sibling_to("/custom/base/model_usage") is None
    assert dca_alerts.attempt_log_root_sibling_to(None) is None


# -- write_failed_start_marker ------------------------------------------------------


def test_write_failed_start_marker_persists_readable_json(tmp_path):
    triggered = datetime(2026, 7, 27, 20, 30, 0, tzinfo=timezone.utc)
    path = dca_alerts.write_failed_start_marker(
        "market_open", RuntimeError("connector timed out"), triggered_at_utc=triggered, root=str(tmp_path)
    )
    assert path is not None and path == tmp_path / "20260727" / "market_open.json"
    marker = json.loads(path.read_bytes())
    assert marker["stream"] == "market_open"
    assert marker["error_class"] == "RuntimeError"
    assert marker["error_message"] == "connector timed out"
    assert marker["triggered_at_utc"] == "2026-07-27T20:30:00Z"
    # Default phase is the genuine failed start this marker was built for.
    assert marker["failure_phase"] == "before_run_artifact"


def test_write_failed_start_marker_records_the_late_failure_phase(tmp_path):
    # A run can persist a valid run_state.json and only then raise; the marker
    # must say so, or the report invents an earlier failed start plus a
    # successful retry -- codex review discussion_r3659298031.
    path = dca_alerts.write_failed_start_marker(
        "morning",
        RuntimeError("report generation failed after the run was written"),
        triggered_at_utc=datetime(2026, 7, 27, 14, 40, 0, tzinfo=timezone.utc),
        root=str(tmp_path),
        run_artifact_written=True,
    )
    assert path is not None and json.loads(path.read_bytes())["failure_phase"] == "after_run_artifact"


def test_guarded_run_probes_whether_the_run_artifact_was_written(monkeypatch):
    phases = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda stream, exc, *, root=None, run_artifact_written=False: phases.append(run_artifact_written),
    )

    # A probe reporting the artifact already exists records the late phase; a
    # probe reporting otherwise -- and no probe at all -- record a failed start.
    for probe, expected in ((lambda: True, True), (lambda: False, False), (None, False)):
        phases.clear()
        with pytest.raises(RuntimeError, match="boom"):
            guarded_run(
                "morning",
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                config=GuardedRunConfig(run_artifact_written=probe),
            )
        assert phases == [expected], probe


def test_guarded_run_artifact_probe_failure_falls_back_to_failed_start(monkeypatch):
    # A broken probe must never lose the failure record; it fails closed to a
    # plain failed start, the pre-existing behavior.
    phases = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: None)
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda stream, exc, *, root=None, run_artifact_written=False: phases.append(run_artifact_written),
    )

    def _broken_probe():
        raise OSError("cannot stat the run directory")

    with pytest.raises(RuntimeError, match="boom"):
        guarded_run(
            "morning",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            config=GuardedRunConfig(run_artifact_written=_broken_probe),
        )
    assert phases == [False]


def test_artifact_freshness_probe_true_for_a_fresh_write(tmp_path):
    # No file exists at snapshot time; one appears afterward -- that must be
    # THIS invocation's own write.
    path = tmp_path / "run_state.json"
    probe = dca_alerts.artifact_freshness_probe(path)
    assert probe() is False
    path.write_text("{}", encoding="utf-8")
    assert probe() is True


def test_artifact_freshness_probe_false_for_a_stale_untouched_file(tmp_path):
    # A retry can reuse a directory that already has an EARLIER invocation's
    # run_state.json. If this invocation never touches it, the probe must NOT
    # credit that pre-existing file as its own work -- codex review
    # discussion_r3660858013.
    import os

    path = tmp_path / "run_state.json"
    path.write_text('{"run_id": "earlier_attempt"}', encoding="utf-8")
    old = datetime(2026, 7, 27, 12, 0, 0).timestamp()
    os.utime(path, (old, old))

    probe = dca_alerts.artifact_freshness_probe(path)
    # Untouched since the snapshot -- not this invocation's write.
    assert probe() is False


def test_artifact_freshness_probe_true_when_a_stale_file_is_overwritten(tmp_path):
    # The mirror: if THIS invocation genuinely rewrites the file after the
    # snapshot, it must be credited even though a stale version existed
    # before.
    import os

    path = tmp_path / "run_state.json"
    path.write_text('{"run_id": "earlier_attempt"}', encoding="utf-8")
    old = datetime(2026, 7, 27, 12, 0, 0).timestamp()
    os.utime(path, (old, old))

    probe = dca_alerts.artifact_freshness_probe(path)
    path.write_text('{"run_id": "this_invocation"}', encoding="utf-8")
    new = datetime(2026, 7, 27, 12, 5, 0).timestamp()
    os.utime(path, (new, new))
    assert probe() is True


def test_write_failed_start_marker_uses_env_root(monkeypatch, tmp_path):
    monkeypatch.setenv("DCA_ATTEMPT_LOG_ROOT", str(tmp_path))
    triggered = datetime(2026, 7, 27, 14, 0, 0, tzinfo=timezone.utc)
    path = dca_alerts.write_failed_start_marker("watchlist", ValueError("x"), triggered_at_utc=triggered)
    assert path is not None and path == tmp_path / "20260727" / "watchlist.json"
    assert path.is_file()


def test_repeated_failures_keep_every_attempt_in_the_history(tmp_path):
    # <stream>.json is a single fixed path, so a second same-day failure
    # overwrites the first and the report could only ever see the last
    # attempt. Every attempt is additionally appended to the append-only
    # <stream>.attempts.jsonl -- codex review discussion_r3659140585.
    for index, message in enumerate(("first failure", "second failure")):
        dca_alerts.write_failed_start_marker(
            "morning",
            RuntimeError(message),
            triggered_at_utc=datetime(2026, 7, 27, 14, index, 0, tzinfo=timezone.utc),
            root=str(tmp_path),
        )
    history = tmp_path / "20260727" / f"morning{dca_alerts.ATTEMPT_LOG_HISTORY_SUFFIX}"
    entries = [json.loads(line) for line in history.read_bytes().splitlines() if line.strip()]
    assert [e["error_message"] for e in entries] == ["first failure", "second failure"]
    # The single marker still holds the LATEST attempt, so existing readers
    # keep working unchanged.
    latest = json.loads((tmp_path / "20260727" / "morning.json").read_bytes())
    assert latest["error_message"] == "second failure"


def test_attempt_history_append_failure_does_not_lose_the_marker(monkeypatch, tmp_path):
    # The history append is best-effort like every other write here: if it
    # fails, the single marker must still be written rather than the whole
    # attempt being lost.
    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if self.name.endswith(dca_alerts.ATTEMPT_LOG_HISTORY_SUFFIX):
            raise OSError("history unwritable")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(dca_alerts.Path, "open", failing_open)
    path = dca_alerts.write_failed_start_marker(
        "midday",
        RuntimeError("boom"),
        triggered_at_utc=datetime(2026, 7, 27, 16, 30, 0, tzinfo=timezone.utc),
        root=str(tmp_path),
    )
    assert path is not None and path.is_file()
    assert json.loads(path.read_bytes())["error_message"] == "boom"


def test_write_failed_start_marker_never_raises_on_unwritable_root(monkeypatch):
    def broken_mkdir(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(dca_alerts.Path, "mkdir", broken_mkdir)
    result = dca_alerts.write_failed_start_marker("morning", RuntimeError("boom"), root="/no/such/root")
    assert result is None


# -- entrypoint wiring ------------------------------------------------------------


def test_run_pipeline_scheduled_live_failure_alerts_and_reraises(monkeypatch):
    import run_dca
    from near_close_dca import StrategyConfig

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda stream, exc, **kw: alerts.append(stream))

    def failing_impl(*args, **kwargs):
        raise RuntimeError("impl exploded")

    monkeypatch.setattr(run_dca, "_run_pipeline_impl", failing_impl)

    with pytest.raises(RuntimeError, match="impl exploded"):
        run_dca.run_pipeline(
            datetime(2026, 7, 23, 15, 20),
            StrategyConfig(dry_run=False),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            artifacts_dir="/tmp/unused",
            scheduled=True,
        )
    assert pings == [("afternoon", False)]
    assert alerts == ["afternoon"]


def test_run_pipeline_dry_run_never_pings(monkeypatch, tmp_path):
    import run_dca
    from near_close_dca import StrategyConfig

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: pings.append(a))

    monkeypatch.setattr(run_dca, "_run_pipeline_impl", lambda *a, **kw: {"ok": True})

    result = run_dca.run_pipeline(
        datetime(2026, 7, 23, 15, 20),
        StrategyConfig(dry_run=True),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        artifacts_dir=str(tmp_path),
        scheduled=True,
    )
    assert result == {"ok": True}
    assert pings == []


def test_run_pipeline_raw_persistence_failure_result_pings_fail(monkeypatch, tmp_path):
    import run_dca
    from near_close_dca import StrategyConfig

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda *a, **kw: alerts.append(a))

    monkeypatch.setattr(
        run_dca,
        "_run_pipeline_impl",
        lambda *a, **kw: {"raw_persistence_error": "raw/ not writable", "blocked": False},
    )

    result = run_dca.run_pipeline(
        datetime(2026, 7, 23, 15, 20),
        StrategyConfig(dry_run=False),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        artifacts_dir=str(tmp_path),
        scheduled=True,
    )
    assert result["raw_persistence_error"] == "raw/ not writable"
    assert pings == [("afternoon", False)]
    assert alerts == []


def test_morning_run_scheduled_live_failure_alerts_and_reraises(monkeypatch):
    from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda stream, *, ok=True, **kw: pings.append((stream, ok)))
    alerts = []
    monkeypatch.setattr(dca_alerts, "alert_failure", lambda stream, exc, **kw: alerts.append(stream))

    executor = MorningDCAExecutor(MorningConfig(), mode=MORNING_LIVE_MODE)
    assert executor.trades, "live-mode executor should be trading-enabled for this test"

    def failing_impl(*args, **kwargs):
        raise RuntimeError("morning exploded")

    monkeypatch.setattr(executor, "_run_impl", failing_impl)

    with pytest.raises(RuntimeError, match="morning exploded"):
        executor.run(datetime(2026, 7, 23, 10, 30), data=None)
    assert pings == [("morning", False)]
    assert alerts == ["morning"]


def test_morning_run_manual_failure_does_not_ping(monkeypatch):
    from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor

    pings = []
    monkeypatch.setattr(dca_alerts, "send_heartbeat", lambda *a, **kw: pings.append(a))

    executor = MorningDCAExecutor(MorningConfig(), mode=MORNING_LIVE_MODE)
    monkeypatch.setattr(
        executor, "_run_impl", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        executor.run(datetime(2026, 7, 23, 10, 30), data=None, manual=True)
    assert pings == []
