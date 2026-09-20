"""Reports the console renders in memory from artifacts (GUI expansion Phase 4).

The weekly dashboard, the monthly cost tracker, the latest-run summary and the
Notion payload are all produced today by external agent sessions that also
write them somewhere. This module calls the same builders the sessions call
(``dca_weekly_dashboard``, ``dca_cost_tracker``, ``dca_notion_sync``) and hands
their output to the page without writing anything: no ``write_weekly_dashboard``,
no ``generate_cost_tracker``, no Notion push. Markdown is returned as text and
rendered by the page's text-node markdown renderer; every string still passes
the route's structured redaction.

Two disclaimers travel with the data on purpose: the cost tracker reports
proxy units, never dollars or credits (``dca_cost_tracker.PROXY_DISCLAIMER``),
and the weekly dashboard is scheduled-live only, because
``load_scheduled_run_states`` never traverses ``manual_runs/``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

from dca_console_reader import BoundedReader, RootSpec, iso_date, root_label

MAX_MARKDOWN_CHARS = 200_000
DIGEST_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.(json|md)")
# Where a scheduled afternoon run keeps its artifacts under a root: the legacy
# ce16ba6b tree, the plain scheduled_runs tree, and the date-named directories
# the repo-root artifacts/ layout uses.
SUMMARY_DIRS = ("ce16ba6b/scheduled_runs", "scheduled_runs", "")


def _week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _finite(value: Any) -> Any:
    if isinstance(value, float) and value != value:
        return None
    return value


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in list(value.items())[:200]}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, depth + 1) for item in list(value)[:200]]
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    if isinstance(value, float):
        return _finite(value)
    return str(value)[:200]


def weekly_dashboard(roots: Sequence[RootSpec], day: date) -> dict[str, Any]:
    """The weekly performance dashboard markdown for the ISO week containing ``day``."""
    from dca_weekly_dashboard import build_weekly_dashboard, load_json_files, load_scheduled_run_states

    start, end = _week_bounds(day)
    warnings: list[str] = []
    states: list[dict[str, Any]] = []
    sources = []
    for root in roots:
        loaded: list[dict[str, Any]] = []
        for base in (root.path, root.path / "ce16ba6b"):
            try:
                loaded.extend(load_scheduled_run_states(base))
            except (OSError, ValueError, TypeError):
                warnings.append(f"Scheduled run states under {root.label} could not be read.")
        # The library globs <base>/scheduled_runs/** only. The repo-root
        # artifacts/ tree keeps its afternoon runs in date-named directories,
        # so those are gathered here (bounded, confined) and passed through
        # the same manual/stub-dropping loader the weekly task uses.
        reader = root.reader(warnings)
        dated = [entry for entry in reader.entries("", limit=2000)
                 if iso_date(entry.name) and (root.path / entry).is_dir()]
        paths = []
        for entry in dated[:400]:
            for child in reader.entries(entry, limit=50):
                if child.name.startswith("run_state") and child.suffix == ".json" and reader.confined(child) is not None:
                    paths.append(root.path / child)
        if paths:
            try:
                loaded.extend(load_json_files(sorted(paths)))
            except (OSError, ValueError, TypeError):
                warnings.append(f"Date-named run directories under {root.label} could not be read.")
        kept = [state for state in loaded
                if isinstance(state, dict) and start.isoformat() <= str(state.get("trading_day") or "") <= end.isoformat()]
        if kept:
            sources.append({"root_label": root.label, "at_risk": root.kind != "primary", "runs": len(kept)})
        states.extend(kept)
    label = f"{start.isoformat()} to {end.isoformat()}"
    if not states:
        return {"week": label, "run_count": 0, "markdown": None, "sources": sources,
                "warnings": warnings + ["No scheduled run states fall in this week; the dashboard has nothing to summarise."]}
    try:
        markdown = build_weekly_dashboard(states, week_label=label)
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as error:
        # The builder refuses to guess (e.g. a run_state without
        # budget.base_amount_usd); say which rule refused rather than hide it.
        return {"week": label, "run_count": len(states), "markdown": None, "sources": sources,
                "warnings": warnings + [f"The dashboard builder refused: {type(error).__name__}: {str(error)[:240]}"]}
    truncated = len(markdown) > MAX_MARKDOWN_CHARS
    return {"week": label, "run_count": len(states), "markdown": markdown[:MAX_MARKDOWN_CHARS], "truncated": truncated,
            "sources": sources, "scope": "scheduled-live runs only; manual_runs/ is never traversed",
            "warnings": warnings}


def cost_tracker(roots: Sequence[RootSpec], year: int, month: int) -> dict[str, Any]:
    """The monthly automation cost tracker, in proxy units, over the primary root."""
    from dca_cost_tracker import (
        PROXY_DISCLAIMER,
        aggregate_usage,
        build_cost_tracker_markdown,
        collect_month_run_states,
    )

    if not roots:
        return {"month": f"{year:04d}-{month:02d}", "run_count": 0, "markdown": None, "aggregate": None,
                "warnings": ["No artifact root is configured."]}
    primary = roots[0]
    label = f"{year:04d}-{month:02d}"
    try:
        states = collect_month_run_states(primary.path, year, month)
    except (OSError, ValueError, TypeError):
        return {"month": label, "run_count": 0, "markdown": None, "aggregate": None,
                "warnings": [f"Run states under {primary.label} could not be read."]}
    aggregate = aggregate_usage(states, month_label=label)
    return {
        "month": label, "run_count": len(states), "root_label": primary.label,
        "aggregate": _jsonable(aggregate), "markdown": build_cost_tracker_markdown(aggregate)[:MAX_MARKDOWN_CHARS],
        "disclaimer": PROXY_DISCLAIMER, "warnings": [] if states else ["No run states carry a trading day in this month."],
    }


def _newest_named(roots: Sequence[RootSpec], filename: str) -> tuple[RootSpec | None, Path | None, list[str]]:
    """Newest ``filename`` under the summary directories of every root, by mtime."""
    warnings: list[str] = []
    best: tuple[float, RootSpec, Path] | None = None
    for root in roots:
        reader = root.reader(warnings)
        for base in SUMMARY_DIRS:
            for entry in reader.entries(base, limit=2000):
                candidate = entry / filename
                path = reader.confined(candidate)
                if path is None or not path.is_file():
                    continue
                try:
                    stamp = path.stat().st_mtime
                except OSError:
                    continue
                if best is None or stamp > best[0]:
                    best = (stamp, root, candidate)
    if best is None:
        return None, None, warnings
    return best[1], best[2], warnings


def latest_summary(roots: Sequence[RootSpec]) -> dict[str, Any]:
    """The newest latest_run_summary.json with its Notion metrics and compact markdown."""
    from dca_notion_sync import build_compact_sync_markdown, build_notion_sync_payload

    root, relative, warnings = _newest_named(roots, "latest_run_summary.json")
    if root is None or relative is None:
        return {"found": False, "warnings": warnings + ["No latest_run_summary.json exists under the observed roots."]}
    summary = root.reader(warnings).json(relative)
    if not summary:
        return {"found": False, "warnings": warnings + ["The newest latest_run_summary.json could not be parsed."]}
    try:
        payload = build_notion_sync_payload(summary)
        markdown = build_compact_sync_markdown(summary)
    except (ValueError, TypeError, KeyError) as error:
        return {"found": True, "root_label": root.label, "relative_path": relative.as_posix(),
                "warnings": warnings + [f"The Notion payload could not be built: {type(error).__name__}."]}
    return {
        "found": True, "root_label": root.label, "at_risk": root.kind != "primary", "relative_path": relative.as_posix(),
        "trading_day": iso_date(summary.get("trading_day")), "metrics": _jsonable(payload.get("metrics")),
        "title": str(payload.get("title") or "")[:120], "markdown": markdown[:MAX_MARKDOWN_CHARS],
        "page_id": None, "warnings": warnings,
    }


def console_digests(output: Path, mode: str) -> dict[str, Any]:
    """The console's own saved daily digests (names only)."""
    reader = BoundedReader(output / "summaries", [])
    days: dict[str, dict[str, bool]] = {}
    for entry in reader.entries(mode, limit=2000):
        if DIGEST_NAME.fullmatch(entry.name):
            day, suffix = entry.name.rsplit(".", 1)
            days.setdefault(day, {"json": False, "md": False})[suffix] = True
    rows = [{"day": day, **flags} for day, flags in sorted(days.items(), reverse=True)][:200]
    return {"mode": mode, "days": rows, "count": len(days), "location": root_label(output / "summaries" / mode)}


def console_digest(output: Path, mode: str, day: date) -> dict[str, Any]:
    """One saved daily digest's markdown."""
    warnings: list[str] = []
    reader = BoundedReader(output / "summaries", warnings)
    text = reader.text(Path(mode) / f"{day.isoformat()}.md")
    if text is None:
        return {"found": False, "day": day.isoformat(), "warnings": warnings + ["No digest was saved for this day."]}
    return {"found": True, "day": day.isoformat(), "markdown": text[:MAX_MARKDOWN_CHARS], "warnings": warnings}


def average_cost(roots: Sequence[RootSpec]) -> dict[str, Any]:
    """The newest average-cost snapshot, with account identities hashed away."""
    from dca_average_cost_snapshot import AVERAGE_COST_SNAPSHOT_FILENAME

    root, relative, warnings = _newest_named(roots, AVERAGE_COST_SNAPSHOT_FILENAME)
    if root is None or relative is None:
        return {"found": False, "warnings": warnings + ["No average-cost snapshot exists under the observed roots."]}
    snapshot = root.reader(warnings).json(relative)
    if not snapshot:
        return {"found": False, "warnings": warnings + ["The newest average-cost snapshot could not be parsed."]}
    combined: dict[str, Any] = {}
    accounts: dict[str, Any] = {}
    if isinstance(snapshot, dict):
        raw_combined = snapshot.get("combined")
        if isinstance(raw_combined, dict):
            combined = raw_combined
        raw_accounts = snapshot.get("accounts")
        if isinstance(raw_accounts, dict):
            accounts = raw_accounts
    rows = []
    for ticker, block in list(combined.items())[:500]:
        if not isinstance(block, dict) or not re.fullmatch(r"[A-Z][A-Z.]{0,5}", str(ticker)):
            continue
        rows.append({"ticker": ticker, "average_cost_usd": _finite(block.get("average_cost_usd")),
                     "total_quantity": _finite(block.get("total_quantity"))})
    return {
        "found": True, "root_label": root.label, "relative_path": relative.as_posix(),
        "generated_at": str(snapshot.get("generated_at") or "")[:40] if isinstance(snapshot, dict) else None,
        "account_count": len(accounts),
        # Account numbers are keys in the artifact; the page only ever sees a digest of each.
        "accounts": [hashlib.sha256(str(key).encode()).hexdigest()[:12] for key in list(accounts)[:50]],
        "combined": rows, "warnings": warnings,
    }


def build_reports_snapshot(roots: Sequence[RootSpec], output: Path, mode: str, day: date) -> dict[str, Any]:
    """Cheap summary for the Reports page; the heavy markdown loads on demand."""
    weekly = weekly_dashboard(roots, day)
    cost = cost_tracker(roots, day.year, day.month)
    latest = latest_summary(roots)
    return {
        "date": day.isoformat(),
        "weekly": {key: weekly.get(key) for key in ("week", "run_count", "sources", "scope", "warnings")},
        "cost": {key: cost.get(key) for key in ("month", "run_count", "root_label", "disclaimer", "warnings")},
        "latest": {key: latest.get(key) for key in ("found", "root_label", "trading_day", "relative_path", "warnings")},
        "digests": console_digests(output, mode),
        "average_cost": average_cost(roots),
        "boundary": "Rendered in memory from artifacts; nothing here writes a report file or pushes to Notion.",
    }
