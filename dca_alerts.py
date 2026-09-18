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

import json
import logging
import os
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from dca_fileio import atomic_write_text
from dca_net_utils import (
    BlockedFileHandler,
    BlockedFTPHandler,
    PinnedHTTPHandler,
    PinnedHTTPSHandler,
)
from dca_net_utils import (
    UnsafeDestinationError as _UnsafeDestinationError,
)
from dca_net_utils import (
    assert_safe_destination as _assert_safe_destination,
)
from dca_net_utils import (
    redact_url as _redact_url,
)
from dca_net_utils import (
    sanitize_exception as _sanitize_exception,
)
from dca_paths import stream_artifact_dir, stream_root
from scripts.idempotency import STREAM_SLOT_IDS

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


def attempt_log_root_sibling_to(model_usage_log_root: Optional[str | Path]) -> Optional[str]:
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
    configured attempt_log location (Codex review: "Respect the
    DCA_ATTEMPT_LOG_ROOT override on derived siblings").
    """
    if os.environ.get(ATTEMPT_LOG_ROOT_ENV):
        return None
    if not model_usage_log_root:
        return None
    return str(Path(model_usage_log_root).resolve().parent / "attempt_log")


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
        class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(_NoRedirectHandler, PinnedHTTPHandler, PinnedHTTPSHandler, BlockedFileHandler, BlockedFTPHandler)
        with opener.open(url, timeout=timeout) as response:  # noqa: S310 - operator-supplied URL
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 - delivery failure must not raise
        logger.warning(
            "heartbeat ping for %s failed (%s): %s",
            stream,
            _redact_url(url),
            _sanitize_exception(exc),
        )
        return False


def _redacted(text: str) -> str:
    """Account-number/credential redaction for text that leaves the process.

    The strategy core applies ``near_close_dca._redacted_diagnostic_text`` at
    every site that persists or transmits exception text; this module POSTed
    raw ``str(exc)`` plus a traceback to a third-party webhook and wrote both
    to the attempt_log marker unredacted, and MCP/transport exceptions
    routinely echo the account number and token-bearing URLs (2026-09-07
    diagnostic, S:NET-01). Imported lazily: ``near_close_dca`` is the heavy
    strategy module and this one must stay importable on its own.
    """
    from near_close_dca import _redacted_diagnostic_text

    return _redacted_diagnostic_text(text)


def _log_redacted_exception(message: str, *args: object) -> None:
    """Redact the entire exception chain before handing text to log handlers."""
    diagnostic = message % args if args else message
    logger.error("%s\n%s", _redacted(diagnostic), _redacted(traceback.format_exc()))


def _trading_day_et(now: Optional[datetime] = None) -> str:
    from zoneinfo import ZoneInfo

    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _resolve_alert_slot(stream: str, now: Optional[datetime] = None) -> str:
    """The roster slot token for ``stream``'s Gate 9 alert filename.

    ``scripts/alert_digest.py`` counts an alert toward a per-slot gate streak
    only when the filename's slot field is a roster slot (``0935`` ...
    ``1530``); a stream word there loses the file's authoritative slot. A
    single-slot stream resolves from :data:`scripts.idempotency.STREAM_SLOT_IDS`;
    a slot-qualified namespace such as ``intraday-1330`` names its own slot;
    a multi-slot stream with no qualifier takes the roster slot nearest to
    the wall clock, which is the slot that fired. An unknown stream keeps its
    own name (Copilot review, PR #1394: "Supply parser-compatible slot and
    gate tokens for structured-failure filenames").
    """
    base, _, qualifier = stream.partition("-")
    slots = STREAM_SLOT_IDS.get(base, ())
    if qualifier and qualifier in slots:
        return qualifier
    if len(slots) == 1:
        return slots[0]
    if not slots:
        return stream
    from zoneinfo import ZoneInfo

    moment = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    minute_of_day = moment.hour * 60 + moment.minute
    return min(slots, key=lambda slot: abs(int(slot[:2]) * 60 + int(slot[2:]) - minute_of_day))


def write_alert_file(
    stream: str,
    *,
    slot: str,
    gate: str,
    body: str,
    trading_day: Optional[date] = None,
) -> Optional[Path]:
    """Write the durable Gate 9 alert file ``scripts/alert_digest.py`` reads.

    ``<cron root>/<stream artifact dir>/alerts/<YYYY-MM-DD>-<slot>-<gate>.txt``,
    with the root resolved through ``dca_paths`` exactly like every other
    artifact writer -- never a bare repo-relative ``artifacts/`` directory
    (gitignored; 23 of the 2026-08 alerts were lost that way, see
    ``docs/alert_digest.md``) and never a literal fallback root. Until the
    2026-09-07 diagnostic (T:F08) this channel had no in-code writer at all:
    every executor prompt hand-rolled the path. ``body`` is redacted. Never
    raises; returns the path written, or ``None`` on failure (logged).

    The filename key is ``(day, slot, gate)`` only -- there is no executor or
    session identity in it. A second call for the same tuple (a retry, or two
    executors alerting the same slot/gate the same day, e.g. the 9/15 11:30
    incident) would otherwise silently overwrite the first file's content. On
    a collision this probes ``<day>-<slot>-<gate>.2.txt``, ``.3.txt``, etc.,
    reusing ``scripts/alert_digest.py``'s already-supported optional ``.N``
    suffix on the gate token so no downstream parser needs to change.
    """
    day = trading_day.isoformat() if trading_day is not None else _trading_day_et()
    directory = Path(stream_root(stream_artifact_dir(stream), "alerts"))
    path = directory / f"{day}-{slot}-{gate}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            n = 2
            while (directory / f"{day}-{slot}-{gate}.{n}.txt").exists():
                n += 1
            path = directory / f"{day}-{slot}-{gate}.{n}.txt"
        atomic_write_text(path, _redacted(body).rstrip("\n") + "\n")
    except Exception as exc:  # noqa: BLE001 - an alert-file failure must not mask the run failure
        logger.warning("alert file for %s could not be written (%s): %s", stream, path, _sanitize_exception(exc))
        return None
    return path


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
    trace = _redacted("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    if len(trace) > _TRACEBACK_CHAR_LIMIT:
        trace = "…" + trace[-_TRACEBACK_CHAR_LIMIT:]
    lines = [f":rotating_light: DCA {stream} live run FAILED: {type(exc).__name__}: {_redacted(str(exc))}"]
    if context:
        lines.append(_redacted(context))
    lines.append(f"```{trace}```")
    payload = json.dumps({"text": "\n".join(lines)}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(_NoRedirectHandler, PinnedHTTPHandler, PinnedHTTPSHandler, BlockedFileHandler, BlockedFTPHandler)
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied webhook
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
        trace = _redacted("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if len(trace) > _TRACEBACK_CHAR_LIMIT:
            trace = "…" + trace[-_TRACEBACK_CHAR_LIMIT:]
        marker = {
            "stream": stream,
            "error_class": type(exc).__name__,
            "error_message": _redacted(str(exc)),
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
            _log_redacted_exception("failed-start attempt-history append failed for %s", stream)
        path = day_dir / f"{stream}.json"
        atomic_write_text(path, serialized, encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 - see alert_failure/send_heartbeat above
        _log_redacted_exception("failed-start marker write raised for %s", stream)
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
    ``config=GuardedRunConfig(run_artifact_written=...)``.
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


@dataclass(frozen=True)
class GuardedRunConfig:
    """Configuration for :func:`guarded_run`.

    ``enabled`` toggles alerting/heartbeats (see :func:`guarded_run` for the
    full behavioral contract); the remaining fields correspond 1:1 to
    ``guarded_run``'s former keyword arguments of the same name.
    """

    enabled: bool = True
    context: Optional[str] = None
    result_ok: Optional[Callable[[Any], bool]] = None
    attempt_log_root: Optional[str] = None
    run_artifact_written: Optional[Callable[[], bool]] = None
    # Slot label for the durable alert file written when ``result_ok`` says the
    # returned result is a structured failure (see :func:`write_alert_file`);
    # resolved from the stream's roster slot when unset (the intraday executor
    # passes its own slot). ``alert_gate`` is the file's gate token; Gate 9 is
    # the evidence-and-alerting gate this channel belongs to, and the digest
    # needs a ``gate<N>`` token to file the alert under a gate at all.
    alert_slot: Optional[str] = None
    alert_gate: str = "gate9"


def guarded_run(
    stream: str,
    fn: Callable[[], T],
    config: Optional[GuardedRunConfig] = None,
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
    config = config or GuardedRunConfig()
    ensure_log_handler()
    if not config.enabled:
        return fn()
    try:
        result = fn()
    except Exception as exc:
        _log_redacted_exception("live %s run failed", stream)
        try:
            alert_failure(stream, exc, context=config.context)
        except Exception:  # noqa: BLE001 - alerting errors must not mask the run failure
            _log_redacted_exception("alert_failure raised while handling a %s run failure", stream)
        try:
            send_heartbeat(stream, ok=False)
        except Exception:  # noqa: BLE001 - alerting errors must not mask the run failure
            _log_redacted_exception("send_heartbeat raised while handling a %s run failure", stream)
        try:
            # Probe whether the run already persisted its own artifact, so
            # the marker records the failure PHASE rather than unconditionally
            # claiming a failed start for an exception raised long after the
            # run wrote its run_state.json. Fails CLOSED to False (a plain
            # failed start) if the probe itself raises, ensuring the
            # failure record is never lost.
            artifact_written = False
            if config.run_artifact_written is not None:
                try:
                    artifact_written = config.run_artifact_written()
                except Exception:  # noqa: BLE001 - a broken probe must not lose the marker
                    _log_redacted_exception("run_artifact_written probe raised for %s; recording a failed start", stream)
            write_failed_start_marker(
                stream, exc, root=config.attempt_log_root, run_artifact_written=artifact_written
            )
        except Exception:  # noqa: BLE001 - alerting errors must not mask the run failure
            _log_redacted_exception("write_failed_start_marker raised while handling a %s run failure", stream)
        raise
    ok = True
    if config.result_ok is not None:
        try:
            ok = config.result_ok(result)
        except Exception:  # noqa: BLE001 - fail closed: a broken predicate must not report healthy
            _log_redacted_exception("result_ok predicate raised for %s; pinging failed heartbeat", stream)
            ok = False
    if not ok:
        # The one channel that needs no configuration: Slack and heartbeats
        # are env-gated off on a default deployment and the failed-start
        # marker is written only for an ESCAPING exception, so a class-based
        # stream that failed closed via a structured result was silent
        # everywhere in code (2026-09-07 diagnostic, T:F08).
        write_alert_file(
            stream,
            slot=config.alert_slot or _resolve_alert_slot(stream),
            gate=config.alert_gate,
            body=f"{stream} run returned a structured failure result\n{repr(result)[:4000]}",
        )
    try:
        if not ok:
            logger.warning(
                "%s run returned a structured failure result; pinging failed heartbeat", stream
            )
        send_heartbeat(stream, ok=ok)
    except Exception:  # noqa: BLE001 - see above
        _log_redacted_exception("heartbeat raised for %s", stream)
    return result
