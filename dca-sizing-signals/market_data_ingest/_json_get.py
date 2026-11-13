"""Stdlib GET-and-parse for the two keyless JSON sources (SEC EDGAR, DoltHub).

Kept apart from ``prices/http.py`` on purpose: that seam imports ``requests`` and carries
vendor-credential rules, while EDGAR and DoltHub are plain unauthenticated GET/JSON, so the
test files that specified these modules asked for stdlib ``urllib.request`` rather than a
third-party HTTP client (tests/test_edgar_fundamentals.py module docstring).

Fail-closed rules:

* Every request carries an explicit timeout; a timeout or connection error propagates as
  ``OSError`` (``urllib.error.URLError`` is a subclass) and is not retried.
* A 404 raises ``LookupError`` so a caller can choose to treat "this filer / symbol has no
  record" as an empty result. Every other HTTP error propagates unchanged.
* Bodies are parsed strictly: duplicate keys, NaN/Infinity and a non-object top level are
  rejected with ``ValueError``. No response body is copied into an exception message
  (.jules/sentinel.md: never log or echo external responses).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

# (seconds) one companyfacts document for a large filer is a few MB.
DEFAULT_TIMEOUT_S = 30.0


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def parse_strict_json_object(text: str, *, source: str) -> dict[str, Any]:
    """Parse ``text`` as one JSON object, or raise ``ValueError`` naming ``source`` only."""
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError subclass
        raise ValueError(f"{source} response rejected by strict JSON parsing") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{source} response is not a JSON object")
    return value


def get_json_object(
    url: str,
    *,
    source: str,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """GET ``url`` and return its body as a strictly parsed JSON object.

    Raises:
        LookupError: the server answered 404.
        urllib.error.HTTPError: any other non-2xx status.
        OSError: timeout or connection failure.
        ValueError: the body is not one strict JSON object.
    """
    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 -- https URLs built here
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError(f"{source} returned 404") from exc
        raise
    return parse_strict_json_object(body, source=source)
