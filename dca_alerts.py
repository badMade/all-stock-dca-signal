"""Operator alerting and dead-man's-switch heartbeats for live DCA runs.

Two gaps this module closes for scheduled live runs:

* **Escaping exceptions were silent.** The only in-code alert lived inside
  ``run_pipeline``'s raw-persistence handler; a failure anywhere else propagated
  to the external scheduler with no notification. :func:`guarded_run` wraps a
  stream's entrypoint so *any* escaping exception is logged, alerted to Slack,
  and pinged as a failed heartbeat before re-raising.
* **A run that never fires looks like a quiet day.** Compact operating mode
  suppresses no-op notifications, so the 2026-07-16 / 2026-07-20 missed-fire
  incidents produced no signal at all. Per-stream success heartbeats (a
  healthchecks.io-style dead-man's switch) make the *absence* of a run alert.

Configuration is environment-only (no secrets in git, matching
``scripts/drift_detection.py``):

* ``DCA_HEARTBEAT_URL_<STREAM>`` (e.g. ``DCA_HEARTBEAT_URL_MORNING``) or the
  shared fallback ``DCA_HEARTBEAT_URL`` — pinged with a GET on success, and at
  ``<url>/fail`` on failure. Unset means heartbeats are a no-op.
* ``DCA_ALERT_SLACK_WEBHOOK``, falling back to ``DCA_DRIFT_SLACK_WEBHOOK`` (so
  an existing drift-alert deployment gets failure alerts with zero new config) —
  POSTed a Slack ``{"text": ...}`` payload on failure. Unset means no alert.

Alert and heartbeat delivery never raises: a broken/unreachable webhook must not
mask the original run failure (it is logged instead).
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import socket
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import urlsplit

from dca_fileio import atomic_write_text
from dca_paths import stream_root

logger = logging.getLogger("near_close_dca.alerts")

HEARTBEAT_URL_ENV_PREFIX = "DCA_HEARTBEAT_URL"
FAILURE_WEBHOOK_ENV = "DCA_ALERT_SLACK_WEBHOOK"
# Same env var scripts/drift_detection.py uses (DEFAULT_SLACK_WEBHOOK_ENV);
# duplicated as a literal so this module stays free of project imports and is
# usable from any module without cycles.
FALLBACK_WEBHOOK_ENV = "DCA_DRIFT_SLACK_WEBHOOK"
# Root for the failed-start attempt log (see _write_failed_start_marker).
# Same cron_tracking root scripts/run_classification.CRON_TRACKING_ROOT uses,
# duplicated as a literal for the same no-project-imports reason as
# FALLBACK_WEBHOOK_ENV above.
ATTEMPT_LOG_ROOT_ENV = "DCA_ATTEMPT_LOG_ROOT"
# Resolved at IMPORT time and kept only as the module's advertised default for
# callers that read the constant. The write path below deliberately does NOT use
# it: $DCA_CRON_TRACKING_ROOT set after this module was imported would leave the
# marker under the old tree while the credit-usage report, which resolves the
# shared root per call, scans the relocated one -- and a fired-but-failed slot
# then reads as a MISSING run rather than a failed one (Codex review, PR #219).
DEFAULT_ATTEMPT_LOG_ROOT = stream_root("attempt_log")
# Suffix of the append-only per-attempt history written alongside the single
# latest-attempt <stream>.json marker, so a stream that fails more than once
# on the same trading day keeps every failure rather than overwriting all but
# the last -- codex review discussion_r3659140585.
ATTEMPT_LOG_HISTORY_SUFFIX = ".attempts.jsonl"

_DEFAULT_TIMEOUT_SECONDS = 10.0
_TRACEBACK_CHAR_LIMIT = 1500

T = TypeVar("T")


def attempt_log_root_sibling_to(model_usage_log_root: Optional[str]) -> Optional[str]:
    """Derive the attempt_log root as a sibling of a configured model_usage_log_root.

    A deployment that overrides model_usage_log_root (e.g. a non-default
    artifacts_dir/cron_tracking root) but doesn't separately set
    DCA_ATTEMPT_LOG_ROOT would otherwise have write_failed_start_marker fall
    back to DEFAULT_ATTEMPT_LOG_ROOT while the report reads attempt_log from
    the custom root, missing every marker -- codex review
    discussion_r3658373966. Returns None (let write_failed_start_marker use
    its own env-var/default) when model_usage_log_root itself isn't set.

    An explicit DCA_ATTEMPT_LOG_ROOT env var always takes precedence over a
    derived sibling: returning None here (rather than the derived path) lets
    write_failed_start_marker's own ``root or env-var or default`` resolution
    fall through to that env var, instead of this helper unconditionally
    deriving a sibling path that would otherwise always win (an explicit
    ``root=`` argument beats the env var) and silently shadow a deployment's
    intentional override -- codex review discussion_r3659124650.
    """
    if os.environ.get(ATTEMPT_LOG_ROOT_ENV):
        return None
    if not model_usage_log_root:
        return None
    return str(Path(model_usage_log_root).parent / "attempt_log")


def ensure_log_handler(level: int = logging.INFO) -> None:
    """Install a root stream handler if none exists (idempotent).

    Library modules never configure handlers; if the scheduler also does not,
    Python's last-resort handler drops everything below WARNING — including
    "Placed buy for …" lines. ``logging.basicConfig`` is a no-op when the root
    logger already has handlers, so calling this from every entrypoint is safe.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


class _UnsafeDestinationError(Exception):
    """URL scheme is not http(s), or its host resolves to a disallowed address."""


def _resolve_ips(hostname: str) -> "set[ipaddress._BaseAddress]":
    """Resolve ``hostname``'s IP addresses. A seam so tests can fake DNS."""
    return {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(hostname, None)}


def _is_disallowed_ip(ip: "ipaddress._BaseAddress") -> bool:
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


def _proxy_configured_for(url: str) -> bool:
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


def _assert_safe_destination(url: str) -> None:
    """Raise :class:`_UnsafeDestinationError` unless ``url`` is http(s) and every
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
    rebinding). :class:`_PinnedHTTPConnection` below closes that gap for the
    actual request by resolving, validating, and connecting in one step;
    this function still runs first so callers get an early, clear rejection
    (e.g. :func:`_validated_url`'s config-time warning) without attempting a
    connection at all.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise _UnsafeDestinationError(f"unsupported scheme {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise _UnsafeDestinationError("missing host")
    try:
        _ = parsed.port
    except ValueError as exc:
        # e.g. "https://example.com:notaport/..." -- .hostname parses fine but
        # .port raises ValueError; left unchecked, that same access later (in
        # _redact_url's failure-log path) raises again out of an exception
        # handler that must never itself raise (codex review
        # discussion_r3679190501). Reject it here instead.
        raise _UnsafeDestinationError("malformed port") from exc
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
        if _is_disallowed_ip(literal_ip):
            raise _UnsafeDestinationError("disallowed address")
        return
    try:
        ips = _resolve_ips(hostname)
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
        if _proxy_configured_for(url):
            return
        raise _UnsafeDestinationError("host does not resolve") from exc
    if not ips or any(_is_disallowed_ip(ip) for ip in ips):
        raise _UnsafeDestinationError("host resolves to a disallowed address")


def _connect_pinned(conn: "http.client.HTTPConnection") -> None:
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
        ips = list(_resolve_ips(conn.host))
    except OSError as exc:
        raise _UnsafeDestinationError("host does not resolve") from exc
    if not ips or any(_is_disallowed_ip(ip) for ip in ips):
        raise _UnsafeDestinationError("host resolves to a disallowed address")
    last_exc: Optional[OSError] = None
    for ip in ips:
        try:
            conn.sock = socket.create_connection(
                (str(ip), conn.port), conn.timeout, conn.source_address
            )
            return
        except OSError as exc:
            last_exc = exc
    raise last_exc if last_exc is not None else OSError("no address to connect to")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection that pins its socket to a validated address (see
    :func:`_connect_pinned`) instead of independently re-resolving ``host``.
    """

    def connect(self) -> None:
        _connect_pinned(self)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The HTTPS equivalent of :class:`_PinnedHTTPConnection`.

    ``HTTPSConnection.connect()`` calls ``super().connect()`` to obtain the
    raw socket and then wraps it for TLS using ``self.host`` (unchanged here)
    as the SNI ``server_hostname`` -- so overriding only the raw connect step
    keeps TLS verification and SNI targeting the real hostname, not the
    pinned IP.
    """

    def connect(self) -> None:
        _connect_pinned(self)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


def _request_is_proxied(req: "urllib.request.Request") -> bool:
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


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        if _request_is_proxied(req):
            return self.do_open(http.client.HTTPConnection, req)
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # Only pass context, not check_hostname: on Python 3.12+,
        # HTTPSHandler.__init__ folds check_hostname into the SSL context
        # and no longer sets self._check_hostname at all, and
        # http.client.HTTPSConnection.__init__ no longer accepts a
        # check_hostname argument either -- accessing/forwarding it here
        # raised AttributeError/TypeError on every real HTTPS request under
        # 3.12+ (Codex review on this PR). This handler is always
        # constructed with no arguments (build_opener() below), so
        # self._check_hostname is always the default None on 3.11 too,
        # making this identical in practice to the stock 3.11 behavior.
        if _request_is_proxied(req):
            return self.do_open(http.client.HTTPSConnection, req, context=self._context)
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target the same way as the initial request.

    ``urlopen``'s default redirect handling blindly follows ``Location``
    headers, so an initially-allowed public URL could 302 to
    ``http://169.254.169.254/...`` and defeat :func:`_assert_safe_destination`
    entirely. Each hop through this handler re-runs the same scheme and
    resolved-address check before it is followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        try:
            _assert_safe_destination(newurl)
        except _UnsafeDestinationError as exc:
            raise urllib.error.HTTPError(
                newurl, code, f"blocked unsafe redirect destination: {exc}", headers, fp
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Installed once at import time so every urllib.request.urlopen() call in this
# process -- not just the ones in this module -- gets redirect validation and
# pinned-address connections.
urllib.request.install_opener(
    urllib.request.build_opener(_SafeRedirectHandler, _PinnedHTTPHandler, _PinnedHTTPSHandler)
)


def _redact_url(url: str) -> str:
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


def _sanitize_exception(exc: BaseException) -> str:
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


def _validated_url(url: Optional[str], purpose: str) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    try:
        _assert_safe_destination(url)
    except _UnsafeDestinationError:
        logger.warning("ignoring %s URL: not a safe http(s) destination", purpose)
        return None
    return url


def heartbeat_url(stream: str) -> Optional[str]:
    """Resolve the heartbeat base URL for ``stream`` from the environment."""
    stream_env = f"{HEARTBEAT_URL_ENV_PREFIX}_{stream.upper()}"
    url = os.environ.get(stream_env) or os.environ.get(HEARTBEAT_URL_ENV_PREFIX)
    return _validated_url(url, f"heartbeat ({stream_env})")


def send_heartbeat(
    stream: str, *, ok: bool = True, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> bool:
    """GET the stream's heartbeat URL (``<url>/fail`` when ``ok`` is False).

    Returns True only when a ping was attempted and got a 2xx response. Never
    raises — heartbeat delivery must not break (or mask the failure of) a run.
    """
    url = heartbeat_url(stream)
    if url is None:
        return False
    if not ok:
        url = url.rstrip("/") + "/fail"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - operator-supplied URL
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 - delivery failure must not raise
        logger.warning(
            "heartbeat ping for %s failed (%s): %s",
            stream,
            _redact_url(url),
            _sanitize_exception(exc),
        )
        return False


def alert_failure(
    stream: str,
    exc: BaseException,
    *,
    context: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """POST a failure message to the operator Slack webhook, if configured.

    Returns True only when an alert was attempted and got a 2xx response. Never
    raises — a broken webhook must not mask the original run failure.
    """
    url = _validated_url(
        os.environ.get(FAILURE_WEBHOOK_ENV) or os.environ.get(FALLBACK_WEBHOOK_ENV),
        "failure alert webhook",
    )
    if url is None:
        return False
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(trace) > _TRACEBACK_CHAR_LIMIT:
        trace = "…" + trace[-_TRACEBACK_CHAR_LIMIT:]
    lines = [f":rotating_light: DCA {stream} live run FAILED: {type(exc).__name__}: {exc}"]
    if context:
        lines.append(context)
    lines.append(f"```{trace}```")
    payload = json.dumps({"text": "\n".join(lines)}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied webhook
            return 200 <= response.status < 300
    except Exception as delivery_exc:  # noqa: BLE001 - delivery failure must not raise
        logger.warning(
            "failure alert for %s could not be delivered (%s): %s",
            stream,
            _redact_url(url),
            _sanitize_exception(delivery_exc),
        )
        return False


def write_failed_start_marker(
    stream: str,
    exc: BaseException,
    *,
    triggered_at_utc: Optional[datetime] = None,
    root: Optional[str] = None,
    run_artifact_written: bool = False,
) -> Optional[Path]:
    """Persist a compact local record that ``stream`` was attempted and crashed
    before writing any run artifact of its own.

    ``run_artifact_written`` records WHICH PHASE the failure happened in, and
    is the difference between a genuine failed start and an ordinary late
    error. A scheduled invocation can persist a valid run_state.json and only
    then raise -- the supported ``generate_full_reports=True`` path, for
    instance, fails after ``run_daily_compact()`` has already written the run
    -- and an unconditional marker claims "failed before writing any run
    artifact" about a run whose artifact plainly exists. The report then sees
    a valid run directory plus a marker and fabricates an earlier failed start
    followed by a successful retry, inflating recovery/error counts and
    forcing the high error band off a run that actually completed its work
    (codex review discussion_r3659298031). Pass True when the run's own
    artifact is already on disk; the marker records
    ``failure_phase='after_run_artifact'`` and readers must not treat it as a
    failed start.

    Slack alerts and heartbeat pings are external, fire-and-forget calls with
    no local trace, so a report reading only cron_tracking artifacts cannot
    tell a scheduled invocation that raised before writing anything apart from
    a slot the scheduler never triggered at all -- both look like a totally
    empty run directory (Codex review: "Persist evidence that distinguishes
    failed starts from missed slots"). This marker is the durable, locally
    readable signal that closes that gap: written under
    ``<root>/YYYYMMDD/<stream>.json`` (root defaults to
    ``DCA_ATTEMPT_LOG_ROOT`` env var, then ``DEFAULT_ATTEMPT_LOG_ROOT``), it
    records the concrete failure so classification can key off it rather than
    guessing from an empty directory.

    A stream can fail more than once on the same trading day (the scheduler
    retries, or a multi-slot task such as 7be82b97 fires again later), and
    ``<day>/<stream>.json`` is a single fixed path, so each failure used to
    overwrite the previous one and the report could only ever observe the
    LAST attempt -- undercounting error_count, retry evidence, and recovery
    rows for exactly the repeated-failure case they matter most for (codex
    review discussion_r3659140585, discussion_r3659124633). Every attempt is therefore ALSO appended
    as one line to ``<day>/<stream>.attempts.jsonl``, which is append-only and
    never rewritten. ``<stream>.json`` is still written, holding the latest
    attempt, so existing readers keep working unchanged; readers that need
    every attempt (see the report's ``_read_attempt_markers``) prefer the
    JSONL.

    Best-effort and never raises: a broken/unwritable attempt log must not
    mask (or break) the original run failure, exactly like alert_failure and
    send_heartbeat above. Returns the written ``<stream>.json`` path, or None
    if the write failed or was skipped.
    """
    try:
        triggered = triggered_at_utc or datetime.now(timezone.utc)
        # stream_root() per call, not DEFAULT_ATTEMPT_LOG_ROOT: see that
        # constant's note -- the writer and the report must resolve the shared
        # root at the same moment or they disagree about where the marker lives.
        log_root = root or os.environ.get(ATTEMPT_LOG_ROOT_ENV) or stream_root("attempt_log")
        day_dir = Path(log_root) / triggered.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(trace) > _TRACEBACK_CHAR_LIMIT:
            trace = "…" + trace[-_TRACEBACK_CHAR_LIMIT:]
        marker = {
            "stream": stream,
            "error_class": type(exc).__name__,
            "error_message": str(exc),
            "traceback": trace,
            "triggered_at_utc": triggered.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # See run_artifact_written above: 'before_run_artifact' is the
            # true failed start this marker was built for; 'after_run_artifact'
            # is an ordinary late error on a run that DID persist its artifact,
            # and must never be reported as a failed start.
            "failure_phase": "after_run_artifact" if run_artifact_written else "before_run_artifact",
        }
        serialized = json.dumps(marker, sort_keys=True)
        # Append-only history first: if the single-marker write below fails,
        # the attempt is still recorded somewhere rather than lost entirely.
        try:
            with (day_dir / f"{stream}{ATTEMPT_LOG_HISTORY_SUFFIX}").open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
        except OSError:
            logger.exception("failed-start attempt-history append failed for %s", stream)
        path = day_dir / f"{stream}.json"
        atomic_write_text(path, serialized, encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 - see alert_failure/send_heartbeat above
        logger.exception("failed-start marker write raised for %s", stream)
        return None


def artifact_freshness_probe(path: "str | Path") -> Callable[[], bool]:
    """Build a ``run_artifact_written`` probe for :func:`guarded_run` that
    proves THIS invocation wrote ``path`` -- not that a stale file from an
    earlier invocation happens to already sit there.

    A scheduled slot's own run_state.json can already exist when a call is
    made: an external-scheduler retry reuses the SAME directory as the
    attempt before it, so a bare existence check would report
    ``after_run_artifact`` for a crash that this invocation's own work never
    reached, attributing the failure to a wholly earlier, unrelated attempt's
    artifact instead of recording a genuine failed start -- codex review
    discussion_r3660858013. Snapshotting ``path``'s mtime (or absence) at
    call time and comparing against it later distinguishes the two: only a
    path that is newly created, or modified since this snapshot, counts as
    this invocation's own write.

    Call this BEFORE invoking the guarded function (so the snapshot predates
    any write it could make) and pass the returned callable as
    ``guarded_run(..., run_artifact_written=...)``.
    """
    p = Path(path)
    try:
        before = p.stat().st_mtime
    except OSError:
        before = None

    def _probe() -> bool:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return False
        return before is None or mtime > before

    return _probe


def guarded_run(
    stream: str,
    fn: Callable[..., T],
    *args: Any,
    enabled: bool = True,
    context: Optional[str] = None,
    result_ok: Optional[Callable[[T], bool]] = None,
    attempt_log_root: Optional[str] = None,
    run_artifact_written: Optional[Callable[[], bool]] = None,
    **kwargs: Any,
) -> T:
    """Run ``fn`` with guaranteed logging, failure alerting, and heartbeats.

    On success the stream's heartbeat is pinged OK; on any ``Exception`` the
    failure is logged with traceback, alerted to Slack, pinged as a failed
    heartbeat, and then **re-raised** so the scheduler still sees the failure.
    With ``enabled=False`` (manual/dry-run/demo invocations) ``fn`` runs with a
    log handler ensured but no alerting or heartbeats.

    ``result_ok`` classifies a *returned* result: entrypoints that catch their
    own failures and report them as data (e.g. the raw-persistence failure path
    returns a result carrying ``raw_persistence_error`` instead of raising)
    would otherwise ping a healthy heartbeat for a run that did no trading
    work. When the predicate returns False — or itself raises (fail closed) —
    the heartbeat is pinged as failed. No Slack alert is sent for that case:
    the structured-failure paths already emit their own targeted alerts, and a
    duplicate here would double-page. The result is returned unchanged either
    way.

    An escaping ``Exception`` also gets a durable local marker via
    :func:`write_failed_start_marker`, in addition to the Slack alert and
    failed heartbeat: those two are fire-and-forget external calls with no
    local trace, so this is the only locally readable evidence that
    distinguishes an attempted-and-crashed run from a slot the scheduler never
    triggered at all. ``attempt_log_root`` overrides where that marker is
    written (see :func:`attempt_log_root_sibling_to`); ``None`` lets
    :func:`write_failed_start_marker` fall back to its own env-var/default.

    ``run_artifact_written`` is an optional zero-argument probe reporting
    whether the run's own artifact is already on disk when the exception
    escapes. A scheduled run can persist a valid run_state.json and only then
    raise (the ``generate_full_reports=True`` path fails after
    ``run_daily_compact()`` has written the run), and a marker that
    unconditionally means "failed before writing any artifact" makes the
    report fabricate an earlier failed start plus a successful retry --
    inflating recovery/error counts and forcing the high error band on a run
    that completed its work. The probe's answer is recorded as the marker's
    ``failure_phase``; it fails closed to a plain failed start if the probe
    itself raises -- codex review discussion_r3659298031.

    Each of the three failure-path actions below (Slack alert, heartbeat,
    marker) runs in its OWN try/except: alert_failure raising (e.g. malformed
    webhook URL) must not prevent the heartbeat or the marker from still
    being attempted, since the marker especially is the only durable evidence
    this failure ever happened -- codex review discussion_r3658373974.
    """
    ensure_log_handler()
    if not enabled:
        return fn(*args, **kwargs)
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        logger.exception("live %s run failed", stream)
        try:
            alert_failure(stream, exc, context=context)
        except Exception:  # noqa: BLE001 - alerting bugs must not mask the run failure
            logger.exception("alert_failure raised while handling a %s run failure", stream)
        try:
            send_heartbeat(stream, ok=False)
        except Exception:  # noqa: BLE001 - alerting bugs must not mask the run failure
            logger.exception("send_heartbeat raised while handling a %s run failure", stream)
        try:
            # Probe whether the run already persisted its own artifact, so
            # the marker records the failure PHASE rather than unconditionally
            # claiming a failed start for an exception raised long after the
            # run wrote its run_state.json -- codex review
            # discussion_r3659298031. Fails CLOSED to False (a plain failed
            # start) if the probe itself raises: that is the pre-existing
            # behavior and never loses the failure record.
            artifact_written = False
            if run_artifact_written is not None:
                try:
                    artifact_written = bool(run_artifact_written())
                except Exception:  # noqa: BLE001 - a broken probe must not lose the marker
                    logger.exception("run_artifact_written probe raised for %s; recording a failed start", stream)
            write_failed_start_marker(
                stream, exc, root=attempt_log_root, run_artifact_written=artifact_written
            )
        except Exception:  # noqa: BLE001 - alerting bugs must not mask the run failure
            logger.exception("write_failed_start_marker raised while handling a %s run failure", stream)
        raise
    ok = True
    if result_ok is not None:
        try:
            ok = bool(result_ok(result))
        except Exception:  # noqa: BLE001 - fail closed: a broken predicate must not report healthy
            logger.exception("result_ok predicate raised for %s; pinging failed heartbeat", stream)
            ok = False
    try:
        if not ok:
            logger.warning(
                "%s run returned a structured failure result; pinging failed heartbeat", stream
            )
        send_heartbeat(stream, ok=ok)
    except Exception:  # noqa: BLE001 - see above
        logger.exception("heartbeat raised for %s", stream)
    return result
