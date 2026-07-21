#!/usr/bin/env python3
"""Scheduled-run artifact contract for the daily DCA preflight/live streams.

Every scheduled run_state.json for the 9:45 pre-flight validation, the 10:30
morning live run, and the 12:30 midday live run MUST carry a non-null top-level
``run_classification`` object, and every LIVE run MUST write ``signals.csv``
(whenever signals can be computed) and ``submitted_orders.csv`` (with a header
row even when no order was submitted). This module is the single source of truth
for those guarantees so the runners and the tests agree on one contract.

Why this exists: the Jul 21 2026 runs wrote a run_state.json whose top-level
``run_classification`` was null and, for the 10:30 live run, dropped signals.csv
and submitted_orders.csv even though the summary claimed they were written. A
null classification breaks downstream scheduled-vs-manual attribution, and a
missing order CSV makes the drift detector report every intended buy as an
unsubmitted order. Both are artifact-contract failures, not soft warnings.

Standard library only. No trading logic, no network, no secrets.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from scripts.same_day_pointer import (
    _utc_now_iso,
    derive_valid_scheduled_evidence,
    today_et,
)

RUN_STATE_FILENAME = "run_state.json"
SIGNALS_CSV_FILENAME = "signals.csv"
SUBMITTED_ORDERS_CSV_FILENAME = "submitted_orders.csv"

# Any of these satisfies the signals contract. The morning/midday runners emit
# stream-specific names; the drift detector (SIGNAL_CSV_NAMES) accepts the same
# set, so a stream-specific file still counts as signals.csv for the contract.
SIGNAL_CSV_NAMES = (SIGNALS_CSV_FILENAME, "morning_signals.csv", "midday_signals.csv")

# Header written for submitted_orders.csv even when there are zero order rows.
SUBMITTED_ORDERS_FIELDS = ("ticker", "order_id", "submitted_amount_usd", "status")

# Broker outcome statuses that represent a placed or in-flight order and so must
# be written as a submitted_orders.csv row. A pending/unconfirmed response keeps
# its concrete status but still produces a row (and the run_state). Deliberate
# non-orders (skipped, cap_exceeded, dry_run, blocked) are not order rows.
ORDER_ATTEMPT_STATUSES = frozenset(
    {
        "submitted",
        "filled",
        "pending",
        "unconfirmed",
        "not_submitted_confirmation_required",
    }
)

# Scheduled pre-flight validation (9:45 ET) identity. Must match the actual
# producer's constants in morning_dca.py (MORNING_PREFLIGHT_CRON_ID,
# MORNING_PREFLIGHT_RUN_TYPE) and schemas/morning_preflight.schema.json, whose
# own description documents run_type='morning_dca_preflight' and
# stream='morning' -- not an aspirational separate identity, or any consumer
# adopting this contract to validate/attribute the preflight would reject
# every real artifact.
PREFLIGHT_CRON_ID = "morning-dca-0945"
PREFLIGHT_RUN_TYPE = "morning_dca_preflight"
PREFLIGHT_SCHEDULED_FOR_ET = "09:45"
PREFLIGHT_WINDOW_START_ET = "09:40"
PREFLIGHT_WINDOW_END_ET = "09:50"
PREFLIGHT_STREAM = "morning"
PREFLIGHT_STATE_SCHEMA_VERSION = "morning-preflight-v2-machine-complete"

# Scheduled morning live (10:30 ET) identity.
MORNING_LIVE_CRON_ID = "42173154"
MORNING_LIVE_RUN_TYPE = "morning_dca_live"
MORNING_LIVE_SCHEDULED_FOR_ET = "10:30"
MORNING_LIVE_WINDOW_START_ET = "10:25"
MORNING_LIVE_WINDOW_END_ET = "10:35"
MORNING_LIVE_STREAM = "morning"
MORNING_LIVE_STATE_SCHEMA_VERSION = "morning-live-v2-preauthorized"
MORNING_LIVE_EXECUTION_BROKER = "robinhood"
MORNING_LIVE_ACCOUNT_NUMBER = "562057042"

# Scheduled midday live (12:30 ET) identity.
MIDDAY_LIVE_CRON_ID = "a6239dae"
MIDDAY_LIVE_SCHEDULED_FOR_ET = "12:30"
MIDDAY_LIVE_STREAM = "midday"


def build_preflight_run_classification(
    *,
    trading_day: str,
    run_id: str,
    valid_scheduled_evidence: bool,
    triggered_at_utc: Optional[str] = None,
    cron_id: str = PREFLIGHT_CRON_ID,
) -> Dict[str, Any]:
    """Non-null run_classification for the 9:45 pre-flight validation run.

    ``valid_scheduled_evidence`` must be derived from the completed run_state
    (see :func:`derive_valid_scheduled_evidence`), never asserted blindly.
    Includes every field schemas/morning_run_classification.schema.json
    requires for a scheduled run_type='morning_dca_preflight' document
    (trades, valid_afternoon_evidence, and the pinned scheduled window), not
    just the same-day pointer fields.
    """
    return {
        "run_origin": "scheduled",
        "run_type": PREFLIGHT_RUN_TYPE,
        "trades": False,
        "scheduled_for_et": PREFLIGHT_SCHEDULED_FOR_ET,
        "scheduled_window_start_et": PREFLIGHT_WINDOW_START_ET,
        "scheduled_window_end_et": PREFLIGHT_WINDOW_END_ET,
        "manual_run": False,
        "state_schema_version": PREFLIGHT_STATE_SCHEMA_VERSION,
        "run_id": run_id,
        "triggered_at_utc": triggered_at_utc or _utc_now_iso(),
        "cron_id": cron_id,
        "stream": PREFLIGHT_STREAM,
        "valid_afternoon_evidence": False,
        "trading_day": trading_day,
        "same_day_required": True,
        "valid_scheduled_evidence": bool(valid_scheduled_evidence),
    }


def build_morning_live_run_classification(
    *,
    trading_day: str,
    run_id: str,
    valid_scheduled_evidence: bool,
    triggered_at_utc: Optional[str] = None,
    state_schema_version: str = MORNING_LIVE_STATE_SCHEMA_VERSION,
    account_number: str = MORNING_LIVE_ACCOUNT_NUMBER,
) -> Dict[str, Any]:
    """Non-null run_classification for the 10:30 morning live run.

    Includes every field schemas/morning_run_classification.schema.json
    requires for a scheduled run_type='morning_dca_live' document: run_type,
    trades, the pinned scheduled window, execution_broker/account_number
    (required whenever run_type='morning_dca_live'), and
    preauthorized_direct_place/valid_morning_evidence (required for the
    default v2-preauthorized state_schema_version) -- not just the same-day
    pointer fields.
    """
    return {
        "run_origin": "scheduled",
        "run_type": MORNING_LIVE_RUN_TYPE,
        "trades": True,
        "manual_run": False,
        "stream": MORNING_LIVE_STREAM,
        "scheduled_for_et": MORNING_LIVE_SCHEDULED_FOR_ET,
        "scheduled_window_start_et": MORNING_LIVE_WINDOW_START_ET,
        "scheduled_window_end_et": MORNING_LIVE_WINDOW_END_ET,
        "cron_id": MORNING_LIVE_CRON_ID,
        "run_id": run_id,
        "triggered_at_utc": triggered_at_utc or _utc_now_iso(),
        "state_schema_version": state_schema_version,
        "execution_broker": MORNING_LIVE_EXECUTION_BROKER,
        "account_number": account_number,
        "preauthorized_direct_place": True,
        "valid_morning_evidence": True,
        "trading_day": trading_day,
        "same_day_required": True,
        "valid_afternoon_evidence": False,
        "valid_scheduled_evidence": bool(valid_scheduled_evidence),
    }


def submitted_order_rows(
    broker_outcomes: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Rows for submitted_orders.csv derived from a run's broker outcomes.

    One row per ticker whose outcome is a placed or in-flight order (see
    :data:`ORDER_ATTEMPT_STATUSES`) or that carries a concrete order_id. The
    concrete status is preserved so a pending/unconfirmed response is still
    recorded rather than dropped.
    """
    rows: List[Dict[str, Any]] = []
    for ticker, outcome in (broker_outcomes or {}).items():
        if not isinstance(outcome, dict):
            continue
        status = outcome.get("status")
        if status in ORDER_ATTEMPT_STATUSES or outcome.get("order_id"):
            rows.append(
                {
                    "ticker": ticker,
                    "order_id": outcome.get("order_id"),
                    "submitted_amount_usd": outcome.get("submitted_amount_usd"),
                    "status": status,
                }
            )
    return rows


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_signals_csv(
    run_dir: Path | str,
    rows: Iterable[Dict[str, Any]],
    *,
    fieldnames: Optional[Sequence[str]] = None,
    filename: str = SIGNALS_CSV_FILENAME,
) -> Path:
    """Write signals.csv for a run. Emits a header row even when empty."""
    out = Path(run_dir)
    out.mkdir(parents=True, exist_ok=True)
    row_list = [dict(r) for r in rows]
    if fieldnames is None:
        seen: Dict[str, None] = {}
        for row in row_list:
            seen.update(dict.fromkeys(row))
        fieldnames = list(seen) or ["ticker"]
    path = out / filename
    _write_csv(path, row_list, fieldnames)
    return path


def write_submitted_orders_csv(
    run_dir: Path | str,
    rows: Iterable[Dict[str, Any]],
    *,
    fieldnames: Sequence[str] = SUBMITTED_ORDERS_FIELDS,
) -> Path:
    """Write submitted_orders.csv for a live run.

    Always writes the header, even when ``rows`` is empty, so a no-order live
    run still produces a machine-readable order record instead of a missing
    file the drift detector would flag as every intended buy going unsubmitted.
    """
    out = Path(run_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / SUBMITTED_ORDERS_CSV_FILENAME
    _write_csv(path, [dict(r) for r in rows], fieldnames)
    return path


@dataclass
class ArtifactContractResult:
    """Outcome of validating a scheduled run's artifact contract."""

    ok: bool
    errors: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # allow `if validate_run_artifacts(...):`
        return self.ok


def _has_signal_csv(run_dir: Path) -> bool:
    return any((run_dir / name).exists() for name in SIGNAL_CSV_NAMES)


def validate_run_artifacts(
    run_dir: Path | str,
    *,
    require_signals: bool = True,
    require_submitted_orders: bool = True,
) -> ArtifactContractResult:
    """Validate the artifact contract for a scheduled run directory.

    Fails when run_state.json is missing/unreadable, when its top-level
    ``run_classification`` is null/absent or not an object, or when a required
    CSV (signals.csv / submitted_orders.csv) is missing. ``require_submitted_orders``
    should be True for live runs and False for read-only pre-flight runs.
    """
    run_dir = Path(run_dir)
    errors: List[str] = []

    state_path = run_dir / RUN_STATE_FILENAME
    if not state_path.exists():
        errors.append(f"missing {RUN_STATE_FILENAME}")
    else:
        state: Any = None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors.append(f"{RUN_STATE_FILENAME} is not readable JSON: {exc}")
        if isinstance(state, dict):
            if "run_classification" not in state or state.get("run_classification") is None:
                errors.append(
                    "run_classification is null or absent; it must be a non-null object"
                )
            elif not isinstance(state.get("run_classification"), dict):
                errors.append("run_classification must be an object, not "
                              f"{type(state.get('run_classification')).__name__}")
        elif state is not None:
            errors.append(f"{RUN_STATE_FILENAME} is not a JSON object")

    if require_signals and not _has_signal_csv(run_dir):
        errors.append(f"missing {SIGNALS_CSV_FILENAME}")
    if require_submitted_orders and not (run_dir / SUBMITTED_ORDERS_CSV_FILENAME).exists():
        errors.append(f"missing {SUBMITTED_ORDERS_CSV_FILENAME}")

    return ArtifactContractResult(not errors, errors)


__all__ = [
    "RUN_STATE_FILENAME",
    "SIGNALS_CSV_FILENAME",
    "SUBMITTED_ORDERS_CSV_FILENAME",
    "SIGNAL_CSV_NAMES",
    "SUBMITTED_ORDERS_FIELDS",
    "ORDER_ATTEMPT_STATUSES",
    "PREFLIGHT_CRON_ID",
    "PREFLIGHT_RUN_TYPE",
    "PREFLIGHT_STREAM",
    "MORNING_LIVE_CRON_ID",
    "MORNING_LIVE_STREAM",
    "MIDDAY_LIVE_CRON_ID",
    "MIDDAY_LIVE_STREAM",
    "build_preflight_run_classification",
    "build_morning_live_run_classification",
    "submitted_order_rows",
    "write_signals_csv",
    "write_submitted_orders_csv",
    "validate_run_artifacts",
    "ArtifactContractResult",
    "derive_valid_scheduled_evidence",
    "today_et",
]
