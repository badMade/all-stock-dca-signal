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

from dca_fileio import atomic_open_w
from scripts.same_day_pointer import (
    _utc_now_iso,
    derive_valid_scheduled_evidence,
    today_et,
)

RUN_STATE_FILENAME = "run_state.json"
SIGNALS_CSV_FILENAME = "signals.csv"
SUBMITTED_ORDERS_CSV_FILENAME = "submitted_orders.csv"

# Any of these satisfies the signals contract. The morning/midday/market-open/
# intraday runners emit stream-specific names; the drift detector
# (SIGNAL_CSV_NAMES) accepts the same set, so a stream-specific file still
# counts as signals.csv for the contract.
SIGNAL_CSV_NAMES = (
    SIGNALS_CSV_FILENAME,
    "morning_signals.csv",
    "midday_signals.csv",
    "market_open_signals.csv",
    "intraday_signals.csv",
)

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
PREFLIGHT_CRON_ID = "bb4f6ef1"
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

# Scheduled afternoon final live (3:30 PM / 15:30 ET) identity. This is the
# repaired live task 10ce362c, which supersedes the deleted ce16ba6b.
AFTERNOON_LIVE_CRON_ID = "10ce362c"
AFTERNOON_LIVE_SUPERSEDES_CRON_ID = "ce16ba6b"
AFTERNOON_LIVE_RUN_TYPE = "scheduled_live_test"
AFTERNOON_LIVE_SCHEDULED_FOR_ET = "15:30"
AFTERNOON_LIVE_WINDOW_START_ET = "15:25"
# Retimed from 3:20 PM ET on 2026-07-27. Legacy runs written before then carry
# these values; validators accept them so existing artifacts stay valid.
AFTERNOON_LIVE_LEGACY_SCHEDULED_FOR_ET = "15:20"
AFTERNOON_LIVE_LEGACY_WINDOW_START_ET = "15:15"
AFTERNOON_LIVE_WINDOW_END_ET = "15:35"
AFTERNOON_LIVE_STREAM = "afternoon"
AFTERNOON_LIVE_STATE_SCHEMA_VERSION = "run-classification-v1"

# order_submit_status / error_class values for the 3:30 placement contract
# (Jul 22 2026). A compute-only module that plans orders but never places them
# is NOT a valid terminal state for the scheduled live task; when the approved
# fallback placement path is genuinely unreachable, the run records these.
ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE = "blocked_placement_phase_unreachable"
ERROR_CLASS_PLACEMENT_PHASE_UNREACHABLE = "placement_phase_unreachable"
ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT = "placement_not_implemented_in_compute_module"

# run_summary.json / run_state.json filenames whose run_classification must
# mirror each other for scheduled live runs. The canonical run_summary.json is the
# one the scheduled contract requires to exist (require_summary); latest_run_summary.json
# is the report-only variant dca_reporting writes and is only mirror-checked when present.
CANONICAL_RUN_SUMMARY_FILENAME = "run_summary.json"
SUMMARY_FILENAMES = (CANONICAL_RUN_SUMMARY_FILENAME, "latest_run_summary.json")


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


def build_afternoon_live_run_classification(
    *,
    trading_day: str,
    run_id: str,
    valid_scheduled_evidence: bool,
    valid_afternoon_scheduled_evidence: Optional[bool] = None,
    triggered_at_utc: Optional[str] = None,
    state_schema_version: str = AFTERNOON_LIVE_STATE_SCHEMA_VERSION,
) -> Dict[str, Any]:
    """Non-null run_classification for the 3:30 PM ET afternoon final live run.

    Both run_state.json and run_summary.json must carry this same object for a
    scheduled 3:30 run. ``valid_scheduled_evidence`` and
    ``valid_afternoon_scheduled_evidence`` must be derived from the completed
    run_state (a placed-order outcome), never asserted blindly;
    ``valid_afternoon_scheduled_evidence`` defaults to ``valid_scheduled_evidence``.
    Carries both ``cron_id='10ce362c'`` and ``supersedes_cron_id='ce16ba6b'`` per
    the writer contract. ``run_id`` should be the scheduled id
    ``scheduled_YYYYMMDD_1530ET``.
    """
    if valid_afternoon_scheduled_evidence is None:
        valid_afternoon_scheduled_evidence = bool(valid_scheduled_evidence)
    return {
        "run_origin": "scheduled",
        "run_type": AFTERNOON_LIVE_RUN_TYPE,
        "manual_run": False,
        "stream": AFTERNOON_LIVE_STREAM,
        "scheduled_for_et": AFTERNOON_LIVE_SCHEDULED_FOR_ET,
        "scheduled_window_start_et": AFTERNOON_LIVE_WINDOW_START_ET,
        "scheduled_window_end_et": AFTERNOON_LIVE_WINDOW_END_ET,
        "cron_id": AFTERNOON_LIVE_CRON_ID,
        "supersedes_cron_id": AFTERNOON_LIVE_SUPERSEDES_CRON_ID,
        "run_id": run_id,
        "triggered_at_utc": triggered_at_utc or _utc_now_iso(),
        "state_schema_version": state_schema_version,
        "trading_day": trading_day,
        "same_day_required": True,
        "valid_afternoon_scheduled_evidence": bool(valid_afternoon_scheduled_evidence),
        "valid_scheduled_evidence": bool(valid_scheduled_evidence),
    }


def _planned_amount(candidate: Dict[str, Any]) -> float:
    """Planned order dollars for a candidate/signal row.

    Prefers the same-day top-up incremental amount when present, then the
    reconciled planned amount. Non-numeric/absent values count as 0.
    """
    for key in ("incremental_usd", "incremental_amount_usd", "planned_amount_usd"):
        value = candidate.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def has_planned_orders(run_state: Dict[str, Any]) -> bool:
    """True when the run planned at least one order with a positive amount.

    This is the trigger the 3:30 task uses to require a placement phase in the
    same run: once signals.csv exists and any eligible row has
    ``incremental_usd`` (or an equivalent planned amount) > 0, the run must
    attempt placement rather than terminate at compute-only planning.
    """
    if not isinstance(run_state, dict):
        return False
    for candidate in run_state.get("buy_candidates") or []:
        if isinstance(candidate, dict) and _planned_amount(candidate) > 0:
            return True
    return False


def _has_submitted_order(run_state: Dict[str, Any]) -> bool:
    for candidate in run_state.get("buy_candidates") or []:
        if isinstance(candidate, dict) and (
            candidate.get("status") == "submitted" or candidate.get("order_id")
        ):
            return True
    return False


# Candidate statuses that prove a positive planned row entered the placement
# phase -- placed, attempted-and-failed, or review-passed-but-confirmation-blocked
# -- plus the deliberate non-order outcomes the guards intentionally produce
# (skipped/blocked) and dry_run. A positive planned row left at the default
# ``not_submitted`` (with no order_id) is NONE of these: it never entered placement.
_PLACEMENT_ENTERED_STATUSES = frozenset(
    {
        "submitted",
        "failed",
        "not_submitted_confirmation_required",
        "skipped",
        "blocked",
        "dry_run",
    }
)


def _has_unplaced_planned_order(run_state: Dict[str, Any]) -> bool:
    """True when any positive planned candidate never entered the placement phase.

    The 3:30 contract requires EVERY positive planned row to enter placement. A
    candidate with a positive planned amount still at its default
    ``status='not_submitted'`` (and no order_id) was never placed -- even when a
    *different* candidate in the same run was submitted (e.g. the placement loop
    stopped after an initial broker success). This catches that partial,
    per-candidate case that the whole-run markers miss.
    """
    for candidate in run_state.get("buy_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if _planned_amount(candidate) <= 0:
            continue
        if candidate.get("order_id"):
            continue
        if candidate.get("status") not in _PLACEMENT_ENTERED_STATUSES:
            return True
    return False


def is_compute_only_terminal_failure(run_state: Dict[str, Any]) -> bool:
    """True when a live run stopped at compute-only planning with orders pending.

    The Jul 22 2026 3:20 incident: signals + planned orders existed
    (``planned_spend`` > 0) but no order was placed because the saved compute
    module ended with ``error_class='placement_not_implemented_in_compute_module'``
    (or a bare ``order_submit_status='not_submitted'``) while planned orders
    remained. That is not a valid terminal state for the pre-authorized
    scheduled live task, so validate_run_artifacts and the 3:40 watchdog reject
    it instead of silently passing. A dry run, a run with no planned orders, or a
    run that actually placed every planned order is not a compute-only failure.

    This also rejects the PARTIAL case: a run that placed one candidate but left
    another positive planned candidate unplaced (still ``not_submitted`` with no
    order_id) fails too -- every positive planned row must enter placement, not
    just one. The approved ``placement_phase_unreachable`` terminal state
    (fallback placement tried and failed) legitimately leaves rows unplaced and is
    explicitly NOT a compute-only failure.
    """
    if not isinstance(run_state, dict):
        return False
    if run_state.get("dry_run"):
        return False
    if not has_planned_orders(run_state):
        return False
    # The approved 'placement phase unreachable' terminal state legitimately
    # leaves planned rows unplaced after the fallback path was tried and failed;
    # it is a valid terminal state, never a compute-only failure. Require ALL of
    # its markers exactly as placement_unreachable_state_fields writes them: BOTH
    # paired markers (order_submit_status AND error_class) plus
    # valid_completed_order_evidence explicitly False. Accepting a subset would let
    # a malformed compute-only / partially unplaced run bypass this check by setting
    # one field while its other fields still claim a completed order.
    if (
        run_state.get("order_submit_status") == ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE
        and run_state.get("error_class") == ERROR_CLASS_PLACEMENT_PHASE_UNREACHABLE
        and run_state.get("valid_completed_order_evidence") is False
    ):
        return False
    # Per-candidate: any positive planned row that never entered placement is a
    # failure, even when a different candidate was submitted.
    if _has_unplaced_planned_order(run_state):
        return True
    if _has_submitted_order(run_state):
        return False
    if run_state.get("error_class") == ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT:
        return True
    if run_state.get("order_submit_status") == "not_submitted":
        return True
    return False


def mirror_run_classification(run_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a copy of run_state's run_classification for the run_summary.

    run_summary.json must carry the SAME non-null run_classification object as
    run_state.json for scheduled live runs. Returns None when the run_state has
    no usable classification, which the caller records as an
    ``artifact_contract_failure`` rather than silently passing.
    """
    if not isinstance(run_state, dict):
        return None
    classification = run_state.get("run_classification")
    return dict(classification) if isinstance(classification, dict) else None


def artifact_contract_failure(reason: str, *, detail: Optional[str] = None) -> Dict[str, Any]:
    """Standard ``artifact_contract_failure`` marker for run_state/run_summary."""
    failure: Dict[str, Any] = {"failure": "artifact_contract_failure", "reason": reason}
    if detail:
        failure["detail"] = detail
    return failure


def placement_unreachable_state_fields(*, detail: Optional[str] = None) -> Dict[str, Any]:
    """run_state fields for the approved 'placement phase unreachable' terminal state.

    Used when the 3:30 task planned orders but neither the saved compute module
    nor the approved fallback placement path (main live placement runner /
    direct connector) could place them. Completed-order evidence is false, but
    the run still proves the scheduled task fired. Callers still write
    signals.csv with the planned rows and submitted_orders.csv with headers.
    """
    fields: Dict[str, Any] = {
        "order_submit_status": ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE,
        "error_class": ERROR_CLASS_PLACEMENT_PHASE_UNREACHABLE,
        "valid_completed_order_evidence": False,
    }
    if detail:
        fields["error_detail"] = detail
    return fields


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
    with atomic_open_w(path, newline="") as handle:
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


def _submitted_orders_errors(run_dir: Path) -> List[str]:
    """Contract errors for submitted_orders.csv: it must exist AND carry the header.

    Existence alone is not enough: a zero-byte or truncated file (e.g. an
    interrupted write) would otherwise pass as a valid no-order run. The contract
    requires the header row even when no order was submitted, so parse the first
    row and require it to equal SUBMITTED_ORDERS_FIELDS.
    """
    path = run_dir / SUBMITTED_ORDERS_CSV_FILENAME
    if not path.exists():
        return [f"missing {SUBMITTED_ORDERS_CSV_FILENAME}"]
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle), None)
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{SUBMITTED_ORDERS_CSV_FILENAME} is not readable: {exc}"]
    if header != list(SUBMITTED_ORDERS_FIELDS):
        return [
            f"{SUBMITTED_ORDERS_CSV_FILENAME} header {header!r} does not match the "
            f"required {list(SUBMITTED_ORDERS_FIELDS)!r} (empty or corrupt file)"
        ]
    return []


def validate_run_artifacts(
    run_dir: Path | str,
    *,
    require_signals: bool = True,
    require_submitted_orders: bool = True,
    require_summary: bool = False,
) -> ArtifactContractResult:
    """Validate the artifact contract for a scheduled run directory.

    Fails when run_state.json is missing/unreadable, when its top-level
    ``run_classification`` is null/absent or not an object, or when a required
    CSV (signals.csv / submitted_orders.csv) is missing. ``require_submitted_orders``
    should be True for live runs and False for read-only pre-flight runs; when True,
    submitted_orders.csv must not only exist but carry the SUBMITTED_ORDERS_FIELDS
    header row (a zero-byte/truncated file fails rather than passing as no-order).

    The run_summary.json <-> run_state run_classification mirror check is only
    enforced for scheduled LIVE runs (``require_submitted_orders=True``). Read-only
    pre-flight runs write a run_summary.json without a run_classification by design
    (the 9:45 preflight writer never mirrors it), so requiring the mirror on them
    would wrongly fail a valid preflight whose run_state classification is present.

    ``require_summary`` (True for the scheduled afternoon live run) additionally
    fails unless the CANONICAL ``run_summary.json`` is present. A report-only
    ``latest_run_summary.json`` left behind after the canonical file was lost is
    NOT sufficient, or the missing-canonical-artifact detection is defeated. The
    mirror check above only validates a summary that exists, so without this a live
    dir carrying only run_state.json + the CSVs would silently pass despite the 3:30
    contract requiring a canonical run_summary.json.
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
            # A live run that planned orders but stopped at compute-only
            # planning (no placement) is not a valid terminal state for the
            # pre-authorized scheduled task. See the Jul 22 2026 3:20 incident.
            if is_compute_only_terminal_failure(state):
                errors.append(
                    "compute-only terminal state: planned orders exist but none were placed "
                    f"({ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT}); the run must enter the "
                    "placement phase or record placement_phase_unreachable"
                )
            # The CANONICAL run_summary.json must mirror the SAME non-null
            # run_classification object -- but ONLY for scheduled LIVE runs
            # (require_submitted_orders is True). Only the canonical run_summary.json
            # is checked: the report-only latest_run_summary.json can be a STALE copy
            # from a prior attempt in a reused same-day recovery dir (its
            # triggered_at_utc differs), so mirror-checking it would wrongly mark a
            # valid recovery blocked after orders may already have been placed.
            # Read-only pre-flight dirs (require_submitted_orders=False) write
            # run_summary.json WITHOUT a run_classification by design (the 9:45
            # preflight writer never mirrors it), so enforcing the mirror on them
            # would wrongly fail a valid preflight. Within live runs it is still only
            # enforced when the canonical summary is present, so a live run without
            # one still validates here (require_summary covers its absence).
            state_classification = (
                state.get("run_classification")
                if isinstance(state.get("run_classification"), dict)
                else None
            )
            if require_submitted_orders:
                for summary_name in (CANONICAL_RUN_SUMMARY_FILENAME,):
                    summary_path = run_dir / summary_name
                    if not summary_path.exists():
                        continue
                    try:
                        summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                        errors.append(f"{summary_name} is not readable JSON: {exc}")
                        continue
                    if not isinstance(summary, dict):
                        errors.append(f"{summary_name} is not a JSON object")
                        continue
                    summary_classification = summary.get("run_classification")
                    if summary_classification is None:
                        errors.append(
                            f"{summary_name} run_classification is null or absent; it must "
                            "mirror the non-null run_state run_classification"
                        )
                    elif not isinstance(summary_classification, dict):
                        errors.append(
                            f"{summary_name} run_classification must be an object, not "
                            f"{type(summary_classification).__name__}"
                        )
                    elif state_classification is not None and summary_classification != state_classification:
                        errors.append(
                            f"{summary_name} run_classification does not mirror run_state "
                            "run_classification"
                        )
        elif state is not None:
            errors.append(f"{RUN_STATE_FILENAME} is not a JSON object")

    if require_signals and not _has_signal_csv(run_dir):
        errors.append(f"missing {SIGNALS_CSV_FILENAME}")
    if require_submitted_orders:
        errors.extend(_submitted_orders_errors(run_dir))
    if require_summary and not (run_dir / CANONICAL_RUN_SUMMARY_FILENAME).exists():
        # Specifically the canonical run_summary.json -- a report-only
        # latest_run_summary.json left behind after the canonical file was lost is
        # NOT sufficient, or the missing-canonical-artifact detection is defeated.
        errors.append(
            f"missing {CANONICAL_RUN_SUMMARY_FILENAME} (the canonical run_summary is "
            "required for scheduled live runs)"
        )

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
    "AFTERNOON_LIVE_CRON_ID",
    "AFTERNOON_LIVE_SUPERSEDES_CRON_ID",
    "AFTERNOON_LIVE_RUN_TYPE",
    "AFTERNOON_LIVE_SCHEDULED_FOR_ET",
    "AFTERNOON_LIVE_WINDOW_START_ET",
    "AFTERNOON_LIVE_WINDOW_END_ET",
    "AFTERNOON_LIVE_STREAM",
    "AFTERNOON_LIVE_STATE_SCHEMA_VERSION",
    "ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE",
    "ERROR_CLASS_PLACEMENT_PHASE_UNREACHABLE",
    "ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT",
    "SUMMARY_FILENAMES",
    "CANONICAL_RUN_SUMMARY_FILENAME",
    "build_preflight_run_classification",
    "build_morning_live_run_classification",
    "build_afternoon_live_run_classification",
    "has_planned_orders",
    "is_compute_only_terminal_failure",
    "mirror_run_classification",
    "artifact_contract_failure",
    "placement_unreachable_state_fields",
    "submitted_order_rows",
    "write_signals_csv",
    "write_submitted_orders_csv",
    "validate_run_artifacts",
    "ArtifactContractResult",
    "derive_valid_scheduled_evidence",
    "today_et",
]
