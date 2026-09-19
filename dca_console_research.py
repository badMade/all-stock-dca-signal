"""The Research page's catalog: Octopus runs, research launches and the analysis archive (GUI expansion Phase 4).

The walled-off research world stays walled off here: this module lists run
directories and their metadata files through the bounded reader, never
imports a research package, never reads a model checkpoint's tensors, and
never touches the Google Drive ticker archive. Generated pages (the market
graph, the Octopus inspector) reach the page through the launcher manifests,
so nothing here resolves a browser-supplied path either.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from dca_console_reader import BoundedReader, RootSpec, iso_date, root_label

RUN_NAME = re.compile(r"[A-Za-z0-9._-]{1,120}")
MAX_RUNS = 200


def _modified(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def _octopus_run(reader: BoundedReader, relative: Path, label: str, *, demo: bool) -> dict[str, Any]:
    meta = reader.json(relative / "run_meta.json")
    state = reader.json(relative / "checkpoint" / "harness_state.json") or reader.json(relative / "checkpoint.prev" / "harness_state.json")
    report_path = reader.confined(relative / "report" / "report.json")
    universe = meta.get("universe") if isinstance(meta.get("universe"), dict) else {}
    tickers = universe.get("tickers") if isinstance(universe.get("tickers"), list) else None
    return {
        "name": relative.name,
        "location": label,
        "demo": demo,
        "run_meta_present": bool(meta),
        # daily_update.py refuses a run without run_meta.json: say so up front.
        "updatable": bool(meta),
        "model_selection": str(meta.get("model_selection") or "")[:40] or None,
        "ticker_count": len(tickers) if tickers is not None else None,
        "last_processed_date": iso_date(state.get("last_processed_date")),
        "report_present": bool(report_path and report_path.is_file()),
        "modified": _modified(reader.root / relative),
    }


def octopus_runs(roots: Sequence[RootSpec], output: Path) -> dict[str, Any]:
    """Every Octopus run directory under the primary root's tree and the console's demo tree."""
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    locations = []
    if roots:
        locations.append((roots[0].path / "wondrous_octopus" / "runs", roots[0].label, False))
    locations.append((output / "demo" / "octopus", root_label(output / "demo" / "octopus"), True))
    for base, label, demo in locations:
        reader = BoundedReader(base, warnings)
        if not base.is_dir():
            continue
        for entry in sorted(reader.entries("", limit=MAX_RUNS), reverse=True):
            if RUN_NAME.fullmatch(entry.name) and (base / entry).is_dir():
                rows.append(_octopus_run(reader, entry, label, demo=demo))
    return {"runs": rows[:MAX_RUNS], "count": len(rows), "warnings": warnings,
            "note": "Selection stays a startup flag (--octopus-run); a run without run_meta.json cannot be updated."}


def analysis_archive(repo: Path) -> dict[str, Any]:
    """Top-level analysis/ entries with their README presence and any ticker-advisory outputs."""
    warnings: list[str] = []
    reader = BoundedReader(repo / "analysis", warnings)
    rows = []
    for entry in sorted(reader.entries("", limit=500), reverse=True):
        path = reader.confined(entry)
        if path is None or not path.is_dir():
            continue
        children = reader.entries(entry, limit=200)
        advisory = "ticker-advisory" in entry.name
        rows.append({
            "name": entry.name,
            "readme": any(child.name.lower() == "readme.md" for child in children),
            "entries": len(children),
            "ticker_advisory": advisory,
            "outputs": sorted(child.name for child in children if advisory and child.suffix in {".json", ".md"})[:20],
            "modified": _modified(path),
        })
    return {"entries": rows, "count": len(rows), "warnings": warnings,
            "note": "Dated one-off analyses and research output; a ticker-advisory proposal is never applied by the console."}


def research_launches(app) -> list[dict[str, Any]]:
    """Launcher manifests whose artifacts are research pages or dry-run outputs."""
    from dca_console_launch import recent_launches

    return [entry for entry in recent_launches(app) if entry.get("kind") in {"page", "research"}]


def build_research_snapshot(app, repo: Path) -> dict[str, Any]:
    from dca_console_reader import roots_from_settings

    roots = roots_from_settings(app.settings)
    return {
        "octopus": octopus_runs(roots, app.settings.output),
        "analysis": analysis_archive(repo),
        "launches": research_launches(app),
        "boundary": "Research artifacts are read-only here and never influence live sizing; the two live-sizing config files are written only by their guarded publishers.",
    }
