"""Route table, query validation and response redaction for the console server.

Phase 0 of the GUI expansion moved the GET bodies that used to sit inline in
``dca_console.make_server``'s ``do_GET`` into the ``GET_ROUTES`` table below.
The handler still runs the unchanged gate order first (Host/Origin/Fetch
Metadata trust check, then HTTP Basic, then the CSRF token for writes) and only
then dispatches here, so every route added to the table inherits the hardened
prefix without restating it. Three rules apply to every entry:

- A route receives a parsed query through :func:`query`, which is closed over
  an explicit key set. The legacy routes keep their lenient reading (unknown
  keys ignored) because tests pin that behaviour; every new route is strict.
- A route returns a :class:`Response`; JSON responses pass through
  :func:`walk_redact` so no registered secret or account-shaped literal can
  leave the process inside a structured body, however deep it was nested.
- A route never receives a browser-supplied path. Ids are enums, ISO dates, or
  opaque tokens the server minted itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from urllib.parse import SplitResult, parse_qs

from dca_console_jobs import redact_value

DEFAULT_CSP = "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
# Generated research pages (the 3D market graph, the Octopus inspector) are
# self-contained HTML files with inline scripts, which DEFAULT_CSP forbids.
# They are served from one dedicated route inside an opaque-origin sandbox that
# can run its own inline code but can never reach this console's origin, its
# cookies, or the network. Every other route keeps DEFAULT_CSP; a test pins
# that the main routes' policy never loosens (GUI expansion, Phase 4).
SANDBOX_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
)
CREDENTIAL_FREE_ROUTES = frozenset({"/api/health"})


@dataclass(frozen=True)
class RouteRequest:
    """What a route may see of one request: the split URL and diagnostics."""

    url: SplitResult
    # Already redacted and bounded by the handler; reported back on /api/health
    # and 403 bodies so an operator can decide --behind-proxy from evidence.
    authority: dict[str, str | None]


@dataclass(frozen=True)
class Response:
    code: int
    body: bytes
    kind: str = "application/json"
    download: str = ""
    csp: str = DEFAULT_CSP

    @classmethod
    def json(cls, code: int, value: Any, *, redacted: bool = True, indent: int | None = None,
             download: str = "") -> Response:
        payload = walk_redact(value) if redacted else value
        return cls(code, json.dumps(payload, indent=indent, allow_nan=False).encode(), download=download)


def query(raw: str, allowed: tuple[str, ...], *, max_fields: int, strict: bool = True) -> dict[str, str]:
    """Parse one query string against a closed key set.

    ``parse_qs`` raises ValueError past ``max_fields``, which the handler
    already turns into a 400. Strict mode additionally rejects any key outside
    ``allowed`` so a new route cannot quietly accept an undocumented parameter;
    the legacy snapshot/export routes stay lenient because their tests pin the
    ignore-unknown behaviour. Blank values are dropped exactly as before, so
    ``date=`` still reads as "no date supplied".
    """
    fields = parse_qs(raw, max_num_fields=max_fields)
    if strict:
        unknown = sorted(set(fields) - set(allowed))
        if unknown:
            raise ValueError(f"Unknown query field: {unknown[0]}")
    return {key: values[0] for key, values in fields.items() if key in allowed and values}


def walk_redact(value: Any) -> Any:
    """Apply the structured-safe redaction to every string in a JSON-shaped value."""
    if isinstance(value, str):
        return redact_value(value)
    if isinstance(value, dict):
        return {key: walk_redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [walk_redact(item) for item in value]
    return value


def _route_bootstrap(app, request: RouteRequest) -> Response:
    # The session token IS the value here, so the redaction pass must not see
    # it: a labelled-secret scan would erase the one field the page needs.
    return Response.json(200, {"token": app.token, "default_date": app.default_day().isoformat()}, redacted=False)


def _snapshot_arguments(request: RouteRequest) -> tuple[date | None, str, str]:
    fields = query(request.url.query, ("date", "ticker", "period"), max_fields=8, strict=False)
    raw_day = fields.get("date")
    # Let snapshot() resolve an absent date once for both its evidence day and
    # default_date. Besides keeping the rolling rule in one place, this avoids
    # a midnight split inside one response (Same Page round 2).
    day = date.fromisoformat(raw_day) if raw_day is not None else None
    return day, fields.get("ticker", "")[:32], fields.get("period", "all")[:80]


def _route_snapshot(app, request: RouteRequest) -> Response:
    day, ticker, period = _snapshot_arguments(request)
    return Response.json(200, app.snapshot(day, ticker, period))


def _route_export(app, request: RouteRequest) -> Response:
    day, ticker, period = _snapshot_arguments(request)
    data = app.snapshot(day, ticker, period)
    export = {key: data[key] for key in ("mode", "now", "daily", "octopus", "warnings")}
    return Response.json(200, export, indent=2, download=f"dca-summary-{data['daily']['date']}.json")


def _route_health(app, request: RouteRequest) -> Response:
    return Response.json(200, {"status": "ok", "mode": app.settings.mode, **request.authority})


def _route_docs(app, request: RouteRequest) -> Response:
    from dca_console import REPO, module_catalog

    fields = query(request.url.query, ("path",), max_fields=1, strict=False)
    name = fields.get("path", "")
    allowed = {row["docs"] for row in module_catalog(app.settings.python)}
    path = (REPO / name).resolve()
    if name not in allowed or not path.is_relative_to(REPO) or not path.is_file():
        return Response.json(404, {"error": "Documentation not found"})
    return Response(200, path.read_bytes(), "text/plain")


GET_ROUTES: dict[str, Callable[[Any, RouteRequest], Response]] = {
    "/api/bootstrap": _route_bootstrap,
    "/api/snapshot": _route_snapshot,
    "/api/export": _route_export,
    "/api/health": _route_health,
    "/api/docs": _route_docs,
}
