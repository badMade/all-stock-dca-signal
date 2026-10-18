"""The one HTTP seam both vendors share: timeouts, header auth, status mapping, strict JSON.

Fail-closed rules, each pinned by tests/prices/test_http_fail_closed.py:

* Every request carries an explicit timeout. A timeout or connection error is a
  ``ProviderError`` chained to the original exception, and is not retried.
* A 429 is retried exactly once, after ``Retry-After``; a second 429 raises ``rate_limited``.
  401/403 raise ``not_authorized``; other 4xx ``http_4xx``; 5xx ``http_5xx``. None retried.
* Bodies are parsed from ``response.text`` strictly: duplicate keys, NaN/Infinity and the
  wrong top-level type are rejected. A body that ends mid-document raises
  ``IncompleteBatchError`` (``truncated``), so a cut-off batch is never mistaken for a short one.
* Credentials travel in headers only, never in the URL or query, and no response body is
  copied into an exception message (a vendor may echo the key back).
* A client-side limiter spaces calls by ``min_interval_s`` using the injected clock, so the
  Massive free tier's 5 calls/minute is respected and tests stay deterministic.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import requests

from .errors import IncompleteBatchError, ProviderError

logger = logging.getLogger(__name__)

# (connect, read) seconds. A grouped-daily body for the whole US market is a few MB.
DEFAULT_TIMEOUT = (10.0, 60.0)
MAX_RETRY_AFTER_S = 120.0


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def parse_strict_json(text: str, *, vendor: str, expect: type = dict) -> Any:
    """Parse a vendor body, or raise. Never returns a partial or substitute value."""
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        if exc.pos >= len(text.rstrip()):
            raise IncompleteBatchError(f"{vendor} response ended mid-document", code="truncated") from exc
        raise ProviderError(f"{vendor} response is not valid JSON", code="malformed") from exc
    except ValueError as exc:
        raise ProviderError(f"{vendor} response rejected by strict JSON parsing", code="malformed") from exc
    if not isinstance(value, expect):
        raise ProviderError(f"{vendor} response is not a JSON {expect.__name__}", code="malformed")
    return value


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        seconds = float(raw) if raw is not None else 1.0
    except ValueError:
        seconds = 1.0  # an HTTP-date Retry-After; wait the minimum rather than guess
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


class HttpClient:
    """GET-and-parse with the rules above. One instance per vendor credential set."""

    def __init__(
        self,
        session: Any,
        *,
        vendor: str,
        headers: Mapping[str, str],
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        min_interval_s: float,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self._vendor = vendor
        self._headers = dict(headers)
        self._clock = clock
        self._sleep = sleep
        self._min_interval_s = min_interval_s
        self._timeout = timeout
        self._last_call: datetime | None = None

    def __repr__(self) -> str:  # never show headers: they hold the credentials
        return f"HttpClient(vendor={self._vendor!r})"

    def _throttle(self) -> None:
        if self._last_call is None or self._min_interval_s <= 0:
            return
        elapsed = (self._clock() - self._last_call).total_seconds()
        if elapsed < self._min_interval_s:
            self._sleep(self._min_interval_s - elapsed)

    def get_json(self, url: str, params: Mapping[str, Any] | None = None, *, expect: type = dict) -> Any:
        for attempt in (1, 2):
            self._throttle()
            try:
                response = self._session.get(url, params=dict(params or {}), headers=self._headers,
                                             timeout=self._timeout)
            except requests.exceptions.Timeout as exc:
                raise ProviderError(f"{self._vendor} request timed out", code="timeout") from exc
            except requests.exceptions.ConnectionError as exc:
                raise ProviderError(f"{self._vendor} connection failed", code="connection") from exc
            except requests.exceptions.RequestException as exc:
                raise ProviderError(f"{self._vendor} request failed ({type(exc).__name__})", code="transport") from exc
            finally:
                self._last_call = self._clock()

            status = response.status_code
            if status == 429:
                if attempt == 1:
                    wait = _retry_after_seconds(response.headers or {})
                    logger.warning("%s rate limited (HTTP 429); retrying once after %.1fs", self._vendor, wait)
                    self._sleep(wait)
                    self._last_call = self._clock()
                    continue
                raise ProviderError(f"{self._vendor} rate limited twice (HTTP 429)", code="rate_limited")
            if status >= 500:
                raise ProviderError(f"{self._vendor} server error (HTTP {status})", code="http_5xx")
            if status in (401, 403):
                raise ProviderError(f"{self._vendor} refused the request (HTTP {status})", code="not_authorized")
            if status >= 400:
                raise ProviderError(f"{self._vendor} rejected the request (HTTP {status})", code="http_4xx")
            if status != 200:
                raise ProviderError(f"{self._vendor} unexpected HTTP {status}", code="http_unexpected")
            return parse_strict_json(response.text, vendor=self._vendor, expect=expect)
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises
