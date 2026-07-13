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
# Stricter successor introduced 2026-07-10 for the pre-authorized direct-placement
# live task (no review_equity_order): a scheduled run written under the
# pre-authorized execution policy tags itself v2. Both versions are accepted for
# scheduled/manual evidence as long as the required scheduled identity fields are
# correct; v2 is treated as a stricter successor of v1, not a different contract.
STATE_SCHEMA_VERSION_V2 = "run-classification-v2-preauthorized"
ACCEPTED_STATE_SCHEMA_VERSIONS = (STATE_SCHEMA_VERSION, STATE_SCHEMA_VERSION_V2)
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
# Parent directory holding every per-task cron_tracking tree (ce16ba6b, morning_dca,
# midday_dca, final_manual_review, ...). Used to reach the out-of-task manual-exclusion
# roots below.
CRON_TRACKING_ROOT = "/home/user/workspace/cron_tracking"
RUN_STATE_FILENAME = "run_state.json"
LATEST_SCHEDULED_RUN_FILENAME = "latest_scheduled_run.json"

# Final manual buy override (Jul 13 2026 final-buy behavior invoked manually). These
# runs are manual by contract: run_origin='manual', manual_run=true, and a distinct
# run_type so slippage/watchdog/weekly reporting exclude them from every scheduled
# metric and surface them only in a "Manual Final Buy Override / Same-Day Top-Up
# Context" section.
FINAL_MANUAL_OVERRIDE_RUN_TYPE = "final_manual_buy_override"
FINAL_MANUAL_OVERRIDE_DIR_PREFIX = "final_manual_override_"

# Manual-exclusion roots beyond ce16ba6b/manual_runs. The 2026-07-13 final manual buy
# override backfilled live artifacts under midday_dca/manual_reruns/final_manual_override_*
# and a final_manual_review/** tree, both OUTSIDE the ce16ba6b task root. Slippage
# (67aa802b), the watchdog (a46bf007), and weekly/monthly reporting must treat every
# run_state/orders CSV under these roots as manual and exclude their order IDs from all
# scheduled-live metrics. Paths are relative to CRON_TRACKING_ROOT.
MANUAL_EXCLUSION_SUBROOTS = (
    "final_manual_review",
    "midday_dca/manual_reruns",
)

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


def build_final_manual_override_classification(
    triggered_at_utc: Optional[str] = None, run_id: Optional[str] = None
) -> Dict[str, object]:
    """Return a manual run_classification block for a final manual buy override.

    The final-buy behavior (full active-universe scan, trend filter ignored for the
    final-buy decision only) may be invoked manually after the scheduled 3:20 PM run.
    Such a run is manual by contract and additionally flags itself as NOT valid
    afternoon scheduled evidence and same-day top-up context only, so downstream
    reporting shows it only in the Manual Final Buy Override / Same-Day Top-Up Context
    section and never in any scheduled-live metric.
    """
    ts = triggered_at_utc or _utc_now_iso()
    block = build_manual_classification(ts, run_id)
    block["run_type"] = FINAL_MANUAL_OVERRIDE_RUN_TYPE
    block["valid_afternoon_scheduled_evidence"] = False
    block["same_day_top_up_context_only"] = True
    return block


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


def cron_tracking_root(base: Optional[Path] = None) -> Path:
    """Parent directory holding every per-task cron_tracking tree.

    With an explicit ``base`` (tests point ``base`` at a temporary ce16ba6b root) this
    is ``base.parent``; otherwise the canonical ``CRON_TRACKING_ROOT``. Used to reach
    the out-of-task manual-exclusion roots (``final_manual_review``,
    ``midday_dca/manual_reruns``).
    """
    if base is not None:
        return Path(base).parent
    return Path(CRON_TRACKING_ROOT)


def manual_exclusion_roots(base: Optional[Path] = None) -> List[Path]:
    """Every directory scanned for manual order IDs.

    Always includes the ce16ba6b ``manual_runs/`` tree (which holds
    ``final_manual_override_*`` subdirs). Adds the out-of-task roots that the
    2026-07-13 final manual buy override introduced — ``final_manual_review/**`` and
    ``midday_dca/manual_reruns/**`` (incl. ``final_manual_override_*``) — so their
    order IDs are excluded from every scheduled-live metric even though they live
    outside the ce16ba6b task root.
    """
    roots = [state_root(base) / "manual_runs"]
    parent = cron_tracking_root(base)
    for sub in MANUAL_EXCLUSION_SUBROOTS:
        roots.append(parent / sub)
    return roots


# --- Raw connector-input persistence (afternoon escalation fix, 2026-07-10) --
#
# The 3:20 PM scheduled live task escalated on 2026-07-10 because quotes and
# historicals were fetched successfully but the raw connector responses were
# never persisted to disk before signal computation; the bash compute step had
# no quotes_batchN.json / historicals_batchN.json to read. Contract from that
# date on: every raw Robinhood/Finance/Alpaca/Yahoo connector response used by a
# scheduled run is persisted (normalized to parseable JSON) under the run's
# raw/ directory BEFORE any signal is computed, and compute reads only those
# persisted files -- never in-memory tool responses.

RAW_INPUTS_SUBDIR = "raw"

# Deterministic raw-input filenames written before signal computation.
RAW_TRADABILITY_FILENAME = "tradability_pre_place.json"
RAW_PORTFOLIO_FILENAME = "portfolio_pre_place.json"
RAW_ORDERS_AFTER_PLACE_FILENAME = "orders_after_place.json"


def raw_input_dir(trading_day: str, base: Optional[Path] = None) -> Path:
    """Directory holding a scheduled run's persisted raw connector responses."""
    return scheduled_run_dir(trading_day, base) / RAW_INPUTS_SUBDIR


def quotes_batch_filename(n: int) -> str:
    """Deterministic filename for a persisted quotes batch (1-based)."""
    return f"quotes_batch{int(n)}.json"


def historicals_batch_filename(n: int) -> str:
    """Deterministic filename for a persisted historicals batch (1-based)."""
    return f"historicals_batch{int(n)}.json"


def place_filename(symbol: str) -> str:
    """Deterministic filename for a persisted per-symbol place response."""
    safe = re.sub(r"[^0-9A-Za-z._-]", "", str(symbol).upper()) or "UNKNOWN"
    return f"place_{safe}.json"


def normalize_connector_payload(
    payload: object, *, read_pointer: bool = True, base_dir: Optional[Path] = None
) -> Dict[str, object]:
    """Normalize a raw connector response into an inline, parseable JSON object.

    Some connectors return a large result indirectly as
    ``{"output_path": "/.../foo.json", ...}`` rather than inline data. Compute
    must read from persisted, parseable files -- not chase pointers at compute
    time -- so when a payload carries an ``output_path`` we resolve it to the
    referenced file's parsed contents (embedded under ``data``) while preserving
    the original pointer under ``output_path`` and flagging ``normalized=true``.

    A pointer that cannot be read/parsed is left as-is with
    ``normalized=false`` and a ``normalize_error`` note, so persistence still
    succeeds and the failure is visible rather than silently dropped.
    """
    if not isinstance(payload, Mapping):
        return {"data": payload, "normalized": False}

    result: Dict[str, object] = dict(payload)
    pointer = result.get("output_path")
    if not (read_pointer and isinstance(pointer, str) and pointer):
        if isinstance(pointer, str) and pointer:
            # Has an output_path but caller asked not to resolve it — flag as not normalized.
            result.setdefault("normalized", False)
        return result

    path = Path(pointer)
    if base_dir is not None and not path.is_absolute():
        path = Path(base_dir) / path
    try:
        result["data"] = json.loads(path.read_text())
        result["normalized"] = True
    except (OSError, json.JSONDecodeError) as exc:
        result["normalized"] = False
        result["normalize_error"] = f"could not read output_path {pointer!r}: {exc}"
    return result


def save_raw_input(
    trading_day: str,
    filename: str,
    payload: object,
    *,
    base: Optional[Path] = None,
    normalize: bool = True,
) -> Path:
    """Persist one raw connector response under the run's raw/ directory.

    Returns the written path. ``filename`` must be one of the deterministic
    names (``quotes_batchN.json``, ``historicals_batchN.json``,
    ``tradability_pre_place.json``, ``portfolio_pre_place.json``,
    ``orders_after_place.json``, ``place_SYMBOL.json``). Large connector
    ``output_path`` pointers are normalized to inline JSON by default so the
    compute step can parse the file directly.
    """
    safe = Path(str(filename)).name
    if not safe or safe != str(filename):
        raise ValueError(f"raw input filename must be a bare filename, got {filename!r}")
    out_dir = raw_input_dir(trading_day, base)
    out_dir.mkdir(parents=True, exist_ok=True)
    to_write = normalize_connector_payload(payload) if normalize else payload
    path = out_dir / safe
    path.write_text(json.dumps(to_write, indent=2) + "\n")
    return path


def list_raw_inputs_saved(trading_day: str, base: Optional[Path] = None) -> List[str]:
    """Sorted filenames actually persisted under the run's raw/ directory."""
    out_dir = raw_input_dir(trading_day, base)
    if not out_dir.exists():
        return []
    return sorted(p.name for p in out_dir.iterdir() if p.is_file())


def build_raw_persistence_state(
    trading_day: str,
    *,
    base: Optional[Path] = None,
    recovery_from_escalation: bool = False,
) -> Dict[str, object]:
    """Build the run_state fields recording raw-input persistence for a run.

    Reflects what is actually on disk under raw/ at call time, so the run_state
    cannot claim inputs were saved that were not.
    """
    saved = list_raw_inputs_saved(trading_day, base)
    return {
        "raw_input_dir": str(raw_input_dir(trading_day, base)),
        "raw_inputs_saved": saved,
        "recovery_from_escalation": bool(recovery_from_escalation),
    }


def raw_persistence_failure_run_state(
    trading_day: str,
    error: str,
    *,
    base: Optional[Path] = None,
    triggered_at_utc: Optional[str] = None,
    broker: str = "robinhood",
    budget: Optional[Dict[str, object]] = None,
    recovery_from_escalation: bool = False,
) -> Dict[str, object]:
    """Build a valid scheduled run_state for a raw-persistence/normalization failure.

    Contract: if persisting or normalizing raw connector responses fails, the
    task must still write a valid scheduled run_state (``run_origin='scheduled'``,
    ``manual_run=false``) recording the error and placing **no** orders, rather
    than escalating with no state at all. The returned dict satisfies
    run_state.schema.json (required run_id/generated_at/trading_day/broker/
    budget/counts) and carries ``order_submit_status='not_submitted_error'``.
    """
    ts = triggered_at_utc or _utc_now_iso()
    return {
        "run_id": scheduled_run_id(trading_day),
        "generated_at": datetime.now(_EASTERN).strftime("%Y-%m-%dT%H:%M:%S"),
        "trading_day": trading_day,
        "broker": broker,
        "order_submit_status": ORDER_SUBMIT_STATUS_ERROR,
        "budget": budget or {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 0},
        "counts": {"evaluated": 0, "buy_candidates": 0, "submitted": 0},
        "buy_candidates": [],
        "run_classification": build_scheduled_classification(trading_day, ts),
        "raw_input_dir": str(raw_input_dir(trading_day, base)),
        "raw_inputs_saved": [],
        "recovery_from_escalation": bool(recovery_from_escalation),
        "raw_persistence_error": str(error),
    }


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

    if rc.get("state_schema_version") not in ACCEPTED_STATE_SCHEMA_VERSIONS:
        errors.append(
            f"state_schema_version must be one of {list(ACCEPTED_STATE_SCHEMA_VERSIONS)!r}, "
            f"got {rc.get('state_schema_version')!r}"
        )

    required = SCHEDULED_REQUIRED_FIELDS if origin == "scheduled" else MANUAL_REQUIRED_FIELDS
    for f_name in required:
        if rc.get(f_name) in (None, ""):
            errors.append(f"missing required field: {f_name}")

    # Lineage values must be canonical. A supersedes marker, when present, must be
    # exactly SUPERSEDES_CRON_ID; placeholder/ambiguous values such as
    # 'ce16ba6b_replacement' are forbidden regardless of origin.
    cron_id = rc.get("cron_id")
    if cron_id == "ce16ba6b_replacement":
        errors.append(
            "cron_id cannot be 'ce16ba6b_replacement'; placeholder/ambiguous values are forbidden"
        )

    supersedes = rc.get("supersedes_cron_id")
    if supersedes not in (None, SUPERSEDES_CRON_ID):
        errors.append(
            f"supersedes_cron_id must be {SUPERSEDES_CRON_ID!r} or absent; "
            f"placeholder/ambiguous values like 'ce16ba6b_replacement' are forbidden, "
            f"got {supersedes!r}"
        )

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


def is_preauthorized_v2(state_or_rc: Dict[str, object]) -> bool:
    """True if a run_state (or its run_classification block) is tagged v2-preauthorized."""
    rc = state_or_rc
    if isinstance(state_or_rc, dict) and isinstance(state_or_rc.get("run_classification"), dict):
        rc = state_or_rc["run_classification"]
    return isinstance(rc, dict) and rc.get("state_schema_version") == STATE_SCHEMA_VERSION_V2


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

def update_latest_scheduled_run_pointer(
    run_state: Dict[str, object], run_state_path: str | Path, base: Optional[Path] = None
) -> Path:
    """Write/refresh latest_scheduled_run.json to point at an already-written scheduled run_state.

    Extracted from :func:`write_run_artifacts` so a caller that writes
    run_state.json to its own already-correct artifacts directory (e.g.
    near_close_dca's live path, whose ``artifacts_dir`` the scheduler already
    points at the canonical ``scheduled_runs/...`` directory per
    config/scheduled_tasks.json) can update the pointer without a second,
    potentially differently-located copy of run_state.json being written.
    Callers must validate ``run_state["run_classification"]`` themselves first.
    """
    rc = run_state.get("run_classification") or {}
    day = str(run_state.get("trading_day", "")).strip()
    pointer = latest_scheduled_run_path(base)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "run_id": rc.get("run_id"),
                "trading_day": day,
                "run_state_path": str(run_state_path),
                "cron_id": rc.get("cron_id"),
                "supersedes_cron_id": rc.get("supersedes_cron_id"),
                "triggered_at_utc": rc.get("triggered_at_utc"),
                "updated_at_utc": _utc_now_iso(),
                "state_schema_version": STATE_SCHEMA_VERSION,
            },
            indent=2,
        )
        + "\n"
    )
    return pointer


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

        pointer = update_latest_scheduled_run_pointer(run_state, state_path, base)
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
    """Union of every order_id found under any manual-exclusion root.

    Scans the ce16ba6b ``manual_runs/`` tree plus the out-of-task
    ``final_manual_review/**`` and ``midday_dca/manual_reruns/**`` roots (see
    :func:`manual_exclusion_roots`), so final manual buy overrides and midday manual
    reruns are excluded too. Reads both the embedded ``buy_candidates``/``orders`` in
    each ``run_state*.json`` and the ``order_id`` column of any orders CSV the state
    references or sits beside (e.g. a final override's ``filled_orders.csv``), so
    manual fills from the heavier ``run_and_report`` path are excluded as well. These
    IDs must never appear in scheduled-live metrics.
    """
    ids: Set[str] = set()
    for manual_root in manual_exclusion_roots(base):
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
    if confirmation_blocked:
        # By contract a confirmation-blocked run has zero scheduled orders. A
        # malformed/stale artifact could still carry a stray order_id (e.g. a
        # partial write before the status field was finalized); never let that
        # leak through as scheduled order-id evidence.
        kept = []

    used_fallback = False
    fallback_reason: Optional[str] = None
    # A confirmation-blocked run (and any other explicit no-submit status) has
    # no orders by design, so it is not a write failure and must never trigger
    # the broker time-window fallback.
    if (
        not kept
        and scheduled_state_write_failed
        and broker_window_orders is not None
        and oss not in {
            ORDER_SUBMIT_STATUS_NO_CANDIDATES,
            ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
            ORDER_SUBMIT_STATUS_ERROR,
            ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
            ORDER_SUBMIT_STATUS_DRY_RUN,
        }
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
    "STATE_SCHEMA_VERSION_V2",
    "ACCEPTED_STATE_SCHEMA_VERSIONS",
    "is_preauthorized_v2",
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
    "build_final_manual_override_classification",
    "FINAL_MANUAL_OVERRIDE_RUN_TYPE",
    "FINAL_MANUAL_OVERRIDE_DIR_PREFIX",
    "MANUAL_EXCLUSION_SUBROOTS",
    "CRON_TRACKING_ROOT",
    "scheduled_run_id",
    "manual_run_id",
    "scheduled_run_dir",
    "manual_run_dir",
    "latest_scheduled_run_path",
    "cron_tracking_root",
    "manual_exclusion_roots",
    "RAW_INPUTS_SUBDIR",
    "RAW_TRADABILITY_FILENAME",
    "RAW_PORTFOLIO_FILENAME",
    "RAW_ORDERS_AFTER_PLACE_FILENAME",
    "raw_input_dir",
    "quotes_batch_filename",
    "historicals_batch_filename",
    "place_filename",
    "normalize_connector_payload",
    "save_raw_input",
    "list_raw_inputs_saved",
    "build_raw_persistence_state",
    "raw_persistence_failure_run_state",
    "validate_classification",
    "is_manual_state",
    "is_scheduled_state",
    "write_run_artifacts",
    "update_latest_scheduled_run_pointer",
    "collect_manual_order_ids",
    "exclude_manual_orders",
    "select_scheduled_state_for_reporting",
    "ScheduledSelection",
    "ValidationResult",
]
