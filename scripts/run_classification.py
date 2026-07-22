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
from datetime import datetime, time, timezone
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
    "morning_dca/manual_recovery",
    "midday_dca/manual_recovery",
    "midday_dca/manual_runs",
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
    """Return a fully-populated scheduled run_classification block.

    Stamps ``stream="afternoon"`` (this builder is the 15:20 ET scheduled live
    task) so ``scripts.same_day_pointer.select_same_day_run_state``'s
    direct-resolution path -- which fails closed when a target's own
    ``run_classification.stream`` is absent -- accepts a real scheduled
    afternoon run_state instead of rejecting it for a missing field (Codex
    review: "Accept the valid afternoon dated state").
    """
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
        "stream": "afternoon",
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
    afternoon scheduled evidence, not valid scheduled evidence at all, and same-day
    top-up context only, so downstream reporting shows it only in the Manual Final
    Buy Override / Same-Day Top-Up Context section and never in any scheduled-live
    metric. ``valid_scheduled_evidence=False`` is required for
    :func:`is_top_up_context_state` to admit it as prior-allocation context for the
    same-day top-up guard (Codex review: "Admit final-manual overrides as top-up
    context") -- without it, a real final-manual-override artifact built by this
    function was rejected before its CSV fills were ever read.
    """
    ts = triggered_at_utc or _utc_now_iso()
    block = build_manual_classification(ts, run_id)
    block["run_type"] = FINAL_MANUAL_OVERRIDE_RUN_TYPE
    block["valid_afternoon_scheduled_evidence"] = False
    block["valid_scheduled_evidence"] = False
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

    A pointer that cannot be read/parsed raises ``RuntimeError`` so the
    caller knows immediately and cannot silently persist a broken payload.
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
        result["data"] = json.loads(path.read_text(encoding="utf-8"))
        result["normalized"] = True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"normalize_connector_payload: could not read output_path {pointer!r}: {exc}"
        ) from exc
    return result


def save_raw_input(
    trading_day: str,
    filename: str,
    payload: object,
    *,
    base: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
    normalize: bool = True,
) -> Path:
    """Persist one raw connector response under the run's raw/ directory.

    Returns the written path. ``filename`` must be one of the deterministic
    names (``quotes_batchN.json``, ``historicals_batchN.json``,
    ``tradability_pre_place.json``, ``portfolio_pre_place.json``,
    ``orders_after_place.json``, ``place_SYMBOL.json``). Large connector
    ``output_path`` pointers are normalized to inline JSON by default so the
    compute step can parse the file directly.

    ``raw_dir`` overrides the default ``raw_input_dir(trading_day, base)``
    computation, so callers can pin raw inputs directly under their
    ``artifacts_dir / "raw"`` regardless of directory layout.

    Fails closed when normalization of an ``output_path`` pointer fails:
    raises ``ValueError`` and writes nothing, rather than persisting a
    ``{"normalized": false, "normalize_error": ...}`` placeholder that the
    compute step -- which reads only the persisted file, never the original
    in-memory payload -- would silently treat as valid input. A payload with
    no pointer to resolve (plain inline data) is not a failure and persists
    normally.
    """
    safe = Path(str(filename)).name
    if not safe or safe != str(filename):
        raise ValueError(f"raw input filename must be a bare filename, got {filename!r}")
    try:
        to_write = normalize_connector_payload(payload) if normalize else payload
    except RuntimeError as exc:
        raise ValueError(
            f"failed to persist {filename}: {exc}; refusing to "
            "silently write an unnormalized raw connector payload"
        ) from exc
    out_dir = raw_dir if raw_dir is not None else raw_input_dir(trading_day, base)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / safe
    path.write_text(json.dumps(to_write, indent=2) + "\n", encoding="utf-8")
    return path


def list_raw_inputs_saved(
    trading_day: str,
    base: Optional[Path] = None,
    *,
    raw_dir: Optional[Path] = None,
) -> List[str]:
    """Sorted filenames actually persisted under the run's raw/ directory."""
    out_dir = raw_dir if raw_dir is not None else raw_input_dir(trading_day, base)
    if not out_dir.exists():
        return []
    return sorted(p.name for p in out_dir.iterdir() if p.is_file())


def build_raw_persistence_state(
    trading_day: str,
    *,
    base: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
    recovery_from_escalation: bool = False,
) -> Dict[str, object]:
    """Build the run_state fields recording raw-input persistence for a run.

    Reflects what is actually on disk under raw/ at call time, so the run_state
    cannot claim inputs were saved that were not.

    ``raw_dir`` overrides the default ``raw_input_dir`` computation; pass the
    same value as the corresponding ``save_raw_input`` calls.
    """
    out_dir = raw_dir if raw_dir is not None else raw_input_dir(trading_day, base)
    saved = list_raw_inputs_saved(trading_day, base, raw_dir=raw_dir)
    return {
        "raw_input_dir": str(out_dir),
        "raw_inputs_saved": saved,
        "recovery_from_escalation": bool(recovery_from_escalation),
    }


def raw_persistence_failure_run_state(
    trading_day: str,
    error: str,
    *,
    base: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
    triggered_at_utc: Optional[str] = None,
    generated_at: Optional[str] = None,
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

    ``raw_dir`` overrides the default ``raw_input_dir`` computation; pass the
    same value as the corresponding ``save_raw_input`` calls.

    ``generated_at`` pins the run timestamp for replays/backfills where ``now``
    is the scheduled fire time; when omitted, falls back to the wall clock.
    ``dca_risk.run_timestamp_et()`` reads ``generated_at`` before the
    classification timestamp, so passing the scheduled ``now`` here prevents
    a failure state from being attributed to the wrong time window.
    """
    out_dir = raw_dir if raw_dir is not None else raw_input_dir(trading_day, base)
    ts = triggered_at_utc or _utc_now_iso()
    _generated_at = generated_at or datetime.now(_EASTERN).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "run_id": scheduled_run_id(trading_day),
        "generated_at": _generated_at,
        "trading_day": trading_day,
        "broker": broker,
        "order_submit_status": ORDER_SUBMIT_STATUS_ERROR,
        "budget": budget or {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 0},
        "counts": {"evaluated": 0, "buy_candidates": 0, "submitted": 0},
        "buy_candidates": [],
        "run_classification": build_scheduled_classification(trading_day, ts),
        "raw_input_dir": str(out_dir),
        "raw_inputs_saved": [],
        "recovery_from_escalation": bool(recovery_from_escalation),
        "raw_persistence_error": str(error),
    }


# --- Automatic failure recovery for scheduled live tasks (2026-07-15) --------
#
# The three scheduled LIVE Robinhood execution tasks (morning 42173154, midday
# a6239dae, afternoon 10ce362c) treat a mid-run failure caused by internal
# persistence, parsing, wrapper-normalization, transient connector throttling,
# or raw-file write problems -- occurring AFTER data was already fetched -- as
# a recovery trigger rather than a terminal stop: rerun the required read-only
# inputs once, persist all raw responses first, recompute only from the
# persisted files, and continue through the same guarded path if every
# safeguard still passes (see failure_recovery_policy in
# config/scheduled_tasks.json and docs/run_classification.md#recovery-reruns).
# These helpers are the runtime counterpart of that policy: the orchestrator
# calls build_recovery_state() to stamp a successful recovery rerun's
# run_state, or recovery_escalation_run_state() when the single recovery
# attempt also fails or a safety/account/order rejection occurs.

RECOVERY_TRIGGER_REASONS = (
    "internal_persistence_failure",
    "parsing_failure",
    "wrapper_normalization_failure",
    "transient_connector_throttling",
    "raw_file_write_failure",
)


def _validate_recovery_trigger_reason(trigger_reason: str) -> None:
    if trigger_reason not in RECOVERY_TRIGGER_REASONS:
        raise ValueError(
            f"trigger_reason must be one of {RECOVERY_TRIGGER_REASONS}, got {trigger_reason!r}"
        )


def build_recovery_state(
    trigger_reason: str,
    *,
    persisted_raw_first: bool,
    recomputed_from_persisted: bool,
    attempt: int = 1,
) -> Dict[str, object]:
    """Build the run_state fields recording a successful automatic recovery rerun.

    Merge the returned dict into the run_state produced by the recovery attempt
    (it keeps `run_origin='scheduled'` via the run's normal classification block
    -- a recovery rerun is never reclassified as manual). ``trigger_reason`` must
    be one of RECOVERY_TRIGGER_REASONS, kept in sync with
    ``failure_recovery_policy.recovery_triggers`` in config/scheduled_tasks.json
    and the ``recovery.trigger_reason`` enum in schemas/run_state.schema.json.
    Only one recovery attempt is permitted per scheduled run, so ``attempt`` must
    be 1; the schema's top-level ``if/then`` requires this ``recovery`` block
    whenever ``recovery_rerun=true``.
    """
    _validate_recovery_trigger_reason(trigger_reason)
    if attempt != 1:
        raise ValueError(f"only one recovery attempt is permitted per scheduled run, got attempt={attempt!r}")
    return {
        "recovery_rerun": True,
        "recovery": {
            "trigger_reason": trigger_reason,
            "attempt": attempt,
            "persisted_raw_first": bool(persisted_raw_first),
            "recomputed_from_persisted": bool(recomputed_from_persisted),
            "escalated": False,
            "blocking_reason": None,
        },
    }


def recovery_escalation_run_state(
    trading_day: str,
    trigger_reason: str,
    blocking_reason: str,
    *,
    persisted_raw_first: bool = False,
    recomputed_from_persisted: bool = False,
    triggered_at_utc: Optional[str] = None,
    generated_at: Optional[str] = None,
    broker: str = "robinhood",
    budget: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Build a valid scheduled run_state for an escalated recovery attempt.

    Contract: if the single recovery attempt also fails, or a safety/account/
    order rejection occurs, the task writes run_state.json recording the
    blocking reason and escalates -- it does not retry further or drop to an
    interactive/manual path (see failure_recovery_policy.escalation in
    config/scheduled_tasks.json). The returned dict satisfies
    run_state.schema.json (required run_id/generated_at/trading_day/broker/
    budget/counts, plus recovery -- required because recovery_rerun=true) and
    carries ``order_submit_status='not_submitted_error'``.

    ``persisted_raw_first``/``recomputed_from_persisted`` default to False (the
    recovery attempt itself failed before completing those steps); pass True
    for a rejection that occurred after a successful recompute.

    ``generated_at`` pins the run timestamp for replays/backfills where ``now``
    is the scheduled fire time; when omitted, falls back to the wall clock.
    """
    _validate_recovery_trigger_reason(trigger_reason)
    ts = triggered_at_utc or _utc_now_iso()
    _generated_at = generated_at or datetime.now(_EASTERN).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "run_id": scheduled_run_id(trading_day),
        "generated_at": _generated_at,
        "trading_day": trading_day,
        "broker": broker,
        "order_submit_status": ORDER_SUBMIT_STATUS_ERROR,
        "budget": budget or {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 0},
        "counts": {"evaluated": 0, "buy_candidates": 0, "submitted": 0},
        "buy_candidates": [],
        "run_classification": build_scheduled_classification(trading_day, ts),
        "recovery_rerun": True,
        "recovery": {
            "trigger_reason": trigger_reason,
            "attempt": 1,
            "persisted_raw_first": bool(persisted_raw_first),
            "recomputed_from_persisted": bool(recomputed_from_persisted),
            "escalated": True,
            "blocking_reason": str(blocking_reason),
        },
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
    # valid_scheduled_evidence is derived from the state being pointed at, not
    # asserted blindly: only a scheduled, non-manual run is same-day evidence
    # (see scripts/same_day_pointer.py and config/scheduled_tasks.json ->
    # same_day_pointer_validation_policy). manual_run must be the explicit
    # boolean False -- an incomplete classification that omits the key must
    # not default to non-manual.
    valid_scheduled_evidence = rc.get("run_origin") == "scheduled" and rc.get("manual_run") is False
    pointer_status = "valid" if valid_scheduled_evidence else "pending"
    pointer.write_text(
        json.dumps(
            {
                "run_id": rc.get("run_id"),
                "trading_day": day,
                "run_state_path": str(run_state_path),
                "stream": "afternoon",
                "pointer_status": pointer_status,
                "same_day_required": True,
                "valid_scheduled_evidence": bool(valid_scheduled_evidence),
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


# --- Same-day prior-allocation / top-up context ------------------------------
#
# The later same-day runs (12:30 midday, 3:20 afternoon/final) must fold earlier
# same-day MANUAL fills in as prior-allocation context for the top-up guard: a
# later run buys only max(0, desired - prior_same_day_amount) and only when its
# decision price is strictly below the LOWEST prior same-day reference price.
#
# Jul 21 2026 incident (see config/run_classification.json ->
# midday_manual_run_2026_07_21): the 3:20 final run under
# ce16ba6b/scheduled_runs/20260721_1520ET correctly EXCLUDED the manual midday
# fills from scheduled evidence, but FAILED to load
# midday_dca/manual_runs/20260721_1345ET as same-day prior-allocation context.
# It therefore set prior_usd blank and incremental_usd 0 for MSFT, GOOGL, GOOG,
# MA, IBM even though those manual fills were on disk. These helpers close that
# gap: the manual roots that are excluded from scheduled evidence are ingested
# here as prior-allocation context ONLY. A fill loaded here NEVER counts as
# scheduled evidence, scheduled slippage, scheduled fill counts, reliability,
# cost-basis evidence, watchdog proof, or proof the 12:30/3:20 tasks ran -- its
# order IDs stay in collect_manual_order_ids()'s exclusion set.

# Roots (relative to CRON_TRACKING_ROOT) scanned for same-day MANUAL top-up
# context. This overlaps MANUAL_EXCLUSION_SUBROOTS on purpose: a root excluded
# from scheduled evidence is exactly a root whose fills are valid ONLY as
# prior-allocation context. midday_dca/manual_runs/YYYYMMDD_* (the Jul 21 manual
# midday root) is included so later same-day runs pick it up. ce16ba6b/manual_runs
# is the canonical afternoon task's OWN manual/final-manual-override root -- a
# pre-3:20 CSV-only run there is same-day top-up context too, and without it
# here the afternoon guard could treat that ticker as no_prior_run and buy the
# full amount again (Codex review: "Scan the afternoon manual-runs root for
# CSV-only context"). final_manual_review is explicitly documented as same-day
# top-up context too (config/scheduled_tasks.json:
# same_day_top_up_policy.manual_override_handling), alongside
# midday_dca/manual_reruns and ce16ba6b/manual_runs/final_manual_override_*
# (Codex review: "Include final_manual_review in the top-up context roots").
TOP_UP_CONTEXT_SUBROOTS = (
    "morning_dca/manual_recovery",
    "midday_dca/manual_recovery",
    "midday_dca/manual_runs",
    "midday_dca/manual_reruns",
    "ce16ba6b/manual_runs",
    "final_manual_review",
)

# CSV column aliases for extracting per-ticker prior allocation from a manual
# run's orders CSV (submitted_orders.csv / filled_orders.csv). The Jul 21 manual
# midday CSV uses symbol/amount/average_price; other manual streams use
# ticker/submitted_amount_usd/average_fill_price, so both are accepted.
_TICKER_COLS = ("ticker", "symbol")
_AMOUNT_COLS = (
    "amount",
    "amount_usd",
    "submitted_amount_usd",
    "planned_amount_usd",
    "filled_amount_usd",
    # The codebase-standard amount column (near_close_dca._CSV_FIELDS, existing
    # manual-order CSV tests) -- without this alias a manual CSV using that
    # convention has no recognized amount and the ticker is silently discarded
    # (Codex review: "Parse the standard notional_usd column").
    "notional_usd",
    # A buy_candidate's own already-computed incremental top-up amount (see
    # _rows_from_buy_candidates) when no planned/notional amount is present.
    "incremental_order_amount",
)
# Reference-price preference order (contract): logged target/decision price, then
# average fill price. Lower-priority price columns are only used when no
# higher-priority column is present.
_PRICE_COLS = (
    "target_price",
    "decision_price",
    "average_price",
    "average_fill_price",
    # The codebase-standard confirmed-fill price column (near_close_dca.
    # _orders_csv_fills, near_close_dca._CSV_FIELDS, the scheduled artifact
    # writers) -- without this alias a manual CSV using that convention is
    # read with no price, so the guard wrongly returns missing_prior_price
    # (Codex review: "Accept the standard avg_fill_price column").
    "avg_fill_price",
    "price",
    # A buy_candidate's logged decision-bar close (see _rows_from_buy_candidates),
    # lowest priority since it's a looser proxy than an explicit target/decision price.
    "decision_bar_close",
)
# Fill-quantity column aliases, used only to derive a notional (price * quantity)
# when no amount/notional column is present -- the codebase-standard confirmed-fill
# shape (near_close_dca._orders_csv_fills) records avg_fill_price/quantity_filled
# with no separate dollar amount (Codex review: "Derive a prior notional from
# confirmed fill quantity"). The Jul 21 manual midday CSV instead uses "quantity".
_QUANTITY_COLS = ("quantity_filled", "quantity")


def top_up_context_roots(base: Optional[Path] = None) -> List[Path]:
    """Directories scanned for same-day manual prior-allocation / top-up context."""
    parent = cron_tracking_root(base)
    return [parent / sub for sub in TOP_UP_CONTEXT_SUBROOTS]


def is_top_up_context_state(
    state: Dict[str, object], trading_day: Optional[str] = None
) -> bool:
    """True only for a same-day MANUAL run usable as prior-allocation context.

    Enforces the manual-root markers before a run's fills may be folded into the
    top-up guard: ``run_origin='manual'``, ``manual_run=true``,
    ``same_day_top_up_context_only=true``, ``valid_scheduled_evidence=false``,
    and -- when the key is present -- ``valid_afternoon_scheduled_evidence=false``.
    When ``trading_day`` is given, the run's ``trading_day`` must match it. A run
    that is (or claims to be) scheduled evidence fails closed and is never
    returned as top-up context.
    """
    if not isinstance(state, dict):
        return False
    rc = state.get("run_classification")
    if not isinstance(rc, dict):
        return False
    if rc.get("run_origin") != "manual" or rc.get("manual_run") is not True:
        return False
    if rc.get("same_day_top_up_context_only") is not True:
        return False
    if rc.get("valid_scheduled_evidence") is not False:
        return False
    # Optional flag: reject only when explicitly True. A missing flag is allowed
    # (older manual artifacts predate it) but a True value means the artifact is
    # asserting afternoon scheduled evidence and must not be top-up context.
    if rc.get("valid_afternoon_scheduled_evidence") is True:
        return False
    if trading_day is not None:
        state_day = str(rc.get("trading_day") or state.get("trading_day") or "").strip()
        if state_day != trading_day:
            return False
    return True


@dataclass
class PriorAllocation:
    """Per-ticker same-day prior allocation folded into the top-up guard."""

    ticker: str
    prior_usd: float
    reference_price: Optional[float]
    order_ids: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)


def _row_ticker(row: Mapping[str, object]) -> Optional[str]:
    for col in _TICKER_COLS:
        value = row.get(col)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _row_number(row: Mapping[str, object], cols: Iterable[str]) -> Optional[float]:
    import math

    for col in cols:
        raw = row.get(col)
        if raw in (None, "", "-"):
            continue
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        return value
    return None


def _rows_from_buy_candidates(state: Dict[str, object]) -> List[Dict[str, object]]:
    """Convert a run_state's embedded buy_candidates into row-shaped dicts.

    Lets a run whose fills live only in ``buy_candidates`` (no orders CSV at
    all) flow through the same dedup/aggregation pipeline as CSV rows (see
    :func:`_dedupe_rows_by_order_id`, :func:`_prior_from_rows`). Only needed
    for a run under a root :func:`_already_covered_by_prior_allocation_loader`
    has decided ``near_close_dca.load_prior_same_day_allocations`` never
    actually reaches (currently ``final_manual_review/**``, which sits
    entirely outside the ce16ba6b task root and the morning_dca/midday_dca
    trees) -- everywhere else, a run with real buy_candidates is already
    covered by that loader and reading them again here would double-count
    (Codex review: "Do not skip final-review artifacts outside the core scan
    roots").
    """
    rows: List[Dict[str, object]] = []
    for cand in state.get("buy_candidates", []) or []:
        if not isinstance(cand, dict):
            continue
        ticker = cand.get("ticker")
        if not (isinstance(ticker, str) and ticker.strip()):
            continue
        status = cand.get("status")
        if status not in ("submitted", "filled"):
            continue
        row = dict(cand)
        row["ticker"] = ticker
        row["_confirmed_fill"] = status == "filled"
        rows.append(row)
    return rows


def _read_csv_rows(csv_path: Path) -> List[Dict[str, object]]:
    """Read one orders CSV's rows, keeping only submitted/filled statuses.

    Each returned row is tagged with an internal ``_confirmed_fill`` bool: True
    for an explicit ``status='filled'`` row, OR a STATUSLESS row from a file
    named ``filled_orders.csv`` (or carrying an ``amount_usd`` column) -- the
    documented manual-fill shape ``near_close_dca._orders_csv_fills`` already
    treats as a confirmed fill without a status column (Codex review: "Treat
    statusless filled-orders rows as confirmed fills"). Only a confirmed row's
    price may be trusted as a reference.
    """
    rows: List[Dict[str, object]] = []
    try:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                status = (row.get("status") or "").strip().lower()
                if status and status not in ("submitted", "filled"):
                    continue
                is_manual_filled_shape = not status and (
                    csv_path.name == "filled_orders.csv" or "amount_usd" in row
                )
                row = dict(row)
                row["_confirmed_fill"] = status == "filled" or is_manual_filled_shape
                rows.append(row)
    except (OSError, csv.Error, UnicodeDecodeError):
        return []
    return rows


def _dedupe_rows_by_order_id(
    rows: Iterable[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Deduplicate order rows by ``order_id``, preferring a confirmed fill row.

    The same order can appear in more than one sibling CSV for a single manual
    run -- e.g. both ``submitted_orders.csv`` and ``filled_orders.csv`` record
    the same ``order_id`` -- and summing each file's rows independently would
    double-count that order's notional (Codex review: "Deduplicate orders
    repeated in sibling CSVs"). Rows with no usable ``order_id`` can't be
    matched to a duplicate and are kept as-is.
    """
    by_order_id: Dict[str, Dict[str, object]] = {}
    unkeyed: List[Dict[str, object]] = []
    for row in rows:
        oid = (row.get("order_id") or "").strip()
        if not oid or oid == "-":
            unkeyed.append(row)
            continue
        existing = by_order_id.get(oid)
        if existing is None:
            by_order_id[oid] = row
            continue
        if row.get("_confirmed_fill") and not existing.get("_confirmed_fill"):
            by_order_id[oid] = row
    return [*by_order_id.values(), *unkeyed]


def _exclude_seen_order_ids(
    rows: Iterable[Dict[str, object]], seen_order_ids: Set[str]
) -> List[Dict[str, object]]:
    """Drop rows whose ``order_id`` was already counted by an earlier run directory.

    ``_dedupe_rows_by_order_id`` only dedupes within one run's sibling CSVs; a
    manual override can be backfilled or copied into MORE THAN ONE top-up
    context root (e.g. both ``midday_dca/manual_reruns`` and
    ``final_manual_review``), and each run directory is aggregated separately,
    so the same order's notional would otherwise be added again per directory
    (Codex review: "Deduplicate copied orders across manual run directories").
    Mutates ``seen_order_ids`` with every kept order_id so the caller can reuse
    it across run directories. Rows with no usable ``order_id`` can't be
    matched to a duplicate and are always kept.
    """
    kept: List[Dict[str, object]] = []
    for row in rows:
        oid = (row.get("order_id") or "").strip()
        if oid and oid != "-":
            if oid in seen_order_ids:
                continue
            seen_order_ids.add(oid)
        kept.append(row)
    return kept


def _prior_from_rows(rows: Iterable[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    """Per-ticker {prior_usd, reference_price, order_ids} from deduplicated order rows.

    ``prior_usd`` sums every filled/submitted amount for the ticker (conservative:
    an outstanding submission may already have consumed the day's budget even
    before it fills). When no amount/notional column is present, the notional is
    derived from ``avg_fill_price * quantity_filled`` -- the same fallback
    ``near_close_dca.py``'s own prior-allocation loader applies for a confirmed
    fill with no separate dollar amount (Codex review: "Derive a prior notional
    from confirmed fill quantity"). ``reference_price`` is trusted ONLY from a
    row tagged ``_confirmed_fill`` (see :func:`_read_csv_rows`) -- a still-
    outstanding submitted row may yet be rejected, canceled, or repriced, so
    trusting its logged price as a confirmed reference could let a later top-up
    race ahead of the first order's own fill and double-buy. Any row that isn't
    a confirmed fill poisons that ticker's ``reference_price`` to unknown (None)
    even when another row for the same ticker WAS filled with a known price --
    matching the "any unknown reference poisons the whole aggregate" policy
    ``near_close_dca.py`` already applies (Codex review: "Preserve an unknown
    price for submitted manual orders"), preferring higher-priority price
    columns per :data:`_PRICE_COLS`.
    """
    out: Dict[str, Dict[str, object]] = {}
    for row in rows:
        ticker = _row_ticker(row)
        if not ticker:
            continue
        amount = _row_number(row, _AMOUNT_COLS)
        if amount is None:
            fill_price = _row_number(row, _PRICE_COLS)
            quantity = _row_number(row, _QUANTITY_COLS)
            if fill_price is not None and quantity is not None:
                amount = fill_price * quantity
        first_row_for_ticker = ticker not in out
        entry = out.setdefault(
            ticker, {"prior_usd": 0.0, "reference_price": None, "order_ids": []}
        )
        if amount is not None:
            entry["prior_usd"] = float(entry["prior_usd"]) + amount
        price = _row_number(row, _PRICE_COLS) if row.get("_confirmed_fill") else None
        if first_row_for_ticker:
            entry["reference_price"] = price
        elif price is None or entry["reference_price"] is None:
            entry["reference_price"] = None
        else:
            entry["reference_price"] = min(float(entry["reference_price"]), price)
        oid = (row.get("order_id") or "").strip()
        if oid and oid != "-":
            entry["order_ids"].append(oid)
    return out


def _state_generated_before(state: Dict[str, object], boundary_et: Optional[time]) -> bool:
    """True when ``state['generated_at']`` (ET) is strictly before ``boundary_et``.

    Mirrors ``near_close_dca._prior_run_before_boundary``: a run with no
    parseable timestamp is treated as eligible (conservative -- more prior
    context only ever reduces the guarded buy), and ``boundary_et=None`` (the
    caller didn't ask for a boundary) skips the check entirely. Real callers
    (``run_dca.py``) pass the scheduled run's own execution time, so a
    REPLAYED/backfilled run never sees a manual fill written after that
    replay's nominal time as prior context (Codex review: "Bound manual top-up
    context to the scheduled execution time").
    """
    if boundary_et is None:
        return True
    minutes = _et_minutes(state.get("generated_at"))
    if minutes is None:
        return True
    return minutes < boundary_et.hour * 60 + boundary_et.minute


# Roots that near_close_dca.load_prior_same_day_allocations[_multi] never
# scans from ANY caller (run_dca.py's afternoon_root + cross_stream_roots, or
# morning_dca.py's roots), regardless of a run's buy_candidates/
# artifacts.orders_csv_path shape. final_manual_review/** sits entirely
# outside the ce16ba6b task root and the morning_dca/midday_dca trees (see
# MANUAL_EXCLUSION_SUBROOTS's own note that it backfilled artifacts "OUTSIDE
# the ce16ba6b task root"), so a run found there is NEVER "already covered" by
# that loader no matter what it contains (Codex review: "Do not skip
# final-review artifacts outside the core scan roots").
_ROOTS_NEVER_REACHED_BY_CORE_LOADER = ("final_manual_review",)


def _outside_core_loader_reach(run_dir: Path) -> bool:
    """True when ``run_dir`` sits under a root the core loader never scans."""
    return any(part in _ROOTS_NEVER_REACHED_BY_CORE_LOADER for part in run_dir.parts)


def _already_covered_by_prior_allocation_loader(
    state: Dict[str, object], run_dir: Path
) -> bool:
    """True when ``near_close_dca.load_prior_same_day_allocations`` already reads this run.

    That loader reads a run's fills from its embedded ``buy_candidates`` when
    present, or -- only when ``buy_candidates`` is absent -- from a recorded
    ``artifacts.orders_csv_path``, but ONLY when that path actually resolves to
    an existing file: it joins a relative path onto ``run_dir`` (mirrored here)
    but checks an absolute path as-is, with no basename/sibling fallback. A
    stale recorded path (e.g. an absolute path from a relocated environment)
    means the core loader finds nothing for this run, so this loader must still
    process it via :func:`_referenced_orders_csvs`'s own basename/glob fallback
    -- treating a merely-*present* ``orders_csv_path`` as "already covered"
    would silently drop the run from BOTH loaders (Codex review: "Fall back
    when a recorded orders CSV path is stale"). Either mechanism means the run
    is already fully covered by the sibling-stream prior-allocation scan the
    scheduled afternoon path runs first; folding it in again here would
    double-count its notional (Codex review: "Deduplicate manual context
    already present in prior allocations"). This loader's unique value is the
    remaining case: fills that live ONLY in a bare sibling orders CSV the
    run_state never names (the Jul 21 manual midday shape), PLUS any run under
    :func:`_outside_core_loader_reach` -- see :func:`_rows_from_buy_candidates`.
    """
    if _outside_core_loader_reach(run_dir):
        return False
    if state.get("buy_candidates"):
        return True
    artifacts = state.get("artifacts")
    if isinstance(artifacts, dict):
        recorded = artifacts.get("orders_csv_path")
        if isinstance(recorded, str) and recorded:
            recorded_path = Path(recorded)
            if not recorded_path.is_absolute():
                recorded_path = run_dir / recorded_path
            if recorded_path.exists():
                return True
    return False


def _merge_prior(
    dst: Dict[str, PriorAllocation],
    src: Dict[str, Dict[str, object]],
    source_label: str,
) -> None:
    """In-place merge of a raw ticker map into the aggregate PriorAllocation map.

    Same poisoning rule as :func:`_prior_from_rows`: an unknown reference from
    this or any earlier merged source makes the ticker's aggregate reference
    unknown, rather than silently keeping an earlier known price.
    """
    for ticker, entry in src.items():
        alloc = dst.get(ticker)
        if alloc is None:
            alloc = PriorAllocation(ticker=ticker, prior_usd=0.0, reference_price=None)
            dst[ticker] = alloc
        first_contribution = not alloc.sources
        alloc.prior_usd += float(entry.get("prior_usd") or 0.0)
        price = entry.get("reference_price")
        if first_contribution:
            alloc.reference_price = float(price) if isinstance(price, (int, float)) else None
        elif price is None or alloc.reference_price is None:
            alloc.reference_price = None
        else:
            alloc.reference_price = min(alloc.reference_price, float(price))
        for oid in entry.get("order_ids", []) or []:
            if oid not in alloc.order_ids:
                alloc.order_ids.append(oid)
        if source_label not in alloc.sources:
            alloc.sources.append(source_label)


def load_same_day_top_up_context(
    trading_day: str, base: Optional[Path] = None, *, boundary_et: Optional[time] = None
) -> Dict[str, PriorAllocation]:
    """Load same-day MANUAL fills as per-ticker prior-allocation / top-up context.

    Scans :func:`top_up_context_roots` for ``run_state*.json`` files that pass
    :func:`is_top_up_context_state` for ``trading_day`` (run_origin='manual',
    manual_run=true, same_day_top_up_context_only=true,
    valid_scheduled_evidence=false, valid_afternoon_scheduled_evidence!=true, and
    a matching trading_day) and, when ``boundary_et`` is given, were generated
    strictly before it (see :func:`_state_generated_before` -- pass the
    scheduled run's own execution time so a replayed/backfilled run never sees
    a later same-day manual fill as prior context).

    A run already readable by ``near_close_dca.load_prior_same_day_allocations``
    (it has embedded ``buy_candidates`` or a recorded
    ``artifacts.orders_csv_path``) is skipped entirely -- see
    :func:`_already_covered_by_prior_allocation_loader` -- so a manual recovery
    or override with real ``buy_candidates`` is never double-counted between
    that loader and this one. For the remaining runs (fills that live ONLY in a
    bare sibling orders CSV the run_state never names -- the Jul 21 manual
    midday shape) it reads every referenced orders CSV's rows, deduplicates
    them by ``order_id`` (a manual run's ``submitted_orders.csv`` and
    ``filled_orders.csv`` can both record the same order -- Codex review:
    "Deduplicate orders repeated in sibling CSVs" -- see
    :func:`_dedupe_rows_by_order_id`), and folds the result into a per-ticker
    :class:`PriorAllocation` (summed ``prior_usd`` and the LOWEST prior
    ``reference_price``, with any unconfirmed/unknown price poisoning that
    ticker's reference -- see :func:`_prior_from_rows`).

    A dry-run artifact (``state['dry_run']``) is always skipped -- it simulates
    submitted/filled rows against no real capital, exactly the reason
    ``near_close_dca.load_prior_same_day_allocations`` skips it too (Codex
    review: "Skip dry-run artifacts when loading manual CSV context"). An
    ``order_id`` already counted from an earlier run directory is excluded from
    every later one: a manual override can be backfilled or copied into more
    than one top-up context root, and each run directory is otherwise
    aggregated independently, so the same order would be added twice (Codex
    review: "Deduplicate copied orders across manual run directories").

    A run under a root the core loader never reaches at all (currently just
    ``final_manual_review/**`` -- see :func:`_outside_core_loader_reach`) has
    its ``buy_candidates`` read directly here too (see
    :func:`_rows_from_buy_candidates`), since no other mechanism will ever read
    them and skipping it as "already covered" would drop it from both loaders
    entirely (Codex review: "Do not skip final-review artifacts outside the
    core scan roots").

    The returned context is prior-allocation input for the top-up guard ONLY.
    These fills are never scheduled evidence; their order IDs remain in
    :func:`collect_manual_order_ids` so they stay out of every scheduled-live
    metric and out of the scheduled run's submitted_orders.csv.
    """
    result: Dict[str, PriorAllocation] = {}
    seen_order_ids: Set[str] = set()
    for root in top_up_context_roots(base):
        for path in _iter_run_state_files(root):
            try:
                state = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(state, dict) or not is_top_up_context_state(state, trading_day):
                continue
            if state.get("dry_run"):
                continue
            if not _state_generated_before(state, boundary_et):
                continue
            if _already_covered_by_prior_allocation_loader(state, path.parent):
                continue
            rc = state.get("run_classification")
            label = (rc.get("run_id") if isinstance(rc, dict) else None) or str(path.parent)
            all_rows: List[Dict[str, object]] = []
            for csv_path in _referenced_orders_csvs(state, path.parent):
                all_rows.extend(_read_csv_rows(csv_path))
            if _outside_core_loader_reach(path.parent):
                all_rows.extend(_rows_from_buy_candidates(state))
            deduped_rows = _dedupe_rows_by_order_id(all_rows)
            deduped_rows = _exclude_seen_order_ids(deduped_rows, seen_order_ids)
            csv_prior = _prior_from_rows(deduped_rows)
            _merge_prior(result, csv_prior, str(label))
    return result


def incremental_top_up_amount(
    desired_amount: float,
    prior_usd: float,
    current_price: Optional[float],
    reference_price: Optional[float],
) -> tuple[float, str]:
    """Apply the same-day top-up guard for one ticker.

    Returns ``(incremental_amount, guard_result)`` where ``guard_result`` is one
    of the ``top_up_guard_result`` codes in schemas/run_state.schema.json. A
    later run buys only ``max(0, desired - prior)`` and only when its
    ``current_price`` is strictly below the ``reference_price``. When prior
    allocation exists but no comparable price is available, it conservatively
    buys $0 and flags manual review (``missing_prior_price``).
    """
    if prior_usd <= 0:
        return max(0.0, desired_amount), "no_prior_run"
    if reference_price is None or current_price is None:
        return 0.0, "missing_prior_price"
    if not (current_price < reference_price):
        return 0.0, "price_not_below_prior_run"
    incremental = desired_amount - prior_usd
    if incremental <= 0:
        return 0.0, "no_incremental_difference"
    return incremental, "incremental_top_up"


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
    "RECOVERY_TRIGGER_REASONS",
    "build_recovery_state",
    "recovery_escalation_run_state",
    "validate_classification",
    "is_manual_state",
    "is_scheduled_state",
    "write_run_artifacts",
    "update_latest_scheduled_run_pointer",
    "collect_manual_order_ids",
    "exclude_manual_orders",
    "TOP_UP_CONTEXT_SUBROOTS",
    "top_up_context_roots",
    "is_top_up_context_state",
    "PriorAllocation",
    "load_same_day_top_up_context",
    "incremental_top_up_amount",
    "select_scheduled_state_for_reporting",
    "ScheduledSelection",
    "ValidationResult",
]
