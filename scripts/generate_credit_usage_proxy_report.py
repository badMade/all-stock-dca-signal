#!/usr/bin/env python3
"""Generator for the one-time 2026-07-27 4:30 PM ET credit-usage proxy report.

Implements the contract declared in ``config/scheduled_tasks.json`` ->
``credit_usage_proxy_report_2026_07_27`` (see
``docs/credit_usage_proxy_report.md``). Analytics and reporting only:
read-only, no trading, no order review/submission/cancellation, and never
writes ``run_state.json`` or any same-day pointer.

Every counter is derived from persisted same-day artifacts and logs. A
counter or run that cannot be measured is reported as ``not_logged`` or
``not_available`` (with a reason) rather than estimated, inferred, or
fabricated, per the contract's ``unavailable_data_rule``/``counts_rule``.

Run as ``python -m scripts.generate_credit_usage_proxy_report`` (not as a bare
script) so the ``scripts.*`` absolute imports below resolve; direct execution
puts ``scripts/`` rather than the repo root on ``sys.path``.

Standard library only. No secrets, no network, no broker calls.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dca_alerts import ATTEMPT_LOG_HISTORY_SUFFIX, ATTEMPT_LOG_ROOT_ENV
from dca_paths import CRON_TRACKING_ROOT_ENV as _CRON_TRACKING_ROOT_ENV
from dca_paths import cron_tracking_root
from scripts.model_usage_log import MODEL_ROUTING_MODE_VALUES
from scripts.run_classification import (
    CRON_TRACKING_ROOT,
    MANUAL_EXCLUSION_SUBROOTS,
    ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
    ORDER_SUBMIT_STATUS_DRY_RUN,
    ORDER_SUBMIT_STATUS_NO_CANDIDATES,
)
from scripts.scheduled_artifacts import (
    CANONICAL_RUN_SUMMARY_FILENAME,
    RUN_STATE_FILENAME,
    SUBMITTED_ORDERS_CSV_FILENAME,
    SUBMITTED_ORDERS_FIELDS,
    SUMMARY_FILENAMES,
)

TASK_ID = "onetime-1630-credit-usage-proxy-2026-07-27"
DEFAULT_TRADING_DAY = "2026-07-27"
# This report is content-gated to the 2026-07-27 trading day it was built to
# summarize. A 5-field cron expression has no year field, so the external
# registration (cron_utc='30 20 27 7 *') recurs every July 27 -- the
# scheduler side needs a one-shot trigger or a manual retirement step after
# 2026-07-27 fires (codex review discussion_r3658332755); this guard is the
# script-side half, so an accidental 2027 (or later) firing produces an
# explicit refusal rather than silently regenerating stale output.
SUPPORTED_TRADING_DAY = "2026-07-27"

_EASTERN = ZoneInfo("America/New_York")

NO_RUN_ID = "no_run_id_written"
NOT_APPLICABLE_DID_NOT_RUN = "not_applicable_did_not_run"
RECOVERY_CONTEXT_LABEL = "recovery context only"

CLASSIFICATION_VALUES = (
    "valid_scheduled_evidence",
    "top_up_context_only",
    "manual_context_only",
    "failed_start_no_evidence",
    "did_not_run",
)

# Per-slot chain entries, in report order. ``run_dir`` is relative to the
# cron_tracking root with ``{day}`` standing in for YYYYMMDD (no dashes).
# 67aa802b (slippage) and a46bf007 (watchdog) are consumer-only roles (see
# their run_classification.role='consumer' in config/scheduled_tasks.json)
# with no dedicated artifact directory of their own; their status is derived
# from model_usage.jsonl instead (see _build_consumer_row).
CHAIN_SLOTS: List[Dict[str, Any]] = [
    {"et": "09:35", "task_id": "2ba19526", "stream": "market_open", "run_dir": "market_open_dca/{day}_0935ET"},
    # bb4f6ef1's two sub-invocations share a cron ID *and* a scheduled ET, so
    # cron_id/scheduled_for_et alone cannot tell them apart: a watchlist
    # artifact copied into the preflight directory would satisfy this slot and
    # be counted as the machine-complete preflight the 10:30 gate depends on.
    # expected_run_type is the discriminator -- codex review
    # discussion_r3658801012. (morning_dca.py's MORNING_PREFLIGHT_RUN_TYPE /
    # MORNING_WATCHLIST_RUN_TYPE.)
    {
        "et": "09:45",
        "task_id": "bb4f6ef1",
        "stream": "preflight",
        "run_dir": "morning_dca_preflight/{day}_0945ET",
        "expected_run_type": "morning_dca_preflight",
        # Read-only: the 9:45 pre-flight computes eligibility and writes its
        # artifact set, but places no orders and records no order CSV or
        # order_submit_status. Its order count is therefore a KNOWN zero (see
        # read_only_zero_order_note).
        "read_only_zero_orders": True,
    },
    {
        "et": "09:45",
        "task_id": "bb4f6ef1",
        "stream": "watchlist",
        "run_dir": "morning_dca_watchlist/{day}_0945ET",
        "expected_run_type": "morning_dca_watchlist_scan",
        # Read-only, same as the pre-flight above: a watchlist scan never
        # places an order.
        "read_only_zero_orders": True,
    },
    {"et": "10:30", "task_id": "42173154", "stream": "morning", "run_dir": "morning_dca/{day}_1030ET"},
    {"et": "11:30", "task_id": "7be82b97", "stream": "intraday", "run_dir": "intraday_dca/{day}_1130ET"},
    {"et": "12:30", "task_id": "a6239dae", "stream": "midday", "run_dir": "midday_dca/{day}_1230ET"},
    {"et": "13:30", "task_id": "7be82b97", "stream": "intraday", "run_dir": "intraday_dca/{day}_1330ET"},
    {"et": "14:30", "task_id": "7be82b97", "stream": "intraday", "run_dir": "intraday_dca/{day}_1430ET"},
    {"et": "15:30", "task_id": "10ce362c", "stream": "afternoon", "run_dir": "ce16ba6b/scheduled_runs/{day}_1530ET"},
    {"et": "15:35", "task_id": "67aa802b", "stream": "slippage", "run_dir": None},
    {"et": "15:40", "task_id": "a46bf007", "stream": "watchdog", "run_dir": None},
]

# Every stream a recovery_and_escalation_runs row can carry: every CHAIN_SLOTS
# stream, plus "recovery" -- the manual-recovery fallback bucket for a scanned
# root with no mapped stream (see _stream_from_subroot). Used to
# pre-initialize recovery_run_count_by_stream so an all-clean day still
# reports every stream's honest 0 -- codex review discussion_r3658511865.
ALL_STREAMS = tuple(dict.fromkeys([slot["stream"] for slot in CHAIN_SLOTS])) + ("recovery",)

# e3e6f09e (15:05 monitor-only signal check) is PAUSED, not part of
# must_cover_active_daily_chain's eight active ids -- reported as paused
# rather than missing (active_daily_chain_note), but never counted in any
# scheduled/recovery metric -- codex review discussion_r3658263864.
PAUSED_MONITOR_SLOT = {"et": "15:05", "task_id": "e3e6f09e", "stream": "monitor"}

# Task ids whose in-repo Python entrypoint routes through
# dca_alerts.guarded_run, so an attempt_log marker (see _read_attempt_marker)
# can actually exist for them. 2ba19526/bb4f6ef1/7be82b97 have no in-repo
# executor (the external scheduler/agent invokes them directly) and
# 67aa802b/a46bf007 are read-only consumers of other tasks' artifacts, so for
# those five a did_not_run classification cannot rule out an unrecorded crash
# -- see the caveat this generator adds for them (codex review
# discussion_r3658096689).
MARKER_COVERED_TASK_IDS = frozenset({"42173154", "a6239dae", "10ce362c"})


def _did_not_run_model_usage_rows(task_id: str) -> Any:
    """model_usage_rows for a did_not_run row: ``0`` only when the absence is
    PROVEN, else ``"not_available"``.

    For a MARKER_COVERED_TASK_IDS stream, did_not_run means both no run
    directory AND no attempt_log marker, which together do rule out an
    attempted-and-crashed invocation -- so zero model usage is a measured
    fact. For the other five ids no in-repo code writes a marker at all, so
    the same evidence cannot tell a slot that never fired from one that
    crashed after consuming model usage. Hardcoding 0 there asserted a
    measurement this report's own caveat simultaneously disclaims, and could
    hold model_load at low despite genuinely unknown usage -- codex review
    discussion_r3659713621.
    """
    return 0 if task_id in MARKER_COVERED_TASK_IDS else "not_available"

# The canonical afternoon manual-run root lives directly under STATE_ROOT
# (ce16ba6b), separate from MANUAL_EXCLUSION_SUBROOTS (which lists only the
# ADDITIONAL roots outside that tree) -- codex review discussion_r3658263744.
MANUAL_SCAN_SUBROOTS = tuple(MANUAL_EXCLUSION_SUBROOTS) + ("ce16ba6b/manual_runs",)

# First path component of a MANUAL_SCAN_SUBROOTS entry -> the stream it
# belongs to, used as a fallback when a manual run's own run_classification
# doesn't carry a stream field -- codex review discussion_r3658511853 (a
# hard-coded 'recovery' stream defeated recovery_run_count_by_stream's
# per-stream breakdown for every manual row).
_MANUAL_SUBROOT_STREAM = {
    "morning_dca": "morning",
    "midday_dca": "midday",
    "ce16ba6b": "afternoon",
}


def _stream_from_subroot(subroot: str) -> str:
    return _MANUAL_SUBROOT_STREAM.get(subroot.split("/", 1)[0], "recovery")


# The recovery_artifact_types taxonomy declared in
# config/scheduled_tasks.json -> credit_usage_proxy_report_2026_07_27, in
# contract order. recovery_artifact_types_rule requires EVERY type to be
# reported, with zero for a type nothing was found under, so "searched and
# found none" is distinguishable from "never searched" -- codex review
# discussion_r3659140599.
RECOVERY_ARTIFACT_TYPES = (
    "same_day_recovery_run",
    "manual_recovery_artifact",
    "manual_rerun",
    "scheduled_recovery_attempt",
    "order_placement_recovery_artifact",
    "missed_run_recovery_artifact",
    "parent_recovery_run",
    "subagent_escalation",
    "failed_scheduled_start",
    "retry",
    "repair_artifact",
)

# Chain-slot recovery `source` -> its recovery_artifact_types label. Each row
# is filed under exactly ONE type, the most specific one its own evidence
# supports, per recovery_counter_membership_rule's deduplication clause ("a
# recovery rerun that is also a retry is filed once, as
# scheduled_recovery_attempt or same_day_recovery_run, not listed again under
# retry") -- which is also why the standalone `retry` and
# `same_day_recovery_run` labels stay at zero while their artifacts are filed
# under the more specific scheduled_recovery_attempt/failed_scheduled_start.
# Recovery `source` values whose row CROSS-REFERENCES an artifact already
# represented by a substantive scheduled row, rather than being an execution
# of its own. Such a row must not be re-counted into the aggregates, and must
# not be ranked as a second usage driver for the same run_id.
CROSS_REFERENCE_RECOVERY_SOURCES = (
    "recovery_rerun",
    "parent_recovery_submission",
    "subagent_escalation",
    "repair_artifact",
)

_SOURCE_ARTIFACT_TYPE = {
    "recovery_rerun": "scheduled_recovery_attempt",
    "parent_recovery_submission": "parent_recovery_run",
    "subagent_escalation": "subagent_escalation",
    "repair_artifact": "repair_artifact",
    "failed_start_escalation": "failed_scheduled_start",
}

# Narrow, documented shapes that mark a manual recovery as recovering a slot
# that never fired (missed_morning_run_recovery_policy), rather than one
# repairing a run that did fire. Checked only after an explicit boolean field,
# and only against the reason the artifact itself recorded.
#
# A bare `missed` alternative also matched "missed fill for AAPL" or
# "reconcile missed order" -- wording about a missed ORDER/FILL on a slot that
# DID fire, not a missed RUN -- and mis-filed it as missed_run_recovery_artifact,
# corrupting the per-type recovery inventory whose missed-run category
# specifically means the slot never fired. "missed"/"never" must therefore be
# paired with a word identifying the run/slot itself (in either order) --
# codex review discussion_r3660464552.
_MISSED_RUN_REASON_PATTERN = re.compile(
    r"(?i)\bmissed[ _-]?(?:run|slot|invocation|schedule|cron)\b"
    r"|\b(?:run|slot|invocation|schedule|cron)[ _-]?missed\b"
    r"|\bdid[ _-]?not[ _-]?(?:fire|run)\b"
    r"|\bnever[ _-]?(?:fired|ran)\b"
)


def _manual_recovery_artifact_type(subroot: str, run_state: Dict[str, Any], recorded_reason: str) -> str:
    """The recovery_artifact_types label for a run_state-backed manual row.

    missed_run_recovery_artifact requires a positive signal that the slot
    never fired: an explicit boolean the producer stamped, or -- best effort,
    since this repo defines no dedicated field -- the recorded reason matching
    missed_morning_run_recovery_policy's own documented phrasing. Absent that,
    a manual_recovery/ root is a manual_recovery_artifact and every other
    documented manual root (manual_runs, manual_reruns, final_manual_review)
    is a manual_rerun -- codex review discussion_r3659140599.
    """
    recovery = run_state.get("recovery") if isinstance(run_state.get("recovery"), dict) else {}
    rc = run_state.get("run_classification") if isinstance(run_state.get("run_classification"), dict) else {}
    for source in (run_state, recovery, rc):
        for field in ("missed_run_recovery", "missed_scheduled_run", "recovers_missed_run"):
            if source.get(field) is True:
                return "missed_run_recovery_artifact"
    if recorded_reason and _MISSED_RUN_REASON_PATTERN.search(recorded_reason):
        return "missed_run_recovery_artifact"
    if "manual_recovery" in subroot:
        return "manual_recovery_artifact"
    return "manual_rerun"

REQUIRED_SECTIONS = (
    "scheduled_runs",
    "recovery_and_escalation_runs",
    "usage_counts",
    "proxy_bands",
    "biggest_usage_drivers",
    "caveats",
    # Derived rather than free-text: an empty list is what render_markdown
    # turns into output_mode.narrative_rule's single clean-day statement --
    # codex review discussion_r3658688924.
    "actionable_issues",
)

PER_RUN_REQUIRED_FIELDS = (
    "run_id",
    "stream",
    "triggering_issue",
    "artifacts_written",
    "order_attempts",
    "connector_calls",
    "model_usage_rows",
    "classification",
)

# Per-(task_id, stream) required-artifact groups for a valid_scheduled_evidence
# row, sourced from each task's OWN documented contract in
# config/scheduled_tasks.json (tasks[*].artifacts.always_write,
# tasks[*].state_file_guarantee.always_write, and
# also_runs_required_preflight_writer.preflight_artifacts.files) -- never the
# aspirational live_reliability_fix_2026_07_17.always_write_artifacts blocks,
# which 42173154's own manifest_accuracy_note says documents "the intended
# contract, not current behavior". Each group is a tuple of names, any ONE of
# which satisfies it (mirroring scheduled_artifacts.SIGNAL_CSV_NAMES/
# SUMMARY_FILENAMES' own any-of shape). A (task_id, stream) with no entry, or
# a group not listed for it, is not asserted here -- e.g. submitted_orders.csv
# for morning/midday is write_if_applicable (only emitted once an order is
# attempted), never an unconditional requirement -- codex review
# discussion_r3658790021.
REQUIRED_ARTIFACT_GROUPS: Dict[Any, tuple] = {
    ("2ba19526", "market_open"): (
        SUMMARY_FILENAMES,
        ("market_open_signals.csv",),
        (SUBMITTED_ORDERS_CSV_FILENAME,),
    ),
    ("bb4f6ef1", "watchlist"): (("watchlist_signals.csv",),),
    ("bb4f6ef1", "preflight"): (
        ("signals.csv",),
        # also_runs_required_preflight_writer.preflight_artifacts.files
        # declares all FOUR of run_state.json, validation.csv, signals.csv,
        # and run_summary.json. validation.csv was the one omitted here, so a
        # preflight missing the artifact the 10:30 gate actually validates
        # against reported no missing artifacts and raised no actionable
        # issue -- codex review discussion_r3660192627.
        ("validation.csv",),
        SUMMARY_FILENAMES,
    ),
    ("42173154", "morning"): (("morning_signals.csv",),),
    ("7be82b97", "intraday"): (
        SUMMARY_FILENAMES,
        ("intraday_signals.csv",),
        (SUBMITTED_ORDERS_CSV_FILENAME,),
    ),
    ("a6239dae", "midday"): (SUMMARY_FILENAMES,),
    ("10ce362c", "afternoon"): (
        # near_close_dca.py's own artifact check is the ONLY production
        # validate_run_artifacts(...) call site in this repo that sets
        # require_summary=True, and that flag deliberately rejects
        # latest_run_summary.json (the separate post-trade/consumer report
        # dca_reporting writes) as a stand-in for the canonical run_summary.json
        # -- the canonical file must mirror the run state's own classification,
        # which the consumer report never does. Accepting the any-of
        # SUMMARY_FILENAMES group here let an afternoon run missing its
        # canonical summary report no missing artifact as long as the
        # unrelated consumer report happened to exist -- codex review
        # discussion_r3660464558.
        (CANONICAL_RUN_SUMMARY_FILENAME,),
        (SUBMITTED_ORDERS_CSV_FILENAME,),
    ),
}


def _missing_required_artifacts(task_id: str, stream: str, artifact_paths: List[str]) -> List[str]:
    """Required-artifact groups (see REQUIRED_ARTIFACT_GROUPS) this scanned
    run_dir's own top-level file list does not satisfy, as the first (most
    canonical) name in each unmet group -- e.g. ['run_summary.json'] when
    neither run_summary.json nor latest_run_summary.json was found. Empty
    when every documented group is satisfied, or this (task_id, stream) has
    no documented requirement beyond a parseable run_state.json. Previously
    this scan reported whatever files happened to exist with no comparison
    to the stream's own contract, so a directory missing a mandatory
    artifact (e.g. market_open with only run_state.json) was silently
    accepted as complete -- codex review discussion_r3658790021.
    """
    groups = REQUIRED_ARTIFACT_GROUPS.get((task_id, stream))
    if not groups:
        return []
    present = set(artifact_paths)
    return [group[0] for group in groups if not any(name in present for name in group)]


def _yyyymmdd(trading_day: str) -> str:
    return trading_day.replace("-", "")


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    """The JSON object at ``path``, or None when it is absent, unreadable,
    malformed, or decodes to something that is not an object.

    ``UnicodeDecodeError`` is caught explicitly: it is a ``ValueError``
    subclass but neither an ``OSError`` nor a ``json.JSONDecodeError``, so a
    truncated or non-UTF-8 ``run_state.json`` would otherwise escape and
    abort the whole one-time report instead of degrading that one run to
    unavailable evidence -- codex review discussion_r3658801044.

    A syntactically valid non-object payload (``[1]``, ``"x"``, ``3``) is
    rejected for the same reason: every caller here treats the result as a
    mapping and would raise ``AttributeError`` on the first ``.get()``,
    letting one corrupt artifact or attempt marker prevent both report
    artifacts from being generated -- codex review discussion_r3658688909.
    """
    try:
        parsed = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _scan_run_dir(run_dir: Path) -> Dict[str, Any]:
    """Every persisted file under ``run_dir``, recursively (relative posix
    paths, so nested raw/ connector captures are included -- codex review
    discussion_r3658332732), plus their total size in bytes.

    ``bytes`` is ``"not_available"`` when ANY enumerated file could not be
    stat'ed (it vanished mid-scan, or an I/O/permission error). Silently
    omitting that file's size would return a partial total presented as
    complete and could select a lower artifact_load band -- codex review
    discussion_r3658688914.
    """
    if not run_dir.is_dir():
        return {"paths": [], "bytes": 0}
    paths: List[str] = []
    total_bytes = 0
    bytes_measurable = True
    for p in sorted(run_dir.rglob("*")):
        try:
            is_file = p.is_file()
        except OSError:
            # is_file() itself stats the path; a vanished/unreadable entry
            # fails the same way the later size read below does, and must
            # degrade the same way -- not propagate and abort the whole
            # report. This entry cannot be confirmed a regular file, so it is
            # not added to paths, but the byte total is still marked
            # unmeasurable since something real may have been missed.
            bytes_measurable = False
            continue
        if not is_file:
            continue
        paths.append(p.relative_to(run_dir).as_posix())
        try:
            total_bytes += p.stat().st_size
        except OSError:
            bytes_measurable = False
    return {"paths": paths, "bytes": total_bytes if bytes_measurable else "not_available"}


def _measure_path_bytes(path: Path) -> Any:
    """Total bytes at ``path``: the file's own size, or the recursive total of
    every file beneath it when it is a directory.

    ``generate_scheduled_reports()`` logs ``artifact_path=str(output_dir)`` --
    a DIRECTORY, not a file -- so a bare ``stat()`` here measured only the
    directory entry and omitted every report persisted inside it -- codex
    review discussion_r3658801019. Returns ``"not_available"`` when any
    component cannot be stat'ed, for the same reason as _scan_run_dir.
    """
    try:
        if path.is_dir():
            total = 0
            for p in path.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
            return total
        return path.stat().st_size
    except OSError:
        return "not_available"


def _expand_artifact_path(path: Path) -> List[str]:
    """``path`` as a list of concrete persisted file paths: itself when it is a
    file, or every file beneath it when it is a directory (see
    _measure_path_bytes for why a logged artifact_path is often a directory)
    -- codex review discussion_r3658801019.
    """
    try:
        if path.is_dir():
            return sorted(p.as_posix() for p in path.rglob("*") if p.is_file())
        if path.is_file():
            return [path.as_posix()]
    except OSError:
        pass
    return [path.as_posix()]


def _is_current_day_artifact(path: Path, trading_day: str) -> bool:
    """Whether ``path`` is a real file that this trading day actually produced.

    Two independent signals, either of which suffices:
      * the path itself carries the day stamp (``YYYYMMDD`` or ``YYYY-MM-DD``),
        which every scheduled artifact root in this repo embeds; or
      * its modification time falls on the trading day in ET.

    Existence alone is deliberately NOT enough: a consumer's logged
    artifact_path is usually a directory created before any output is written,
    so a run that raised mid-way still left it present, and a stale file from
    an earlier day inside it would otherwise read as today's evidence -- codex
    review discussion_r3659140605. A file whose mtime cannot be read fails
    closed here (it is not counted as proof) rather than being assumed
    current.
    """
    day = _yyyymmdd(trading_day)
    posix = path.as_posix()
    if day in posix or trading_day in posix:
        return path.is_file()
    try:
        if not path.is_file():
            return False
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=_EASTERN)
    except (OSError, OverflowError, ValueError):
        return False
    return modified.strftime("%Y-%m-%d") == trading_day


# order_submit_status values that PROVE a run attempted exactly zero orders,
# so an absent submitted_orders.csv is a definite 0 rather than an unmeasured
# gap. Sourced from scripts/run_classification's own constants: no candidates
# to submit, a dry run (never submits), and a budget-exhausted run (nothing
# left to spend). Deliberately NOT in this set: not_submitted_error and
# not_submitted_confirmation_required, where an attempt may have been made and
# then failed or been blocked mid-flight -- codex review discussion_r3659713603.
_ZERO_ORDER_SUBMIT_STATUSES = frozenset(
    {
        ORDER_SUBMIT_STATUS_NO_CANDIDATES,
        ORDER_SUBMIT_STATUS_DRY_RUN,
        ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
    }
)

# Order-artifact filenames in this repo's own documented preference order,
# mirroring scripts/drift_detection.ORDER_CSV_NAMES. A manual override or
# recovery under midday_dca/manual_reruns persists its attempts only in
# filled_orders.csv, so looking exclusively for submitted_orders.csv reported
# not_logged for a fully recorded recovery -- and because manual rows feed
# placed_order_count, one such row made the whole aggregate unavailable --
# codex review discussion_r3659713605.
_ORDER_CSV_NAMES = (SUBMITTED_ORDERS_CSV_FILENAME, "filled_orders.csv", "orders.csv")


def _count_csv_data_rows(csv_path: Path) -> Optional[int]:
    """Data-row count for ``csv_path`` (header excluded), or None when the file
    is absent, unreadable, or (for ``submitted_orders.csv`` specifically)
    missing/carrying a malformed header.

    scripts/scheduled_artifacts.py's own validate_run_artifacts() treats a
    zero-byte or truncated submitted_orders.csv -- one whose
    write_submitted_orders_csv() call never completed the header -- as
    CORRUPT, not as a valid zero-order file: the header row is what
    distinguishes the two (a genuine no-order run still writes it). Returning
    ``max(0, len(rows) - 1)`` unconditionally read that corrupt shape as a
    measured 0 (an empty file has zero lines), silently undercounting a
    submitted run as a no-op and keeping placed_order_count numeric after the
    artifact was damaged -- codex review discussion_r3660464556. This check
    is scoped to submitted_orders.csv's own documented SUBMITTED_ORDERS_FIELDS
    header: filled_orders.csv/orders.csv (the other names
    _count_submitted_orders falls back to) are manual-recovery producer
    formats with their own, different columns, and have no equivalent
    contract to validate against here.
    """
    if not csv_path.is_file():
        return None
    try:
        with csv_path.open("r", encoding="utf-8") as f:
            rows = [line for line in f if line.strip()]
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, NOT an OSError, so a partially
        # written or non-UTF-8 CSV escaped an OSError-only handler and
        # aborted generate_report() outright -- leaving BOTH report outputs
        # unwritten instead of degrading this one run's order count. An
        # undecodable file is unreadable -- codex review
        # discussion_r3659293079 and discussion_r3659297982.
        return None
    if not rows:
        return None
    if csv_path.name == SUBMITTED_ORDERS_CSV_FILENAME:
        header = next(csv.reader([rows[0]]), None)
        if header != list(SUBMITTED_ORDERS_FIELDS):
            return None
    return len(rows) - 1


def _count_submitted_orders(
    run_dir: Path, run_state: Optional[Dict[str, Any]] = None, read_only: bool = False
) -> Any:
    """Order-attempt count for ``run_dir``, or ``"not_logged"``.

    ``read_only`` marks a slot whose role cannot place orders at all -- the
    9:45 pre-flight and watchlist scans (CHAIN_SLOTS ``read_only_zero_orders``).
    Their documented artifact sets contain no order CSV and no
    order_submit_status precisely BECAUSE they are read-only, so the fallbacks
    below find nothing and returned not_logged. Since placed_order_count
    aggregates every valid scheduled row and propagates any non-integer, one
    successful 9:45 row made the whole day's order total not_available, and the
    row could not count toward no_op_count either -- even though its order
    count is necessarily zero. This mirrors the existing zero-order treatment
    of the read-only consumer roles in _build_consumer_row (Codex review:
    "Record zero orders for read-only 9:45 slots").

    A real order artifact still wins if one is somehow present, so this can
    only ever replace an UNMEASURED not_logged with the known zero, never
    override measured evidence.

    Reads the first persisted order artifact found in _ORDER_CSV_NAMES, so a
    manual recovery that recorded its attempts in filled_orders.csv rather
    than submitted_orders.csv is still counted -- codex review
    discussion_r3659713605.

    With no readable order CSV at all, a run_state whose order_submit_status
    is one of _ZERO_ORDER_SUBMIT_STATUSES still yields ``0``: that status is
    the run's own persisted proof it attempted nothing, which is a measured
    zero, not an unmeasured gap. Returning not_logged there kept the run out
    of no_op_count and forced placed_order_count to not_available even though
    the evidence was on disk -- codex review discussion_r3659713603.
    """
    for name in _ORDER_CSV_NAMES:
        count = _count_csv_data_rows(run_dir / name)
        if count is not None:
            return count
    if isinstance(run_state, dict):
        status = run_state.get("order_submit_status")
        if isinstance(status, str) and status in _ZERO_ORDER_SUBMIT_STATUSES:
            return 0
    if read_only:
        return 0
    return "not_logged"


def _notifications_sent_from(run_state: Optional[Dict[str, Any]]) -> Any:
    """Notifications this run persisted, or ``"not_logged"``.

    ``near_close_dca.build_usage_proxy()`` records
    ``usage_proxy.notifications_sent``; the report hardcoded
    notification_count to not_logged and so claimed the source was
    unavailable even when the exact count was sitting in the artifact --
    codex review discussion_r3659559689. The field is explicitly Optional in
    build_usage_proxy (it defaults to None), so a null is a genuine "this run
    did not record it" and stays out of scope rather than becoming a 0.
    """
    if not isinstance(run_state, dict):
        return "not_logged"
    usage_proxy = run_state.get("usage_proxy")
    if not isinstance(usage_proxy, dict):
        return "not_logged"
    value = usage_proxy.get("notifications_sent")
    if isinstance(value, bool) or not isinstance(value, int):
        return "not_logged"
    return value


def _connector_calls_from(run_state: Optional[Dict[str, Any]]) -> Any:
    """Connector pull count persisted by this run, or ``"not_logged"``.

    ``near_close_dca.build_usage_proxy()`` persists
    ``usage_proxy.finance_calls_attempted`` and
    ``usage_proxy.robinhood_calls_attempted`` on every run it writes -- real,
    already-on-disk telemetry this report previously discarded, hardcoding
    connector_pull_count_where_logged (and therefore the connector_load band)
    to unavailable even on days the data was right there -- codex review
    discussion_r3659298016. ``not_logged`` is reserved for a run whose
    run_state genuinely carries neither field, which is the honest answer for
    the streams that never write a usage_proxy block.
    """
    if not isinstance(run_state, dict):
        return "not_logged"
    usage_proxy = run_state.get("usage_proxy")
    if not isinstance(usage_proxy, dict):
        return "not_logged"
    total = 0
    found = False
    for field in ("finance_calls_attempted", "robinhood_calls_attempted"):
        value = usage_proxy.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        total += value
        found = True
    return total if found else "not_logged"


def _mask_account_number(value: Optional[str]) -> Optional[str]:
    """Mask all but the last 4 digits, per account_number_obfuscation_rule."""
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def _account_number_from(run_state: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(run_state, dict):
        return None
    rc = run_state.get("run_classification")
    if isinstance(rc, dict) and rc.get("account_number"):
        return rc["account_number"]
    top_level = run_state.get("account_number")
    # The top-level field is no longer the raw account number for an
    # artifact `near_close_dca.build_run_artifacts` writes: it now holds a
    # non-reversible HMAC fingerprint (or the "account-fingerprint-
    # unavailable" sentinel), not the account identifier itself (Codex
    # review, PR #911: "Persist a non-sensitive account fingerprint
    # instead"). run_classification carries no account_number at all for
    # those artifacts (both scheduled and manual), so this fallback used
    # to mask and publish a meaningless fingerprint suffix under
    # "account_number", corrupting attribution for every newly written run
    # (Codex review, PR #911 round 29: "Stop reporting the fingerprint as
    # an account number"). A genuine account number is purely numeric (a
    # 6-12 digit Robinhood account number, per this repo's own account
    # number convention); a fingerprint is a 32-character lowercase hex
    # digest and the sentinel contains a hyphen, so neither passes this
    # check. Scoped this way rather than dropped entirely so an artifact
    # written BEFORE the fingerprint change -- which genuinely stored the
    # raw account number here -- still reports it correctly.
    if isinstance(top_level, str) and top_level.isdigit() and 6 <= len(top_level) <= 12:
        return top_level
    return None


def _run_id_from(run_state: Optional[Dict[str, Any]]) -> Optional[str]:
    """``run_state``'s run_id, checking the nested ``run_classification``
    block before the top-level field -- the same nested-or-top-level
    fallback ``_identity_matches()`` itself accepts (``rc.get("run_id") or
    run_state.get("run_id")``). A producer that stores run_id only inside
    run_classification still passes identity as valid_scheduled_evidence, so
    a caller checking the top-level field alone discarded that ID and
    reported ``no_run_id_written`` for a run _identity_matches() had just
    accepted -- breaking the exact-run-ID contract and hiding that run's
    telemetry from ``_model_usage_rows_for_run()`` (and its premium usage
    from model_load) -- codex review discussion_r3661312420.
    """
    if not isinstance(run_state, dict):
        return None
    rc = run_state.get("run_classification")
    if isinstance(rc, dict) and rc.get("run_id"):
        return rc["run_id"]
    return run_state.get("run_id")


# Secret-shaped substrings to strip from free-text exception messages before
# they are embedded into triggering_issue. Best-effort, not exhaustive: a live
# adapter's raised error can include a raw broker response, account
# identifier, tokenized URL, or credential verbatim, which would otherwise
# violate no_secrets_rule -- codex review discussion_r3658373978.
# A secret-bearing key name, allowing the affixes real payloads use
# (access_token, refresh_token, client_secret, X-Api-Key, db_password...).
# The core terms are matched as substrings of the surrounding identifier
# rather than on \b boundaries: "access_token" contains no word boundary
# before "token" (an underscore is a word character), so a \btoken\b
# alternative never matched the single most common credential key there is --
# codex review discussion_r3660192592 (P1).
_SECRET_KEY = r"[A-Za-z0-9_.-]*(?:api[_-]?key|token|password|passwd|pwd|secret|authorization|credential)[A-Za-z0-9_.-]*"

# The value after the key: a quoted string, or an unquoted run stopping at the
# delimiters that end a JSON/query value, so a match does not swallow the
# enclosing brace or the next field.
_SECRET_VALUE = r"""(?:"[^"]*"|'[^']*'|[^\s,;}\]&]+)"""

_SECRET_TEXT_PATTERNS = (
    # key=value, key: value, AND the quoted JSON form "key": "value".
    # The optional quote around the key is backreferenced so an opening quote
    # must be closed -- a JSON fragment like {"access_token":"secret"} puts the
    # closing quote BETWEEN the key and the colon, which a bare `key\s*[:=]`
    # pattern cannot match, so the credential passed through unredacted into
    # both report formats -- codex review discussion_r3660192592 (P1).
    re.compile(rf"""(?i)(["']?){_SECRET_KEY}\1\s*[:=]\s*{_SECRET_VALUE}"""),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)://[^\s/@]+:[^\s/@]+@"),  # userinfo credentials in a URL
    # A URL whose path embeds an opaque, credential-shaped segment -- a
    # Slack webhook (https://hooks.slack.com/services/T.../B.../<secret>) or
    # an API token placed directly in a path -- with no recognizable KEY
    # naming it at all, so the key=value/JSON rule above (which requires a
    # key like "token"/"secret" immediately before the value) never matches
    # it and the complete credential passed through unredacted into both
    # report formats -- codex review discussion_r3660858022 (P1). 16+
    # contiguous letters/digits/underscores/hyphens is long enough to catch
    # real webhook and API-key path segments while leaving short, ordinary
    # path components (ids, slugs) alone. The WHOLE url is redacted, not
    # just the token segment, since the host/path shape can itself already
    # identify which service the credential belongs to.
    re.compile(r"(?i)https?://(?=\S*[A-Za-z0-9_-]{16,})\S+"),
    re.compile(r"\b\d{9,12}\b"),  # bare account/routing-shaped numbers
)


def _redact_secrets(text: str) -> str:
    """Best-effort redaction of secret- or account-number-shaped substrings
    from ``text`` before it is embedded in triggering_issue, per
    no_secrets_rule -- codex review discussion_r3658373978. A filter over
    known-shaped patterns, not a substitute for adapters never raising with
    secrets embedded in the first place.
    """
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _load_model_usage_entries(model_usage_path: Path) -> Optional[List[Dict[str, Any]]]:
    """Every parsed entry in the day's monthly model_usage.jsonl, or None if
    the file is absent OR any line in it is malformed/non-object/missing its
    required run_id/carrying an invalid routing mode.

    A malformed line could itself be a dropped premium-routed entry, and its
    usage cannot safely be inferred as absent -- silently skipping bad lines
    would understate model_load rather than surfacing the uncertainty. The
    whole file is therefore treated as unavailable rather than partially
    parsed -- codex review discussion_r3658332743.

    A syntactically valid JSON object can still be schema-invalid in a way
    that causes SILENT miscounting rather than a clean non-match: a
    corrupted or partially-written line missing
    scripts.model_usage_log.REQUIRED_FIELDS' run_id, or carrying a
    model_routing_mode outside MODEL_ROUTING_MODE_VALUES, is still accepted
    by every downstream check here (they only ever compare against KNOWN
    values, so an absent/garbled one just never equals anything -- it
    doesn't raise). _premium_run_ids() in particular reads a
    missing/malformed model_routing_mode as plain non-premium rather than
    unknown, silently understating model_load instead of surfacing the
    uncertainty a real producer's own build_entry() would never have let
    onto disk in the first place -- codex review discussion_r3661312388.
    Malformed in exactly the sense the docstring above already treats the
    whole file as unavailable for. Every OTHER REQUIRED_FIELDS entry
    (stream, run_origin, triggered_at_utc, artifact_path, ...) is already
    guarded by this report's own exact-match predicates elsewhere (an
    absent one simply fails to match anything, the same safe degrade as a
    genuinely irrelevant row), so is not re-validated here.

    A truncated file or one containing an invalid UTF-8 byte raises
    UnicodeDecodeError from read_text() -- a ValueError subclass, not an
    OSError, so it previously escaped this function and aborted
    generate_report() outright (both report outputs went unwritten) instead
    of degrading this one metric to unavailable, exactly like _load_json
    already handles it for run_state.json/attempt markers -- codex review
    discussion_r3659124627.
    """
    if not model_usage_path.is_file():
        return None
    entries: List[Dict[str, Any]] = []
    try:
        with model_usage_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    return None
                if not isinstance(parsed, dict):
                    return None
                if not isinstance(parsed.get("run_id"), str) or not parsed["run_id"]:
                    return None
                if parsed.get("model_routing_mode") not in MODEL_ROUTING_MODE_VALUES:
                    return None
                entries.append(parsed)
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, NOT an OSError, so a partially
        # written or non-UTF-8 monthly log escaped this handler and aborted
        # the whole report. This helper already converts a malformed log to
        # None so the model metrics degrade to not_available; an undecodable
        # one is malformed in exactly the same sense -- codex review
        # discussion_r3659293067.
        return None
    return entries


def _model_usage_rows_for_run(entries: Optional[List[Dict[str, Any]]], run_id: str) -> Any:
    """Rows matching run_id, per per_run_field_rules.model_usage_rows.

    ``not_available`` whenever NO row matches this exact run_id -- whether
    the monthly log file itself is absent/malformed, or present (other runs
    logged fine) but missing a row for this specific run.
    scripts/model_usage_log.py's log_model_usage() is deliberately
    best-effort and silently swallows its own append failures, so a missing
    row never proves zero usage; treating it as 0 could hide a premium-routed
    run and understate model_load -- codex review discussion_r3658096711.
    Only ever an int (the exact matching count) when at least one row
    matches.
    """
    if entries is None:
        return "not_available"
    count = sum(1 for e in entries if e.get("run_id") == run_id)
    return count if count > 0 else "not_available"


def _is_failed_start_marker(marker: Dict[str, Any]) -> bool:
    """Whether ``marker`` records a crash BEFORE the run wrote its artifact.

    dca_alerts.guarded_run also writes a marker when a run raises AFTER
    persisting a valid run_state.json (the ``generate_full_reports=True`` path
    fails after ``run_daily_compact()`` has written the run). Those carry
    ``failure_phase='after_run_artifact'`` and are ordinary late errors, not
    failed starts: treating them as failed starts made the report invent an
    earlier failed start plus a successful retry, inflating recovery/error
    counts and forcing the high error band for a run that completed its work
    -- codex review discussion_r3659298031. A marker with no failure_phase at
    all (written before that field existed) is treated as a failed start,
    preserving the prior behavior for existing artifacts.
    """
    return marker.get("failure_phase") != "after_run_artifact"


def _read_all_attempt_markers(cron_root: Path, trading_day: str, stream: str) -> tuple:
    """``(markers, corrupted)``: EVERY durable marker
    dca_alerts.write_failed_start_marker wrote for ``stream`` today, oldest
    first, regardless of failure phase, plus whether a line in the
    append-only history could not be parsed. Callers that want only genuine
    failed starts use :func:`_read_attempt_markers`; the late-error
    remainder is surfaced via :func:`_late_error_markers`.

    Prefers the append-only ``<stream>.attempts.jsonl`` history, which holds
    one line per attempt: a stream that failed more than once today would
    otherwise be represented by the single ``<stream>.json`` marker alone,
    which each failure overwrites, so the report saw only the last failure and
    undercounted error_count/retry evidence/recovery rows -- codex review
    discussion_r3659140585 and discussion_r3659124633. Falls back to the single marker when no history
    file exists (a marker written before the history was introduced, or an
    attempt whose history append failed).

    ``corrupted`` is True when a NON-EMPTY line in the history failed to
    parse (bad JSON, or valid JSON that isn't an object) and was skipped.
    That line's own evidence is genuinely lost: if it was an EARLIER attempt
    (not the latest), the single-marker fallback below can only ever recover
    the latest one, so the markers this function DOES return silently
    understate a real, persisted attempt history with no indication anything
    was dropped. Corruption is reported even when the surviving markers are
    non-empty -- a truncated/malformed MIDDLE line between two good ones is
    exactly the case a plain empty-vs-nonempty check would miss -- codex
    review discussion_r3661312403. Callers surface this as a caveat rather
    than aborting; see _scan_recovery_runs.

    dca_alerts.write_failed_start_marker resolves its own write root as
    ``root or $DCA_ATTEMPT_LOG_ROOT or DEFAULT_ATTEMPT_LOG_ROOT``, and no
    production call site passes an explicit ``root``, so a deployment that
    sets ``DCA_ATTEMPT_LOG_ROOT`` to anything other than
    ``<cron_root>/attempt_log`` writes markers there instead. Always reading
    ``<cron_root>/attempt_log`` here regardless of that env var silently
    missed every marker on such a deployment -- a real failed start persisted
    successfully but reported as did_not_run -- codex review
    discussion_r3660464523.
    """
    attempt_log_root = Path(os.environ[ATTEMPT_LOG_ROOT_ENV]) if os.environ.get(ATTEMPT_LOG_ROOT_ENV) else cron_root / "attempt_log"
    day_dir = attempt_log_root / _yyyymmdd(trading_day)
    markers: List[Dict[str, Any]] = []
    corrupted = False
    history_path = day_dir / f"{stream}{ATTEMPT_LOG_HISTORY_SUFFIX}"
    if history_path.is_file():
        try:
            with history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        corrupted = True
                        continue
                    if isinstance(parsed, dict):
                        markers.append(parsed)
                    else:
                        corrupted = True
        except (OSError, UnicodeDecodeError):
            markers = []
            corrupted = True

    single = _load_json(day_dir / f"{stream}.json")
    if not markers:
        return ([single] if isinstance(single, dict) else []), corrupted
    # The single <stream>.json marker is merged in whenever it is DISTINCT
    # from everything the history holds -- never gated on the history having
    # visibly lost a line. write_failed_start_marker appends to the history
    # FIRST and writes the single marker second, so the newest failure can be
    # missing from an otherwise perfectly clean history in two ways: the
    # append was truncated mid-write (discussion_r3659559667), or it raised
    # OSError and never landed at all while the single write still succeeded
    # (discussion_r3660192619). The second case leaves a history that parses
    # without complaint, so a lossiness gate dropped the latest failed start
    # in exactly the append-failure scenario the single marker exists to
    # survive. Identity is the (triggered_at_utc, error_class, error_message)
    # triple, which write_failed_start_marker fills for both files from one
    # exception -- so when the history DOES already hold this marker, the
    # dedupe below is what prevents a double count.
    if isinstance(single, dict):
        def _identity(marker: Dict[str, Any]) -> tuple:
            return (
                marker.get("triggered_at_utc"),
                marker.get("error_class"),
                marker.get("error_message"),
            )

        if _identity(single) not in {_identity(m) for m in markers}:
            markers.append(single)
    return markers, corrupted


def _read_attempt_markers(cron_root: Path, trading_day: str, stream: str) -> tuple:
    """``(markers, corrupted)`` -- only the markers recording a genuine
    failed start -- see :func:`_is_failed_start_marker` for why a late-phase
    marker must not classify a completed run as a failed start.
    """
    markers, corrupted = _read_all_attempt_markers(cron_root, trading_day, stream)
    return [m for m in markers if _is_failed_start_marker(m)], corrupted


def _late_error_markers(cron_root: Path, trading_day: str, stream: str) -> tuple:
    """``(markers, corrupted)`` -- the complement of
    :func:`_read_attempt_markers`: markers for a run that persisted its
    artifact and only then raised. These are real errors and still count
    toward error_count via the run's own recorded_error, but they are never
    failed starts -- codex review discussion_r3659298031.
    """
    markers, corrupted = _read_all_attempt_markers(cron_root, trading_day, stream)
    return [m for m in markers if not _is_failed_start_marker(m)], corrupted


# run_id prefix per (task_id, stream), matching each stream's own
# scheduled_run_id_prefix/folder_suffix convention (morning_dca.py's
# MORNING_PROFILES, midday_dca.py's equivalent, and the documented literals
# for the streams with no in-repo executor -- market_open, intraday,
# afternoon). "{slot}" is filled from the chain slot's own et
# (e.g. "11:30" -> "1130") for 7be82b97, which fires at three different ET
# slots under the same task_id.
_EXPECTED_RUN_ID_PREFIX = {
    ("2ba19526", "market_open"): "market_open_live_{day}_0935ET",
    ("bb4f6ef1", "preflight"): "morning_preflight_{day}_0945ET",
    ("bb4f6ef1", "watchlist"): "morning_{day}_0945ET",
    ("42173154", "morning"): "morning_live_{day}_1030ET",
    ("7be82b97", "intraday"): "intraday_live_{day}_{slot}ET",
    ("a6239dae", "midday"): "midday_live_{day}_1230ET",
    ("10ce362c", "afternoon"): "scheduled_{day}_1530ET",
}


def _expected_run_id(slot: Dict[str, Any], trading_day: str) -> Optional[str]:
    """The run ID this slot would have used had it completed, per
    failed_start_field_rule / discussion_r3658334863: a failed-start row
    records ``no_run_id_written`` (see NO_RUN_ID) plus this expected ID, so
    consumers can still tie an attempt to its logical run identity across
    retries. Only defined for slots with a documented naming convention
    (every CHAIN_SLOTS entry with its own run_dir); returns None for the
    consumer-only slippage/watchdog roles, which have no run_id template of
    their own to fail into.
    """
    template = _EXPECTED_RUN_ID_PREFIX.get((slot["task_id"], slot["stream"]))
    if template is None:
        return None
    day = _yyyymmdd(trading_day)
    return template.format(day=day, slot=slot["et"].replace(":", ""))


def _identity_matches(rc: Dict[str, Any], slot: Dict[str, Any], run_state: Dict[str, Any], trading_day: str) -> bool:
    """Lightweight scheduled-identity check beyond run_origin/manual_run: the
    classification block's own cron_id (or supersedes_cron_id) must name this
    slot's task, a present scheduled_for_et must roughly match this slot's
    et, and the run's own trading_day (top-level, matching this repo's
    manual-recovery producers) must match the day this report covers. A
    stale, wrong-stream, wrong-day, or malformed classification therefore
    cannot silently pass as this slot's evidence -- codex review
    discussion_r3658263785 and discussion_r3658416961. This is a lightweight
    check, not the full per-stream schema validation scripts/run_classification.py
    performs for 10ce362c specifically; it catches the identity mismatches
    that are cheap to check generically across every stream. run_id is
    deliberately NOT cross-checked against an expected pattern here: unlike
    cron_id/scheduled_for_et/trading_day, this repo has no single run_id
    template guaranteed stable across every recovery/backfill shape, so
    treating a run_id mismatch as a hard identity failure risked a false
    failed_start_no_evidence for a legitimately-completed run.

    Required-field presence: scripts/run_classification.SCHEDULED_REQUIRED_FIELDS
    documents that a real scheduled classification always carries cron_id,
    scheduled_for_et, run_id, triggered_at_utc, and state_schema_version.
    Every check below treats an ABSENT field as "nothing to contradict" and
    skips it, so a crash that left a partial-but-syntactically-valid state
    (only run_origin='scheduled'/manual_run=false, every identity field
    missing) passed every check purely for lack of anything to compare
    against, inflating run_count_scheduled instead of surfacing as failed
    evidence -- codex review discussion_r3659124641. Missing any of these
    five is now an identity failure on its own; run_id/triggered_at_utc are
    accepted from either rc or the top-level run_state, matching where this
    repo's own producers place them.

    scheduled_for_et specifically is REQUIRED, not merely checked when
    present: for 7be82b97 (the only task whose slots share a cron_id across
    multiple ETs), omitting the field entirely skipped both the direct et
    comparison AND the combined-form run_id discriminator below, so an
    artifact from one slot (e.g. 11:30) copied into another slot's directory
    (e.g. 13:30) with scheduled_for_et stripped was accepted as that other
    slot's own valid evidence -- codex review discussion_r3660464531.
    """
    if not rc.get("cron_id"):
        return False
    if not (rc.get("run_id") or run_state.get("run_id")):
        return False
    if not (rc.get("triggered_at_utc") or run_state.get("triggered_at_utc")):
        return False
    if not rc.get("state_schema_version"):
        return False
    scheduled_for_et = rc.get("scheduled_for_et")
    if not isinstance(scheduled_for_et, str) or not scheduled_for_et:
        return False
    cron_id = rc.get("cron_id")
    supersedes = rc.get("supersedes_cron_id")
    if cron_id is not None or supersedes is not None:
        if cron_id != slot["task_id"] and supersedes != slot["task_id"]:
            return False
    if scheduled_for_et != slot["et"]:
        # Multi-slot tasks (7be82b97) sometimes write a combined string
        # ("11:30,13:30,14:30") instead of the fired slot; that combined form
        # is a known-legacy/logging shape, not evidence of the WRONG slot, so
        # it does not fail identity on its own.
        if slot["et"] not in scheduled_for_et:
            return False
        # ...but it does not IDENTIFY the slot either, and accepting it for
        # every slot it lists let an 11:30 artifact copied into the 13:30
        # directory pass as valid 13:30 evidence, inflating the scheduled,
        # artifact, and model-usage counts. When the combined form is what we
        # have, fall through to the slot's own deterministic run_id
        # (intraday_live_{day}_{slot}ET) as the discriminator instead -- codex
        # review discussion_r3659559657. A run_id that is absent or off-template
        # leaves this no worse than before; only a run_id that positively names
        # a DIFFERENT slot is rejected.
        expected_for_slot = _expected_run_id(slot, trading_day)
        run_id = rc.get("run_id") or run_state.get("run_id")
        if expected_for_slot is not None and isinstance(run_id, str) and run_id != expected_for_slot:
            sibling_ids = {
                _expected_run_id(other, trading_day)
                for other in CHAIN_SLOTS
                if other["task_id"] == slot["task_id"] and other["et"] != slot["et"]
            }
            if run_id in sibling_ids:
                return False
    run_trading_day = run_state.get("trading_day")
    if run_trading_day is None and isinstance(rc, dict):
        run_trading_day = rc.get("trading_day")
    if run_trading_day is not None and run_trading_day != trading_day:
        return False
    # The optional top-level trading_day field above is a manual-recovery-
    # producer convention, not something every SCHEDULED classification
    # stamps -- when absent, nothing above catches a stale prior-day artifact
    # (a real, otherwise-complete classification, just from an earlier
    # trading day) copied or left under today's run directory: run_id is
    # deliberately not date-validated for most slots either (see this
    # function's own docstring). triggered_at_utc IS required (checked
    # above) and every real producer stamps it, so its own ET trading date is
    # now compared against the day this report covers -- a parseable
    # timestamp naming a different day, or one that doesn't parse at all,
    # fails identity just like a missing required field does -- codex review
    # discussion_r3660858026.
    triggered_at_utc = rc.get("triggered_at_utc") or run_state.get("triggered_at_utc")
    parsed_triggered = _parse_utc_timestamp(triggered_at_utc) if isinstance(triggered_at_utc, str) else None
    if parsed_triggered is None:
        return False
    if parsed_triggered.astimezone(_EASTERN).strftime("%Y-%m-%d") != trading_day:
        return False
    # Slots whose (task_id, et) pair is NOT unique -- bb4f6ef1's preflight and
    # watchlist sub-invocations -- additionally pin run_type, so one mode's
    # artifact cannot stand in as the other's evidence -- codex review
    # discussion_r3658801012.
    #
    # REQUIRED, not "checked only when present": run_type is the sole field
    # that tells these two rows apart, and every other identity field they
    # carry (cron_id, scheduled_for_et, trading_day) is identical by
    # construction. Treating absence as unknown therefore let a malformed
    # state satisfy EITHER slot and corrupt the scheduled/artifact counts --
    # codex review discussion_r3659293057. Absence is malformed rather than
    # merely unknown: schemas/morning_run_classification.schema.json lists
    # run_type in `required`, and both real producers always stamp it
    # (morning_dca.py's MorningDCAExecutor.run_classification and
    # scripts/scheduled_artifacts.build_preflight_run_classification), so
    # requiring it here cannot reject a legitimately-completed run.
    expected_run_type = slot.get("expected_run_type")
    if expected_run_type is not None:
        run_type = rc.get("run_type") or run_state.get("run_type")
        if run_type != expected_run_type:
            return False
        # The expected run_id is ALSO deterministic for this ambiguous pair
        # specifically -- each mode's scheduled id is
        # f"{scheduled_run_id_prefix}_{day}_{folder_suffix}" (morning_dca.py's
        # MORNING_PROFILES), fixed for the trading day -- so it is a second
        # independent discriminator, and it still catches a copied artifact
        # whose run_type field is absent. Checked ONLY for these slots, not
        # generically: the docstring above explains why a run_id mismatch is
        # unsafe as a global identity test, and that reasoning does not apply
        # to a pair whose ids are pinned by a shared same-day template --
        # codex review discussion_r3658790008.
        expected = _expected_run_id(slot, trading_day)
        run_id = run_state.get("run_id")
        if expected is not None and isinstance(run_id, str) and run_id and run_id != expected:
            return False
    return True


def _run_state_recorded_error(run_state: Dict[str, Any]) -> Optional[str]:
    """A concrete recorded-error signal on an otherwise-valid completed run
    (e.g. order_submit_status='not_submitted_error', a blocking_reason, or a
    connector error field), distinct from failed_start_no_evidence (which
    requires no artifact was ever written). These are a normal documented
    failure path and must still count toward error_count -- codex review
    discussion_r3658332759.

    Every free-form value below is redacted before it is returned. This
    string is stored as the row's ``recorded_error`` and reaches BOTH report
    formats (the JSON rows, and the Markdown actionable-issues and
    usage-driver sections), so a blocking_reason copied verbatim from a
    connector exception -- which can carry a token or an account number --
    would bypass the _redact_secrets() protection already applied to
    recovery reasons and failed-start markers and break no_secrets_rule --
    codex review discussion_r3659559649 (P1). order_submit_status is a
    closed enum and errors_count an int, but both are redacted too rather
    than relying on that: the cost is nil and it removes the standing risk
    that a future producer widens either field.
    """
    status = run_state.get("order_submit_status")
    if isinstance(status, str) and status.endswith("_error"):
        return _redact_secrets(f"order_submit_status={status}")
    rc = run_state.get("run_classification")
    blocking_reason = None
    if isinstance(rc, dict):
        blocking_reason = rc.get("blocking_reason")
    blocking_reason = blocking_reason or run_state.get("blocking_reason")
    if blocking_reason:
        # Redacted: unlike order_submit_status (a constrained status string)
        # and usage_proxy.errors_count (an int), blocking_reason is arbitrary
        # free text a producer wrote, so it can carry a credential, tokenized
        # URL, or account identifier -- and this value is copied verbatim into
        # the JSON report, the Markdown actionable-issues list, and
        # biggest_usage_drivers evidence, bypassing no_secrets_rule
        # everywhere -- codex review discussion_r3659561350 and
        # discussion_r3659559649 (P1).
        return f"blocking_reason={_redact_secrets(str(blocking_reason))}"
    # near_close_dca.build_usage_proxy() records a per-run errors_count that
    # survives a partially-successful run: _derive_order_submit_status makes
    # the WHOLE run 'submitted' as soon as any one order lands, so a run that
    # placed one order and errored on another candidate reports
    # order_submit_status='submitted' with no blocking_reason -- and both
    # checks above return None despite the persisted error evidence. That
    # under-counted error_count and dropped the actionable issue entirely --
    # codex review discussion_r3659298024.
    usage_proxy = run_state.get("usage_proxy")
    if isinstance(usage_proxy, dict):
        errors_count = usage_proxy.get("errors_count")
        if isinstance(errors_count, int) and errors_count > 0:
            return _redact_secrets(f"usage_proxy.errors_count={errors_count}")
    return None


def _classify_chain_run(
    run_dir_exists: bool,
    run_dir_has_files: bool,
    run_state: Optional[Dict[str, Any]],
    attempt_markers: List[Dict[str, Any]],
    slot: Dict[str, Any],
    trading_day: str,
) -> str:
    """Classify a chain-slot run per classification_values.

    The run_dir not existing at all is ambiguous by itself: it could mean no
    attempt occurred (did_not_run), or an attempt crashed before writing
    anything (failed_start_no_evidence) -- Slack/heartbeat alerts leave no
    local trace, so attempt_markers (see _read_attempt_markers) is the
    tie-breaker: if any same-day marker exists for this stream, a crash
    actually happened, so this is failed_start_no_evidence, not did_not_run
    -- codex review discussion_r3658096689. Absent every marker AND absent a
    run directory, the slot is did_not_run.

    The run_dir existing but run_state.json missing/unreadable/malformed is
    failed_start_no_evidence ONLY when there is other evidence an invocation
    was actually attempted: either the directory holds at least one other
    file (run_dir_has_files -- e.g. a partial log dropped before the crash),
    or at least one same-day attempt marker exists. A run_dir that exists but
    is completely empty and has no attempt marker is not itself proof an
    invocation was attempted -- it can be a directory pre-created by
    deployment, a cleanup tool, or an earlier setup step -- so that case is
    did_not_run, not failed_start_no_evidence (which would otherwise wrongly
    inflate error_count and force error_load to high) -- codex review
    discussion_r3658509331.

    A persisted run_state with run_origin='scheduled', manual_run=False, and
    a matching identity (_identity_matches) -> valid_scheduled_evidence.
    top_up_context_only itself (that is a same-day top-up guard's own
    determination, not something derivable generically here); callers with
    stream-specific top-up context may override.
    """
    if not isinstance(run_state, dict):
        if not run_dir_has_files and not attempt_markers:
            return "did_not_run"
        return "failed_start_no_evidence"
    rc = run_state.get("run_classification")
    if not isinstance(rc, dict):
        return "failed_start_no_evidence"
    if rc.get("manual_run") is True or rc.get("run_origin") == "manual":
        return "manual_context_only"
    if rc.get("run_origin") == "scheduled" and rc.get("manual_run") is False:
        # A run whose ARTIFACT contract was rejected is not valid evidence
        # however complete its identity looks. near_close_dca deliberately
        # persists a top-level artifact_contract_failure marker (and sets
        # run_classification.valid_scheduled_evidence=false) for exactly this
        # case, and scripts.run_classification.is_scheduled_state() fails
        # closed on the same marker. Keying only off the identity fields let
        # a contract-rejected partial-placement run into run_count_scheduled
        # and presented it as successful evidence -- codex review
        # discussion_r3659559695.
        if run_state.get("artifact_contract_failure") is not None:
            return "failed_start_no_evidence"
        if rc.get("valid_scheduled_evidence") is False:
            return "failed_start_no_evidence"
        if not _identity_matches(rc, slot, run_state, trading_day):
            return "failed_start_no_evidence"
        return "valid_scheduled_evidence"
    return "failed_start_no_evidence"


def _consumer_matches(
    slot: Dict[str, Any], trading_day: str, model_usage_entries: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """This slot's scheduled model_usage.jsonl rows for ``trading_day``."""
    if model_usage_entries is None:
        return []
    day_prefix = trading_day  # triggered_at_utc is 'YYYY-MM-DDTHH:MM:SSZ'
    return [
        e
        for e in model_usage_entries
        if (e.get("cron_id") == slot["task_id"] or e.get("task_id") == slot["task_id"])
        # A manual rerun of the same consumer task on the report date would
        # otherwise still match on task_id + date alone and get counted as a
        # scheduled execution -- require the log's own run_origin and this
        # slot's stream before treating it as scheduled evidence -- codex
        # review discussion_r3658511867.
        and e.get("run_origin") == "scheduled"
        and e.get("stream") == slot["stream"]
        and isinstance(e.get("triggered_at_utc"), str)
        and e["triggered_at_utc"].startswith(day_prefix)
    ]


# The complete output set a consumer must have written for an invocation to
# count as successful. dca_reporting.generate_reports() is documented to
# "generate all four post-trade report files", and writes exactly these -- so
# accepting ANY single current-day file let an invocation that wrote its first
# report and then raised pass as a clean run, hiding a fired-but-failed
# attempt the `finally` block had already logged -- codex review
# discussion_r3660192605. a46bf007 has no comparable documented set, so it
# keeps the any-current-day-file test.
_CONSUMER_REQUIRED_OUTPUTS = {
    "67aa802b": (
        "slippage_ranked.csv",
        "missing_fills_report.csv",
        "slippage_and_coverage_report.md",
        "latest_run_summary.json",
    ),
}


def _parse_utc_timestamp(value: Any) -> Optional[datetime]:
    """Parse a ``triggered_at_utc``-shaped string (model_usage_log.py's own
    ``"%Y-%m-%dT%H:%M:%SZ"`` format) into an aware UTC datetime, or None for
    anything absent or malformed.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _consumer_output_complete(
    task_id: str, artifact_paths: List[str], trading_day: str, newer_than: Optional[datetime] = None
) -> bool:
    """Whether ``artifact_paths`` satisfy this consumer's documented output set.

    Every required name must be present as a concrete current-day file (see
    _is_current_day_artifact). With no documented set for the task, any single
    current-day file suffices, which is the prior behavior.

    ``newer_than``, when given, additionally requires every counted file's
    mtime to be strictly AFTER that moment. Two invocations can log the SAME
    output directory (a retry that reuses the prior attempt's output_dir), and
    a directory's on-disk contents cannot otherwise be attributed to whichever
    invocation actually produced them: a later invocation that raised before
    writing anything still sees an earlier invocation's already-complete
    output and would be wrongly credited with it, since evaluating each row
    only against its OWN artifact_path cannot tell the two apart when that
    path is identical. _consumer_invocations passes the PREVIOUS invocation
    sharing this exact path's own triggered_at_utc here, so only files this
    invocation could plausibly have written itself (i.e. newer than the prior
    attempt's own completion) count -- codex review discussion_r3660464510.
    """
    current_day = [Path(p) for p in artifact_paths if _is_current_day_artifact(Path(p), trading_day)]
    if newer_than is not None:
        fresh = []
        for p in current_day:
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime > newer_than:
                fresh.append(p)
        current_day = fresh
    required = _CONSUMER_REQUIRED_OUTPUTS.get(task_id)
    if not required:
        return bool(current_day)
    present = {p.name for p in current_day}
    return all(name in present for name in required)


def _consumer_invocations(
    slot: Dict[str, Any], trading_day: str, model_usage_entries: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """One evaluated record per matching invocation, in log order.

    Each model_usage.jsonl row is a SEPARATE invocation, so each is evaluated
    against its OWN artifact_path rather than against the union of every
    match. dca_reporting.generate_scheduled_reports() appends its row from a
    `finally` block, so a run that raised still logs one; merging all matches
    and asking `any(...)` whether some output exists let a successful RETRY
    mask an earlier failed attempt -- the combined attempts were reported as a
    single successful scheduled run, no failed-start recovery row was emitted,
    and retry_count stayed 0 (Codex review: "Preserve failed consumer attempts
    when a retry succeeds").

    Each record carries the entry, its expanded artifact paths, the measured
    byte total for those paths, and whether at least one of them is a concrete
    file carrying this trading day's identity.
    """
    invocations: List[Dict[str, Any]] = []
    # Logged output path -> the most recent PRIOR invocation sharing that
    # exact path's own triggered_at_utc. A retry commonly reuses the same
    # output_dir as the attempt before it, so completeness alone cannot tell
    # which invocation actually produced the files currently on disk -- see
    # _consumer_output_complete's newer_than parameter.
    prior_triggered_by_path: Dict[str, datetime] = {}
    for entry in _consumer_matches(slot, trading_day, model_usage_entries):
        logged = entry.get("artifact_path")
        # scripts/model_usage_log.py's REQUIRED_FIELDS documents artifact_path
        # as a string, but a malformed/corrupted log row can carry any JSON
        # value there (e.g. a stray 42). A non-string value is degraded to
        # "no artifact recorded" -- exactly like a missing artifact_path --
        # rather than reaching Path(logged), which raises TypeError for a
        # non-str/PathLike value and previously escaped generate_report()
        # entirely, preventing both report outputs from being written --
        # codex review discussion_r3660464514.
        if not isinstance(logged, str):
            logged = None
        artifact_paths: List[str] = (
            list(dict.fromkeys(_expand_artifact_path(Path(logged)))) if logged else []
        )
        artifact_bytes: Any = 0
        for artifact_path in artifact_paths:
            measured = _measure_path_bytes(Path(artifact_path))
            if measured == "not_available" or artifact_bytes == "not_available":
                artifact_bytes = "not_available"
            else:
                artifact_bytes += measured
        # Bare Path.exists() on the logged path is too weak a test, because
        # that path is usually a DIRECTORY the consumer creates (or is handed)
        # BEFORE it writes anything: an empty or pre-existing output_dir plus a
        # raise still left exists()==True, so the row was classified
        # valid_scheduled_evidence and could even count a previous day's
        # leftover files as today's evidence. Require instead at least one
        # concrete FILE carrying this trading day's identity -- its own path
        # naming the day, or an mtime that falls on it -- codex review
        # discussion_r3659140605.
        invocations.append(
            {
                "entry": entry,
                "artifact_paths": artifact_paths,
                "artifact_bytes": artifact_bytes,
                # ...and, where the consumer has a documented output set, that
                # the WHOLE set landed: one file plus a raise is a failed
                # invocation, not a successful one -- codex review
                # discussion_r3660192605. When this exact output path was
                # already logged by an earlier invocation, only files newer
                # than THAT invocation's own completion can be this
                # invocation's own work -- codex review discussion_r3660464510.
                "output_exists": _consumer_output_complete(
                    slot["task_id"], artifact_paths, trading_day,
                    newer_than=prior_triggered_by_path.get(logged) if logged else None,
                ),
            }
        )
        if logged:
            parsed = _parse_utc_timestamp(entry.get("triggered_at_utc"))
            if parsed is not None:
                prior_triggered_by_path[logged] = parsed
    return invocations


def _build_consumer_row(slot: Dict[str, Any], trading_day: str, model_usage_entries: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """67aa802b/a46bf007 have no dedicated artifact directory, but each
    appends its own model_usage.jsonl row when it fires successfully -- that
    log is a persisted usage record this generator already loads, so deriving
    status from it (rather than unconditionally declaring did_not_run) avoids
    a false missed-run row -- codex review discussion_r3658263726.
    """
    if model_usage_entries is not None:
        invocations = _consumer_invocations(slot, trading_day, model_usage_entries)
        matches = [inv["entry"] for inv in invocations]
        if matches:
            succeeded = [inv for inv in invocations if inv["output_exists"]]
            run_id = (succeeded[0] if succeeded else invocations[0])["entry"].get("run_id") or NO_RUN_ID
            # Each matching entry carries a required artifact_path (see
            # scripts/model_usage_log.py's REQUIRED_FIELDS); measure it
            # instead of reporting artifacts_written unavailable, so a
            # successful consumer run still contributes to artifact_count/
            # artifact_bytes_approx -- codex review discussion_r3658511877.
            #
            # The logged path is usually a DIRECTORY (dca_reporting's
            # generate_scheduled_reports logs artifact_path=str(output_dir)),
            # so it is expanded to the files inside rather than counted as one
            # entry -- codex review discussion_r3658801019.
            #
            # Deduplicated across invocations, because a consumer can log the
            # SAME output path on more than one row (a retry, or a duplicate
            # audit append), and two DIFFERENT logged paths can overlap when
            # one is a directory containing the other. Expanding and measuring
            # per row repeated those files in artifacts_written and added their
            # bytes once per row, inflating artifact_count/
            # artifact_bytes_approx and potentially raising artifact_load --
            # codex review discussion_r3659713615. Concrete files are therefore
            # deduplicated first, then measured exactly once each
            # (dict.fromkeys preserves first-seen order).
            #
            # Only the SUCCEEDING invocations' artifacts are reported when any
            # succeeded: a failed attempt contributed nothing that exists.
            counted = succeeded or invocations
            artifact_paths: List[str] = list(
                dict.fromkeys(p for inv in counted for p in inv["artifact_paths"])
            )
            artifact_bytes: Any = 0
            for artifact_path in artifact_paths:
                measured = _measure_path_bytes(Path(artifact_path))
                if measured == "not_available" or artifact_bytes == "not_available":
                    artifact_bytes = "not_available"
                else:
                    artifact_bytes += measured
            # A model-usage row alone does NOT prove the consumer succeeded:
            # dca_reporting.generate_scheduled_reports() appends its entry
            # from a `finally` block, so a run whose generate_reports() raised
            # still logs one -- codex review discussion_r3658801008. Evaluated
            # per invocation (see _consumer_invocations), so a successful retry
            # no longer masks an earlier failed attempt.
            output_exists = bool(succeeded)
            if not output_exists:
                return {
                    "et": slot["et"],
                    "task_id": slot["task_id"],
                    "stream": slot["stream"],
                    "run_id": run_id,
                    "triggering_issue": (
                        "consumer logged a model_usage.jsonl row but none of its recorded artifact_path "
                        "outputs exist; the append happens in a finally block, so this is a fired-but-failed "
                        "run, not successful scheduled evidence"
                    ),
                    "artifacts_written": [],
                    "artifact_bytes": 0,
                    "order_attempts": 0,
                    "connector_calls": "not_logged",
                    "model_usage_rows": len(matches),
                    "account_number": None,
                    "recorded_error": "consumer_output_missing",
                    "classification": "failed_start_no_evidence",
                }
            return {
                "et": slot["et"],
                "task_id": slot["task_id"],
                "stream": slot["stream"],
                "run_id": run_id,
                "triggering_issue": "scheduled",
                "artifacts_written": artifact_paths,
                "artifact_bytes": artifact_bytes,
                # 0, not not_available: these read-only consumer roles
                # cannot place orders when they fire successfully, so this is
                # a known zero rather than an unmeasured unknown -- reporting
                # not_available here forced placed_order_count unavailable on
                # every otherwise-fully-measured day either consumer ran --
                # codex review discussion_r3658511894.
                "order_attempts": 0,
                # Only the SUCCEEDING invocations. Each failed attempt that
                # preceded a success now gets its own recovery-section row
                # carrying model_usage_rows=1, so counting every match here
                # billed the failed attempt's log entry twice -- three rows
                # for two real entries in model_usage_jsonl_row_count --
                # codex review discussion_r3660192611.
                "model_usage_rows": len(succeeded),
                "account_number": None,
                "classification": "valid_scheduled_evidence",
            }
    return {
        "et": slot["et"],
        "task_id": slot["task_id"],
        "stream": slot["stream"],
        "run_id": NOT_APPLICABLE_DID_NOT_RUN,
        "triggering_issue": "Consumer-only role with no dedicated artifact directory; no matching model_usage.jsonl row found for this trading day either (see unavailable_data_rule).",
        "artifacts_written": "not_available",
        "order_attempts": "not_available",
        "connector_calls": "not_logged",
        # not_available, NOT 0: 67aa802b/a46bf007 have no in-repo entrypoint
        # writing an attempt_log marker, so "no artifact and no matching log
        # row" cannot distinguish a slot that never fired from one that
        # crashed after consuming model usage -- which is exactly what this
        # report's own caveat says about these ids. Claiming a measured 0
        # contradicted that caveat and could hold model_load at low despite
        # genuinely unknown usage -- codex review discussion_r3659713621.
        "model_usage_rows": _did_not_run_model_usage_rows(slot["task_id"]),
        "account_number": None,
        "classification": "did_not_run",
    }


def _cross_reference_row(slot: Dict[str, Any], trading_day: str) -> Dict[str, Any]:
    """The scheduled_runs placeholder for a slot whose substantive record
    lives in recovery_and_escalation_runs (failed_start_scheduled_slot_rule).

    The slot WAS attempted, so it must still appear in scheduled_runs
    (must_cover_active_daily_chain), but it is never counted into
    run_count_scheduled, recovery_and_escalation_count, or error_count --
    those are incremented once, from the real record.
    """
    return {
        "et": slot["et"],
        "task_id": slot["task_id"],
        "stream": slot["stream"],
        "run_id": NO_RUN_ID,
        "expected_run_id": _expected_run_id(slot, trading_day),
        "triggering_issue": "see recovery_and_escalation_runs",
        "artifacts_written": "cross_reference_only",
        "order_attempts": "cross_reference_only",
        "connector_calls": "cross_reference_only",
        "model_usage_rows": "cross_reference_only",
        "account_number": None,
        "classification": "failed_start_no_evidence",
    }


def _build_chain_row(
    slot: Dict[str, Any], cron_root: Path, trading_day: str, model_usage_entries: Optional[List[Dict[str, Any]]]
) -> Dict[str, Any]:
    run_dir_template = slot["run_dir"]
    if run_dir_template is None:
        consumer_row = _build_consumer_row(slot, trading_day, model_usage_entries)
        # A consumer that fired but left no output follows the same
        # failed_start_scheduled_slot_rule split as a chain slot: this row
        # becomes a cross-reference and the substantive record is emitted
        # once, by _scan_recovery_runs -- codex review discussion_r3658801008.
        if consumer_row["classification"] == "failed_start_no_evidence":
            return _cross_reference_row(slot, trading_day)
        return consumer_row

    run_dir = cron_root / run_dir_template.format(day=_yyyymmdd(trading_day))
    run_dir_exists = run_dir.is_dir()
    run_state = _load_json(run_dir / RUN_STATE_FILENAME)
    scan = _scan_run_dir(run_dir)
    # Read markers UNCONDITIONALLY, exactly as _scan_recovery_runs does. The
    # old `[] if run_dir_exists else ...` suppression made the two sections
    # contradict each other: a slot whose run directory was pre-created empty
    # but which left a real attempt marker classified did_not_run here (no
    # files, no markers visible) while _scan_recovery_runs -- which never
    # suppressed the marker -- emitted a substantive failed-start row for the
    # same slot. That also left the scheduled row failing to be the
    # cross-reference failed_start_scheduled_slot_rule requires, and made its
    # "no attempt_log marker either" text a falsehood -- codex review
    # discussion_r3659561339.
    # The corrupted flag is surfaced as a caveat by _scan_recovery_runs's own
    # identical scan of this same (cron_root, trading_day, stream); not
    # duplicated here.
    attempt_markers, _ = _read_attempt_markers(cron_root, trading_day, slot["stream"])
    classification = _classify_chain_run(
        run_dir_exists, bool(scan["paths"]), run_state, attempt_markers, slot, trading_day
    )

    if classification == "did_not_run":
        return {
            "et": slot["et"],
            "task_id": slot["task_id"],
            "stream": slot["stream"],
            "run_id": NOT_APPLICABLE_DID_NOT_RUN,
            # Distinguishes the two ways a slot reaches did_not_run, rather
            # than always claiming the directory is absent: it may exist and
            # simply be empty (pre-created by deployment or a cleanup tool),
            # which is not evidence an invocation was attempted. Either way
            # no attempt_log marker exists, since one would have made this
            # failed_start_no_evidence -- codex review discussion_r3659561339.
            "triggering_issue": (
                f"run directory {run_dir} exists but is empty for {trading_day}, and no attempt_log marker exists"
                if run_dir_exists
                else f"no run artifact found under {run_dir} for {trading_day}, and no attempt_log marker either"
            ),
            "artifacts_written": [],
            "order_attempts": "not_logged",
            "connector_calls": "not_logged",
            # 0 only when this slot's stream actually writes an attempt_log
            # marker, so did_not_run is PROVEN (no run directory and no
            # marker) rather than merely unobserved -- see
            # _did_not_run_model_usage_rows.
            "model_usage_rows": _did_not_run_model_usage_rows(slot["task_id"]),
            "account_number": None,
            "classification": "did_not_run",
        }

    if classification == "failed_start_no_evidence":
        return _cross_reference_row(slot, trading_day)

    run_id = _run_id_from(run_state) or NO_RUN_ID
    order_attempts = _count_submitted_orders(run_dir, run_state, read_only=slot.get("read_only_zero_orders", False))

    recorded_error = _run_state_recorded_error(run_state) if isinstance(run_state, dict) else None
    if recorded_error is None:
        # A run that persisted its artifact and only THEN raised leaves a
        # late-phase marker rather than a failed-start one. That is a real
        # error and must still reach error_count -- but as this run's own
        # recorded_error, never as a fabricated separate failed start --
        # codex review discussion_r3659298031.
        late_markers, _ = _late_error_markers(cron_root, trading_day, slot["stream"])
        if late_markers:
            latest = late_markers[-1]
            message = _redact_secrets(str(latest.get("error_message", "(no message recorded)")))
            recorded_error = f"after_run_artifact {latest.get('error_class', 'Exception')}: {message}"

    return {
        "et": slot["et"],
        "task_id": slot["task_id"],
        "stream": slot["stream"],
        "run_id": run_id,
        "triggering_issue": "scheduled",
        "artifacts_written": scan["paths"],
        "artifact_bytes": scan["bytes"],
        "order_attempts": order_attempts,
        "connector_calls": _connector_calls_from(run_state),
        "notifications_sent": _notifications_sent_from(run_state),
        "model_usage_rows": _model_usage_rows_for_run(model_usage_entries, run_id),
        "account_number": _mask_account_number(_account_number_from(run_state)),
        "recorded_error": recorded_error,
        "missing_required_artifacts": _missing_required_artifacts(slot["task_id"], slot["stream"], scan["paths"]),
        "classification": classification,
    }


_REPAIR_ARTIFACT_FILENAME_PATTERN = re.compile(r"(?i)(recover|repair|reconcil|resubmi[st])")

# The standard chain-slot artifact names a repair/reconciliation filename
# scan (see _has_repair_artifact_filename) must not mistake for a repair
# artifact of its own -- run_state.json and each stream's own documented
# signals/summary/orders files are expected in every valid run and are never
# themselves repair evidence. Derived from REQUIRED_ARTIFACT_GROUPS rather
# than a second hard-coded list, so the two stay in sync.
_STANDARD_CHAIN_ARTIFACT_NAMES = frozenset(
    {RUN_STATE_FILENAME} | {name for groups in REQUIRED_ARTIFACT_GROUPS.values() for group in groups for name in group}
)


def _has_repair_artifact_filename(artifact_paths: List[str]) -> bool:
    """Whether ``artifact_paths`` (a run_dir's own scanned file list) names a
    re-submission/repair/reconciliation-shaped artifact of its own, beyond
    the standard run_state.json/signals/submitted_orders.csv/run_summary.json
    set -- e.g. parent_recovery_submission_summary.json (docs/strategy.md's
    2026-07-24 intraday recovery). A run_state.json that sets none of
    recovery_rerun/parent_recovery_submission/recovery_from_escalation would
    otherwise hide this evidence entirely -- codex review
    discussion_r3658509308.
    """
    return any(
        Path(p).name not in _STANDARD_CHAIN_ARTIFACT_NAMES and _REPAIR_ARTIFACT_FILENAME_PATTERN.search(Path(p).name)
        for p in artifact_paths
    )


def _recovery_artifact_source(run_state: Dict[str, Any]) -> Optional[str]:
    """Which recovery_artifact_types label (if any) this chain-slot's own
    run_state names on its own, beyond the historically-only-checked
    recovery_rerun=true. parent_recovery_submission and
    recovery_from_escalation are both real, independent recovery signals a
    scheduled run_state can carry -- recovery_from_escalation in particular
    is stamped by scripts.run_classification.build_raw_persistence_state /
    raw_persistence_failure_run_state without ever setting recovery_rerun --
    and keying recovery-section membership on recovery_rerun alone silently
    dropped both from the recovery section -- codex review
    discussion_r3658509308.

    Ordered MOST-SPECIFIC FIRST, because the flags are not exclusive: the
    repository's own documented parent-recovery shape
    (intraday_dca_scheduled_recovery_2026_07_24) sets recovery_rerun=true AND
    parent_recovery_submission=true on the same run_state. Checking the
    generic rerun flag first filed that shape as scheduled_recovery_attempt
    and left parent_recovery_run at zero even with its dedicated summary
    artifact present, which is exactly the miscount
    recovery_counter_membership_rule's "filed once, under whichever type is
    most specific to it" forbids -- codex review discussion_r3659559675.
    """
    if run_state.get("parent_recovery_submission") is True:
        return "parent_recovery_submission"
    if run_state.get("recovery_from_escalation") is True:
        return "subagent_escalation"
    if run_state.get("recovery_rerun") is True:
        return "recovery_rerun"
    return None


def _scan_recovery_runs(
    cron_root: Path, trading_day: str, model_usage_entries: Optional[List[Dict[str, Any]]]
) -> tuple:
    """``(rows, corrupted_attempt_streams)``: recovery/manual/repair/escalation
    rows for recovery_and_escalation_runs, plus the set of streams whose
    attempt_log history had an unparseable line (see
    _read_all_attempt_markers).

    Covers (a) any chain-slot run_state naming a recovery_artifact_types
    signal of its own -- recovery_rerun, parent_recovery_submission, or
    recovery_from_escalation (see _recovery_artifact_source) -- or a
    repair/reconciliation-shaped filename scanned into an otherwise-valid
    run (see _has_repair_artifact_filename): an in-place scheduled recovery
    (scheduled_recovery_rule): its scheduled_runs row is already the
    substantive valid_scheduled_evidence record (see _build_chain_row), so
    this row is a CROSS-reference of that same artifact, not a separate run
    -- it counts toward recovery_and_escalation_count but not
    run_count_recovery, and its artifacts/model-usage rows are NOT re-summed
    into the aggregate totals (see _usage_counts) since they'd otherwise
    double-count the same underlying artifact (codex review
    discussion_r3658263754, discussion_r3658332738, discussion_r3658509308);
    (b) any chain slot classified failed_start_no_evidence -- this is that
    failure's ONE substantive record (failed_start_no_evidence_rule); the
    matching scheduled_runs row is a cross-reference only (see
    _build_chain_row) -- and (c) any run_state under the documented
    manual-run roots dated today (MANUAL_SCAN_SUBROOTS, which includes the
    canonical ce16ba6b/manual_runs root -- codex review
    discussion_r3658263744), classified from its own source artifact
    (manual_recovery_classification_rule), never assumed manual_context_only
    just because it is a recovery. Every row here carries
    recovery_context_label='recovery context only'
    (recovery_context_only_label_rule) in addition to its own classification.
    """
    rows: List[Dict[str, Any]] = []
    day = _yyyymmdd(trading_day)
    # Streams whose attempt_log history had a line that failed to parse (see
    # _read_all_attempt_markers's ``corrupted`` return). That line's own
    # evidence is genuinely lost -- if it recorded an EARLIER attempt, the
    # single-marker fallback can only ever recover the latest one -- so the
    # recovery/error/retry counts derived from these streams' markers may be
    # lower than the persisted history actually contains, with nothing else
    # in the report saying so. Surfaced as a caveat by generate_report()
    # rather than degrading the (many, interdependent) numeric counters
    # themselves to not_available -- codex review discussion_r3661312403.
    corrupted_attempt_streams: set = set()

    for slot in CHAIN_SLOTS:
        run_dir_template = slot["run_dir"]
        if run_dir_template is None:
            # Consumer-only slot: its substantive failed-start record (a run
            # that fired but left no output) is emitted here, matching the
            # cross-reference row _build_chain_row wrote -- codex review
            # discussion_r3658801008.
            consumer_row = _build_consumer_row(slot, trading_day, model_usage_entries)
            invocations = _consumer_invocations(slot, trading_day, model_usage_entries)
            failed = [inv for inv in invocations if not inv["output_exists"]]
            any_succeeded = any(inv["output_exists"] for inv in invocations)
            if consumer_row["classification"] == "failed_start_no_evidence":
                rows.append(
                    {
                        "source": "failed_start_escalation",
                        "recovery_artifact_type": "failed_scheduled_start",
                        "et": slot["et"],
                        "task_id": slot["task_id"],
                        "stream": slot["stream"],
                        "run_id": consumer_row["run_id"],
                        "triggering_issue": consumer_row["triggering_issue"],
                        "artifacts_written": [],
                        "artifact_bytes": 0,
                        "order_attempts": "not_logged",
                        "connector_calls": "not_logged",
                        "model_usage_rows": consumer_row["model_usage_rows"],
                        "account_number": None,
                        "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                        "classification": "failed_start_no_evidence",
                    }
                )
            elif failed and any_succeeded:
                # A failed attempt followed by a successful RETRY. The chain row
                # above is legitimately valid_scheduled_evidence (the retry did
                # produce output), but the failed attempt still consumed a real
                # invocation and must stay visible: without a row here the whole
                # sequence read as one clean scheduled run and retry_count stayed
                # 0 (Codex review: "Preserve failed consumer attempts when a
                # retry succeeds"). evidences_retry is what feeds retry_count.
                for inv in failed:
                    rows.append(
                        {
                            "source": "failed_start_escalation",
                            "recovery_artifact_type": "failed_scheduled_start",
                            "et": slot["et"],
                            "task_id": slot["task_id"],
                            "stream": slot["stream"],
                            "run_id": inv["entry"].get("run_id") or NO_RUN_ID,
                            "triggering_issue": (
                                "consumer invocation logged a model_usage.jsonl row but its recorded "
                                "artifact_path output does not exist; a later same-day invocation "
                                "succeeded, so this attempt is a failed try preceding a successful retry"
                            ),
                            "artifacts_written": [],
                            "artifact_bytes": 0,
                            "order_attempts": "not_logged",
                            "connector_calls": "not_logged",
                            "model_usage_rows": 1,
                            "account_number": None,
                            "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                            "recorded_error": "consumer_output_missing",
                            "evidences_retry": True,
                            "classification": "failed_start_no_evidence",
                        }
                    )
            continue
        run_dir = cron_root / run_dir_template.format(day=day)
        run_dir_exists = run_dir.is_dir()
        run_state = _load_json(run_dir / RUN_STATE_FILENAME)
        scan = _scan_run_dir(run_dir)
        # Read the marker even when a run directory now exists: an initial
        # invocation can crash before writing anything (marker persisted) and
        # an external-scheduler retry then succeed and create the directory.
        # Suppressing the marker in that case erased the failed start from
        # recovery/retry/error load entirely -- codex review
        # discussion_r3658801030.
        attempt_markers, attempt_markers_corrupted = _read_attempt_markers(cron_root, trading_day, slot["stream"])
        if attempt_markers_corrupted:
            corrupted_attempt_streams.add(slot["stream"])
        classification = _classify_chain_run(
            run_dir_exists, bool(scan["paths"]), run_state, attempt_markers, slot, trading_day
        )

        recovery_source = _recovery_artifact_source(run_state) if isinstance(run_state, dict) else None
        if (
            recovery_source is None
            and classification == "valid_scheduled_evidence"
            and _has_repair_artifact_filename(scan["paths"])
        ):
            recovery_source = "repair_artifact"

        if recovery_source:
            rs = run_state if isinstance(run_state, dict) else {}
            run_id = rs.get("run_id") or NO_RUN_ID
            recovery = rs.get("recovery") if isinstance(rs.get("recovery"), dict) else {}
            # Redacted like every other free-text field republished here:
            # raw_persistence_error in particular is a str(exception) from a
            # live adapter and can embed a broker response or credential --
            # codex review discussion_r3659140593.
            triggering_issue = _redact_secrets(
                str(
                    recovery.get("trigger_reason")
                    or rs.get("raw_persistence_error")
                    or f"{recovery_source} (reason not recorded)"
                )
            )
            # A recovery-flagged run whose OWN classification is still
            # failed_start_no_evidence (recovery_rerun=true alongside
            # valid_scheduled_evidence=false -- the documented shape when the
            # bounded recovery also fails) is a genuine failed start, not a
            # cross-reference to an already-counted successful artifact:
            # _build_chain_row converts ANY failed_start_no_evidence
            # classification's scheduled_runs row to a pure cross-reference
            # contributing nothing, so this recovery row is the run's ONE
            # substantive record. Filing it under the original recovery
            # source left it invisible to _usage_counts()'s
            # failed_start_escalation_count/error_count (keyed on
            # source=='failed_start_escalation' specifically) and to
            # error_load's mandatory any_failed_start override -- codex
            # review discussion_r3660464539. Its concrete artifacts/model-
            # usage must also stop being "see scheduled_runs" placeholders
            # for the same reason: there is no longer a scheduled_runs record
            # counting them.
            failed = classification == "failed_start_no_evidence"
            rows.append(
                {
                    "source": "failed_start_escalation" if failed else recovery_source,
                    "recovery_artifact_type": (
                        "failed_scheduled_start" if failed else _SOURCE_ARTIFACT_TYPE[recovery_source]
                    ),
                    "et": slot["et"],
                    "task_id": slot["task_id"],
                    "stream": slot["stream"],
                    "run_id": run_id,
                    "triggering_issue": triggering_issue,
                    "artifacts_written": (
                        scan["paths"] if failed else "see scheduled_runs (same artifact, not re-counted)"
                    ),
                    "artifact_bytes": scan["bytes"] if failed else 0,
                    "order_attempts": "not_logged",
                    "connector_calls": "not_logged",
                    "model_usage_rows": (
                        "not_available" if failed else "see scheduled_runs (same artifact, not re-counted)"
                    ),
                    "account_number": _mask_account_number(_account_number_from(run_state)),
                    "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                    # Carried independently of `source`, which names only the
                    # MOST SPECIFIC artifact type (see
                    # _recovery_artifact_source). The documented parent-recovery
                    # shape sets recovery_rerun=true alongside
                    # parent_recovery_submission=true, and that run really is a
                    # rerun -- keying retry_count off source alone would drop it
                    # the moment the row is filed under the more specific type
                    # -- codex review discussion_r3659559675.
                    "recovery_rerun_flagged": rs.get("recovery_rerun") is True,
                    # Never upgraded/downgraded here: keeps the underlying
                    # run's own classification (failure_recovery_policy keeps
                    # run_origin='scheduled' through a recovery), matching
                    # recovery_section_rule's "never upgrades its own
                    # classification". recovery_context_label above is what
                    # marks this row as recovery context, not a promotion of
                    # the classification itself.
                    "classification": classification,
                }
            )
        elif classification == "failed_start_no_evidence":
            recovery = run_state.get("recovery") if isinstance(run_state, dict) and isinstance(run_state.get("recovery"), dict) else {}
            if attempt_markers:
                # Evidenced by durable local attempt_log markers (the run
                # directory itself never existed) -- use each marker's own
                # error info rather than a generic fallback. ONE ROW PER
                # MARKER: a stream that crashed repeatedly today produced one
                # failed start per attempt, and collapsing them into a single
                # row undercounted error_count and the recovery section --
                # codex review discussion_r3659140585. Redacted: a live
                # adapter's raised exception can embed a raw broker response,
                # account identifier, or credential -- codex review
                # discussion_r3658373978.
                #
                # Only the first row carries this slot's scanned artifacts;
                # the rest report an empty list so one partial-write
                # directory is not re-counted once per attempt.
                for index, marker in enumerate(attempt_markers):
                    error_message = _redact_secrets(str(marker.get("error_message", "(no message recorded)")))
                    attempt_note = (
                        "" if len(attempt_markers) == 1 else f" (attempt {index + 1} of {len(attempt_markers)})"
                    )
                    rows.append(
                        {
                            "source": "failed_start_escalation",
                            "recovery_artifact_type": "failed_scheduled_start",
                            "et": slot["et"],
                            "task_id": slot["task_id"],
                            "stream": slot["stream"],
                            "run_id": NO_RUN_ID,
                            "expected_run_id": _expected_run_id(slot, trading_day),
                            "triggering_issue": f"{marker.get('error_class', 'Exception')}: {error_message}{attempt_note}",
                            "artifacts_written": scan["paths"] if index == 0 else [],
                            "artifact_bytes": scan["bytes"] if index == 0 else 0,
                            "order_attempts": "not_logged",
                            "connector_calls": "not_logged",
                            "model_usage_rows": "not_available",
                            "account_number": None,
                            "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                            "classification": "failed_start_no_evidence",
                        }
                    )
            else:
                rows.append(
                    {
                        "source": "failed_start_escalation",
                        "recovery_artifact_type": "failed_scheduled_start",
                        "et": slot["et"],
                        "task_id": slot["task_id"],
                        "stream": slot["stream"],
                        "run_id": NO_RUN_ID,
                        "expected_run_id": _expected_run_id(slot, trading_day),
                        "triggering_issue": _redact_secrets(
                            str(
                                recovery.get(
                                    "blocking_reason",
                                    "scheduled run failed before writing a valid run_classification",
                                )
                            )
                        ),
                        "artifacts_written": scan["paths"],
                        "artifact_bytes": scan["bytes"],
                        "order_attempts": "not_logged",
                        "connector_calls": "not_logged",
                        "model_usage_rows": "not_available",
                        "account_number": None,
                        "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                        "classification": "failed_start_no_evidence",
                    }
                )

        if run_dir_exists and attempt_markers and classification != "failed_start_no_evidence":
            # An earlier invocation of this same slot crashed before writing
            # anything (durable marker), and a later retry succeeded and
            # created the directory. The successful run keeps its own
            # scheduled_runs row; these are the DISTINCT earlier failed
            # starts, which would otherwise vanish from recovery/retry/error
            # load entirely -- codex review discussion_r3658801030. One row
            # per marker, so repeated crashes before the eventual success are
            # each counted -- codex review discussion_r3659140585.
            for index, marker in enumerate(attempt_markers):
                error_message = _redact_secrets(str(marker.get("error_message", "(no message recorded)")))
                attempt_note = (
                    "" if len(attempt_markers) == 1 else f" (attempt {index + 1} of {len(attempt_markers)})"
                )
                rows.append(
                    {
                        "source": "failed_start_escalation",
                        "recovery_artifact_type": "failed_scheduled_start",
                        "et": slot["et"],
                        "task_id": slot["task_id"],
                        "stream": slot["stream"],
                        "run_id": NO_RUN_ID,
                        "expected_run_id": _expected_run_id(slot, trading_day),
                        "triggering_issue": (
                            f"{marker.get('error_class', 'Exception')}: {error_message}{attempt_note} "
                            "(earlier failed start; a later retry for this slot did write a run directory)"
                        ),
                        "artifacts_written": [],
                        "artifact_bytes": 0,
                        "order_attempts": "not_logged",
                        "connector_calls": "not_logged",
                        "model_usage_rows": "not_available",
                        "account_number": None,
                        "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                        # This crash-then-success pair IS an observed retry of
                        # the slot, even though the succeeding run never
                        # stamped recovery_rerun (an external-scheduler retry
                        # has no in-repo code path to stamp it). retry_count
                        # keys off this flag as well as the recovery_rerun
                        # source, so the evidenced retry stops reading as zero
                        # -- codex review discussion_r3659140576.
                        "evidences_retry": True,
                        "classification": "failed_start_no_evidence",
                    }
                )

        late_markers, late_markers_corrupted = _late_error_markers(cron_root, trading_day, slot["stream"])
        if late_markers_corrupted:
            corrupted_attempt_streams.add(slot["stream"])
        if len(late_markers) > 1 and classification not in ("did_not_run", "failed_start_no_evidence"):
            # write_failed_start_marker() persists one after_run_artifact
            # history entry per attempt (see ATTEMPT_LOG_HISTORY_SUFFIX), so
            # a run that fails in a LATER phase more than once -- report
            # generation fails, the scheduler retries, and it fails again
            # before eventually succeeding -- leaves multiple
            # after_run_artifact markers. _build_chain_row's own
            # recorded_error reflects only the LATEST one (see
            # _late_error_markers usage there); the earlier attempts
            # otherwise vanished from error/retry accounting entirely --
            # codex review discussion_r3661312397. Filed as a distinct
            # source (not failed_start_escalation): an after_run_artifact
            # marker is by definition NOT a failed start -- the run's
            # artifact was already written -- and failed_start_escalation is
            # exactly what forces error_load's mandatory any_failed_start
            # override, which must stay reserved for a genuine failed start.
            row_run_id = _run_id_from(run_state) or NO_RUN_ID
            for index, marker in enumerate(late_markers[:-1]):
                error_message = _redact_secrets(str(marker.get("error_message", "(no message recorded)")))
                rows.append(
                    {
                        "source": "late_error_escalation",
                        "recovery_artifact_type": "retry",
                        "et": slot["et"],
                        "task_id": slot["task_id"],
                        "stream": slot["stream"],
                        "run_id": row_run_id,
                        "triggering_issue": (
                            f"after_run_artifact {marker.get('error_class', 'Exception')}: {error_message} "
                            f"(attempt {index + 1} of {len(late_markers)}; the latest is this run's own recorded_error)"
                        ),
                        "artifacts_written": [],
                        "artifact_bytes": 0,
                        "order_attempts": "not_logged",
                        "connector_calls": "not_logged",
                        "model_usage_rows": "not_available",
                        "account_number": None,
                        "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                        # A retry evidenced by the run's OWN repeated late
                        # failures, not just a crash-before-writing pair --
                        # retry_count keys off this flag too.
                        "evidences_retry": True,
                        "classification": classification,
                    }
                )

    # Dedup key for a manual run's stable identity across mirrored roots (the
    # documented final-manual-override flow writes the same execution under
    # both ce16ba6b/manual_runs and midday_dca/manual_reruns) -- codex review
    # discussion_r3658511860. Only a real run_id is deduplicated on; distinct
    # runs that both lack one (NO_RUN_ID) are never collapsed into each other.
    seen_manual_run_ids: set = set()
    # Directories already represented by a run_state.json row, so the
    # orphan-repair sweep below does not re-report the same execution.
    seen_manual_dirs: set = set()

    for subroot in MANUAL_SCAN_SUBROOTS:
        bases = sorted(cron_root.glob(subroot)) if "*" in subroot else [cron_root / subroot]
        for base in bases:
            if not base.is_dir():
                continue
            for run_state_path in base.rglob(RUN_STATE_FILENAME):
                run_state = _load_json(run_state_path)
                if not isinstance(run_state, dict):
                    continue
                rc = run_state.get("run_classification")
                # trading_day lives at the TOP LEVEL of run_state for this
                # repo's manual-recovery producers (build_final_manual_override_classification
                # and its siblings never nest it inside run_classification), so
                # checking only rc.get('trading_day') always finds None and
                # silently accepts every historical run regardless of date --
                # codex review discussion_r3658263795 (P1). Check the top-level
                # field first; fall back to a nested one if some producer ever
                # does nest it. If NEITHER carries a trading_day, exclude rather
                # than accept: an undated artifact must never be assumed to be
                # today's (never fabricate -- unavailable_data_rule's spirit).
                trading_day_field = run_state.get("trading_day")
                if trading_day_field is None and isinstance(rc, dict):
                    trading_day_field = rc.get("trading_day")
                if trading_day_field != trading_day:
                    continue
                run_id = run_state.get("run_id") or NO_RUN_ID
                if run_id != NO_RUN_ID:
                    if run_id in seen_manual_run_ids:
                        continue
                    seen_manual_run_ids.add(run_id)
                # manual_recovery_classification_rule: read from the source
                # artifact rather than assuming manual_context_only just because
                # it's a recovery. missed_morning_run_recovery_policy and
                # midday_dca_manual_recovery_2026_07_20 both stamp
                # same_day_top_up_context_only=true on their manual recovery
                # artifacts -- those are top_up_context_only, matching the
                # source. manual_context_only is reserved for a manual artifact
                # NOT marked same_day_top_up_context_only=true.
                manual_classification = (
                    "top_up_context_only"
                    if isinstance(rc, dict) and rc.get("same_day_top_up_context_only") is True
                    else "manual_context_only"
                )
                # Stream: prefer the source artifact's own run_classification.stream
                # (when a manual producer records one); otherwise infer it from
                # which documented root it was scanned under, rather than
                # hard-coding a synthetic 'recovery' bucket that defeats
                # recovery_run_count_by_stream's per-stream breakdown -- codex
                # review discussion_r3658511853.
                #
                # The recorded value must still be one of this report's own
                # canonical stream names: a producer-specific alias (e.g. the Jul.
                # 21 midday manual shape's stream='midday_manual') is not a
                # promise this report ever made, and republishing it verbatim
                # both violates the row's allowed stream set and adds an
                # unexpected key to recovery_run_count_by_stream alongside the
                # zero-initialized canonical 'midday' -- so an alias falls back to
                # the same subroot-inferred canonical name a producer that
                # recorded no stream at all would get -- codex review
                # discussion_r3660464546.
                _rc_stream = rc.get("stream") if isinstance(rc, dict) else None
                stream = _rc_stream if _rc_stream in ALL_STREAMS else _stream_from_subroot(subroot)
                # Triggering issue: read the concrete recorded cause from the
                # source artifact (recovery.trigger_reason/blocking_reason, or a
                # blocking_reason directly on run_classification) rather than
                # discarding it for a generic label -- codex review
                # discussion_r3658511885.
                recovery_info = run_state.get("recovery") if isinstance(run_state.get("recovery"), dict) else {}
                # Redacted for the same reason attempt-marker exception text is:
                # these are arbitrary free-text fields a producer wrote, so a
                # trigger_reason/blocking_reason can carry a tokenized URL,
                # credential, account identifier, or a raw connector error
                # verbatim, and it is copied into both the JSON and the Markdown
                # -- no_secrets_rule applies to every free-text field this report
                # republishes, not only the ones from attempt markers -- codex
                # review discussion_r3659140593.
                triggering_issue = _redact_secrets(
                    str(
                        recovery_info.get("trigger_reason")
                        or recovery_info.get("blocking_reason")
                        or (rc.get("blocking_reason") if isinstance(rc, dict) else None)
                        or "manual recovery (reason not recorded)"
                    )
                )
                run_dir = run_state_path.parent
                seen_manual_dirs.add(run_dir)
                scan = _scan_run_dir(run_dir)
                rows.append(
                    {
                        "source": "manual",
                        "recovery_artifact_type": _manual_recovery_artifact_type(
                            subroot, run_state, triggering_issue
                        ),
                        "et": None,
                        "task_id": None,
                        "stream": stream,
                        "run_id": run_id,
                        "triggering_issue": triggering_issue,
                        "artifacts_written": scan["paths"],
                        "artifact_bytes": scan["bytes"],
                        # Parsed from the run's own order CSV when persisted --
                        # submitted_orders.csv, or the filled_orders.csv/orders.csv
                        # a manual override writes instead -- rather than
                        # hard-coded not_logged, so a manual recovery's order
                        # attempts are actually counted into placed_order_count --
                        # codex review discussion_r3658511891 and
                        # discussion_r3659713605.
                        "order_attempts": _count_submitted_orders(run_dir, run_state),
                        "connector_calls": _connector_calls_from(run_state),
                        "notifications_sent": _notifications_sent_from(run_state),
                        "model_usage_rows": _model_usage_rows_for_run(model_usage_entries, run_id),
                        "account_number": _mask_account_number(_account_number_from(run_state)),
                        "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                        "classification": manual_classification,
                    }
                )

    # recovery_artifact_types_rule requires every recovery artifact TYPE to be
    # searched, not only those that happen to carry a run_state.json. An
    # order-reconciliation/repair pass can persist a submitted_orders.csv with
    # no run state at all, which the run_state-keyed sweep above never visits
    # -- codex review discussion_r3658688900. Dated from the directory path
    # (the only date evidence an orphan CSV carries), never assumed to be
    # today's when the path has no day stamp.
    for subroot in MANUAL_SCAN_SUBROOTS:
        bases = sorted(cron_root.glob(subroot)) if "*" in subroot else [cron_root / subroot]
        for base in bases:
            if not base.is_dir():
                continue
            # Every documented order-artifact name, not submitted_orders.csv
            # alone: a manual override that recorded its attempts only in
            # filled_orders.csv would otherwise never be DISCOVERED here, the same
            # single-filename assumption that made _count_submitted_orders report
            # not_logged for it -- codex review discussion_r3659713605.
            orphan_csv_paths = sorted({p for name in _ORDER_CSV_NAMES for p in base.rglob(name)})
            for csv_path in orphan_csv_paths:
                run_dir = csv_path.parent
                if run_dir in seen_manual_dirs:
                    continue
                # Skip only when a run_state.json is present AND parseable: the
                # run_state-keyed sweep above then made a deliberate decision
                # about this directory (emitting a row, or excluding it by date),
                # so re-reporting it here would double-count. A present but
                # MALFORMED/unreadable run_state.json is different: _load_json
                # returned None so that sweep silently skipped it, and the old
                # bare is_file() check excluded it here too -- so the persisted
                # reconciliation attempt disappeared from the recovery rows,
                # order totals, and proxy bands entirely -- codex review
                # discussion_r3659297993.
                if _load_json(run_dir / RUN_STATE_FILENAME) is not None:
                    continue
                if day not in run_dir.as_posix():
                    continue
                seen_manual_dirs.add(run_dir)
                scan = _scan_run_dir(run_dir)
                rows.append(
                    {
                        "source": "manual",
                        # This sweep finds a run directory by its submitted_orders.csv
                        # alone, which is exactly recovery_artifact_types_rule's
                        # order_placement_recovery_artifact: "any re-submission,
                        # repair, or reconciliation artifact touching
                        # submitted_orders.csv" -- codex review discussion_r3659140599.
                        "recovery_artifact_type": "order_placement_recovery_artifact",
                        "et": None,
                        "task_id": None,
                        "stream": _stream_from_subroot(subroot),
                        "run_id": NO_RUN_ID,
                        "triggering_issue": (
                            f"repair/reconciliation artifact under {run_dir} with no run_state.json "
                            "(dated from its path); recorded so the execution is not omitted from the "
                            "recovery section"
                        ),
                        "artifacts_written": scan["paths"],
                        "artifact_bytes": scan["bytes"],
                        "order_attempts": _count_submitted_orders(run_dir),
                        "connector_calls": "not_logged",
                        "model_usage_rows": "not_available",
                        "account_number": None,
                        "recovery_context_label": RECOVERY_CONTEXT_LABEL,
                        "classification": "manual_context_only",
                    }
                )

    return rows, corrupted_attempt_streams


def _usage_counts(
    scheduled_rows: List[Dict[str, Any]],
    recovery_rows: List[Dict[str, Any]],
    model_usage_available: bool = True,
) -> Dict[str, Any]:
    def _sum_int(values: List[Any]) -> Any:
        """Sum values, but propagate not_available/not_logged rather than
        silently dropping them and returning a partial total: if ANY covered
        value isn't a measured int, the aggregate itself is unmeasurable --
        codex review discussion_r3658263811.
        """
        if not values:
            return 0
        if any(not isinstance(v, int) for v in values):
            return "not_available"
        return sum(values)

    def _sum_where_logged(values: List[Any]) -> Any:
        """Sum only the measured ints, for a ``*_where_logged`` counter.

        Deliberately NOT _sum_int: the counter's own name scopes it to the
        runs that logged the value, so a run without that telemetry is out of
        scope rather than an unknown that poisons the total. ``not_logged``
        only when no covered run logged anything at all -- an honest "nothing
        recorded this" rather than a fabricated 0.
        """
        measured = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
        return sum(measured) if measured else "not_logged"

    # run_count_recovery (scheduled_recovery_rule): "the subset of [recovery]
    # rows that represent an actual additional execution with its own run
    # ID." Every recovery_artifact_type counts toward it EXCEPT
    # failed_scheduled_start (zero successful execution by definition) and
    # subagent_escalation (a delegation event, not the executor's own
    # additional run) -- not just source=='manual'. A manual override that
    # lands directly in a chain slot's OWN run_dir (rather than under a
    # separate MANUAL_SCAN_SUBROOTS path) is filed with a more specific
    # source like 'parent_recovery_submission' by _recovery_artifact_source,
    # not 'manual' -- the documented parent-takes-over-after-subagent-
    # failure shape is exactly this: run_origin='manual' (so its OWN
    # classification is manual_context_only, never counted by
    # run_count_scheduled) with parent_recovery_submission=true. Restricting
    # this counter to source=='manual' silently dropped that genuinely
    # separate execution -- codex review discussion_r3661312409.
    #
    # The one case a CROSS_REFERENCE_RECOVERY_SOURCES row must still be
    # excluded here is the documented "successful in-place scheduled
    # recovery" (recovery_rerun, and the parent-recovery shape that sets
    # parent_recovery_submission on that SAME successful run_state): when its
    # own classification actually IS valid_scheduled_evidence, that
    # execution already counted once via run_count_scheduled above, and
    # counting its cross-reference row here too would double it.
    #
    # A concrete run_id is additionally required: recovery_counter_membership_rule
    # defines this counter as "the subset of those rows that represent an
    # actual additional EXECUTION with its own run_id". An orphan
    # repair/reconciliation artifact (a bare submitted_orders.csv with no
    # run_state.json) and a malformed manual state both surface as
    # source='manual' with run_id=NO_RUN_ID -- real evidence worth a
    # recovery-section row, but no identified execution, so counting them
    # here inflated run_count_recovery and, through it, the run_volume band
    # -- codex review discussion_r3659140611.
    run_rows = [
        r
        for r in recovery_rows
        if r.get("recovery_artifact_type") not in ("failed_scheduled_start", "subagent_escalation")
        # late_error_escalation rows always share their run_id with the SAME
        # chain slot's own scheduled_runs row (see the late-marker block
        # above) -- they are a repeated-attempt EVENT within that one run,
        # never a separate execution of their own, so they must never add a
        # second count here.
        and r.get("source") != "late_error_escalation"
        and r.get("run_id") not in (NO_RUN_ID, None)
        and not (r.get("source") in CROSS_REFERENCE_RECOVERY_SOURCES and r.get("classification") == "valid_scheduled_evidence")
    ]

    # recovery_counts_by_stream_rule: every recovery_and_escalation_runs row
    # broken down by stream; must sum to recovery_and_escalation_count. Every
    # stream is pre-initialized to 0 (rather than only ever appearing when a
    # row is actually observed for it), so an all-clean day still reports
    # every stream's honest zero instead of an empty mapping consumers can't
    # tell apart from an unsupported/forgotten stream -- codex review
    # discussion_r3658511865.
    recovery_run_count_by_stream: Dict[str, int] = {stream: 0 for stream in ALL_STREAMS}
    for r in recovery_rows:
        stream = r.get("stream") or "recovery"
        recovery_run_count_by_stream[stream] = recovery_run_count_by_stream.get(stream, 0) + 1

    # recovery_artifact_types_rule: every declared type reports a count, and a
    # type nothing was found under reports 0 rather than being omitted, so a
    # silent miss (a type the generator never searched) is distinguishable
    # from a real zero. Pre-initialized from the full taxonomy for exactly
    # that reason, then incremented from each row's own single most-specific
    # label -- codex review discussion_r3659140599. Sums to
    # recovery_and_escalation_count, since every row carries exactly one type.
    recovery_count_by_artifact_type: Dict[str, int] = {t: 0 for t in RECOVERY_ARTIFACT_TYPES}
    for r in recovery_rows:
        artifact_type = r.get("recovery_artifact_type")
        if artifact_type in recovery_count_by_artifact_type:
            recovery_count_by_artifact_type[artifact_type] += 1

    # artifact_count/model_usage_jsonl_row_count: recovery_rerun,
    # parent_recovery_submission, subagent_escalation, and repair_artifact
    # rows all cross-reference the SAME artifact/model-usage row already
    # counted in scheduled_rows (see _recovery_artifact_source), so only
    # rows outside that set are summed here to avoid double-counting
    # (discussion_r3658263754, discussion_r3658332738, discussion_r3658509308).
    non_duplicate_recovery_rows = [r for r in recovery_rows if r.get("source") not in CROSS_REFERENCE_RECOVERY_SOURCES]

    def _artifact_len(r: Dict[str, Any]) -> int:
        written = r.get("artifacts_written")
        return len(written) if isinstance(written, list) else 0

    all_counted_rows = scheduled_rows + non_duplicate_recovery_rows

    # error_count: a failed_start_escalation record (zero artifact ever
    # written) PLUS any completed scheduled run that recorded a concrete
    # error/blocking-reason field of its own (e.g.
    # order_submit_status='not_submitted_error') -- the latter is a normal
    # documented failure path distinct from a failed start, and was
    # previously invisible to this counter -- codex review
    # discussion_r3658332759. PLUS each earlier late_error_escalation row: a
    # run that failed in a later phase more than once has one persisted
    # after_run_artifact marker per attempt, and only the LATEST is folded
    # into its scheduled row's own recorded_error above -- the earlier
    # attempts are real, distinct error events of their own and must not be
    # collapsed away -- codex review discussion_r3661312397.
    # ⚡ Bolt Optimization: Replace multiple O(N) generator passes with manual loops
    # (one pass over recovery_rows and one pass over scheduled_rows) to avoid
    # redundant iteration overhead when computing multiple counters.
    failed_start_escalation_count = 0
    late_error_escalation_count = 0
    for r in recovery_rows:
        src = r.get("source")
        if src == "failed_start_escalation":
            failed_start_escalation_count += 1
        elif src == "late_error_escalation":
            late_error_escalation_count += 1

    recorded_error_count = 0
    run_count_scheduled = 0
    no_op_count = 0
    for r in scheduled_rows:
        if r.get("recorded_error"):
            recorded_error_count += 1
        # Use .get() consistently: did_not_run and failed-start rows omit
        # "classification" entirely, so strict key access would KeyError.
        if r.get("classification") == "valid_scheduled_evidence":
            run_count_scheduled += 1
        if r.get("order_attempts") == 0:
            no_op_count += 1

    error_count = failed_start_escalation_count + late_error_escalation_count + recorded_error_count

    return {
        "run_count_scheduled": sum(1 for r in scheduled_rows if r.get("classification") == "valid_scheduled_evidence"),
        "run_count_recovery": len(run_rows),
        "no_op_count": no_op_count,
        # Only rows that actually ran (valid_scheduled_evidence, or a manual
        # recovery execution) could have placed an order; did_not_run/
        # failed-start-cross-reference rows contribute nothing and are
        # excluded rather than treated as unknown. Among the runs that DID
        # happen, propagate not_available if any one's order_attempts
        # couldn't be measured (_sum_int), rather than silently summing only
        # the known ones -- codex review discussion_r3658263811,
        # discussion_r3658511891.
        "placed_order_count": _sum_int(
            [r.get("order_attempts") for r in scheduled_rows if r.get("classification") == "valid_scheduled_evidence"]
            + [r.get("order_attempts") for r in recovery_rows if r.get("source") == "manual"]
        ),
        # failed_start_scheduled_slot_rule: the scheduled_runs cross-reference
        # row is never counted here -- only the substantive recovery-section
        # record is, so every row in recovery_rows counts exactly once.
        "recovery_and_escalation_count": len(recovery_rows),
        "recovery_run_count_by_stream": recovery_run_count_by_stream,
        "recovery_count_by_artifact_type": recovery_count_by_artifact_type,
        # Summed from each run's persisted usage_proxy.notifications_sent
        # (see _notifications_sent_from) over exactly the runs that recorded
        # it, rather than hardcoded not_logged -- the report claimed the
        # source was unavailable even when the exact count was in the
        # artifact -- codex review discussion_r3659559689. not_logged only
        # when no covered run recorded one.
        "notification_count": _sum_where_logged(
            [r.get("notifications_sent") for r in all_counted_rows]
        ),
        "notion_updates": "not_logged",
        "drive_updates": "not_logged",
        # cross_reference_only rows are excluded entirely (not even as 0):
        # failed_start_scheduled_slot_rule already counts that slot's real
        # model usage once, via its substantive recovery-section record, so
        # the cross-reference row is not a second data point at all.
        #
        # When the monthly log itself is absent or malformed
        # (model_usage_available=False) the aggregate is not_available
        # outright, never a sum of the did_not_run placeholders' definitional
        # zeroes. Those zeroes are only honest when the log EXISTS and simply
        # has no row for a slot that never ran; with no readable source at
        # all, summing them emitted model_usage_jsonl_row_count=0 while the
        # report's own caveat simultaneously said "reported not_available,
        # not 0" -- a self-contradicting report, and for the streams with no
        # attempt marker a missing artifact cannot prove an invocation
        # consumed no model usage before crashing -- codex review
        # discussion_r3659298011.
        "model_usage_jsonl_row_count": (
            _sum_int(
                [r.get("model_usage_rows") for r in all_counted_rows if r.get("model_usage_rows") != "cross_reference_only"]
            )
            if model_usage_available
            else "not_available"
        ),
        "artifact_count": sum(_artifact_len(r) for r in all_counted_rows),
        # Propagated, not silently dropped: a file that could not be stat'ed
        # makes the whole total unmeasurable rather than a partial sum
        # presented as complete (which could pick a lower artifact_load band)
        # -- codex review discussion_r3658688914.
        "artifact_bytes_approx": _sum_int([r.get("artifact_bytes", 0) for r in all_counted_rows]),
        # "where_logged": summed over exactly the runs that DID persist a
        # usage_proxy connector count (see _connector_calls_from), which is
        # what the counter's own name scopes it to -- runs without that
        # telemetry are skipped rather than poisoning the total to
        # not_available. Only when NO covered run logged any connector call
        # is the whole counter not_logged, which is then the honest answer
        # rather than a fabricated 0 -- codex review discussion_r3659298016.
        "connector_pull_count_where_logged": _sum_where_logged(
            [r.get("connector_calls") for r in all_counted_rows]
        ),
        # Still unlogged: no per-run retry/error connector counters exist in
        # any persisted artifact this generator can read (usage_proxy records
        # attempted calls and an overall errors_count, neither of which is a
        # connector-specific retry or error count).
        "connector_retry_count_where_logged": "not_logged",
        "connector_error_count_where_logged": "not_logged",
        "error_count": error_count,
        # Exposed separately from error_count: an ordinary completed-run
        # recorded_error must not force error_load's any_failed_start
        # override on its own -- only an actual failed_start_no_evidence
        # record should -- codex review discussion_r3658511848.
        "failed_start_escalation_count": failed_start_escalation_count,
        # Two kinds of retry are counted, not just the flagged one:
        #   * source='recovery_rerun' -- an in-place scheduled recovery that
        #     stamped recovery_rerun=true on its own run_state; and
        #   * evidences_retry -- a crash that left an attempt_log marker
        #     followed by a later invocation that DID write the run directory.
        #     An external-scheduler retry has no in-repo code path to stamp
        #     recovery_rerun, so keying only off that flag reported zero
        #     retries for exactly the failed-start-then-success case the
        #     recovery scan already emits a row for, and fed the wrong input
        #     to the artifact/error proxy bands -- codex review
        #     discussion_r3659140576.
        #   * recovery_rerun_flagged -- the run_state's own recovery_rerun
        #     flag, carried separately from `source` because a row filed under
        #     a MORE SPECIFIC type (parent_recovery_submission) can still be a
        #     rerun -- codex review discussion_r3659559675.
        "retry_count": sum(
            1
            for r in recovery_rows
            if r.get("source") == "recovery_rerun" or r.get("recovery_rerun_flagged") or r.get("evidences_retry")
        ),
    }


def _band(value: Any, low_max: int, moderate_max: int) -> str:
    if not isinstance(value, int):
        return "not_available"
    if value <= low_max:
        return "low"
    if value <= moderate_max:
        return "moderate"
    return "high"


def _premium_row_count(
    model_usage_entries: Optional[List[Dict[str, Any]]], known_run_ids: set, model_usage_jsonl_row_count: Any
) -> Any:
    """Count of today's model_usage.jsonl rows with model_routing_mode='premium'.

    Scoped to known_run_ids (today's actual runs) since the log file is
    monthly, not daily -- otherwise this would count other days' premium
    rows too. Returns not_available when the log file itself is absent or
    malformed, OR when model_usage_jsonl_row_count is itself not_available:
    that means at least one covered run's own model_usage_rows could not be
    matched (append is best-effort, so a missing row never proves zero
    usage -- see _model_usage_rows_for_run), and the premium subset of that
    same unmeasurable log is equally unmeasurable. Silently returning a
    numeric count (often 0) in that case would report model_load low even
    though a covered run's model usage is genuinely unknown -- codex review
    discussion_r3658334896.
    """
    if model_usage_entries is None or model_usage_jsonl_row_count == "not_available":
        return "not_available"
    return sum(
        1
        for e in model_usage_entries
        if e.get("run_id") in known_run_ids and e.get("model_routing_mode") == "premium"
    )


# Baseline scheduled-run count on a fully successful, no-recovery day: eight
# active ids, with bb4f6ef1 firing twice (preflight + watchlist) and 7be82b97
# firing three times (11:30/13:30/14:30) -- 6 single-slot ids + 2 + 3 = 11
# (e3e6f09e is paused and contributes none) -- codex review
# discussion_r3658332746, updating the prior (pre-bb4f6ef1-split) baseline of
# 10.
RUN_VOLUME_BASELINE = 11

# Human-readable threshold per proxy dimension, rendered alongside each band so
# a reader can reproduce the derivation from the Markdown report alone --
# codex review discussion_r3658801025. Mirrors _proxy_bands' own predicates.
BAND_THRESHOLDS = {
    "run_volume": f"low <= {RUN_VOLUME_BASELINE}; moderate {RUN_VOLUME_BASELINE + 1}-{RUN_VOLUME_BASELINE + 6}; high >= {RUN_VOLUME_BASELINE + 7}",
    "recovery_load": "low 0; moderate 1-2; high >= 3",
    "connector_load": "low <= 50; moderate 51-90; high >= 91 (not_available when no covered run persisted a connector count)",
    "model_load": "low 0 premium rows; moderate 1; high >= 2",
    "artifact_load": "high if count > 80 or bytes > 5000000 or retry_count >= 2; moderate if count > 50 or bytes > 2000000; else low (precedence: high, then moderate, then low)",
    "error_load": "high if any failed scheduled start; else not_available while connector error/retry telemetry is unlogged; otherwise low 0, moderate 1-3, high >= 4",
}


def _proxy_bands(counts: Dict[str, Any], premium_row_count: Any) -> Dict[str, Any]:
    """Bands per credit_usage_proxy_report_2026_07_27.proxy_bands.dimensions.

    connector_load is banded from connector_pull_count_where_logged alone
    (low <= 50, moderate 51-90, high >= 91), exactly as the contract's own
    note requires: "Bands are derived solely from the
    connector_pull_count_where_logged count. This report's per-run fields do
    not capture call scope or provider identity, so a qualitative cause (a
    full-universe refetch, a provider failover) is not itself a band
    determinant". This band was previously hardcoded not_available on the
    reasoning that routine-vs-refetch could not be distinguished -- which the
    contract explicitly says is NOT a band determinant, so the band was being
    suppressed for a reason the contract had already ruled out, on days the
    count was genuinely measured -- codex review discussion_r3659298016. It
    stays not_available only when no covered run persisted a connector count
    at all (_band returns not_available for a non-int).
    """
    total_runs = counts["run_count_scheduled"] + counts["run_count_recovery"]
    if total_runs >= RUN_VOLUME_BASELINE + 7:
        run_volume_band = "high"
    elif total_runs > RUN_VOLUME_BASELINE:
        run_volume_band = "moderate"
    else:
        run_volume_band = "low"

    # artifact_load precedence_rule: high, then moderate, then low, checked
    # in that fixed order so the predicates are mutually exclusive. Bytes are
    # now actually measured (see _scan_run_dir), so this is never forced to
    # not_available purely for lack of a byte count -- codex review
    # discussion_r3658263766.
    artifact_count = counts["artifact_count"]
    artifact_bytes = counts["artifact_bytes_approx"]
    retry_count = counts["retry_count"]
    bytes_known = isinstance(artifact_bytes, int)
    count_known = isinstance(artifact_count, int)
    high_artifact_load = (count_known and artifact_count > 80) or (bytes_known and artifact_bytes > 5_000_000)
    high_artifact_load = high_artifact_load or (isinstance(retry_count, int) and retry_count >= 2)
    moderate_artifact_load = (count_known and artifact_count > 50) or (bytes_known and artifact_bytes > 2_000_000)
    if high_artifact_load:
        artifact_load_band = "high"
    elif moderate_artifact_load:
        artifact_load_band = "moderate"
    elif count_known and bytes_known:
        artifact_load_band = "low"
    else:
        artifact_load_band = "not_available"

    # model_load: zero premium rows -> low (cost_conscious/default are both
    # non-premium), exactly one -> moderate, two or more -> high.
    model_load_band = _band(premium_row_count, 0, 1)

    # error_load precedence_rule: high, then moderate, then low. Any failed
    # scheduled start is always high regardless of the count. Keyed off
    # failed_start_escalation_count specifically, not the broader error_count
    # (which also includes ordinary completed-run recorded_error values) --
    # an isolated order_submit_status='not_submitted_error' with no failed
    # start must stay in the moderate 1-3 band -- codex review
    # discussion_r3658511848.
    any_failed_start = counts["failed_start_escalation_count"] > 0
    error_and_retry = (
        counts["error_count"] + counts["retry_count"]
        if isinstance(counts["error_count"], int) and isinstance(counts["retry_count"], int)
        else "not_available"
    )
    # A definite failed start pins the band high regardless of telemetry gaps:
    # unlogged connector errors can only ever ADD errors, never remove the one
    # already proven, so this positive evidence is safe to assert.
    #
    # Absent that, error_load leans on connector error/retry telemetry this
    # generator cannot read (connector_error_count_where_logged='not_logged'),
    # so a numeric band derived only from the narrower run-state error count
    # would commonly report low and hide an unpersisted connector failure. The
    # honest answer is not_available, with the observed run-state floor still
    # surfaced below -- codex review discussion_r3658801041.
    error_telemetry_unlogged = any(
        counts.get(key) in ("not_logged", "not_available")
        for key in ("connector_error_count_where_logged", "connector_retry_count_where_logged")
    )
    if any_failed_start:
        error_load_band = "high"
    elif error_telemetry_unlogged:
        error_load_band = "not_available"
    else:
        error_load_band = _band(error_and_retry, 0, 3)

    return {
        "run_volume": {"band": run_volume_band, "observed": total_runs, "recovery_runs": counts["run_count_recovery"]},
        "recovery_load": {"band": _band(counts["recovery_and_escalation_count"], 0, 2), "observed": counts["recovery_and_escalation_count"]},
        "connector_load": {
            "band": _band(counts["connector_pull_count_where_logged"], 50, 90),
            "observed": counts["connector_pull_count_where_logged"],
        },
        "model_load": {"band": model_load_band, "observed": premium_row_count},
        "artifact_load": {"band": artifact_load_band, "observed": artifact_count, "bytes": artifact_bytes, "retry_count": retry_count},
        "error_load": {"band": error_load_band, "observed": error_and_retry, "any_failed_start": any_failed_start},
    }


def _premium_run_ids(model_usage_entries: Optional[List[Dict[str, Any]]]) -> set:
    """Run IDs with at least one premium-routed model_usage.jsonl row.

    biggest_usage_drivers scored purely on row COUNT, so a lone premium
    escalation tied with any routine cost-conscious/default run that logged
    the same number of rows and could be dropped from the stable top-five
    entirely -- even though model_load treats premium routing as materially
    higher usage. Premium rows are ranked distinctly instead -- codex review
    discussion_r3658688929.
    """
    if not model_usage_entries:
        return set()
    return {
        e.get("run_id")
        for e in model_usage_entries
        if e.get("model_routing_mode") == "premium" and e.get("run_id")
    }


def _driver_score(row: Dict[str, Any], premium_run_ids: Optional[set] = None) -> tuple:
    """(score, evidence) for ranking biggest_usage_drivers across every
    measured usage dimension, not just placed-order attempts.

    A day's most expensive work can be a premium-model escalation, a
    recovery/retry, a connector failure, or an artifact-heavy no-order run --
    filtering to only rows with order_attempts > 0 excludes every one of
    those and can render "None recorded" on a day whose real cost was
    exactly that kind of activity (codex review discussion_r3658334936).
    Weights are ordered by how directly each proxy reflects deployed
    capital/work (order attempts > model premium routing > connector pulls >
    recovery source/recorded error > artifact volume) but are not meant to be
    an exact cost model -- they only need to rank, not price, usage.
    """
    parts = []
    score = 0
    order_attempts = row.get("order_attempts")
    if isinstance(order_attempts, int) and order_attempts > 0:
        score += order_attempts * 1000
        parts.append(f"order_attempts={order_attempts}")
    model_usage_rows = row.get("model_usage_rows")
    if isinstance(model_usage_rows, int) and model_usage_rows > 0:
        score += model_usage_rows * 100
        parts.append(f"model_usage_rows={model_usage_rows}")
    # Weighted above a routine model row but below an order attempt: a premium
    # escalation is the day's most expensive model work and must outrank an
    # equal count of cost-conscious/default rows -- codex review
    # discussion_r3658688929.
    if premium_run_ids and row.get("run_id") in premium_run_ids:
        score += 500
        parts.append("model_routing_mode=premium")
    # connector_calls is one of this report's own measured proxy dimensions
    # (connector_load), but had no weight or evidence here at all: a run that
    # performed a large logged connector pull but placed no orders and used no
    # premium model could be omitted from the top five entirely even though
    # connector_load itself was high -- codex review discussion_r3660464565.
    connector_calls = row.get("connector_calls")
    if isinstance(connector_calls, int) and connector_calls > 0:
        score += connector_calls * 10
        parts.append(f"connector_calls={connector_calls}")
    if row.get("recorded_error"):
        score += 60
        parts.append(f"recorded_error={row['recorded_error']}")
    if row.get("source") in (
        "recovery_rerun",
        "failed_start_escalation",
        "manual",
        "parent_recovery_submission",
        "subagent_escalation",
        "repair_artifact",
    ):
        score += 40
        parts.append(f"recovery_source={row['source']}")
    artifact_bytes = row.get("artifact_bytes")
    if isinstance(artifact_bytes, int) and artifact_bytes > 0:
        score += artifact_bytes // 1000  # ~1 point per KB, the lowest-weight signal
        parts.append(f"artifact_bytes={artifact_bytes}")
    return score, "; ".join(parts)


def _rank_usage_drivers(rows: List[Dict[str, Any]], premium_run_ids: Optional[set] = None) -> List[Dict[str, Any]]:
    scored = []
    for row in rows:
        score, evidence = _driver_score(row, premium_run_ids)
        if score > 0:
            scored.append({**row, "usage_driver_score": score, "usage_driver_evidence": evidence})
    scored.sort(key=lambda r: r["usage_driver_score"], reverse=True)
    return scored[:5]


NO_ACTIONABLE_ISSUES_STATEMENT = (
    "No actionable issues found for this trading day: no failed scheduled starts, "
    "no recovery or escalation runs, no recorded run errors, and no high proxy bands."
)


def _actionable_issues(
    scheduled_rows: List[Dict[str, Any]],
    recovery_rows: List[Dict[str, Any]],
    counts: Dict[str, Any],
    bands: Dict[str, Any],
) -> List[str]:
    """Every actionable condition observed today, or an empty list.

    An empty list is what lets render_markdown emit the contractually
    required single clean-day statement: without deriving this, a clean report
    and a generator that simply skipped actionable-issue analysis were
    indistinguishable to a reader -- codex review discussion_r3658688924.
    """
    issues: List[str] = []
    # output_mode.actionable_issue_definition, in order.
    for row in recovery_rows:
        if row.get("source") == "failed_start_escalation":
            issues.append(
                f"failed scheduled start: {row.get('task_id') or row.get('stream')} "
                f"({row.get('stream')}) -- {row.get('triggering_issue')}"
            )
    recovery_executions = [r for r in recovery_rows if r.get("source") != "failed_start_escalation"]
    if recovery_executions:
        issues.append(
            f"{len(recovery_executions)} recovery, manual rerun, or repair artifact(s) present today "
            "-- see recovery_and_escalation_runs."
        )
    for row in scheduled_rows:
        if row.get("recorded_error"):
            issues.append(
                f"recorded run error: {row.get('task_id')} ({row.get('stream')}) -- {row['recorded_error']}"
            )
        missing_required = row.get("missing_required_artifacts")
        if missing_required:
            # required_artifact_missing_rule: a valid_scheduled_evidence run
            # dir missing one of its stream's own documented required-artifact
            # groups (see REQUIRED_ARTIFACT_GROUPS) is an actionable gap, not
            # silently accepted just because SOME files exist -- codex review
            # discussion_r3658790021.
            issues.append(
                f"missing required artifact(s): {row.get('task_id')} ({row.get('stream')}, "
                f"run_id={row.get('run_id')}) -- {', '.join(missing_required)}"
            )
    # actionable_issue_definition's "any required artifact missing for a task
    # that was expected to run". A slot that never ran is the MAXIMAL case of
    # that -- it produced no artifacts at all -- but it carries neither a
    # recorded_error nor a missing_required_artifacts list (both of which
    # presuppose a run that got far enough to write something), so nothing
    # above caught it. On a completely empty artifact tree every one of the
    # 11 active slots was missing and actionable_issues was still [], making
    # the Markdown report state outright that no actionable issues were found
    # -- the single most misleading output this report can produce -- codex
    # review discussion_r3659561357.
    #
    # Aggregated into one line rather than 11, matching how the recovery
    # executions above are summarized, so compact output mode stays compact
    # while still naming every affected slot.
    missing_runs = [
        f"{row.get('task_id')} ({row.get('stream')}, {row.get('et')})"
        for row in scheduled_rows
        if row.get("classification") == "did_not_run" and not row.get("paused")
    ]
    if missing_runs:
        issues.append(
            f"{len(missing_runs)} expected scheduled slot(s) produced no run and no artifacts: "
            f"{', '.join(missing_runs)}."
        )
    for counter in ("error_count", "retry_count"):
        value = counts.get(counter)
        if isinstance(value, int) and value > 0:
            issues.append(f"{counter} is {value} (above zero).")
        elif value in ("not_logged", "not_available"):
            issues.append(f"{counter} is {value} -- cannot be confirmed zero, so it is surfaced rather than assumed clean.")
    for counter in ("connector_error_count_where_logged", "connector_retry_count_where_logged"):
        value = counts.get(counter)
        if isinstance(value, int) and value > 0:
            issues.append(f"{counter} is {value} (connector error or retry recorded).")
    for dimension, info in bands.items():
        if info.get("band") == "high":
            issues.append(f"proxy band {dimension} is high (observed: {info.get('observed')}).")
    return issues


def generate_report(
    trading_day: str = DEFAULT_TRADING_DAY,
    cron_tracking_root: str | Path = CRON_TRACKING_ROOT,
) -> Dict[str, Any]:
    if trading_day != SUPPORTED_TRADING_DAY:
        raise ValueError(
            f"generate_credit_usage_proxy_report is content-gated to {SUPPORTED_TRADING_DAY!r} "
            f"(the day this one-time report contract summarizes), got {trading_day!r}. "
            "A 5-field cron has no year field, so the external registration recurs every "
            "July 27; this refusal is the script-side guard against an accidental re-fire "
            "in a later year regenerating stale output -- codex review discussion_r3658332755."
        )

    cron_root = Path(cron_tracking_root)
    model_usage_path = cron_root / "model_usage" / trading_day.replace("-", "")[:6] / "model_usage.jsonl"
    model_usage_entries = _load_model_usage_entries(model_usage_path)

    scheduled_rows = [_build_chain_row(slot, cron_root, trading_day, model_usage_entries) for slot in CHAIN_SLOTS]
    paused_row = {
        "et": PAUSED_MONITOR_SLOT["et"],
        "task_id": PAUSED_MONITOR_SLOT["task_id"],
        "stream": PAUSED_MONITOR_SLOT["stream"],
        "run_id": NOT_APPLICABLE_DID_NOT_RUN,
        "triggering_issue": "paused (not part of must_cover_active_daily_chain; reported for completeness only)",
        "artifacts_written": "not_available",
        "order_attempts": "not_available",
        "connector_calls": "not_logged",
        "model_usage_rows": "not_available",
        "account_number": None,
        "paused": True,
        "classification": "did_not_run",
    }
    scheduled_rows.append(paused_row)

    recovery_rows, corrupted_attempt_streams = _scan_recovery_runs(cron_root, trading_day, model_usage_entries)
    counted_scheduled_rows = [r for r in scheduled_rows if not r.get("paused")]
    counts = _usage_counts(
        counted_scheduled_rows, recovery_rows, model_usage_available=model_usage_entries is not None
    )
    known_run_ids = {
        r["run_id"]
        for r in scheduled_rows + recovery_rows
        if r["run_id"] not in (NO_RUN_ID, NOT_APPLICABLE_DID_NOT_RUN, "not_available")
    }
    premium_row_count = _premium_row_count(model_usage_entries, known_run_ids, counts["model_usage_jsonl_row_count"])
    bands = _proxy_bands(counts, premium_row_count)

    caveats = [
        "Exact Perplexity credit totals are not exposed to this repo or to the scheduled tasks; every figure above is a measurable proxy or band, never an exact or estimated credit/dollar figure.",
    ]
    if model_usage_entries is None:
        # Test model_usage_path itself, not just the shared model_usage/ root:
        # the root can exist because a DIFFERENT month was logged while this
        # trading day's own month file is simply absent, which the prior
        # is_dir() check misreported as "malformed" -- codex review
        # discussion_r3658511882.
        if not model_usage_path.is_file():
            caveats.append(f"{model_usage_path} not found -- model_usage_jsonl_row_count and model_usage_rows are reported not_available, not 0.")
        else:
            caveats.append(f"{model_usage_path} exists but contains a malformed or non-object line -- treated as fully unavailable rather than partially parsed, since a dropped line could itself be a premium-routed entry.")
    for stream in sorted(corrupted_attempt_streams):
        # A dropped line's own evidence is genuinely lost: if it recorded an
        # EARLIER attempt (not the latest), the single <stream>.json marker
        # can only ever recover the latest one, so this stream's recovery,
        # error, and retry counts may be lower than the persisted
        # attempt_log history actually contains -- codex review
        # discussion_r3661312403.
        caveats.append(
            f"{stream}: attempt_log history for this stream today contains an unparseable/malformed line "
            "-- that attempt's own evidence is lost, so this stream's recovery, error, and retry counts may "
            "understate the persisted attempt history."
        )
    for row in scheduled_rows:
        if row["classification"] == "did_not_run" and not row.get("paused"):
            caveats.append(f"{row['task_id']} ({row['stream']}, {row['et']}): did_not_run -- {row['triggering_issue']}.")
            if row["task_id"] not in MARKER_COVERED_TASK_IDS:
                caveats.append(
                    f"{row['task_id']} ({row['stream']}): no in-repo entrypoint writes an attempt_log marker for "
                    "this stream, so an attempted-and-crashed invocation cannot be distinguished from a genuinely "
                    "missed slot from this repo's own artifacts -- did_not_run is reported as the honest default, "
                    "not a proof of a clean miss."
                )

    # unavailable_data_rule: disclose every required metric this run could
    # not measure, not just the model_usage root -- codex review
    # discussion_r3658263824.
    unmeasured_metric_notes = {
        "notification_count": "no covered run persisted a usage_proxy.notifications_sent value",
        "notion_updates": "no persisted Notion-update log this generator can read",
        "drive_updates": "no persisted Drive-update log this generator can read",
        "connector_pull_count_where_logged": (
            "no covered run persisted a usage_proxy connector count "
            "(finance_calls_attempted/robinhood_calls_attempted)"
        ),
        "connector_retry_count_where_logged": "no per-run connector-retry log this generator can read",
        "connector_error_count_where_logged": "no per-run connector-error log this generator can read",
    }
    for metric, reason in unmeasured_metric_notes.items():
        if counts.get(metric) in ("not_logged", "not_available"):
            caveats.append(f"{metric}: {counts[metric]} -- {reason}.")
    if counts.get("model_usage_jsonl_row_count") == "not_available":
        caveats.append("model_usage_jsonl_row_count: not_available -- at least one covered run's model_usage_rows could not be measured, so the aggregate is not partially summed.")
    if counts.get("placed_order_count") == "not_available":
        caveats.append("placed_order_count: not_available -- at least one covered run's order_attempts could not be measured, so the aggregate is not partially summed.")
    if bands["connector_load"]["band"] == "not_available":
        caveats.append(
            "connector_load: not_available -- no covered run persisted a usage_proxy connector count "
            "(finance_calls_attempted/robinhood_calls_attempted), so there is no measured pull count to band."
        )
    # unavailable_data_rule applies to the byte aggregate and the band it
    # feeds too: _scan_run_dir deliberately propagates not_available when any
    # scanned file cannot be stat'ed, but neither value named a reason, so the
    # report emitted an unexplained not_available -- codex review
    # discussion_r3660192649.
    if counts.get("artifact_bytes_approx") == "not_available":
        caveats.append(
            "artifact_bytes_approx: not_available -- at least one scanned artifact could not be stat'ed "
            "(it vanished mid-scan, or an I/O/permission error), so the byte total is not partially summed."
        )
    if bands["artifact_load"]["band"] == "not_available":
        caveats.append(
            "artifact_load: not_available -- the band needs both a file count and a byte total, and at "
            "least one scanned artifact could not be stat'ed, so no threshold can be applied without "
            "understating the real volume."
        )

    for row in scheduled_rows + recovery_rows:
        assert row["classification"] in CLASSIFICATION_VALUES, f"unknown classification {row['classification']!r} for {row.get('run_id')!r}"
    # Every recovery row must file under exactly one declared type, or
    # recovery_count_by_artifact_type silently under-sums against
    # recovery_and_escalation_count and a reader cannot tell which rows were
    # dropped -- codex review discussion_r3659140599.
    for row in recovery_rows:
        assert row.get("recovery_artifact_type") in RECOVERY_ARTIFACT_TYPES, (
            f"recovery row {row.get('run_id')!r} carries unknown "
            f"recovery_artifact_type {row.get('recovery_artifact_type')!r}"
        )
    assert sum(counts["recovery_count_by_artifact_type"].values()) == counts["recovery_and_escalation_count"], (
        "recovery_count_by_artifact_type must sum to recovery_and_escalation_count"
    )

    report = {
        "trading_day": trading_day,
        "task_id": TASK_ID,
        "scheduled_runs": scheduled_rows,
        "recovery_and_escalation_runs": recovery_rows,
        "usage_counts": counts,
        "proxy_bands": bands,
        # Cross-reference recovery rows are excluded from RANKING for the same
        # reason they are excluded from the aggregates: they describe an
        # artifact a scheduled row already represents. Ranking both let one
        # underlying run occupy two of the five driver slots under the same
        # run_id -- a premium in-place recovery scored once as the substantive
        # scheduled row and again as its cross-reference -- displacing a
        # distinct real driver -- codex review discussion_r3660192640.
        "biggest_usage_drivers": _rank_usage_drivers(
            scheduled_rows
            + [r for r in recovery_rows if r.get("source") not in CROSS_REFERENCE_RECOVERY_SOURCES],
            _premium_run_ids(model_usage_entries),
        ),
        "caveats": caveats,
        "actionable_issues": _actionable_issues(scheduled_rows, recovery_rows, counts, bands),
    }
    assert all(section in report for section in REQUIRED_SECTIONS), "report is missing a required_sections entry"
    return report


def _md_cell(value: Any) -> str:
    """``value`` rendered as a single safe GitHub-Markdown table cell.

    Per-run values include free-form exception text and recovery reasons read
    verbatim from persisted artifacts, so an unescaped ``|`` splits the row
    into extra columns and an embedded newline ends the row early and starts a
    bogus one -- either way the rendered evidence and its recovery-context
    label silently misalign against their headers -- codex review
    discussion_r3659140614. Backslashes are escaped first so the escape added
    for a pipe cannot itself be neutralized by a literal trailing backslash in
    the source text.
    """
    text = str(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    # \r\n first, so a CRLF pair collapses to one separator rather than two.
    for line_break in ("\r\n", "\n", "\r"):
        text = text.replace(line_break, "<br>")
    return text


def _render_row(values: List[Any]) -> str:
    return "| " + " | ".join(_md_cell(v) for v in values) + " |"


# Identity columns prepended to both rendered per-run tables. task_id and et
# are on every JSON row but are NOT in PER_RUN_REQUIRED_FIELDS, so rendering
# from that tuple alone produced a Markdown report that never showed the eight
# required task IDs and could not tell 7be82b97's three slots apart -- even
# though must_cover_active_daily_chain is stated per slot and the JSON had the
# data all along -- codex review discussion_r3659298029.
_RENDERED_IDENTITY_FIELDS = ("task_id", "et")
_RENDERED_PER_RUN_FIELDS = _RENDERED_IDENTITY_FIELDS + PER_RUN_REQUIRED_FIELDS


def _render_per_run_row(row: Dict[str, Any]) -> str:
    return _render_row([row.get(field, "") for field in _RENDERED_PER_RUN_FIELDS])


def render_markdown(report: Dict[str, Any]) -> str:
    header = "| " + " | ".join(_RENDERED_PER_RUN_FIELDS) + " |"
    separator = "|" + "|".join(["---"] * len(_RENDERED_PER_RUN_FIELDS)) + "|"

    lines = [f"# Credit-usage proxy report -- {report['trading_day']}", ""]
    lines.append("## Scheduled runs")
    lines.append("")
    lines.append(header)
    lines.append(separator)
    for row in report["scheduled_runs"]:
        lines.append(_render_per_run_row(row))
    lines.append("")
    lines.append("## Recovery and escalation runs")
    lines.append("")
    if report["recovery_and_escalation_runs"]:
        lines.append(header + " recovery_context_label |")
        lines.append(separator + "---|")
        for row in report["recovery_and_escalation_runs"]:
            # Built by appending the label to the row's own value list rather
            # than string-slicing an already-rendered row: the old
            # ``[:-2]``-then-concatenate trick assumed the rendered row always
            # ended in exactly " |", which stops holding once cells are
            # escaped -- codex review discussion_r3659140614.
            lines.append(
                _render_row(
                    [row.get(field, "") for field in _RENDERED_PER_RUN_FIELDS]
                    + [row.get("recovery_context_label", "")]
                )
            )
    else:
        lines.append("None recorded.")
    lines.append("")
    lines.append("## Usage counts")
    lines.append("")
    for key, value in report["usage_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    lines.append("## Proxy bands")
    lines.append("")
    # Every field that can determine the band is rendered, not just
    # `observed`: artifact_load can go high on bytes or retry_count while
    # `observed` (the file count) stays small, which printed a misleading
    # "high (observed: 10)" with the deciding value invisible. The threshold
    # each dimension was evaluated against is rendered too, so the band is
    # reproducible from the Markdown alone -- codex review
    # discussion_r3658801025.
    for dimension, info in report["proxy_bands"].items():
        detail = ", ".join(f"{key}: {value}" for key, value in info.items() if key != "band")
        threshold = BAND_THRESHOLDS.get(dimension)
        line = f"- `{dimension}`: **{info['band']}**"
        if detail:
            line += f" ({detail})"
        if threshold:
            line += f" -- thresholds: {threshold}"
        lines.append(line)
    lines.append("")
    lines.append("## Biggest usage drivers")
    lines.append("")
    if report["biggest_usage_drivers"]:
        for row in report["biggest_usage_drivers"]:
            label = row.get("task_id") or row["stream"]
            # usage_driver_evidence is built from persisted free text
            # (recorded_error, recovery source labels) that can carry a raw
            # newline; interpolating it directly wrote the continuation as a
            # new, unindented Markdown line -- able to render as a bogus
            # heading or extra list item and break the report -- codex review
            # discussion_r3660464570. _md_cell already neutralizes exactly
            # this for table cells.
            lines.append(f"- {label}: {_md_cell(row.get('usage_driver_evidence', ''))}")
    else:
        lines.append("None recorded.")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    for caveat in report["caveats"]:
        lines.append(f"- {caveat}")
    lines.append("")
    lines.append("## Actionable issues")
    lines.append("")
    actionable = report.get("actionable_issues") or []
    if actionable:
        for issue in actionable:
            # Each issue string can embed a persisted blocking_reason/
            # exception message (via triggering_issue/recorded_error) that
            # carries a raw newline -- same rendering hazard as
            # usage_driver_evidence above.
            lines.append(f"- {_md_cell(issue)}")
    else:
        lines.append(NO_ACTIONABLE_ISSUES_STATEMENT)
    lines.append("")
    return "\n".join(lines)


# Deployment override for the artifact tree (Codex review: "Make the scheduled
# report paths configurable"), so a deployment with a different home or artifact
# root can redirect both the tree this reads and the outputs it writes without
# editing the registered scheduler command.
#
# This module defined the variable name and the resolution rule first; both now
# live in dca_paths, which every stream module shares. Re-exported here because
# the CLI --help text and the tests reference it by this name.
CRON_TRACKING_ROOT_ENV = _CRON_TRACKING_ROOT_ENV


def _default_cron_tracking_root() -> str:
    """Artifact-tree root: the env override when set and non-empty, else bundled.

    Unlike the module-level ``CRON_TRACKING_ROOT`` (resolved at import), this
    reads the environment on every call, so a caller that sets the variable
    late -- notably the tests -- still gets the redirected tree.
    """
    return cron_tracking_root()


def default_report_paths(trading_day: str, cron_tracking_root: str | Path | None = None) -> Dict[str, Path]:
    """Canonical JSON/Markdown output paths for ``trading_day`` under the root.

    Derived from the SAME root the report reads, so redirecting the tree
    redirects the outputs with it. The registered scheduler command passes
    these explicitly via shell expansion of the same variable; this keeps a
    bare invocation (env var set, no flags) writing to the matching place.
    """
    root = Path(cron_tracking_root) if cron_tracking_root else Path(_default_cron_tracking_root())
    day = _yyyymmdd(trading_day)
    out_dir = root / "credit_usage_proxy_report" / day
    return {
        "json": out_dir / f"credit_usage_proxy_report_{day}.json",
        "md": out_dir / f"credit_usage_proxy_report_{day}.md",
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.generate_credit_usage_proxy_report",
        description="Generate the one-time credit-usage proxy report from persisted same-day artifacts.",
    )
    parser.add_argument("--trading-day", default=DEFAULT_TRADING_DAY, help="ET trading day, YYYY-MM-DD (default: %(default)s).")
    parser.add_argument(
        "--cron-tracking-root",
        default=None,
        help=(
            "Root of the cron_tracking artifact tree "
            f"(default: ${CRON_TRACKING_ROOT_ENV} when set, else {CRON_TRACKING_ROOT})."
        ),
    )
    parser.add_argument("--json-out", default=None, help="Write the report as JSON to this path.")
    parser.add_argument("--md-out", default=None, help="Write the report as Markdown to this path.")
    parser.add_argument(
        "--write-defaults",
        action="store_true",
        help=(
            "Write JSON/Markdown to default_report_paths(trading_day, cron_tracking_root) "
            "instead of requiring --json-out/--md-out explicitly. Lets the scheduler "
            "invoke this script without hardcoding any root or output path in the shell "
            "command -- both are derived entirely from dca_paths.cron_tracking_root(), "
            "the same function every other in-repo producer/consumer resolves its root "
            "through, so this can never drift from them the way a shell ${VAR:-literal} "
            "fallback could (Codex review: \"Keep the report command on the shared "
            "artifact root\"). Ignored if --json-out/--md-out are also given explicitly."
        ),
    )
    parser.add_argument(
        "--allow-execution-date-mismatch",
        action="store_true",
        help=(
            "Permit running when today's ET date is not the supported trading day "
            f"({SUPPORTED_TRADING_DAY}). Required for a deliberate later replay; the scheduler "
            "must never pass it, since the guard exists to stop an unretired annual cron re-fire."
        ),
    )
    return parser


def _execution_date_refusal(trading_day: str, today_et: str) -> Optional[str]:
    """Why this invocation must refuse, or None when it may proceed.

    ``generate_report``'s own gate compares its ``trading_day`` ARGUMENT, and
    the registered scheduler command hardcodes ``--trading-day 2026-07-27``,
    so that gate can never notice a later execution: ``30 20 27 7 *`` has no
    year field and fires every July 27, and in 2027 the hardcoded argument
    would sail through, re-read the 2026 artifact tree, and overwrite the 2026
    outputs -- codex review discussion_r3659140568. Comparing the ACTUAL
    execution date closes that, because it is the one input the hardcoded
    command cannot pin.

    Scoped to the calendar YEAR rather than the exact date, so a legitimate
    same-week manual re-run to inspect the report still works while the
    annual-recurrence hole stays closed.
    """
    if today_et[:4] == trading_day[:4]:
        return None
    return (
        f"refusing to run: today's America/New_York date is {today_et}, but this one-time report "
        f"covers {trading_day}. A 5-field cron ('30 20 27 7 *') has no year field, so an unretired "
        "scheduler entry re-fires every July 27; regenerating now would re-read that trading day's "
        "artifact tree and overwrite its published outputs. Retire the scheduler entry (or use a "
        "scheduler-native one-shot trigger); pass --allow-execution-date-mismatch only for a "
        "deliberate replay -- codex review discussion_r3659140568."
    )


def _main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    cron_tracking_root = args.cron_tracking_root or _default_cron_tracking_root()
    if not args.allow_execution_date_mismatch:
        refusal = _execution_date_refusal(args.trading_day, datetime.now(_EASTERN).strftime("%Y-%m-%d"))
        if refusal:
            print(f"generate_credit_usage_proxy_report: {refusal}", file=sys.stderr)
            return 2
    report = generate_report(trading_day=args.trading_day, cron_tracking_root=cron_tracking_root)
    json_out_path = args.json_out
    md_out_path = args.md_out
    if args.write_defaults and not json_out_path and not md_out_path:
        defaults = default_report_paths(args.trading_day, cron_tracking_root)
        json_out_path = str(defaults["json"])
        md_out_path = str(defaults["md"])
    if json_out_path:
        json_out = Path(json_out_path)
        # On a fresh artifact tree (e.g. the first scheduled run) nothing has
        # pre-created credit_usage_proxy_report/20260727/, and Path.write_text
        # does not create parent directories -- codex review
        # discussion_r3658511841 (P1).
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"generate_credit_usage_proxy_report: wrote {json_out_path}")
    if md_out_path:
        md_out = Path(md_out_path)
        md_out.parent.mkdir(parents=True, exist_ok=True)
        md_out.write_text(render_markdown(report), encoding="utf-8")
        print(f"generate_credit_usage_proxy_report: wrote {md_out_path}")
    if not json_out_path and not md_out_path:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
