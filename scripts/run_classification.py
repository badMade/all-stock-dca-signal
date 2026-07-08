#!/usr/bin/env python3
"""Run-classification contract for the graded DCA live test (task ce16ba6b).

This module is the enforcement layer that keeps *scheduled* live runs of the
graded DCA task from being confused with *manual* runs. It encodes the contract
described in ``config/run_classification.json`` / ``docs/run_classification.md``
and validated by ``schemas/run_classification.schema.json``.

Responsibilities:
  * Build the ``run_classification`` block for a scheduled or manual run.
  * Choose the correct artifact directory per origin.
  * Write run_state artifacts, updating ``latest_scheduled_run.json`` ONLY for
    scheduled runs.
  * Validate a run_state's classification block (required fields, origin/flag
    agreement, run_id patterns).
  * Collect manual order IDs and exclude them from scheduled-live metrics.
  * Select the scheduled state for reporting, applying the narrow broker
    time-window fallback rule.

Standard library only. No secrets, tokens, or network calls. The concrete live
trading code (near_close_dca) should call these helpers rather than
re-implementing the path/pointer/exclusion logic; the reporting/watchdog/weekly
tasks should route every scheduled-vs-manual decision through here.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set
from zoneinfo import ZoneInfo

# --- Contract constants (mirror config/run_classification.json) --------------

STATE_SCHEMA_VERSION = "run-classification-v1"
# Repaired live task id. The deleted ce16ba6b is superseded by 10ce362c; the
# artifact root stays under .../ce16ba6b for continuity (see STATE_ROOT).
CRON_ID = "10ce362c"
SUPERSEDES_CRON_ID = "ce16ba6b"
SCHEDULED_RUN_TYPE = "scheduled_live_test"
SCHEDULED_FOR_ET = "15:20"
SCHEDULED_WINDOW_START_ET = "15:15"
SCHEDULED_WINDOW_END_ET = "15:35"

# Artifact root remains the ce16ba6b path for continuity across the task repair.
STATE_ROOT = "/home/user/workspace/cron_tracking/ce16ba6b"
RUN_STATE_FILENAME = "run_state.json"
LATEST_SCHEDULED_RUN_FILENAME = "latest_scheduled_run.json"

SCHEDULED_RUN_ID_RE = re.compile(r"^scheduled_[0-9]{8}_1520ET$")

# order_submit_status values (mirror schemas/run_state.schema.json). The scheduled
# 3:20 task always writes a run_state (and updates the pointer) even when nothing is
# submitted; this field records why.
ORDER_SUBMIT_STATUS_SUBMITTED = "submitted"
ORDER_SUBMIT_STATUS_NO_CANDIDATES = "not_submitted_no_candidates"
# Robinhood review passed but submission was blocked by required post-review user
# confirmation: valid scheduled-run evidence the task ran, but NOT an order/fill.
ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED = "not_submitted_confirmation_required"
ORDER_SUBMIT_STATUS_ERROR = "not_submitted_error"
ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED = "not_submitted_budget_exhausted"
ORDER_SUBMIT_STATUS_DRY_RUN = "dry_run"


_EASTERN = ZoneInfo("America/New_York")


def _parse_iso_datetime(text: str) -> Optional[datetime]:
    """Parse an ISO-8601 datetime, tolerating a trailing 'Z'. None if not a
    full datetime (a bare 'HH:MM' wall-clock is handled separately)."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _et_minutes(value: object) -> Optional[int]:
    """Minutes-since-midnight in **ET** for an order timestamp.

    Accepts a bare ET wall-clock ('15:20', '15:20:33'), a naive ISO datetime
    (assumed already ET), or a tz-aware ISO string / ``datetime`` — which is
    converted to America/New_York first, so a UTC ``'...T19:20:00Z'`` correctly
    reads as 15:20 ET rather than being dropped. Returns None if unparseable.
    """
    dt = value if isinstance(value, datetime) else _parse_iso_datetime(str(value))
    if dt is not None:
        if dt.tzinfo is not None:
            dt = dt.astimezone(_EASTERN)
        return dt.hour * 60 + dt.minute
    # Bare 'HH:MM[:SS]' wall-clock with no date -> treated as ET.
    match = re.search(r"(\d{1,2}):(\d{2})", str(value))
    if not match:
        return None
    hh, mm = int(match.group(1)), int(match.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh * 60 + mm


_WINDOW_START_MIN = _et_minutes(SCHEDULED_WINDOW_START_ET)  # 15:15
_WINDOW_END_MIN = _et_minutes(SCHEDULED_WINDOW_END_ET)      # 15:35


def _in_scheduled_window_et(submitted_at_et: object) -> bool:
    """True only if an ET timestamp falls within the scheduled window (inclusive)."""
    minutes = _et_minutes(submitted_at_et)
    if minutes is None:
        return False
    return _WINDOW_START_MIN <= minutes <= _WINDOW_END_MIN

SCHEDULED_REQUIRED_FIELDS = (
    "run_origin",
    "run_type",
    "cron_id",
    "scheduled_for_et",
    "scheduled_window_start_et",
    "scheduled_window_end_et",
    "manual_run",
    "run_id",
    "triggered_at_utc",
    "state_schema_version",
)

MANUAL_REQUIRED_FIELDS = (
    "run_origin",
    "manual_run",
    "cron_id",
    "run_id",
    "triggered_at_utc",
    "state_schema_version",
)


# --- Building classification blocks ------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scheduled_run_id(trading_day: str) -> str:
    """'2026-07-06' -> 'scheduled_20260706_1520ET'."""
    compact = trading_day.replace("-", "")
    if not re.fullmatch(r"[0-9]{8}", compact):
        raise ValueError(f"trading_day must be YYYY-MM-DD, got {trading_day!r}")
    return f"scheduled_{compact}_1520ET"


def manual_run_id(triggered_at_utc: Optional[str] = None) -> str:
    """Distinct id that can never match the scheduled pattern."""
    ts = triggered_at_utc or _utc_now_iso()
    compact = re.sub(r"[^0-9]", "", ts)[:14] or "00000000000000"
    return f"manual_{compact[:8]}_{compact[8:14]}Z"


def build_scheduled_classification(
    trading_day: str, triggered_at_utc: Optional[str] = None
) -> Dict[str, object]:
    """Return a fully-populated scheduled run_classification block."""
    return {
        "run_origin": "scheduled",
        "run_type": SCHEDULED_RUN_TYPE,
        "cron_id": CRON_ID,
        "supersedes_cron_id": SUPERSEDES_CRON_ID,
        "scheduled_for_et": SCHEDULED_FOR_ET,
        "scheduled_window_start_et": SCHEDULED_WINDOW_START_ET,
        "scheduled_window_end_et": SCHEDULED_WINDOW_END_ET,
        "manual_run": False,
        "run_id": scheduled_run_id(trading_day),
        "triggered_at_utc": triggered_at_utc or _utc_now_iso(),
        "state_schema_version": STATE_SCHEMA_VERSION,
    }


def build_manual_classification(
    triggered_at_utc: Optional[str] = None, run_id: Optional[str] = None
) -> Dict[str, object]:
    """Return a manual run_classification block with a distinct run_id."""
    ts = triggered_at_utc or _utc_now_iso()
    return {
        "run_origin": "manual",
        "run_type": "manual_test",
        "cron_id": CRON_ID,
        "supersedes_cron_id": SUPERSEDES_CRON_ID,
        "manual_run": True,
        "run_id": run_id or manual_run_id(ts),
        "triggered_at_utc": ts,
        "state_schema_version": STATE_SCHEMA_VERSION,
    }


# --- Path helpers ------------------------------------------------------------

def state_root(base: Optional[Path] = None) -> Path:
    return Path(base) if base is not None else Path(STATE_ROOT)


def scheduled_run_dir(trading_day: str, base: Optional[Path] = None) -> Path:
    compact = trading_day.replace("-", "")
    return state_root(base) / "scheduled_runs" / f"{compact}_1520ET"


def manual_run_dir(timestamp: str, base: Optional[Path] = None) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "", timestamp) or "unknown"
    return state_root(base) / "manual_runs" / safe


def latest_scheduled_run_path(base: Optional[Path] = None) -> Path:
    return state_root(base) / LATEST_SCHEDULED_RUN_FILENAME


# --- Validation --------------------------------------------------------------

@dataclass
class ValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # allow `if validate(...):`
        return self.ok


def validate_classification(rc: Dict[str, object]) -> ValidationResult:
    """Validate a run_classification block against the contract.

    Mirrors run_classification.schema.json but usable at runtime with no
    third-party dependency.
    """
    errors: List[str] = []
    if not isinstance(rc, dict):
        return ValidationResult(False, ["run_classification is missing or not an object"])

    origin = rc.get("run_origin")
    if origin not in ("scheduled", "manual"):
        errors.append(f"run_origin must be 'scheduled' or 'manual', got {origin!r}")

    if rc.get("state_schema_version") != STATE_SCHEMA_VERSION:
        errors.append(
            f"state_schema_version must be {STATE_SCHEMA_VERSION!r}, "
            f"got {rc.get('state_schema_version')!r}"
        )

    required = SCHEDULED_REQUIRED_FIELDS if origin == "scheduled" else MANUAL_REQUIRED_FIELDS
    for f_name in required:
        if rc.get(f_name) in (None, ""):
            errors.append(f"missing required field: {f_name}")

    manual_flag = rc.get("manual_run")
    if origin == "scheduled":
        if manual_flag is not False:
            errors.append("scheduled run must have manual_run=false")
        if rc.get("run_type") != SCHEDULED_RUN_TYPE:
            errors.append(f"scheduled run_type must be {SCHEDULED_RUN_TYPE!r}")
        # Lineage of the repaired live task: accept the current task id OR the
        # superseded-task marker so continuity artifacts stay valid.
        if rc.get("cron_id") != CRON_ID and rc.get("supersedes_cron_id") != SUPERSEDES_CRON_ID:
            errors.append(
                f"scheduled run must have cron_id={CRON_ID!r} "
                f"OR supersedes_cron_id={SUPERSEDES_CRON_ID!r}"
            )
        if rc.get("scheduled_for_et") != SCHEDULED_FOR_ET:
            errors.append(f"scheduled_for_et must be {SCHEDULED_FOR_ET!r}")
        if rc.get("scheduled_window_start_et") != SCHEDULED_WINDOW_START_ET:
            errors.append(f"scheduled_window_start_et must be {SCHEDULED_WINDOW_START_ET!r}")
        if rc.get("scheduled_window_end_et") != SCHEDULED_WINDOW_END_ET:
            errors.append(f"scheduled_window_end_et must be {SCHEDULED_WINDOW_END_ET!r}")
        run_id = rc.get("run_id")
        if not isinstance(run_id, str):
            errors.append(f"scheduled run_id must be a string, got {run_id!r}")
        elif not SCHEDULED_RUN_ID_RE.match(run_id):
            errors.append(f"scheduled run_id must match scheduled_YYYYMMDD_1520ET, got {run_id!r}")
    elif origin == "manual":
        if manual_flag is not True:
            errors.append("manual run must have manual_run=true")
        # Manual runs for this contract must belong to the live task's lineage
        # (same check as scheduled) so a stray manual state for another task
        # cannot be written under ce16ba6b/manual_runs and silently exclude its
        # order IDs from scheduled-live metrics.
        if rc.get("cron_id") != CRON_ID and rc.get("supersedes_cron_id") != SUPERSEDES_CRON_ID:
            errors.append(
                f"manual run must have cron_id={CRON_ID!r} "
                f"OR supersedes_cron_id={SUPERSEDES_CRON_ID!r}"
            )
        run_id = rc.get("run_id")
        if not isinstance(run_id, str):
            errors.append(f"manual run_id must be a string, got {run_id!r}")
        elif SCHEDULED_RUN_ID_RE.match(run_id):
            errors.append("manual run_id must not match the scheduled_YYYYMMDD_1520ET pattern")

    return ValidationResult(not errors, errors)


def is_manual_state(state: Dict[str, object]) -> bool:
    """True if a run_state is manual by EITHER origin or flag (defensive OR)."""
    rc = state.get("run_classification") if isinstance(state, dict) else None
    if not isinstance(rc, dict):
        return False
    return rc.get("run_origin") == "manual" or rc.get("manual_run") is True


def is_scheduled_state(state: Dict[str, object]) -> bool:
    """True only if a run_state is a *valid* scheduled run.

    A manual flag anywhere wins: a state that is both marked scheduled and
    carries manual_run=true is treated as NOT scheduled.
    """
    rc = state.get("run_classification") if isinstance(state, dict) else None
    if not isinstance(rc, dict):
        return False
    if is_manual_state(state):
        return False
    return rc.get("run_origin") == "scheduled" and bool(validate_classification(rc))


def order_submit_status(state: Optional[Dict[str, object]]) -> Optional[str]:
    """Return the run_state's top-level order_submit_status, or None if absent."""
    if not isinstance(state, dict):
        return None
    value = state.get("order_submit_status")
    return value if isinstance(value, str) and value else None


def is_confirmation_blocked(state: Optional[Dict[str, object]]) -> bool:
    """True if the run passed review but submission was blocked awaiting confirmation.

    Such a run is valid scheduled-run evidence that the task fired but carries no
    order/fill: reporting must report zero scheduled fills/order IDs, and neither
    slippage nor the watchdog may mark the scheduled state invalid on this basis.
    """
    return order_submit_status(state) == ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED


# --- Writing artifacts -------------------------------------------------------

def write_run_artifacts(
    run_state: Dict[str, object], base: Optional[Path] = None
) -> Dict[str, str]:
    """Persist a run_state to the correct directory for its origin.

    Scheduled runs also (atomically enough for this backup) update
    latest_scheduled_run.json. Manual runs NEVER touch that pointer.

    Returns a dict of the paths written.
    """
    rc = run_state.get("run_classification")
    result = validate_classification(rc if isinstance(rc, dict) else {})
    if not result:
        raise ValueError("refusing to write invalid run_classification: " + "; ".join(result.errors))

    origin = rc["run_origin"]
    written: Dict[str, str] = {}

    if origin == "scheduled":
        day = str(run_state.get("trading_day", "")).strip()
        out_dir = scheduled_run_dir(day, base)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = out_dir / RUN_STATE_FILENAME
        state_path.write_text(json.dumps(run_state, indent=2) + "\n")
        written["run_state"] = str(state_path)

        pointer = latest_scheduled_run_path(base)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "run_id": rc["run_id"],
                    "trading_day": day,
                    "run_state_path": str(state_path),
                    "cron_id": rc.get("cron_id"),
                    "supersedes_cron_id": rc.get("supersedes_cron_id"),
                    "triggered_at_utc": rc["triggered_at_utc"],
                    "updated_at_utc": _utc_now_iso(),
                    "state_schema_version": STATE_SCHEMA_VERSION,
                },
                indent=2,
            )
            + "\n"
        )
        written["latest_scheduled_run"] = str(pointer)
    else:  # manual
        ts = re.sub(r"[^0-9]", "", str(rc.get("triggered_at_utc", ""))) or rc["run_id"]
        out_dir = manual_run_dir(ts, base)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = out_dir / RUN_STATE_FILENAME
        state_path.write_text(json.dumps(run_state, indent=2) + "\n")
        written["run_state"] = str(state_path)
        # Explicitly NOT updating latest_scheduled_run.json.

    return written


# --- Manual order exclusion --------------------------------------------------

def _iter_run_state_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("run_state*.json"))


def _order_ids_from_state(state: Dict[str, object]) -> Set[str]:
    ids: Set[str] = set()
    for cand in state.get("buy_candidates", []) or []:
        if isinstance(cand, dict):
            oid = cand.get("order_id")
            if isinstance(oid, str) and oid:
                ids.add(oid)
    # Some artifacts also carry a flat orders list.
    for order in state.get("orders", []) or []:
        if isinstance(order, dict):
            oid = order.get("order_id") or order.get("broker_order_id")
            if isinstance(oid, str) and oid:
                ids.add(oid)
    return ids


def _order_ids_from_orders_csv(csv_path: Path) -> Set[str]:
    """Collect order IDs from an execution/slippage CSV's ``order_id`` column."""
    ids: Set[str] = set()
    try:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                oid = (row.get("order_id") or "").strip()
                if oid and oid != "-":
                    ids.add(oid)
    except (OSError, csv.Error, UnicodeDecodeError):
        return set()
    return ids


def _referenced_orders_csvs(state: Dict[str, object], run_dir: Path) -> List[Path]:
    """Resolve the orders CSV(s) a run_state points at or sits beside.

    Manual runs from ``near_close_dca.run_and_report()`` keep their order IDs only
    in the CSV named by ``artifacts.orders_csv_path`` (the state itself has no
    embedded candidates/orders), so scanning embedded fields alone misses them.
    The recorded path may be stale/absolute, so we also try its basename inside
    the run directory and any sibling ``*orders.csv``.
    """
    candidates: List[Path] = []
    artifacts = state.get("artifacts") if isinstance(state, dict) else None
    if isinstance(artifacts, dict):
        recorded = artifacts.get("orders_csv_path")
        if isinstance(recorded, str) and recorded:
            candidates.append(Path(recorded))
            candidates.append(run_dir / Path(recorded).name)
    candidates.extend(sorted(run_dir.glob("*orders.csv")))

    seen: Set[str] = set()
    resolved: List[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        resolved.append(path)
    return resolved


def collect_manual_order_ids(base: Optional[Path] = None) -> Set[str]:
    """Union of every order_id found under manual_runs/.

    Reads both the embedded ``buy_candidates``/``orders`` in each
    ``run_state*.json`` and the ``order_id`` column of any orders CSV the state
    references or sits beside, so manual fills from the heavier
    ``run_and_report`` path are excluded too. These IDs must never appear in
    scheduled-live metrics.
    """
    manual_root = state_root(base) / "manual_runs"
    ids: Set[str] = set()
    for path in _iter_run_state_files(manual_root):
        try:
            state = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ids |= _order_ids_from_state(state)
        for csv_path in _referenced_orders_csvs(state, path.parent):
            ids |= _order_ids_from_orders_csv(csv_path)
    return ids


def exclude_manual_orders(
    order_ids: Iterable[str], base: Optional[Path] = None
) -> List[str]:
    """Filter an iterable of broker order IDs, dropping any manual-run IDs."""
    manual = collect_manual_order_ids(base)
    return [oid for oid in order_ids if oid not in manual]


# --- Scheduled-state selection for reporting ---------------------------------

@dataclass
class ScheduledSelection:
    state: Optional[Dict[str, object]]
    state_path: Optional[str]
    order_ids: List[str]
    used_broker_fallback: bool
    fallback_reason: Optional[str]
    excluded_manual_order_ids: List[str]
    order_submit_status: Optional[str] = None
    confirmation_blocked: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state is not None and not self.errors


def select_scheduled_state_for_reporting(
    trading_day: str,
    base: Optional[Path] = None,
    broker_window_orders: Optional[Iterable[Mapping[str, object]]] = None,
    scheduled_state_write_failed: bool = False,
) -> ScheduledSelection:
    """Pick the scheduled run_state for a trading day and derive its order IDs.

    Enforces the reporting rules:
      * Only a validated scheduled run_state may be used.
      * Manual order IDs are always excluded.
      * Never infer scheduled status from broker orders alone.
      * Broker time-window fallback is allowed ONLY when a valid scheduled
        state exists but lacks order IDs due to a known write failure; even
        then manual order IDs are excluded AND only broker orders whose ET
        timestamp lands inside the 15:15-15:35 scheduled window are attributed.

    ``broker_window_orders`` items are mappings carrying at least ``order_id``
    (or ``broker_order_id``) and ``submitted_at_et``: a bare ET wall-clock
    (``"15:20"``), a naive ET ISO timestamp, or a tz-aware ISO timestamp /
    ``datetime`` (converted to ET before the window compare, so UTC order history
    is handled correctly). Orders with no parseable in-window time are dropped
    rather than attributed.
    """
    manual_ids = collect_manual_order_ids(base)
    out_dir = scheduled_run_dir(trading_day, base)
    state_path = out_dir / RUN_STATE_FILENAME

    if not state_path.exists():
        return ScheduledSelection(
            state=None,
            state_path=None,
            order_ids=[],
            used_broker_fallback=False,
            fallback_reason=None,
            excluded_manual_order_ids=sorted(manual_ids),
            errors=[f"no scheduled run_state at {state_path}"],
        )

    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return ScheduledSelection(
            state=None,
            state_path=str(state_path),
            order_ids=[],
            used_broker_fallback=False,
            fallback_reason=None,
            excluded_manual_order_ids=sorted(manual_ids),
            errors=[f"could not read scheduled run_state: {exc}"],
        )

    if not is_scheduled_state(state):
        rc = state.get("run_classification", {}) if isinstance(state, dict) else {}
        detail = validate_classification(rc if isinstance(rc, dict) else {}).errors
        return ScheduledSelection(
            state=None,
            state_path=str(state_path),
            order_ids=[],
            used_broker_fallback=False,
            fallback_reason=None,
            excluded_manual_order_ids=sorted(manual_ids),
            errors=["run_state is not a valid scheduled run"] + detail,
        )

    scheduled_ids = _order_ids_from_state(state)
    kept = [oid for oid in sorted(scheduled_ids) if oid not in manual_ids]

    oss = order_submit_status(state)
    confirmation_blocked = oss == ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED

    used_fallback = False
    fallback_reason: Optional[str] = None
    # A confirmation-blocked run has no orders by design (review passed but
    # submission awaited confirmation), so it is not a write failure and must
    # never trigger the broker time-window fallback.
    if (
        not kept
        and scheduled_state_write_failed
        and broker_window_orders is not None
        and not confirmation_blocked
    ):
        # Valid scheduled state exists but has no usable order IDs due to a known
        # write failure: attribute broker orders that fall inside the scheduled
        # ET window, still excluding manual IDs.
        used_fallback = True
        fallback_reason = "scheduled state present but order IDs missing (known write failure)"
        seen_fallback: Set[str] = set()
        kept = []
        for order in broker_window_orders:
            if not isinstance(order, Mapping):
                continue
            oid = order.get("order_id") or order.get("broker_order_id")
            if not (isinstance(oid, str) and oid) or oid in manual_ids or oid in seen_fallback:
                continue
            if not _in_scheduled_window_et(order.get("submitted_at_et")):
                continue
            seen_fallback.add(oid)
            kept.append(oid)

    return ScheduledSelection(
        state=state,
        state_path=str(state_path),
        order_ids=kept,
        used_broker_fallback=used_fallback,
        fallback_reason=fallback_reason,
        excluded_manual_order_ids=sorted(manual_ids),
        order_submit_status=oss,
        confirmation_blocked=confirmation_blocked,
        errors=[],
    )


__all__ = [
    "STATE_SCHEMA_VERSION",
    "CRON_ID",
    "SUPERSEDES_CRON_ID",
    "ORDER_SUBMIT_STATUS_SUBMITTED",
    "ORDER_SUBMIT_STATUS_NO_CANDIDATES",
    "ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED",
    "ORDER_SUBMIT_STATUS_ERROR",
    "ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED",
    "ORDER_SUBMIT_STATUS_DRY_RUN",
    "order_submit_status",
    "is_confirmation_blocked",
    "build_scheduled_classification",
    "build_manual_classification",
    "scheduled_run_id",
    "manual_run_id",
    "scheduled_run_dir",
    "manual_run_dir",
    "latest_scheduled_run_path",
    "validate_classification",
    "is_manual_state",
    "is_scheduled_state",
    "write_run_artifacts",
    "collect_manual_order_ids",
    "exclude_manual_orders",
    "select_scheduled_state_for_reporting",
    "ScheduledSelection",
    "ValidationResult",
]
