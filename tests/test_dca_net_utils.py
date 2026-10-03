import http.client
import inspect
import ipaddress
import socket
import urllib.error
import urllib.request
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import dca_net_utils


def test_resolve_ips():
    # Line 40: resolve_ips basic function test
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        # Mock returns [(family, type, proto, canonname, sockaddr)]
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 80)),
        ]
        ips = dca_net_utils.resolve_ips("example.com")
        assert ipaddress.ip_address("93.184.216.34") in ips
        assert ipaddress.ip_address("93.184.216.35") in ips



def test_resolve_ips_empty():
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = []
        ips = dca_net_utils.resolve_ips("example.com")
        assert ips == set()

def test_resolve_ips_oserror():
    with patch("socket.getaddrinfo", side_effect=OSError("resolution failed")):
        with pytest.raises(OSError, match="resolution failed"):
            dca_net_utils.resolve_ips("example.com")

def test_resolve_ips_ipv6():
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 80, 0, 0)),
        ]
        ips = dca_net_utils.resolve_ips("example.com")
        assert ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946") in ips

def test_resolve_ips_duplicate_ips():
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        ]
        ips = dca_net_utils.resolve_ips("example.com")
        assert len(ips) == 1
        assert ipaddress.ip_address("93.184.216.34") in ips

def test_proxy_configured_for_missing_host():
    # Line 78: missing host check in proxy_configured_for
    assert dca_net_utils.proxy_configured_for("file:///path/to/something") is False


def test_assert_safe_destination_missing_host():
    # Line 110: missing host check in assert_safe_destination
    with pytest.raises(dca_net_utils.UnsafeDestinationError, match="missing host"):
        dca_net_utils.assert_safe_destination("http://")


def test_connect_pinned_resolve_ips_oserror():
    # Lines 171-172: OSError when resolving ips in connect_pinned
    conn = MagicMock()
    conn.host = "example.com"
    with patch("dca_net_utils.resolve_ips", side_effect=OSError("resolution failed")):
        with pytest.raises(dca_net_utils.UnsafeDestinationError, match="host does not resolve"):
            dca_net_utils.connect_pinned(conn)


def test_connect_pinned_create_connection_oserror():
    # Lines 182-184: OSError for all ips when creating connection
    conn = MagicMock()
    conn.host = "example.com"
    conn.port = 80
    conn.timeout = 10
    conn.source_address = None

    with patch("dca_net_utils.resolve_ips") as mock_resolve:
        mock_resolve.return_value = {ipaddress.ip_address("8.8.8.8")}
        with patch("socket.create_connection", side_effect=OSError("connection refused")):
            with pytest.raises(OSError, match="connection refused"):
                dca_net_utils.connect_pinned(conn)


def test_pinned_http_connection_connect():
    # Line 193: connect method of PinnedHTTPConnection
    conn = dca_net_utils.PinnedHTTPConnection("example.com")
    with patch("dca_net_utils.connect_pinned") as mock_connect:
        conn.connect()
        mock_connect.assert_called_once_with(conn)


def test_pinned_https_connection_wraps_the_pinned_socket_with_host_sni():
    # Lines 207-209: connect method of PinnedHTTPSConnection, direct (untunneled)
    # branch. Asserting only that connect_pinned ran would leave the TLS step
    # itself unguarded: if the wrap were dropped the connection would carry
    # plaintext over an https:// URL and this test would still pass, so pin the
    # wrapped socket and the SNI hostname too (Codex review,
    # discussion_r3837275530).
    conn = dca_net_utils.PinnedHTTPSConnection("example.com")
    raw_sock = MagicMock(name="raw_sock")
    wrapped_sock = MagicMock(name="wrapped_sock")
    context = MagicMock()
    context.wrap_socket.return_value = wrapped_sock
    cast(Any, conn)._context = context
    conn.sock = raw_sock
    cast(Any, conn)._tunnel_host = None

    with patch("dca_net_utils.connect_pinned") as mock_connect:
        conn.connect()

    mock_connect.assert_called_once_with(conn)
    # The socket handed to TLS is the one connect_pinned left behind, and SNI
    # targets the real hostname rather than the pinned IP.
    context.wrap_socket.assert_called_once_with(raw_sock, server_hostname="example.com")
    assert conn.sock is wrapped_sock


def test_pinned_https_connection_uses_the_tunnel_host_for_sni():
    # The CONNECT-tunneled branch of the same lines: self.host is the proxy, so
    # verification and SNI must follow _tunnel_host -- the real destination --
    # or the certificate would be checked against the proxy's name.
    conn = dca_net_utils.PinnedHTTPSConnection("proxy.example.com")
    raw_sock = MagicMock(name="raw_sock")
    wrapped_sock = MagicMock(name="wrapped_sock")
    context = MagicMock()
    context.wrap_socket.return_value = wrapped_sock
    cast(Any, conn)._context = context
    conn.sock = raw_sock
    cast(Any, conn)._tunnel_host = "tunnel.example.com"

    with patch("dca_net_utils.connect_pinned") as mock_connect:
        conn.connect()

    mock_connect.assert_called_once_with(conn)
    context.wrap_socket.assert_called_once_with(raw_sock, server_hostname="tunnel.example.com")
    assert conn.sock is wrapped_sock


def test_pinned_http_handler_proxied_request():
    # Line 244: proxied request in PinnedHTTPHandler.http_open
    handler = dca_net_utils.PinnedHTTPHandler()
    req = MagicMock()
    req.selector = "http://example.com/path" # This makes request_is_proxied return True
    req._tunnel_host = None

    with patch.object(handler, "do_open") as mock_do_open:
        handler.http_open(req)
        mock_do_open.assert_called_once_with(http.client.HTTPConnection, req)


# -- SafeRedirectHandler.redirect_request signature ---------------------------------
#
# PR #1348 briefly collapsed the override to ``def redirect_request(self, *args)`` to
# dodge PLR0913. That broke keyword callers with TypeError, turned an arity mistake
# into a ValueError at the tuple unpack, and drifted from the base class's explicit
# signature (Antigravity review). These pin the explicit, urllib-dictated signature
# and prove the safety check still runs however the arguments arrive. Hermetic: the
# public-host case fakes DNS through the resolve_ips seam (as the tests above do) and
# the blocked case uses a literal loopback address, which needs no resolution at all.

_PUBLIC_IP = ipaddress.ip_address("93.184.216.34")


def test_redirect_request_signature_matches_urllib_base_class():
    ours = inspect.signature(dca_net_utils.SafeRedirectHandler.redirect_request)
    base = inspect.signature(urllib.request.HTTPRedirectHandler.redirect_request)
    assert list(ours.parameters) == list(base.parameters)
    assert list(ours.parameters) == ["self", "req", "fp", "code", "msg", "headers", "newurl"]


def test_redirect_request_keyword_call_blocks_loopback_target():
    handler = dca_net_utils.SafeRedirectHandler()
    req = urllib.request.Request("https://hc.example/start")
    with pytest.raises(urllib.error.HTTPError, match="blocked unsafe redirect destination") as excinfo:
        handler.redirect_request(
            req=req, fp=None, code=302, msg="Found", headers={}, newurl="http://127.0.0.1/secret"
        )
    # The urllib-facing HTTPError must still be chained from the real reason.
    assert isinstance(excinfo.value.__cause__, dca_net_utils.UnsafeDestinationError)


def test_redirect_request_keyword_call_allows_public_target():
    handler = dca_net_utils.SafeRedirectHandler()
    req = urllib.request.Request("https://hc.example/start")
    with patch("dca_net_utils.resolve_ips", return_value={_PUBLIC_IP}):
        new_req = handler.redirect_request(
            req=req, fp=None, code=302, msg="Found", headers={}, newurl="https://hc.example/elsewhere"
        )
    assert isinstance(new_req, urllib.request.Request)
    assert new_req.full_url == "https://hc.example/elsewhere"


def test_redirect_request_positional_call_still_enforces_safe_destination():
    handler = dca_net_utils.SafeRedirectHandler()
    req = urllib.request.Request("https://hc.example/start")
    with pytest.raises(urllib.error.HTTPError, match="blocked unsafe redirect destination"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/secret")
    with patch("dca_net_utils.resolve_ips", return_value={_PUBLIC_IP}):
        new_req = handler.redirect_request(req, None, 302, "Found", {}, "https://hc.example/elsewhere")
    assert isinstance(new_req, urllib.request.Request)
    assert new_req.full_url == "https://hc.example/elsewhere"


def test_redirect_request_missing_argument_is_a_type_error():
    # An arity mistake must surface as Python's own TypeError from argument
    # binding -- the way it does for the stock urllib method -- not as a
    # ValueError from an internal ``args[:6]`` unpack.
    handler = dca_net_utils.SafeRedirectHandler()
    req = urllib.request.Request("https://hc.example/start")
    with pytest.raises(TypeError):
        cast(Any, handler).redirect_request(req, None, 302, "Found", {})


def test_redact_url_with_port():
    # Line 318: redact_url with a port
    redacted = dca_net_utils.redact_url("http://example.com:8080/secret?token=123")
    assert redacted == "http://example.com:8080"

def test_file_handler_blocked():
    with pytest.raises(urllib.error.URLError, match="file:// scheme is blocked for security"):
        urllib.request.urlopen("file:///etc/passwd")  # nosec

def test_ftp_handler_blocked():
    with pytest.raises(urllib.error.URLError, match="ftp:// scheme is blocked for security"):
        urllib.request.urlopen("ftp://example.com")  # nosec


@pytest.mark.parametrize(
    "ip_str, expected",
    [
        # Allowed
        ("8.8.8.8", False),
        ("2001:4860:4860::8888", False),
        # Disallowed: Private
        ("192.168.1.1", True),
        ("10.0.0.1", True),
        ("fc00::1", True),
        # Disallowed: Loopback
        ("127.0.0.1", True),
        ("::1", True),
        # Disallowed: CGNAT
        ("100.64.0.1", True),
        # Disallowed: Multicast
        ("224.0.0.1", True),
        ("ff02::1", True),
        # Disallowed: Reserved
        ("240.0.0.1", True),
        # Disallowed: Site-local
        ("fec0::1", True),
    ]
)
def test_is_disallowed_ip(ip_str, expected):
    ip = ipaddress.ip_address(ip_str)
    assert dca_net_utils.is_disallowed_ip(ip) is expected
def test_assert_safe_destination_unsupported_scheme():
    with pytest.raises(dca_net_utils.UnsafeDestinationError, match="unsupported scheme"):
        dca_net_utils.assert_safe_destination("ftp://example.com")

def test_assert_safe_destination_malformed_port():
    with pytest.raises(dca_net_utils.UnsafeDestinationError, match="malformed port"):
        dca_net_utils.assert_safe_destination("https://example.com:notaport/")

def test_assert_safe_destination_literal_disallowed_ip():
    with pytest.raises(dca_net_utils.UnsafeDestinationError, match="disallowed address"):
        dca_net_utils.assert_safe_destination("http://127.0.0.1")

def test_assert_safe_destination_literal_allowed_ip():
    # Should return silently
    dca_net_utils.assert_safe_destination("http://93.184.216.34")

def test_assert_safe_destination_proxy_fails_resolution_with_proxy():
    # Hostname fails to resolve locally, but a proxy is configured for it
    with patch("dca_net_utils.resolve_ips", side_effect=OSError("resolution failed")):
        with patch("dca_net_utils.proxy_configured_for", return_value=True):
            dca_net_utils.assert_safe_destination("https://unresolvable-but-proxied.example.com")

def test_assert_safe_destination_proxy_fails_resolution_without_proxy():
    # Hostname fails to resolve locally, and no proxy is configured
    with patch("dca_net_utils.resolve_ips", side_effect=OSError("resolution failed")):
        with patch("dca_net_utils.proxy_configured_for", return_value=False):
            with pytest.raises(dca_net_utils.UnsafeDestinationError, match="host does not resolve"):
                dca_net_utils.assert_safe_destination("https://unresolvable.example.com")

def test_assert_safe_destination_resolves_to_disallowed_ip():
    with patch("dca_net_utils.resolve_ips") as mock_resolve:
        mock_resolve.return_value = {ipaddress.ip_address("169.254.169.254")}
        with pytest.raises(dca_net_utils.UnsafeDestinationError, match="host resolves to a disallowed address"):
            dca_net_utils.assert_safe_destination("https://example.com")


def test_assert_safe_destination_resolves_empty_set():
    with patch("dca_net_utils.resolve_ips") as mock_resolve:
        mock_resolve.return_value = set()
        with pytest.raises(dca_net_utils.UnsafeDestinationError, match="host resolves to a disallowed address"):
            dca_net_utils.assert_safe_destination("https://example.com")


def test_assert_safe_destination_resolves_to_allowed_ip():
    with patch("dca_net_utils.resolve_ips") as mock_resolve:
        mock_resolve.return_value = {ipaddress.ip_address("93.184.216.34")}
        # Should return silently
        dca_net_utils.assert_safe_destination("https://example.com")
