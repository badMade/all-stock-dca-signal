import http.client
import ipaddress
import socket
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
    conn._context = context
    conn.sock = raw_sock
    conn._tunnel_host = None

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
    conn._context = context
    conn.sock = raw_sock
    conn._tunnel_host = "tunnel.example.com"

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


def test_redact_url_with_port():
    # Line 318: redact_url with a port
    redacted = dca_net_utils.redact_url("http://example.com:8080/secret?token=123")
    assert redacted == "http://example.com:8080"
