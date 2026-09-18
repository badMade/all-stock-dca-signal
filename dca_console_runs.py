"""Read-only builders behind the console's Runs page (GUI expansion, Phase 1).

Four builders, one per panel: the artifact tree map, the run index across every
observed root, one run's detail, and the same-day pointer strip. Each takes the
``RootSpec`` sequence from :mod:`dca_console_reader` and returns a JSON-safe
dict whose strings are bounded and passed through the console redaction, whose
roots are named by label, and whose files are named relative to their root --
never an absolute path, a broker order id, or an account number. ``None``
always means *unavailable*, never zero.

Everything here is lock-free by construction: it reads ``run_state.json`` and
the CSV/JSON artifacts beside it through :class:`BoundedReader`, and reuses
the analysis helpers (drift, artifact contract, slippage, close-window grading,
evidence-gap diagnosis) that operate on plain dicts and directories. It never
instantiates a ledger class, never opens ``strategy_state.json`` through
``dca_runner.state_transaction``, and never constructs an executor or broker;
a test pins that no cross-process lock is taken by any builder.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dca_console_data import _day_of, _run_row
from dca_console_jobs import redact_value
from dca_console_reader import MAX_FILES, BoundedReader, RootSpec, iso_date
from dca_paths import STREAM_ARTIFACT_DIRS
from dca_risk import find_run_state_files, run_timestamp_et
from scripts.run_classification import is_manual_state

ET = ZoneInfo("America/New_York")
RUN_ID_RE = re.compile(r"^run-[0-9a-f]{12}$")
SLOT_DIR_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})ET$")
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Any absolute path a helper echoes back (drift evidence, contract errors,
# pointer reasons) is replaced before it can reach the browser.
_ABSOLUTE_PATH_RE = re.compile(r"/(?:[\w.@+-]+/)+[\w.@+-]*")
MAX_ROWS = 200
MAX_RUNS = 500
MAX_TEXT = 500
# Stream directories, then everything else an operator expects to find under
# a cron_tracking-shaped root. Absent directories render present=False rather
# than as empty history, because a missing tree is the alarm, not a zero.
_STREAM_DIRS = tuple(STREAM_ARTIFACT_DIRS.values())
_AFTERNOON_DIRS = ("scheduled_runs", "ce16ba6b/scheduled_runs")
TREE_DIRECTORIES = (
    *_STREAM_DIRS, *_AFTERNOON_DIRS,
    *(f"{name}/manual_runs" for name in _STREAM_DIRS), *(f"{name}/alerts" for name in _STREAM_DIRS),
    "alerts", "attempt_log", "model_usage", "cumulative_dca_credits", "whole_share_accumulation",
    "inverse_martingale_dca", "per_ticker_calibration", "ticker_history_update", "wondrous_octopus/runs",
    "console", "dca_runner",
)
_DIR_STREAM = {name: stream.replace("_", "-") for stream, name in STREAM_ARTIFACT_DIRS.items()}
_DIR_STREAM.update({name: "afternoon" for name in _AFTERNOON_DIRS})
IDENTITY_KEYS = ("trading_day", "stream", "slot", "status", "order_submit_status", "error_class", "dry_run",
                 "demo", "mode", "counts", "budget", "risk_guard", "usage_proxy")
CLASSIFICATION_KEYS = ("stream", "run_origin", "manual_run", "run_type", "cron_id", "schema", "state_schema_version")
TABLE_NAMES = ("signals.csv", "morning_signals.csv", "midday_signals.csv", "market_open_signals.csv",
               "intraday_signals.csv", "buy_candidates.csv", "submitted_orders.csv", "orders.csv", "observations.csv")
# Column allowlist for every table: symbol, prices, ladder, amounts, statuses,
# guard/block reasons and timestamps. Order ids, account fields, run ids and
# free-text warning columns are never forwarded (contract: no broker ids).
TABLE_COLUMNS = frozenset({
    "ticker", "symbol", "action", "reason", "source", "data_source", "execution_status", "status", "stream", "slot",
    "trading_day", "manual_run", "decision_bar_close", "previous_close", "previous_low", "lookback_price",
    "current_price", "price", "quote_price", "drop_pct", "drop_vs_previous_close_pct", "price_vs_previous_close_pct",
    "intraday_trend_pct", "buy_multiplier", "multiplier", "ladder_multiplier", "selected_base_amount_usd",
    "suggested_amount_usd", "planned_amount_usd", "submitted_amount_usd", "notional_usd", "hypothetical_amount_usd",
    "incremental_order_amount", "target_price", "target_price_missing", "top_up_guard_result", "top_up_skipped",
    "top_up_manual_review", "skip_reason", "deliberate_skip", "block_reason", "breaker_halt_reason", "spread_ok",
    "spread_pct", "spread_status", "auto_buy_signal", "trend_validated", "decision_bar_timestamp",
    "previous_close_timestamp", "lookback_bar_timestamp", "quote_time", "price_as_of", "previous_close_as_of",
    "bar_timestamp", "observed_at_et", "order_submitted_at", "order_filled_at", "avg_fill_price", "quantity_filled",
    "slippage_abs", "slippage_pct", "execution_quality",
})
INPUT_PREFIXES = ("market_data_", "orders_pages_", "cands_", "candidates_")
MULTIPLIER_LABEL = "reporting cap 3x"


# --- shared bounding ---------------------------------------------------------

def run_identity(label: str, relative: str) -> str:
    """Opaque, stable id for one run directory: ``run-`` + sha256(label:relative)[:12]."""
    return "run-" + hashlib.sha256(f"{label}:{relative}".encode()).hexdigest()[:12]


def _text(value: Any, roots: Sequence[RootSpec] = ()) -> str:
    """Bound one free-text value, replace root paths with labels, scrub any other path."""
    text = str(value)[:MAX_TEXT]
    for root in roots:
        text = text.replace(str(root.path), f"<{root.label}>")
    # Whatever spelling a helper used (resolved, symlinked, a sibling tree),
    # no absolute path survives; the label substitution above is cosmetic.
    return redact_value(_ABSOLUTE_PATH_RE.sub("<path>", text))


def _json_safe(value: Any, roots: Sequence[RootSpec] = (), depth: int = 0) -> Any:
    """Coerce a helper's output to JSON-safe primitives; non-finite floats become None."""
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _text(value, roots)
    if depth >= 6:
        return None
    if isinstance(value, dict):
        return {str(key)[:80]: _json_safe(item, roots, depth + 1) for key, item in list(value.items())[:MAX_ROWS]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, roots, depth + 1) for item in list(value)[:MAX_ROWS]]
    return _text(value, roots)


def _warn(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _stat_time(path: Path) -> float | None:
    try:
        return path.lstat().st_mtime
    except OSError:
        return None


def _iso(stamp: float | None) -> str | None:
    return datetime.fromtimestamp(stamp, ET).isoformat() if stamp is not None else None


# --- 1. artifact tree map ----------------------------------------------------

def artifact_tree_map(roots: Sequence[RootSpec]) -> dict[str, Any]:
    """Probe every expected artifact directory under each root.

    Returns ``{"roots": [{label, kind, exists, at_risk, directories: [{name,
    entries, newest, present}]}], "warnings"}``. ``present=False`` means the
    directory does not exist (never "empty history"); ``entries`` is a bounded
    count; ``newest`` is the ISO mtime of the newest entry or None when the
    directory is absent or empty. Secondary roots are ``at_risk`` because the
    repo-root ``artifacts/`` tree is gitignored and unmanaged.
    """
    warnings: list[str] = []
    result = []
    for root in roots:
        reader = root.reader(warnings)
        directories = []
        for name in TREE_DIRECTORIES:
            folder = reader.confined(name)
            present = folder is not None and folder.is_dir()
            entries = reader.entries(name) if present else []
            newest = max((_stat_time(root.path / entry) or 0.0 for entry in entries), default=None)
            directories.append({"name": name, "present": present, "entries": len(entries),
                                "newest": _iso(newest) if newest else None})
        result.append({"label": root.label, "kind": root.kind, "exists": root.path.is_dir(),
                       "at_risk": root.kind == "secondary", "directories": directories})
    return {"roots": result, "warnings": warnings}


# --- 2. run index ------------------------------------------------------------

def _slot_time(name: str) -> tuple[str | None, str | None]:
    """``(slot, iso_time)`` for a ``YYYYMMDD_HHMMET`` directory name, else ``(None, None)``."""
    found = SLOT_DIR_RE.match(name)
    if not found:
        return None, None
    try:
        stamp = datetime(*(int(part) for part in found.groups()), tzinfo=ET)
    except ValueError:
        return None, None
    return f"{found[4]}{found[5]}ET", stamp.isoformat()


def _day_label(state: dict[str, Any], relative_dir: str) -> str | None:
    """Trading day of a run: the state's own day, else the slot/date-shaped directory name."""
    if state:
        day = _day_of(state)
        if day is not None:
            return day.isoformat()
    name = Path(relative_dir).name
    if DATE_DIR_RE.match(name):
        return iso_date(name)
    _, stamp = _slot_time(name)
    return stamp[:10] if stamp else None


def _state_row(root: RootSpec, relative_dir: str, state_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """One index row for a readable ``run_state`` variant, in ``dca_console_data._run_row``'s shape."""
    row = _run_row(state_path, root.path, state, [])
    del row["confirmed_spend_usd"]  # the Runs page never counts fills; None here would read as "no data"
    slot, _ = _slot_time(Path(relative_dir).name)
    if row["origin"] == "unknown" and is_manual_state(state):
        row["origin"] = "manual"
    row.update({
        "id": run_identity(root.label, relative_dir), "root_label": root.label, "relative_dir": relative_dir,
        "slot": slot, "day": _day_label(state, relative_dir), "incomplete": False,
        "at_risk": root.kind == "secondary",
    })
    return row


def _incomplete_row(root: RootSpec, relative_dir: str, files: list[str]) -> dict[str, Any]:
    """A slot- or date-shaped directory with no ``run_state.json``: incomplete, never failed."""
    name = Path(relative_dir).name
    slot, stamp = _slot_time(name)
    parent = str(Path(relative_dir).parent)
    return {
        "id": run_identity(root.label, relative_dir), "stream": _DIR_STREAM.get(parent, "unknown"), "time": stamp,
        "status": "incomplete", "mode": "unknown", "origin": "unknown", "candidates": None, "submitted": None,
        "root_label": root.label, "relative_dir": relative_dir, "slot": slot,
        "day": _day_label({}, relative_dir), "incomplete": True, "at_risk": root.kind == "secondary",
        "files": sorted(_text(item) for item in files[:20]),
    }


def _excluded(path: Path, exclusions: Sequence[Path]) -> bool:
    return any(path.is_relative_to(item) for item in exclusions)


def _index_root(root: RootSpec, exclusions: Sequence[Path], warnings: list[str]) -> list[dict[str, Any]]:
    reader = root.reader(warnings)
    base = root.path.resolve()
    if not base.is_dir():
        _warn(warnings, f"Root {root.label} does not exist; its runs are unavailable.")
        return []
    try:
        paths = find_run_state_files(base)
    except OSError:
        _warn(warnings, f"Root {root.label} could not be scanned.")
        return []
    if len(paths) > MAX_FILES:
        _warn(warnings, f"Root {root.label} exceeds the file scan limit; later run states were not read.")
        paths = paths[:MAX_FILES]
    by_dir: dict[str, tuple[Path, dict[str, Any]]] = {}
    covered: set[str] = set()
    malformed = 0
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(base) or _excluded(resolved, exclusions):
            continue
        relative = resolved.relative_to(base)
        relative_dir = str(relative.parent)
        # A directory that wrote any run_state -- even a corrupt one -- is
        # never "incomplete"; the malformed count below is its report.
        covered.add(relative_dir)
        state = reader.json(relative)
        if not state:
            malformed += 1
            continue
        # Several run_state variants in one directory are snapshots of one run:
        # keep the newest timestamp for presentation, as the daily reader does.
        stamp = (str(run_timestamp_et(state) or ""), path.name)
        current = by_dir.get(relative_dir)
        if current is None or stamp > (str(run_timestamp_et(current[1]) or ""), current[0].name):
            by_dir[relative_dir] = (path, state)
    if malformed:
        _warn(warnings, f"{malformed} unreadable or malformed run-state file(s) under {root.label} were skipped.")
    rows = [_state_row(root, relative_dir, path, state) for relative_dir, (path, state) in by_dir.items()]
    for parent in ("", *_STREAM_DIRS, *_AFTERNOON_DIRS):
        for entry in reader.entries(parent):
            name = entry.name
            relative_dir = str(entry)
            if not (SLOT_DIR_RE.match(name) or DATE_DIR_RE.match(name)) or not (base / entry).is_dir():
                continue
            if _excluded((base / entry).resolve(), exclusions):
                continue
            # A run whose state sits in a nested artifacts/ subfolder is still
            # that directory's run (Jul 27 2026 layout); only a directory with
            # no run_state at or below it is incomplete.
            if any(item == relative_dir or item.startswith(relative_dir + "/") for item in covered):
                continue
            files = [item.name for item in reader.entries(entry, limit=50)]
            rows.append(_incomplete_row(root, relative_dir, files))
    return rows


def build_run_index(roots: Sequence[RootSpec], *, day: date | None = None, month: str | None = None,
                    excluded_roots: Sequence[Path] = ()) -> dict[str, Any]:
    """Every run under every root, newest first: ``{"runs", "recent_days", "warnings"}``.

    Each row carries the daily reader's fields (stream, time, status, mode,
    origin, candidates, submitted) plus ``id`` (stable hash of label and
    relative directory), ``root_label``, ``relative_dir``, ``slot``, ``day``,
    ``incomplete`` and ``at_risk``. ``origin="manual"`` rows are never
    scheduled evidence; ``incomplete=True`` rows are directories that never
    wrote a ``run_state.json`` and are not failures. ``day`` (ISO date) or
    ``month`` (``YYYY-MM``) filter; the result is capped at 500 rows with a
    warning. ``recent_days`` lists every day with a row, newest first.
    """
    warnings: list[str] = []
    exclusions = tuple(Path(item).resolve() for item in excluded_roots)
    rows: list[dict[str, Any]] = []
    for root in roots:
        rows.extend(_index_root(root, exclusions, warnings))
    recent = sorted({row["day"] for row in rows if row["day"]}, reverse=True)[:90]
    if day is not None:
        rows = [row for row in rows if row["day"] == day.isoformat()]
    elif month is not None:
        rows = [row for row in rows if row["day"] and row["day"].startswith(month)]
    rows.sort(key=lambda row: (row["time"] or row["day"] or "", row["relative_dir"]), reverse=True)
    if len(rows) > MAX_RUNS:
        _warn(warnings, f"Only the newest {MAX_RUNS} of {len(rows)} runs are listed; narrow by day or month.")
        rows = rows[:MAX_RUNS]
    return {"runs": rows, "recent_days": recent, "warnings": warnings}


# --- 3. run detail -----------------------------------------------------------

def _identity(state: dict[str, Any], roots: Sequence[RootSpec]) -> dict[str, Any]:
    """Allowlisted ``run_state.json`` keys only; everything else (account, ids, rows) is dropped."""
    out = {key: _json_safe(state[key], roots) for key in IDENTITY_KEYS if key in state}
    for key, value in state.items():
        if any(token in key for token in ("top_up", "reconcil", "foreground")):
            out[key] = _json_safe(value, roots)
    classification = state.get("run_classification")
    if isinstance(classification, dict):
        out["run_classification"] = {key: _json_safe(classification[key], roots)
                                     for key in CLASSIFICATION_KEYS if key in classification}
    return out


def _contract(run_dir: Path, roots: Sequence[RootSpec], warnings: list[str]) -> dict[str, Any] | None:
    from scripts.scheduled_artifacts import validate_run_artifacts

    try:
        result = validate_run_artifacts(run_dir)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        _warn(warnings, "The artifact contract could not be evaluated for this run.")
        return None
    return {"ok": bool(result.ok), "errors": [_text(error, roots) for error in result.errors[:MAX_ROWS]]}


def _drift(run_dir: Path, roots: Sequence[RootSpec], warnings: list[str]) -> dict[str, Any] | None:
    # detect_drift only; send_slack_alert lives in the same module and is never touched here.
    from scripts.drift_detection import detect_drift

    try:
        report = detect_drift(run_dir).to_dict()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, csv.Error, UnicodeError):
        _warn(warnings, "Drift detection could not be evaluated for this run.")
        return None
    report.pop("run_dir", None)
    return _json_safe(report, roots)


def _tables(reader: BoundedReader, relative_dir: str, roots: Sequence[RootSpec]) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for name in TABLE_NAMES:
        source = reader.text(Path(relative_dir) / name)
        if source is None:
            continue
        try:
            parsed = csv.DictReader(io.StringIO(source))
            rows = []
            for row in parsed:
                if len(rows) >= MAX_ROWS:
                    reader.warn(f"{name} shows only its first {MAX_ROWS} rows.")
                    break
                rows.append({key: _text(value, roots) for key, value in row.items()
                             if key in TABLE_COLUMNS and value not in (None, "")})
            columns = [column for column in (parsed.fieldnames or []) if column in TABLE_COLUMNS]
        except (csv.Error, UnicodeError):
            reader.warn(f"{name} is not a readable CSV; its rows are unavailable.")
            continue
        tables[name] = {"columns": columns, "rows": rows}
    return tables


def _post_trade(reader: BoundedReader, relative_dir: str, state: dict[str, Any],
                roots: Sequence[RootSpec]) -> dict[str, Any]:
    from dca_reporting import (
        build_latest_run_summary,
        build_missing_fills,
        compute_slippage_rows,
        fills_from_order_history_payload,
        rank_slippage,
    )

    source = reader.text(Path(relative_dir) / "raw" / "orders_after_place.json")
    if source is None:
        return {"available": False, "reason": "raw/orders_after_place.json is absent"}
    candidates = [item for item in state.get("buy_candidates") or [] if isinstance(item, dict)]
    try:
        fills = fills_from_order_history_payload(json.loads(source))
        slippage = compute_slippage_rows(candidates, fills)
        missing = build_missing_fills(candidates, fills)
        summary = build_latest_run_summary(state, slippage, missing)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {"available": False, "reason": "the persisted order history could not be interpreted"}
    keep = ("ticker", "planned_amount_usd", "buy_multiplier", "target_price", "target_price_missing", "fill_price",
            "filled_qty", "slippage_abs", "slippage_pct")
    return {
        "available": True,
        "summary": {key: _json_safe(summary.get(key), roots) for key in ("coverage", "execution_quality", "budget")},
        "slippage": [{key: _json_safe(row.get(key)) for key in keep} for row in rank_slippage(slippage)[:MAX_ROWS]],
        "missing_fills": [{key: _json_safe(row.get(key)) for key in keep[:4]} for row in missing[:MAX_ROWS]],
    }


def _close_window(reader: BoundedReader, relative_dir: str, state: dict[str, Any], config_dir: Path,
                  tables: dict[str, Any]) -> dict[str, Any]:
    """Grade "what would have fired" from the persisted afternoon signal prices.

    The live path persists each ticker's decision bar close, prior close and
    60-minute lookback price in ``signals.csv``, which is exactly the quote
    contract ``build_close_window_summary`` asks for; the raw ``quotes_batch*``
    files hold broker quotes without the lookback bar, so they only count as
    corroborating inputs here. Multipliers carry the summary's own 3x
    reporting cap and are labelled as such.
    """
    from dca_config import load_per_ticker_sizing, load_sizing_tiers
    from run_dca import _parse_raw_json
    from scripts.close_window_signal import build_close_window_summary

    rows = tables.get("signals.csv", {}).get("rows", [])
    quotes = {}
    for row in rows:
        ticker = row.get("ticker")
        if ticker and row.get("decision_bar_close") and row.get("previous_close"):
            quotes[ticker] = {"decision_price": row["decision_bar_close"], "prior_close": row["previous_close"],
                              "lookback_price": row.get("lookback_price"), "data_source": row.get("source", "")}
    if not quotes:
        return {"available": False, "reason": "signals.csv with decision and prior-close prices is absent"}
    try:
        tiers = load_sizing_tiers(config_dir).get("sizing_tiers")
        per_ticker = {ticker: entry["tiers"] for ticker, entry in load_per_ticker_sizing(config_dir).items()
                      if isinstance(entry, dict) and entry.get("tiers")}
    except (OSError, ValueError, TypeError, KeyError):
        tiers, per_ticker = None, {}
    if not tiers:
        return {"available": False, "reason": "config/sizing_ladder.json tiers are unavailable"}
    raw_quotes = 0
    for entry in reader.entries(Path(relative_dir) / "raw", limit=MAX_ROWS):
        if entry.name.startswith("quotes") and entry.name.endswith(".json"):
            _, payload, error = _parse_raw_json(reader.root / entry)
            raw_quotes += error is None and payload is not None
    trading_day = _day_label(state, relative_dir) or "unknown"
    summary = build_close_window_summary(quotes, tiers=tiers, trading_day=trading_day, per_ticker_tiers=per_ticker)
    safe = _json_safe(summary)
    safe.update({"available": True, "multiplier_label": MULTIPLIER_LABEL, "raw_quote_files": raw_quotes,
                 "source": "signals.csv"})
    return safe


def _inputs(reader: BoundedReader, relative_dir: str) -> list[dict[str, Any]]:
    """Names and sizes of the saved Gate 5 inputs in the run directory (names only, never contents)."""
    found = []
    for entry in reader.entries(relative_dir, limit=MAX_ROWS):
        if entry.name.startswith(INPUT_PREFIXES) and entry.name.endswith(".json"):
            try:
                size = (reader.root / entry).lstat().st_size
            except OSError:
                size = None
            found.append({"name": _text(entry.name), "bytes": size})
    return sorted(found, key=lambda item: item["name"])


def _evidence_gap(state: dict[str, Any]) -> bool:
    guard = state.get("risk_guard") if isinstance(state.get("risk_guard"), dict) else {}
    markers = " ".join(str(state.get(key) or "") for key in ("status", "order_submit_status", "error_class",
                                                               "completion_reason")).lower()
    return guard.get("kill_switch_engaged") is True or "evidence" in markers or "kill_switch" in markers


def _abort_explanation(root: RootSpec, row: dict[str, Any], state: dict[str, Any], roots: Sequence[RootSpec],
                       warnings: list[str]) -> dict[str, Any] | None:
    if row["stream"] != "afternoon" or not state or not _evidence_gap(state):
        return None
    from scripts.gate_failure_diagnostics import diagnose_afternoon_evidence_gap

    as_of = iso_date(row["day"])
    if as_of is None:
        _warn(warnings, "The abort could not be explained: the run carries no trading day.")
        return None
    scanned = [root.path / "ce16ba6b" / "scheduled_runs", root.path / "scheduled_runs", root.path / "intraday_dca"]
    try:
        report = diagnose_afternoon_evidence_gap([str(item) for item in scanned], as_of=date.fromisoformat(as_of))
    except (OSError, ValueError, TypeError, KeyError):
        _warn(warnings, "The evidence-gap diagnostic could not be evaluated for this run.")
        return None
    # The diagnostic echoes the roots it scanned and the process-wide cron root
    # as absolute paths; both are replaced by labels before leaving the process.
    report.pop("cron_tracking_root", None)
    for entry in report.get("roots", []):
        entry["root"] = _text(entry.get("root", ""), roots)
    return _json_safe(report, roots)


def build_run_detail(roots: Sequence[RootSpec], run_id: str, *, config_dir: Path) -> dict[str, Any]:
    """Everything the console can say about one run named by an index id.

    Returns ``{"found": False, "warnings"}`` for a malformed or unknown id.
    Otherwise ``found=True`` with the index ``run`` row, ``identity``
    (allowlisted ``run_state.json`` keys), ``contract`` (artifact contract
    result), ``drift`` (drift report), ``tables`` (allowlisted CSV columns,
    200 rows each), ``post_trade`` (slippage/coverage when
    ``raw/orders_after_place.json`` exists), ``close_window`` (what would have
    fired, ``multiplier_label`` = reporting cap 3x), ``inputs`` (saved Gate 5
    input file names) and ``abort_explanation`` (afternoon evidence-gap
    diagnosis, else None). A section is None or ``available=False`` when its
    inputs are missing; an incomplete run has an empty identity.
    """
    if not RUN_ID_RE.match(run_id or ""):
        return {"found": False, "warnings": ["The run id is not a console run id."]}
    index = build_run_index(roots)
    warnings = list(index["warnings"])
    row = next((item for item in index["runs"] if item["id"] == run_id), None)
    root = next((item for item in roots if row and item.label == row["root_label"]), None)
    if row is None or root is None:
        _warn(warnings, "No run with that id exists under the observed roots.")
        return {"found": False, "warnings": warnings}
    reader = root.reader(warnings)
    relative_dir = row["relative_dir"]
    run_dir = reader.confined(relative_dir)
    state = reader.json(Path(relative_dir) / "run_state.json") if not row["incomplete"] else {}
    if row["incomplete"]:
        _warn(warnings, "This directory never wrote run_state.json; it is incomplete, not failed.")
    tables = _tables(reader, relative_dir, roots)
    return {
        "found": True, "run": row, "identity": _identity(state, roots),
        "contract": _contract(run_dir, roots, warnings) if run_dir else None,
        "drift": _drift(run_dir, roots, warnings) if run_dir else None,
        "tables": tables,
        "post_trade": _post_trade(reader, relative_dir, state, roots),
        "close_window": _close_window(reader, relative_dir, state, Path(config_dir), tables),
        "inputs": _inputs(reader, relative_dir),
        "abort_explanation": _abort_explanation(root, row, state, roots, warnings),
        "warnings": warnings,
    }


# --- 4. pointer strip --------------------------------------------------------

def _pointer_row(root: RootSpec, stream: str, pointer: Path, expected_root: Path, day: date,
                 roots: Sequence[RootSpec], warnings: list[str], *, alias: bool = False) -> dict[str, Any]:
    from scripts.same_day_pointer import STATUS_VALID, STATUS_VALID_RECOVERY_CONTEXT, validate_pointer

    reader = root.reader(warnings)
    relative = pointer.relative_to(root.path)
    outcome = validate_pointer(pointer, expected_stream=stream, expected_root=expected_root, today=day.isoformat())
    target = Path(outcome.run_state_path) if outcome.run_state_path else None
    # The target is only followed when it sits inside the root the pointer was
    # found under; a pointer naming a path elsewhere is reported, never read.
    confined = reader.confined(target.relative_to(root.path)) if target and target.is_relative_to(root.path) else None
    present = confined is not None and confined.is_file()
    state = reader.json(confined.relative_to(root.path)) if present else {}
    evidence = outcome.status == STATUS_VALID and outcome.valid_scheduled_evidence
    if evidence:
        label = "scheduled evidence"
    elif outcome.status == STATUS_VALID_RECOVERY_CONTEXT:
        label = "recovery context, not scheduled evidence"
    elif pointer.is_file() and not present:
        label = "unverified"
    else:
        label = "rejected"
    return {
        "stream": stream, "label": root.label, "at_risk": root.kind == "secondary", "pointer_file": str(relative),
        "alias": alias, "status": outcome.status, "evidence": evidence, "evidence_label": label,
        "reasons": [_text(reason, roots) for reason in outcome.reasons[:20]],
        "trading_day": iso_date(outcome.trading_day), "run_state_present": present,
        "order_submit_status": _text(state.get("order_submit_status")) if state.get("order_submit_status") else None,
    }


def pointer_strip(roots: Sequence[RootSpec], day: date, *, config_dir: Path) -> dict[str, Any]:
    """Validate every stream's ``latest_<stream>_run.json`` under each root for ``day``.

    Returns ``{"streams": [...], "warnings"}``; each row carries the literal
    ``same_day_pointer.STATUS_*`` value, ``evidence`` (True only for
    ``STATUS_VALID`` with ``valid_scheduled_evidence``), an ``evidence_label``
    ("scheduled evidence", "recovery context, not scheduled evidence",
    "unverified" for a pointer whose run directory lacks ``run_state.json``,
    else "rejected"), the validator's reasons, ``run_state_present`` and the
    target's ``order_submit_status``. The afternoon pointer is probed under
    ``ce16ba6b/scheduled_runs`` and the local ``scheduled_runs``; the
    ``morning_dca/latest_morning_live.json`` alias is reported with
    ``alias=True`` when present. ``config_dir`` is accepted for parity with the
    other builders; pointer validation needs no configuration.
    """
    from scripts.run_classification import latest_scheduled_run_path

    warnings: list[str] = []
    rows = []
    for root in roots:
        if not root.path.is_dir():
            _warn(warnings, f"Root {root.label} does not exist; its pointers are unavailable.")
            continue
        for stream, folder in STREAM_ARTIFACT_DIRS.items():
            probes = [(root.path / folder / f"latest_{stream}_run.json", root.path / folder)]
            if stream == "afternoon":
                probes = [(latest_scheduled_run_path(root.path / folder), root.path / folder),
                          (root.path / "scheduled_runs" / "latest_scheduled_run.json", root.path)]
            for pointer, expected_root in probes:
                if pointer.is_file() or expected_root == root.path / folder:
                    rows.append(_pointer_row(root, stream, pointer, expected_root, day, roots, warnings))
        alias = root.path / "morning_dca" / "latest_morning_live.json"
        if alias.is_file():
            rows.append(_pointer_row(root, "morning", alias, root.path / "morning_dca", day, roots, warnings,
                                     alias=True))
    return {"streams": rows, "warnings": warnings}
