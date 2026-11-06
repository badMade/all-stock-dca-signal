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
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from urllib.parse import SplitResult, parse_qs

from dca_console_jobs import redact_value
from dca_console_release import CONSOLE_VERSION

JOB_ID = re.compile(r"[a-zA-Z0-9-]{1,100}")

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
    # The listening port, for the Environment page's access-policy panel.
    port: int | None = None


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
    return Response.json(200, {"status": "ok", "mode": app.settings.mode, "channel": getattr(app.settings, "channel", "stable"),
                                "version": CONSOLE_VERSION, **request.authority})


def _catalog_refs(app) -> set[str]:
    from dca_console import module_catalog

    return {row["docs"] for row in module_catalog(app.settings.python) if isinstance(row.get("docs"), str)}


def _route_docs(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_docs import read_doc

    fields = query(request.url.query, ("path",), max_fields=1, strict=False)
    name = fields.get("path", "")
    # One closed allowlist: the docs catalog's bounded listings, which a test
    # proves cover every program-directory doc_ref. read_doc re-checks
    # membership and confinement itself.
    data = read_doc(REPO, name) if name else None
    if data is None:
        return Response.json(404, {"error": "Documentation not found"})
    return Response(200, redact_value(data.decode("utf-8", errors="replace")).encode(), "text/plain")


def _route_docs_index(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_docs import docs_index

    query(request.url.query, (), max_fields=1)
    return Response.json(200, docs_index(REPO, _catalog_refs(app)))


def _route_jobs_log(app, request: RouteRequest) -> Response:
    """Delta log polling for one job: strict query, opaque id, integer offset."""
    fields = query(request.url.query, ("job", "offset"), max_fields=2)
    job = fields.get("job", "")
    if JOB_ID.fullmatch(job) is None:
        raise ValueError("job must be a console job id")
    offset = int(fields.get("offset", "0"))
    if offset < 0:
        raise ValueError("offset must not be negative")
    delta = app.jobs.log_since(job, offset)
    if delta is None:
        return Response.json(404, {"error": "Not found"})
    return Response.json(200, delta)


def _route_env(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_env import build_env_snapshot

    query(request.url.query, (), max_fields=1)
    return Response.json(200, build_env_snapshot(app, REPO, port=request.port))


def _route_artifact(app, request: RouteRequest) -> Response:
    """One manifest-listed launcher artifact; HTML pages get the sandbox policy."""
    from dca_console_launch import ARTIFACT_NAME, artifact_bytes

    fields = query(request.url.query, ("job", "name"), max_fields=2)
    job, name = fields.get("job", ""), fields.get("name", "")
    if JOB_ID.fullmatch(job) is None or ARTIFACT_NAME.fullmatch(name) is None:
        raise ValueError("job and name must name a launcher artifact")
    found = artifact_bytes(app, job, name)
    if found is None:
        return Response.json(404, {"error": "Not found"})
    data, kind = found
    if kind == "html":
        # Generated research pages carry inline scripts; they run only inside
        # the opaque sandbox and never on this console's origin.
        return Response(200, data, "text/html", csp=SANDBOX_CSP)
    if kind == "json":
        try:
            return Response.json(200, json.loads(data.decode("utf-8", errors="replace")), indent=2)
        except ValueError:
            return Response.json(404, {"error": "Not found"})
    return Response(200, redact_value(data.decode("utf-8", errors="replace")).encode(), "text/plain")


def _route_launch(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_launch import launch_bench

    query(request.url.query, (), max_fields=1)
    return Response.json(200, launch_bench(app, REPO))


# --- Reader pages (GUI expansion Phases 1-3) ----------------------------------------
#
# Every route below is a thin adapter: parse a closed query, resolve the roots
# and the day from the console's own settings, call one reader builder, and let
# Response.json redact the result. The demo archive under <output>/demo is
# excluded exactly as ConsoleApp.snapshot excludes it, and the same tuple is
# forwarded to the detail route so a run the index hides is never readable by id.

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
RUN_ID = re.compile(r"run-[0-9a-f]{12}")
POLICY_KEY = re.compile(r"[A-Za-z0-9_$]{1,120}")
LEDGER_KINDS = ("cumulative", "whole_share", "inverse_martingale")


def _roots(app):
    from dca_console_reader import roots_from_settings

    return roots_from_settings(app.settings)


def _excluded_roots(app) -> tuple:
    return (app.settings.output / "demo",) if app.settings.mode == "local" else ()


def _now(app):
    from datetime import datetime

    from dca_console import ET

    return datetime.now(ET)


def _day_field(app, fields: dict[str, str], name: str = "date") -> date:
    raw = fields.get(name)
    return date.fromisoformat(raw) if raw else app.default_day()


def _route_runs_tree(app, request: RouteRequest) -> Response:
    from dca_console_runs import artifact_tree_map

    query(request.url.query, (), max_fields=1)
    return Response.json(200, artifact_tree_map(_roots(app)))


def _route_runs(app, request: RouteRequest) -> Response:
    from dca_console_runs import build_run_index

    fields = query(request.url.query, ("date", "month"), max_fields=2)
    if "date" in fields and "month" in fields:
        raise ValueError("Supply date or month, not both")
    month = fields.get("month")
    if month is not None and MONTH.fullmatch(month) is None:
        raise ValueError("month must be YYYY-MM")
    day = date.fromisoformat(fields["date"]) if "date" in fields else None
    return Response.json(200, build_run_index(_roots(app), day=day, month=month, excluded_roots=_excluded_roots(app)))


def _route_runs_detail(app, request: RouteRequest) -> Response:
    from dca_console_runs import build_run_detail

    fields = query(request.url.query, ("run",), max_fields=1)
    run_id = fields.get("run", "")
    if RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run must be a run id")
    detail = build_run_detail(_roots(app), run_id, config_dir=app.settings.config_dir, excluded_roots=_excluded_roots(app))
    if not detail.get("found"):
        return Response.json(404, {"error": "Not found", "warnings": detail.get("warnings", [])})
    return Response.json(200, detail)


def _route_runs_pointers(app, request: RouteRequest) -> Response:
    from dca_console_runs import pointer_strip

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, pointer_strip(_roots(app), _day_field(app, fields), config_dir=app.settings.config_dir))


def _route_schedule(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_runs import pointer_strip
    from dca_console_schedule import build_schedule_layers, observed_from_pointer_strip

    fields = query(request.url.query, ("date",), max_fields=1)
    day = _day_field(app, fields)
    roots = _roots(app)
    observed = observed_from_pointer_strip(pointer_strip(roots, day, config_dir=app.settings.config_dir))
    return Response.json(200, build_schedule_layers(app.settings.config_dir, REPO, roots, day, _now(app), observed=observed))


def _route_schedule_policy(app, request: RouteRequest) -> Response:
    from dca_console_schedule import policy_block

    fields = query(request.url.query, ("key",), max_fields=1)
    key = fields.get("key", "")
    if POLICY_KEY.fullmatch(key) is None:
        raise ValueError("key must name a manifest policy block")
    block = policy_block(app.settings.config_dir, key)
    if not block.get("found"):
        return Response.json(404, {"error": "Policy block not found"})
    return Response.json(200, block)


def _route_schedule_tasks(app, request: RouteRequest) -> Response:
    from dca_console_schedule import manifest_tasks, roster_identity, slot_evidence

    fields = query(request.url.query, ("date",), max_fields=1)
    day, now = _day_field(app, fields), _now(app)
    # Same per-slot evidence join as /api/schedule, so the two never disagree
    # about whether an elapsed live row is verified.
    owners = {slot["slot_id"]: slot["cron_id"] for slot in roster_identity(app.settings.config_dir)["slots"]
              if isinstance(slot.get("cron_id"), str)}
    return Response.json(200, manifest_tasks(app.settings.config_dir, day, now,
                                             evidence=slot_evidence(_roots(app), day, now), slot_owners=owners))


def _route_state(app, request: RouteRequest) -> Response:
    from dca_console_state import build_state_snapshot

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, build_state_snapshot(_roots(app), _day_field(app, fields), config_dir=app.settings.config_dir))


def _route_state_ledger(app, request: RouteRequest) -> Response:
    from dca_console_state import ledger_view

    fields = query(request.url.query, ("kind", "ticker", "date"), max_fields=3)
    kind = fields.get("kind", "")
    if kind not in LEDGER_KINDS:
        raise ValueError("kind must be cumulative, whole_share or inverse_martingale")
    # The builder validates the symbol and the day itself before either is
    # used as a path component; a ValueError from it becomes a 400 here.
    return Response.json(200, ledger_view(_roots(app), kind, ticker=fields.get("ticker"), day=fields.get("date")))


def _route_state_purchases(app, request: RouteRequest) -> Response:
    from dca_console_state import purchases_view

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, purchases_view(_roots(app), day=fields.get("date")))


def _route_state_risk(app, request: RouteRequest) -> Response:
    from dca_console_state import risk_view

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, risk_view(_roots(app), _day_field(app, fields), config_dir=app.settings.config_dir))


def _route_state_maintenance(app, request: RouteRequest) -> Response:
    from dca_console_state import maintenance_events

    fields = query(request.url.query, ("days",), max_fields=1)
    days = int(fields.get("days", "30"))
    if not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366")
    return Response.json(200, maintenance_events(_roots(app), days=days))


def _route_state_strategy(app, request: RouteRequest) -> Response:
    from dca_console_state import strategy_state_view

    query(request.url.query, (), max_fields=1)
    return Response.json(200, strategy_state_view(roots=_roots(app)))


def _route_config(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_config import build_config_snapshot

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, build_config_snapshot(app.settings.config_dir, REPO, _now(app), _day_field(app, fields)))


def _route_config_file(app, request: RouteRequest) -> Response:
    from dca_console_config import STEMS, config_file_view

    fields = query(request.url.query, ("stem",), max_fields=1)
    stem = fields.get("stem", "")
    if stem not in STEMS:
        return Response.json(404, {"error": "Not found"})
    return Response.json(200, config_file_view(app.settings.config_dir, stem))


def _route_config_effective(app, request: RouteRequest) -> Response:
    from dca_console_config import effective_config

    query(request.url.query, (), max_fields=1)
    return Response.json(200, effective_config(app.settings.config_dir, _now(app)))


def _route_config_ladder(app, request: RouteRequest) -> Response:
    from dca_console_config import ladder_preview

    query(request.url.query, (), max_fields=1)
    return Response.json(200, ladder_preview(app.settings.config_dir))


def _route_config_ladder_point(app, request: RouteRequest) -> Response:
    import math

    from dca_console_config import ladder_point

    fields = query(request.url.query, ("drop", "ticker"), max_fields=2)
    try:
        drop = float(fields.get("drop", ""))
    except (ValueError, TypeError, OverflowError):
        drop = math.nan
    if not math.isfinite(drop) or not 0 <= drop <= 50:
        raise ValueError("drop must be a percentage between 0 and 50")
    return Response.json(200, ladder_point(app.settings.config_dir, drop, fields.get("ticker", "")[:16]))


def _route_today(app, request: RouteRequest) -> Response:
    """The Today page's truth strip: live gates, window countdown, same-day pointers."""
    from dca_console_config import live_gate_summary, window_countdown
    from dca_console_runs import pointer_strip

    fields = query(request.url.query, ("date",), max_fields=1)
    day = _day_field(app, fields)
    return Response.json(200, {
        "date": day.isoformat(),
        "gates": live_gate_summary(app.settings.config_dir, day),
        "window": window_countdown(app.settings.config_dir, day),
        "pointers": pointer_strip(_roots(app), day, config_dir=app.settings.config_dir),
    })


def _alert_window(app, fields: dict[str, str]) -> tuple[date, date]:
    from datetime import timedelta

    end = date.fromisoformat(fields["to"]) if "to" in fields else app.default_day()
    start = date.fromisoformat(fields["from"]) if "from" in fields else end - timedelta(days=30)
    return start, end


def _route_alerts(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_alerts import build_alerts_snapshot

    fields = query(request.url.query, ("from", "to"), max_fields=2)
    start, end = _alert_window(app, fields)
    return Response.json(200, build_alerts_snapshot(_roots(app), start, end, config_dir=app.settings.config_dir,
                                                    repo=REPO, observed_days=None, now=_now(app)))


def _route_alerts_inbox(app, request: RouteRequest) -> Response:
    from dca_console_alerts import alert_inbox

    fields = query(request.url.query, ("from", "to"), max_fields=2)
    start, end = _alert_window(app, fields)
    return Response.json(200, alert_inbox(_roots(app), start, end, config_dir=app.settings.config_dir))


def _route_alerts_usage(app, request: RouteRequest) -> Response:
    from dca_console_alerts import model_usage_view

    fields = query(request.url.query, ("month",), max_fields=1)
    month = fields.get("month") or _now(app).strftime("%Y-%m")
    if MONTH.fullmatch(month) is None:
        raise ValueError("month must be YYYY-MM")
    return Response.json(200, model_usage_view(_roots(app), month))


def _route_alerts_drift(app, request: RouteRequest) -> Response:
    """Drift for every run the index lists on one day; ids map to paths server-side."""
    from dca_console_alerts import drift_summary
    from dca_console_runs import build_run_index

    fields = query(request.url.query, ("date",), max_fields=1)
    day = _day_field(app, fields)
    roots = {root.label: root for root in _roots(app)}
    index = build_run_index(_roots(app), day=day, excluded_roots=_excluded_roots(app))
    targets = []
    for row in index.get("runs", []):
        root = roots.get(row.get("root_label"))
        if root is None or row.get("incomplete") or not isinstance(row.get("relative_dir"), str):
            continue
        targets.append((row["id"], root.path / row["relative_dir"]))
    return Response.json(200, {"date": day.isoformat(), **drift_summary(targets[:50])})


def _route_reports(app, request: RouteRequest) -> Response:
    from dca_console_reports import build_reports_snapshot

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, build_reports_snapshot(_roots(app), app.settings.output, app.settings.mode, _day_field(app, fields)))


def _route_reports_weekly(app, request: RouteRequest) -> Response:
    from dca_console_reports import weekly_dashboard

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, weekly_dashboard(_roots(app), _day_field(app, fields)))


def _route_reports_cost(app, request: RouteRequest) -> Response:
    from dca_console_reports import cost_tracker

    fields = query(request.url.query, ("month",), max_fields=1)
    month = fields.get("month") or app.default_day().strftime("%Y-%m")
    if MONTH.fullmatch(month) is None:
        raise ValueError("month must be YYYY-MM")
    return Response.json(200, cost_tracker(_roots(app), int(month[:4]), int(month[5:])))


def _route_reports_latest(app, request: RouteRequest) -> Response:
    from dca_console_reports import latest_summary

    query(request.url.query, (), max_fields=1)
    return Response.json(200, latest_summary(_roots(app)))


def _route_reports_summary(app, request: RouteRequest) -> Response:
    from dca_console_reports import console_digest

    fields = query(request.url.query, ("date",), max_fields=1)
    return Response.json(200, console_digest(app.settings.output, app.settings.mode, _day_field(app, fields)))


def _route_architecture(app, request: RouteRequest) -> Response:
    """Developer-only architecture map shell (static ast read, never shipped).

    Served under the strict default CSP like every main route: the script and
    stylesheet are separate same-origin routes, so nothing here needs the
    sandbox policy that only generated research pages use.
    """
    from dca_console_diagnostics import render_page

    query(request.url.query, (), max_fields=1)
    return Response(200, render_page(), "text/html")


def _route_architecture_asset(kind: str) -> Callable[[Any, RouteRequest], Response]:
    def route(app, request: RouteRequest) -> Response:
        from dca_console_diagnostics import PAGE_CSS, PAGE_JS

        query(request.url.query, (), max_fields=1)
        if kind == "css":
            return Response(200, PAGE_CSS.encode(), "text/css")
        return Response(200, PAGE_JS.encode(), "text/javascript")
    return route


def _route_architecture_data(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_diagnostics import SCAN_CACHE

    fields = query(request.url.query, ("refresh",), max_fields=1)
    return Response.json(200, SCAN_CACHE.get(REPO, refresh=fields.get("refresh") == "1"))


def _route_architecture_module(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_diagnostics import build_detail

    fields = query(request.url.query, ("module",), max_fields=1)
    if re.fullmatch(r"[A-Za-z0-9_.]{1,100}", fields.get("module", "")) is None:
        raise ValueError("module must be a scanned module name")
    detail = build_detail(REPO, fields["module"])
    if detail is None:
        return Response.json(404, {"error": "Not found"})
    return Response.json(200, detail)


def _route_research(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_research import build_research_snapshot

    query(request.url.query, (), max_fields=1)
    return Response.json(200, build_research_snapshot(app, REPO))


def _route_rank(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_rank import build_rank_snapshot

    query(request.url.query, (), max_fields=1)
    return Response.json(200, build_rank_snapshot(REPO))


def _route_tickers(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_tickers import build_tickers_snapshot

    query(request.url.query, (), max_fields=1)
    return Response.json(200, build_tickers_snapshot(REPO, app.settings.config_dir))


def _route_training(app, request: RouteRequest) -> Response:
    from dca_console import REPO
    from dca_console_training import build_training_snapshot

    query(request.url.query, (), max_fields=1)
    return Response.json(200, build_training_snapshot(app, REPO))


GET_ROUTES: dict[str, Callable[[Any, RouteRequest], Response]] = {
    "/api/bootstrap": _route_bootstrap,
    "/api/snapshot": _route_snapshot,
    "/api/export": _route_export,
    "/api/health": _route_health,
    "/api/docs": _route_docs,
    "/api/docs/index": _route_docs_index,
    "/api/jobs/log": _route_jobs_log,
    "/api/env": _route_env,
    "/api/launch": _route_launch,
    "/api/artifact": _route_artifact,
    "/api/today": _route_today,
    "/api/runs": _route_runs,
    "/api/runs/tree": _route_runs_tree,
    "/api/runs/detail": _route_runs_detail,
    "/api/runs/pointers": _route_runs_pointers,
    "/api/schedule": _route_schedule,
    "/api/schedule/policy": _route_schedule_policy,
    "/api/schedule/tasks": _route_schedule_tasks,
    "/api/state": _route_state,
    "/api/state/ledger": _route_state_ledger,
    "/api/state/purchases": _route_state_purchases,
    "/api/state/risk": _route_state_risk,
    "/api/state/maintenance": _route_state_maintenance,
    "/api/state/strategy": _route_state_strategy,
    "/api/config": _route_config,
    "/api/config/file": _route_config_file,
    "/api/config/effective": _route_config_effective,
    "/api/config/ladder": _route_config_ladder,
    "/api/config/ladder/point": _route_config_ladder_point,
    "/api/alerts": _route_alerts,
    "/api/alerts/inbox": _route_alerts_inbox,
    "/api/alerts/usage": _route_alerts_usage,
    "/api/alerts/drift": _route_alerts_drift,
    "/api/reports": _route_reports,
    "/api/reports/weekly": _route_reports_weekly,
    "/api/reports/cost": _route_reports_cost,
    "/api/reports/latest": _route_reports_latest,
    "/api/reports/summary": _route_reports_summary,
    "/api/research": _route_research,
    "/diagnostics/architecture": _route_architecture,
    "/diagnostics/architecture.css": _route_architecture_asset("css"),
    "/diagnostics/architecture.js": _route_architecture_asset("js"),
    "/api/diagnostics/architecture": _route_architecture_data,
    "/api/diagnostics/architecture/module": _route_architecture_module,
    "/api/rank": _route_rank,
    "/api/tickers": _route_tickers,
    "/api/training": _route_training,
}
