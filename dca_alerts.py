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
import urllib.request
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import urlsplit

logger = logging.getLogger("near_close_dca.alerts")

HEARTBEAT_URL_ENV_PREFIX = "DCA_HEARTBEAT_URL"
FAILURE_WEBHOOK_ENV = "DCA_ALERT_SLACK_WEBHOOK"
# Same env var scripts/drift_detection.py uses (DEFAULT_SLACK_WEBHOOK_ENV);
# duplicated as a literal so this module stays free of project imports and is
# usable from any module without cycles.
FALLBACK_WEBHOOK_ENV = "DCA_DRIFT_SLACK_WEBHOOK"

_DEFAULT_TIMEOUT_SECONDS = 10.0
_TRACEBACK_CHAR_LIMIT = 1500

T = TypeVar("T")


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
    scheme = urlsplit(url).scheme.lower()
    if scheme not in {"http", "https"}:
        logger.warning("ignoring %s URL with unsupported scheme %r", purpose, scheme)
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
        logger.warning("heartbeat ping for %s failed (%s): %s", stream, url, exc)
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
        logger.warning("failure alert for %s could not be delivered: %s", stream, delivery_exc)
        return False


def guarded_run(
    stream: str,
    fn: Callable[..., T],
    *args: Any,
    enabled: bool = True,
    context: Optional[str] = None,
    result_ok: Optional[Callable[[T], bool]] = None,
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
            send_heartbeat(stream, ok=False)
        except Exception:  # noqa: BLE001 - alerting bugs must not mask the run failure
            logger.exception("alert delivery raised while handling a %s run failure", stream)
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
