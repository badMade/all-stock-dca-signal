"""Shared outbound-network safety checks for webhook/heartbeat requests.

Consolidates the SSRF-hardening logic (DNS-rebinding-resistant "resolve once,
connect to the resolved address" pinning; a private/reserved/link-local/
carrier-NAT denylist; redirect re-validation; forward-proxy awareness) that
``dca_alerts`` and ``scripts/drift_detection.py`` both need before making an
outbound request to an operator-supplied URL (Slack webhook, heartbeat ping).
It was previously duplicated between those two modules and had drifted; this
is the single implementation both now import.

Entry points:
  * :func:`assert_safe_destination` -- validate a URL before requesting it.
  * :func:`connect_pinned` -- pin an ``http.client`` connection to the
    already-validated address (used by :class:`PinnedHTTPConnection` /
    :class:`PinnedHTTPSConnection`).
  * :class:`PinnedHTTPHandler` / :class:`PinnedHTTPSHandler` -- drop-in
    ``urllib.request`` handlers that route non-proxied requests through the
    pinned connection classes; pass to ``urllib.request.build_opener()``.
  * :class:`SafeRedirectHandler` -- re-runs the same validation on every
    redirect hop so an initially-allowed URL cannot 302 its way past it.
"""

import ipaddress
import logging
import socket
import urllib.error
import urllib.request
from http.client import HTTPConnection, HTTPSConnection
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

class UnsafeDestinationError(Exception):
    """URL scheme is not http(s), or its host resolves to a disallowed address."""


def resolve_ips(hostname: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``hostname``'s IP addresses. A seam so tests can fake DNS."""
    return {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(hostname, None)}


def is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True unless ``ip`` is safe to open an outbound webhook/heartbeat connection to.

    ``is_global`` is documented as having "value opposite to is_private,
    except for the 100.64.0.0/10 [carrier-grade NAT / shared address space]
    range where they are both False" -- so checking only ``is_private`` (the
    pre-fix state) let a resolved address in that range through even though
    it can address services inside a VPC, Kubernetes cluster, or carrier
    network. ``is_multicast`` and IPv6's deprecated site-local space
    (``fec0::/10``) are checked explicitly because ``is_global`` reports
    both of those as ``True``.
    """
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or bool(getattr(ip, "is_site_local", False))
    )


def proxy_configured_for(url: str) -> bool:
    """True if an environment-configured forward proxy applies to ``url``.

    Mirrors the same resolution :class:`urllib.request.ProxyHandler` performs
    (``getproxies()`` plus a ``no_proxy``-aware bypass check) so the safety
    checks below can recognize a proxy-only deployment before running local
    DNS resolution: in that mode the proxy -- not this process -- resolves
    and connects to the destination, so requiring a local, successful
    ``getaddrinfo()`` here would reject an otherwise-legitimate destination
    whose host intentionally does not resolve on this host (chatgpt-codex-connector
    review discussion_r3679190507).
    """
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        return False
    proxy = urllib.request.getproxies().get(parsed.scheme.lower())
    if not proxy:
        return False
    return not urllib.request.proxy_bypass(hostname)


def assert_safe_destination(url: str) -> None:
    """Raise :class:`UnsafeDestinationError` unless ``url`` is http(s) and every
    address its host resolves to is a public, routable address.

    Restricting only the scheme (the pre-fix state) still lets an
    attacker-influenced webhook/heartbeat value point at ``http://127.0.0.1:8080/``
    or the ``http://169.254.169.254/latest/meta-data/`` cloud-metadata endpoint.
    Every resolved address is checked, not just the first, since a hostname can
    resolve to a mix of public and private IPs.

    This is a preflight check only: it resolves ``hostname`` in a separate
    lookup from the one the eventual connection makes, which -- for an
    attacker who controls the destination host's DNS -- leaves a window to
    answer this lookup with a public address and a later one (made when the
    connection is actually opened) with a private/loopback address (DNS
    rebinding). :class:`PinnedHTTPConnection` below closes that gap for the
    actual request by resolving, validating, and connecting in one step;
    this function still runs first so callers get an early, clear rejection
    without attempting a connection at all.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeDestinationError(f"unsupported scheme {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeDestinationError("missing host")
    try:
        _ = parsed.port
    except ValueError as exc:
        # e.g. "https://example.com:notaport/..." -- .hostname parses fine but
        # .port raises ValueError; left unchecked, that same access later (in
        # _redact_url's failure-log path) raises again out of an exception
        # handler that must never itself raise (codex review
        # discussion_r3679190501). Reject it here instead.
        raise UnsafeDestinationError("malformed port") from exc
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        # A literal address (e.g. "https://169.254.169.254/...") needs no DNS
        # resolution at all -- checking it is free regardless of whether a
        # proxy is configured, and skipping it (the proxy-bypass branch
        # below) would let an attacker-controlled literal straight through
        # in proxy-enabled deployments (codex review on this PR).
        if is_disallowed_ip(literal_ip):
            raise UnsafeDestinationError("disallowed address")
        return
    try:
        ips = resolve_ips(hostname)
    except OSError as exc:
        # Only a hostname that genuinely fails to resolve locally gets the
        # proxy exemption -- that's the deployment this exists for (the
        # proxy resolves it remotely). A hostname that DOES resolve locally
        # is still checked below even when some proxy happens to be
        # configured (e.g. a blanket corporate egress policy unrelated to
        # this URL): unconditionally skipping validation whenever any proxy
        # exists let an attacker-controlled hostname (as opposed to a
        # literal IP, handled above) resolve via the proxy to a private/
        # link-local address completely unchecked (codex review on this PR).
        if proxy_configured_for(url):
            return
        raise UnsafeDestinationError("host does not resolve") from exc
    if not ips or any(is_disallowed_ip(ip) for ip in ips):
        raise UnsafeDestinationError("host resolves to a disallowed address")


def connect_pinned(conn: "HTTPConnection") -> None:
    """Resolve ``conn.host``, validate every resolved address, and connect to
    one of the validated addresses -- all in one step.

    :func:`_assert_safe_destination` performs its own ``getaddrinfo()`` call
    well before a connection is opened; letting ``HTTPConnection.connect()``
    independently re-resolve the same hostname afterwards leaves a DNS
    rebinding window for an attacker who controls the destination host's DNS
    to answer the preflight lookup with a public address and this later one
    with a private/loopback/link-local address (codex review
    discussion_r3679190498). Connecting to the exact address this function
    just validated -- instead of handing the hostname to a fresh lookup --
    closes that gap. The hostname itself is left untouched on ``conn``, so
    the HTTP ``Host`` header and (for HTTPS, via ``HTTPSConnection.connect()``
    calling this through ``super().connect()``) the TLS SNI value are
    unaffected.
    """
    try:
        ips = list(resolve_ips(conn.host))
    except OSError as exc:
        raise UnsafeDestinationError("host does not resolve") from exc
    if not ips or any(is_disallowed_ip(ip) for ip in ips):
        raise UnsafeDestinationError("host resolves to a disallowed address")
    last_exc: Optional[OSError] = None
    for ip in ips:
        try:
            conn.sock = socket.create_connection(
                (str(ip), conn.port), conn.timeout, getattr(conn, "source_address", None)
            )
            return
        except OSError as exc:
            last_exc = exc
    raise last_exc if last_exc is not None else OSError("no address to connect to")


class PinnedHTTPConnection(HTTPConnection):
    """An HTTP connection that pins its socket to a validated address (see
    :func:`_connect_pinned`) instead of independently re-resolving ``host``.
    """

    def connect(self) -> None:
        connect_pinned(self)


class PinnedHTTPSConnection(HTTPSConnection):
    """The HTTPS equivalent of :class:`_PinnedHTTPConnection`.

    ``HTTPSConnection.connect()`` calls ``super().connect()`` to obtain the
    raw socket and then wraps it for TLS using ``self.host`` (unchanged here)
    as the SNI ``server_hostname`` -- so overriding only the raw connect step
    keeps TLS verification and SNI targeting the real hostname, not the
    pinned IP.
    """

    def connect(self) -> None:
        connect_pinned(self)
        server_hostname = getattr(self, "_tunnel_host", None) or self.host
        context = getattr(self, "_context", None)
        if context is not None:
            self.sock = context.wrap_socket(self.sock, server_hostname=server_hostname)


def request_is_proxied(req: "urllib.request.Request") -> bool:
    """True if ``req`` is being routed through a forward proxy.

    ``urllib.request.ProxyHandler`` rewrites the request before our handlers
    below see it: for a CONNECT-tunneled (typically HTTPS) proxy it sets
    ``req._tunnel_host`` to the real target and points ``req.host`` at the
    proxy; for a plain-HTTP proxy it points ``req.host`` at the proxy and
    rewrites ``req.selector`` to the full original URL (normally just a
    path). Either way, ``req.host`` at that point is the *proxy's* address,
    not the destination's -- pinning/validating it against the
    private-address denylist would reject the (often private-range) proxy
    itself and break proxy-only deployments (codex review
    discussion_r3679190507), so those requests fall back to the stock,
    unpinned connection classes and let the configured proxy do the
    resolving and connecting.
    """
    if getattr(req, "_tunnel_host", None):
        return True
    return req.selector.startswith(("http://", "https://"))


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    """``urllib.request`` handler that opens non-proxied requests through
    :class:`PinnedHTTPConnection`, so the connection targets the exact
    address :func:`assert_safe_destination` validated rather than
    independently re-resolving the host at connect time. Proxied requests
    (see :func:`request_is_proxied`) fall back to the stock connection class
    and let the configured proxy resolve and connect instead.
    """

    # Note: These methods are dynamically invoked by urllib.request.OpenerDirector.
    # Do not remove; they are required to enforce SSRF protection via IP pinning.
    def http_open(self, req):
        if request_is_proxied(req):
            return self.do_open(HTTPConnection, req)
        return self.do_open(PinnedHTTPConnection, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """The HTTPS equivalent of :class:`PinnedHTTPHandler`: opens non-proxied
    requests through :class:`PinnedHTTPSConnection` so TLS is established
    against the already-validated, pinned address while SNI/verification
    still target the real hostname.
    """

    # Note: These methods are dynamically invoked by urllib.request.OpenerDirector.
    # Do not remove; they are required to enforce SSRF protection via IP pinning.
    def https_open(self, req):
        # Only pass context, not check_hostname: on Python 3.12+,
        # HTTPSHandler.__init__ folds check_hostname into the SSL context
        # and no longer sets self._check_hostname at all, and
        # HTTPSConnection.__init__ no longer accepts a
        # check_hostname argument either -- accessing/forwarding it here
        # raised AttributeError/TypeError on every real HTTPS request under
        # 3.12+ (Codex review on this PR). This handler is always
        # constructed with no arguments (build_opener() below), so
        # self._check_hostname is always the default None on 3.11 too,
        # making this identical in practice to the stock 3.11 behavior.
        context = getattr(self, "_context", None)
        if request_is_proxied(req):
            return self.do_open(HTTPSConnection, req, context=context)
        return self.do_open(PinnedHTTPSConnection, req, context=context)


class BlockedFileHandler(urllib.request.FileHandler):
    """Completely disable file:// URLs at the global opener level to prevent
    SSRF/LFI even if a caller bypasses assert_safe_destination.
    """
    def file_open(self, req):
        raise urllib.error.URLError("file:// scheme is blocked for security")


class BlockedFTPHandler(urllib.request.FTPHandler):
    """Completely disable ftp:// URLs at the global opener level to prevent
    SSRF/LFI even if a caller bypasses assert_safe_destination.
    """
    def ftp_open(self, req):
        raise urllib.error.URLError("ftp:// scheme is blocked for security")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target the same way as the initial request.

    ``urlopen``'s default redirect handling blindly follows ``Location``
    headers, so an initially-allowed public URL could 302 to
    ``http://169.254.169.254/...`` and defeat :func:`_assert_safe_destination`
    entirely. Each hop through this handler re-runs the same scheme and
    resolved-address check before it is followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Signature dictated by urllib.request.HTTPRedirectHandler: keep it explicit
        # and name-for-name with the base class so keyword callers and super() keep
        # working (a *args collapse broke both -- Antigravity review on PR #1348).
        # PLR0913 is not in this repo's ruff rule set, so no noqa marker is needed.
        try:
            assert_safe_destination(newurl)
        except UnsafeDestinationError as exc:
            raise urllib.error.HTTPError(
                newurl, code, f"blocked unsafe redirect destination: {exc}", headers, fp
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Installed once at import time so every urllib.request.urlopen() call in this
# process -- not just the ones in this module -- gets redirect validation and
# pinned-address connections.
urllib.request.install_opener(
    urllib.request.build_opener(SafeRedirectHandler, PinnedHTTPHandler, PinnedHTTPSHandler, BlockedFileHandler, BlockedFTPHandler)
)


def redact_url(url: str) -> str:
    """Reduce ``url`` to ``scheme://host[:port]``, dropping path/query/fragment.

    Healthcheck-style heartbeat URLs commonly carry their auth token in the
    path (e.g. ``https://hc.example/<token>``), so anything logged about a
    delivery failure must not include it.
    """
    parsed = urlsplit(url)
    netloc = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        # A malformed port (e.g. "https://example.com:notaport/...") is
        # rejected by _assert_safe_destination before delivery is ever
        # attempted, but this helper must not raise regardless -- it's called
        # from an exception handler that itself must never raise (codex
        # review discussion_r3679190501).
        port = None
    if port:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme}://{netloc}" if netloc else "(unknown)"


def sanitize_exception(exc: BaseException) -> str:
    """Render ``exc`` as its class name plus, only for known-safe cases, a
    short reason -- never its full message.

    A delivery failure can raise exceptions whose message embeds the
    complete offending URL/path -- e.g. ``http.client.InvalidURL`` for a
    heartbeat/webhook URL containing a space or control character quotes the
    whole path back -- and healthcheck-style URLs commonly carry their auth
    token in that path. Logging ``str(exc)`` unconditionally would undo
    :func:`_redact_url`'s redaction for exactly the malformed-URL case where
    it matters most (codex review discussion_r3679190511).
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"{type(exc).__name__}: HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        # ``reason`` can be a plain string OR an underlying exception object
        # (e.g. a gaierror) constructed from arbitrary context -- stringifying
        # it directly would risk reintroducing exactly the URL/path leak this
        # function exists to prevent (Copilot review on this PR), so only its
        # TYPE (plus, for an OSError-like reason, its numeric errno -- never
        # its message) is reported.
        reason = exc.reason
        if isinstance(reason, OSError) and getattr(reason, "errno", None) is not None:
            return f"{type(exc).__name__}: errno {reason.errno}"
        if isinstance(reason, BaseException):
            return f"{type(exc).__name__}: {type(reason).__name__}"
        return type(exc).__name__
    return type(exc).__name__
