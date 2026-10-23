"""Allowlisted, parameter-free launchers for the console's Workflows page (GUI expansion Phase 4).

Every launcher here is one more entry in the same closed action list the
console has always had: the browser still posts ``{"action": "<id>"}`` and
nothing else, and every input an action needs (which payload, which run
directory, which day, which figure) is resolved on the server from the
observed artifact trees, the configuration, or the console's own settings.
Three properties are enforced by the tests rather than by convention:

- No template ever carries a token that could post, publish, write into the
  checkout or name an account (``FORBIDDEN_ARGV_TOKENS``); every output path
  is inside the job's own directory under the console output tree.
- Nothing here places, reviews or cancels an order, and nothing writes a
  config file: ``ml-train-dry-run`` pins ``--dry-run`` and points ``--output``
  at the job directory, and the seven Gate 5 replays call generators that stop
  at Gate 5 by construction.
- A captured document is written by :class:`dca_console_jobs.JobManager`'s
  stdout capture (the generators print JSON and accept no ``--out``), then
  listed in a per-job manifest that the artifact route trusts instead of any
  browser-supplied file name.
"""

from __future__ import annotations

import json
import math
import os
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from dca_console_reader import BoundedReader, RootSpec, iso_date, root_label, roots_from_settings
from dca_fileio import atomic_write_text

FORBIDDEN_ARGV_TOKENS = (
    "--slack", "--slack-webhook", "--ledger", "--write", "--force", "--account",
    "--all-tickers-in-archive", "--archive-dir", "--install", "--publish",
)
GATE5_SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430", "1530")
# Slots whose generator requires the afternoon-scheduled 7-day deployment figure
# (scripts.stream_candidates.StreamSpec.requires_deployed_last_7d_usd).
GATE5_NEEDS_DEPLOYED = ("1230", "1530")
GATE5_MODULES = {
    "0935": ("scripts.generate_market_open_candidates", ()),
    "1030": ("scripts.generate_morning_candidates", ()),
    "1130": ("scripts.generate_intraday_candidates", ("--slot", "1130")),
    "1230": ("scripts.generate_midday_candidates", ()),
    "1330": ("scripts.generate_intraday_candidates", ("--slot", "1330")),
    "1430": ("scripts.generate_intraday_candidates", ("--slot", "1430")),
    "1530": ("scripts.generate_buy_candidates", ("--slot", "1530ET")),
}
ARTIFACT_NAME = re.compile(r"[A-Za-z0-9._-]{1,120}")
JOB_ID = re.compile(r"[a-zA-Z0-9-]{1,100}")
ARTIFACT_KINDS = {".html": "html", ".json": "json", ".md": "markdown", ".txt": "text", ".csv": "csv", ".xml": "xml"}
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_PAYLOAD_NAME = re.compile(r"market_data_(?P<a>[0-9_-]*?)(?P<slot>0935|1030|1130|1230|1330|1430|1530)(?:ET)?(?P<b>[0-9_A-Za-z-]*)\.json")
_RUN_DIR = re.compile(r"(\d{8})_(\d{4})ET")
_PAYLOAD_DAY_SOURCE_RANK = {"unknown": -1, "file modification time": 0, "run directory": 1, "filename": 2}


@dataclass(frozen=True)
class LaunchSpec:
    id: str
    label: str
    description: str
    interpreter: str  # core | research
    kind: str  # check | report | page | document | research
    outputs: tuple[str, ...]
    timeout: float = 600


LAUNCH_SPECS: dict[str, LaunchSpec] = {
    "config-invariants": LaunchSpec(
        "config-invariants", "Check config invariants",
        "Run tests/test_config_invariants.py (config vs constant parity, schema references, account-literal scan) and keep the junit report.",
        "core", "check", ("junit.xml", "summary.json"), 900),
    "risk-report": LaunchSpec(
        "risk-report", "Risk report",
        "Run dca_risk over the primary root for the selected day: rolling cash burn, the 7-day kill switch and its evidence block.",
        "core", "report", ("risk.json",)),
    "alert-digest": LaunchSpec(
        "alert-digest", "Alert digest",
        "Digest the Gate 9 alert files under the primary root by gate, slot and streak (Gmail exports stay out of scope).",
        "core", "report", ("digest.json",)),
    "drift-check": LaunchSpec(
        "drift-check", "Drift check (newest run)",
        "Run scripts/drift_detection.py on the newest run directory under the primary root; never posts to Slack.",
        "core", "report", ("drift.json", "drift.md")),
    "market-graph-archive": LaunchSpec(
        "market-graph-archive", "Market graph (archive, last year)",
        "Render the 3D market graph from the ticker archive for the last 365 days as one self-contained page.",
        "core", "page", ("ocean.html",), 1800),
    "market-graph-csv": LaunchSpec(
        "market-graph-csv", "Market graph (committed CSV)",
        "Render the 3D market graph from historical_stock_data.csv (a handful of symbols; the resolution line says which).",
        "core", "page", ("ocean.html",), 900),
    "octopus-inspect-html": LaunchSpec(
        "octopus-inspect-html", "Octopus inspector page",
        "Write the Wondrous Octopus dashboard for the configured run directory as one HTML page.",
        "research", "page", ("octopus.html",), 900),
    "octopus-ledger-export": LaunchSpec(
        "octopus-ledger-export", "Octopus ledger export",
        "Export the Octopus ledger predictions and scores for the active scheduled tickers as CSV, JSON and a Markdown report in the job directory.",
        "research", "report", ("octopus_ledger.csv", "octopus_ledger.json", "octopus_ledger.md"), 900),
    "ml-train-dry-run": LaunchSpec(
        "ml-train-dry-run", "ml-forecaster dry run",
        "Train and evaluate the sizing model without publishing: artifacts and the proposed adjustment file land in the job directory only.",
        "research", "research", ("ml_sizing_adjustment.preview.json",), 1800),
}
for _slot in GATE5_SLOTS:
    LAUNCH_SPECS[f"gate5-replay-{_slot}"] = LaunchSpec(
        f"gate5-replay-{_slot}", f"Gate 5 replay {_slot[:2]}:{_slot[2:]} ET",
        f"Re-run the {_slot} candidate generator on the newest saved market-data payload; Gate 5 only, no broker, output labelled as a replay of saved inputs.",
        "core", "document", ("document.json",), 900)


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


# --- Gate 5 payload discovery ----------------------------------------------------

def _payload_day(name: str, relative_dir: str, modified: float | None) -> tuple[str | None, str]:
    match = _PAYLOAD_NAME.fullmatch(name)
    tokens = (match.group("a") + " " + match.group("b")) if match else ""
    iso = re.search(r"\d{4}-\d{2}-\d{2}", tokens)
    if iso and iso_date(iso.group(0)):
        return iso.group(0), "filename"
    compact = re.search(r"(?<!\d)(\d{8})(?!\d)", tokens)
    if compact:
        candidate = f"{compact.group(1)[:4]}-{compact.group(1)[4:6]}-{compact.group(1)[6:]}"
        if iso_date(candidate):
            return candidate, "filename"
    run_dir = _RUN_DIR.search(relative_dir)
    if run_dir:
        candidate = f"{run_dir.group(1)[:4]}-{run_dir.group(1)[4:6]}-{run_dir.group(1)[6:]}"
        if iso_date(candidate):
            return candidate, "run directory"
    dated_dir = re.search(r"(\d{4}-\d{2}-\d{2})", relative_dir)
    if dated_dir and iso_date(dated_dir.group(1)):
        return dated_dir.group(1), "run directory"
    if modified is not None:
        return datetime.fromtimestamp(modified, timezone.utc).date().isoformat(), "file modification time"
    return None, "unknown"


def _candidate_dirs(reader: BoundedReader) -> list[str]:
    """Directories to scan for payloads: the root itself, each stream tree and its run directories."""
    from dca_paths import STREAM_ARTIFACT_DIRS

    streams = [*dict.fromkeys(STREAM_ARTIFACT_DIRS.values()), "scheduled_runs", "ce16ba6b/scheduled_runs"]
    found = [""]
    for stream in streams:
        for entry in reader.entries(stream, limit=2000):
            if (reader.root / entry).is_dir():
                found.append(entry.as_posix())
    return found


def _payload_sort_key(day: str | None, source: str, modified: float) -> tuple[int, str, int, float]:
    """Prefer payloads with explicit trading days, then by newest day.

    A bare ``market_data_1130ET.json`` can only fall back to its mtime, which is
    the weakest hint and drifts every time the file is recopied. A payload whose
    filename or parent run directory states the trading day is safer to replay,
    even if an undated sibling happened to be touched more recently. Among
    explicitly dated payloads, the newest trading day is preferred.
    """
    explicit = 1 if source in {"filename", "run directory"} else 0
    return (explicit, day or "", _PAYLOAD_DAY_SOURCE_RANK.get(source, -1), modified)


def _discover(roots: Sequence[RootSpec]) -> list[dict[str, Any]]:
    rows = []
    for root in roots:
        warnings: list[str] = []
        reader = root.reader(warnings)
        if not root.path.is_dir():
            continue
        for directory in _candidate_dirs(reader):
            for entry in reader.entries(directory, limit=2000):
                match = _PAYLOAD_NAME.fullmatch(entry.name)
                if match is None:
                    continue
                path = reader.root / entry
                if not path.is_file():
                    continue
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                day, source = _payload_day(entry.name, directory, modified)
                rows.append({
                    "slot": match.group("slot"), "root_label": root.label, "at_risk": root.kind != "primary",
                    "relative_path": entry.as_posix(), "trading_day": day, "day_source": source,
                    "modified": datetime.fromtimestamp(modified, timezone.utc).isoformat(), "_path": path,
                    "_sort": _payload_sort_key(day, source, modified),
                })
    return rows


# Payload discovery walks every stream and run directory under every root, and
# ConsoleApp.actions() asks for it on each 15-second snapshot. Cache the public
# answer briefly per root set; a launch always re-discovers through
# _payload_path so it never acts on a stale listing.
_PAYLOAD_CACHE: dict[tuple[str, ...], tuple[float, dict[str, dict[str, Any] | None]]] = {}
PAYLOAD_CACHE_SECONDS = 30.0


def latest_gate5_payloads(roots: Sequence[RootSpec], *, now: float | None = None) -> dict[str, dict[str, Any] | None]:
    """The newest saved market-data payload per slot across every root, without paths."""
    import time

    key = tuple(str(root.path) for root in roots)
    stamp = time.monotonic() if now is None else now
    cached = _PAYLOAD_CACHE.get(key)
    if cached is not None and stamp - cached[0] < PAYLOAD_CACHE_SECONDS:
        return {slot: (dict(row) if row else None) for slot, row in cached[1].items()}
    by_slot: dict[str, list[dict[str, Any]]] = {}
    for row in _discover(roots):
        by_slot.setdefault(row["slot"], []).append(row)
    # Newest replayable payload per slot: an old payload that still carries the retired yfinance source key
    # is skipped (and counted) instead of being offered and then failing inside the generator.
    best = {slot: row for slot, rows in by_slot.items() if (row := _newest_replayable(rows)) is not None}
    result = {
        slot: ({field: value for field, value in best[slot].items() if not field.startswith("_")} if slot in best else None)
        for slot in GATE5_SLOTS
    }
    if len(_PAYLOAD_CACHE) >= 16:
        _PAYLOAD_CACHE.clear()
    _PAYLOAD_CACHE[key] = (stamp, result)
    return {slot: (dict(row) if row else None) for slot, row in result.items()}


def _payload_path(roots: Sequence[RootSpec], slot: str) -> tuple[dict[str, Any] | None, Path | None]:
    best = _newest_replayable([row for row in _discover(roots) if row["slot"] == slot])
    if best is None:
        return None, None
    return {key: value for key, value in best.items() if not key.startswith("_")}, best["_path"]


# 2026-10-12 owner direction retired the yfinance fallback: every candidate generator now rejects a
# ``sources`` key other than these (tests/test_yfinance_retired.py), so a saved payload that still carries
# ``yahoo_finance_yfinance`` can never be replayed. Kept as a literal (not imported from near_close_dca)
# so payload discovery stays import-light on the 15-second snapshot path; a test pins it to SourceName.
REPLAYABLE_SOURCE_KEYS = frozenset({"finance", "robinhood"})
_PAYLOAD_PROBE_MAX_BYTES = 16 * 1024 * 1024


def _retired_source_keys(path: Path) -> list[str]:
    """``sources`` keys in a saved payload that the generators reject; empty when replayable or unreadable.

    Only a ``sources`` object with a non-canonical key marks a payload unreplayable. A file we cannot read,
    parse or recognise is left to the generator, which owns the real validation and reports it in the job
    log, rather than being silently hidden from the operator here.
    """
    try:
        if path.stat().st_size > _PAYLOAD_PROBE_MAX_BYTES:
            return []
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return []
    sources = document.get("sources") if isinstance(document, dict) else None
    if not isinstance(sources, dict):
        return []
    return sorted(str(key) for key in sources if key not in REPLAYABLE_SOURCE_KEYS)


def _newest_replayable(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest row whose payload the generators would accept, skipping retired-source payloads."""
    skipped = 0
    for row in sorted(rows, key=lambda item: item["_sort"], reverse=True):
        if _retired_source_keys(row["_path"]):
            skipped += 1
            continue
        return {**row, "skipped_retired_source": skipped}
    return None


# --- Readiness ---------------------------------------------------------------------

def _archive_configured(repo: Path) -> bool:
    configured = os.environ.get("DCA_TICKER_HISTORY_DIR")
    target = Path(configured).expanduser() if configured else repo / "ticker_data"
    # lstat only: the archive is a Google Drive symlink and must never be
    # followed on the request thread.
    return os.path.lexists(target)


def launch_actions(app, repo: Path, *, payloads: dict[str, dict | None] | None = None) -> list[dict[str, Any]]:
    """Availability rows for every launcher, in the shape ConsoleApp.actions() already uses."""
    from dca_console import RESEARCH_PACKAGES, dependency_reason

    settings = app.settings
    research_reason = dependency_reason(app.packages, RESEARCH_PACKAGES)
    root_ok = settings.root.is_dir()
    root_reason = "" if root_ok else "No artifact root is available yet."
    if payloads is None:
        payloads = latest_gate5_payloads(roots_from_settings(settings)) if root_ok or settings.secondary_roots else {
            slot: None for slot in GATE5_SLOTS}
    rows = []
    for spec in LAUNCH_SPECS.values():
        available, reason, extra = True, "", {}
        if spec.id == "config-invariants":
            available, reason = app.dev_packages.get("pytest") is True, "Install requirements-dev.txt in the Python environment."
        elif spec.id in {"risk-report", "alert-digest", "drift-check"}:
            available, reason = root_ok, root_reason
        elif spec.id == "market-graph-archive":
            available, reason = _archive_configured(repo), "The ticker archive (ticker_data or DCA_TICKER_HISTORY_DIR) is not present."
        elif spec.id == "market-graph-csv":
            available, reason = (repo / "historical_stock_data.csv").is_file(), "historical_stock_data.csv is missing from the checkout."
        elif spec.id in {"octopus-inspect-html", "octopus-ledger-export"}:
            active_run = getattr(app, "octopus_run", None) or settings.octopus_run
            available = not research_reason and active_run is not None
            reason = research_reason or "Supply --octopus-run with an existing Octopus run directory."
        elif spec.id == "ml-train-dry-run":
            available, reason = not research_reason, research_reason
        elif spec.id.startswith("gate5-replay-"):
            slot = spec.id.rsplit("-", 1)[1]
            payload = payloads.get(slot)
            available = payload is not None
            reason = "" if available else (
                f"No replayable saved market_data payload for {slot} was found under the observed roots "
                "(payloads still carrying the retired yahoo_finance_yfinance source are skipped).")
            extra = {"payload": payload, "needs_deployed_figure": slot in GATE5_NEEDS_DEPLOYED}
        rows.append({"id": spec.id, "label": spec.label, "description": spec.description,
                     "available": bool(available), "reason": "" if available else reason,
                     "kind": spec.kind, "interpreter": spec.interpreter, "outputs": list(spec.outputs), **extra})
    return rows


# --- Running --------------------------------------------------------------------------

def launch_dir(app, job_id: str) -> Path:
    if JOB_ID.fullmatch(job_id) is None:
        raise ValueError("job id has an unexpected shape")
    return app.settings.output / "launches" / job_id


def _newest_run_dir(root: Path) -> Path | None:
    from dca_risk import find_run_state_files, run_timestamp_et

    newest: tuple[tuple[str, str, str], Path] | None = None
    try:
        for path in find_run_state_files(root)[:20000]:
            try:
                state = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            if not isinstance(state, dict):
                continue
            stamp = run_timestamp_et(state)
            # Order by the run's own timestamp, then its trading day, then the
            # directory name, so a run_state without generated_at still ranks.
            key = (stamp.isoformat() if stamp else "", str(state.get("trading_day") or ""), path.parent.name)
            if newest is None or key > newest[0]:
                newest = (key, path.parent)
    except OSError:
        return None
    return newest[1] if newest else None


def _deployed_figure(app, day: date, log: Callable[[str], None]) -> float | None:
    """The afternoon-scheduled 7-day deployment figure, or None when the evidence gate refuses it."""
    from dca_console_state import risk_view

    view = risk_view([RootSpec.of(app.settings.root, "primary")], day, config_dir=app.settings.config_dir)
    if not view.get("usable"):
        for warning in view.get("warnings", []):
            log(f"Risk evidence: {warning}\n")
        log("Refusing the replay: the deployment figure cannot be trusted without complete risk evidence.\n")
        return None
    figure = _finite(((view.get("report") or {}).get("scheduled_live") or {}).get("deployed_last_7d_usd"))
    if figure is None or figure < 0:
        log("Refusing the replay: the risk report carries no finite, non-negative 7-day deployment figure.\n")
        return None
    return figure


def _write_manifest(app, job_id: str, action: str, inputs: dict[str, Any], directory: Path) -> None:
    spec = LAUNCH_SPECS[action]
    artifacts = []
    for name in spec.outputs:
        path = directory / name
        if path.is_file():
            artifacts.append({"name": name, "kind": ARTIFACT_KINDS.get(path.suffix, "text"), "bytes": path.stat().st_size})
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / "manifest.json", json.dumps({
        "action": action, "job": job_id, "kind": spec.kind, "label": spec.label,
        "written_at": datetime.now(timezone.utc).isoformat(), "inputs": inputs, "artifacts": artifacts,
    }, indent=2, allow_nan=False))


def run_launch(app, action: str, job_id: str, log: Callable[[str], None], *, repo: Path,
               run_command: Callable[..., int], today: date) -> int:
    """Build and run one allowlisted launcher; ``run_command`` is ConsoleApp._command."""
    spec = LAUNCH_SPECS[action]
    directory = launch_dir(app, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    settings = app.settings
    inputs: dict[str, Any] = {"primary_root": root_label(settings.root), "day": today.isoformat()}
    results: list[int] = []

    if action == "config-invariants":
        results.append(run_command(["-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
                                    f"--junitxml={directory / 'junit.xml'}", "tests/test_config_invariants.py"],
                                   log, timeout=spec.timeout))
        summary = junit_summary(directory / "junit.xml")
        atomic_write_text(directory / "summary.json", json.dumps(summary, indent=2, allow_nan=False))
        log(f"\nConfig invariants: {summary['passed']} passed, {summary['failed']} failed, "
            f"{summary['skipped']} skipped (skipped schema checks mean 'not validated', not 'passed').\n")
    elif action == "risk-report":
        results.append(run_command(["-m", "dca_risk", "--state-root", str(settings.root), "--as-of", today.isoformat()],
                                   log, timeout=spec.timeout, capture_to=directory / "risk.json"))
    elif action == "alert-digest":
        results.append(run_command(["-m", "scripts.alert_digest", "--alerts-dir", str(settings.root), "--json"],
                                   log, timeout=spec.timeout, capture_to=directory / "digest.json"))
    elif action == "drift-check":
        run_dir = _newest_run_dir(settings.root)
        if run_dir is None:
            log("No run directory with a run_state.json exists under the primary root.\n")
            results.append(1)
        else:
            inputs["run_dir"] = str(run_dir.relative_to(settings.root)) if run_dir.is_relative_to(settings.root) else run_dir.name
            results.append(run_command(["scripts/drift_detection.py", "--run-dir", str(run_dir),
                                        "--json-out", str(directory / "drift.json"), "--md-out", str(directory / "drift.md")],
                                       log, timeout=spec.timeout))
    elif action in {"market-graph-archive", "market-graph-csv"}:
        source = "archive" if action == "market-graph-archive" else "csv"
        argv = ["-m", "scripts.generate_market_graph", "--source", source, "--out", str(directory / "ocean.html")]
        if source == "archive":
            # Bound the window server-side: the whole archive renders to a 35 MB page.
            start = today - timedelta(days=365)
            argv.extend(["--start", start.isoformat()])
            inputs["start"] = start.isoformat()
        results.append(run_command(argv, log, timeout=spec.timeout))
    elif action == "octopus-inspect-html":
        active_run = getattr(app, "octopus_run", None) or settings.octopus_run
        if active_run is None:
            log("No Octopus run directory is configured.\n")
            results.append(1)
        else:
            target = active_run
            if (target / "shard_1" / "checkpoint").is_dir() and not (target / "checkpoint").is_dir():
                target = target / "shard_1"
            inputs["octopus_run"] = root_label(target)
            results.append(run_command(["wondrous-octopus/inspect_run.py", "--run-dir", str(target),
                                        "--html", str(directory / "octopus.html"), "--summary"],
                                       log, research=True, timeout=spec.timeout))
    elif action == "octopus-ledger-export":
        active_run = getattr(app, "octopus_run", None) or settings.octopus_run
        if active_run is None:
            log("No Octopus run directory is configured.\n")
            results.append(1)
        else:
            target = active_run
            # Same shard resolution as the inspector page: a sharded run keeps its checkpoint under shard_1.
            if (target / "shard_1" / "checkpoint").is_dir() and not (target / "checkpoint").is_dir():
                target = target / "shard_1"
            inputs["octopus_run"] = root_label(target)
            # --all-scheduled is the card's documented form; every output path is pinned to the job directory.
            results.append(run_command(["scripts/export_octopus_ledger.py", "--run-dir", str(target), "--all-scheduled",
                                        "--csv-out", str(directory / "octopus_ledger.csv"),
                                        "--json-out", str(directory / "octopus_ledger.json"),
                                        "--report-md", str(directory / "octopus_ledger.md")],
                                       log, research=True, timeout=spec.timeout))
    elif action == "ml-train-dry-run":
        results.append(run_command(["ml-forecaster/train.py", "--dry-run", "--models-dir", str(directory / "models"),
                                    "--output", str(directory / "ml_sizing_adjustment.preview.json")],
                                   log, research=True, timeout=spec.timeout))
    elif action.startswith("gate5-replay-"):
        slot = action.rsplit("-", 1)[1]
        info, payload = _payload_path(roots_from_settings(settings), slot)
        if payload is None or info is None or not info.get("trading_day"):
            log(f"No saved market_data payload for {slot} with a resolvable trading day was found.\n")
            results.append(1)
        else:
            inputs["payload"] = info
            log(f"Replay of saved inputs: payload {info['relative_path']} from {info['root_label']}, "
                f"trading day {info['trading_day']} (from {info['day_source']}).\n")
            module, slot_args = GATE5_MODULES[slot]
            argv = ["-m", module, *slot_args, "--trading-day", info["trading_day"], "--market-data", str(payload),
                    "--config-dir", str(settings.config_dir)]
            figure_ok = True
            if slot in GATE5_NEEDS_DEPLOYED:
                figure = _deployed_figure(app, date.fromisoformat(info["trading_day"]), log)
                if figure is None:
                    figure_ok = False
                    results.append(1)
                else:
                    inputs["deployed_last_7d_usd"] = figure
                    argv.extend(["--deployed-last-7d-usd", f"{figure:.2f}"])
            if figure_ok:
                results.append(run_command(argv, log, timeout=spec.timeout, capture_to=directory / "document.json"))
    else:  # pragma: no cover - LAUNCH_SPECS and this dispatch are kept in step by a test
        raise ValueError(f"Unknown launcher {action}")

    _write_manifest(app, job_id, action, inputs, directory)
    log(f"Artifacts for this job are listed in the Workflows page under {job_id}.\n")
    return next((code for code in results if code != 0), 0) if results else 1


# --- Reading back -----------------------------------------------------------------------

def read_manifest(app, job_id: str) -> dict[str, Any] | None:
    if JOB_ID.fullmatch(job_id) is None:
        return None
    reader = BoundedReader(app.settings.output / "launches", [])
    manifest = reader.json(Path(job_id) / "manifest.json")
    return manifest or None


def artifact_bytes(app, job_id: str, name: str) -> tuple[bytes, str] | None:
    """Return one manifest-listed artifact's bytes and kind, or None."""
    manifest = read_manifest(app, job_id)
    if manifest is None or ARTIFACT_NAME.fullmatch(name) is None:
        return None
    listed = next((item for item in manifest.get("artifacts", []) if item.get("name") == name), None)
    if listed is None:
        return None
    path = (app.settings.output / "launches" / job_id / name).resolve()
    if not path.is_relative_to((app.settings.output / "launches").resolve()) or not path.is_file():
        return None
    with path.open("rb") as handle:
        data = handle.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        return None
    return data, str(listed.get("kind") or "text")


def recent_launches(app, limit: int = 30) -> list[dict[str, Any]]:
    """Manifests of recent launcher jobs, newest first, for the Workflows page."""
    reader = BoundedReader(app.settings.output / "launches", [])
    rows = []
    for entry in sorted(reader.entries("", limit=500), reverse=True)[:limit]:
        manifest = reader.json(entry / "manifest.json")
        if manifest:
            rows.append({key: manifest.get(key) for key in ("action", "job", "kind", "label", "written_at", "inputs", "artifacts")})
    return rows


def launch_bench(app, repo: Path) -> dict[str, Any]:
    """Everything the Workflows page needs beyond the action cards: payloads and recent artifacts."""
    payloads = latest_gate5_payloads(roots_from_settings(app.settings))
    return {
        "payloads": payloads,
        "actions": launch_actions(app, repo, payloads=payloads),
        "launches": recent_launches(app),
        "forbidden_tokens": list(FORBIDDEN_ARGV_TOKENS),
        "boundary": "Every launcher is Gate 5 or read-only: no order is placed, reviewed or cancelled, and no config file is written.",
    }


def _is_safe_xml(path: Path) -> bool:
    """
    Validates XML is free of DOCTYPE declarations to prevent XXE/Billion Laughs.
    Attempts strict decoding to prevent encoding-based bypasses.
    """
    for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'):
        try:
            content = path.read_text(encoding=encoding, errors='strict')
            if "<!DOCTYPE" in content.upper():
                return False
            return True
        except UnicodeError:
            continue
    return False


def junit_summary(path: Path) -> dict[str, Any]:
    """Summarise a pytest junit report; skipped means 'not validated', never 'passed'."""
    summary: dict[str, Any] = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "cases": [], "parsed": False}
    try:
        if not _is_safe_xml(path):
            return summary
        tree = ElementTree.parse(path)
    except (OSError, ElementTree.ParseError):
        return summary
    for case in tree.iter("testcase"):
        name = (case.get("name") or "")[:200]
        outcome = "passed"
        message = ""
        for child in case:
            if child.tag in {"failure", "error", "skipped"}:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[child.tag]
                message = (child.get("message") or "")[:300]
                break
        summary[{"passed": "passed", "failed": "failed", "error": "errors", "skipped": "skipped"}[outcome]] += 1
        if len(summary["cases"]) < 500:
            summary["cases"].append({"name": name, "outcome": outcome, "message": message})
    summary["parsed"] = True
    return summary


def argv_templates(app, repo: Path) -> dict[str, list[str]]:
    """Every launcher's argv rendered with placeholder inputs, for the forbidden-token test."""
    seen: dict[str, list[str]] = {}

    def capture(action: str):
        def run(argv, log, *, research=False, timeout=600, capture_to=None, demo=False):
            seen[action] = list(argv)
            return 0
        return run

    class _Log:
        def __call__(self, message: str) -> None:
            return None

    for action in LAUNCH_SPECS:
        seen.setdefault(action, [])
        try:
            run_launch(app, action, "template-job", _Log(), repo=repo, run_command=capture(action), today=date(2026, 1, 5))
        except Exception:  # noqa: BLE001 - a template render failure is reported by the caller's assertion
            pass
    return seen
