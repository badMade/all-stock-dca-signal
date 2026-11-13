"""``PredictionMarketSource`` (D2), the shared HTTP seam, and the fail-closed ``run_ingestion`` (D6, D7).

HTTP rules, shared by every adapter:

* Every request carries an explicit timeout and goes through the per-endpoint budget first.
* Non-200 raises ``FetchError("http_<status>")``; a timeout ``"timeout"``; a connection error
  ``"connection"``. Nothing is retried: a failed run is logged and the next scheduled run tries
  again, so a retry storm cannot eat a venue's rate limit.
* Bodies are parsed from ``.text`` strictly -- duplicate keys, NaN/Infinity, truncation and a
  non-object top level all raise ``FetchError("parse")``.
* No response body is ever copied into an error, a log line or ``ingestion_runs``
  (.jules/sentinel.md).

``run_ingestion`` is the only way fetched rows reach the store. It never raises: every outcome
is one ``ingestion_runs`` row and a ``RunResult``.
"""

from __future__ import annotations

import abc
import json
import logging
import math
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import requests

from .errors import CredentialsError, FetchError
from .models import IngestBatch, RunResult
from .throttle import RequestBudget

logger = logging.getLogger(__name__)

# (connect, read) seconds. The largest body here, a Gamma event with ~100 markets, is ~400 KB.
DEFAULT_TIMEOUT = (10.0, 30.0)


def to_float(value: Any) -> float | None:
    """Venue numerics arrive as numbers or numeric strings; anything else becomes None.

    bool is rejected explicitly (``float(True)`` is 1.0), as are NaN and infinities, so a bad
    value can never turn into a plausible price or liquidity figure.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return out if math.isfinite(out) else None


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 (``Z`` or offset; nanosecond fractions truncated) or epoch seconds -> aware UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = _FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_json_list(value: Any) -> list[Any]:
    """Gamma double-encodes arrays (``outcomes``, ``outcomePrices``, ``clobTokenIds``) as JSON strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        decoded = _strict_loads(value)
        if isinstance(decoded, list):
            return decoded
    if value in (None, ""):
        return []
    raise FetchError("parse", "expected a JSON array")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise FetchError("parse", "duplicate JSON key")
        out[key] = value
    return out


def _reject_constant(_name: str) -> Any:
    raise FetchError("parse", "non-standard JSON constant")


def _strict_loads(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise FetchError("parse", "invalid or truncated JSON") from exc


class PredictionMarketSource(abc.ABC):
    """One adapter per venue (D2). A later ``ForecastProvider`` consumes these, never a live slot.

    Subclasses set ``venue`` and implement the three abstract fetches; each returns an
    ``IngestBatch`` and raises ``FetchError`` on anything it cannot prove complete.
    """

    venue: str = ""

    def __init__(self, session: Any, *, config: Any, clock: Callable[[], datetime], sleep: Callable[[float], None]):
        self._session = session
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._source = config.sources[self.venue]
        self._budget = RequestBudget.from_config(config, self.venue, clock=clock, sleep=sleep)

    def __repr__(self) -> str:
        # Deliberately field-free: nothing that could hold a credential is ever rendered.
        return f"<{type(self).__name__} venue={self.venue}>"

    @abc.abstractmethod
    def fetch_event(self, slug: str) -> IngestBatch: ...

    @abc.abstractmethod
    def fetch_price_history(self, ids: Any, *, interval: str, fidelity: int) -> IngestBatch: ...

    @abc.abstractmethod
    def fetch_orderbook(self, market_id: str) -> IngestBatch: ...

    # --- shared HTTP seam ---------------------------------------------------------------

    def _usable(self, liquidity_usd: float | None) -> bool:
        # Unknown liquidity is never usable: fail closed on the flag, store the row anyway (D4).
        return liquidity_usd is not None and liquidity_usd >= self._config.liquidity_threshold_usd

    def _request(
        self,
        method: str,
        host: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self._source.hosts[host].rstrip("/") + path
        self._budget.acquire_request(host, path)
        try:
            if method == "GET":
                response = self._session.get(url, params=params, headers=headers or {}, timeout=DEFAULT_TIMEOUT)
            else:
                response = self._session.post(
                    url, json=body, params=params, headers=headers or {}, timeout=DEFAULT_TIMEOUT
                )
        except requests.exceptions.Timeout as exc:
            raise FetchError("timeout", f"{host} {path}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise FetchError("connection", f"{host} {path}") from exc
        if response.status_code != 200:
            raise FetchError(f"http_{response.status_code}", f"{host} {path}")
        value = _strict_loads(response.text)
        if not isinstance(value, dict):
            raise FetchError("parse", "top-level JSON is not an object")
        return value


def _classify(exc: BaseException) -> str:
    if isinstance(exc, FetchError):
        return exc.code
    if isinstance(exc, (KeyError, TypeError, ValueError, AttributeError, IndexError)):
        # A body that parsed but does not have the documented shape.
        return "parse"
    return f"unexpected_{type(exc).__name__}"


def run_ingestion(
    store: Any, venue: str, fetch: Callable[[], IngestBatch], *, clock: Callable[[], datetime]
) -> RunResult:
    """Fetch, then persist in one transaction if and only if the source is ``store_ok``.

    Never raises (D6). Writes exactly one ``ingestion_runs`` row:
    ``failed`` (no pm_* rows written), ``not_persisted`` (fetched and parsed, license scope
    is not store_ok, D7), or ``ok``.
    """
    run_id = uuid.uuid4().hex
    started = clock()
    try:
        batch = fetch()
    except Exception as exc:  # noqa: BLE001 - D6: every failure becomes a failed run
        reason = _classify(exc)
        if isinstance(exc, CredentialsError):
            reason = exc.code
        logger.warning("prediction-market fetch failed: venue=%s reason=%s", venue, reason)
        return _record(store, run_id, venue, "failed", reason, 0, started, clock())

    scope = store.config.scope_for(venue)
    if scope != "store_ok":
        return _record(store, run_id, venue, "not_persisted", f"license_scope={scope}", 0, started, clock())
    try:
        written = store.write_batch(venue, batch, run_id=run_id, ingested_at=started)
    except Exception as exc:  # noqa: BLE001 - the write rolled back; report, don't raise
        reason = f"store_{_classify(exc)}"
        logger.warning("prediction-market store write failed: venue=%s reason=%s", venue, reason)
        return _record(store, run_id, venue, "failed", reason, 0, started, clock())
    return _record(store, run_id, venue, "ok", "", written, started, clock())


def _record(store, run_id, venue, status, reason, rows, started, finished) -> RunResult:
    try:
        store.record_run(
            run_id=run_id, venue=venue, status=status, reason=reason, rows_written=rows,
            started_at=started, finished_at=finished,
        )
    except Exception:  # noqa: BLE001 - logging the run must not turn a result into a raise
        logger.exception("could not record prediction-market run %s", run_id)
    return RunResult(run_id=run_id, venue=venue, ok=status == "ok", status=status, reason=reason, rows_written=rows)
