"""Morning graded-DCA executor (watchlist scan + live execution streams).

This module adds a *morning* stream on top of the existing near-close strategy
library (:mod:`near_close_dca`). It is deliberately isolated from the afternoon
scheduled live task (cron ``10ce362c``, which supersedes the deleted
``ce16ba6b``):

    - It uses the SAME canonical $2-base 0x -> 5x graded ladder as the afternoon
      and midday live streams (bound to ``StrategyConfig.sizing_tiers``); the
      former coarser 0x -> 3x morning ladder is historical/superseded.
    - It keeps its OWN budget ledger namespace (:class:`MorningLedger`) so
      morning activity can never consume the afternoon 3:30 budget ledger.
    - It writes artifacts under its OWN run-state roots and NEVER touches
      ``.../ce16ba6b/latest_scheduled_run.json`` or the afternoon
      ``scheduled_runs/`` folders.
    - Its run_classification always carries a distinct ``stream='morning'``
      marker and ``valid_afternoon_evidence=false`` so downstream afternoon
      reporting (which only globs ``.../ce16ba6b/scheduled_runs/**``) never
      mistakes it for afternoon scheduled evidence.

The morning stream has two modes (see :data:`MORNING_PROFILES`):

    - ``watchlist`` (9:45 AM ET, ``bb4f6ef1``): a READ-ONLY early-warning
      watchlist scan. Analytics only, ``trades=false``. It may surface early
      candidates but every dollar figure is HYPOTHETICAL / watchlist-only. It
      writes under ``.../cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/``
      with ``run_type='morning_dca_watchlist_scan'`` and
      ``state_schema_version='morning-watchlist-v1'``.
    - ``live`` (10:30 AM ET, ``42173154``): the approved live Robinhood morning
      execution, moved from 9:45 to 10:30 so it evaluates a true 60-minute
      trend window (9:30-10:30). ``trades=true``. It writes under
      ``.../cron_tracking/morning_dca/YYYYMMDD_1030ET/`` with
      ``run_type='morning_dca_live'``, ``scheduled_for_et='10:30'``,
      ``run_id='morning_live_YYYYMMDD_1030ET'`` and
      ``state_schema_version='morning-live-v2-preauthorized'`` (the
      pre-authorized direct-placement contract with raw-data persistence and
      automatic failure recovery).

Confidence rule (live mode): when a ticker's price/drop qualifies for a buy but
the intraday trend over the 60-minute window cannot be validated, the candidate
is NOT auto-bought. It is classified as a ``manual_review_candidate``. The
loosened "buy anyway when trend is unavailable" rule is deliberately NOT
implemented.

Robinhood remains the only execution broker (the account number comes from
$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, never from git); Alpaca stays
read-only. Morning fills are never afternoon scheduled evidence; they count only
as prior-allocation context for the afternoon same-day top-up guard.
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import math
import os
import re
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Sequence, Tuple, cast

from dca_alerts import (
    GuardedRunConfig,
    artifact_freshness_probe,
    attempt_log_root_sibling_to,
    guarded_run,
    write_failed_start_marker,
)
from dca_common import build_top_up_outcome
from dca_config import (
    ACCOUNT_NUMBER_SHAPE,
    READ_ONLY_ACCOUNT,
    InvalidAccountNumberError,
    MissingConfigEnvError,
    get_config_dir,
    load_approved_dates,
    load_per_ticker_sizing,
    load_tickers,
    resolve_broker_account,
)
from dca_cumulative import CumulativeCreditLedger, apply_cumulative_credit
from dca_fileio import atomic_open_w, atomic_write_text, order_csv_covers
from dca_intraday_archive import (
    archive_observations_safely,
    observations_from_signal_rows,
    write_run_snapshot,
)
from dca_paths import cron_tracking_root, rebase_bundled_roots_in, stream_root
from near_close_dca import (
    ACTIVE_TICKERS,
    BASE_NOT_BELOW_CLOSE,
    EASTERN,
    EXCLUDED_TICKERS,
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
    TOP_UP_INCREMENTAL,
    TOP_UP_MISSING_PRIOR_BASE,
    TOP_UP_MISSING_PRIOR_PRICE,
    TOP_UP_NO_INCREMENTAL,
    TOP_UP_NO_PRIOR_RUN,
    TOP_UP_PRICE_NOT_BELOW,
    Action,
    Bar,
    BrokerAdapter,
    OrderConfirmationRequiredError,
    PriorSameDayAllocation,
    StrategyConfig,
    _ticker_sizing,
    account_pinning_failure,
    completed_bar_anchor,
    cumulative_price_step_amount_usd,
    graded_buy_multiplier,
    iso_week_key,
    latest_bar_at_or_before,
    load_prior_same_day_allocations_multi,
    merge_manual_top_up_context,
    minimum_drop_for_ticker,
    nyse_holidays,
    quote_spread_pct,
    resolve_top_up_desired_amount_usd,
    select_first_allocation_base_usd,
    warn_if_window_expiring,
)
from scripts.file_lock import cross_process_lock
from scripts.model_usage_log import log_model_usage
from scripts.run_classification import (
    BLOCKED_BY_SPREAD_GATE,
    COMPLETION_REASON_DATE_GATE_BLOCKED,
    ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT,
    ERROR_CLASS_DEGRADED_MARKET_DATA,
    ERROR_CLASS_PARTIAL_PLACEMENT_FAILURE,
    ERROR_CLASS_POST_SUBMISSION_FAILURE,
    ERROR_CLASS_STARTUP_FAILURE,
    ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
    ORDER_SUBMIT_STATUS_DRY_RUN,
    ORDER_SUBMIT_STATUS_ERROR,
    ORDER_SUBMIT_STATUS_NO_ORDERS,
    ORDER_SUBMIT_STATUS_SUBMITTED,
    RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
    RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION,
    RUN_CLASSIFICATION_STARTUP_FAILED,
    SPREAD_STATUS_NOT_EVALUATED,
    classify_no_order_run,
    deny_scheduled_evidence,
    load_same_day_top_up_context,
    save_raw_input,
    spread_gate_fields,
    stamp_failure_classification,
)
from scripts.same_day_pointer import (
    STATUS_PENDING as POINTER_STATUS_PENDING,
)
from scripts.same_day_pointer import (
    STATUS_VALID as POINTER_STATUS_VALID,
)
from scripts.same_day_pointer import (
    build_pointer_metadata,
    derive_valid_scheduled_evidence,
    validate_pointer,
)
from scripts.scheduled_artifacts import (
    ORDER_ATTEMPT_STATUSES,
    SIGNAL_CSV_NAMES,
    build_run_summary,
    submitted_order_rows,
    write_submitted_orders_csv,
)
from scripts.scheduled_artifacts import (
    csv_qualifying_notional_total as _csv_qualifying_notional_total,
)

_log = logging.getLogger(__name__)


# --- Morning stream identity / classification constants ---------------------
#
# The morning stream is INDEPENDENT of the afternoon live task; it never
# supersedes it and is never valid afternoon evidence. These constants
# intentionally differ from the afternoon 15:30 classification so the streams can
# never be confused (see docs/morning_dca.md).
MORNING_STREAM = "morning"
# The afternoon live task id, recorded on morning artifacts for cross-reference.
AFTERNOON_LIVE_CRON_ID = "10ce362c"

# Stable signal-CSV column order, mirroring MorningSignal.to_row(). Used as the
# header for a run that evaluated no signal at all, so the file still carries
# columns instead of being written empty.
SIGNAL_CSV_FIELDS = (
    "ticker",
    "source",
    "action",
    "reason",
    "current_price",
    "previous_close",
    "drop_pct",
    "multiplier",
    "previous_low",
    "selected_base_amount_usd",
    "base_selection_reason",
    "hypothetical_amount_usd",
    "warnings",
    "execution_status",
    "trend_validated",
    "auto_buy_signal",
    "spread_ok",
    "spread_pct",
    "spread_status",
    # Price provenance and intraday OHLCV, appended 2026-08-04. These are the
    # columns that make a signal row usable as an archived time-series
    # observation rather than only as a one-run decision record: which feed the
    # price came from, when that price was as of, and the decision bar's own
    # OHLCV/volume. Appended at the END so a reader of an older signals.csv is
    # unaffected (see dca_intraday_archive).
    "price_source",
    "price_as_of",
    "previous_close_as_of",
    "quote_price",
    "quote_time",
    "bar_timestamp",
    "bar_open",
    "bar_high",
    "bar_low",
    "bar_close",
    "bar_volume",
    "session_open",
    "session_high",
    "session_low",
    "session_volume",
    "bar_count",
    "interval_minutes",
)

# Watchlist (9:45 AM ET) scan identity — analytics-only, no trading.
MORNING_WATCHLIST_MODE = "watchlist"
# bb4f6ef1 is the canonical 9:45 AM slot. The former repo-internal alias
# "morning-dca-0945" was retired from the active schedule on 2026-07-27.
MORNING_WATCHLIST_CRON_ID = "bb4f6ef1"
MORNING_WATCHLIST_RUN_TYPE = "morning_dca_watchlist_scan"
MORNING_WATCHLIST_SCHEDULED_FOR_ET = "09:45"
MORNING_WATCHLIST_WINDOW_START_ET = "09:40"
MORNING_WATCHLIST_WINDOW_END_ET = "09:50"
MORNING_WATCHLIST_SCHEMA_VERSION = "morning-watchlist-v1"

# Pre-flight validation (9:45 AM ET) identity — read-only pre-screen for the
# 10:30 live executor. Writes eligible_to_execute=true/false per ticker. This is
# a COARSE pre-screen (is a ticker worth evaluating at 10:30?), NOT a final
# approval: the 10:30 live run always recomputes signals, budget, spread, trend,
# and top-up from scratch and may still decline to place an order.
#
# As of Mon Jul 13 2026 the artifact is machine-complete
# (schema morning-preflight-v2-machine-complete, schemas/morning_preflight.schema.json):
# it always writes the top-level overall_ok flag plus eligible_count,
# ineligible_count, unresolved_tickers, blocking_errors, and a
# per_ticker_eligibility list with every active-universe symbol exactly once, so
# the 10:30 live task can gate directly off machine fields. Unresolved symbols
# (e.g. AAAU, SPCX) are emitted as rows with eligible_to_execute=false and
# ineligible_reason='unresolved_ticker'; they never force overall_ok=false, which
# flips false only for global/account/buying-power/auth/schema-write failures.
MORNING_PREFLIGHT_MODE = "preflight"
MORNING_PREFLIGHT_CRON_ID = "bb4f6ef1"
MORNING_PREFLIGHT_RUN_TYPE = "morning_dca_preflight"
MORNING_PREFLIGHT_SCHEDULED_FOR_ET = "09:45"
MORNING_PREFLIGHT_WINDOW_START_ET = "09:40"
MORNING_PREFLIGHT_WINDOW_END_ET = "09:50"
MORNING_PREFLIGHT_SCHEMA_VERSION = "morning-preflight-v2-machine-complete"
MORNING_PREFLIGHT_TREND_STATUS = "not_evaluated_preflight"

# Live (10:30 AM ET) execution identity — Robinhood-only live buys.
MORNING_LIVE_MODE = "live"
MORNING_LIVE_CRON_ID = "42173154"
MORNING_LIVE_RUN_TYPE = "morning_dca_live"
MORNING_LIVE_SCHEDULED_FOR_ET = "10:30"
MORNING_LIVE_WINDOW_START_ET = "10:25"
MORNING_LIVE_WINDOW_END_ET = "10:35"
MORNING_LIVE_SCHEMA_VERSION = "morning-live-v2-preauthorized"

# Backwards-compatible aliases (the watchlist scan is the successor of the old
# 9:45 dry-run stream, so unqualified references resolve to it).
MORNING_CRON_ID = MORNING_WATCHLIST_CRON_ID
MORNING_RUN_TYPE = MORNING_WATCHLIST_RUN_TYPE
MORNING_SCHEDULED_FOR_ET = MORNING_WATCHLIST_SCHEDULED_FOR_ET
MORNING_WINDOW_START_ET = MORNING_WATCHLIST_WINDOW_START_ET
MORNING_WINDOW_END_ET = MORNING_WATCHLIST_WINDOW_END_ET

# Artifact roots. Both are siblings of, and never nested under, the afternoon
# ce16ba6b root. The watchlist scan and live execution use distinct roots so a
# read-only scan can never be confused with a live-execution artifact.
DEFAULT_MORNING_WATCHLIST_STATE_ROOT = stream_root("morning_dca_watchlist")
DEFAULT_MORNING_PREFLIGHT_STATE_ROOT = stream_root("morning_dca_preflight")
DEFAULT_MORNING_LIVE_STATE_ROOT = stream_root("morning_dca")
# Legacy alias: the default root historically pointed at the morning stream.
DEFAULT_MORNING_STATE_ROOT = DEFAULT_MORNING_WATCHLIST_STATE_ROOT
# The afternoon live task artifact root. The afternoon scheduled live task
# uses cron id 10ce362c (supersedes ce16ba6b), but the artifact root remains
# under the historical ce16ba6b directory for continuity. Scanned by the
# morning top-up guard so manual/recovery fills recorded before 10:30 AM are
# detected.
DEFAULT_AFTERNOON_TASK_ROOT = stream_root("ce16ba6b")
# The market-open live task (2ba19526, 9:35 AM ET) artifact root. Scanned by
# the morning (10:30) and midday (12:30) top-up guards so a 9:35 fill is
# recognized as same-day prior context instead of being re-bought as a first
# allocation (Codex review: "Feed market-open fills into later top-up
# guards").
DEFAULT_MARKET_OPEN_TASK_ROOT = stream_root("market_open_dca")
# The intraday live task (7be82b97, 11:30/13:30/14:30 ET slots) artifact
# root. Scanned by the midday (12:30) top-up guard -- an 11:30 fill must
# offset the 12:30 desired amount the same way a morning fill does (Codex
# review: "Include the 11:30 intraday run in midday context"). The
# afternoon (3:20) guard already resolves an equivalent sibling root itself
# via run_dca.DEFAULT_INTRADAY_LIVE_STATE_ROOT; this constant is the
# absolute-path form the morning/midday executors' own prior_allocation_roots
# lists use, distinct in shape (not just basename) from that bare sibling
# name.
DEFAULT_INTRADAY_TASK_ROOT = stream_root("intraday_dca")

# Jul 20 2026 pre-flight path fix (see live_task.preflight_artifact_resolution
# in config/morning_dca.json). The 10:30 live run must never read the same-day
# 9:45 pre-flight artifact from either of these roots -- both were implicated
# in the Jul 20 2026 escalation (a stray cron-id path and the live task's own
# output directory). _apply_preflight_gate() fails closed rather than reading
# from a preflight_run_state_root that resolves under one of these.
DEFAULT_PREFLIGHT_FORBIDDEN_ROOTS: Tuple[str, ...] = (
    stream_root("42173154"),
    DEFAULT_MORNING_LIVE_STATE_ROOT,
)

# Jul 20 2026 budget ledger location fix (see budget_ledger.bootstrap in
# config/morning_dca.json). Default filename for the persisted morning budget
# ledger snapshot written under the live run's own output directory.
DEFAULT_BUDGET_LEDGER_FILENAME = "budget_ledger.json"


# Per-mode identity/profile. Selected by MorningDCAExecutor(mode=...).
MORNING_PROFILES: Dict[str, Dict[str, Any]] = {
    MORNING_WATCHLIST_MODE: {
        "mode": MORNING_WATCHLIST_MODE,
        "artifact_mode": "watchlist",
        "trades": False,
        "cron_id": MORNING_WATCHLIST_CRON_ID,
        "run_type": MORNING_WATCHLIST_RUN_TYPE,
        "scheduled_for_et": MORNING_WATCHLIST_SCHEDULED_FOR_ET,
        "window_start_et": MORNING_WATCHLIST_WINDOW_START_ET,
        "window_end_et": MORNING_WATCHLIST_WINDOW_END_ET,
        "state_schema_version": MORNING_WATCHLIST_SCHEMA_VERSION,
        "state_root": DEFAULT_MORNING_WATCHLIST_STATE_ROOT,
        "state_root_stream": "morning_dca_watchlist",
        "folder_suffix": "0945ET",
        "scheduled_run_id_prefix": "morning",
        "manual_run_id_prefix": "manual_morning",
        "decision_time_et": time(9, 45),
        "validate_trend": False,
    },
    MORNING_PREFLIGHT_MODE: {
        "mode": MORNING_PREFLIGHT_MODE,
        "artifact_mode": "preflight",
        "trades": False,
        "cron_id": MORNING_PREFLIGHT_CRON_ID,
        "run_type": MORNING_PREFLIGHT_RUN_TYPE,
        "scheduled_for_et": MORNING_PREFLIGHT_SCHEDULED_FOR_ET,
        "window_start_et": MORNING_PREFLIGHT_WINDOW_START_ET,
        "window_end_et": MORNING_PREFLIGHT_WINDOW_END_ET,
        "state_schema_version": MORNING_PREFLIGHT_SCHEMA_VERSION,
        "state_root": DEFAULT_MORNING_PREFLIGHT_STATE_ROOT,
        "state_root_stream": "morning_dca_preflight",
        "folder_suffix": "0945ET",
        "scheduled_run_id_prefix": "morning_preflight",
        "manual_run_id_prefix": "manual_morning_preflight",
        "decision_time_et": time(9, 45),
        "validate_trend": False,
    },
    MORNING_LIVE_MODE: {
        "mode": MORNING_LIVE_MODE,
        "artifact_mode": "live",
        "trades": True,
        "cron_id": MORNING_LIVE_CRON_ID,
        "run_type": MORNING_LIVE_RUN_TYPE,
        "scheduled_for_et": MORNING_LIVE_SCHEDULED_FOR_ET,
        "window_start_et": MORNING_LIVE_WINDOW_START_ET,
        "window_end_et": MORNING_LIVE_WINDOW_END_ET,
        "state_schema_version": MORNING_LIVE_SCHEMA_VERSION,
        "state_root": DEFAULT_MORNING_LIVE_STATE_ROOT,
        "state_root_stream": "morning_dca",
        "folder_suffix": "1030ET",
        "scheduled_run_id_prefix": "morning_live",
        "manual_run_id_prefix": "manual_morning_live",
        "decision_time_et": time(10, 30),
        "validate_trend": True,
    },
}

STATE_SCHEMA_VERSION = MORNING_WATCHLIST_SCHEMA_VERSION

# Morning sizing ladder. As of the Fri Jul 10 2026 live-automation update every
# live stream (market open 9:35, morning 10:30, intraday 11:30/1:30/2:30, midday 12:30,
# final 3:30) shares the SAME
# canonical $2-base 0x -> 5x graded ladder defined on
# ``near_close_dca.StrategyConfig.sizing_tiers``. The former coarser 0x -> 3x
# morning ladder (0.1-2/2-5/5-8/8+ -> 1x/1.5x/2x/3x) is HISTORICAL/SUPERSEDED and
# is no longer used by the live morning stream. Binding to the afternoon tuple
# keeps all live streams in lockstep, so a single ladder edit updates them all.
#     drop < 0.1%             -> 0x  (no buy)
#     0.1% <= drop < 1.0%     -> 1x
#     1.0% <= drop < 2.0%     -> 1.5x
#     2.0% <= drop < 3.0%     -> 2x
#     3.0% <= drop < 4.0%     -> 2.5x
#     4.0% <= drop < 5.0%     -> 3x
#     5.0% <= drop < 6.0%     -> 3.5x
#     6.0% <= drop < 7.0%     -> 4x
#     7.0% <= drop < 8.0%     -> 4.5x
#     drop >= 8.0%            -> 5x  (no upper cutoff)
MORNING_SIZING_TIERS: Tuple[Tuple[float, float], ...] = StrategyConfig().sizing_tiers

MORNING_MINIMUM_DROP = 0.001

# Every order status that represents a REAL order this stream placed at the
# broker -- ``submitted``, ``filled``, ``pending``, ``unconfirmed``, and
# whatever else ORDER_ATTEMPT_STATUSES grows -- minus
# ``not_submitted_confirmation_required``, which that constant's own docstring
# documents as a deliberate non-order (a confirmation-required response, not a
# placement).
#
# Hoisted to module scope from _existing_placed_run_state() so the helpers that
# rebuild a slot's order record share ONE definition (Codex review, PR #593:
# "Preserve pending rows through the malformed-state fallback"). Those helpers
# -- _submitted_keys() and _carried_forward_order_rows() -- still recognised
# only ``submitted``/``filled`` after _existing_placed_run_state() was widened
# to admit ``pending``/``unconfirmed``, so a blocked retry whose existing
# run_state was structurally malformed took _blocked_live_result()'s graft
# fallback and rewrote a real in-flight order as zero submitted orders with a
# header-only submitted_orders.csv -- erasing a live placement from cap
# accounting and from the duplicate-allocation evidence later runs read.
PLACED_ORDER_STATUSES: FrozenSet[str] = frozenset(
    ORDER_ATTEMPT_STATUSES - {"not_submitted_confirmation_required"}
)

# Timeout for the scheduled-live slot lock (MorningDCAExecutor._run_impl)
# held for the ENTIRE scheduled-live attempt -- raw-dir setup through
# evaluate(), broker order placement, and artifact/run-state publication --
# not just the brief stale-cleanup critical section it used to guard alone
# (Codex review: "Hold the slot lock through raw recording"). That full
# lifecycle does real network I/O (market-data/broker connector calls, order
# placement) and can plausibly run far longer than a single JSON write, so
# file_lock.DEFAULT_TIMEOUT_SECONDS (30s, sized for exactly that -- a quick
# read-modify-write) would make a live run spuriously fail-closed on lock
# contention it would have cleared with more patience. Sized generously above
# any realistic single-attempt runtime while still bounding how long a second
# overlapping scheduled invocation waits before giving up and recording a
# blocked result of its own.
MORNING_SLOT_LOCK_TIMEOUT_SECONDS = 240.0

# Directory name, under the shared artifact root, holding every scheduled-live
# slot lock (MorningDCAExecutor._slot_lock_path).
#
# Codex review, PR #593: "Place the slot lock outside the replaceable run
# directory". The lock started life inside ``raw/``, then moved up to the dated
# run directory -- but flock() (and the portable O_EXCL fallback) locks an
# INODE, not a name, so ANY directory that some other code path can rename away
# and recreate leaves a holder locked on the old inode while an overlapping
# invocation opens a brand-new inode at the same pathname and acquires it
# uncontended. The dated run directory is this stream's artifact PUBLICATION
# directory, so it will always be something that code writes into and could in
# principle rotate; the recursion only terminates at a directory no artifact
# code manages at all.
#
# This is that directory. Nothing cleans it, nothing publishes into it, nothing
# enumerates it as an artifact set, and nothing rotates it. The single operation
# any code in this repository performs on the tree is the
# ``mkdir(parents=True, exist_ok=True)`` that creates it before the first
# acquisition.
#
# WHERE it sits is decided per executor, not globally: it is the SIBLING of
# that executor's ``state_root`` (``MorningDCAExecutor.slot_lock_root``, set
# once in ``__init__``), so the lock is anchored to the very artifact tree it
# protects and the two can never be resolved from different sources (Codex
# review, PR #593: "Anchor the slot lock to the configured artifact root" --
# it used to be ``stream_root(SLOT_LOCK_ROOT_DIRNAME)``, re-read from the
# environment at call time even when ``state_root`` came from config). In the
# default layout ``state_root`` is itself ``stream_root(<stream>)``, so its
# parent is ``cron_tracking_root()`` and this lands on the same
# ``<cron root>/slot_locks`` path as before: ``$DCA_CRON_TRACKING_ROOT`` still
# relocates it with the rest of the tree, and no literal absolute path is
# pinned anywhere (AGENTS.md: "never from a literal ``/home/user/...``
# string").
#
# Growth: one empty, zero-byte file per stream per dated slot, kept forever.
# Nothing in this repository prunes any subtree of ``cron_tracking_root()`` --
# ``attempt_log``, which has exactly this shape (one small append-only file per
# stream per day), is likewise never pruned -- so inventing a reaper here would
# be a new, unreviewed mechanism guarding a handful of kilobytes a year. Left
# deliberately unpruned; a deployment that cares can delete files older than a
# few days out of band, since a lock file for a past slot is never read again.
SLOT_LOCK_ROOT_DIRNAME = "slot_locks"

# How many times a scheduled-live attempt retries its startup-failure
# publication WHILE STILL HOLDING the slot lock before giving up
# (MorningDCAExecutor._publish_startup_failure_artifacts_in_lock).
#
# Codex review: "Retry failure publication before releasing the slot lock".
# The in-lock publication used to get exactly one try: if the writer raised,
# the helper swallowed it, left the "already published" latch false, the
# ``finally`` released the slot lock, and run()'s outer handler retried the
# very same publication with NO lock held -- so a waiting same-slot
# invocation could take the lock in that gap and run its stale-raw/ cleanup
# or publish its own run_state concurrently with the retry, losing this
# attempt's accepted-order evidence and re-opening the duplicate-allocation
# hole the lock exists to close.
#
# Retries happen back-to-back with no sleep on purpose: the lock is held
# throughout, so every millisecond spent here is a millisecond a legitimate
# waiter for this slot is blocked. The failures a retry can actually clear
# are transient (a momentarily exhausted file-descriptor table, a losing
# race on a concurrent rename); a genuinely unwritable destination (disk
# full, permissions) will not heal inside the lock hold no matter how long
# we wait, and for that case the honest end state is "we could not publish;
# the run fails loudly" -- see
# _publish_startup_failure_artifacts_in_lock's docstring.
MORNING_STARTUP_FAILURE_PUBLICATION_ATTEMPTS = 3

logger = logging.getLogger(__name__)


def _model_usage_routing_overrides(config: Any) -> Dict[str, Any]:
    """Build the log_model_usage() kwargs for the config's routing-metadata
    overrides (see MorningConfig.model_usage_model_* / *_reason_for_premium_model),
    including only the fields the caller actually set so unset ones fall back to
    log_model_usage's own cost-conscious defaults rather than being overridden
    with an explicit None (which build_entry would reject)."""
    overrides = {
        "model_requested": getattr(config, "model_usage_model_requested", None),
        "model_actual": getattr(config, "model_usage_model_actual", None),
        "model_routing_mode": getattr(config, "model_usage_model_routing_mode", None),
        "reason_for_premium_model": getattr(config, "model_usage_reason_for_premium_model", None),
    }
    return {k: v for k, v in overrides.items() if v is not None}


def _scheduled_live_result_ok(result: Any) -> bool:
    """``GuardedRunConfig.result_ok`` predicate for scheduled live invocations.

    ``guarded_run`` pings this stream's dead-man's-switch heartbeat OK for any
    run that RETURNS rather than raises, so every failure the executor reports
    as *data* has to be classified here or it silently clears the switch.

    Three such structured failures exist:

    * ``artifacts["raw_persistence_error"]`` -- the raw-input recorder failed;
      the run is surfaced as data rather than raised (the original reason this
      predicate exists).
    * A result that published NO canonical ``run_state`` path. The scheduled
      slot-lock timeout branch of ``_run_impl`` deliberately returns a blocked
      ``slot_lock_unavailable`` result with ``paths == {}``: it writes nothing
      under this slot's run_state_dir precisely because it could not take the
      lock (Codex review discussion_r3840378333). That is the right artifact
      behavior, but it used to sail through this predicate -- it carries no
      ``raw_persistence_error`` -- and pinged a HEALTHY heartbeat for an
      invocation that did no trading work and left no evidence for the slot
      (Codex review, PR #593 discussion_r3840429737: "Mark slot-lock timeout
      results unhealthy"). If the lock holder is itself hung or later fails,
      that healthy ping clears the dead-man's switch while the scheduled slot
      stays silently missing.

      The unusable-``raw_dir`` branch (``error_class == "raw_dir_unusable"``,
      Codex review, PR #593: "Avoid publishing malformed-raw failures without
      the slot lock") returns the same ``paths == {}`` shape for the same
      reason -- it refuses to write anything under a run_state_dir whose
      ``raw/`` is a symlink or a non-directory, since an active same-slot
      lock holder may be mid-placement -- and is classified UNHEALTHY by this
      very rule, with no error-class special case of its own needed.
    * ``artifacts["error_class"] == "slot_lock_unavailable"`` on ANY shape of
      result, published artifact or not. The path-absence check above only
      catches the TIMEOUT branch; the OTHER slot-lock failure branch -- the
      non-timeout ``OSError`` on lock acquisition (a permissions or
      filesystem problem, where nothing else holds the lock and so there is
      no publication to race) -- deliberately DOES publish a blocked
      artifact via ``_blocked_live_result()``, so it reports a
      ``paths["run_state"]`` and carries no ``raw_persistence_error``, and
      sailed through as HEALTHY (Codex review, PR #593: "Treat every
      slot-lock failure as unhealthy"). Writing a blocked stub is not
      trading work: that invocation evaluated nothing, placed nothing, and
      is exactly as unable to vouch for the slot as the timed-out one, so it
      must ping a FAILED heartbeat too.

    Keying the second check on the ABSENCE of a canonical artifact path -- not
    on the ``slot_lock_unavailable`` error class alone -- is the honest signal
    for the broader "did no work at all" case and the one least likely to
    mislabel: every other scheduled-live return path, blocked ones included,
    routes through ``write_artifacts()`` and reports its
    ``paths["run_state"]``, so "no canonical artifact was published for this
    slot" is exactly the set of invocations that cannot vouch for it. The
    error-class check is layered on top of it rather than replacing it,
    because the two slot-lock branches disagree on whether an artifact is
    published while agreeing completely on having done no trading work. A
    non-dict result is left healthy, as before.
    """
    if not isinstance(result, dict):
        return True
    artifacts = result.get("artifacts") or {}
    if artifacts.get("raw_persistence_error"):
        return False
    # Checked before the path probe on purpose: this branch's whole point is
    # that a blocked artifact WAS published, so the probe below would return
    # True for it.
    if artifacts.get("error_class") == "slot_lock_unavailable":
        return False
    return bool((result.get("paths") or {}).get("run_state"))


# Morning budget caps. These are the SAME shared Normal-tier ceiling every live
# stream draws from (near_close_dca.BUDGET_TIERS[0] = 180.0/900.0, mirrored in
# config/budgets.json and config/morning_dca.json's budget_ledger block) -- not
# an isolated per-stream half-share. Raised from the old 90.0/450.0 halves
# 2026-08-02 (Geoff: "caps aren't pooled the way the target design intends --
# fix it"); see MorningConfig.cap_seed_roots and default_cap_seed_roots() for
# how cross-stream spend is folded in so this shared ceiling is actually
# checked against the whole day's/week's spend, not just this stream's own.
# This does NOT add this stream's own dynamic Elevated/High-volatility/Stress
# escalation -- only the 3:30 final run (near_close_dca.select_budget_tier)
# escalates past Normal, from its fuller same-day view; every earlier stream
# conservatively shares the flat Normal floor, which can only ever be MORE
# restrictive than the true escalated-day ceiling, never less.
DEFAULT_MORNING_DAILY_CAP_USD = 180.0
DEFAULT_MORNING_WEEKLY_CAP_USD = 900.0


def default_pooled_cap_seed_roots() -> tuple[str, ...]:
    """Every known live stream's root, for the shared budget cap ledger.

    Used as ``MorningConfig.cap_seed_roots``'s default_factory (2026-08-02
    pooling fix). Before this, the field's bare default was ``None`` -- no
    cap seeding at all -- while DEFAULT_MORNING_DAILY_CAP_USD/
    DEFAULT_MIDDAY_DAILY_CAP_USD were raised from an isolated $90/$450
    half-share to the full shared $180/$900 ceiling. A config that fails to
    load (``load_morning_config``'s / ``load_midday_config``'s "file missing
    or unparseable" fallback) or a caller that constructs ``MorningConfig``/
    ``MiddayConfig`` directly without setting ``cap_seed_roots`` would
    otherwise silently grant that run the FULL $180/$900 ceiling with zero
    cross-stream visibility -- MORE exposure than the old isolated defaults
    ever allowed, not less (Codex review: "Seed cross-stream roots for
    default pooled-cap configs"). Distinct from :func:`default_cap_seed_roots`
    below, which is a differently-scoped fallback for
    ``prior_allocation_roots``, not this field.

    Defined ahead of :class:`MorningConfig` (unlike the other default-root
    helpers further down this module) specifically so it can be used as that
    class's own field default_factory -- a dataclass field's default_factory
    must already be a bound name when the class body executes.

    Resolved at CALL time (not import time) via :func:`stream_root`, so a
    ``$DCA_CRON_TRACKING_ROOT`` set between import and construction is
    honored, matching every other default-root helper in this module.
    """
    roots = (
        stream_root("market_open_dca"),
        stream_root("morning_dca"),
        stream_root("midday_dca"),
        stream_root("intraday_dca"),
        stream_root("ce16ba6b"),
        # Out-of-tree manual final-buy override root (Codex review: "Include
        # out-of-tree manual fills in pooled roots"): scripts/run_classification.py
        # explicitly treats final_manual_review/** as same-day allocation context
        # (see _ROOTS_NEVER_REACHED_BY_CORE_LOADER), and real budget-block-override
        # fills live here (config/scheduled_tasks.json's jul17_manual_budget_block_
        # overrides). Without it, a manual override that filled budget-blocked
        # signals after a scheduled run is invisible to a later stream's shared
        # weekly ledger.
        stream_root("final_manual_review"),
    )
    return _append_dated_manual_override_cap_seed_roots(roots)


def _dated_manual_override_cap_seed_roots() -> tuple[str, ...]:
    """Every existing dated perplexity_primary_*/manual_override_* root.

    E.g. config/scheduled_tasks.json's aug18_manual_cap_override, which
    moved $207 of real capital under a daily-cap override the user
    explicitly authorized. Globbed at call time (like every other root in
    this module, resolved via :func:`stream_root`/:func:`cron_tracking_root`)
    rather than hardcoded to one date, since more such override roots can
    appear later.
    """
    try:
        parent = Path(cron_tracking_root())
        return tuple(
            str(p) for p in sorted(parent.glob("perplexity_primary_*/manual_override_*")) if p.is_dir()
        )
    except OSError:
        return ()


def _append_dated_manual_override_cap_seed_roots(roots: tuple[str, ...]) -> tuple[str, ...]:
    """Append every existing dated manual-override root not already present.

    Without seeding these, a later scheduled stream's shared daily/weekly
    cap ledger has zero visibility into that spend and could let a later
    run exceed the cap the override was only ever meant to bypass for that
    one instruction (Codex review discussion_r3839652545: "Register the
    override with runtime allocation and cap loaders").

    Applied both to :func:`default_pooled_cap_seed_roots`'s own factory
    output AND to an EXPLICITLY CONFIGURED ``artifact_paths.cap_seed_roots``
    list in ``load_morning_config``/``load_stream_config`` -- every
    production stream config (``config/morning_dca.json``,
    ``midday_dca.json``, ``market_open_dca.json``, ``intraday_dca.json``)
    supplies an explicit list that otherwise REPLACES the factory default
    wholesale, silently bypassing the override root a factory-only fix
    would still miss in production (Codex review, PR #656 follow-up on
    discussion_r3839652545: "Add override roots to configured cap seeds").

    A falsy/empty ``roots`` is returned unchanged: an explicit ``[]``
    deliberately disables cap seeding entirely (mirrors
    ``prior_allocation_roots``'s own empty-list contract), and silently
    reactivating it here would defeat that operator choice.
    """
    if not roots:
        return roots
    extra = tuple(r for r in _dated_manual_override_cap_seed_roots() if r not in roots)
    return roots + extra


@dataclass(frozen=True)
class TopUpOutcome:
    """Per-ticker result of the same-day prior-run offset/top-up guard.

    Shared by the morning and midday live streams; ``MiddayTopUpOutcome`` in
    ``midday_dca`` is an alias for backwards compatibility.
    """

    ticker: str
    desired_amount: float
    decision_price: Optional[float]
    prior_same_day_amount: float
    prior_same_day_reference_price: Optional[float]
    prior_same_day_run_id: Optional[str]
    prior_same_day_source: Optional[str]
    incremental_order_amount: float
    top_up_guard_result: str
    top_up_skipped: bool
    top_up_manual_review: bool = False
    # The base ($1 or $2) that actually produced desired_amount -- see
    # near_close_dca.resolve_top_up_desired_amount_usd and TopUpGuardOutcome's
    # matching field (Codex review: "Persist the preserved base on top-up
    # candidates").
    effective_base_amount_usd: float = 0.0
    # Set only when a sub-dollar ladder increment was routed through the
    # cumulative-credit policy: the price decline (prior.reference_price minus
    # this candidate's decision price) that was fed into apply_cumulative_credit.
    # None whenever the credit policy never ran (no prior allocation, price not
    # below reference, or a >= $1 ladder increment). The caller uses this to
    # decide whether the credit ledger needs a durable write -- see
    # MorningDCAExecutor._compute_top_up and _place_broker_orders (Codex
    # review: "Route sub-dollar top-ups through the credit policy").
    cumulative_credit_decline_usd: Optional[float] = None


@dataclass(frozen=True)
class MorningConfig:
    """Configuration hooks for the morning live stream.

    Kept separate from :class:`~near_close_dca.StrategyConfig` so the morning
    ladder, caps, decision anchor, and run-state root can never leak into the
    afternoon scheduled path. ``base_buy_amount_usd`` matches the afternoon $2
    base by default so the hypothetical dollar figures are comparable.
    """

    tickers: Sequence[str] = field(default_factory=lambda: tuple(ACTIVE_TICKERS))
    excluded_tickers: frozenset[str] = field(default_factory=lambda: frozenset(EXCLUDED_TICKERS))
    base_buy_amount_usd: float = 2.0
    # Reduced first-allocation base ($1) used when the price is below the prior
    # close but not below the prior regular-session low. See
    # near_close_dca.select_first_allocation_base_usd.
    base_buy_amount_below_close_usd: float = 1.0
    sizing_tiers: Tuple[Tuple[float, float], ...] = MORNING_SIZING_TIERS
    per_ticker_sizing: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    minimum_order_usd: float = 1.0
    minimum_drop_vs_previous_close: float = MORNING_MINIMUM_DROP
    # None means "use the active mode's default decision anchor" (09:45 for the
    # watchlist scan, 10:30 for the live execution).
    decision_time_et: Optional[time] = None
    interval_minutes: int = 5
    morning_daily_cap_usd: float = DEFAULT_MORNING_DAILY_CAP_USD
    morning_weekly_cap_usd: float = DEFAULT_MORNING_WEEKLY_CAP_USD
    # Optional explicit artifact-root override. When None the executor uses the
    # active mode's default root (watchlist vs. live).
    morning_run_state_root: Optional[str] = None
    # Opt-in root for the model_usage_log audit append (see
    # scripts/model_usage_log.py, config/scheduled_tasks.json ->
    # model_usage_log). None (default) disables the append entirely -- tests
    # and any caller that hasn't set this up never write to it.
    model_usage_log_root: Optional[str] = None
    # Routing metadata for the model_usage_log entry, overriding
    # log_model_usage's cost-conscious defaults. This code has no visibility
    # into which model/agent actually drove the run, so these stay None
    # unless the scheduler that invoked this run explicitly knows it used a
    # permitted premium escalation (e.g. failure recovery) and sets them --
    # e.g. via a wrapper that passes replace(config, model_routing_mode=...)
    # before calling run(). reason_for_premium_model is required (and only
    # meaningful) when model_routing_mode == "premium".
    model_usage_model_requested: Optional[str] = None
    model_usage_model_actual: Optional[str] = None
    model_usage_model_routing_mode: Optional[str] = None
    model_usage_reason_for_premium_model: Optional[str] = None
    # Root the 10:30 live gate reads the same-day 9:45 pre-flight artifact from.
    # When None the live executor uses DEFAULT_MORNING_PREFLIGHT_STATE_ROOT.
    preflight_run_state_root: Optional[str] = None
    # Roots the pre-flight artifact must NEVER be read from (see
    # live_task.preflight_artifact_resolution.must_not_look_under in
    # morning_dca.json). When empty, DEFAULT_PREFLIGHT_FORBIDDEN_ROOTS is used.
    preflight_forbidden_roots: Tuple[str, ...] = ()
    # Whether _apply_preflight_gate performs the single bounded recovery scan
    # (see live_task.preflight_artifact_resolution.bounded_recovery_lookup)
    # when the primary path and latest_preflight.json pointer both miss.
    preflight_bounded_recovery: bool = True
    # Filename of the persisted morning budget ledger snapshot the live task
    # writes under its own output directory (see budget_ledger.bootstrap /
    # budget_ledger.ledger_files in morning_dca.json).
    budget_ledger_filename: str = DEFAULT_BUDGET_LEDGER_FILENAME
    # Roots scanned for same-day prior allocations by the morning live top-up
    # guard. None (default) means use [DEFAULT_AFTERNOON_TASK_ROOT] at runtime
    # so manual/recovery fills before 10:30 AM are detected automatically.
    # Set to () (empty tuple) to fully disable the guard (equivalent to setting
    # artifact_paths.prior_allocation_roots to [] in morning_dca.json).
    # Override via artifact_paths.prior_allocation_roots in morning_dca.json;
    # an explicit [] is preserved as () to honour the opt-out intent.
    prior_allocation_roots: Optional[Tuple[str, ...]] = None
    # Roots seeded into the shared daily/weekly cap ledger with prior same-day/
    # week spend (2026-08-02 pooling fix: every sibling live stream's root, not
    # just this one -- see default_pooled_cap_seed_roots). OMITTING this kwarg
    # gets the default_factory (every known stream root) so a config that
    # fails to load, or a caller that constructs MorningConfig/MiddayConfig
    # directly, still gets cross-stream cap visibility instead of an isolated
    # full ceiling with zero seeding (Codex review: "Seed cross-stream roots
    # for default pooled-cap configs"). Explicitly passing () or None fully
    # disables cap seeding either way (both falsy to the `if cap_seed_roots:`
    # checks that consume this) -- e.g. for an isolated replay/staging config.
    # Override via artifact_paths.cap_seed_roots in morning_dca.json/
    # midday_dca.json.
    cap_seed_roots: Tuple[str, ...] = field(default_factory=default_pooled_cap_seed_roots)
    # Minimum number of completed intraday bars over the trend window required to
    # validate the intraday trend in live mode. When fewer are available a
    # qualifying candidate is flagged manual_review_candidate rather than bought.
    trend_min_bars: int = 6
    # Broker account number that must match the supplied BrokerAdapter before
    # any live orders are placed.
    target_account_number: Optional[str] = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
    # Weekday dates on which live (non-manual) execution is permitted. Empty
    # frozenset means the date gate is disabled (no restriction beyond weekends
    # and NYSE holidays). Mirrors StrategyConfig.approved_test_dates semantics
    # so morning/midday honour the same trading-window config as the afternoon.
    approved_test_dates: frozenset[date] = field(default_factory=frozenset)
    # Absolute per-order ceiling loaded from config/budgets.json
    # circuit_breakers.max_per_order_usd. 5x * $2 base = $10 default. Any
    # single order amount above this is blocked before it reaches the broker.
    max_per_order_usd: float = 10.0
    # Shared daily order-ATTEMPT circuit breaker loaded from config/budgets.json
    # circuit_breakers.max_orders_per_day -- the same knob near_close_dca's own
    # StrategyConfig.max_orders_per_day enforces, shared across all seven live
    # slots and manual buying. None (the default for a directly constructed
    # MorningConfig, e.g. tests) means uncapped; load_morning_config/
    # load_stream_config overlay the configured integer for every real config
    # load (Codex review: "Enforce the shared daily order-count cap in new
    # executors"). See MorningLedger.day_attempts/record_attempt and
    # _place_broker_orders for the enforcement itself.
    max_orders_per_day: Optional[int] = None
    # Bid/ask spread circuit breaker for live placement, mirroring the
    # afternoon stream's StrategyConfig.max_bid_ask_spread_pct. A candidate
    # whose live quote spread exceeds this percent of the quote midpoint is
    # blocked before placement rather than bought at a stale/no-data price.
    max_bid_ask_spread_pct: float = 1.0
    # Master placement switch for this stream. When False the executor still runs
    # the full evaluate/signal/top-up/budget/artifact path and writes every
    # artifact, but never calls the broker -- a shadow run whose decisions can be
    # diffed against whatever actually traded the slot. Defaults True so morning
    # and midday are unchanged; the market-open and intraday streams ship with it
    # False in their JSON config until their output has been verified against a
    # real trading day (docs/safeguards.md#live-execution-roster).
    live_execution_enabled: bool = True

    def as_strategy_config(self) -> StrategyConfig:
        """Return a StrategyConfig view carrying the morning ladder.

        Only the fields the shared sizing math reads (``sizing_tiers``,
        ``minimum_drop_vs_previous_close``, ``base_buy_amount_usd``) are set; the
        result is used solely to drive :func:`graded_buy_multiplier` and never to
        run the afternoon live path. ``dry_run`` stays True.
        """
        return StrategyConfig(
            base_buy_amount_usd=self.base_buy_amount_usd,
            base_buy_amount_below_close_usd=self.base_buy_amount_below_close_usd,
            sizing_tiers=tuple(self.sizing_tiers),
            per_ticker_sizing=self.per_ticker_sizing,
            minimum_drop_vs_previous_close=self.minimum_drop_vs_previous_close,
            dry_run=True,
        )


@dataclass
class MorningLedger:
    """A budget ledger namespaced to the morning stream.

    This is a self-contained session that tracks the morning live stream's own
    notional. It shares no state with the afternoon
    :class:`~near_close_dca.StrategyState`, so morning activity can never consume
    or validate the afternoon 3:30/3:45 daily/weekly budget ledger.
    """

    namespace: str = MORNING_STREAM
    daily_cap_usd: float = DEFAULT_MORNING_DAILY_CAP_USD
    weekly_cap_usd: float = DEFAULT_MORNING_WEEKLY_CAP_USD
    planned_by_day: Dict[date, float] = field(default_factory=dict)
    planned_by_week: Dict[Tuple[int, int], float] = field(default_factory=dict)
    # Order-ATTEMPT counter for MorningConfig.max_orders_per_day (Codex review:
    # "Enforce the shared daily order-count cap in new executors"). Separate
    # from planned_by_day/_week, which track notional, not attempt counts.
    attempts_by_day: Dict[date, int] = field(default_factory=dict)

    def record_hypothetical(self, trading_day: date, amount_usd: float) -> None:
        if amount_usd <= 0:
            return
        self.planned_by_day[trading_day] = self.day_planned(trading_day) + amount_usd
        week = iso_week_key(trading_day)
        self.planned_by_week[week] = self.planned_by_week.get(week, 0.0) + amount_usd

    def day_attempts(self, trading_day: date) -> int:
        return self.attempts_by_day.get(trading_day, 0)

    def record_attempt(self, trading_day: date) -> None:
        self.attempts_by_day[trading_day] = self.day_attempts(trading_day) + 1

    def seed_day_attempts(self, trading_day: date, count: int) -> None:
        """Raise today's attempt count to at least ``count``.

        Monotonic (never decreases), so a retry re-seeding from the same
        deterministic on-disk cross-stream state is idempotent -- unlike
        ``record_attempt``, which increments once per real placement this
        run makes.
        """
        if count > self.day_attempts(trading_day):
            self.attempts_by_day[trading_day] = count

    def day_planned(self, trading_day: date) -> float:
        return self.planned_by_day.get(trading_day, 0.0)

    def week_planned(self, trading_day: date) -> float:
        return self.planned_by_week.get(iso_week_key(trading_day), 0.0)

    def remaining_daily(self, trading_day: date) -> float:
        return max(0.0, self.daily_cap_usd - self.day_planned(trading_day))

    def remaining_weekly(self, trading_day: date) -> float:
        return max(0.0, self.weekly_cap_usd - self.week_planned(trading_day))

    def over_daily_cap(self, trading_day: date) -> bool:
        return self.day_planned(trading_day) > self.daily_cap_usd

    def over_weekly_cap(self, trading_day: date) -> bool:
        return self.week_planned(trading_day) > self.weekly_cap_usd


# Live-mode execution decision for a qualifying candidate.
#   auto_buy_eligible     -> price/drop passed AND intraday trend validated.
#   manual_review_candidate -> price/drop passed but intraday trend could not be
#                              validated; the run does NOT auto-buy.
EXEC_AUTO_BUY_ELIGIBLE = "auto_buy_eligible"
EXEC_MANUAL_REVIEW_CANDIDATE = "manual_review_candidate"


def preflight_eligibility(signal: "MorningSignal") -> bool:
    """Return whether ``signal`` is eligible for the 10:30 live evaluator.

    This is the 9:45 pre-flight verdict: a COARSE pre-screen answering "is this
    ticker worth re-evaluating at 10:30?", NOT a final buy approval. A ticker is
    eligible when it graded to a positive-multiplier BUY at 9:45 with both a
    usable current price and previous close. The 10:30 live run always recomputes
    signals/budget/spread/trend/top-up from scratch and may still decline, so an
    eligible ticker is not guaranteed to be bought and an ineligible ticker is
    simply skipped by the live gate.
    """
    return (
        signal.action == Action.BUY
        and signal.multiplier > 0
        and signal.current_price is not None
        and signal.previous_close is not None
    )


def preflight_ticker_eligibility_row(signal: "MorningSignal") -> Dict[str, Any]:
    """Build one machine-complete ``per_ticker_eligibility`` row for ``signal``.

    Emits every field the 10:30 live gate consumes, exactly once per active
    symbol. Fields the coarse read-only pre-flight does not evaluate (account,
    buying power, spread) are ``None`` rather than fabricated; the 10:30 live run
    computes them fresh. Trend is ``not_evaluated_preflight`` (no 60-minute
    window at 9:45). Unresolved symbols (no usable quote or prior close, e.g.
    AAAU/SPCX) are marked ``eligible_to_execute=false`` with
    ``ineligible_reason='unresolved_ticker'`` and never invalidate the artifact.
    """
    quote_available = signal.current_price is not None
    prior_close_available = signal.previous_close is not None
    resolved = quote_available and prior_close_available
    eligible = preflight_eligibility(signal)

    if not quote_available and not prior_close_available:
        ineligible_reason: Optional[str] = "unresolved_ticker"
    elif not quote_available:
        ineligible_reason = "quote_unavailable"
    elif not prior_close_available:
        ineligible_reason = "prior_close_unavailable"
    elif not eligible:
        ineligible_reason = "no_qualifying_drop"
    else:
        ineligible_reason = None

    return {
        "symbol": signal.ticker,
        "active_universe": True,
        "quote_available": quote_available,
        "prior_close_available": prior_close_available,
        "tradable": resolved,
        # Account/buying-power/spread are not bound by the read-only pre-flight;
        # the 10:30 live run performs the authoritative checks.
        "account_ok": None,
        "buying_power_ok": None,
        "spread_pct": None,
        "spread_ok": None,
        "trend_status": MORNING_PREFLIGHT_TREND_STATUS,
        "data_confidence_status": "resolved" if resolved else "unresolved",
        "early_drop_pct": signal.drop_pct,
        "early_multiplier": signal.multiplier,
        "hypothetical_amount": signal.hypothetical_amount_usd,
        "eligible_to_execute": eligible,
        "ineligible_reason": ineligible_reason,
    }


@dataclass(frozen=True)
class MorningSignal:
    """One morning signal row for a single ticker."""

    ticker: str
    source: str
    action: Action
    reason: str
    current_price: Optional[float]
    previous_close: Optional[float]
    drop_pct: Optional[float]
    multiplier: float
    hypothetical_amount_usd: float
    warnings: Tuple[str, ...] = ()
    # Prior regular-session low and the first-allocation base scale selected from
    # it (see near_close_dca.select_first_allocation_base_usd). hypothetical_amount_usd
    # == selected_base_amount_usd * multiplier.
    previous_low: Optional[float] = None
    selected_base_amount_usd: float = 0.0
    base_selection_reason: str = ""
    # Live mode only: the execution decision for a qualifying candidate. None in
    # watchlist mode (no execution decision is taken there).
    execution_status: Optional[str] = None
    trend_validated: Optional[bool] = None
    # Price provenance + intraday OHLCV for the archived observation (see
    # dca_intraday_archive). All optional and defaulted, so every existing
    # construction site keeps working; MorningDCAExecutor._current_price fills
    # them via its `provenance` out-parameter.
    observation: Optional["PriceObservation"] = None

    def to_row(self) -> Dict[str, Any]:
        row = {
            "ticker": self.ticker,
            "source": self.source,
            "action": self.action.value,
            "reason": self.reason,
            "current_price": self.current_price,
            "previous_close": self.previous_close,
            "drop_pct": self.drop_pct,
            "multiplier": self.multiplier,
            "previous_low": self.previous_low,
            "selected_base_amount_usd": self.selected_base_amount_usd,
            "base_selection_reason": self.base_selection_reason,
            "hypothetical_amount_usd": self.hypothetical_amount_usd,
            "warnings": "; ".join(self.warnings),
        }
        # Keep the signal CSV schema stable across mixed live rows. Some rows
        # (for example NO_DATA or sub-threshold skips) do not make an execution
        # decision, but later BUY rows may carry these fields; including them on
        # every row prevents DictWriter field mismatches and gives downstream
        # readers predictable columns.
        row["execution_status"] = self.execution_status
        row["trend_validated"] = self.trend_validated
        row["auto_buy_signal"] = self.execution_status == EXEC_AUTO_BUY_ELIGIBLE
        # Row-level spread outcome, filled in by the placement loop for the rows
        # that actually reach the spread gate. Present on every row so the CSV
        # header is stable and a reader can tell "never reached the gate"
        # (not_evaluated) from "failed closed there" without parsing skip prose.
        row.setdefault("spread_ok", None)
        row.setdefault("spread_pct", None)
        row.setdefault("spread_status", SPREAD_STATUS_NOT_EVALUATED)
        # Observation columns are written on EVERY row, present or not, for the
        # same reason as the spread columns above: a stable CSV header across
        # mixed rows. A row whose price never resolved (NO_DATA) still carries
        # the columns, empty -- which is itself the useful record that this
        # ticker was polled at this slot and produced nothing.
        row.update((self.observation or PriceObservation()).to_row())
        return row


@dataclass(frozen=True)
class PriceObservation:
    """Where a decision price came from, and the intraday bars behind it.

    Populated by :meth:`MorningDCAExecutor._current_price` (which writes into a
    plain dict) and by :meth:`MorningDCAExecutor._session_stats`. Kept as its own
    frozen record rather than a dozen more fields on :class:`MorningSignal` so
    the archive's column set can grow without widening the signal dataclass.
    """

    price_source: Optional[str] = None
    price_as_of: Optional[datetime] = None
    previous_close_as_of: Optional[datetime] = None
    quote_price: Optional[float] = None
    quote_time: Optional[datetime] = None
    bar_timestamp: Optional[datetime] = None
    bar_open: Optional[float] = None
    bar_high: Optional[float] = None
    bar_low: Optional[float] = None
    bar_close: Optional[float] = None
    bar_volume: Optional[float] = None
    # Regular-session aggregates from 9:30 ET through the decision anchor. The
    # daily OHLCV archive cannot express these mid-session, which is the whole
    # point of recording them per poll.
    session_open: Optional[float] = None
    session_high: Optional[float] = None
    session_low: Optional[float] = None
    session_volume: Optional[float] = None
    bar_count: Optional[int] = None
    interval_minutes: Optional[int] = None

    _ROW_FIELDS: ClassVar[Tuple[str, ...]] = (
        "price_source",
        "price_as_of",
        "previous_close_as_of",
        "quote_price",
        "quote_time",
        "bar_timestamp",
        "bar_open",
        "bar_high",
        "bar_low",
        "bar_close",
        "bar_volume",
        "session_open",
        "session_high",
        "session_low",
        "session_volume",
        "bar_count",
        "interval_minutes",
    )

    def to_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for field_name in self._ROW_FIELDS:
            value = getattr(self, field_name)
            row[field_name] = value.isoformat() if isinstance(value, datetime) else value
        return row


def _finite_float_or_none(value: Any) -> Optional[float]:
    """Coerce to a finite float, or ``None``. Never raises.

    Used on raw provider values headed for the observation archive, where a
    ``None``/``NaN``/unparseable price must land as an empty cell rather than
    abort the run that was only trying to record it.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def session_stats(bars: Sequence[Bar]) -> Dict[str, Any]:
    """Aggregate regular-session bars into open/high/low/volume so far.

    ``bars`` is the 9:30-ET-through-decision-anchor window the executor already
    fetched, so this adds no provider calls. Returns ``{}`` for an empty or
    entirely non-finite window rather than inventing zeroes -- a zero session
    volume and an unknown session volume must not look the same in the archive.
    """
    opens = [b for b in sorted(bars, key=lambda b: b.timestamp) if math.isfinite(b.open)]
    highs = [b.high for b in bars if math.isfinite(b.high)]
    lows = [b.low for b in bars if math.isfinite(b.low)]
    volumes = [b.volume for b in bars if math.isfinite(b.volume)]
    stats: Dict[str, Any] = {}
    if opens:
        stats["session_open"] = opens[0].open
    if highs:
        stats["session_high"] = max(highs)
    if lows:
        stats["session_low"] = min(lows)
    if volumes:
        stats["session_volume"] = sum(volumes)
    return stats


def load_max_per_order_usd(config_dir_path: Path) -> Optional[float]:
    """Read ``circuit_breakers.max_per_order_usd`` from ``config/budgets.json``.

    Returns the value when present and valid, ``None`` when the file is absent or
    the key is not set. Raises ``ValueError`` when the key is present but invalid,
    and raises ``RuntimeError`` when the file exists but cannot be read or parsed
    (both are fail-closed: a cap file that exists but is unreadable must not
    silently fall back to the dataclass default and bypass a deliberate cap edit).
    """
    budgets_path = config_dir_path / "budgets.json"
    if not budgets_path.exists():
        return None
    try:
        budgets_raw = json.loads(budgets_path.read_bytes())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"budgets.json exists but could not be read or parsed; "
            f"refusing to silently bypass the per-order cap ({exc})"
        ) from exc
    if not isinstance(budgets_raw, dict):
        raise RuntimeError(
            f"budgets.json exists but is not a JSON object (got {type(budgets_raw).__name__}); "
            "refusing to silently bypass the per-order cap"
        )
    cb = budgets_raw.get("circuit_breakers", {})
    if not isinstance(cb, dict):
        raise RuntimeError(
            f"budgets.json circuit_breakers must be an object, got {type(cb).__name__}; "
            "refusing to silently bypass the per-order cap"
        )
    if "max_per_order_usd" not in cb:
        return None
    raw_val = cb["max_per_order_usd"]
    try:
        max_order = float(raw_val)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"budgets.json circuit_breakers.max_per_order_usd must be a finite "
            f"positive number, got {raw_val!r}"
        ) from exc
    if not (math.isfinite(max_order) and max_order > 0):
        raise ValueError(
            f"budgets.json circuit_breakers.max_per_order_usd must be a finite "
            f"positive number, got {raw_val!r}"
        )
    return max_order


def load_max_orders_per_day(config_dir_path: Path) -> Optional[int]:
    """Read ``circuit_breakers.max_orders_per_day`` from ``config/budgets.json``.

    Mirrors :func:`load_max_per_order_usd`'s fail-closed contract: returns the
    value when present and valid, ``None`` when the file is absent or the key
    is not set, raises ``ValueError`` when the key is present but invalid, and
    raises ``RuntimeError`` when the file exists but cannot be read or parsed
    -- a cap file that exists but is unreadable must not silently fall back to
    the dataclass default and bypass a deliberate cap edit.
    """
    budgets_path = config_dir_path / "budgets.json"
    if not budgets_path.exists():
        return None
    try:
        budgets_raw = json.loads(budgets_path.read_bytes())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"budgets.json exists but could not be read or parsed; "
            f"refusing to silently bypass the daily order-count cap ({exc})"
        ) from exc
    if not isinstance(budgets_raw, dict):
        raise RuntimeError(
            f"budgets.json exists but is not a JSON object (got {type(budgets_raw).__name__}); "
            "refusing to silently bypass the daily order-count cap"
        )
    cb = budgets_raw.get("circuit_breakers", {})
    if not isinstance(cb, dict):
        raise RuntimeError(
            f"budgets.json circuit_breakers must be an object, got {type(cb).__name__}; "
            "refusing to silently bypass the daily order-count cap"
        )
    if "max_orders_per_day" not in cb:
        return None
    raw_val = cb["max_orders_per_day"]
    if isinstance(raw_val, bool) or not isinstance(raw_val, int) or raw_val <= 0:
        raise ValueError(
            f"budgets.json circuit_breakers.max_orders_per_day must be a positive "
            f"integer, got {raw_val!r}"
        )
    return raw_val


# A non-empty, unreachable date so the existing `bool(approved) and trading_day
# not in approved` gate check in _maybe_block_for_holiday() still blocks every
# real trading day (fail closed) rather than silently disabling the date gate
# the way an empty frozenset would (see _load_approved_dates_fail_closed()).
_APPROVED_DATES_LOAD_FAILED = frozenset({date.min})


def _load_approved_dates_fail_closed(config_dir_path: Path) -> "frozenset[date]":
    """Return approved_test_dates from trading_window.json, failing closed on error.

    An absent trading_window.json is a legitimate, expected configuration (the
    stream falls back to its hardcoded/empty default, matching prior behavior)
    and load_approved_dates() returns ``{}`` for that case without raising. It
    only raises when the file exists but is malformed (missing start_date/
    end_date, an invalid date range, or a bad decision_time_et) -- an anomalous
    state that must not be treated the same as "no restriction configured".
    Falling back to an empty set there would silently disable the live date
    gate precisely when its configuration is broken, so this returns
    _APPROVED_DATES_LOAD_FAILED (an unreachable date) instead, which blocks
    every real trading day through the normal "outside approved trading
    window" path.
    """
    try:
        return load_approved_dates(config_dir_path).get("approved_test_dates", frozenset())
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "could not load approved dates from trading_window.json (%s); "
            "failing closed (blocking all dates) rather than disabling the gate",
            exc,
        )
        return _APPROVED_DATES_LOAD_FAILED


def default_prior_allocation_roots() -> tuple[str, ...]:
    """The default prior-allocation roots, resolved at CALL time.

    Must not be the DEFAULT_*_ROOT module constants: those bind when this module
    is imported, while the roots loaded from ``config/*.json`` are rebased when
    the config is READ. Setting ``$DCA_CRON_TRACKING_ROOT`` between those two
    moments -- an import, then an override, then a load -- moved the loaded
    values and left the constants behind, so the exact-tuple comparison in
    ``_prior_allocation_roots`` saw a mismatch and concluded the caller had
    explicitly overridden the roots. That skips the same-day manual top-up merge,
    and a midday run can then re-buy a ticker an earlier manual allocation
    already covered (Codex review, PR #219).

    Deriving both sides from ``stream_root`` at the same moment keeps them in
    step regardless of when the override lands.
    """
    return (
        stream_root("market_open_dca"),
        stream_root("morning_dca"),
        stream_root("intraday_dca"),
        stream_root("ce16ba6b"),
    )


def default_cap_seed_roots() -> tuple[str, str]:
    """The default (afternoon, market-open) cap-seeding roots, at CALL time.

    ``_compute_top_up`` used to name ``DEFAULT_AFTERNOON_TASK_ROOT`` and
    ``DEFAULT_MARKET_OPEN_TASK_ROOT`` directly inside its body. Both are
    module-level constants bound once at import, so a ``$DCA_CRON_TRACKING_ROOT``
    set afterward left the cap-seeding scan reading the OLD tree on every run
    that leaves ``prior_allocation_roots`` at its default -- not just the
    "config file missing" fallback, the common case (Codex review, PR #219).

    A separate function from :func:`default_prior_allocation_roots` because the
    two lists are genuinely different: this one is the pair
    ``_compute_top_up`` actually scans for cap-seeding, not the four-root tuple
    used to detect whether the caller left ``prior_allocation_roots`` at its
    class default.

    Named and public, not inlined, so a test can redirect it the same way
    :func:`default_prior_allocation_roots` is redirected -- monkeypatching the
    now-unused ``DEFAULT_AFTERNOON_TASK_ROOT``/``DEFAULT_MARKET_OPEN_TASK_ROOT``
    module constants would no longer have any effect on this function.
    """
    return stream_root("ce16ba6b"), stream_root("market_open_dca")


def default_preflight_forbidden_roots() -> tuple[str, ...]:
    """The default must-not-look-under roots for the preflight gate, at CALL time.

    ``_apply_preflight_gate`` already resolves its OWN ``root`` fresh via
    ``stream_root("morning_dca_preflight")`` at call time, but used to compare
    it against ``DEFAULT_PREFLIGHT_FORBIDDEN_ROOTS`` directly -- a module-level
    tuple bound once at import. A ``$DCA_CRON_TRACKING_ROOT`` set afterward
    moved ``root`` to the new tree while these forbidden roots stayed under the
    old one, so a ``preflight_run_state_root`` that relocated into the (now
    unguarded) old live-output directory passed the ``must_not_look_under``
    check it exists to enforce (Codex review, PR #219: "Resolve forbidden
    preflight roots at call time").
    """
    return stream_root("42173154"), stream_root("morning_dca")


def load_morning_config(
    config_dir=None,
    mode: str = MORNING_WATCHLIST_MODE,
    *,
    allow_unpinned_account: bool = False,
):
    """Read ``config/morning_dca.json`` and return a :class:`MorningConfig`.

    ``mode`` selects which artifact root to bind (the watchlist scan and the live
    execution share the morning ladder and budget caps but write to distinct
    roots). Looks for ``morning_dca.json`` in ``config_dir`` (default: resolved
    by :func:`dca_config.get_config_dir` -- checks ``$DCA_CONFIG_DIR``, then
    ``config/`` beside this module, then the installed-wheel share dir, then
    ``./config``). If the file is absent or unreadable the function logs a
    warning and returns ``MorningConfig()`` with hardcoded defaults -- the same
    graceful-fallback pattern used by :func:`dca_config.load_strategy_config`.

    ``allow_unpinned_account`` decides what happens when the configured
    ``broker_account`` cannot be resolved -- almost always because
    ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` is unset in the scheduler's
    environment. The default raises, which is right for a caller that only wants
    a path or a report. It is WRONG for the scheduled live path: the raise
    happens inside ``MorningDCAExecutor.__init__``, before an executor exists, so
    the fail-closed machinery that exists for exactly this case
    (``account_pinning_failure`` -> ``_blocked_live_result``) was unreachable for
    the most common pinning failure, and the slot produced no artifact at all --
    monitoring saw a MISSING run rather than a blocked one, which is the
    distinction the blocked-evidence path exists to preserve (Codex review,
    PR #219).

    With it set, ``target_account_number`` is left unset instead. That is not a
    weakening: an unset account is precisely what ``account_pinning_failure``
    refuses to trade on, and ``build_broker`` refuses to construct a live adapter
    without one. The run still places nothing -- it just leaves evidence saying
    so.
    """
    if config_dir is None:
        config_dir_path = get_config_dir()
    else:
        config_dir_path = Path(config_dir)

    cfg_path = config_dir_path / "morning_dca.json"
    if not cfg_path.exists():
        logger.warning(
            "morning_dca.json not found at %s; using MorningConfig defaults", cfg_path
        )
        approved_dates = _load_approved_dates_fail_closed(config_dir_path)
        # morning_run_state_root/preflight_run_state_root deliberately stay
        # None here rather than being resolved via stream_root() at load
        # time the way model_usage_log_root is just below: run_dca.py's
        # _resolve_cross_stream_prior_allocation_roots reads this exact
        # field to tell "this stream's config actually configured a root"
        # (truthy) apart from "no config, use the default" (None/falsy) --
        # baking a real path in here made every config-missing fallback look
        # explicitly configured, which defeated that containment check and
        # let a relocated (test/demo/staging) run's cross-stream scan trust
        # the bundled production default (Codex/self-review, PR #219: "Keep
        # relocated runs contained" regressed by an earlier fix here). The
        # staleness this was meant to fix -- self.state_root and the
        # preflight-gate root falling back to an import-time DEFAULT_*
        # constant instead of a fresh stream_root() call -- is fixed at
        # those actual call sites instead (see MorningDCAExecutor.__init__
        # and _apply_preflight_gate).
        #
        # target_account_number is resolved fresh here too, mirroring the
        # config-present path below: MorningConfig's dataclass default is
        # ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, an import-time constant, so a
        # config-missing live run either pinned a stale account or blocked on
        # a stale unset sentinel instead of reflecting the account actually
        # set in the environment right now (Codex review, PR #219: "Resolve
        # the account when falling back to defaults"). Watchlist/preflight
        # never trade, so they get the READ_ONLY_ACCOUNT literal rather than
        # a real account -- the same value the bundled config's own
        # watchlist_task/preflight_task blocks use -- as defense in depth
        # alongside run_classification's own profile["trades"] gate (Codex
        # review, PR #219: "Omit account numbers from read-only
        # classifications").
        if mode == MORNING_LIVE_MODE:
            try:
                account_number = resolve_broker_account(
                    "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}",
                    source="morning_dca.json missing; live_task fallback",
                )
            except (MissingConfigEnvError, InvalidAccountNumberError):
                if not allow_unpinned_account:
                    raise
                # See this function's docstring: an unset account here is
                # exactly what account_pinning_failure() fails closed on.
                account_number = None
        else:
            account_number = READ_ONLY_ACCOUNT
        return MorningConfig(
            approved_test_dates=approved_dates,
            model_usage_log_root=stream_root("model_usage"),
            target_account_number=account_number,
        )

    approved_date_overrides = {
        "approved_test_dates": _load_approved_dates_fail_closed(config_dir_path),
        # stream_root() at LOAD time, not the import-time constant:
        # $DCA_CRON_TRACKING_ROOT set between import and config load
        # rebased the run-state roots while leaving model-usage evidence
        # (and the attempt-log sibling derived from it) under the old
        # tree, where the call-time credit-usage report never looks
        # (Codex review, PR #219).
        "model_usage_log_root": stream_root("model_usage"),
    }

    try:
        raw = json.loads(cfg_path.read_bytes())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("could not parse %s (%s); using MorningConfig defaults", cfg_path, exc)
        return MorningConfig(**approved_date_overrides)

    # The bundled artifact_paths / prior_allocation_roots / preflight-resolution
    # values are absolute paths under the default cron_tracking tree, and they
    # are loaded into the executor -- overriding the stream_root()-derived
    # defaults. Without this, setting $DCA_CRON_TRACKING_ROOT moved every
    # dca_paths consumer but left the executors reading and writing the bundled
    # production tree. No-op when the variable is unset.
    raw = rebase_bundled_roots_in(raw)

    kwargs: Dict[str, Any] = dict(approved_date_overrides)

    sizing = raw.get("sizing_ladder", {})
    if "base_amount_usd" in sizing:
        kwargs["base_buy_amount_usd"] = float(sizing["base_amount_usd"])
    if "base_amount_below_close_usd" in sizing:
        kwargs["base_buy_amount_below_close_usd"] = float(sizing["base_amount_below_close_usd"])
    if "no_buy_below_pct" in sizing:
        kwargs["minimum_drop_vs_previous_close"] = float(sizing["no_buy_below_pct"]) / 100.0
    if "tiers" in sizing:
        tiers = tuple(
            (float(t["min_drop_pct"]) / 100.0, float(t["multiplier"]))
            for t in sizing["tiers"]
        )
        kwargs["sizing_tiers"] = tiers

    budget = raw.get("budget_ledger", {})
    if "morning_daily_cap_usd" in budget:
        kwargs["morning_daily_cap_usd"] = float(budget["morning_daily_cap_usd"])
    if "morning_weekly_cap_usd" in budget:
        kwargs["morning_weekly_cap_usd"] = float(budget["morning_weekly_cap_usd"])
    # budget_ledger.bootstrap (added Jul 20 2026): the live task persists a
    # budget_ledger.json snapshot under its own output directory using this
    # configured filename, bootstrapping it from the caps above when absent
    # rather than requiring a pre-existing ledger file.
    ledger_files = budget.get("ledger_files")
    if isinstance(ledger_files, list) and ledger_files:
        kwargs["budget_ledger_filename"] = str(ledger_files[0])

    if "trend_min_bars" in raw:
        kwargs["trend_min_bars"] = int(raw["trend_min_bars"])

    live_task = raw.get("live_task", {})

    # The broker account comes from THIS mode's task block, not unconditionally
    # from live_task. Each block already carries the right value -- live_task has
    # the "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}" reference, watchlist_task and
    # preflight_task have the literal "read_only" -- but this used to read
    # live_task for every mode, so the read-only scans silently bound the live
    # trading account.
    #
    # That became load-breaking once the account moved into the environment:
    # expand_config_env raises when the variable is unset, so constructing a
    # watchlist or preflight executor started failing on a host where the secret
    # is scoped only to the trading jobs -- and the 9:45 preflight artifact is a
    # hard dependency of the 10:30 live run (Codex review, PR #219: "Require the
    # account variable only for live mode"). Reading the mode's own block fixes
    # both: read-only modes get "read_only" and never touch the variable.
    mode_task_key = {
        MORNING_WATCHLIST_MODE: "watchlist_task",
        MORNING_PREFLIGHT_MODE: "preflight_task",
        MORNING_LIVE_MODE: "live_task",
    }.get(mode)
    account_task = raw.get(mode_task_key) if mode_task_key else None
    if not isinstance(account_task, dict) or "broker_account" not in account_task:
        # No block for this mode (or an older config without the per-mode
        # blocks): fall back to live_task, preserving the previous behavior.
        account_task, account_task_key = live_task, "live_task"
    else:
        account_task_key = mode_task_key
    if "broker_account" in account_task:
        source = f"{mode} {account_task_key}.broker_account"
        try:
            kwargs["target_account_number"] = resolve_broker_account(
                account_task["broker_account"], source=source
            )
        except (MissingConfigEnvError, InvalidAccountNumberError):
            if not allow_unpinned_account:
                raise
            # Explicitly None, NOT merely left out: the dataclass default is the
            # IMPORT-TIME module constant, so omitting the kwarg would silently
            # fall back to whatever account the environment held when this module
            # was first imported -- which for a malformed configured account is a
            # real, usable account number. That would turn a pinning failure into
            # a live run against an unintended account, the exact bypass this
            # whole path exists to close.
            kwargs["target_account_number"] = None
            # Leave target_account_number unset so the caller's live path reaches
            # account_pinning_failure() and writes a durable blocked artifact.
            # See load_morning_config's `allow_unpinned_account` note.
            logging.warning(
                "%s could not be resolved to a usable account; the live run will "
                "fail closed and record account_pinning_failed",
                source,
            )
    # preflight_artifact_resolution (added Jul 20 2026): pins where the 10:30
    # live gate is and is not allowed to read the same-day 9:45 pre-flight
    # artifact from, and whether it performs the single bounded recovery scan.
    preflight_resolution = live_task.get("preflight_artifact_resolution", {})
    forbidden_roots = preflight_resolution.get("must_not_look_under")
    if isinstance(forbidden_roots, list) and forbidden_roots:
        kwargs["preflight_forbidden_roots"] = tuple(str(p) for p in forbidden_roots)
    if "bounded_recovery_lookup" in preflight_resolution:
        kwargs["preflight_bounded_recovery"] = bool(preflight_resolution["bounded_recovery_lookup"])

    artifact_paths = raw.get("artifact_paths", {})
    # Per-mode root keys take precedence; fall back to the legacy shared key.
    mode_root_key = {
        MORNING_WATCHLIST_MODE: "watchlist_run_state_root",
        MORNING_PREFLIGHT_MODE: "preflight_run_state_root",
        MORNING_LIVE_MODE: "live_run_state_root",
    }.get(mode)
    if mode_root_key and mode_root_key in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths[mode_root_key]
    elif "morning_run_state_root" in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths["morning_run_state_root"]
    # The 10:30 live gate reads the same-day pre-flight artifact from this root.
    if "preflight_run_state_root" in artifact_paths:
        kwargs["preflight_run_state_root"] = artifact_paths["preflight_run_state_root"]
    prior_roots = artifact_paths.get("prior_allocation_roots")
    if isinstance(prior_roots, list):
        # Preserve [] as () so an explicit empty array in JSON fully disables
        # the guard (None = not set = use default afternoon root at runtime).
        kwargs["prior_allocation_roots"] = tuple(str(r) for r in prior_roots)
    cap_roots = artifact_paths.get("cap_seed_roots")
    if isinstance(cap_roots, list):
        # Preserve an explicit [] as () so it fully disables cap seeding
        # rather than falling through to MorningConfig's default_factory
        # (every sibling stream root) -- consistent with how
        # prior_allocation_roots treats an explicit empty array above.
        # Still appends any existing dated manual-override root on top of a
        # non-empty configured list, exactly like the default_factory does,
        # so a configured cap_seed_roots list (every production stream
        # config sets one) does not silently bypass that visibility (Codex
        # review, PR #656 follow-up on discussion_r3839652545: "Add
        # override roots to configured cap seeds").
        kwargs["cap_seed_roots"] = _append_dated_manual_override_cap_seed_roots(
            tuple(str(r) for r in cap_roots)
        )
    # model_usage_log_root (see scripts/model_usage_log.py, config/scheduled_tasks.json
    # -> model_usage_log): defaults to stream_root("model_usage"), resolved when
    # this loader RUNS rather than at import, so the audit
    # log is enabled for every real load_morning_config() caller; an explicit
    # artifact_paths.model_usage_log_root overrides it, and null/empty-string disables
    # the append entirely for this stream.
    if "model_usage_log_root" in artifact_paths:
        configured_log_root = artifact_paths["model_usage_log_root"]
        kwargs["model_usage_log_root"] = str(configured_log_root) if configured_log_root else None

    # Only apply the fail-closed per-order cap in live mode: watchlist/preflight runs
    # never reach broker placement, so a malformed budgets.json must not block the
    # 9:45 preflight from writing its validation artifact (which the 10:30 live gate
    # requires to proceed).
    if mode == MORNING_LIVE_MODE:
        max_order = load_max_per_order_usd(config_dir_path)
        if max_order is not None:
            kwargs["max_per_order_usd"] = max_order
        max_orders_per_day = load_max_orders_per_day(config_dir_path)
        if max_orders_per_day is not None:
            kwargs["max_orders_per_day"] = max_orders_per_day

    # Overlay tickers.json so config-only emergency exclusions take effect without
    # a code deploy (mirrors the afternoon load_strategy_config ticker overlay).
    ticker_cfg = load_tickers(config_dir_path)
    if "tickers" in ticker_cfg:
        kwargs["tickers"] = ticker_cfg["tickers"]
    if "excluded_tickers" in ticker_cfg:
        kwargs["excluded_tickers"] = ticker_cfg["excluded_tickers"]
    kwargs.update(load_per_ticker_sizing(config_dir_path))

    return MorningConfig(**kwargs)


def _coerce_live_execution_enabled(value: Any, *, source_label: str) -> bool:
    """Interpret the master live-placement switch, failing closed on any
    non-boolean JSON type.

    ``bool(value)`` on a JSON string like ``"false"`` or ``"0"`` is truthy
    for any non-empty string, so an operator who tried to disable live
    trading with a quoted value would silently leave placement ENABLED --
    the opposite of what the config says, in the one field that gates real
    broker orders (Codex review: "Reject non-boolean values for the midday
    live switch"). Only Python's actual ``bool`` is accepted; every other
    type fails closed to ``False`` and is logged so the config drift is
    visible in the run's own log output rather than silently taking effect.
    """
    if isinstance(value, bool):
        return value
    _log.warning(
        "%s live_execution_enabled must be a JSON boolean, got %s (%r); disabling live execution",
        source_label,
        type(value).__name__,
        value,
    )
    return False


def load_stream_config(
    config_dir,
    *,
    filename: str,
    config_cls: type,
    daily_cap_key: str,
    weekly_cap_key: str,
    source_label: str,
    allow_unpinned_account: bool = False,
):
    """Load a single-live-mode stream config into ``config_cls``.

    The market-open and intraday streams read config files with the same shape as
    ``config/midday_dca.json``, differing only in filename, budget-ledger key
    names, and target dataclass. This is that shared body, so a new stream does
    not re-copy ~150 lines of overlay logic per module.

    ``load_midday_config`` predates this helper and keeps its own copy; it is live
    code with its own regression tests and is deliberately left alone.

    Graceful-fallback semantics match ``load_morning_config``: a missing or
    unparseable file logs a warning and returns ``config_cls()`` defaults, still
    resolving the fail-closed approved-date gate and the model-usage log root.
    """
    config_dir_path = get_config_dir() if config_dir is None else Path(config_dir)

    cfg_path = config_dir_path / filename
    # model_usage_log_root and approved_test_dates resolve at LOAD time, never
    # from import-time constants: $DCA_CRON_TRACKING_ROOT set between import and
    # config load would otherwise leave model-usage evidence under the old tree.
    # morning_run_state_root deliberately stays unset in the fallback paths so
    # run_dca.py's "did this stream's config actually configure a root"
    # containment check still works; the executor resolves it from the profile's
    # state_root_stream instead.
    base_overrides = {
        "approved_test_dates": _load_approved_dates_fail_closed(config_dir_path),
        "model_usage_log_root": stream_root("model_usage"),
        # Fail closed to shadow mode, not MorningConfig.live_execution_enabled's
        # inherited True default: every stream built on this loader ships with
        # live_execution_enabled=false in its own config (market_open_dca.json,
        # intraday_dca.json -- "SHIPS DISABLED"), so a missing or unreadable
        # config file must never silently promote a shadow-only stream to live
        # broker placement precisely when its configuration cannot be trusted
        # (Codex review: "Keep disabled streams disabled when config loading
        # fails"). A config file that IS readable still overrides this below.
        "live_execution_enabled": False,
    }

    if not cfg_path.exists():
        _log.warning("%s not found at %s; using %s defaults", filename, cfg_path, config_cls.__name__)
        return config_cls(**base_overrides)

    try:
        raw = json.loads(cfg_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        _log.warning("could not parse %s (%s); using %s defaults", cfg_path, exc, config_cls.__name__)
        return config_cls(**base_overrides)

    # A file can be valid JSON but not an object (e.g. "[]", "42", "null") --
    # every overlay below assumes raw.get(...)/raw["..."] on a dict, and a
    # list/str/int/None has no .get(), so this would otherwise crash with
    # AttributeError instead of falling back to shadow-mode defaults, the
    # exact fail-closed behavior this loader documents for every other
    # malformed-file case (Copilot review: "load_stream_config() assumes the
    # parsed JSON is a dict").
    if not isinstance(raw, dict):
        _log.warning(
            "%s did not contain a JSON object (got %s); using %s defaults",
            cfg_path,
            type(raw).__name__,
            config_cls.__name__,
        )
        return config_cls(**base_overrides)

    # Bundled absolute artifact paths follow $DCA_CRON_TRACKING_ROOT instead of
    # overriding it. No-op when the variable is unset.
    raw = rebase_bundled_roots_in(raw)

    kwargs: Dict[str, Any] = dict(base_overrides)

    if "trend_min_bars" in raw:
        kwargs["trend_min_bars"] = int(raw["trend_min_bars"])
    # Master placement switch. Absent means enabled, matching MorningConfig's
    # default, so an existing stream's config gains nothing by omission.
    if "live_execution_enabled" in raw:
        kwargs["live_execution_enabled"] = _coerce_live_execution_enabled(
            raw["live_execution_enabled"], source_label=source_label
        )

    sizing = raw.get("sizing_ladder", {})
    if "base_amount_usd" in sizing:
        kwargs["base_buy_amount_usd"] = float(sizing["base_amount_usd"])
    if "base_amount_below_close_usd" in sizing:
        kwargs["base_buy_amount_below_close_usd"] = float(sizing["base_amount_below_close_usd"])
    if "no_buy_below_pct" in sizing:
        kwargs["minimum_drop_vs_previous_close"] = float(sizing["no_buy_below_pct"]) / 100.0
    if "tiers" in sizing:
        kwargs["sizing_tiers"] = tuple(
            (float(t["min_drop_pct"]) / 100.0, float(t["multiplier"]))
            for t in sizing["tiers"]
        )

    budget = raw.get("budget_ledger", {})
    if daily_cap_key in budget:
        kwargs["morning_daily_cap_usd"] = float(budget[daily_cap_key])
    if weekly_cap_key in budget:
        kwargs["morning_weekly_cap_usd"] = float(budget[weekly_cap_key])

    live_task = raw.get("live_task", {})
    if "broker_account" in live_task:
        # The account number lives in the environment, never in git. These are
        # live-only streams, so an unresolvable account is left explicitly None
        # (not merely omitted -- omitting it would fall back to the import-time
        # module constant, turning a pinning failure into a live run against an
        # unintended account) so the executor reaches account_pinning_failure()
        # and writes durable blocked evidence.
        source = f"{source_label} live_task.broker_account"
        try:
            kwargs["target_account_number"] = resolve_broker_account(
                live_task["broker_account"], source=source
            )
        except (MissingConfigEnvError, InvalidAccountNumberError):
            if not allow_unpinned_account:
                raise
            kwargs["target_account_number"] = None
            _log.warning(
                "%s could not be resolved to a usable account; the live run will "
                "fail closed and record account_pinning_failed",
                source,
            )

    artifact_paths = raw.get("artifact_paths", {})
    if "live_run_state_root" in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths["live_run_state_root"]

    # An explicit [] is preserved as () so it fully disables the guard / cap
    # seeding rather than falling through to the default_factory.
    roots = artifact_paths.get("prior_allocation_roots")
    if isinstance(roots, list):
        kwargs["prior_allocation_roots"] = tuple(str(r) for r in roots)
    cap_roots = artifact_paths.get("cap_seed_roots")
    if isinstance(cap_roots, list):
        # See load_morning_config's matching block for why this appends
        # rather than just converting: every production stream config here
        # (market_open_dca.json, intraday_dca.json) supplies an explicit
        # cap_seed_roots list too (Codex review, PR #656 follow-up on
        # discussion_r3839652545: "Add override roots to configured cap
        # seeds").
        kwargs["cap_seed_roots"] = _append_dated_manual_override_cap_seed_roots(
            tuple(str(r) for r in cap_roots)
        )

    if "model_usage_log_root" in artifact_paths:
        configured_log_root = artifact_paths["model_usage_log_root"]
        kwargs["model_usage_log_root"] = str(configured_log_root) if configured_log_root else None

    max_order = load_max_per_order_usd(config_dir_path)
    if max_order is not None:
        kwargs["max_per_order_usd"] = max_order
    max_orders_per_day = load_max_orders_per_day(config_dir_path)
    if max_orders_per_day is not None:
        kwargs["max_orders_per_day"] = max_orders_per_day

    # Overlay tickers.json so config-only emergency exclusions take effect
    # without a code deploy.
    ticker_cfg = load_tickers(config_dir_path)
    if "tickers" in ticker_cfg:
        kwargs["tickers"] = ticker_cfg["tickers"]
    if "excluded_tickers" in ticker_cfg:
        kwargs["excluded_tickers"] = ticker_cfg["excluded_tickers"]
    kwargs.update(load_per_ticker_sizing(config_dir_path))

    return config_cls(**kwargs)


def load_same_day_preflight(
    preflight_root: str,
    trading_day: date,
    *,
    bounded_recovery: bool = True,
    forbidden_roots: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    """Load the same-day 9:45 pre-flight artifact for the 10:30 live gate.

    Returns a dict ``{"run_id", "eligible_tickers": set[str], "run_state_path",
    "recovered_via_bounded_lookup"}`` when a same-day pre-flight
    ``run_state.json`` is found under ``preflight_root/YYYYMMDD_0945ET/`` (or
    via ``latest_preflight.json`` when it validates as same-day scheduled
    evidence per ``scripts/same_day_pointer.py`` -- same stream, inside the
    preflight root, non-manual, and both the pointer and its target dated
    today -- or via the bounded recovery scan below), else ``None``. The live executor
    uses the returned ``eligible_tickers`` to restrict which tickers it
    re-evaluates; a ``None`` result means "no pre-flight found" and the caller
    degrades to evaluating the full universe with a warning rather than
    blocking all buys.

    Regardless of which of the three lookup paths resolves ``state_path``, the
    loaded state itself must independently prove same-day (``trading_day``
    matches), scheduled non-manual (``derive_valid_scheduled_evidence``), and
    morning-stream (``run_classification.stream``, when present) evidence
    before its eligibility data is trusted -- a prior-day, manual, or
    wrong-stream artifact merely existing at the expected path (the direct
    join, a scan hit, or a pointer target) is never enough on its own.

    An artifact that explicitly declares ``overall_ok=False`` (a global/
    account/buying-power/auth/schema-write failure per the machine-complete
    contract) is treated the same as "no pre-flight found" -- None is
    returned -- rather than trusting whatever eligibility data happens to be
    present on a preflight run that flagged itself invalid.

    Eligibility is derived primarily from ``per_ticker_eligibility`` (the
    schema-required v2 field: every active-universe symbol with a ``symbol``
    key and an ``eligible_to_execute`` flag), falling back to the legacy
    ``eligible_to_execute_tickers`` convenience field only when
    ``per_ticker_eligibility`` is absent (an older artifact shape).

    ``bounded_recovery`` implements
    ``live_task.preflight_artifact_resolution.bounded_recovery_lookup``: when
    the primary path and the ``latest_preflight.json`` pointer both miss,
    perform exactly ONE additional lookup -- a directory scan of
    ``preflight_root`` for today's ``YYYYMMDD_0945ET`` directory -- before
    reporting "not found". This is a second, independent lookup path (scan vs.
    direct path join) so a regression in the primary path construction does
    not silently fail closed without ever finding an artifact that is actually
    on disk.

    Before the selected artifact is read, its canonical path is checked
    against ``forbidden_roots``. This final-path check prevents a symlink
    below an otherwise allowed pre-flight root from redirecting the live gate
    into a forbidden live-output or cron-ID tree.
    """
    root = Path(preflight_root)
    expected_dir_name = f"{trading_day.strftime('%Y%m%d')}_0945ET"
    run_dir = root / expected_dir_name
    state_path = run_dir / "run_state.json"
    recovered_via_bounded_lookup = False
    if not state_path.exists():
        pointer_state_path: Optional[Path] = None
        pointer = root / "latest_preflight.json"
        if pointer.exists():
            result = validate_pointer(
                pointer,
                expected_stream=MORNING_STREAM,
                expected_root=root,
                today=trading_day.isoformat(),
            )
            if result.is_valid and result.run_state_path:
                candidate = Path(result.run_state_path)
                if candidate.exists():
                    pointer_state_path = candidate
        if pointer_state_path is not None:
            state_path = pointer_state_path
        elif bounded_recovery:
            recovered_path = None
            try:
                if root.is_dir():
                    for entry in sorted(root.iterdir()):
                        if entry.name == expected_dir_name and entry.is_dir():
                            candidate = entry / "run_state.json"
                            if candidate.exists():
                                recovered_path = candidate
                            break
            except OSError:
                return None
            if recovered_path is None:
                return None
            state_path = recovered_path
            recovered_via_bounded_lookup = True
        else:
            return None
    try:
        resolved_root = root.resolve()
        resolved_state_path = state_path.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved_root not in resolved_state_path.parents:
        return None
    if _forbidden_preflight_root_reason(str(resolved_state_path), forbidden_roots) is not None:
        return None
    try:
        state = json.loads(resolved_state_path.read_bytes())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(state, dict):
        # Valid JSON whose root is a list, scalar, or null (e.g. after a
        # partial recovery) has no .get() method; treat it the same as "no
        # pre-flight found" instead of raising, so the 10:30 live gate still
        # degrades to the full universe with a warning rather than crashing
        # (Codex review: "Treat non-object preflight JSON as unavailable").
        return None
    if state.get("overall_ok") is False:
        return None
    # The loaded state must independently prove it is same-day, scheduled,
    # non-manual, morning-stream evidence -- regardless of which of the three
    # lookup paths above found it (direct path, validated pointer, or bounded
    # recovery scan) -- so a prior-day, manual, or wrong-stream artifact left
    # or copied into today's expected directory can never drive the 10:30
    # live gate just because a file happens to exist at that path.
    if str(state.get("trading_day", "")).strip() != trading_day.isoformat():
        return None
    if not derive_valid_scheduled_evidence(state):
        return None
    target_rc = state.get("run_classification")
    target_stream = target_rc.get("stream") if isinstance(target_rc, dict) else None
    if target_stream is not None and target_stream != MORNING_STREAM:
        return None
    # bb4f6ef1 now fires BOTH the preflight and watchlist sub-invocations at
    # the same 9:45 ET slot, sharing task ID, scheduled_for_et, and
    # stream='morning' -- exactly the fields checked above -- so those checks
    # alone can no longer tell a preflight artifact from a watchlist one. A
    # watchlist state copied or misrouted into the preflight directory would
    # otherwise pass every check above and its rows (watchlist artifacts
    # never populate per_ticker_eligibility) would yield an empty eligibility
    # set, causing the 10:30 live gate to shut out the entire universe.
    # run_type and state_schema_version are the two fields that DO
    # distinguish the pair -- always stamped by run_classification() (see
    # MORNING_PROFILES) -- so requiring both here cannot reject a
    # legitimately-completed preflight run -- codex review
    # discussion_r3659738862.
    target_run_type = target_rc.get("run_type") if isinstance(target_rc, dict) else None
    if target_run_type != MORNING_PREFLIGHT_RUN_TYPE:
        return None
    target_schema_version = state.get("state_schema_version")
    if target_schema_version is None and isinstance(target_rc, dict):
        target_schema_version = target_rc.get("state_schema_version")
    if target_schema_version != MORNING_PREFLIGHT_SCHEMA_VERSION:
        return None
    per_ticker = state.get("per_ticker_eligibility")
    if isinstance(per_ticker, list) and per_ticker:
        eligible = [
            row.get("symbol")
            for row in per_ticker
            if isinstance(row, dict) and row.get("eligible_to_execute") is True
        ]
    else:
        eligible = state.get("eligible_to_execute_tickers")
        if eligible is None:
            eligible = [
                row["ticker"]
                for row in state.get("signal_rows", [])
                if row.get("eligible_to_execute") is True
            ]
    return {
        "run_id": state.get("run_id"),
        "eligible_tickers": set(eligible),
        "run_state_path": str(state_path),
        "recovered_via_bounded_lookup": recovered_via_bounded_lookup,
    }


# Tag key used to round-trip a BARE ``datetime.date`` (as opposed to a full
# ``datetime``) through JSON persistence without collapsing it to a plain
# "YYYY-MM-DD" string. A yfinance/pandas-backed Finance connector can return a
# bar whose timestamp is a bare date; ``dca_live_adapters._coerce_bar()``
# deliberately branches on ``isinstance(ts_value, date)`` to anchor it at
# 16:00 ET (the close), a DIFFERENT instant than the same string parsed by
# ``_parse_rfc3339`` (midnight ET). Losing the date/datetime distinction on
# the persist-then-replay round trip silently shifts that bar's timestamp by
# 16 hours, which can alter freshness/window decisions (Codex review, PR #538
# discussion_r3821694980: "Preserve bare-date bar timestamps across
# persistence").
_BARE_DATE_TAG = "__bare_date__"


def _raw_json_safe(value: Any) -> Any:
    """Recursively convert a connector response to a JSON-serializable value.

    Handles the shapes returned by :class:`near_close_dca.MarketDataProvider`/
    :class:`near_close_dca.BrokerAdapter` implementations: ``Bar`` dataclasses,
    raw datetimes (e.g. inside a ``get_quote``/``get_bid_ask`` tuple), and
    nested lists/dicts of those. Anything already JSON-safe passes through.

    A bare ``datetime.date`` (not a ``datetime``) is tagged rather than
    flattened to a plain isoformat string -- see :data:`_BARE_DATE_TAG` --
    so :func:`_restore_raw_datetimes` can reconstruct it as a ``date`` object
    and downstream bar coercion anchors it at the correct time of day.

    A non-string iterable that is not already a list/tuple/dict (e.g. a
    generator, which a Finance ``bars_fn`` may legitimately return per its
    documented contract) is materialized into a list before recursing --
    otherwise it is neither JSON-serializable nor iterable a second time, and
    ``json.dumps`` inside ``save_raw_input`` raises, failing the whole
    persist() call and blocking the scheduled run for market data that was
    actually available (Codex review, PR #538 discussion_r3821694971:
    "Materialize iterable Finance bar payloads").
    """
    if isinstance(value, Bar):
        return {
            "timestamp": value.timestamp.isoformat(),
            "open": value.open,
            "high": value.high,
            "low": value.low,
            "close": value.close,
            "volume": value.volume,
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date,)):
        return {_BARE_DATE_TAG: value.isoformat()}
    if isinstance(value, dict):
        return {str(k): _raw_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_raw_json_safe(v) for v in value]
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Iterable):
        # Generators and other one-shot iterables must be drained into a
        # concrete list HERE -- both because json.dumps cannot serialize them
        # directly, and because a value already consumed once cannot be
        # iterated again by a later caller.
        return [_raw_json_safe(v) for v in value]
    if hasattr(value, "item"):
        # Duck-typed detection of a NumPy/pandas scalar (e.g. numpy.int64 for
        # a Finance connector's vendor-native volume field, numpy.bool_ for a
        # flag). ``.item()`` is the standard NumPy scalar accessor that
        # returns the equivalent plain Python int/float/bool. This repo is
        # standard-library-only at runtime (see CLAUDE.md), so this is
        # deliberately duck-typed rather than an ``isinstance`` check against
        # an imported numpy type -- a narrow try/except keeps any other
        # well-behaved object that happens to expose an unrelated ``.item()``
        # method from raising here instead of just passing through
        # unconverted. Left unconverted, ``json.dumps()`` raises ``TypeError``
        # on the numpy scalar, ``persist()`` sets ``recorder.error``, and the
        # scheduled run blocks even though
        # ``FinanceDataProvider._coerce_bar()`` could have consumed the
        # original numeric value fine (Codex review, PR #538
        # discussion_r3837489856: "Serialize vendor scalar values before
        # archiving Finance bars").
        try:
            converted = value.item()
        except Exception:  # noqa: BLE001 - duck-typed accessor, arbitrary object
            return value
        if converted is not value:
            return _raw_json_safe(converted)
    return value


def _restore_raw_datetimes(value: Any, *, restore_datetime_strings: bool = True) -> Any:
    """Restore datetime/date values after a persisted JSON payload is read back.

    ``restore_datetime_strings`` gates ONLY the "ordinary RFC3339 string ->
    ``datetime`` object" conversion; the ``__bare_date__`` tag is always
    restored to a ``date`` regardless of this flag.

    This split exists because two very different kinds of persisted payload
    flow through this function:

    * Already-parsed adapter output (``Bar``/quote/bid-ask values built by the
      provider's OWN parsing before ``_raw_json_safe`` serialized them) --
      callers here (``_bar_from_raw``, ``get_quote``/``get_bid_ask``) need a
      real ``datetime`` object back, so they call with the default
      ``restore_datetime_strings=True``.
    * Untouched raw connector responses intercepted by
      ``_PersistingMarketDataProvider._wrap_raw_connectors`` (the
      ``_historicals_fn``/``_quotes_fn``/``_bars_fn``/``_previous_close_fn``
      callables) -- these are handed BACK to the provider's own parsing
      (``dca_live_adapters.RobinhoodDataProvider._parse_bars`` ->
      ``_parse_rfc3339``, which only accepts strings). Eagerly converting a
      plain RFC3339 string to a ``datetime`` object here made every
      Robinhood-path bar unparseable -- ``_parse_rfc3339`` silently returns
      ``None`` for a non-string input, discarding the bar without setting
      ``recorder.error``, which starved the Robinhood signal path of any
      usable data (Codex review, PR #538 discussion_r3837449172: "Preserve
      RFC3339 strings for Robinhood parsing"). Callers on this path pass
      ``restore_datetime_strings=False`` so ONLY the explicitly-tagged
      bare-date marker is restored, and every other string -- RFC3339 or not
      -- is handed back exactly as persisted for the adapter to parse itself.
    """
    if isinstance(value, list):
        return [_restore_raw_datetimes(item, restore_datetime_strings=restore_datetime_strings) for item in value]
    if isinstance(value, dict):
        if set(value.keys()) == {_BARE_DATE_TAG}:
            try:
                return date.fromisoformat(value[_BARE_DATE_TAG])
            except (ValueError, TypeError):
                return value
        return {
            key: _restore_raw_datetimes(item, restore_datetime_strings=restore_datetime_strings)
            for key, item in value.items()
        }
    if restore_datetime_strings and isinstance(value, str) and "T" in value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return value


def _bar_from_raw(value: Any) -> Bar:
    """Build a strategy bar exclusively from its persisted representation."""
    if not isinstance(value, dict):
        raise ValueError("persisted bar is not an object")
    timestamp = _restore_raw_datetimes(value.get("timestamp"))
    if not isinstance(timestamp, datetime):
        raise ValueError("persisted bar has no valid timestamp")
    return Bar(
        timestamp=timestamp,
        open=float(value["open"]),
        high=float(value["high"]),
        low=float(value["low"]),
        close=float(value["close"]),
        volume=float(value["volume"]),
    )


def _raw_envelope_reports_failure(envelope: Dict[str, Any]) -> bool:
    """Mirror ``dca_live_adapters._is_error_response`` for the raw-persist round trip.

    A connector wrapper can surface an outage as data (``{"isError": true}`` /
    ``{"error": ...}`` / ``{"success": false}`` / ``{"ok": false}``) rather than raising.
    That failure flag must not be discarded by ``_RawInputRecorder.persist()``'s envelope
    unwrapping -- downstream broker safeguards check for it on the returned payload.

    The ``isError`` check MUST come first and independently of the other markers: an
    mcp-bridge error envelope like ``{"isError": true, "data": {"orders": []}}`` carries
    no truthy ``error``/``success``/``ok`` field of its own, so without this check
    ``persist()`` unwrapped it straight to the inner ``data`` and replayed it to
    ``RobinhoodBroker`` as a valid *empty* order history -- fooling a duplicate-order
    guard into thinking the broker lookup succeeded when it actually failed (Codex
    review, PR #538 discussion_r3837449174: "Preserve isError envelopes during
    replay"). This mirrors ``dca_live_adapters._is_error_response``'s own ``isError``
    handling, which is checked first there for the same reason.
    """
    if envelope.get("isError") is True:
        return True
    if envelope.get("error"):
        return True
    return any(envelope.get(flag) is False for flag in ("success", "ok"))


class _RawInputRecorder:
    """Persists connector responses used by a scheduled live run under ``raw/``.

    Implements ``live_schedule_hardening_2026_07_20``'s raw-input persistence
    contract (config/morning_dca.json) for the 10:30 morning live task (and,
    via inheritance, the 12:30 midday live task): before compute uses a
    connector response, that response is written to ``raw/`` so a downstream
    consumer never has to trust an in-memory value that was never durably
    recorded. Morning fetches data lazily per ticker (unlike the afternoon
    stream's upfront batch fetch), so persistence happens per-call rather than
    as one preceding batch step -- functionally equivalent (nothing is used
    for signal computation or order placement before its own response is on
    disk), just interleaved instead of front-loaded.

    A write failure (disk full, unwritable path, non-serializable payload) is
    never raised into the caller: it is recorded on :attr:`error` (first
    failure wins) so ``run()`` can fail the scheduled run closed at a safe
    checkpoint, rather than surfacing as an unrelated-looking crash deep
    inside signal evaluation or order placement.
    """

    def __init__(self, raw_dir: Path, trading_day_iso: str):
        self.raw_dir = raw_dir
        self.trading_day_iso = trading_day_iso
        self.saved_filenames: List[str] = []
        self.error: Optional[str] = None
        # Namespace by retry attempt (Codex review, PR #538 discussion_r3809442201:
        # "Namespace persisted raw inputs by retry attempt"): a scheduled retry
        # constructs a brand-new recorder for the SAME dated-slot raw_dir, and a
        # _seq that always restarted at 0 produced deterministic filenames
        # (prior_close_AAPL_1.json, ...) that a later attempt would silently
        # overwrite -- destroying the raw evidence an earlier attempt's already-
        # submitted order was justified by. Seeding _seq from the highest
        # trailing "_<N>.json" sequence number already on disk means a new
        # attempt's filenames always sort after every prior attempt's, so an
        # existing file is never reused as a later attempt's own filename.
        self._seq = self._max_existing_sequence()

    def _max_existing_sequence(self) -> int:
        """Highest trailing ``_<N>.json`` sequence number already under raw_dir.

        Returns 0 when the directory does not exist or has no matching files,
        so a first attempt still starts numbering at 1 as before.
        """
        try:
            existing = list(self.raw_dir.iterdir())
        except OSError:
            return 0
        highest = 0
        for entry in existing:
            match = re.search(r"_(\d+)\.json$", entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest

    def record_error(self, kind: str, key: str, exc: Exception) -> None:
        """Record a connector/normalization failure as a raw-input failure."""
        if self.error is None:
            self.error = f"failed to fetch or normalize {kind} for {key}: {exc}"

    def persist(self, kind: str, key: str, payload: Any) -> Any:
        """Persist *payload* and return the representation read back from disk."""
        safe_key = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in key)
        # Two overlapping scheduled retries for the SAME dated slot construct
        # their OWN recorder instances -- each seeding its own in-memory
        # `_seq` from `_max_existing_sequence()` at __init__ time. If both
        # constructors (or both persist() calls) run before either has
        # actually written a file, they read the same on-disk maximum and
        # allocate the SAME next filename, and save_raw_input()'s
        # atomic_write_text() then has the later writer silently overwrite
        # the earlier attempt's raw evidence -- defeating the retry-isolation
        # guarantee this namespacing exists for (Codex review, PR #538:
        # "Allocate raw filenames atomically across attempts"). The sequence
        # number is now re-derived from the CURRENT on-disk state and the
        # file written to disk in the SAME cross-process critical section, so
        # a second recorder can only ever see this file already on disk (and
        # allocate past it) or not yet exist (and race for the lock instead
        # of the filename).
        filename: Optional[str] = None
        try:
            with cross_process_lock(self.raw_dir / ".raw_seq.lock"):
                self._seq = max(self._seq, self._max_existing_sequence()) + 1
                filename = f"{kind}_{safe_key}_{self._seq}.json"
                path = save_raw_input(
                    self.trading_day_iso,
                    filename,
                    _raw_json_safe(payload),
                    raw_dir=self.raw_dir,
                )
            persisted = json.loads(Path(path).read_text(encoding="utf-8"))
            # save_raw_input() may wrap payloads in one or more {"data": ...} envelopes.
            # Unwrap the normalize_connector_payload envelope (normalized true/false) and
            # any nested carrier {"data": {...}} layers so computation sees the archived shape.
            # But an envelope that reports failure (e.g. {"success": false, "data": {"order":
            # {"id": "..."}}}) must keep that outer flag: RobinhoodBroker._response_failure()
            # inspects the round-tripped payload for it, and discarding it in favor of the
            # inner "data" would let a failed placement/lookup with an inner id or empty
            # orders list read back as success. Stop unwrapping at the first envelope that
            # reports failure instead.
            if (
                isinstance(persisted, dict)
                and "results" not in persisted
                and "data" in persisted
                and "normalized" in persisted
                and not _raw_envelope_reports_failure(persisted)
            ):
                persisted = persisted["data"]
            seen_ids: set[int] = set()
            while (
                isinstance(persisted, dict)
                and "results" not in persisted
                and isinstance(persisted.get("data"), dict)
                and not _raw_envelope_reports_failure(persisted)
            ):
                if id(persisted) in seen_ids:
                    break
                seen_ids.add(id(persisted))
                persisted = persisted["data"]
        except (OSError, ValueError, TypeError) as exc:
            # filename is still None if the cross-process lock itself timed
            # out (TimeoutError is an OSError subclass) before a sequence
            # number was ever allocated -- name the connector call instead of
            # a filename that was never chosen.
            if self.error is None:
                self.error = f"failed to persist {filename or f'{kind}_{safe_key}'}: {exc}"
            return None
        self.saved_filenames.append(filename)
        return persisted


class _PersistingMarketDataProvider:
    """``MarketDataProvider`` decorator that persists every response it returns.

    Wraps ``get_previous_close_bar``/``get_intraday_bars``/``get_quote``/
    ``get_bid_ask`` -- the price-data calls ``evaluate_signal`` makes -- and
    forwards everything else (including the ``source`` attribute) to the
    wrapped provider unchanged.
    """

    def __init__(self, inner: Any, recorder: _RawInputRecorder):
        self._inner = inner
        self._recorder = recorder
        self._wrapped_connector_attributes: set[str] = set()
        self._wrap_raw_connectors()

    def _connector_fully_wrapped(self, *attributes: str) -> bool:
        """True iff every one of ``attributes`` reachable from a public method call
        was itself intercepted for raw persistence.

        A single global "something got wrapped" flag wrongly skipped fallback
        persistence for EVERY public method once ANY one connector was
        wrapped -- a provider exposing only e.g. ``_bars_fn`` while serving
        ``get_quote()``/``get_previous_close_bar()`` from an unwrapped source
        (or falling back to a second, unwrapped connector attribute) could
        then return a live response with no raw artifact ever written and no
        ``recorder.error`` raised (Codex review, PR #538
        discussion_r3837662132: "Track wrapped connectors per market-data
        method"). An attribute that is absent/non-callable on this provider
        is irrelevant to coverage -- that code path can never be reached
        live. But if NONE of ``attributes`` are callable on this provider at
        all (a plain, non-connector-based test double, or a real adapter
        this class has no recognized connector attribute for), there is
        nothing this method's own persistence fallback would be redundant
        with, so it must still run -- vacuously reporting "fully wrapped"
        for zero candidates would silently drop the only raw-persistence
        path a connector-less provider has.
        """
        callable_attributes = [attribute for attribute in attributes if callable(getattr(self._inner, attribute, None))]
        if not callable_attributes:
            return False
        return all(attribute in self._wrapped_connector_attributes for attribute in callable_attributes)

    def _wrap_raw_connectors(self) -> bool:
        """Intercept Robinhood and Finance connector callables before adapter parsing.

        Wraps a private *copy* of ``self._inner`` rather than mutating the caller-
        supplied adapter in place: the caller may reuse that same
        data-provider instance for a later manual or
        watchlist run, and a wrapper left bound to this run's (short-lived)
        ``_RawInputRecorder`` would then archive that later run's responses into this
        run's ``raw/`` directory -- or return ``None`` for a perfectly valid response
        if that directory has since become unwritable.
        """
        wrapped = False
        copied = False
        for attribute, kind in (
            ("_historicals_fn", "historicals"),
            ("_quotes_fn", "quotes"),
            ("_bars_fn", "intraday_bars"),
            ("_previous_close_fn", "prior_close"),
        ):
            connector = getattr(self._inner, attribute, None)
            if not callable(connector) or getattr(connector, "_raw_recorder", None) is self._recorder:
                continue
            connector = getattr(connector, "_raw_connector_original", connector)
            if not copied:
                self._inner = copy.copy(self._inner)
                copied = True
                # copy.copy() is a SHALLOW copy: it duplicates the instance's
                # __dict__ but the values inside it -- including a populated
                # RobinhoodDataProvider._signal_quote_cache from an earlier
                # query at this same `as_of` -- are the same objects the
                # caller's original instance still references. Left as-is,
                # get_quote()/get_previous_close_bar() below would serve that
                # stale cached quote without ever calling the newly wrapped
                # _quotes_fn, and because "_quotes_fn" is already recorded in
                # _wrapped_connector_attributes for this copy, the
                # "not fully wrapped" fallback-persistence branch in those
                # methods is also skipped -- so the wrapped connector is
                # never invoked, no raw artifact is ever written, and no
                # recorder.error is raised (Codex review, PR #538
                # discussion_r3837489860: "Clear copied provider caches
                # before wrapping connectors"). Reset defensively via
                # duck-typing (hasattr/setattr) rather than assuming every
                # wrapped provider carries these attributes --
                # FinanceDataProvider and other adapters do not.
                if hasattr(self._inner, "_signal_quote_cache"):
                    self._inner._signal_quote_cache = {}
                if hasattr(self._inner, "_signal_quote_as_of"):
                    self._inner._signal_quote_as_of = None

            def persisted_connector(*args: Any, _connector=connector, _kind=kind, **kwargs: Any) -> Any:
                symbols = kwargs.get("symbols") or (args[0] if args else ["unknown"])
                # A one-shot Iterable (a generator, in particular) must be
                # materialized into a list BEFORE anything iterates it: the
                # hyphenated-key join below exhausts a generator on first
                # pass, and _connector(*args, **kwargs) below reuses this same
                # `symbols` object -- an exhausted generator there silently
                # fetches nothing while persistence still looks successful
                # (Codex review, PR #538: "Do not consume one-shot symbol
                # iterables before fetching"). Replacing it back into
                # args/kwargs ensures the connector call sees the same
                # materialized symbols the key was built from.
                if isinstance(symbols, Iterable) and not isinstance(symbols, (str, bytes)):
                    symbols = list(symbols)
                    if "symbols" in kwargs:
                        kwargs["symbols"] = symbols
                    elif args:
                        args = (symbols,) + args[1:]
                # A set (or any other non-str/bytes Iterable, e.g. a generator)
                # was falling through to str(symbols) -- "{'AAPL', 'MSFT'}" --
                # instead of the intended hyphenated key, since the check only
                # recognized list/tuple (Antigravity review on PR #538: keep
                # symbol-collection keys hyphenated regardless of container
                # type).
                key = (
                    "-".join(str(symbol) for symbol in symbols)
                    if isinstance(symbols, Iterable) and not isinstance(symbols, (str, bytes))
                    else str(symbols)
                )
                try:
                    payload = _connector(*args, **kwargs)
                    persisted = self._recorder.persist(_kind, key, payload)
                    if isinstance(persisted, dict) and _raw_envelope_reports_failure(persisted):
                        # A connector-level outage can be reported as data (e.g.
                        # {"success": false, ...}) rather than raised.
                        # RobinhoodDataProvider does not inspect this itself (unlike
                        # RobinhoodBroker._response_failure()), so historicals silently
                        # become empty data and a failed bid/ask call becomes None
                        # unless this is recorded as a raw-input failure here (Codex
                        # review: "Treat failed market-data envelopes as raw-input
                        # errors").
                        self._recorder.record_error(
                            _kind, key, RuntimeError(f"connector reported failure: {persisted}")
                        )
                    # Restore datetimes/dates from their persisted JSON representation
                    # before handing the round-tripped payload back to the provider's
                    # OWN bar coercion (e.g. dca_live_adapters._coerce_bar), which
                    # branches on isinstance(ts_value, date) vs isinstance(ts_value,
                    # datetime) to anchor a bare-date bar at 16:00 ET rather than
                    # midnight. Without this, persist()'s plain json.loads() read-back
                    # hands the coercion function a dict (or bare ISO string) it
                    # cannot recognize as either type, either silently dropping the
                    # bar or misplacing its timestamp by 16 hours (Codex review, PR
                    # #538 discussion_r3821694980).
                    #
                    # BUT this is a RAW, un-parsed connector response -- the provider's
                    # own parsing (e.g. RobinhoodDataProvider._parse_bars ->
                    # _parse_rfc3339) still needs to receive plain RFC3339 strings, not
                    # datetime objects, so restore_datetime_strings=False here: only the
                    # tagged bare-date marker is restored, ordinary timestamp strings are
                    # left untouched (Codex review, PR #538 discussion_r3837449172:
                    # "Preserve RFC3339 strings for Robinhood parsing").
                    return (
                        _restore_raw_datetimes(persisted, restore_datetime_strings=False)
                        if persisted is not None
                        else None
                    )
                except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
                    self._recorder.record_error(_kind, key, exc)
                    raise

            persisted_connector._raw_recorder = self._recorder  # type: ignore[attr-defined]
            persisted_connector._raw_connector_original = connector  # type: ignore[attr-defined]
            setattr(self._inner, attribute, persisted_connector)
            wrapped = True
            self._wrapped_connector_attributes.add(attribute)
        return wrapped

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        try:
            bar = self._inner.get_previous_close_bar(ticker, as_of)
            # RobinhoodDataProvider tries _quotes_fn first, falling back to
            # _historicals_fn; FinanceDataProvider uses _previous_close_fn.
            # ALL of a provider's callable candidates must be wrapped before
            # skipping this fallback -- if only some are, the path this call
            # actually took might not have been.
            if self._connector_fully_wrapped("_quotes_fn", "_historicals_fn", "_previous_close_fn"):
                return bar
            persisted = self._recorder.persist("prior_close", ticker, bar)
            return _bar_from_raw(persisted) if persisted is not None else None
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("prior_close", ticker, exc)
            raise

    def get_intraday_bars(
        self, *, ticker: str, start: datetime, end: datetime, interval_minutes: int
    ) -> Sequence[Bar]:
        try:
            bars = self._inner.get_intraday_bars(
                ticker=ticker, start=start, end=end, interval_minutes=interval_minutes
            )
            # RobinhoodDataProvider uses _historicals_fn; FinanceDataProvider
            # uses _bars_fn -- only the one this provider type actually has
            # needs to be wrapped, via the callable-only filter in
            # _connector_fully_wrapped().
            if self._connector_fully_wrapped("_historicals_fn", "_bars_fn"):
                return bars
            persisted = self._recorder.persist("intraday_bars", ticker, list(bars))
            return [_bar_from_raw(item) for item in persisted] if persisted is not None else []
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("intraday_bars", ticker, exc)
            raise

    def get_quote(self, ticker: str, as_of: datetime) -> Any:
        get_quote = getattr(self._inner, "get_quote", None)
        if not callable(get_quote):
            return None
        try:
            quote = get_quote(ticker, as_of)
            if self._connector_fully_wrapped("_quotes_fn"):
                return quote
            persisted = self._recorder.persist("quote", ticker, quote)
            return _restore_raw_datetimes(persisted)
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("quote", ticker, exc)
            raise

    def get_bid_ask(self, ticker: str, as_of: datetime) -> Any:
        get_bid_ask = getattr(self._inner, "get_bid_ask", None)
        if not callable(get_bid_ask):
            return None
        try:
            result = get_bid_ask(ticker, as_of)
            if self._connector_fully_wrapped("_quotes_fn"):
                return result
            persisted = self._recorder.persist("bid_ask", ticker, result)
            return _restore_raw_datetimes(persisted)
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("bid_ask", ticker, exc)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _BuyingPowerLookupError(RuntimeError):
    """Signal a broker buying-power lookup failure to the run orchestrator."""


class _PersistingBrokerAdapter:
    """``BrokerAdapter`` decorator that persists the read-only queries it serves.

    Wraps ``account_number``/``available_buying_power``/
    ``is_tradable_for_fractional_buy``/``same_day_strategy_order_exists`` --
    the broker-side inputs a live run consults before placing an order -- so
    portfolio/buying-power/tradability/same-day-order-existence responses are
    captured under ``raw/``. For adapters exposing injected connector callables,
    all connector results (including order actions and fill evidence) are
    intercepted before the adapter selects or converts values from them.
    """

    def __init__(self, inner: BrokerAdapter, recorder: _RawInputRecorder):
        self._inner = inner
        self._recorder = recorder
        self._account_persisted = False
        self._buying_power_persisted = False
        self._wrapped_connector_attributes = self._wrap_raw_connectors()

    def _wrap_raw_connectors(self) -> set[str]:
        """Persist injected broker connector responses before broker parsing.

        Wraps a private *copy* of ``self._inner`` rather than mutating the caller-
        supplied adapter in place: see ``_PersistingMarketDataProvider._wrap_raw_connectors``
        for why a wrapper bound to this run's short-lived ``_RawInputRecorder`` must
        never leak onto an adapter instance the caller may reuse for a later run.
        """
        kinds = {
            "_buying_power_fn": "buying_power",
            "_orders_fn": "orders_since_midnight",
            "_tradability_fn": "tradability",
            "_review_fn": "review",
            "_place_fn": "place",
            "_order_fill_fn": "order_fill",
        }
        wrapped: set[str] = set()
        copied = False
        for attribute, kind in kinds.items():
            connector = getattr(self._inner, attribute, None)
            if not callable(connector) or getattr(connector, "_raw_recorder", None) is self._recorder:
                continue
            connector = getattr(connector, "_raw_connector_original", connector)
            if not copied:
                self._inner = copy.copy(self._inner)
                # copy.copy() is shallow: RobinhoodBroker._raw_placement_responses
                # (the mutable drain buffer each place_market_buy call appends
                # to) would otherwise stay the SAME list object shared with
                # whatever instance the caller reuses across runs. Morning/
                # midday never drain it via get_raw_placement_responses(), so
                # without an independent buffer here, responses from every
                # earlier run through this same broker instance accumulate and
                # a later get_raw_placement_responses() call on the ORIGINAL
                # instance returns stale orders from prior slots (Codex review:
                # "Isolate the copied broker's placement-response buffer"),
                # mirroring the quote-cache reset above for the market-data
                # provider.
                if hasattr(self._inner, "_raw_placement_responses"):
                    self._inner._raw_placement_responses = []
                copied = True

            def persisted_connector(*args: Any, _connector=connector, _kind=kind, **kwargs: Any) -> Any:
                key = kwargs.get("symbol") or kwargs.get("order_id") or "account"
                try:
                    payload = _connector(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
                    # "place" raising (e.g. an ambiguous timeout after the
                    # broker may have already accepted the order) is still
                    # recorded onto raw_recorder.error here -- surfaced for
                    # operator visibility in artifacts["raw_persistence_error"]
                    # -- but no longer risks discarding the ticker-specific
                    # placement evidence _place_broker_orders' own try/except
                    # already recorded as a structured "failed" broker outcome:
                    # the any_placement_outcome check in run() (Codex review,
                    # PR #538 discussion_r3837630723: "Preserve broker failures
                    # when connector calls raise") now falls through to the
                    # normal record/build/write path for ANY recorded
                    # placement outcome (submitted OR failed), not just
                    # "submitted", so this no longer misroutes into an
                    # empty-broker_outcomes _blocked_live_result() call.
                    self._recorder.record_error(_kind, str(key), exc)
                    raise
                persisted = self._recorder.persist(_kind, str(key), payload)
                if (
                    _kind in ("buying_power", "tradability")
                    and isinstance(persisted, dict)
                    and _raw_envelope_reports_failure(persisted)
                ):
                    # A connector-level outage can be reported as data (e.g.
                    # {"success": false, ...}) rather than raised.
                    # available_buying_power()/is_tradable_for_fractional_buy() do
                    # not call _response_failure() themselves (unlike place), so
                    # without this the envelope silently becomes zero buying power
                    # or not-tradable -- a deliberate skip the run can finish as a
                    # clean no-order run, instead of the promised
                    # missing_or_invalid_raw_inputs failure (Codex review: "Treat
                    # failed broker envelopes as raw-input errors"). "place" is
                    # deliberately excluded: its outer failure flag must survive
                    # unrecorded here so RobinhoodBroker._response_failure() raises
                    # its own specific placement-failure error instead.
                    self._recorder.record_error(
                        _kind, str(key), RuntimeError(f"connector reported failure: {persisted}")
                    )
                if persisted is None and payload is not None and _kind == "place":
                    # The connector already submitted a real order -- self._recorder.error
                    # is now set (the run still fails closed on it below), but returning
                    # None here would also erase the only record of that already-completed
                    # broker-side action, letting place_market_buy report "no order id" for
                    # an order that in fact went through. Fall back to the in-memory
                    # response so the id still reaches _place_broker_orders and gets
                    # recorded as submitted before the run blocks.
                    return payload
                return persisted

            persisted_connector._raw_recorder = self._recorder  # type: ignore[attr-defined]
            persisted_connector._raw_connector_original = connector  # type: ignore[attr-defined]
            setattr(self._inner, attribute, persisted_connector)
            wrapped.add(attribute)
        return wrapped

    def account_number(self) -> str:
        value = self._inner.account_number()
        if not self._account_persisted:
            self._recorder.persist("account_number", "account", {"account_number": value})
            self._account_persisted = True
        return value

    def available_buying_power(self) -> float:
        value = self._inner.available_buying_power()
        if not self._buying_power_persisted and "_buying_power_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist("buying_power", "account", {"available_buying_power": value})
            self._buying_power_persisted = True
        return value

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        value = self._inner.same_day_strategy_order_exists(ticker, trading_day)
        if "_orders_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist(
                "orders_since_midnight",
                ticker,
                {"ticker": ticker, "trading_day": trading_day.isoformat(), "same_day_order_exists": value},
            )
        return value

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: Sequence[str]
    ) -> Any:
        """Return and persist the guarded top-up idempotency query response."""
        checker = getattr(self._inner, "same_day_strategy_order_exists_with_ref_id_prefix", None)
        if not callable(checker):
            raise RuntimeError("broker does not support same-stream ref-id duplicate checks for guarded top-ups")
        value = checker(ticker, trading_day, ref_id_prefixes)
        if "_orders_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist(
                "orders_since_midnight",
                ticker,
                {
                    "ticker": ticker,
                    "trading_day": trading_day.isoformat(),
                    "ref_id_prefixes": list(ref_id_prefixes),
                    "same_stream_order": value,
                },
            )
        return value

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        value = self._inner.is_tradable_for_fractional_buy(ticker)
        if "_tradability_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist("tradability", ticker, {"ticker": ticker, "tradable": value})
        return value

    def review_market_buy(self, ticker: str, notional_usd: float):
        return self._inner.review_market_buy(ticker, notional_usd)

    def place_market_buy(self, ticker: str, notional_usd: float, ref_id: str) -> str:
        return self._inner.place_market_buy(ticker, notional_usd, ref_id)

    def get_order_fill(self, order_id: str):
        return self._inner.get_order_fill(order_id)

    def get_raw_placement_responses(self) -> List[Dict[str, Any]]:
        getter: Any = getattr(self._inner, "get_raw_placement_responses", None)
        if callable(getter):
            result: Any = getter()
            if isinstance(result, list):
                return result
            if isinstance(result, (tuple, set)):
                return list(cast(Any, result))
        return []

    def __getattr__(self, name: str) -> Any:
        # Forward optional, non-protocol broker methods (e.g.
        # same_day_strategy_order_exists_with_ref_id_prefix) that some
        # BrokerAdapter implementations support beyond the base protocol.
        return getattr(self._inner, name)


_RESERVED_ARTIFACT_FILENAMES = frozenset({
    "",
    ".",
    "..",
    "raw",
    "manual_runs",
    "manual_reruns",
    "manual_recovery",
    "scheduled_runs",
    "run_state.json",
    "run_summary.json",
    "latest_run_summary.json",
    "morning_signals.csv",
    "midday_signals.csv",
    "market_open_signals.csv",
    "intraday_signals.csv",
    "signals.csv",
    "submitted_orders.csv",
    "filled_orders.csv",
    "orders.csv",
    "buy_candidates.csv",
    "validation.csv",
    "observations.csv",
    "slippage_ranked.csv",
    "missing_fills_report.csv",
    "drift_report.json",
    "drift_report.md",
    "slippage_and_coverage_report.md",
    "weekly_performance_dashboard.md",
    "notion_latest_run_compact.md",
    "notion_latest_run_payload.json",
    "gfuz_supplemental_preflight.json",
    "latest_morning_run.json",
    "latest_preflight.json",
    "latest_scheduled_run.json",
    "latest_midday_run.json",
    "latest_market_open_run.json",
    "latest_intraday_run.json",
    "latest_dca_run.json",
    "latest_trade_run.json",
})


def _sanitize_budget_ledger_filename(raw_filename: Optional[str]) -> str:
    """Return a safe single-file basename for the budget ledger snapshot.

    Rejects empty strings, directory traversal segments ('.', '..'), path
    separators, run-owned directories, and reserved artifact filenames,
    falling back to the canonical 'budget_ledger.json'.
    """
    if not raw_filename or not isinstance(raw_filename, str):
        return "budget_ledger.json"
    cleaned = raw_filename.strip()
    if cleaned.endswith(("/", "\\", "/.", "\\.", "/..", "\\..")):
        return "budget_ledger.json"
    name = Path(cleaned).name.strip()
    if (
        not name
        or name in _RESERVED_ARTIFACT_FILENAMES
        or name.lower() in _RESERVED_ARTIFACT_FILENAMES
        or name.startswith("..")
        or name == "."
        or "/" in name
        or "\\" in name
    ):
        return "budget_ledger.json"
    return name


def _strip_template_segments(path: str) -> str:
    """Drop ``YYYYMMDD*`` template path segments (e.g. ``YYYYMMDD_1030ET``).

    ``must_not_look_under`` entries in config/morning_dca.json include the
    dated live-output directory as a template
    (``.../morning_dca/YYYYMMDD_1030ET/``); this normalizes that down to the
    actual root (``.../morning_dca``) so it can be compared against a
    configured ``preflight_run_state_root``.
    """
    segments = [segment for segment in Path(path).parts if "YYYYMMDD" not in segment]
    return str(Path(*segments))


def _forbidden_preflight_root_reason(
    root: str, forbidden_roots: Sequence[str]
) -> Optional[str]:
    """Return a block reason if ``root`` resolves under a forbidden path, else None.

    Implements ``live_task.preflight_artifact_resolution.must_not_look_under``:
    the same-day 9:45 pre-flight artifact must never be read from the 10:30
    live output directory or a bare cron-id path (the Jul 20 2026 incident).
    """
    try:
        resolved = Path(root).resolve()
    except (OSError, RuntimeError):
        resolved = Path(root)
    for forbidden in forbidden_roots:
        try:
            normalized = Path(_strip_template_segments(forbidden)).resolve()
        except (OSError, RuntimeError):
            normalized = Path(_strip_template_segments(forbidden))
        if resolved == normalized or normalized in resolved.parents:
            return (
                f"preflight_run_state_root {root!r} resolves under a forbidden "
                f"must_not_look_under path ({forbidden!r}); refusing to read the "
                "pre-flight artifact from there (see "
                "live_task.preflight_artifact_resolution in config/morning_dca.json)"
            )
    return None


def morning_preflight_eligible_tickers(
    now: datetime,
    tickers: Sequence[str],
    *,
    preflight_run_state_root: Optional[str] = None,
    preflight_forbidden_roots: Sequence[str] = (),
    preflight_bounded_recovery: bool = True,
    manual: bool = False,
) -> tuple[List[str], Dict[str, Any]]:
    """Restrict ``tickers`` to the same-day 9:45 pre-flight verdict.

    Shared by :meth:`MorningDCAExecutor._apply_preflight_gate` (which
    additionally no-ops outside morning-live-mode) and the seven-slot roster's
    ``run_dca.run_slot("1030ET", ...)``, so config/morning_dca.json's
    NON-BLOCKING contract (2026-08-02) -- a missing, invalid, or valid-but-
    empty pre-flight artifact degrades to the FULL configured universe with a
    warning, never a hard block -- is enforced exactly once no matter which
    caller reaches the 10:30 slot (Codex review: "Route the morning slot
    through its required preflight gate", originally about a fail-closed
    contract this function no longer implements; superseded 2026-08-02 by
    Geoff's "should not block any later runs" direction).

    Returns ``(admitted_tickers, gate_diagnostic)``: the same
    ``preflight_gate``-shaped dict :class:`MorningDCAExecutor` stores on
    itself, so a caller that wants to surface it (e.g. in artifacts) can.
    """
    trading_day = now.astimezone(EASTERN).date()
    # stream_root() at CALL time, not an import-time constant -- same
    # staleness reasoning as elsewhere in this module (Codex review, PR #219).
    root = preflight_run_state_root or stream_root("morning_dca_preflight")
    forbidden_roots = preflight_forbidden_roots or default_preflight_forbidden_roots()
    forbidden_reason = _forbidden_preflight_root_reason(root, forbidden_roots)
    if forbidden_reason is not None:
        # NON-BLOCKING (Geoff, 2026-08-02: "should not block any later runs"):
        # a misconfigured root used to fail SCHEDULED runs closed to zero
        # tickers (preflight_missing_block=True); it now degrades to the full
        # configured universe with a warning for scheduled and manual runs
        # alike, exactly like a missing artifact below. The 9:45 pre-flight
        # remains a coarse pre-screen only, never a hard gate the 10:30 slot
        # depends on to run at all -- see the 9:45 scan's own note in
        # config/morning_dca.json for why this is an acceptable trade (a
        # missing/misconfigured pre-flight just means the 10:30 slot
        # re-evaluates the whole universe itself instead of a pre-screened
        # subset; every other safeguard at 10:30 is unaffected).
        return list(tickers), {"preflight_found": False, "warning": forbidden_reason}
    loaded = load_same_day_preflight(
        root,
        trading_day,
        bounded_recovery=preflight_bounded_recovery,
        forbidden_roots=forbidden_roots,
    )
    if loaded is None:
        # NON-BLOCKING (see the note above): a missing or unparseable same-day
        # pre-flight artifact degrades to the full configured universe with a
        # warning for scheduled and manual runs alike. Previously a scheduled
        # run failed closed to zero tickers here; the 10:30 slot's own signal/
        # budget/spread/trend/top-up checks are the real safety gates and are
        # unaffected by this -- only the coarse pre-screen is skipped.
        return list(tickers), {
            "preflight_found": False,
            "warning": "no same-day 9:45 pre-flight artifact found; evaluating full universe",
        }
    eligible = loaded["eligible_tickers"]
    if not eligible:
        # NON-BLOCKING (see the notes above): a VALID preflight that found
        # zero eligible tickers is exactly as coarse/stale-by-10:30 as a
        # missing one -- treating it as authoritative would make a quiet
        # preflight MORE restrictive than a missing one (which already
        # degrades to the full universe), contradicting the "coarse
        # pre-screen only, never a hard gate" policy this function
        # implements everywhere else (Codex review: "Keep valid empty
        # preflights non-blocking"). Degrade the same way: full universe,
        # with a warning, letting the 10:30 slot's own signal/budget/spread/
        # trend/top-up checks be the real safety gates as usual.
        return list(tickers), {
            "preflight_found": True,
            "preflight_run_id": loaded["run_id"],
            "eligible_to_execute_tickers": [],
            "recovered_via_bounded_lookup": loaded.get("recovered_via_bounded_lookup", False),
            "warning": "same-day 9:45 pre-flight found zero eligible tickers; evaluating full universe",
        }
    admitted = [t for t in tickers if t in eligible]
    gated_out = [t for t in tickers if t not in eligible]
    gate = {
        "preflight_found": True,
        "preflight_run_id": loaded["run_id"],
        "eligible_to_execute_tickers": sorted(eligible),
        "admitted_tickers": admitted,
        "gated_out_tickers": gated_out,
        "recovered_via_bounded_lookup": loaded.get("recovered_via_bounded_lookup", False),
    }
    return admitted, gate


class DCAExecutor:
    """Base class for class-based DCA executors over the strategy library.

    The historical afternoon path is functional (``near_close_dca.run_once`` /
    ``run_daily_compact``) and remains the authoritative live path. This base
    introduces the shared extension points a class-based executor needs — the
    sizing ladder, a run-state root, and a run-classification block — so new
    streams (starting with the morning live stream) can plug in without touching
    the afternoon code.

    Subclasses provide the concrete ladder via :attr:`sizing_config`, the artifact
    root via :meth:`run_state_dir`, and the classification via
    :meth:`run_classification`.
    """

    stream: str = "base"
    run_type: str = "dca"

    def multiplier_for_drop(self, drop_fraction: float, ticker: Optional[str] = None) -> float:
        """Map a drop (fraction of previous close) to a buy multiplier."""
        return graded_buy_multiplier(drop_fraction, self.sizing_config, ticker)

    @property
    def sizing_config(self) -> StrategyConfig:  # pragma: no cover - abstract
        raise NotImplementedError

    def run_state_dir(self, now: datetime) -> Path:  # pragma: no cover - abstract
        raise NotImplementedError

    def run_classification(self, now: datetime) -> Dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError


class MorningDCAExecutor(DCAExecutor):
    """Morning graded-DCA executor: drop-vs-yesterday's-close evaluation.

    Evaluates each ticker's morning price weakness relative to yesterday's
    regular-session close and grades it on the shared $2-base 0x -> 5x live
    ladder. Two modes (see :data:`MORNING_PROFILES`):

    - ``watchlist`` (default, 9:45 AM ET): a read-only early-warning scan. It
      places NO orders and every dollar figure is hypothetical/watchlist-only.
    - ``live`` (10:30 AM ET): the approved Robinhood morning execution. A
      candidate whose price/drop qualifies but whose 60-minute intraday trend
      cannot be validated is flagged ``manual_review_candidate`` and NOT
      auto-bought (the loosened trend-unavailable rule is not implemented).

    Either mode writes ONLY under its morning run-state root and NEVER into the
    afternoon ce16ba6b artifact tree or ``latest_scheduled_run.json``.
    """

    stream = MORNING_STREAM
    # Stream-identity hooks. Subclasses (e.g. the midday executor) override these
    # to reuse the whole evaluate/build/write pipeline under a different stream
    # identity without duplicating it. The defaults reproduce the morning stream
    # exactly, so morning behavior is unchanged.
    PROFILES: Dict[str, Dict[str, Any]] = MORNING_PROFILES
    ledger_namespace: str = MORNING_STREAM
    ladder_name: str = "morning_0x_to_5x"
    signals_csv_name: str = "morning_signals.csv"
    latest_pointer_name: str = "latest_morning_run.json"
    manual_run_subdir: str = "manual_runs"
    budget_daily_key: str = "morning_daily_cap_usd"
    budget_weekly_key: str = "morning_weekly_cap_usd"
    budget_label: str = "morning"
    # Whether _seed_ledger_from_prior_runs should also fold manual_run_subdir/
    # reruns into this stream's OWN cap ledger. True for morning: a manual
    # rerun there is a rerun of this same scheduled task and should count
    # against this stream's own cap. Midday overrides this to False because
    # its manual_reruns/ folder holds post-12:30 backfill artifacts that are
    # documented (near_close_dca.py) as feeding ONLY the afternoon top-up
    # guard, never the midday stream's own scheduled cap.
    scan_manual_reruns_for_ledger: bool = True
    # Whether a live run persists a budget_ledger.json snapshot beside its other
    # artifacts. True for morning (config/morning_dca.json's budget_ledger
    # bootstrap contract) and for the market-open/intraday streams, whose task
    # manifests define their own ledger_dir/ledger_filename. Midday overrides it
    # to False: it has no such manifest block and never wrote one.
    writes_budget_ledger: bool = True

    def __init__(
        self,
        config: Optional[MorningConfig] = None,
        ledger: Optional[MorningLedger] = None,
        *,
        mode: str = MORNING_WATCHLIST_MODE,
        config_dir: Optional[str] = None,
    ) -> None:
        if mode not in self.PROFILES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(self.PROFILES)}")
        self.mode = mode
        self.profile = self.PROFILES[mode]
        self.run_type = self.profile["run_type"]
        self.trades = self.profile["trades"]
        self.config = config if config is not None else self._load_default_config(config_dir, mode)
        self.ledger = ledger or MorningLedger(
            namespace=self.ledger_namespace,
            daily_cap_usd=self.config.morning_daily_cap_usd,
            weekly_cap_usd=self.config.morning_weekly_cap_usd,
        )
        # Shares near_close_dca's default on-disk root (CumulativeCreditLedger's
        # own stream_root("cumulative_dca_credits") default) so a sub-dollar
        # credit accumulated by this stream can be picked up -- and eventually
        # flushed -- by a later same-day slot or the 3:30 PM final session
        # (Codex review: "Route sub-dollar top-ups through the credit policy").
        self.credit_ledger = CumulativeCreditLedger()
        self._sizing_config = self.config.as_strategy_config()
        # stream_root() at CALL time, not self.profile["state_root"] (an
        # import-time constant): a $DCA_CRON_TRACKING_ROOT set between
        # import and this executor's construction must still be honored for
        # a stream whose config didn't itself set morning_run_state_root
        # (Codex review, PR #219). See load_morning_config's config-missing
        # fallback for why that resolution can't happen there instead.
        # os.path.abspath() so a RELATIVE configured root is pinned to the
        # working directory in force at construction. Left relative, it would
        # keep re-resolving against os.getcwd(), so a process that chdir'd
        # after building one executor would have a second executor publish
        # into the SAME scheduled-live slot while deriving a DIFFERENT
        # slot_lock_root below -- both entering the slot and placing
        # concurrently, the exact hole anchoring the lock to the artifact tree
        # was meant to close. Normalising state_root itself (rather than only
        # the lock root) keeps the lock and the artifacts it protects derived
        # from one stable path, so they cannot drift apart (Codex review,
        # PR #593: "Normalize state_root before deriving its lock root").
        self.state_root = os.path.abspath(
            self.config.morning_run_state_root
            or stream_root(self.profile["state_root_stream"])
        )
        # The scheduled-live slot lock's root, resolved ONCE here and retained
        # for this executor's whole life (Codex review, PR #593: "Anchor the
        # slot lock to the configured artifact root").
        #
        # _slot_lock_path() used to build this as stream_root(
        # SLOT_LOCK_ROOT_DIRNAME) on every call, which re-reads
        # $DCA_CRON_TRACKING_ROOT at CALL time -- but what the lock protects is
        # self.state_root, fixed just above and, when the config sets
        # morning_run_state_root explicitly, derived from that config value and
        # not from the environment at all. Those two resolutions could
        # therefore disagree: two workers publishing into the SAME configured
        # morning_run_state_root but running with different
        # $DCA_CRON_TRACKING_ROOT exports would lock two DIFFERENT files, both
        # enter the same live slot, and place/publish duplicate orders -- and a
        # single process whose export changed mid-life would likewise split
        # two calls that must agree.
        #
        # Anchoring the lock root to the artifact tree it guards removes that
        # degree of freedom: the lock root is the SIBLING of state_root,
        # ``<state_root>.parent/slot_locks``. Two invocations agree on the lock
        # exactly when they agree on where they publish, which is the property
        # the lock needs and the only one it needs.
        #
        # Sibling, not child: a lock file INSIDE state_root would re-open the
        # replaceable-directory hole earlier rounds closed (flock() locks an
        # inode, so any directory artifact code can rename away and recreate
        # lets an overlapping invocation acquire a brand-new inode at the same
        # pathname). state_root's PARENT is not an artifact directory of this
        # stream -- in the default layout it is cron_tracking_root() itself,
        # which merely holds the per-stream trees -- so the lock stays outside
        # everything this repository cleans, publishes into, enumerates or
        # rotates, exactly as SLOT_LOCK_ROOT_DIRNAME requires.
        #
        # The default case still honours $DCA_CRON_TRACKING_ROOT, and still at
        # construction time rather than import time: state_root itself is
        # stream_root(<stream>) there, so its parent is that export's root and
        # the lock root lands on the same ``<cron root>/slot_locks`` path the
        # previous stream_root() call produced. No absolute path is written
        # down here (AGENTS.md: "never from a literal ``/home/user/...``
        # string"). self.state_root is already absolutised above -- deliberately
        # there rather than only here, so the lock and the artifacts it guards
        # are derived from ONE stable path and a post-construction chdir cannot
        # move one without the other. Note abspath, never resolve(): resolve()
        # would follow symlinks and could re-point the lock at a replaceable
        # tree, undoing the reason the lock sits outside the published tree.
        self.slot_lock_root = Path(self.state_root).parent / SLOT_LOCK_ROOT_DIRNAME
        self.decision_time = self.config.decision_time_et or self.profile["decision_time_et"]
        # run_ids already folded into self.ledger by _seed_ledger_from_prior_runs
        # -- including this instance's OWN completed runs (added in run() right
        # after write_artifacts) -- so a retried/rerun run() on this SAME
        # executor instance never seeds the same on-disk artifact into the
        # ledger twice.
        self._seeded_prior_run_ids: set = set()
        # Submitted/filled candidate rows recovered from today's own fixed
        # scheduled run_state.json by the most recent _seed_ledger_from_prior_runs
        # call, so a scheduled retry can carry them forward into the artifact
        # it's about to overwrite. Reset at the top of every seeding call.
        self._prior_same_day_candidate_rows: List[Dict[str, Any]] = []
        # Tracks total amount already seeded per (frozenset(cap_roots), trading_day)
        # so repeated run() calls correctly pick up new fills without double-counting.
        self._cap_seeded_amounts: Dict[Tuple, float] = {}
        # Tracks amounts recorded directly into the ledger by record_buy_candidates
        # so cap-seeding doesn't double-count this executor's own submitted orders
        # once the filesystem scan later picks up the same on-disk artifact (e.g.
        # a manual run followed by a scheduled run the same day, or a scheduled
        # RETRY re-scanning its own earlier attempt's dated-slot artifact). Split
        # by manual vs. scheduled origin since cap_seed_roots may cover one
        # without covering the other (Codex review, PR #88: "Avoid double-
        # counting this executor's scheduled fills" -- widening cap_seed_roots
        # to include the scheduled dated slot, done for a separate PR #88 fix,
        # meant a scheduled run's own submission could now also be re-scanned,
        # but only the manual bucket was ever credited back out).
        self._own_recorded_manual: Dict[date, float] = {}
        self._own_recorded_scheduled: Dict[date, float] = {}
        # Always 0.0: cap seeding writes straight into self.ledger (see
        # _compute_top_up's cap_seed_roots block) rather than a separate
        # seeded-amount field, so remaining_daily/remaining_weekly already
        # reflect it. Kept only so the reporting/budget fields below never
        # need a None-check.
        self._prior_daily_spend_seeded = 0.0
        # True when a cross-stream cap artifact could not be read/parsed this
        # run, in which case _compute_top_up forces remaining capacity to 0
        # rather than place against understated headroom (Codex review:
        # "Fail closed when cross-stream cap evidence is unreadable"). Reset
        # at the top of every _compute_top_up call.
        self._cap_seed_scan_incomplete = False
        self._cap_seed_scan_incomplete_paths: List[str] = []
        # Tracks the synthetic ceiling-topping amount the fail-closed branch of
        # _compute_top_up's cap_seed_roots block injects directly into
        # self.ledger.planned_by_day/planned_by_week, keyed by trading_day / iso
        # week, so a LATER successful rescan on this same long-lived executor
        # instance can subtract back out exactly what it injected -- rather than
        # leaving the ledger permanently pinned at the cap ceiling once the
        # transient read failure clears (Codex review: "Clear fail-closed cap
        # poisoning after a recovered retry").
        self._cap_seed_poison_day: Dict[date, float] = {}
        self._cap_seed_poison_week: Dict[Tuple[int, int], float] = {}

    def _load_default_config(self, config_dir: Optional[str], mode: str) -> MorningConfig:
        """Load this executor's config when none is passed. Overridable per stream.

        A live run tolerates an unresolvable account so it can reach its own
        fail-closed path and leave a blocked artifact; see
        :func:`load_morning_config`'s ``allow_unpinned_account`` note. Read-only
        modes keep the strict behavior -- nothing downstream of them can produce
        block evidence, so a raise is the clearest failure they have.
        """
        return load_morning_config(
            config_dir, mode=mode, allow_unpinned_account=mode == MORNING_LIVE_MODE
        )

    def _idempotency_stream(self) -> str:
        """Namespace for the broker idempotency ref id / duplicate check.

        Defaults to ``self.stream``. Overridden by :class:`IntradayDCAExecutor`
        (Codex review, P1: "Scope intraday idempotency keys to each slot"):
        all three intraday slots (11:30/13:30/14:30) share ONE ``self.stream``
        value ("intraday", pinned by schemas/intraday_run_classification.schema.json
        so run_classification and prior-allocation matching stay correct) --
        but using that same value here for the ref_id/`_same_stream_order_exists`
        check made every slot indistinguishable, so a later slot's legitimate,
        lower-price incremental top-up matched an earlier slot's ref id and was
        wrongly treated as an already-submitted retry, capping the ticker to at
        most one intraday order all day. Never used for run_classification,
        reporting, or ledger namespacing -- those must keep the shared
        ``self.stream`` value.
        """
        return self.stream

    def _seed_same_day_cap_from_roots(self, now: datetime) -> None:
        """Seed this stream's cap ledger from ``cap_seed_roots`` for same-day spend.

        Runs for ALL live runs (scheduled, manual, and blocked) so prior same-day/week
        fills always reduce remaining_daily/remaining_weekly before placing orders or
        writing blocked ledger snapshots.
        """
        if self.mode != MORNING_LIVE_MODE:
            return
        cap_roots = getattr(self.config, "cap_seed_roots", None)
        if not cap_roots:
            return
        trading_day = now.astimezone(EASTERN).date()
        boundary_et = now.astimezone(EASTERN).time()
        seed_key = (frozenset(cap_roots), trading_day)
        cap_skip_sink: List[Path] = []
        cap_allocations = load_prior_same_day_allocations_multi(
            cap_roots,
            trading_day,
            boundary_et=boundary_et,
            include_scheduled_afternoon=True,
            skip_sink=cap_skip_sink,
        )
        this_week = iso_week_key(trading_day)
        if cap_skip_sink:
            self._cap_seed_scan_incomplete = True
            self._cap_seed_scan_incomplete_paths = [str(p) for p in cap_skip_sink]
            day_poison_delta = max(
                0.0, self.ledger.daily_cap_usd - self.ledger.day_planned(trading_day)
            )
            if day_poison_delta > 0:
                self.ledger.planned_by_day[trading_day] = (
                    self.ledger.day_planned(trading_day) + day_poison_delta
                )
                self._cap_seed_poison_day[trading_day] = (
                    self._cap_seed_poison_day.get(trading_day, 0.0) + day_poison_delta
                )
            week_poison_delta = max(
                0.0, self.ledger.weekly_cap_usd - self.ledger.week_planned(trading_day)
            )
            if week_poison_delta > 0:
                self.ledger.planned_by_week[this_week] = (
                    self.ledger.week_planned(trading_day) + week_poison_delta
                )
                self._cap_seed_poison_week[this_week] = (
                    self._cap_seed_poison_week.get(this_week, 0.0) + week_poison_delta
                )
        else:
            poisoned_day = self._cap_seed_poison_day.pop(trading_day, 0.0)
            if poisoned_day > 0:
                self.ledger.planned_by_day[trading_day] = max(
                    0.0, self.ledger.day_planned(trading_day) - poisoned_day
                )
            poisoned_week = self._cap_seed_poison_week.pop(this_week, 0.0)
            if poisoned_week > 0:
                self.ledger.planned_by_week[this_week] = max(
                    0.0, self.ledger.week_planned(trading_day) - poisoned_week
                )
            new_total = sum(
                a.amount_usd
                for a in cap_allocations.values()
                if a.amount_usd > 0 and math.isfinite(a.amount_usd)
            )
            self.ledger.seed_day_attempts(
                trading_day,
                sum(a.attempt_count for a in cap_allocations.values()),
            )
            manual_root = str(Path(self.state_root) / self.manual_run_subdir)
            own_manual_in_scan = any(
                Path(manual_root).is_relative_to(Path(r)) for r in cap_roots
            )
            own_scheduled_in_scan = any(
                Path(self.state_root).is_relative_to(Path(r)) for r in cap_roots
            )
            own_amount = (
                self._own_recorded_manual.get(trading_day, 0.0) if own_manual_in_scan else 0.0
            ) + (
                self._own_recorded_scheduled.get(trading_day, 0.0) if own_scheduled_in_scan else 0.0
            )
            prev_total = self._cap_seeded_amounts.get(seed_key, 0.0) + own_amount
            incremental = new_total - prev_total
            if incremental > 0:
                self._cap_seeded_amounts[seed_key] = new_total - own_amount
                self.ledger.record_hypothetical(trading_day, incremental)

    # -- Top-up guard (same-day prior-allocation offset) ---------------------
    def _compute_top_up(
        self, now: datetime, signals: Sequence["MorningSignal"], *, manual: bool = False
    ) -> Dict[str, TopUpOutcome]:
        """Return per-ticker prior-allocation/top-up outcomes.

        Only active in ``MORNING_LIVE_MODE``.  Watchlist and preflight modes are
        analytics-only and return ``{}`` immediately.  For all live runs (scheduled
        and manual alike) the morning cap ledger is seeded from ``cap_seed_roots``
        so prior morning fills always reduce ``remaining_daily`` before orders are
        placed.  Per-ticker incremental top-up outcomes on the MORNING stream are
        computed only for scheduled runs — a manual morning recovery run relies on
        ``same_day_strategy_order_exists`` as its sole duplicate-order gate. Other
        streams (e.g. midday) keep computing per-ticker outcomes on manual retries
        too, since a manual midday retry always writes to its own fresh,
        uuid-suffixed artifact directory (see ``run_id()``) rather than overwriting
        the original run's ``run_state.json`` — so the guarded same-stream
        duplicate check must still run to avoid double-counting a same-stream
        order as a fresh submission (see the ``has_prior_allocation and manual``
        branch in ``_place_broker_orders``).  When ``config.prior_allocation_roots``
        is not set (None) scans the default afternoon root and the morning
        ``manual_runs`` subtree; returns an empty dict only when roots is
        explicitly set to ``[]`` (fully disabled).  Also seeds the cap ledger
        from ``cap_seed_roots`` regardless of per-ticker guard status.

        Cap seeding is intentionally separate from ``prior_allocation_roots`` so
        cross-stream fills (e.g. afternoon fills scanned for per-ticker top-up
        context) never erroneously reduce this stream's cap.
        """
        self._prior_daily_spend_seeded = 0.0
        self._cap_seed_scan_incomplete = False
        self._cap_seed_scan_incomplete_paths = []
        if self.mode != MORNING_LIVE_MODE:
            return {}
        raw_roots = getattr(self.config, "prior_allocation_roots", None)
        # Track whether the operator explicitly disabled the per-ticker guard ([]
        # opt-out). Cap seeding must still run even when the guard is disabled, so
        # we defer the early return until after the cap-seeding block below.
        explicit_disable = raw_roots is not None and len(raw_roots) == 0
        # The market-open root is scanned alongside the afternoon root so a
        # 9:35 AM market-open fill offsets this run's 10:30 desired amount
        # instead of being re-bought as a first allocation (Codex review:
        # "Feed market-open fills into later top-up guards").
        configured = list(raw_roots) if raw_roots is not None else list(default_cap_seed_roots())
        # True when the caller left prior_allocation_roots at its class default
        # (None for MorningConfig's own auto-derive marker, or MiddayConfig's own
        # hardcoded production default) rather than pointing it at a specific,
        # narrower tree -- e.g. an isolated replay/staging config (Codex review:
        # "Honor explicit prior-allocation roots in shared executors").
        uses_default_prior_allocation_roots = (
            raw_roots is None or tuple(raw_roots) == default_prior_allocation_roots()
        )
        # Append the manual_runs subtree of self.state_root only for the morning
        # stream. Midday manual reruns are prior context for the afternoon guard,
        # not for the midday scheduled run; skip to avoid scanning
        # midday_dca/manual_reruns as live prior allocation.
        # Also skip when a configured root already covers manual_root (ancestor)
        # to avoid load_prior_same_day_allocations_multi double-counting fills.
        manual_root = str(Path(self.state_root) / self.manual_run_subdir)
        morning_only = self.stream == MORNING_STREAM
        roots = []
        if not explicit_disable:
            already_covered = any(
                Path(manual_root).resolve().is_relative_to(Path(r).resolve()) for r in configured
            )
            if morning_only and not already_covered:
                roots = list(dict.fromkeys([*configured, manual_root]))
            else:
                roots = list(dict.fromkeys(configured))
        trading_day = now.astimezone(EASTERN).date()
        boundary_et = now.astimezone(EASTERN).time()
        # Whether the resolved scan roots include THIS stream's own scheduled
        # dated-slot root (self.state_root), not just its manual/recovery
        # subtrees. Both config/morning_dca.json and config/midday_dca.json
        # deliberately exclude their own scheduled root from
        # prior_allocation_roots (relying on the same-stream ref-id duplicate
        # check for own-stream retries instead), so this is False under the
        # documented production default -- but a caller/config can still
        # override prior_allocation_roots to include it. When True,
        # prior.amount_usd already reflects a settling order from an earlier
        # attempt of THIS SAME slot (that order's own dated-slot artifact,
        # under this root), so _evaluate_top_up's ladder_incremental =
        # desired - prior.amount_usd is already net of it; separately
        # subtracting settled_slot_credit_usd there would double-deduct the
        # same paid amount and under-order the retry (Codex review, PR #656
        # discussion_r3837494241: "Avoid deducting settled credit already
        # included in prior spend").
        # Containment, not just exact equality: an explicit override can name
        # a common ANCESTOR of self.state_root (e.g. cron_tracking_root
        # itself) rather than self.state_root literally -- the recursive
        # allocation loader still picks up this stream's own scheduled
        # artifacts from under it either way (same containment relationship
        # already used for manual_root's own coverage check just above).
        # Codex review, PR #656 discussion (follow-up on
        # discussion_r3837494241): "Recognize ancestor roots that include
        # the scheduled stream" -- an equality-only test missed this case.
        own_scheduled_root_in_prior_context = any(
            Path(self.state_root).resolve().is_relative_to(Path(r).resolve()) for r in roots
        )
        self._seed_same_day_cap_from_roots(now)
        # Per-ticker incremental outcomes on the MORNING stream: scheduled runs
        # only. A manual morning recovery run relies on same_day_strategy_order_exists
        # as its sole duplicate-order gate. Other streams (midday) keep the
        # guarded per-ticker check active on manual retries too.
        if manual and self.stream == MORNING_STREAM:
            return {}
        if explicit_disable:
            return {}
        prior_allocations = load_prior_same_day_allocations_multi(
            roots, trading_day, boundary_et=boundary_et
        )
        # Fold in same-day MANUAL fills that live only in a bare sibling orders CSV
        # (no buy_candidates, no recorded artifacts.orders_csv_path -- the Jul 21
        # manual midday shape) -- load_prior_same_day_allocations_multi above has no
        # way to find those. Applies to both the morning and midday streams (this
        # method is shared), matching the documented policy that every same-day
        # guard folds these manual artifacts in, not just the afternoon one (Codex
        # review: "Wire manual CSV context into the midday guard"). Anchored on this
        # stream's own state_root so the shared cron_tracking parent resolves
        # correctly regardless of which stream is calling; capped at this run's own
        # time for the same replay-safety reason boundary_et is used above.
        #
        # Only when prior_allocation_roots is still at its class default: a
        # caller that explicitly configured it to an isolated/relocated tree
        # must have that isolation honored, the same guard already applied to
        # run_dca.py's explicit prior_allocation_state_root branch (Codex
        # review: "Honor explicit prior-allocation roots in shared executors").
        if uses_default_prior_allocation_roots:
            merge_manual_top_up_context(
                prior_allocations,
                load_same_day_top_up_context(
                    trading_day.isoformat(), base=Path(self.state_root), boundary_et=boundary_et
                ),
            )
        outcomes: Dict[str, TopUpOutcome] = {}
        for signal in signals:
            if signal.action != Action.BUY or signal.multiplier <= 0:
                continue
            ticker = signal.ticker
            slot_key = self._credit_idempotency_key(now, manual=manual)
            try:
                cached_decision = self.credit_ledger.recorded_decision(
                    ticker, trading_day.isoformat(), slot_key
                )
            except Exception as exc:  # noqa: BLE001
                cached_decision = None
                _log.warning(
                    "cumulative credit ledger idempotency read failed for %s: %s",
                    ticker,
                    exc,
                )
            if cached_decision is not None:
                # THIS slot's own earlier attempt already durably recorded a
                # stays-pending decline for this ticker (the only record()
                # call site that supplies an idempotency_key is the
                # stays-pending branch below). Recomputing from current price
                # data here and combining it with pending_usd() -- which
                # already reflects that earlier attempt's write -- would
                # double count a single slot's decline across a crash retry:
                # the combined total can cross the order threshold purely
                # from being retried, and because it is then positive the
                # stays-pending branch below (the only one that consults this
                # idempotency key) is skipped, so the check never runs at all
                # (Codex review: "Apply retry idempotency before recomputing
                # the credit" -- "check the slot key before applying the
                # current decline"). Reuse the cached decision unchanged
                # instead: it is always order_amount_usd == 0 at this call
                # site, so this candidate contributes no incremental order
                # this run (see _effective_buy_amount, which would otherwise
                # fall back to the full hypothetical amount for a ticker
                # missing from this dict entirely).
                prior = prior_allocations.get(ticker)
                desired, effective_base = resolve_top_up_desired_amount_usd(
                    prior, signal.multiplier, signal.hypothetical_amount_usd, signal.selected_base_amount_usd
                )
                outcomes[ticker] = TopUpOutcome(
                    ticker=ticker,
                    desired_amount=desired,
                    decision_price=signal.current_price,
                    prior_same_day_amount=round(prior.amount_usd, 2) if prior else 0.0,
                    prior_same_day_reference_price=prior.reference_price if prior else None,
                    prior_same_day_run_id=prior.run_id if prior else None,
                    prior_same_day_source=prior.source if prior else None,
                    incremental_order_amount=round(max(0.0, cached_decision.order_amount_usd), 2),
                    top_up_guard_result=TOP_UP_NO_INCREMENTAL,
                    top_up_skipped=True,
                    effective_base_amount_usd=effective_base,
                    cumulative_credit_decline_usd=None,
                )
                continue
            try:
                existing_pending_credit_usd = self.credit_ledger.pending_usd(
                    ticker, trading_day.isoformat()
                )
                # Snapshot of the ticker's TOTAL shared pending balance at
                # the moment the order-vs-stays-pending decision below is
                # about to be made from it -- passed through to
                # reconcile_preserved_credit as expected_pending_usd so a
                # concurrent stream settling or recording an UNRELATED
                # contribution for this same ticker between now and that
                # locked reconciliation invalidates this decision instead of
                # silently reconciling against a stale total (Codex review:
                # "Revalidate the shared pending balance before ordering").
                raw_pending_credit_usd = existing_pending_credit_usd
                settled_slot_credit_usd = self.credit_ledger.settled_credit_for_slot(
                    ticker, trading_day.isoformat(), slot_key
                )
                preserved_slot_credit_usd = self.credit_ledger.preserved_credit_for_slot(
                    ticker, trading_day.isoformat(), slot_key
                )
                # Distinct from preserved_slot_credit_usd > 0: a preserve
                # entry that a PRIOR retry already reconciled down to zero
                # (e.g. a temporary price recovery) still EXISTS in the
                # ledger, and the reconcile-vs-skip decision below must know
                # that -- see has_preserved_entry_for_slot's own docstring
                # (Codex review, PR #656 discussion_r3838839294: "Reconcile
                # zeroed preserve entries before re-preserving").
                preserve_entry_exists_for_slot = self.credit_ledger.has_preserved_entry_for_slot(
                    ticker, trading_day.isoformat(), slot_key
                )
                # THIS slot's own earlier (blocked) attempt may have already
                # folded its decline into the durable balance above via
                # preserve_pending(). Subtracting it back out before
                # recomputing a fresh decline for this same slot avoids
                # double-counting that same-slot contribution -- unlike the
                # stays-pending case handled by recorded_decision above, a
                # blocked placement must still be free to retry (a later
                # gate check might now pass), so it cannot simply reuse a
                # cached final decision here (Codex review: "Recognize
                # preserved credits before recomputing the slot").
                existing_pending_credit_usd = max(
                    0.0,
                    existing_pending_credit_usd - preserved_slot_credit_usd,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "cumulative credit ledger provenance read failed for %s; skipping ticker: %s",
                    ticker,
                    exc,
                )
                prior = prior_allocations.get(ticker)
                desired, effective_base = resolve_top_up_desired_amount_usd(
                    prior, signal.multiplier, signal.hypothetical_amount_usd, signal.selected_base_amount_usd
                )
                outcomes[ticker] = TopUpOutcome(
                    ticker=ticker,
                    desired_amount=desired,
                    decision_price=signal.current_price,
                    prior_same_day_amount=round(prior.amount_usd, 2) if prior else 0.0,
                    prior_same_day_reference_price=prior.reference_price if prior else None,
                    prior_same_day_run_id=prior.run_id if prior else None,
                    prior_same_day_source=prior.source if prior else None,
                    incremental_order_amount=0.0,
                    top_up_guard_result=TOP_UP_NO_INCREMENTAL,
                    top_up_skipped=True,
                    effective_base_amount_usd=effective_base,
                    cumulative_credit_decline_usd=None,
                )
                continue
            evaluate_kwargs = {"existing_pending_credit_usd": existing_pending_credit_usd}
            # Root containment alone does not prove the settling order is
            # actually reflected in THIS ticker's loaded prior: a crash
            # after settle(order_id=...) succeeded but before the original
            # attempt's own run-state artifact was persisted leaves
            # prior.amount_usd without that spend even though the root is
            # covered, and a ticker with no prior allocation at all trips
            # the same blanket root check regardless of whether it was ever
            # bought before. A magnitude-only comparison (loaded prior >=
            # settled amount) is not sufficient either: an unrelated larger
            # prior allocation for the SAME ticker (e.g. a $5 morning buy)
            # can satisfy that inequality even though the specific $0.80
            # settling order's own artifact was never loaded at all, wrongly
            # skipping the settled-credit deduction and letting the retry
            # repurchase the $0.80 (Codex review, PR #656 follow-up on
            # discussion_r3837494241 and discussion_r3839800765: "Base
            # settlement deduction on the loaded allocation" / "Verify that
            # prior allocation contains the settlement"). Instead require
            # the loaded prior's own contributing run_id to be THIS EXACT
            # slot's run_id (run_classification.run_id is always
            # self.run_id(now, manual=manual) on this slot's own artifact,
            # see run_classification() above) -- prior_allocations aggregates
            # amount_usd across every contributing run for the ticker but
            # attributes run_id/reference_price to only the MOST RECENT
            # contributor, so this positively identifies "the run that
            # settled this slot is the one whose artifact was actually
            # loaded" rather than merely "some prior allocation happens to
            # be large enough." Scoped to scheduled retries only (not
            # manual): self.run_id(now, manual=True) mints a fresh random
            # suffix on every call rather than reusing the one already
            # cached/written into this manual invocation's own artifact, so
            # it can never reliably match here -- manual runs simply keep
            # the pre-existing (safe, never-skip) settled_slot_credit_usd
            # deduction instead.
            ticker_prior = prior_allocations.get(ticker)
            # ticker_prior.run_id names only the MOST RECENT contributor (for
            # reference_price ownership), but amount_usd aggregates every
            # contributing run via contributing_run_ids. A newer same-day
            # allocation for this ticker can overwrite run_id to itself while
            # this slot's own settled spend remains folded into amount_usd --
            # an exact run_id equality then wrongly concludes "not loaded"
            # even though it demonstrably is, re-deducting settled_slot_
            # credit_usd and under-ordering the retry by the already-paid
            # amount (Codex review, PR #656 follow-up: "Check all
            # contributors for the loaded settlement"). Membership in the
            # full contributor set is the correct positive-identification
            # check.
            prior_reflects_settlement = (
                not manual
                and own_scheduled_root_in_prior_context
                and ticker_prior is not None
                and self.run_id(now, manual=False) in ticker_prior.contributing_run_ids
                and round(ticker_prior.amount_usd, 2) >= round(settled_slot_credit_usd, 2)
            )
            if settled_slot_credit_usd > 0:
                # Always pass the settled amount through -- prior_reflects_
                # settlement only means the LADDER component (prior.amount_usd)
                # already nets it out; the sub-dollar branch's price/decline
                # floors never reflect prior.amount_usd at all and must still
                # deduct it directly, or a retry can recompute the full
                # pre-settlement floor and repurchase an already-paid dollar
                # (Codex review, PR #656 follow-up: "Keep the settled
                # deduction for non-ladder floors"). _evaluate_top_up's own
                # settled_credit_reflected_in_prior flag is what actually
                # skips the (redundant) ladder-only deduction.
                evaluate_kwargs["settled_slot_credit_usd"] = settled_slot_credit_usd
                evaluate_kwargs["settled_credit_reflected_in_prior"] = prior_reflects_settlement
            if preserved_slot_credit_usd > 0:
                # Thread this slot's own outstanding preserved contribution
                # into the full-dollar ladder branch too: without it, a
                # ladder-driven order recomputed after a partial settlement
                # reports no cumulative-credit metadata at all, so neither
                # the reconciliation below nor _place_broker_orders' settle()
                # call ever runs for it, permanently orphaning the preserved
                # remainder (Codex review: "Deduct settled credit from
                # full-dollar retries too").
                evaluate_kwargs["preserved_slot_credit_usd"] = preserved_slot_credit_usd
            outcome = self._evaluate_top_up(signal, prior_allocations.get(ticker), **evaluate_kwargs)
            preserved_retry_reconciled = False
            # Gated on preserve_entry_exists_for_slot, NOT preserved_slot_
            # credit_usd > 0: a slot whose preserve entry a PRIOR retry
            # already reconciled down to zero still has that entry sitting
            # in the ledger. If this retry's own outcome now carries a fresh
            # positive decline (the price fell again), that revived amount
            # MUST go through reconcile_preserved_credit here -- otherwise it
            # falls through to _place_broker_orders' preserve_pending() call
            # with the SAME idempotency key, whose dedup check finds the
            # (stale, zeroed) entry already present and returns early without
            # adding the revived decline to the shared balance at all (Codex
            # review, PR #656 discussion_r3838839294: "Reconcile zeroed
            # preserve entries before re-preserving").
            if preserve_entry_exists_for_slot and outcome.cumulative_credit_decline_usd is not None:
                # Price movement can change this slot's recomputed remainder.
                # Replace its preserve contribution (including retiring it at
                # zero) rather than recording the changed amount as minimum-
                # order provenance alongside the stale preserve.
                try:
                    self.credit_ledger.reconcile_preserved_credit(
                        ticker,
                        trading_day.isoformat(),
                        slot_key,
                        outcome.cumulative_credit_decline_usd,
                        expected_settled_credit_usd=settled_slot_credit_usd,
                        expected_outstanding_credit_usd=preserved_slot_credit_usd,
                        expected_pending_usd=raw_pending_credit_usd,
                        stream=self.run_type,
                        recorded_at=now.isoformat(),
                    )
                    preserved_retry_reconciled = True
                except Exception as exc:  # noqa: BLE001
                    # A stale settlement snapshot or transient ledger failure
                    # must neither overbuy this ticker nor abort unrelated buys.
                    _log.warning("cumulative credit reconciliation failed for %s: %s", ticker, exc)
                    outcome = replace(
                        outcome,
                        incremental_order_amount=0.0,
                        top_up_guard_result=TOP_UP_NO_INCREMENTAL,
                        top_up_skipped=True,
                        cumulative_credit_decline_usd=None,
                    )
                    outcomes[ticker] = outcome
            if (
                outcome.cumulative_credit_decline_usd is not None
                and outcome.incremental_order_amount <= 0
                and not preserved_retry_reconciled
                # _credit_decline_already_recorded_this_slot always reads the
                # FIXED scheduled run_state_dir(now), regardless of `manual`.
                # A manual run must never consult it: the ledger key it would
                # be guarding against reusing is now manual-unique (see
                # _credit_idempotency_key), but this artifact-based check has
                # no such distinction -- a manual poll can find an UNRELATED
                # scheduled slot's already-recorded pending decline for the
                # same ticker and wrongly treat its own new, genuinely
                # different decline as already recorded, silently dropping it
                # (Codex review: "Bypass the scheduled artifact guard for
                # manual credits"). Scheduled runs keep this belt-and-braces
                # check alongside the ledger-based recorded_decision() short
                # circuit above; manual runs rely on the ledger alone.
                and (manual or not self._credit_decline_already_recorded_this_slot(now, ticker))
            ):
                # The credit policy decided this decline stays pending -- no
                # broker order will be attempted for this ticker this run, so
                # there is no cap/spread/buying-power/placement gate to defer
                # the write through (unlike the crosses-to-an-order case,
                # which _place_broker_orders settles only once the order is
                # actually confirmed submitted). Persist the new pending
                # balance now so a later same-day slot or the 3:30 PM final
                # session can read it back via pending_usd().
                try:
                    self.credit_ledger.record(
                        ticker,
                        trading_day.isoformat(),
                        outcome.cumulative_credit_decline_usd,
                        final_session=False,
                        stream=self._idempotency_stream(),
                        recorded_at=now.astimezone(timezone.utc).isoformat(),
                        # This slot's own dated state-directory name (or, for
                        # a manual run, a stable per-invocation manual ID) --
                        # stable across a scheduler retry of the SAME firing
                        # but distinct for a new day, a different slot, or a
                        # separate manual invocation. Makes the ledger itself
                        # reject a double-apply if the process crashes after
                        # this write but before run_state.json is persisted,
                        # closing the gap left by
                        # _credit_decline_already_recorded_this_slot's
                        # artifact-only check above (Codex review: "Make
                        # pending-credit recording retry-idempotent",
                        # "Give distinct manual runs distinct credit keys").
                        idempotency_key=slot_key,
                        # existing_pending_credit_usd above already netted
                        # THIS slot's own earlier preserved contribution out
                        # of the ledger's raw pending balance so the ORDER
                        # decision above wouldn't double-count it. record()
                        # itself would otherwise re-read that same raw
                        # (still includes this slot's stale, larger
                        # contribution) balance and combine it with
                        # outcome.cumulative_credit_decline_usd a second
                        # time -- a retry recomputing a smaller decline could
                        # then cross the order threshold from double-
                        # counting alone and clear the pending balance even
                        # though this run submitted no broker order (Codex
                        # review: "Do not re-record adjusted credits against
                        # the full ledger"). Passing the already-netted
                        # baseline here keeps this write consistent with the
                        # decision _evaluate_top_up actually made.
                        pending_baseline_usd=existing_pending_credit_usd,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "cumulative credit ledger record failed for %s: %s", ticker, exc
                    )
            outcomes[ticker] = outcome
        return outcomes

    def _evaluate_top_up(
        self,
        signal: "MorningSignal",
        prior: Optional[PriorSameDayAllocation],
        *,
        existing_pending_credit_usd: float = 0.0,
        settled_slot_credit_usd: float = 0.0,
        preserved_slot_credit_usd: float = 0.0,
        settled_credit_reflected_in_prior: bool = False,
    ) -> TopUpOutcome:
        """Apply the same-day top-up rule to one live buy candidate.

        No earlier allocation → full desired amount.  Earlier allocation and
        current price strictly below the prior reference → incremental
        difference only (desired − prior; $0 if ≤ 0).  Price not below
        reference → $0.  Amount known but reference price missing → $0 with
        manual review flagged.

        ``desired`` reuses the first same-day allocation's selected base when
        known (see ``near_close_dca.resolve_top_up_desired_amount_usd``) so a
        later, lower price cannot silently upgrade the whole day's target to
        the $2 scale.

        ``existing_pending_credit_usd`` is a same-day sub-dollar credit
        balance the caller already durably read (via
        ``CumulativeCreditLedger.pending_usd``) from an earlier slot's
        decline. Passed in as plain data -- this function stays a pure
        computation, performing no ledger I/O itself; see
        ``_compute_top_up`` and ``_place_broker_orders``, the only callers
        that read/write the ledger (mirrors
        ``near_close_dca.evaluate_top_up_guard``).

        ``settled_slot_credit_usd`` is the part of this same scheduled slot's
        earlier preserved decline that a later order already paid. Deducting
        it from the freshly recomputed decline prevents a partial settlement
        from being rebuilt on retry.

        ``settled_credit_reflected_in_prior`` is True only when the caller's
        loaded ``prior`` (its own ``amount_usd``, aggregated across
        ``contributing_run_ids``) already includes this same settled amount --
        i.e. ``ladder_incremental`` below is already net of it. Only the
        ladder component is skipped from a further deduction in that case;
        the sub-dollar branch's ``price_step_incremental``/``decline_usd``
        floors are computed purely from prices, never from ``prior.amount_
        usd``, so they never reflect the settlement and must still deduct
        ``settled_slot_credit_usd`` directly, or a retry at the same price can
        recompute the full pre-settlement floor and repurchase the already-
        paid dollar once the recovered order's idempotent settlement no-ops
        (Codex review, PR #656 follow-up: "Keep the settled deduction for
        non-ladder floors").

        ``preserved_slot_credit_usd`` is this same slot's CURRENT outstanding
        preserved contribution (already net of any settlement). The
        sub-dollar branch below folds it into its own decline computation via
        ``existing_pending_credit_usd``/``settled_slot_credit_usd``; the
        full-dollar ladder branch does not otherwise touch the credit ledger
        at all, so it is surfaced on the returned outcome instead, purely so
        the caller's post-decision reconciliation and ``_place_broker_orders``'
        settle-on-placement path both still run for it (Codex review: "Deduct
        settled credit from full-dollar retries too").
        """
        desired, effective_base = resolve_top_up_desired_amount_usd(
            prior, signal.multiplier, signal.hypothetical_amount_usd, signal.selected_base_amount_usd
        )
        price = signal.current_price

        def _outcome(
            incremental: float,
            result: str,
            *,
            skipped: bool,
            manual_review: bool = False,
            cumulative_credit_decline_usd: Optional[float] = None,
        ) -> TopUpOutcome:
            return build_top_up_outcome(
                outcome_cls=TopUpOutcome,
                ticker=signal.ticker,
                desired_amount=desired,
                decision_price=price,
                prior=prior,
                incremental=incremental,
                result=result,
                skipped=skipped,
                effective_base=effective_base,
                manual_review=manual_review,
                cumulative_credit_decline_usd=cumulative_credit_decline_usd,
            )

        if prior is None or prior.amount_usd <= 0:
            return _outcome(desired, TOP_UP_NO_PRIOR_RUN, skipped=desired <= 0)
        # Prior amount is known but its first-allocation base could not be
        # resolved -- the fallback above would otherwise silently recompute
        # `desired` from this run's own freshly selected base (Codex review:
        # "Fail closed when the first allocation base is unknown"), matching
        # the same fix in near_close_dca.evaluate_top_up_guard.
        if prior.selected_base_amount_usd is None or prior.selected_base_amount_usd <= 0:
            return _outcome(0.0, TOP_UP_MISSING_PRIOR_BASE, skipped=True, manual_review=True)
        if prior.reference_price is None or price is None:
            return _outcome(0.0, TOP_UP_MISSING_PRIOR_PRICE, skipped=True, manual_review=True)
        if price >= prior.reference_price:
            return _outcome(0.0, TOP_UP_PRICE_NOT_BELOW, skipped=True)
        ladder_incremental = desired - round(prior.amount_usd, 2)
        if ladder_incremental < 0:
            return _outcome(0.0, TOP_UP_NO_INCREMENTAL, skipped=True)
        price_step_incremental = cumulative_price_step_amount_usd(
            prior.reference_price, price, self.sizing_config
        )
        if ladder_incremental < 1.0:
            # A ladder/price-step floor below $1 must never be submitted as a
            # raw broker order (Robinhood's $1 minimum) nor silently dropped
            # when it computes to $0 -- either would lose the credit for a
            # later same-day slot or the 3:30 PM final session to pick up.
            # Route the whole sub-dollar signal (the larger of the ladder
            # target, the price-step floor, and the raw price decline) through
            # the cumulative-credit policy instead: its own decision is always
            # either $0 (stays pending) or >= $1 (Codex review: "Route
            # sub-dollar top-ups through the credit policy"). This session is
            # never the 3:30 PM final one, so final_session=False -- a credit
            # below 50 cents simply stays pending rather than forcing an order.
            decline_usd = round(max(0.0, prior.reference_price - price), 2)
            # ladder_incremental alone is skipped from this deduction when the
            # loaded prior already reflects the settlement (it's already net
            # of it via prior.amount_usd) -- but price_step_incremental and
            # decline_usd are computed purely from prices and never reflect
            # any settlement, so they always need it subtracted directly.
            ladder_floor = round(
                max(0.0, ladder_incremental - (0.0 if settled_credit_reflected_in_prior else settled_slot_credit_usd)),
                2,
            )
            non_ladder_floor = round(
                max(0.0, max(price_step_incremental, decline_usd) - settled_slot_credit_usd), 2
            )
            credit_amount_usd = max(ladder_floor, non_ladder_floor)
            decision = apply_cumulative_credit(
                existing_pending_credit_usd, credit_amount_usd, final_session=False
            )
            incremental = decision.order_amount_usd
            if incremental <= 0:
                return _outcome(
                    0.0,
                    TOP_UP_NO_INCREMENTAL,
                    skipped=True,
                    cumulative_credit_decline_usd=credit_amount_usd,
                )
            return _outcome(
                incremental,
                TOP_UP_INCREMENTAL,
                skipped=False,
                cumulative_credit_decline_usd=credit_amount_usd,
            )
        # The full-dollar branch must deduct already-settled slot credit too --
        # otherwise a partially settled preserved slot (Codex review: "Deduct
        # settled credit in the full-dollar branch") can be retried once the
        # ladder crosses back above $1 and resubmit the whole prior decline,
        # repurchasing the portion a partial settlement already paid for.
        incremental = round(
            ladder_incremental - (0.0 if settled_credit_reflected_in_prior else settled_slot_credit_usd), 2
        )
        if incremental <= 0:
            return _outcome(0.0, TOP_UP_NO_INCREMENTAL, skipped=True)
        if incremental < 1.0:
            # ladder_incremental itself was >= $1 (the earlier `if
            # ladder_incremental < 1.0` branch above already ruled out
            # anything smaller), but subtracting settled_slot_credit_usd can
            # still drop the remainder back under the broker's $1 minimum
            # (e.g. $1.20 - $0.80 = $0.40). Returning that directly as a
            # placeable order would submit a notional the broker rejects,
            # and a retry could then never resolve the still-preserved
            # credit. Route it through the same cumulative-credit policy the
            # sub-dollar branch above uses instead (Codex review, PR #656
            # follow-up on discussion_r3837494241: "Route post-settlement
            # sub-dollar amounts through credit handling").
            decision = apply_cumulative_credit(
                existing_pending_credit_usd, incremental, final_session=False
            )
            sub_dollar_incremental = decision.order_amount_usd
            if sub_dollar_incremental <= 0:
                return _outcome(
                    0.0,
                    TOP_UP_NO_INCREMENTAL,
                    skipped=True,
                    cumulative_credit_decline_usd=incremental,
                )
            return _outcome(
                sub_dollar_incremental,
                TOP_UP_INCREMENTAL,
                skipped=False,
                cumulative_credit_decline_usd=incremental,
            )
        return _outcome(
            incremental,
            TOP_UP_INCREMENTAL,
            skipped=False,
            cumulative_credit_decline_usd=(
                preserved_slot_credit_usd if preserved_slot_credit_usd > 0 else None
            ),
        )

    # -- Reuse hooks ---------------------------------------------------------
    def _effective_buy_amount(
        self,
        signal: "MorningSignal",
        top_up_outcomes: Optional[Dict[str, Any]],
        broker_outcomes: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Dollar amount actually planned for a buy candidate.

        Prefers the real submitted_amount_usd from broker_outcomes (covers
        duplicate-found retry orders where dup_amount may differ from the
        pre-placement incremental_order_amount). Falls back to the incremental
        order amount when a top-up outcome exists, then the full graded amount.
        """
        if broker_outcomes:
            boc = broker_outcomes.get(signal.ticker)
            if boc is not None:
                submitted = boc.get("submitted_amount_usd")
                if submitted is not None:
                    return float(submitted)
        if top_up_outcomes:
            outcome = top_up_outcomes.get(signal.ticker)
            if outcome is not None:
                return outcome.incremental_order_amount
        return signal.hypothetical_amount_usd

    def _augment_buy_candidate_row(
        self, row: Dict[str, Any], signal: "MorningSignal", top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        """Attach top-up guard fields to a buy-candidate row when the guard ran."""
        outcome = top_up_outcomes.get(signal.ticker) if top_up_outcomes else None
        if outcome is None:
            return
        row["prior_same_day_amount"] = outcome.prior_same_day_amount
        row["prior_same_day_reference_price"] = outcome.prior_same_day_reference_price
        row["prior_same_day_run_id"] = outcome.prior_same_day_run_id
        row["prior_same_day_source"] = outcome.prior_same_day_source
        row["morning_desired_amount"] = outcome.desired_amount
        row["morning_decision_price"] = outcome.decision_price
        row["incremental_order_amount"] = outcome.incremental_order_amount
        row["top_up_guard_result"] = outcome.top_up_guard_result
        row["top_up_skipped"] = outcome.top_up_skipped
        row["top_up_manual_review"] = outcome.top_up_manual_review
        # The guard's effective base (the preserved first-allocation base when
        # reused) actually produced desired/incremental amounts above -- it must
        # replace to_row()'s freshly recomputed selected_base_amount_usd so a
        # later reader sees the base that was ACTUALLY used (Codex review:
        # "Persist the preserved base on top-up candidates").
        row["selected_base_amount_usd"] = outcome.effective_base_amount_usd
        if outcome.top_up_manual_review:
            row["execution_status"] = EXEC_MANUAL_REVIEW_CANDIDATE
        # Persisted so a retry of this same scheduled slot can recognize an
        # already-recorded pending credit (see
        # _credit_decline_already_recorded_this_slot) instead of re-applying
        # the same decline on top of the balance its own earlier attempt this
        # slot already durably wrote (Codex review: "Route sub-dollar top-ups
        # through the credit policy").
        if outcome.cumulative_credit_decline_usd is not None:
            row["cumulative_credit_decline_usd"] = outcome.cumulative_credit_decline_usd

    def _augment_artifacts(
        self, artifacts: Dict[str, Any], top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        """Attach top-up summary fields to the run artifact when the guard ran."""
        if not top_up_outcomes:
            return
        outcomes = list(top_up_outcomes.values())
        top_up_manual_reviews = sorted(o.ticker for o in outcomes if o.top_up_manual_review)
        artifacts["top_up_manual_review_tickers"] = top_up_manual_reviews
        artifacts["top_up_offset_tickers"] = sorted(
            o.ticker for o in outcomes if o.top_up_skipped and not o.top_up_manual_review
        )
        existing_manual_review = set(artifacts.get("manual_review_tickers", []))
        new_top_up_manual = [t for t in top_up_manual_reviews if t not in existing_manual_review]
        if new_top_up_manual:
            artifacts["counts"]["manual_review_candidates"] = (
                artifacts["counts"].get("manual_review_candidates", 0) + len(new_top_up_manual)
            )
            artifacts["manual_review_tickers"] = (
                artifacts.get("manual_review_tickers", []) + new_top_up_manual
            )
            # Also add the full row dicts (not a re-derived minimal dict) so
            # downstream readers of manual_review_candidates see top-up
            # missing-price cases with the same shape as trend-unvalidated
            # cases (MorningSignal.to_row(): source, action, drop_pct,
            # multiplier, warnings, execution_status, trend_validated) plus
            # the top-up fields _attach_top_up_fields already set on the
            # buy_candidates row (prior_same_day_*, morning_desired_amount,
            # morning_decision_price, incremental_order_amount,
            # top_up_guard_result, top_up_skipped, top_up_manual_review).
            new_set = set(new_top_up_manual)
            extra_rows = [
                row for row in artifacts.get("buy_candidates", [])
                if row.get("ticker") in new_set
            ]
            if extra_rows:
                manual_rows = list(artifacts.get("manual_review_candidates", []))
                by_ticker = {r.get("ticker"): r for r in manual_rows if r.get("ticker")}
                for row in extra_rows:
                    ticker = row.get("ticker")
                    if ticker in by_ticker:
                        by_ticker[ticker].update(row)
                    else:
                        # row is one of the SAME dict objects living in
                        # artifacts["buy_candidates"] (extra_rows is filtered
                        # from it, not copied). Append a shallow copy so a
                        # later in-memory mutation of either array's row
                        # (e.g. a broker outcome status update) can't silently
                        # leak into the other before write_artifacts() serializes
                        # them (Gemini review, PR #88).
                        new_row = dict(row)
                        manual_rows.append(new_row)
                        by_ticker[ticker] = new_row
                artifacts["manual_review_candidates"] = manual_rows

    # -- DCAExecutor hooks ---------------------------------------------------
    @property
    def sizing_config(self) -> StrategyConfig:
        return self._sizing_config

    def run_state_dir(self, now: datetime) -> Path:
        now_et = now.astimezone(EASTERN)
        folder = f"{now_et.strftime('%Y%m%d')}_{self.profile['folder_suffix']}"
        return Path(self.state_root) / folder

    def run_id(self, now: datetime, *, manual: bool) -> str:
        now_et = now.astimezone(EASTERN)
        if manual:
            now_utc = now.astimezone(timezone.utc)
            # A short random suffix guarantees uniqueness even when two manual
            # replays share the same decision timestamp (down to the second),
            # so write_artifacts never silently overwrites a prior manual run.
            unique = uuid.uuid4().hex[:6]
            return f"{self.profile['manual_run_id_prefix']}_{now_utc.strftime('%Y%m%d_%H%M%S')}Z_{unique}"
        return f"{self.profile['scheduled_run_id_prefix']}_{now_et.strftime('%Y%m%d')}_{self.profile['folder_suffix']}"

    def _credit_idempotency_key(self, now: datetime, *, manual: bool) -> str:
        """A cumulative-credit-ledger idempotency key stable across retries of
        ONE firing but distinct from every other firing.

        Scheduled runs key on ``run_state_dir(now).name`` (the fixed dated
        slot directory), which a scheduler retry of the same firing reuses
        unchanged. Manual runs must NOT key on that same fixed slot
        directory: unlike a scheduled retry, every legitimate manual rerun of
        the same slot on a trading day is a distinct real invocation, and
        sharing one key would make a later manual run's genuinely new credit
        look like a retry of an earlier one and silently drop it (Codex
        review: "Give distinct manual runs distinct credit keys"). Instead
        this mints (and caches, so every credit-ledger call within the same
        manual process invocation agrees) a stable per-invocation manual ID
        via ``run_id(manual=True)``, which is itself already unique per
        manual call thanks to its random suffix.
        """
        if not manual:
            return self.run_state_dir(now).name
        cached = getattr(self, "_cached_manual_credit_idempotency_key", None)
        if cached is None:
            cached = self.run_id(now, manual=True)
            self._cached_manual_credit_idempotency_key = cached
        return cached

    def run_classification(self, now: datetime, *, manual: bool = False) -> Dict[str, Any]:
        """Build the morning run-classification block for the active mode.

        A scheduled watchlist scan is ``run_type='morning_dca_watchlist_scan'``
        at 09:45; a scheduled live run is ``run_type='morning_dca_live'`` at
        10:30. Either way ``stream='morning'`` and ``valid_afternoon_evidence``
        is false, so the artifact is never afternoon scheduled evidence.
        """
        triggered_iso = now.astimezone(timezone.utc).isoformat()
        profile = self.profile
        classification: Dict[str, Any] = {
            "run_origin": "manual" if manual else "scheduled",
            "run_type": profile["run_type"],
            "stream": self.stream,
            "trades": profile["trades"],
            "cron_id": profile["cron_id"],
            "supersedes_cron_id": None,
            "scheduled_for_et": None if manual else profile["scheduled_for_et"],
            "scheduled_window_start_et": None if manual else profile["window_start_et"],
            "scheduled_window_end_et": None if manual else profile["window_end_et"],
            "manual_run": manual,
            "run_id": self.run_id(now, manual=manual),
            "triggered_at_utc": triggered_iso,
            "state_schema_version": profile["state_schema_version"],
            # The execution broker for all morning stream modes; always Robinhood.
            "execution_broker": "robinhood",
            # Pre-authorized direct placement is only for scheduled LIVE runs; read-only
            # (watchlist/preflight) and manual runs are never pre-authorized.
            "preauthorized_direct_place": bool(profile["trades"]) and not manual,
            # Morning artifacts are valid morning evidence (and prior-allocation context
            # for the midday/afternoon top-up guard), but never afternoon evidence.
            # MiddayDCAExecutor inherits this method unchanged, so this must be
            # scoped to self.stream rather than hardcoded True -- a midday run
            # (self.stream == "midday") is midday prior-allocation context, not
            # morning evidence (Codex review: "Keep morning evidence flags off
            # midday runs").
            "valid_morning_evidence": self.stream == MORNING_STREAM,
            # Explicit, machine-checkable separation from the afternoon stream.
            "valid_afternoon_evidence": False,
            "afternoon_live_cron_id": AFTERNOON_LIVE_CRON_ID,
            # Same-day scheduled-evidence contract (see scripts/scheduled_artifacts.py
            # and docs/run_classification.md): every scheduled run_state carries a
            # non-null run_classification object with trading_day, same_day_required,
            # and valid_scheduled_evidence. valid_scheduled_evidence is finalized in
            # build_artifacts() from the completed state, never asserted blindly here.
            "trading_day": now.astimezone(EASTERN).date().isoformat(),
            "same_day_required": not manual,
            "valid_scheduled_evidence": False,
        }

        # Stamp the account this run is actually pinned to, read from the loaded
        # config rather than the import-time module constant. The constant is
        # captured when morning_dca is imported, so a process that populated or
        # changed $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER before calling
        # load_morning_config() would execute against the loaded account while
        # the artifact claimed the stale import-time value -- including the
        # "UNSET_..." sentinel, which also violates the schema's
        # ^[0-9]{6,12}$ pattern (Codex review, PR #219).
        #
        # The presence rule mirrors the schema, which REQUIRES account_number for
        # every *_dca_live run_type and leaves it optional otherwise:
        #
        #   * A well-formed account is always stamped.
        #   * A trading mode whose account is NOT well-formed records
        #     ``account_pinning_failed: true`` and omits the field.
        #   * Read-only modes simply omit it. Loaded watchlist/preflight configs
        #     carry the literal "read_only", which is not an account and must
        #     never be recorded as one; the field is optional for them anyway.
        #
        # The marker exists because a blocked live run is otherwise
        # unrepresentable. account_pinning_failure() deliberately does not abort
        # the run -- _blocked_live_result() routes through build_artifacts() so
        # operators get durable evidence explaining why the scheduled stream
        # placed nothing. But the schema REQUIRES account_number to match the
        # digit shape for every *_dca_live run, so that evidence had no valid
        # form: stamping the bad value fails the pattern, omitting it fails the
        # conditional `required` (Codex review, PR #219). Raising instead would
        # trade a schema-invalid artifact for no artifact at all, losing exactly
        # the block evidence the fail-closed path exists to produce.
        #
        # So the schema now admits the third state explicitly, and the marker
        # makes it machine-checkable: a watchdog can tell "blocked, account not
        # pinnable" apart from "field missing", which is the distinction that was
        # being lost. Normal live runs are unaffected -- account_number stays
        # required for them.
        #
        # The sole consumer (generate_credit_usage_proxy_report's
        # _account_number_from) falls back to the run_state value when absent.
        account_number = getattr(self.config, "target_account_number", None)
        pinnable = isinstance(account_number, str) and ACCOUNT_NUMBER_SHAPE.fullmatch(
            account_number
        )
        # profile["trades"] gates BOTH branches, not just the failure marker:
        # target_account_number's dataclass default is the import-time
        # ROBINHOOD_AGENTIC_ACCOUNT_NUMBER env constant, and a watchlist/
        # preflight config load that hits load_morning_config's config-missing
        # fallback never overrides it (that fallback only seeds
        # approved_test_dates/model_usage_log_root), so a read-only executor
        # can end up holding a real, well-formed account number. Without this
        # gate `pinnable` alone would stamp that live account into a read-only
        # classification the documented contract above says must omit it
        # entirely (Codex review, PR #219: "Omit account numbers from
        # read-only classifications").
        if pinnable and profile["trades"]:
            classification["account_number"] = account_number
        elif profile["trades"]:
            classification["account_pinning_failed"] = True

        return classification

    # -- Evaluation ----------------------------------------------------------
    def evaluate_signal(self, ticker: str, now: datetime, data: Any) -> MorningSignal:
        """Grade one ticker's morning weakness vs. yesterday's close.

        Uses the provider's previous-close bar and the latest completed intraday
        bar (or a live quote via ``get_quote`` when available) at/or before the
        active mode's decision anchor (09:45 for the watchlist scan, 10:30 for
        the live execution). In live mode a qualifying candidate whose intraday
        trend cannot be validated is flagged ``manual_review_candidate`` and is
        not auto-bought. The watchlist scan places no orders.
        """
        source = getattr(getattr(data, "source", None), "value", "unknown")
        now_et = now.astimezone(EASTERN)
        trading_day = now_et.date()
        decision_dt = datetime.combine(trading_day, self.decision_time, tzinfo=EASTERN)
        anchor_label = self.profile["scheduled_for_et"]

        warnings: List[str] = []
        previous_close: Optional[float] = None
        previous_low: Optional[float] = None
        current_price: Optional[float] = None

        try:
            prev_bar: Optional[Bar] = data.get_previous_close_bar(ticker, decision_dt)
        except Exception as exc:  # noqa: BLE001 - degrade to NO_DATA with a warning
            prev_bar = None
            warnings.append(f"previous_close provider error: {exc}")
        if prev_bar is not None and prev_bar.timestamp >= decision_dt:
            warnings.append(
                f"previous_close timestamp {prev_bar.timestamp.isoformat()} is not before "
                f"the {anchor_label} ET decision anchor; rejecting"
            )
            prev_bar = None
        if prev_bar is not None:
            if not math.isfinite(prev_bar.close) or prev_bar.close <= 0:
                warnings.append(f"previous close {prev_bar.close} is non-positive; rejecting")
            else:
                previous_close = prev_bar.close
                if math.isfinite(prev_bar.low) and prev_bar.low > 0:
                    previous_low = prev_bar.low
        else:
            warnings.append("missing previous close")

        try:
            bars = data.get_intraday_bars(
                ticker=ticker,
                start=datetime.combine(trading_day, time(9, 30), tzinfo=EASTERN),
                end=decision_dt,
                interval_minutes=self.config.interval_minutes,
            )
        except Exception as exc:  # noqa: BLE001
            bars = []
            warnings.append(f"intraday provider error: {exc}")

        # Price provenance + intraday OHLCV for the observation archive. Filled
        # in by _current_price as it resolves the price, then folded into the
        # signal so every artifact path (signals.csv, run_state, the archive)
        # sees the same numbers from one evaluation (dca_intraday_archive).
        #
        # Restricted to bars at or before the same completed-bar anchor
        # _current_price uses to pick the decision bar: the provider's
        # request window ends at decision_dt, but when bar timestamps denote
        # bar starts the newly opened bar at decision_dt is not yet complete
        # and _current_price deliberately excludes it. Aggregating the
        # unfiltered list here would let that bar's high/low/volume leak into
        # the archived session statistics even though the decision price
        # never reflects it (Codex review: "Exclude unfinished bars from
        # session aggregates").
        bar_anchor = completed_bar_anchor(decision_dt, self.config.interval_minutes, self._sizing_config)
        completed_bars = [b for b in bars if b.timestamp <= bar_anchor]
        provenance: Dict[str, Any] = dict(session_stats(completed_bars))
        if prev_bar is not None:
            provenance["previous_close_as_of"] = prev_bar.timestamp
        current_price = self._current_price(
            data, ticker, decision_dt, bars, warnings, provenance
        )
        observation = PriceObservation(
            **{k: v for k, v in provenance.items() if k in PriceObservation._ROW_FIELDS}
        )

        if previous_close is None or not previous_close or current_price is None:
            return MorningSignal(
                ticker=ticker,
                source=source,
                action=Action.NO_DATA,
                reason="missing previous close or current price",
                current_price=current_price,
                previous_close=previous_close,
                drop_pct=None,
                multiplier=0.0,
                hypothetical_amount_usd=0.0,
                warnings=tuple(warnings),
                previous_low=previous_low,
                observation=observation,
            )

        drop_fraction = (previous_close - current_price) / previous_close
        drop_pct = drop_fraction * 100.0
        multiplier = self.multiplier_for_drop(drop_fraction, ticker)
        selected_base_amount_usd, base_selection_reason = select_first_allocation_base_usd(
            current_price, previous_close, previous_low, self.sizing_config, ticker
        )
        hypothetical = round(selected_base_amount_usd * multiplier, 2)
        execution_status: Optional[str] = None
        trend_validated: Optional[bool] = None
        mode_label = "watchlist" if self.mode == MORNING_WATCHLIST_MODE else "live"

        # A calibrated symbol's own first tier is the authoritative minimum
        # drop (Codex review: "Let calibrated first tiers override the
        # global floor" -- "the equivalent check in MorningDCAExecutor has
        # the same problem"): config/per_ticker_sizing.json is a deliberate,
        # reviewed per-symbol model, not a misconfiguration to clamp up to
        # the global default. Only an UNCALIBRATED (fallback) symbol is
        # floored at minimum_drop_vs_previous_close; see the identical fix
        # in near_close_dca.evaluate_source_signal.
        _effective_minimum_drop = (
            minimum_drop_for_ticker(self.sizing_config, ticker)
            if _ticker_sizing(ticker, self.sizing_config)
            else self.sizing_config.minimum_drop_vs_previous_close
        )
        if (
            drop_fraction < _effective_minimum_drop
            or multiplier <= 0
        ):
            # Keyed on multiplier <= 0 as well as the configured minimum drop:
            # a customized config can set minimum_drop_vs_previous_close below
            # the first positive sizing tier, so a drop can clear the minimum
            # while multiplier_for_drop() still returns 0. Without this,
            # _buy_candidates() (which requires multiplier > 0) would still
            # correctly exclude the ticker, but this branch would have already
            # serialized action=BUY / auto_buy_signal=true with a nonzero
            # selected base -- a false execution-gap alert from the drift
            # detector (Codex review: "Normalize every zero-multiplier morning
            # signal").
            action = Action.SKIP if current_price >= previous_close else Action.NO_ACTION
            if current_price >= previous_close:
                reason = "above/at prior close"
            elif drop_fraction < self.config.minimum_drop_vs_previous_close:
                min_drop_pct = self.config.minimum_drop_vs_previous_close * 100.0
                reason = f"below prior close but under {min_drop_pct:.2f}% minimum drop"
            else:
                reason = f"morning weakness {drop_pct:.2f}% vs prior close but under first sizing tier -> 0x"
            multiplier = 0.0
            hypothetical = 0.0
            # select_first_allocation_base_usd only checks decision_price <
            # previous_close, so a price just below close but under the
            # minimum-drop threshold above still sets base_selection_reason to
            # below_prior_close_only/below_prior_close_and_low even though this
            # branch zeros the multiplier to a no-buy. Reset it to the
            # canonical zero-multiplier reason so a buy-scale reason is never
            # serialized alongside a zero-multiplier no-action result (Codex
            # review: "Reset the morning base reason when the multiplier is
            # zero").
            base_selection_reason = BASE_NOT_BELOW_CLOSE
        else:
            action = Action.BUY
            reason = f"morning weakness {drop_pct:.2f}% vs prior close -> {multiplier:g}x ({mode_label})"
            if self.mode == MORNING_LIVE_MODE:
                if self.profile["validate_trend"]:
                    # Confidence rule: price/drop passed, but only auto-buy when the
                    # intraday trend over the 60-minute window can be validated.
                    # Otherwise flag for manual review instead of buying.
                    trend_validated = self._trend_validated(bars, decision_dt, warnings)
                    if trend_validated:
                        execution_status = EXEC_AUTO_BUY_ELIGIBLE
                    else:
                        execution_status = EXEC_MANUAL_REVIEW_CANDIDATE
                        reason += "; intraday trend unvalidated -> manual_review_candidate (no auto-buy)"
                else:
                    # Trend validation is exempted for this profile (market-open's
                    # 9:35 slot: only 5 minutes of session data exist, so a
                    # completed 60-minute downtrend can never validate -- see the
                    # module docstring). The inherited candidate filter still
                    # places a qualifying signal once live_execution_enabled is
                    # turned on, so it must be marked auto-buy eligible the same
                    # way a trend-validated signal is; leaving execution_status
                    # at its None default would serialize auto_buy_signal=false
                    # for a candidate the placement path actually attempts,
                    # blinding scripts/drift_detection.py's missing-order check
                    # to a lost or failed order (Codex review: "Mark trend-exempt
                    # market-open buys as auto-buy eligible"). Scoped to
                    # self.mode == MORNING_LIVE_MODE only: watchlist/preflight
                    # profiles also have validate_trend=False but must keep
                    # execution_status=None (test_watchlist_mode_never_sets_
                    # execution_status), since they never place real orders.
                    execution_status = EXEC_AUTO_BUY_ELIGIBLE

        return MorningSignal(
            ticker=ticker,
            source=source,
            action=action,
            reason=reason,
            current_price=current_price,
            previous_close=previous_close,
            drop_pct=drop_pct,
            multiplier=multiplier,
            hypothetical_amount_usd=hypothetical,
            warnings=tuple(warnings),
            execution_status=execution_status,
            trend_validated=trend_validated,
            previous_low=previous_low,
            selected_base_amount_usd=selected_base_amount_usd if action == Action.BUY else 0.0,
            base_selection_reason=base_selection_reason,
            observation=observation,
        )

    def _trend_validated(
        self, bars: Sequence[Bar], decision_dt: datetime, warnings: List[str]
    ) -> bool:
        """Return True when the intraday trend over the window can be validated.

        The 10:30 live run exists to observe a true ~60-minute trend window
        (9:30-10:30 ET). Validation requires at least ``trend_min_bars``
        completed, positive-priced intraday bars in that window; when fewer are
        available the trend is treated as UNVALIDATED so the caller flags the
        candidate ``manual_review_candidate`` rather than auto-buying. The
        loosened "buy anyway when trend is unavailable" rule is not implemented.
        """
        bar_anchor = completed_bar_anchor(decision_dt, self.config.interval_minutes, self._sizing_config)
        interval = timedelta(minutes=self.config.interval_minutes)
        window_start = decision_dt - timedelta(minutes=60)
        freshest_allowed = bar_anchor - interval
        usable: Dict[Any, Bar] = {}
        for b in bars:
            if window_start <= b.timestamp <= bar_anchor and math.isfinite(b.close) and b.close > 0:
                usable[b.timestamp] = b
        sorted_bars = sorted(usable.values(), key=lambda b: b.timestamp)
        if not sorted_bars or sorted_bars[-1].timestamp < freshest_allowed:
            latest = sorted_bars[-1].timestamp.isoformat() if sorted_bars else "none"
            warnings.append(
                "intraday trend unvalidated: latest completed bar "
                f"{latest} is older than one interval before the decision anchor {bar_anchor.isoformat()}"
            )
            return False
        if len(usable) < self.config.trend_min_bars:
            warnings.append(
                f"intraday trend unvalidated: {len(usable)} usable bars < required "
                f"{self.config.trend_min_bars} over the latest 60-minute window"
            )
            return False
        if sorted_bars[-1].close >= sorted_bars[0].close:
            warnings.append(
                "intraday trend flat or rising: price is at or above open; skipping auto-buy"
            )
            return False
        return True

    def _current_price(
        self,
        data: Any,
        ticker: str,
        decision_dt: datetime,
        bars: Sequence[Bar],
        warnings: List[str],
        provenance: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """Resolve the decision price, optionally recording where it came from.

        ``provenance``, when supplied, is filled in with the price's origin
        (``quote`` vs ``completed_bar``), its as-of timestamp, and the decision
        bar's OHLCV. The return value is unchanged, so callers and tests that
        only want the price are unaffected -- this exists because the price
        alone, with no timestamp and no volume, is not enough to archive the
        poll as a time series (see :mod:`dca_intraday_archive`).
        """
        bar_anchor = completed_bar_anchor(decision_dt, self.config.interval_minutes, self._sizing_config)
        bar = latest_bar_at_or_before(bars, bar_anchor)

        def _record(**fields: Any) -> None:
            if provenance is not None:
                provenance.update(fields)

        _record(
            interval_minutes=self.config.interval_minutes,
            # Completed bars only, matching the same bar_anchor this method
            # uses to pick the decision bar and the caller now uses to build
            # session_stats: the provider's request window ends at
            # decision_dt, so counting the full unfiltered list here would
            # report one more completed interval than the OHLCV aggregates
            # and decision price actually cover when a newly opened,
            # not-yet-finished bar is included (Codex review: "Count only
            # completed bars in archived observations").
            bar_count=sum(1 for b in bars if b.timestamp <= bar_anchor),
        )
        if bar is not None:
            _record(
                bar_timestamp=bar.timestamp,
                bar_open=bar.open,
                bar_high=bar.high,
                bar_low=bar.low,
                bar_close=bar.close,
                bar_volume=bar.volume,
            )

        get_quote = getattr(data, "get_quote", None)
        if callable(get_quote):
            try:
                quote = get_quote(ticker, decision_dt)
                if quote is not None and isinstance(quote, (tuple, list)) and len(quote) >= 1:
                    quote_time = quote[1] if len(quote) >= 2 else None
                    # Record the raw quote whether or not it is accepted below:
                    # a rejected quote is exactly the case where knowing what the
                    # provider actually returned is worth having later.
                    _record(
                        quote_price=_finite_float_or_none(quote[0]),
                        quote_time=quote_time if isinstance(quote_time, datetime) else None,
                    )
                    if isinstance(quote_time, datetime) and bar_anchor <= quote_time <= decision_dt:
                        quote_price = float(quote[0])
                        if quote_price > 0:
                            _record(price_source="quote", price_as_of=quote_time)
                            return quote_price
                        warnings.append(
                            f"quote price {quote_price!r} is not positive; falling back to completed bar"
                        )
                    elif quote_time is None:
                        warnings.append(
                            "quote has no usable timestamp; falling back to completed bar"
                        )
                    elif quote_time > decision_dt:
                        warnings.append(
                            f"quote timestamp {quote_time!r} is after the {self.profile['scheduled_for_et']} ET "
                            "decision anchor; falling back to completed bar"
                        )
                    else:
                        warnings.append(
                            f"quote timestamp {quote_time!r} is older than the completed bar "
                            "anchor; falling back to completed bar"
                        )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"quote provider error: {exc}")
        if bar is None:
            warnings.append(f"missing completed intraday bar at/or before {self.profile['scheduled_for_et']} ET")
            return None
        freshest_allowed = bar_anchor - timedelta(minutes=self.config.interval_minutes)
        if bar.timestamp < freshest_allowed:
            warnings.append(
                f"latest completed intraday bar {bar.timestamp.isoformat()} is older than one interval before "
                f"the {self.profile['scheduled_for_et']} ET decision anchor; rejecting stale data"
            )
            return None
        if not math.isfinite(bar.close) or bar.close <= 0:
            warnings.append(f"completed bar close {bar.close!r} is not positive; rejecting")
            return None
        _record(price_source="completed_bar", price_as_of=bar.timestamp)
        return bar.close

    def evaluate(self, now: datetime, data: Any, *, manual: bool = False) -> List[MorningSignal]:
        """Evaluate every non-excluded ticker for the morning stream.

        In morning live (10:30) mode the universe is first narrowed by the
        same-day 9:45 pre-flight gate: only tickers marked
        ``eligible_to_execute=true`` are re-evaluated. For scheduled
        (non-manual) runs a missing pre-flight artifact is fatal; for manual
        runs the gate degrades to the full universe with a warning.
        """
        tickers = [t for t in self.config.tickers if t not in self.config.excluded_tickers]
        tickers = self._apply_preflight_gate(now, tickers, manual=manual)
        return [self.evaluate_signal(t, now, data) for t in tickers]

    # Populated by :meth:`_apply_preflight_gate` for morning live runs so
    # build_artifacts can surface which tickers the pre-flight gate admitted.
    preflight_gate: Optional[Dict[str, Any]] = None

    def _apply_preflight_gate(self, now: datetime, tickers: List[str], *, manual: bool = False) -> List[str]:
        """Restrict the morning live universe to the same-day pre-flight verdict."""
        self.preflight_gate = None
        if not (self.mode == MORNING_LIVE_MODE and self.stream == MORNING_STREAM):
            return tickers
        admitted, gate = morning_preflight_eligible_tickers(
            now,
            tickers,
            preflight_run_state_root=self.config.preflight_run_state_root,
            preflight_forbidden_roots=self.config.preflight_forbidden_roots,
            preflight_bounded_recovery=self.config.preflight_bounded_recovery,
            manual=manual,
        )
        self.preflight_gate = gate
        return admitted

    @staticmethod
    def _buy_candidates(signals: Sequence[MorningSignal]) -> List[MorningSignal]:
        return [s for s in signals if s.action == Action.BUY and s.multiplier > 0]

    def record_buy_candidates(
        self,
        trading_day: date,
        buy_candidates: Sequence[MorningSignal],
        top_up_outcomes: Optional[Dict[str, Any]] = None,
        broker_outcomes: Optional[Dict[str, Any]] = None,
        *,
        _track_own: Optional[str] = None,
    ) -> None:
        """Record this run's buy candidates against the morning ledger.

        Call exactly once per run (``run()`` does this before building
        artifacts) -- ``build_artifacts`` itself is side-effect-free, so
        inspecting/rebuilding artifacts never double-counts hypothetical spend.
        The effective amount (after any stream-specific top-up offset) is what is
        recorded, so the ledger reflects what the stream actually plans to spend.

        ``broker_outcomes``, when provided, takes precedence for duplicate-found
        orders where dup_amount may differ from incremental_order_amount (see
        ``_effective_buy_amount``).

        ``_track_own="manual"`` or ``_track_own="scheduled"`` (set by ``run()``
        for a genuinely submitted batch) additionally records the amount in the
        matching ``_own_recorded_manual``/``_own_recorded_scheduled`` bucket so
        the cap-seeding watermark in ``_compute_top_up`` can exclude this
        executor's own submissions from the filesystem scan once it re-scans the
        same on-disk artifact. The two buckets use DIFFERENT accumulation
        semantics because manual and scheduled artifacts persist differently:

        - ``"manual"`` ACCUMULATES (``+=``) across calls: each manual run writes
          its own fresh, uuid-suffixed artifact directory (see ``run_id()``) that
          is never overwritten, so every past manual submission still sits on
          disk and the filesystem scan will find ALL of them.
        - ``"scheduled"`` REPLACES (not accumulates): the scheduled dated slot
          (``run_state_dir()``) is the SAME path every call and gets fully
          overwritten by each ``write_artifacts()``. A ticker "submitted" on an
          earlier attempt can become "skipped" on a later retry via the
          ordinary same-day duplicate-check path (the top-up-guard duplicate
          path is the only one that re-records it as "submitted" to preserve
          evidence) -- so the on-disk total can SHRINK between calls, not just
          grow, and only the CURRENT write's own batch should ever be credited.
        """
        if _track_own == "scheduled":
            # The scheduled dated-slot artifact is fully OVERWRITTEN by every
            # write_artifacts() call (unlike manual artifacts, which each get
            # their own unique uuid-suffixed file via run_id() and are never
            # overwritten) -- a ticker "submitted" on an earlier attempt this
            # trading day can become "skipped" on a later retry via the
            # ordinary same-day duplicate-check path in _place_broker_orders
            # (only the top-up-guard duplicate path re-records it as
            # "submitted" to preserve evidence). So the on-disk total
            # attributable to this executor's own writes can SHRINK between
            # calls, not just grow, and accumulating with += would keep
            # crediting an amount the filesystem scan can no longer see.
            # Replace with just this call's own batch total so a later
            # cap-seeding scan credits exactly what THIS write currently
            # puts on disk (Codex review, PR #88: "Do not subtract
            # overwritten scheduled submissions from cap seeding").
            self._own_recorded_scheduled[trading_day] = 0.0
        for candidate in buy_candidates:
            amt = self._effective_buy_amount(candidate, top_up_outcomes, broker_outcomes)
            self.ledger.record_hypothetical(trading_day, amt)
            if _track_own == "manual":
                self._own_recorded_manual[trading_day] = (
                    self._own_recorded_manual.get(trading_day, 0.0) + amt
                )
            elif _track_own == "scheduled":
                self._own_recorded_scheduled[trading_day] = (
                    self._own_recorded_scheduled.get(trading_day, 0.0) + amt
                )

    # -- Artifacts -----------------------------------------------------------
    def build_artifacts(
        self,
        now: datetime,
        signals: Sequence[MorningSignal],
        data_source: str,
        *,
        manual: bool = False,
        top_up_outcomes: Optional[Dict[str, Any]] = None,
        broker_outcomes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Assemble the morning run_state dict for the active mode.

        Pure/side-effect-free: reads the ledger but never mutates it. Callers
        must record buy candidates via :meth:`record_buy_candidates` beforehand
        for the ledger-derived fields (caps, remaining budget) to reflect this
        run. In live mode candidates whose intraday trend could not be validated
        are surfaced as ``manual_review_candidates`` rather than counted as buys.
        """
        now_et = now.astimezone(EASTERN)
        trading_day = now_et.date()
        is_live = self.mode == MORNING_LIVE_MODE
        is_preflight = self.mode == MORNING_PREFLIGHT_MODE

        signal_rows = [s.to_row() for s in signals]
        if is_preflight:
            for row, sig in zip(signal_rows, signals, strict=True):
                row["eligible_to_execute"] = preflight_eligibility(sig)
        buy_candidates = self._buy_candidates(signals)
        manual_review = [
            c for c in buy_candidates if c.execution_status == EXEC_MANUAL_REVIEW_CANDIDATE
        ]
        spend_candidates = [c for c in buy_candidates if c.execution_status != EXEC_MANUAL_REVIEW_CANDIDATE]

        hypothetical_spend = round(
            sum(self._effective_buy_amount(c, top_up_outcomes, broker_outcomes) for c in spend_candidates), 2
        )

        warnings: List[str] = []
        for sig in signals:
            warnings.extend(f"{sig.ticker}: {w}" for w in sig.warnings)
        if self.ledger.over_daily_cap(trading_day):
            warnings.append(
                f"hypothetical {self.budget_label} day spend {self.ledger.day_planned(trading_day):.2f} exceeds "
                f"{self.budget_label} daily cap {self.ledger.daily_cap_usd:.2f}"
            )
        if self.ledger.over_weekly_cap(trading_day):
            warnings.append(
                f"hypothetical {self.budget_label} week spend {self.ledger.week_planned(trading_day):.2f} exceeds "
                f"{self.budget_label} weekly cap {self.ledger.weekly_cap_usd:.2f}"
            )

        classification = self.run_classification(now, manual=manual)

        buy_candidate_rows = []
        for c in spend_candidates:
            row = c.to_row()
            self._augment_buy_candidate_row(row, c, top_up_outcomes)
            buy_candidate_rows.append(row)

        artifacts = {
            "run_id": classification["run_id"],
            "stream": self.stream,
            "mode": self.profile["artifact_mode"],
            "trades": self.trades,
            "generated_at": now_et.isoformat(),
            "trading_day": trading_day.isoformat(),
            "iso_week": list(iso_week_key(trading_day)),
            "data_source": data_source,
            "ladder": {
                "name": self.ladder_name,
                "base_amount_usd": self.config.base_buy_amount_usd,
                "tiers": [list(t) for t in self.config.sizing_tiers],
                "no_buy_below_pct": self.config.minimum_drop_vs_previous_close * 100.0,
            },
            "budget": {
                "ledger_namespace": self.ledger.namespace,
                self.budget_daily_key: self.ledger.daily_cap_usd,
                self.budget_weekly_key: self.ledger.weekly_cap_usd,
                "hypothetical_planned_spend_usd": hypothetical_spend,
                "hypothetical_day_spend_usd": round(self.ledger.day_planned(trading_day), 2),
                "hypothetical_week_spend_usd": round(self.ledger.week_planned(trading_day), 2),
                "prior_same_day_spend_usd": round(self._prior_daily_spend_seeded, 2),
                "remaining_daily_usd": round(
                    max(0.0, self.ledger.remaining_daily(trading_day) - self._prior_daily_spend_seeded), 2
                ),
                "remaining_weekly_usd": round(
                    max(0.0, self.ledger.remaining_weekly(trading_day) - self._prior_daily_spend_seeded), 2
                ),
                "cap_seed_scan_incomplete": self._cap_seed_scan_incomplete,
                "cap_seed_scan_incomplete_paths": list(self._cap_seed_scan_incomplete_paths),
            },
            "counts": {
                "evaluated": len(signals),
                # Aligned with len(buy_candidate_rows)/the "buy_candidates" artifact
                # list below (both built from spend_candidates), not the raw
                # BUY-actioned signal count -- dca_weekly_dashboard.py sums this
                # count directly, so a withheld manual-review row must not inflate
                # it (Codex review on the PR #43 merge).
                "buy_candidates": len(spend_candidates),
                "no_data": sum(1 for s in signals if s.action == Action.NO_DATA),
                "manual_review_candidates": len(manual_review),
            },
            "run_classification": classification,
            "signal_rows": signal_rows,
            "buy_candidates": buy_candidate_rows,
            "warnings": warnings,
        }
        # Derive valid_scheduled_evidence from the completed state rather than
        # asserting it in run_classification(): a scheduled (non-manual) run is
        # same-day scheduled evidence, a manual run never is.
        classification["valid_scheduled_evidence"] = derive_valid_scheduled_evidence(artifacts)
        if is_live:
            # Live-mode confidence rule: expose the trend-unvalidated candidates
            # that were withheld from auto-buy for downstream manual review.
            # Attach top-up guard fields the same way spend_candidates rows do:
            # _compute_top_up only checks action/multiplier (not trend_validated),
            # so a trend-unvalidated BUY can still have a real prior-allocation/
            # top-up outcome a reviewer needs to see (Codex review on the PR #43
            # merge).
            manual_review_rows = []
            for c in manual_review:
                row = c.to_row()
                self._augment_buy_candidate_row(row, c, top_up_outcomes)
                manual_review_rows.append(row)
            artifacts["manual_review_candidates"] = manual_review_rows
            artifacts["manual_review_tickers"] = [c.ticker for c in manual_review]
            if self.preflight_gate is not None:
                artifacts["preflight_gate"] = self.preflight_gate
                if not self.preflight_gate.get("preflight_found"):
                    # The manual degrade-to-full-universe path sets "warning"; the
                    # scheduled fail-closed block sets "block_reason" instead.
                    warn_msg = self.preflight_gate.get("warning") or self.preflight_gate.get("block_reason")
                    if warn_msg:
                        warnings.append(str(warn_msg))
        if is_preflight:
            # Pre-flight verdict: which tickers the 10:30 live evaluator should
            # re-evaluate. Coarse pre-screen only; 10:30 recomputes fresh.
            eligible = [s for s in signals if preflight_eligibility(s)]
            artifacts["eligible_to_execute_tickers"] = [s.ticker for s in eligible]
            artifacts["counts"]["eligible_to_execute"] = len(eligible)
            # Machine-complete envelope (schema morning-preflight-v2-machine-complete):
            # one row per active symbol plus top-level gate fields the 10:30 live
            # task consumes without re-deriving. Unresolved symbols become rows
            # with eligible_to_execute=false; they never flip overall_ok.
            ticker_rows = [preflight_ticker_eligibility_row(s) for s in signals]
            unresolved = [
                r["symbol"] for r in ticker_rows
                if r["data_confidence_status"] != "resolved"
            ]
            blocking_errors: List[str] = []
            artifacts["per_ticker_eligibility"] = ticker_rows

            eligible_count = sum(1 for r in ticker_rows if r["eligible_to_execute"])
            artifacts["eligible_count"] = eligible_count
            artifacts["ineligible_count"] = len(ticker_rows) - eligible_count
            artifacts["unresolved_tickers"] = unresolved
            artifacts["blocking_errors"] = blocking_errors
            # overall_ok is false ONLY for global/account/auth/schema-write
            # failures; per-symbol unresolved/ineligible tickers do not count.
            artifacts["overall_ok"] = not blocking_errors
        # Record the placement switch on every artifact so a reader can tell a
        # shadow run (evaluated fully, deliberately placed nothing) apart from a
        # live run that genuinely found no candidates. Both leave
        # order_submit_status=no_orders, which alone is ambiguous.
        artifacts["live_execution_enabled"] = getattr(
            self.config, "live_execution_enabled", True
        )
        self._augment_artifacts(artifacts, top_up_outcomes)
        return artifacts

    def _complete_signal_rows_for_output(
        self, rows: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Return CSV rows containing every configured active ticker exactly once.

        This is an artifact-contract guard only. Evaluation and order selection
        use the real ``signals`` list before this helper runs; missing rows are
        materialized only when writing signals.csv or validation.csv so a ticker
        dropped by a gate, join, provider edge case, or recovery path is visible
        downstream as a skipped row instead of disappearing from the artifact.
        Any extra/unknown or duplicate rows from the input are preserved after
        the configured set.
        """
        configured = [
            ticker
            for ticker in self.config.tickers
            if ticker not in self.config.excluded_tickers
        ]
        ticker_key = "ticker"
        if rows and any("symbol" in row for row in rows) and not any(
            "ticker" in row for row in rows
        ):
            ticker_key = "symbol"
        row_by_ticker: Dict[str, Dict[str, Any]] = {}
        extras: List[Dict[str, Any]] = []
        for row in rows:
            copied = dict(row)
            ticker = copied.get("ticker") or copied.get("symbol")
            if ticker and ticker not in row_by_ticker:
                row_by_ticker[str(ticker)] = copied
            else:
                extras.append(copied)

        gated_out = set()
        if isinstance(self.preflight_gate, dict):
            gated_out = {str(t) for t in self.preflight_gate.get("gated_out_tickers", [])}

        completed: List[Dict[str, Any]] = []
        for ticker in configured:
            if ticker in row_by_ticker:
                completed.append(row_by_ticker[ticker])
                continue
            reason = (
                "preflight_gate_gated_out"
                if ticker in gated_out
                else "not_evaluated_missing_signal_row"
            )
            completed.append(
                {
                    ticker_key: ticker,
                    "source": "not_evaluated",
                    "action": Action.SKIP.value,
                    "reason": reason,
                    "current_price": None,
                    "previous_close": None,
                    "drop_pct": None,
                    "multiplier": 0.0,
                    "previous_low": None,
                    "selected_base_amount_usd": 0.0,
                    "base_selection_reason": reason,
                    "hypothetical_amount_usd": 0.0,
                    "warnings": reason,
                    "execution_status": None,
                    "trend_validated": None,
                    "auto_buy_signal": False,
                    "spread_ok": None,
                    "spread_pct": None,
                    "spread_status": SPREAD_STATUS_NOT_EVALUATED,
                }
            )

        for ticker, row in row_by_ticker.items():
            if ticker not in configured:
                completed.append(row)
        completed.extend(extras)
        return completed

    def _same_day_pointer_payload(
        self,
        artifacts: Dict[str, Any],
        run_state_path: Any,
        *,
        valid_scheduled_evidence: Optional[bool] = None,
        pointer_status: str = POINTER_STATUS_VALID,
    ) -> Dict[str, Any]:
        """Build the same-day latest-pointer payload for a scheduled run.

        Emits the required same-day metadata (pointer_status, same_day_required,
        updated_at_utc, and valid_scheduled_evidence derived from the run_state
        being pointed at) so downstream consumers can validate the pointer
        against the current ET day and stream (see scripts/same_day_pointer.py
        and config/scheduled_tasks.json -> same_day_pointer_validation_policy).

        ``valid_scheduled_evidence`` overrides the derivation, and
        ``pointer_status`` the default 'valid'. The failure path uses both: it
        points at a run that fired and failed, which proves the slot ran but is
        NOT scheduled evidence, and the generic derivation only inspects the
        scheduled/manual identity so it cannot see that on its own.
        """
        if valid_scheduled_evidence is None:
            valid_scheduled_evidence = derive_valid_scheduled_evidence(artifacts)
        return build_pointer_metadata(
            run_id=artifacts.get("run_id"),
            run_state_path=str(run_state_path),
            stream=artifacts.get("stream", self.stream),
            trading_day=str(artifacts.get("trading_day") or artifacts.get("date") or ""),
            valid_scheduled_evidence=valid_scheduled_evidence,
            pointer_status=pointer_status,
        )

    def _scheduled_pointer_names(self, is_preflight: bool) -> List[str]:
        """Latest-pointer file names refreshed by a scheduled run.

        The morning live 10:30 task refreshes both ``latest_morning_run.json``
        and ``latest_morning_live.json`` so live-only consumers and run-level
        consumers each see the same-day scheduled state. Preflight refreshes its
        own pointer; other live streams (e.g. midday) refresh only their single
        stream pointer.
        """
        if is_preflight:
            return ["latest_preflight.json"]
        names = [self.latest_pointer_name]
        if self.mode == MORNING_LIVE_MODE and self.stream == MORNING_STREAM:
            names.append("latest_morning_live.json")
        return names

    def _write_same_day_pointers(
        self,
        pointer_names: Sequence[str],
        payload: Dict[str, Any],
        paths: Dict[str, str],
    ) -> None:
        """Write ``payload`` to each same-day latest pointer under the state root."""
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        for pointer_name in pointer_names:
            pointer = Path(self.state_root) / pointer_name
            pointer.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(pointer, serialized)
            paths[pointer_name.replace(".json", "")] = str(pointer)

    def write_artifacts(self, artifacts: Dict[str, Any], now: datetime) -> Dict[str, str]:
        """Write morning run_state.json (+ CSV) under the morning run-state root.

        Writes ONLY under the morning root and, for scheduled runs, updates the
        morning-only ``latest_morning_run.json`` pointer. It never writes into the
        afternoon ce16ba6b tree or touches ``latest_scheduled_run.json``.
        """
        manual_run = artifacts["run_classification"]["manual_run"]
        out = (
            Path(self.state_root) / self.manual_run_subdir / artifacts["run_id"]
            if manual_run
            else self.run_state_dir(now)
        )
        out.mkdir(parents=True, exist_ok=True)

        run_state_path = out / "run_state.json"
        atomic_write_text(run_state_path, json.dumps(artifacts, indent=2, sort_keys=True))

        signals_path = out / self.signals_csv_name
        output_signal_rows = self._complete_signal_rows_for_output(
            artifacts["signal_rows"]
        )
        _write_csv(signals_path, output_signal_rows)

        paths = {"run_state": str(run_state_path), "signals": str(signals_path)}

        # budget_ledger.bootstrap (added Jul 20 2026, see config/morning_dca.json):
        # persist a budget_ledger.json snapshot of the morning ledger under the
        # live task's own output directory. No pre-existing file is required --
        # this always (re)writes the snapshot from the caps/spend already
        # computed into artifacts["budget"], so a missing ledger on disk is
        # bootstrapped rather than treated as an escalation condition.
        if self.mode == MORNING_LIVE_MODE and self.writes_budget_ledger:
            ledger_filename = _sanitize_budget_ledger_filename(
                self.config.budget_ledger_filename
            )
            ledger_path = out / ledger_filename
            atomic_write_text(
                ledger_path, json.dumps(artifacts["budget"], indent=2, sort_keys=True)
            )
            paths["budget_ledger"] = str(ledger_path)

        # Write submitted_orders.csv so detect_drift() can build submitted_symbols.
        # Without this file the detector's submitted set is empty and every auto-buy
        # signal is falsely reported as an unsubmitted order. A LIVE run ALWAYS
        # writes this file (with a header row even when no order was submitted) so
        # a no-order live run is a well-formed artifact rather than a missing-CSV
        # contract failure; pending/unconfirmed broker responses keep their concrete
        # status but still produce a row (see scripts/scheduled_artifacts.py).
        broker_outcomes: Dict[str, Any] = artifacts.get("broker_outcomes") or {}
        # Exclude simulated dry-run placements, matching the afternoon contract
        # writer. DryRunBroker assigns each a synthetic DRY-RUN-* order id, and
        # submitted_order_rows() includes any outcome carrying an order id, so
        # without this filter a dry-run report writes simulations into
        # submitted_orders.csv and the drift detector counts them as genuine
        # submissions, reporting no missing-order drift (Codex review).
        placed_outcomes = {
            ticker: outcome
            for ticker, outcome in broker_outcomes.items()
            if not (isinstance(outcome, dict) and outcome.get("status") == "dry_run")
        }
        submitted_rows = submitted_order_rows(placed_outcomes)
        # Plus any submission carried forward from an earlier attempt at this same
        # scheduled slot, so a retry never rewrites the canonical order record
        # without the orders that attempt actually placed (Codex review).
        submitted_rows.extend(
            # submitted_rows is passed in so a candidate folded into an outcome
            # row can hand that row its notional instead of losing it (Codex
            # review, PR #593: "Preserve candidate notional when folding into
            # outcome rows").
            _carried_forward_order_rows(
                placed_outcomes, artifacts.get("buy_candidates") or [], submitted_rows
            )
        )
        is_live_execution = self.mode == MORNING_LIVE_MODE and self.trades
        if is_live_execution or submitted_rows:
            orders_path = write_submitted_orders_csv(out, submitted_rows)
            paths["submitted_orders"] = str(orders_path)

        is_preflight = self.mode == MORNING_PREFLIGHT_MODE
        if is_preflight:
            # validation.csv: the per-ticker eligible_to_execute verdict.
            validation_path = out / "validation.csv"
            _write_csv(validation_path, output_signal_rows)
            paths["validation"] = str(validation_path)
            # run_summary.json: a compact roll-up of the pre-flight verdict.
            summary_path = out / "run_summary.json"
            atomic_write_text(
                summary_path,
                json.dumps(
                    {
                        "run_id": artifacts["run_id"],
                        "trading_day": artifacts["trading_day"],
                        "stream": self.stream,
                        "mode": artifacts["mode"],
                        "counts": artifacts["counts"],
                        "eligible_to_execute_tickers": artifacts.get("eligible_to_execute_tickers", []),
                    },
                    indent=2,
                    sort_keys=True,
                ),
            )
            paths["run_summary"] = str(summary_path)
        elif is_live_execution:
            # A LIVE run (10:30 morning, 12:30 midday) must also write the
            # canonical root-level run_summary.json mirroring run_state's non-null
            # run_classification. Before this, only the read-only pre-flight wrote
            # one, so every live morning/midday run directory failed the scheduled
            # artifact contract's require_summary check and downstream readers had
            # no summary to attribute the run by (Jul 27 2026 12:30 and 1:30).
            summary_path = out / "run_summary.json"
            summary = build_run_summary(
                artifacts, extra={"stream": self.stream, "mode": artifacts.get("mode")}
            )
            atomic_write_text(summary_path, json.dumps(summary, indent=2, sort_keys=True))
            paths["run_summary"] = str(summary_path)

        orders_rows = [
            c for c in artifacts.get('buy_candidates', []) if c.get('order_id')
        ]
        if orders_rows:
            orders_csv_path = out / "orders.csv"
            _write_csv(
                orders_csv_path,
                [
                    {
                        "ticker": c.get("ticker", ""),
                        "order_id": c.get("order_id", ""),
                        "notional_usd": c.get("planned_amount_usd", c.get("hypothetical_amount_usd", "")),
                        "status": c.get("status", ""),
                        "avg_fill_price": "",
                        "quantity_filled": "",
                    }
                    for c in orders_rows
                ],
            )
            paths["orders_csv"] = str(orders_csv_path)

        if not artifacts["run_classification"]["manual_run"]:
            self._write_same_day_pointers(
                self._scheduled_pointer_names(is_preflight),
                self._same_day_pointer_payload(artifacts, run_state_path),
                paths,
            )

        self._archive_observations(artifacts, out, paths, run_state_path)
        return paths

    def _archive_observations(
        self,
        artifacts: Dict[str, Any],
        out: Path,
        paths: Dict[str, str],
        run_state_path: Path,
    ) -> None:
        """Record this poll's prices/timestamps in the shared intraday archive.

        Runs for EVERY mode -- watchlist, pre-flight, live, manual -- because
        every one of them prices the whole universe, and a price observed at
        9:45 is no less real for having been observed by a read-only scan. The
        archive's natural key includes the stream and slot, so the streams never
        collide (see dca_intraday_archive.append_observations).

        Best-effort by contract: a failure is recorded on the run_state as
        ``intraday_archive.error`` and never raised. This runs after every other
        artifact has already been written, so nothing a live run depends on is
        downstream of it; ``run_state.json`` is then re-persisted so the summary
        is durable, and even THAT re-write is guarded -- the already-valid
        run_state on disk must survive a failure to annotate it.
        """
        observations = observations_from_signal_rows(
            artifacts.get("signal_rows") or [],
            run_classification=artifacts.get("run_classification") or {},
            generated_at=artifacts.get("generated_at"),
            data_source=str(artifacts.get("data_source") or ""),
            trading_day=artifacts.get("trading_day"),
        )
        snapshot_path = write_run_snapshot(out, observations)
        if snapshot_path:
            paths["observations"] = snapshot_path
        summary = archive_observations_safely(observations)
        summary["observations"] = len(observations)
        summary["run_snapshot"] = snapshot_path
        artifacts["intraday_archive"] = summary
        if not summary.get("ok"):
            _log.warning(
                "intraday observation archive write failed for %s: %s",
                artifacts.get("run_id"),
                summary.get("error"),
            )
        try:
            atomic_write_text(
                run_state_path, json.dumps(artifacts, indent=2, sort_keys=True)
            )
        except (OSError, TypeError, ValueError) as exc:  # noqa: BLE001
            _log.warning(
                "could not annotate run_state with intraday archive summary: %s", exc
            )

    @staticmethod
    def _restamp_submitted_aggregates(artifacts: Dict[str, Any]) -> None:
        """Recompute ``artifacts``' placement aggregates from its own order rows.

        ``orders_submitted_count``/``order_submit_status``/``counts.submitted``
        are derived from ``broker_outcomes``+``buy_candidates`` via
        ``_submitted_keys()`` everywhere else in this file; this recomputes them
        the same way so a rewritten artifact's failure envelope stays
        internally consistent with the rows it actually carries.

        A zero result changes nothing: an artifact whose rows genuinely contain
        no placed order keeps whatever non-submitted status it already had
        (``no_candidates``, ``budget_exhausted``, ...), which is the honest
        value there.

        Shared by BOTH preservation branches of ``_blocked_live_result``
        (Codex review, PR #593: "Recompute placed counts on wholesale retry
        preservation"). It started life inline in the malformed-state graft
        branch only (Codex review, PR #538 discussion_r3837662134: "Complete
        the malformed-retry failure envelope"); the WHOLESALE branch copies an
        earlier artifact verbatim, so an artifact written before the placed-
        status widening -- a ``pending``/``unconfirmed`` row recorded next to
        ``orders_submitted_count == 0`` -- carried those zeros forward while
        the widened ``_submitted_keys()`` recognised the row and wrote it to
        ``submitted_orders.csv``, publishing a run state and summary claiming
        zero orders beside a canonical CSV holding one.
        """
        submitted_count = len(
            _submitted_keys(artifacts.get("broker_outcomes") or {}, artifacts.get("buy_candidates") or [])
        )
        if submitted_count:
            artifacts["orders_submitted_count"] = submitted_count
            artifacts["order_submit_status"] = ORDER_SUBMIT_STATUS_SUBMITTED
            # isinstance, not ``or {}``: a TRUTHY non-dict counts (a list, a
            # string) survives the falsy guard and then makes ``**`` raise
            # TypeError. That escapes _blocked_live_result() entirely, so this
            # retry never publishes its blocked classification and drops into
            # startup-failure handling with the earlier state left describing
            # the stale attempt -- a worse outcome than the aggregate drift
            # this helper exists to correct (Codex review, PR #593: "Validate
            # counts before restamping aggregates").
            #
            # Normalising rather than re-routing to the graft path: a non-dict
            # counts carries nothing recoverable, so there is nothing for the
            # graft to preserve, and replacing it with a real mapping also
            # repairs the field for every downstream reader instead of leaving
            # the malformed value in the published artifact.
            existing_counts = artifacts.get("counts")
            artifacts["counts"] = {
                **(existing_counts if isinstance(existing_counts, dict) else {}),
                "submitted": submitted_count,
            }

    def _blocked_live_result(
        self,
        now: datetime,
        block_reason: str,
        signals: Sequence[MorningSignal],
        source: str,
        *,
        error_class: Optional[str] = None,
        raw_recorder: Optional["_RawInputRecorder"] = None,
    ) -> Dict[str, Any]:
        """Write and return a fail-closed live-run result before broker placement.

        This is used for scheduler-wiring safety failures (for example no broker
        adapter or the wrong live account) where no ordering should proceed, but
        operators still need a durable run_state explaining why the scheduled
        live stream did not submit orders. Routed through the normal
        build_artifacts()/write_artifacts() pipeline -- mirroring the preflight-
        missing-block path above -- rather than a hand-rolled stub, so the
        persisted state still carries run_id, generated_at, counts, and the
        signal/candidate arrays already evaluated for this run, and the latest
        scheduled pointer is still updated: watchdog/latest-pointer consumers
        see this as an intentional block rather than a malformed or missing run.

        ``raw_recorder``, when passed by a raw-persistence-failure caller, stamps
        the same ``raw_input_dir``/``raw_inputs_saved``/``raw_persistence_error``
        fields onto the blocked artifact that a normal completed run carries
        (see the end of ``run()``) -- this early-return path bypasses that
        block entirely, so without this the documented machine-readable
        raw-persistence fields would silently disappear from blocked scheduled
        states, even though ``block_reason``/``error_class`` still describe the
        failure in prose.

        Before writing a fresh blocked stub, checks whether this scheduled
        slot's run_state.json already exists and carries genuine PLACED-order
        evidence (a buy_candidates row or broker_outcomes entry whose status is
        any real placed/in-flight order status -- see
        ``_existing_placed_run_state``, not just "submitted") from an earlier
        attempt this run. If so, that placed evidence is kept, but THIS
        attempt's block_reason/error_class
        (and raw-persistence fields) are stamped onto it and it is rewritten to
        disk -- returning it completely unchanged (as before) hid a genuine
        current-attempt failure behind whatever status the earlier attempt
        happened to write, so a retry that failed for a NEW reason (e.g. a
        broker-side raw write) looked indistinguishable from a clean run to
        operators and watchdogs. An existing state that is unreadable, not a
        dict, or has no placed evidence is NOT preserved -- it is
        overwritten with the fresh block_reason, so a second, different wiring
        failure is never hidden behind a stale first one.

        The status set here is ``_existing_placed_run_state``'s, NOT the
        narrower exact-"submitted" ``_existing_submitted_run_state``'s this
        used to call (Codex review, PR #593: "Preserve every placed status on
        blocked retries"). An earlier attempt at this slot can legitimately
        have recorded a real order as "filled", "pending", or "unconfirmed"
        rather than "submitted"; with the narrow helper the preservation
        branch was skipped for all three and this method overwrote the
        canonical run_state.json (and the submitted-orders CSV) with a
        pre-placement blocked stub, erasing a LIVE order from cap accounting
        and from the duplicate-allocation evidence later runs read -- so a
        subsequent run could re-allocate against a budget that already has an
        order against it. Only ``"not_submitted_confirmation_required"`` is
        excluded, because ORDER_ATTEMPT_STATUSES documents it as a deliberate
        non-order.

        ``error_class`` optionally stamps a concrete, machine-checkable code
        (e.g. ``"missing_or_invalid_raw_inputs"``) alongside the human-readable
        ``block_reason``; omitted for the pre-existing wiring-failure callers.
        """
        existing = self._existing_placed_run_state(now)
        if existing is not None:
            existing_artifacts = existing["artifacts"]
            # Preserve the existing artifact WHOLESALE -- signal_rows together
            # with the buy_candidates/broker_outcomes they actually produced --
            # as one atomic unit, rather than rebuilding from this retry's
            # freshly recomputed `signals` and grafting only the old order
            # fields onto them. Prices/eligibility can change between
            # attempts, and pairing a submitted order with signal rows,
            # warnings, or budget data that did not actually produce it would
            # corrupt the audit trail (Codex review, PR #538
            # discussion_r3809442198: "Keep preserved orders paired with
            # their original signals"). Only this attempt's OWN failure
            # metadata -- block_reason/error_class/raw-persistence fields --
            # is layered on top of the untouched original artifact; nothing
            # about the earlier attempt's decision data is replaced or
            # partially grafted.
            #
            # Still requires run_classification/signal_rows/budget to be
            # present -- those are exactly what write_artifacts() reads
            # directly rather than via .get() (Codex review, PR #420:
            # "Validate preserved artifacts before rewriting them") -- a
            # structurally partial existing file falls back to a freshly
            # built artifact (the pre-atomic-fix behavior) instead of
            # KeyError-ing.
            #
            # Presence alone is not enough: an existing file can carry these
            # keys with "null" or wrong-typed values (e.g. a partially
            # written run_state from a crash mid-write), and write_artifacts()
            # subscripts/iterates them directly -- run_classification as a
            # mapping, signal_rows as a list, budget as a mapping -- so a
            # None or mistyped value raises instead of falling back to the
            # graft-based path the way an absent key does (Codex review, PR
            # #538 discussion_r3837489862: "Validate preserved field types
            # before rewriting retries"). run_id is checked for simple
            # truthiness since write_artifacts() only ever reads it as an
            # opaque string.
            # write_artifacts() also consumes buy_candidates/broker_outcomes
            # directly -- broker_outcomes.items() and c.get(...) on each
            # buy_candidates element -- so a valid non-dict broker_outcomes
            # (e.g. a nonempty string, which `or {}` does not catch since
            # it's truthy) or a non-list buy_candidates raises there instead
            # of falling back to the graft-based path, partially rewriting
            # the artifact before crashing (Codex review, PR #538:
            # "Validate preserved order collections before wholesale
            # reuse"). Absent is fine -- write_artifacts() defaults both to
            # empty -- only a PRESENT wrong-typed value disqualifies.
            # A valid CONTAINER (list/dict) is not enough on its own:
            # write_artifacts() calls dict(row)/row.get(...) on each
            # signal_rows/buy_candidates ELEMENT directly with no per-element
            # type guard (unlike broker_outcomes, whose consumer already
            # checks isinstance(outcome, dict) inline) -- a malformed element
            # alongside an otherwise-valid submitted row (e.g.
            # [submitted_row, None]) still raised mid-rewrite instead of
            # falling back to the graft-based path (Codex review, PR #538:
            # "Validate collection elements before preserving retries").
            existing_signal_rows = existing_artifacts.get("signal_rows")
            existing_buy_candidates = existing_artifacts.get("buy_candidates", [])
            structurally_sound = (
                isinstance(existing_artifacts.get("run_classification"), dict)
                and isinstance(existing_signal_rows, list)
                and all(isinstance(row, dict) for row in existing_signal_rows)
                and isinstance(existing_artifacts.get("budget"), dict)
                and bool(existing_artifacts.get("run_id"))
                and isinstance(existing_buy_candidates, list)
                and all(isinstance(row, dict) for row in existing_buy_candidates)
                and isinstance(existing_artifacts.get("broker_outcomes", {}), dict)
            )
            if structurally_sound:
                artifacts = copy.deepcopy(existing_artifacts)
                # Same recomputation the malformed-state branch below performs,
                # for the same reason: the copied aggregates can predate the
                # widened placed-status logic (Codex review, PR #593:
                # "Recompute placed counts on wholesale retry preservation").
                # An earlier artifact holding a `pending`/`unconfirmed` row
                # alongside `orders_submitted_count == 0` is exactly the shape
                # that reached submitted_order_rows() -- which recognises the
                # row and writes it to submitted_orders.csv -- while the run
                # state and summary preserved beside it still claimed zero
                # orders. See _restamp_submitted_aggregates.
                self._restamp_submitted_aggregates(artifacts)
            else:
                # The existing artifact's own run_classification/signal_rows/
                # budget are malformed, so it cannot be preserved wholesale.
                # Its buy_candidates/broker_outcomes are still grafted onto
                # THIS attempt's fresh artifact -- NOT because they were
                # produced by this attempt's own signals (they explicitly
                # were not: `preserved_from_earlier_attempt` says so), but
                # because stashing them exclusively under a field no
                # production reader consults (`_existing_submitted_run_state`
                # and every duplicate-order guard built on it read only
                # top-level buy_candidates/broker_outcomes) made a genuinely
                # submitted order invisible to later retries, risking exactly
                # the double-submission this whole preservation path exists
                # to prevent (Codex review, PR #538 discussion_r3837662133:
                # "Keep unverified submissions visible to later retry
                # guards"). The mismatched-signal-pairing concern the
                # earlier graft-avoidance fix addressed (Codex review, PR
                # #538 discussion_r3837630722) is real but strictly lower
                # severity than a live duplicate buy, and is mitigated here
                # by never claiming these rows came from THIS attempt's own
                # signal_rows/budget -- only their own identity (ticker,
                # submitted amount, status) travels forward, tagged as
                # carried over from the earlier attempt.
                artifacts = self.build_artifacts(now, signals, source, manual=False)
                preserved_candidates = existing_artifacts.get("buy_candidates")
                if isinstance(preserved_candidates, list):
                    # A malformed ELEMENT (not just a non-list container) is
                    # just as fatal to write_artifacts()'s buy_candidates
                    # comprehension as it is to the wholesale-preservation
                    # path above -- this graft feeds the SAME write_artifacts()
                    # call. Drop only the malformed elements rather than the
                    # whole list, so one corrupted sibling row doesn't also
                    # discard an otherwise-valid submitted row from this same
                    # earlier attempt.
                    preserved_candidates = [row for row in preserved_candidates if isinstance(row, dict)] or None
                else:
                    preserved_candidates = None
                preserved_outcomes = existing_artifacts.get("broker_outcomes")
                if not isinstance(preserved_outcomes, dict):
                    preserved_outcomes = None
                if preserved_candidates:
                    artifacts["buy_candidates"] = preserved_candidates
                    artifacts["counts"]["buy_candidates"] = len(preserved_candidates)
                if preserved_outcomes:
                    artifacts["broker_outcomes"] = preserved_outcomes
                if preserved_candidates or preserved_outcomes:
                    artifacts["preserved_from_earlier_attempt"] = {
                        "reason": (
                            "existing run_state.json for this slot had a "
                            "malformed run_classification/signal_rows/budget, "
                            "so these buy_candidates/broker_outcomes are "
                            "carried forward from that earlier attempt "
                            "rather than produced by this attempt's own "
                            "(freshly rebuilt) signals/budget"
                        ),
                    }
                    # write_artifacts()/downstream consumers derive
                    # orders_submitted_count/order_submit_status from
                    # broker_outcomes+buy_candidates via _submitted_keys()
                    # everywhere else in this file -- computing them the
                    # same way here keeps this fallback's failure envelope
                    # internally consistent (a run stamped
                    # failed_after_partial_submission must actually show a
                    # nonzero submission count) instead of leaving whatever
                    # build_artifacts() defaulted them to (Codex review, PR
                    # #538 discussion_r3837662134: "Complete the
                    # malformed-retry failure envelope"). Lifted into
                    # _restamp_submitted_aggregates so the wholesale branch
                    # above shares this exact recomputation rather than
                    # growing a second one of its own (Codex review, PR #593:
                    # "Recompute placed counts on wholesale retry
                    # preservation").
                    self._restamp_submitted_aggregates(artifacts)
            artifacts["status"] = "blocked"
            artifacts["block_reason"] = block_reason
            # Stale failure metadata from whatever the PREVIOUS attempt at
            # this slot recorded must never bleed into THIS attempt's own
            # outcome (Codex review, PR #420: "Clear stale failure metadata
            # when rewriting a retry") -- cleared unconditionally, then only
            # what applies to the CURRENT attempt is set back below.
            #
            # raw_input_dir/raw_inputs_saved are the exception: they are NOT
            # unconditionally dropped. The original raw files that justified
            # an already-submitted order are still on disk even when THIS
            # retry's own failure is a non-raw wiring problem (e.g. a missing
            # broker adapter) that never touched raw persistence at all --
            # unconditionally erasing those references broke the audit trail
            # for a submission that raw persistence had nothing to do with
            # (Codex review, PR #538 discussion_r3837489853: "Preserve
            # original raw-input references on blocked retries"). Only THIS
            # attempt's own raw_recorder, when it actually produced values,
            # takes precedence over the preserved original.
            artifacts.pop("error_class", None)
            artifacts.pop("raw_persistence_error", None)
            if error_class is not None:
                artifacts["error_class"] = error_class
            if raw_recorder is not None and (
                raw_recorder.saved_filenames or raw_recorder.error
            ):
                artifacts["raw_input_dir"] = str(raw_recorder.raw_dir)
                # Merge with whatever the PRESERVED (earlier, submitting)
                # attempt already recorded rather than overwriting it outright:
                # those filenames are the raw evidence that actually justified
                # the submitted order, and this retry's own raw_recorder only
                # ever sees ITS OWN attempt's writes. Blindly replacing the list
                # with just this retry's (possibly empty-on-error, or partial)
                # filenames made the original submission's raw inputs vanish
                # from the audit trail even though the files are still on disk
                # (Codex review, PR #538: "Retain earlier raw filenames when
                # recording a failed retry").
                preserved_raw_inputs_saved = existing_artifacts.get("raw_inputs_saved")
                if isinstance(preserved_raw_inputs_saved, list):
                    merged_raw_inputs_saved = list(preserved_raw_inputs_saved)
                    for filename in raw_recorder.saved_filenames:
                        if filename not in merged_raw_inputs_saved:
                            merged_raw_inputs_saved.append(filename)
                    artifacts["raw_inputs_saved"] = merged_raw_inputs_saved
                else:
                    artifacts["raw_inputs_saved"] = raw_recorder.saved_filenames
                if raw_recorder.error:
                    artifacts["raw_persistence_error"] = raw_recorder.error
            # else: leave whatever raw_input_dir/raw_inputs_saved the
            # preserved artifact already carried (from the original
            # submitting attempt) untouched.
            # The preserved artifact (wholesale OR grafted) still carries the
            # EARLIER attempt's success classification (e.g. "submitted")
            # even though THIS attempt is being rewritten as blocked --
            # derive_valid_scheduled_evidence() reads
            # run_classification_outcome/is_trading_failure/
            # scheduled_evidence_denied, not `status`, so leaving the old
            # success classification in place made a failed retry (e.g. a
            # missing broker on THIS attempt) look like valid scheduled
            # evidence to watchdog/recovery consumers even though `status`
            # said "blocked" (Codex review, PR #538
            # discussion_r3837489852: "Deny scheduled evidence for preserved
            # blocked retries" -- the same theme as the
            # raw_persistence_partial_block stamping elsewhere in this file,
            # but for this separate earlier-success retry path). Stamped
            # unconditionally: reaching this branch at all means an earlier
            # attempt succeeded and THIS attempt is being recorded as
            # blocked, so THIS run is never valid scheduled evidence.
            artifacts["run_classification_outcome"] = RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION
            artifacts["is_trading_failure"] = True
            # deny_scheduled_evidence() sets both the top-level
            # scheduled_evidence_denied flag AND the nested
            # run_classification.valid_scheduled_evidence=False. A bare
            # top-level assignment left the preserved artifact's nested
            # run_classification.valid_scheduled_evidence at its earlier
            # `true` value, so scripts/generate_credit_usage_proxy_report.py's
            # _classify_run (which reads the nested field directly) still
            # counted this blocked retry as valid scheduled evidence (Codex
            # review, PR #538 discussion_r3837630717: "Clear nested evidence
            # validity on partial failures").
            artifacts.update(deny_scheduled_evidence(artifacts))
            # write_artifacts() derives the same dated-slot output directory
            # from artifacts["run_classification"]/now, so this overwrites
            # the same run_state.json (+ CSVs) and refreshes the scheduled
            # latest pointer to it -- downstream consumers still see a valid,
            # intentional run for this slot, now describing the CURRENT
            # attempt's failure (with the earlier submission, and the exact
            # signal rows that produced it, still intact and paired together)
            # rather than a stale, unchanged one.
            paths = self.write_artifacts(artifacts, now)
            if self.mode == MORNING_LIVE_MODE and self.trades:
                _run_drift_check(Path(paths["run_state"]).parent)
            return {"artifacts": artifacts, "paths": paths}
        artifacts = self.build_artifacts(now, signals, source, manual=False)
        artifacts["status"] = "blocked"
        artifacts["block_reason"] = block_reason
        if error_class is not None:
            artifacts["error_class"] = error_class
        artifacts["broker_outcomes"] = {}
        if raw_recorder is not None:
            artifacts["raw_input_dir"] = str(raw_recorder.raw_dir)
            artifacts["raw_inputs_saved"] = raw_recorder.saved_filenames
            if raw_recorder.error:
                artifacts["raw_persistence_error"] = raw_recorder.error
        # This path writes and returns before _run_impl's classification call, so
        # it stamps the outcome itself (Codex review).
        _apply_blocked_before_placement_fields(artifacts)
        paths = self.write_artifacts(artifacts, now)
        # A blocked scheduled live run may have committed signals but placed no
        # orders — exactly the scenario the drift detector guards against. Run the
        # check so operators receive an immediate alert rather than a silent gap.
        if self.mode == MORNING_LIVE_MODE and self.trades:
            _run_drift_check(Path(paths["run_state"]).parent)
        return {"artifacts": artifacts, "paths": paths}

    def _existing_run_state_with_statuses(
        self, now: datetime, statuses: frozenset
    ) -> Optional[Dict[str, Any]]:
        """Return the existing scheduled-slot run_state if it has a buy_candidates
        or broker_outcomes entry whose status is one of ``statuses``.

        Returns ``None`` (nothing to preserve) when the file is missing, unreadable,
        not a JSON object, or has no matching entry. Shared by
        ``_existing_submitted_run_state`` (status == "submitted" only) and
        ``_existing_placed_run_state`` (any real placed/in-flight order status --
        see ``ORDER_ATTEMPT_STATUSES``); keep the status set as the only
        difference between those two so neither caller's contract drifts from
        the other's.
        """
        state_path = self.run_state_dir(now) / "run_state.json"
        if not state_path.exists():
            return None
        try:
            existing_artifacts = json.loads(state_path.read_bytes())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            _log.warning(
                "existing run_state.json at %s is corrupted or unreadable (%s); "
                "overwriting with a fresh blocked stub",
                state_path,
                exc,
            )
            return None
        if not isinstance(existing_artifacts, dict):
            return None
        # .get(key, default) only substitutes the default when the key is
        # ABSENT; a structurally corrupted but parseable existing file with
        # "buy_candidates": null or "broker_outcomes": [] still returns that
        # non-list/non-dict value, and iterating/.values()-ing it would raise
        # instead of falling through to the fresh blocked stub below.
        buy_candidates = existing_artifacts.get("buy_candidates")
        if not isinstance(buy_candidates, list):
            buy_candidates = []
        broker_outcomes = existing_artifacts.get("broker_outcomes")
        if not isinstance(broker_outcomes, dict):
            broker_outcomes = {}
        has_match = any(
            isinstance(row, dict) and row.get("status") in statuses for row in buy_candidates
        ) or any(
            isinstance(outcome, dict) and outcome.get("status") in statuses
            for outcome in broker_outcomes.values()
        )
        if not has_match:
            return None
        return {"artifacts": existing_artifacts, "paths": {"run_state": str(state_path)}}

    def _existing_submitted_run_state(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Return the existing scheduled-slot run_state if it has submitted evidence.

        Returns ``None`` (nothing to preserve) when the file is missing, unreadable,
        not a JSON object, or has no buy_candidates/broker_outcomes entry with
        status == "submitted". Deliberately narrower than
        ``_existing_placed_run_state``: each remaining caller of this method
        specifically reasons about "submitted" evidence, so widening this
        method's own status set would silently change behavior for them too.
        The two remaining callers, and why the narrow contract is right for
        each:

        * ``_place_broker_orders``'s ``prior_submitted_amounts`` seed, which
          re-filters every preserved row on ``status != "submitted"`` itself
          before using its notional -- a wider status set here would change
          nothing except the work done.
        * the market-closed/outside-window block in ``_maybe_block_for_holiday``,
          which returns the existing artifact untouched.

        ``_blocked_live_result`` used to be a third caller and is deliberately
        NOT one any more: it rewrites (and can therefore erase) the canonical
        artifact, so it needs the full placed-status set (Codex review, PR
        #593: "Preserve every placed status on blocked retries").
        """
        return self._existing_run_state_with_statuses(now, frozenset({"submitted"}))

    def _existing_placed_run_state(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Return the existing scheduled-slot run_state if it has evidence of any
        real placed/in-flight order -- not just ``status == "submitted"``.

        Uses every status in ``ORDER_ATTEMPT_STATUSES`` except
        ``"not_submitted_confirmation_required"``, which that constant's own
        docstring documents as a deliberate non-order (a confirmation-required
        response, not a placed order). A scheduled retry's prior attempt can
        legitimately have left the order in "filled", "pending", or
        "unconfirmed" -- not only "submitted" -- and each of those is a real
        order this stream must never treat as safe to discard raw evidence
        for (Codex review: "Preserve all placed statuses before cleaning raw
        files").
        """
        return self._existing_run_state_with_statuses(now, PLACED_ORDER_STATUSES)

    def _credit_decline_already_recorded_this_slot(
        self, now: datetime, ticker: str
    ) -> bool:
        """True if THIS slot's own earlier attempt today already recorded a
        cumulative-credit decline for ``ticker``.

        A scheduled retry re-invokes ``_compute_top_up`` from scratch and
        would otherwise recompute the same decline and apply it AGAIN on top
        of the pending balance its own earlier attempt this slot already
        durably wrote via ``CumulativeCreditLedger.record`` -- inflating the
        credit purely from being retried, not from any real additional price
        decline (unlike the crosses-to-a-real-order case, which defers its
        ledger write to ``_place_broker_orders`` and is naturally retry-safe
        via the existing submitted-order duplicate check). Reads the same
        dated-slot ``run_state.json`` ``_existing_submitted_run_state`` reads,
        but regardless of whether anything was ever submitted, since a
        pending-only credit never sets a "submitted" status.
        """
        state_path = self.run_state_dir(now) / "run_state.json"
        if not state_path.exists():
            return False
        try:
            existing_artifacts = json.loads(state_path.read_bytes())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return False
        if not isinstance(existing_artifacts, dict):
            return False
        buy_candidates = existing_artifacts.get("buy_candidates")
        if not isinstance(buy_candidates, list):
            return False
        return any(
            isinstance(row, dict)
            and row.get("ticker") == ticker
            and row.get("cumulative_credit_decline_usd") is not None
            for row in buy_candidates
        )

    def _maybe_block_for_holiday(self, now: datetime, *, manual: bool) -> Optional[Dict[str, Any]]:
        """Return a blocked-run result when ``now`` falls on a weekend or NYSE holiday.

        A cron fire on a weekend or full market closure has no real session to
        evaluate. Unlike near_close_dca.MARKET_HOLIDAYS (scoped to the afternoon
        live-test window and hand-maintained), this stream runs indefinitely with
        no end date, so it uses the computed nyse_holidays() calendar to stay
        correct into future years without manual updates. Builds the blocked
        result through the normal artifact pipeline (``build_artifacts``) so the
        persisted run_state carries the standard shape (run_id, generated_at,
        counts, signal/candidate arrays) and watchdog/reporting consumers see a
        well-formed blocked run rather than a minimal stub. The latest-run
        pointer(s) ARE refreshed to this blocked state -- same as every other
        scheduled no-op/blocked path in this file -- so a same-day-guard consumer
        checking "did today's scheduled task fire" sees today's (blocked)
        evidence instead of a stale pointer left over from the last real trading
        session. Returns None on a normal trading day.
        """
        trading_day = now.astimezone(EASTERN).date()
        is_weekend = trading_day.weekday() >= 5
        is_holiday = trading_day in nyse_holidays(trading_day.year)
        approved = self.config.approved_test_dates
        # Countdown/expiry alarm for the configured trading window (scheduled
        # live runs only). The load-failed sentinel is excluded -- that case
        # already logs its own fail-closed warning and its date.min content
        # would render a nonsense "window ended 0001-01-01" message.
        if (
            self.mode == MORNING_LIVE_MODE
            and not manual
            and approved
            and approved != _APPROVED_DATES_LOAD_FAILED
        ):
            warn_if_window_expiring(approved, trading_day, log=_log)
        is_outside_window = (
            self.mode == MORNING_LIVE_MODE
            and bool(approved)
            and trading_day not in approved
        )
        # Reject a scheduled live invocation outside its declared
        # window_start_et/window_end_et slot window (Codex review: "Reject
        # scheduled runs outside their slot window"). self.profile carries
        # these bounds for every live stream built on this shared method
        # (morning, midday, market-open, intraday), but until now they were
        # only ever copied into run_classification for display -- nothing
        # compared the actual firing time against them. Without this, a
        # delayed non-manual invocation (e.g. a stuck scheduler retrying
        # hours late) would evaluate the historical decision-time anchor,
        # quote the CURRENT broker price/spread, place an order well outside
        # the approved window, and still label the artifact as the on-time
        # scheduled run. Scoped identically to is_outside_window above:
        # watchlist/preflight modes and manual/recovery runs are unaffected.
        is_outside_time_window = False
        if self.mode == MORNING_LIVE_MODE and not manual:
            window_start_et = self.profile.get("window_start_et")
            window_end_et = self.profile.get("window_end_et")
            if window_start_et and window_end_et:
                now_et_time = now.astimezone(EASTERN).time()
                is_outside_time_window = not (
                    time.fromisoformat(window_start_et)
                    <= now_et_time
                    <= time.fromisoformat(window_end_et)
                )
        if manual or (
            not is_weekend
            and not is_holiday
            and not is_outside_window
            and not is_outside_time_window
        ):
            return None
        if is_weekend:
            block_reason = "weekend"
        elif is_holiday:
            block_reason = "market holiday"
        elif is_outside_window:
            block_reason = "outside approved trading window"
        else:
            block_reason = "outside scheduled slot window"
        is_preflight = self.mode == MORNING_PREFLIGHT_MODE
        # Guard: if this date slot already has PLACED-order evidence (e.g. the
        # 10:30 live run placed orders and then trading_window.json was briefly
        # malformed, making a retry fire with is_outside_window=True), never
        # overwrite that evidence — midday's prior-allocation scan reads this root
        # and losing submitted orders would allow a duplicate allocation.
        #
        # Uses _existing_placed_run_state, NOT the narrower exact-"submitted"
        # _existing_submitted_run_state: an order recorded as "filled",
        # "pending", or "unconfirmed" is just as real as a "submitted" one
        # (they are all in scheduled_artifacts.ORDER_ATTEMPT_STATUSES minus the
        # confirmation-required non-order), and the narrow helper let those
        # fall through this guard to have a pre-placement blocked stub written
        # over them below -- erasing exactly the duplicate-allocation evidence
        # this guard exists to protect (Codex review: "Preserve every placed
        # status on blocked retries", applied to this sibling call site too).
        existing = self._existing_placed_run_state(now)
        if existing is not None:
            self._write_same_day_pointers(
                self._scheduled_pointer_names(is_preflight),
                self._same_day_pointer_payload(existing["artifacts"], existing["paths"]["run_state"]),
                existing["paths"],
            )
            return existing
        if self.mode == MORNING_LIVE_MODE:
            self._seed_ledger_from_prior_runs(now)
            self._seed_same_day_cap_from_roots(now)
        artifacts = self.build_artifacts(now, [], "none", manual=manual)
        artifacts["status"] = "blocked"
        artifacts["block_reason"] = block_reason
        artifacts["trades"] = False
        artifacts["broker_outcomes"] = {}
        # This path writes the state and summary directly and returns before
        # _run_impl's classification call, so it stamps the blocked no-order
        # outcome itself. Without this both artifacts would omit the outcome
        # fields the contract requires of every no-order run (Codex review).
        artifacts.update(
            {
                "run_classification_outcome": RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
                "order_submit_status": ORDER_SUBMIT_STATUS_NO_ORDERS,
                "completion_reason": COMPLETION_REASON_DATE_GATE_BLOCKED,
                "error_class": None,
                "is_trading_failure": False,
                "orders_submitted_count": 0,
                "candidates_reaching_spread_gate": 0,
                "candidates_blocked_missing_spread": 0,
            }
        )
        out = self.run_state_dir(now)
        out.mkdir(parents=True, exist_ok=True)
        state_path = out / "run_state.json"
        # Overwrite any stale minimal stub: the blocked content is deterministic
        # for a given date and an older stub without run_id/counts/run_classification
        # would otherwise persist indefinitely. Submitted-evidence cases are
        # already returned above.
        atomic_write_text(state_path, json.dumps(artifacts, indent=2, sort_keys=True))
        paths = {"run_state": str(state_path)}
        # Codex review r3623389445: this early calendar/window gate bypasses
        # write_artifacts() entirely, so a scheduled LIVE block (weekend,
        # holiday, outside approved window) must still write the COMPLETE
        # root-level set -- run_summary.json, the signals CSV, and a header-only
        # submitted_orders.csv (no orders were ever attempted) -- or the run
        # directory fails scripts/scheduled_artifacts.py's live artifact contract
        # and detect_drift() sees a missing-CSV failure instead of a clean no-op.
        # "Blocked" is an outcome, not an excuse for a partial artifact set.
        if self.mode == MORNING_LIVE_MODE and self.trades:
            orders_path = write_submitted_orders_csv(out, [])
            paths["submitted_orders"] = str(orders_path)
            signals_path = out / self.signals_csv_name
            _write_csv(signals_path, artifacts.get("signal_rows") or [])
            paths["signals"] = str(signals_path)
            if self.writes_budget_ledger and artifacts.get("budget"):
                ledger_filename = _sanitize_budget_ledger_filename(
                    self.config.budget_ledger_filename
                )
                ledger_path = out / ledger_filename
                atomic_write_text(
                    ledger_path, json.dumps(artifacts["budget"], indent=2, sort_keys=True)
                )
                paths["budget_ledger"] = str(ledger_path)
            summary_path = out / "run_summary.json"
            atomic_write_text(
                summary_path,
                json.dumps(
                    build_run_summary(
                        artifacts, extra={"stream": self.stream, "mode": artifacts.get("mode")}
                    ),
                    indent=2,
                    sort_keys=True,
                ),
            )
            paths["run_summary"] = str(summary_path)
        self._write_same_day_pointers(
            self._scheduled_pointer_names(is_preflight),
            self._same_day_pointer_payload(artifacts, state_path),
            paths,
        )
        return {"artifacts": artifacts, "paths": paths}

    def _seed_ledger_from_prior_runs(self, now: datetime) -> None:
        """Fold already-submitted same-day/same-week spend into the ledger.

        ``MorningLedger`` is in-memory and this executor is normally instantiated
        fresh per invocation (a new cron process each time), so a retried or
        manually rerun live task would otherwise see an empty ledger and could
        re-spend a cap that an earlier run already spent against. This scans
        this stream's own live run-state artifacts under this stream's
        scheduled-folder naming for every day in the current ISO week up to and
        including today -- the fixed scheduled path for today specifically (a
        retry overwrites that same path, so the pre-retry file must be read
        before that happens) -- plus, when :attr:`scan_manual_reruns_for_ledger`
        is true, any ``manual_run_subdir`` reruns.

        Each already ``submitted``/``filled`` buy candidate's amount from an
        earlier day in the same ISO week folds into ``planned_by_week`` only
        (the daily bucket stays scoped to today, and that day already ended).
        This guards the weekly cap across a fresh process starting later in
        the week. A file dated today folds into both ``planned_by_day`` and
        ``planned_by_week`` (via ``record_hypothetical``) UNLESS this stream
        has ``cap_seed_roots`` configured, in which case ``_compute_top_up``'s
        cap_seed_roots scan already seeds today's own-stream spend from the
        same on-disk artifacts and this method must not seed it a second time.

        Guards against double-counting a run's own on-disk artifact if the
        SAME executor instance calls ``run()`` again later: ``run()`` adds its
        own ``run_id`` to ``self._seeded_prior_run_ids`` right after writing,
        so a later same-instance seeding pass recognizes and skips it (its
        spend is already reflected in ``self.ledger`` from that first call).
        A different (fresh) instance scanning the same file for the first time
        still folds it in correctly.

        Also records today's own submitted/filled rows from the fixed
        scheduled path (not manual reruns, which write to unique paths and are
        never overwritten) into ``self._prior_same_day_candidate_rows`` so
        ``run()`` can carry them forward into the artifact it's about to
        overwrite on a same-day scheduled retry.

        Watchlist/preflight modes never place real orders, so caps are never
        enforced there and this is a no-op outside live mode.
        """
        self._prior_same_day_candidate_rows = []
        if self.mode != MORNING_LIVE_MODE:
            return
        trading_day = now.astimezone(EASTERN).date()
        this_week = iso_week_key(trading_day)
        state_root = Path(self.state_root)
        folder_suffix = self.profile["folder_suffix"]
        # _compute_top_up's cap_seed_roots scan already seeds TODAY's own-stream
        # spend (both planned_by_day and planned_by_week) when cap_seed_roots is
        # configured AND actually covers the scheduled dated-slot path --
        # seeding it again here would double-count the same on-disk artifact.
        # Only skip today's amount in that case; a narrower cap_seed_roots
        # config that covers only manual_runs/ (explicitly supported by
        # _compute_top_up's own_manual_in_scan/own_scheduled_in_scan split)
        # never sees the scheduled slot, so this method must remain the one
        # seeding it or a scheduled retry could re-spend the full cap.
        # Not restricted to the morning stream (2026-08-02): _compute_top_up's
        # cap_seed_roots block above is stream-agnostic and now runs for any
        # stream with cap_seed_roots configured (midday included, since its
        # own cap ledger became genuinely cross-stream-pooled too), so this
        # double-count guard must recognize that for every such stream, not
        # just morning.
        _cap_seed_roots = getattr(self.config, "cap_seed_roots", None)
        _today_seeded_by_cap_roots = bool(_cap_seed_roots) and any(
            Path(self.state_root).is_relative_to(Path(r)) for r in _cap_seed_roots
        )

        scheduled_path = self.run_state_dir(now) / "run_state.json"
        candidate_paths: List[Tuple[Path, bool]] = []
        # Paths whose read/parse failure must fail closed (Codex review:
        # "Fail closed on unreadable prior-week cap artifacts"), tracked
        # separately from same-day/own-stream candidate_paths so a read/parse
        # failure on ONE of these specifically can fail closed without also
        # tripping on TODAY's own-stream miss, which has its own, separate
        # handling elsewhere (_compute_top_up's skip_sink). Despite the name,
        # this covers every EARLIER-weekday candidate discovered below,
        # including THIS stream's own fixed scheduled path for an earlier
        # day -- not just the cross-stream sibling-root scan further down.
        # An earlier fix only added cross-stream sibling paths here, missing
        # that the day-loop immediately below ALSO discovers this stream's
        # OWN earlier-weekday fixed paths (e.g. Monday's own run_state.json,
        # scanned again on Tuesday); an unreadable one of those silently
        # skipped without setting _cap_seed_scan_incomplete, since neither
        # this set nor _compute_top_up (which only ever covers TODAY) caught
        # it, letting Monday's own spend vanish from the weekly ledger
        # (Codex review: "Fail closed on unreadable own-stream prior-week
        # states").
        _earlier_weekday_sibling_paths: set = set()
        # Cache parsed manual-run states to avoid re-reading files in the main loop.
        _manual_state_cache: Dict[Path, Dict[str, Any]] = {}
        week_start = trading_day - timedelta(days=trading_day.weekday())
        day = week_start
        while day <= trading_day:
            path = state_root / f"{day.strftime('%Y%m%d')}_{folder_suffix}" / "run_state.json"
            if path.exists():
                is_scheduled_today = path == scheduled_path
                candidate_paths.append((path, is_scheduled_today))
                if not is_scheduled_today:
                    _earlier_weekday_sibling_paths.add(path)
            day += timedelta(days=1)

        # Cross-stream EARLIER-WEEKDAY seeding (Codex review, 2026-08-02:
        # "Include earlier weekdays in cross-stream cap seeding"). The loop
        # above only scans THIS stream's own root. _compute_top_up's own
        # cap_seed_roots block already covers every sibling stream for TODAY
        # specifically, but never for earlier days in the ISO week -- so
        # without this, a Tuesday midday run never saw Monday's market-open/
        # morning spend against the shared weekly cap, understating real
        # week-to-date spend. Glob each sibling root generically
        # (YYYYMMDD_*ET) rather than assuming this stream's own
        # folder_suffix, since every sibling stream uses its own slot-time
        # suffix (per-slot state-file guarantee, docs/safeguards.md).
        # Deliberately scoped to day < trading_day only: TODAY's cross-stream
        # seeding stays exclusively _compute_top_up's job, which tracks it
        # via a different dedup key (_cap_seeded_amounts) than this method's
        # own _seeded_prior_run_ids -- scanning today here too would let the
        # two independent dedup mechanisms double-count the same fill.
        if _cap_seed_roots:
            # Every known manual-subtree naming convention across EVERY
            # cap_seed_roots entry, including the current stream's OWN root
            # -- Codex review: "Scan nested manual runs when seeding prior
            # weekdays" and, in a later pass, "Include the current stream's
            # nested manual artifacts": the separate own-stream day-loop
            # above only checks the fixed scheduled-slot path (plus, for
            # morning specifically, manual_runs/ but only for TODAY via
            # scan_manual_reruns_for_ledger) -- so an EARLIER-weekday manual
            # recovery/rerun under the current stream's own root (e.g. a
            # morning manual_recovery/ backfill, or any midday
            # manual_reruns/manual_recovery from a prior weekday) was
            # invisible to the weekly ledger. Each manual subdirectory nests
            # its dated run folders the same way the scheduled root does
            # (e.g. midday_dca/manual_runs/20260721_1345ET/run_state.json),
            # so the same day_str + "_*ET" glob pattern applies one level
            # deeper.
            manual_subdirs = ("manual_runs", "manual_reruns", "manual_recovery")
            # The afternoon stream (near_close_dca / ce16ba6b) nests its
            # SCHEDULED artifacts one level deeper than every other stream --
            # config/run_classification.json's scheduled_run_dir is
            # ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/run_state.json, not a
            # bare ce16ba6b/YYYYMMDD_1530ET/ -- so the bare-root glob below
            # never finds afternoon's own scheduled runs at all (Codex
            # review: "Scan prior-week afternoon scheduled runs"). Harmless
            # no-op for every other stream, whose roots have no
            # scheduled_runs/ subdirectory.
            all_cap_roots = [Path(r) for r in _cap_seed_roots]
            earlier_day = week_start
            while earlier_day < trading_day:
                day_str = earlier_day.strftime("%Y%m%d")
                for cap_root in all_cap_roots:
                    if not cap_root.exists():
                        continue
                    # The bare root is scanned for every cap_seed_roots entry,
                    # INCLUDING the current stream's own root: for a stream
                    # whose profiles all share ONE root across MULTIPLE
                    # folder_suffix values (e.g. intraday_dca's 1130ET/1330ET/
                    # 1430ET slots), the day-loop above only matches THIS
                    # instance's own folder_suffix, so an earlier weekday's
                    # sibling-suffix fill under the same root (e.g. Monday's
                    # 11:30/2:30 runs, from a Tuesday 1:30 run's point of view)
                    # was never found by either mechanism and silently
                    # excluded from the pooled weekly cap (Codex review:
                    # "Seed the weekly cap from every intraday slot"). For a
                    # stream with exactly one folder_suffix per root (morning,
                    # midday, market-open, afternoon), the day-loop's own path
                    # is rediscovered here and simply deduped via
                    # _earlier_weekday_sibling_paths below -- a harmless no-op
                    # superset, not a double count.
                    search_dirs = [""] + list(manual_subdirs) + ["scheduled_runs"]
                    for subdir in search_dirs:
                        search_dir = cap_root / subdir if subdir else cap_root
                        if not search_dir.exists():
                            continue
                        # Wildcard-prefixed day match (not anchored to the
                        # start of the directory name): final_manual_review's
                        # own dated dirs are named
                        # budget_block_override_YYYYMMDD_HHMMET, not a bare
                        # YYYYMMDD_*ET (Codex review: "Include out-of-tree
                        # manual fills in pooled roots"). Harmless superset
                        # for every other root, whose dated dirs already
                        # start with YYYYMMDD and match either way.
                        for path in sorted(search_dir.glob(f"*{day_str}_*ET/run_state.json")):
                            if path in _earlier_weekday_sibling_paths:
                                continue
                            candidate_paths.append((path, False))
                            _earlier_weekday_sibling_paths.add(path)
                earlier_day += timedelta(days=1)
            # A cap_seed_roots entry can itself BE a leaf run directory
            # rather than a parent containing dated child directories --
            # e.g. default_pooled_cap_seed_roots()'s own perplexity_primary_
            # <YYYYMMDD>/manual_override_<HHMMET> glob resolves to the leaf
            # manual_override_<HHMMET> directory, whose run_state.json sits
            # directly inside it. The day-loop above only ever looks for a
            # CHILD directory matching *{day_str}_*ET, which such a leaf can
            # never satisfy (its own directory name carries neither the
            # date nor necessarily a matching _*ET suffix) -- silently
            # dropping the documented $207 from the weekly deployment total
            # on any later weekday in the same ISO week (Codex review, PR
            # #656 follow-up: "Seed leaf override states into prior-week
            # cap scans"). Consume the cap root's own run_state.json
            # directly when present; TODAY's own leaf state is intentionally
            # excluded here (bounded to STRICTLY earlier than trading_day,
            # matching the day-loop's own `earlier_day < trading_day`
            # scope) since _compute_top_up's cap_seed_roots scan already
            # seeds today's spend via the generic prior-allocation loader,
            # which has no such nesting assumption.
            for cap_root in all_cap_roots:
                leaf_path = cap_root / "run_state.json"
                if leaf_path in _earlier_weekday_sibling_paths or not leaf_path.exists():
                    continue
                try:
                    leaf_state = json.loads(leaf_path.read_bytes())
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    self._cap_seed_scan_incomplete = True
                    self._cap_seed_scan_incomplete_paths.append(str(leaf_path))
                    continue
                if not isinstance(leaf_state, dict):
                    self._cap_seed_scan_incomplete = True
                    self._cap_seed_scan_incomplete_paths.append(str(leaf_path))
                    continue
                leaf_day_str = leaf_state.get("trading_day")
                try:
                    leaf_day = date.fromisoformat(leaf_day_str) if leaf_day_str else None
                except (TypeError, ValueError):
                    leaf_day = None
                if leaf_day is None:
                    self._cap_seed_scan_incomplete = True
                    self._cap_seed_scan_incomplete_paths.append(str(leaf_path))
                    continue
                if not (week_start <= leaf_day < trading_day):
                    continue
                _manual_state_cache[leaf_path] = leaf_state
                candidate_paths.append((leaf_path, False))
                _earlier_weekday_sibling_paths.add(leaf_path)
        if self.scan_manual_reruns_for_ledger:
            manual_root = state_root / self.manual_run_subdir
            if manual_root.exists():
                filtered_manual: List[Tuple[Path, bool]] = []
                for p in sorted(manual_root.glob("*/run_state.json")):
                    try:
                        raw_state = json.loads(p.read_bytes())
                    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    _manual_state_cache[p] = raw_state
                    filtered_manual.append((p, False))
                candidate_paths.extend(filtered_manual)

        for path, is_scheduled_today in candidate_paths:
            if path in _manual_state_cache:
                state = _manual_state_cache[path]
            else:
                try:
                    state = json.loads(path.read_bytes())
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    # An unreadable/mid-write artifact from the earlier-weekday
                    # sibling scan is exactly as unverifiable as a same-day
                    # skip_sink miss -- fail closed rather than silently
                    # undercounting this week's shared spend (Codex review:
                    # "Fail closed on unreadable prior-week cap artifacts").
                    # A same-day/own-stream miss is NOT included here: that
                    # case is already covered by _compute_top_up's own
                    # skip_sink/fail-closed handling and flagging it again
                    # here would be redundant, not more correct.
                    if path in _earlier_weekday_sibling_paths:
                        self._cap_seed_scan_incomplete = True
                        self._cap_seed_scan_incomplete_paths.append(str(path))
                    continue
            # A valid JSON document whose root is a list, string, or other
            # non-object value (e.g. a partially recovered artifact) has no
            # .get() -- skip it rather than raising AttributeError and
            # crashing the retry before it can reach the fail-closed path.
            if not isinstance(state, dict):
                if path in _earlier_weekday_sibling_paths:
                    self._cap_seed_scan_incomplete = True
                    self._cap_seed_scan_incomplete_paths.append(str(path))
                continue
            file_trading_day_str = state.get("trading_day")
            try:
                file_trading_day = (
                    date.fromisoformat(file_trading_day_str) if file_trading_day_str else None
                )
            except (TypeError, ValueError):
                file_trading_day = None
            if file_trading_day is None:
                # Missing or unparseable trading_day on an otherwise-valid
                # JSON object is invalid required metadata, not a legitimate
                # out-of-scope artifact -- fail closed for an earlier-weekday
                # sibling path the same way a non-object root already does
                # (Codex review: "Fail closed when prior-week state metadata
                # is invalid"). A future-dated or different-week artifact
                # (checked below) is NOT this case: its metadata is valid, it
                # is simply out of this scan's scope, so it stays an ordinary
                # skip.
                if path in _earlier_weekday_sibling_paths:
                    self._cap_seed_scan_incomplete = True
                    self._cap_seed_scan_incomplete_paths.append(str(path))
                continue
            if file_trading_day > trading_day:
                continue
            if iso_week_key(file_trading_day) != this_week:
                continue
            # Applies to manual reruns AND the day-by-day scheduled/fixed-slot
            # paths alike: a replayed/backfilled run (this call's own `now`)
            # must not seed cap spend out of an artifact that was actually
            # written later in the day than the decision being reproduced --
            # e.g. a delayed scheduled invocation whose fixed YYYYMMDD_1030ET/
            # run_state.json ends up with a generated_at well after 10:30.
            if _run_state_after_boundary(state, now):
                continue
            run_id = state.get("run_id")
            if run_id is None:
                # Same reasoning as the missing-trading_day case above: a
                # valid JSON object with no run_id has invalid required
                # metadata, not merely nothing-to-seed-from (Codex review:
                # "Fail closed when prior-week state metadata is invalid").
                if path in _earlier_weekday_sibling_paths:
                    self._cap_seed_scan_incomplete = True
                    self._cap_seed_scan_incomplete_paths.append(str(path))
                continue
            already_seeded = run_id in self._seeded_prior_run_ids
            if already_seeded:
                # Ledger already has this run's spend; skip re-seeding, but
                # still load submitted rows from the scheduled path so a second
                # call to run() on the SAME executor instance (same run_id)
                # carries them forward into _prior_same_day_candidate_rows.
                if is_scheduled_today:
                    _bcs = state.get("buy_candidates") or []
                    if isinstance(_bcs, list):
                        submitted_rows_only = [
                            c for c in _bcs
                            if isinstance(c, dict) and c.get("status") in ("submitted", "filled")
                        ]
                        if submitted_rows_only:
                            self._prior_same_day_candidate_rows.extend(submitted_rows_only)
                continue
            total = 0.0
            submitted_rows = []
            _buy_candidates = state.get("buy_candidates") or []
            if not isinstance(_buy_candidates, list):
                _buy_candidates = []
            for candidate in _buy_candidates:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("status") not in ("submitted", "filled"):
                    continue
                amount = candidate.get("planned_amount_usd")
                if amount is None:
                    amount = candidate.get("hypothetical_amount_usd")
                if amount is None:
                    continue
                try:
                    amount = float(amount)
                except (TypeError, ValueError):
                    continue
                # Reject inf/-inf/nan for the same reason as the CSV cross-check
                # below: an infinite amount would seed infinite cap spend.
                if not math.isfinite(amount):
                    continue
                if amount > 0:
                    total += amount
                    submitted_rows.append(candidate)

            # Cross-check against the broker-order log written alongside
            # run_state.json: a Copilot Autofix pass flagged that the JSON
            # buy_candidates total could under-report actual submitted/filled
            # spend, so take whichever total is larger as the true
            # already-spent amount for cap-seeding purposes.
            # filled_orders.csv (the confirmed-fill source written by manual
            # midday overrides) is generally the more trustworthy signal, since
            # orders.csv is always written with blank avg_fill_price /
            # quantity_filled fields and would shadow real fill data -- but a
            # PARTIAL filled_orders.csv (some rows confirmed, others still only
            # in orders.csv as submitted-not-yet-confirmed) can under-report
            # relative to orders.csv's total. Take the larger of the two
            # qualifying totals, consistent with the same "take whichever is
            # larger" policy already applied against the JSON total above,
            # rather than treating the two files as mutually exclusive.
            filled_orders_csv = path.parent / "filled_orders.csv"
            orders_csv = path.parent / "orders.csv"
            # submitted_orders.csv is the CANONICAL evidence source for the
            # market-open/intraday streams, which have no in-repo Python code
            # writing buy_candidates/filled_orders.csv/orders.csv -- their
            # Claude-executor prompts write only run_state.json (via Gate 9)
            # and submitted_orders.csv (via Gate 6.5-style bookkeeping). Without
            # this, a valid earlier-weekday market-open/intraday run whose
            # evidence exists ONLY in submitted_orders.csv contributed $0 to
            # this week's shared ledger (Codex review: "Read canonical
            # submitted-order files for prior weekdays").
            submitted_orders_csv = path.parent / "submitted_orders.csv"
            # For an earlier-weekday sibling path, a CSV that EXISTS but
            # cannot be read/parsed must fail closed rather than silently
            # read as "no orders in this file" -- Codex review: "Fail closed
            # when prior-week order CSVs cannot be parsed". Scoped to
            # earlier-weekday sibling paths only, matching this method's
            # other fail-closed handling; today's own-stream scan has its
            # own separate skip_sink handling in _compute_top_up.
            _csv_skip_sink: Optional[List[Path]] = (
                [] if path in _earlier_weekday_sibling_paths else None
            )
            csv_total = _csv_qualifying_notional_total(
                filled_orders_csv, blank_status_means_filled=True, skip_sink=_csv_skip_sink
            )
            if orders_csv.exists() and orders_csv != filled_orders_csv:
                csv_total = max(
                    csv_total,
                    _csv_qualifying_notional_total(
                        orders_csv, blank_status_means_filled=False, skip_sink=_csv_skip_sink
                    ),
                )
            if submitted_orders_csv.exists() and submitted_orders_csv not in (
                filled_orders_csv,
                orders_csv,
            ):
                csv_total = max(
                    csv_total,
                    _csv_qualifying_notional_total(
                        submitted_orders_csv, blank_status_means_filled=False, skip_sink=_csv_skip_sink
                    ),
                )
            if _csv_skip_sink:
                self._cap_seed_scan_incomplete = True
                self._cap_seed_scan_incomplete_paths.extend(str(p) for p in _csv_skip_sink)
            total = max(total, csv_total)
            # Only cache this run_id as seeded -- and only credit its total to
            # the ledger -- when its OWN CSV scan this pass didn't fail. A run
            # whose CSV was transiently malformed/mid-write must be rescanned
            # on a later pass once it becomes readable: caching it here would
            # make the already_seeded check above skip it forever, even after
            # the file is repaired, permanently losing whatever spend exists
            # only in that CSV (Codex review: "Do not cache prior runs whose
            # CSV scan failed"). The overall week-level fail-closed poisoning
            # below already accounts conservatively for this run's possible
            # shortfall this pass; adding its partial total to the ledger now
            # would also risk double-counting it once a later successful
            # rescan adds the correct, complete total.
            if not _csv_skip_sink:
                self._seeded_prior_run_ids.add(run_id)
                if total > 0:
                    if file_trading_day == trading_day:
                        # Only seed today's own bucket here when cap_seed_roots isn't
                        # already doing it (see _today_seeded_by_cap_roots above) --
                        # otherwise the same on-disk artifact would be double-counted.
                        if not _today_seeded_by_cap_roots:
                            self.ledger.record_hypothetical(trading_day, total)
                    else:
                        # Cross-day-in-week seeding only bumps the shared weekly
                        # bucket; the daily bucket stays scoped to trading_day.
                        self.ledger.planned_by_week[this_week] = (
                            self.ledger.planned_by_week.get(this_week, 0.0) + total
                        )
            if is_scheduled_today and submitted_rows:
                self._prior_same_day_candidate_rows.extend(submitted_rows)

        if _earlier_weekday_sibling_paths:
            if self._cap_seed_scan_incomplete:
                # FAIL CLOSED: an earlier-weekday sibling artifact that could
                # not be read/parsed (see the skip-and-flag handling above)
                # means real week-to-date cross-stream spend is understated.
                # Force the shared weekly bucket to the cap itself -- zero
                # remaining headroom for the rest of this run -- rather than
                # place against an unproven, possibly too-low figure (same
                # fail-closed policy _compute_top_up already applies to a
                # same-day skip_sink miss; Codex review: "Fail closed on
                # unreadable prior-week cap artifacts"). Injects only the
                # INCREMENTAL delta needed to reach the ceiling (shared
                # tracking dict with _compute_top_up's cap_seed_roots block,
                # since both represent the same "this week's shared bucket is
                # untrustworthy" state) and records exactly how much was
                # injected, so a later successful scan (the else branch
                # below) can subtract it back out instead of leaving the
                # ledger permanently pinned once the transient failure clears
                # (Codex review: "Clear prior-week cap poisoning after a
                # recovered scan").
                week_poison_delta = max(
                    0.0, self.ledger.weekly_cap_usd - self.ledger.week_planned(trading_day)
                )
                if week_poison_delta > 0:
                    self.ledger.planned_by_week[this_week] = (
                        self.ledger.week_planned(trading_day) + week_poison_delta
                    )
                    self._cap_seed_poison_week[this_week] = (
                        self._cap_seed_poison_week.get(this_week, 0.0) + week_poison_delta
                    )
            else:
                poisoned_week = self._cap_seed_poison_week.pop(this_week, 0.0)
                if poisoned_week > 0:
                    self.ledger.planned_by_week[this_week] = max(
                        0.0, self.ledger.week_planned(trading_day) - poisoned_week
                    )

    def _place_broker_orders(
        self,
        candidates: List[MorningSignal],
        trading_day: Any,
        broker: BrokerAdapter,
        top_up_outcomes: Optional[Dict[str, Any]],
        remaining_daily: float,
        remaining_weekly: float,
        manual: bool = False,
        data: Any = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Attempt broker order placement for each EXEC_AUTO_BUY_ELIGIBLE candidate.

        Returns a dict mapping ticker → outcome dict. Each outcome has a ``status``
        key: ``submitted``, ``skipped``, ``blocked``, ``cap_exceeded``,
        ``not_submitted_confirmation_required``, or ``failed``.

        Cap enforcement: tracks running submitted spend against the pre-run
        remaining daily/weekly budget so orders that would exceed the configured
        caps are skipped rather than submitted.

        Top-up awareness: for midday candidates the notional is taken from the
        top-up outcome's ``incremental_order_amount`` rather than the signal's
        ``hypothetical_amount_usd``. Cross-stream prior orders are expected for
        guarded top-ups, but same-stream ref-id duplicate checks still run so
        retries cannot submit the same stream order twice.

        Spread breaker: when ``data`` exposes a live bid/ask quote (the same
        mechanism the afternoon stream uses), a candidate whose spread exceeds
        ``config.max_bid_ask_spread_pct`` is blocked before placement. Providers
        without a ``get_bid_ask`` hook (e.g. most test doubles) are silently
        skipped -- this is a best-effort circuit breaker, not a hard requirement.

        Review policy: scheduled runs (manual=False) skip review_market_buy and
        call place_market_buy directly after all internal checks pass, per the
        pre-authorization policy in config/budgets.json. Manual runs still call
        review_market_buy first.
        """
        if not candidates:
            return {}
        outcomes: Dict[str, Any] = {}
        # Published on the executor by reference BEFORE any placement, so a
        # failure part-way through the loop can still see every order that landed.
        # Without this the startup-failure writer would assert zero orders on a
        # run that had already placed some, and a later same-day top-up guard
        # would re-allocate that capital (Codex review).
        self._in_flight_broker_outcomes = outcomes
        self._in_flight_signals_by_ticker = {s.ticker: s for s in candidates}
        submitted_today = 0.0
        submitted_this_week = 0.0
        # Prior-submitted amounts by ticker from the CURRENT on-disk dated-slot
        # artifact (this stream's own earlier attempt this scheduled slot, if
        # any), read once before this call's outcomes overwrite that file.
        # Used below by the same-stream top-up-duplicate path to PRESERVE the
        # originally submitted notional rather than recording a freshly
        # recomputed (and generally smaller, once a same-day external fill has
        # landed) top-up incremental amount over it -- a retry's recomputed
        # incremental reflects an updated prior-allocation total, not what was
        # actually placed with the broker on the earlier attempt (Codex
        # review, PR #88: "Preserve original scheduled notional across top-up
        # retries").
        prior_submitted_amounts: Dict[str, float] = {}
        if not manual and now is not None:
            existing = self._existing_submitted_run_state(now)
            if existing is not None:
                for row in existing["artifacts"].get("buy_candidates", []):
                    if not isinstance(row, dict) or row.get("status") != "submitted":
                        continue
                    row_ticker = row.get("ticker")
                    if not row_ticker:
                        continue
                    amt = row.get("submitted_amount_usd")
                    if amt is None:
                        amt = row.get("planned_amount_usd")
                    if amt is None:
                        continue
                    try:
                        amt = float(amt)
                    except (TypeError, ValueError):
                        continue
                    if amt > 0:
                        prior_submitted_amounts[row_ticker] = amt
        # Snapshotted lazily (see below) so adapters that immediately reflect
        # placed orders don't cause double-subtraction (reserved_buying_power
        # tracks THIS batch only). None means "not fetched yet" -- fetched on
        # first actual need so a batch where every candidate is blocked by an
        # earlier, cheaper check (per-order cap, duplicate, tradability) never
        # makes an unnecessary broker/account round-trip.
        batch_buying_power: Optional[float] = None
        reserved_buying_power = 0.0

        def raw_persistence_failed() -> bool:
            """Stop before submission when the decorated broker could not save an input."""
            recorder = getattr(broker, "_recorder", None)
            return bool(recorder is not None and recorder.error)

        # Spread verdict per ticker that actually reached the spread gate. Only
        # these tickers can be counted as blocked for want of a proven spread; a
        # ticker that never qualified on the ladder never reaches the gate at all
        # (config/scheduled_tasks.json:spread_data_availability_policy).
        spread_by_ticker: Dict[str, Dict[str, Any]] = {}

        def _mark_unreached_candidates_blocked(from_idx: int) -> None:
            """Stamp every candidate the loop never reached with a blocked outcome.

            A raw-persistence failure stops the loop with ``break`` and already
            records a blocked outcome for the CURRENT ticker -- but every
            candidate still queued behind it (not yet iterated to) previously
            got no ``outcomes`` entry at all. The post-loop cumulative-credit
            safeguard below only preserves a threshold-crossing credit when
            ``outcomes.get(ticker)`` is a dict (it ``continue``s past anything
            else), and ``_compute_top_up`` deliberately leaves a
            threshold-crossing credit unrecorded until placement resolves one
            way or the other -- so a later, not-yet-evaluated candidate whose
            credit had already crossed the order threshold this run had its
            new decline silently dropped instead of preserved as pending for a
            future slot (Codex review: "Preserve credits for candidates after
            the failed one"). Reuses the same generic "blocked" status/
            skip_reason shape every other pre-placement stop in this loop
            already uses, so downstream consumers of ``outcomes`` need no new
            status to handle.
            """
            for later_signal in candidates[from_idx + 1 :]:
                outcomes.setdefault(
                    later_signal.ticker,
                    {
                        "status": "blocked",
                        "skip_reason": "raw input persistence failed before this candidate could be evaluated",
                    },
                )

        for _candidate_idx, signal in enumerate(candidates):
            ticker = signal.ticker
            top_up = (top_up_outcomes or {}).get(ticker)

            # Determine the real order notional from the top-up outcome if active.
            if top_up is not None:
                if top_up.top_up_skipped or top_up.incremental_order_amount <= 0:
                    outcomes[ticker] = {
                        "status": "skipped",
                        "skip_reason": f"top-up guard: {top_up.top_up_guard_result}",
                    }
                    continue
                notional = round(top_up.incremental_order_amount, 2)
            else:
                notional = signal.hypothetical_amount_usd

            # Hard per-order ceiling from config/budgets.json circuit_breakers.max_per_order_usd.
            # A pure local computation (no broker round-trip), so it stays ahead
            # of the duplicate check below -- an over-ceiling candidate must
            # never even reach the broker (Codex review #25 on PR #22).
            _max_order_usd = self.config.max_per_order_usd
            if notional > _max_order_usd:
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"exceeds ${_max_order_usd:.0f} per-order ceiling",
                    "deliberate_skip": True,
                }
                continue

            # Bypass the broad same-day duplicate check only when there is an actual
            # cross-stream prior allocation (top-up guard result is not 'no_prior_run').
            # Even then, keep a same-stream ref-id check so retrying this stream after
            # it already submitted the ticker remains idempotent.
            #
            # This duplicate check runs BEFORE the daily/weekly cap checks
            # below: a duplicate places no new order, so it must never be
            # gated by checks that exist to decide whether a NEW placement
            # should proceed. Once cap-seeding has recovered an earlier
            # scheduled order that exactly consumes (or slightly exceeds, via
            # a freshly recomputed but stale notional) the remaining cap,
            # running the cap check first would record cap_exceeded and
            # overwrite the dated-slot artifact's real "submitted" row for
            # this ticker, erasing evidence a fresh executor's cap-seeding
            # scan depends on (Codex review, PR #88: "Preserve submitted
            # evidence when a cap-seeded retry is full").
            has_prior_allocation = (
                top_up is not None and top_up.top_up_guard_result != TOP_UP_NO_PRIOR_RUN
            )
            dup_order: Optional[Dict[str, Any]] = None
            try:
                if has_prior_allocation:
                    dup_order = _same_stream_order_exists(
                        broker, ticker, trading_day, self._idempotency_stream()
                    )
                    dup = dup_order is not None
                    duplicate_reason = "same-stream broker order exists"
                else:
                    dup = broker.same_day_strategy_order_exists(ticker, trading_day)
                    duplicate_reason = "same-day broker order exists"
            except Exception as exc:  # noqa: BLE001
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"duplicate-check failed: {exc}",
                }
                _log.warning("Duplicate-check error for %s: %s", ticker, exc)
                continue
            if raw_persistence_failed():
                # The duplicate response is an order-placement input. Do not
                # let a failed audit write turn into a later submission.
                # Record a blocked outcome for THIS ticker before breaking so
                # the post-loop cumulative-credit safeguard below (which reads
                # outcomes.get(ticker) to decide whether to preserve a pending
                # threshold-crossing credit) still sees an entry for it --
                # otherwise a raw-persistence stop right here would silently
                # drop that ticker's credit decline instead of settling or
                # preserving it (Codex review: "Preserve credit when raw
                # persistence stops placement").
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": "raw input persistence failed before placement",
                }
                # Also stamp every later, not-yet-evaluated candidate so their
                # own threshold-crossing credits are preserved rather than
                # silently dropped -- see _mark_unreached_candidates_blocked's
                # docstring (Codex review: "Preserve credits for candidates
                # after the failed one").
                _mark_unreached_candidates_blocked(_candidate_idx)
                break
            if dup:
                if has_prior_allocation and not manual:
                    # A same-stream top-up duplicate on a SCHEDULED retry means
                    # this stream already submitted/reserved the incremental
                    # order on an earlier attempt today, and a scheduled rerun
                    # overwrites the same date-keyed run_state.json path (see
                    # run_state_dir()), so there is no separate artifact for
                    # load_prior_same_day_allocations() to double-sum against.
                    # Persist the real broker order's id and amount so rewriting
                    # run_state on retry does not fabricate phantom values that
                    # downstream fill-lookup or reconciliation would reject or
                    # misread. Prefer the broker's own dollar_amount for this
                    # matched order (most authoritative -- straight from the
                    # broker); adapters that only support the legacy boolean
                    # duplicate-check contract normalize to an empty dup_order
                    # dict with no amount fields, so fall back to this
                    # process's own previously-recorded submission for the
                    # ticker (read into prior_submitted_amounts above) before
                    # finally falling back to the freshly recomputed
                    # `notional`, which can have shrunk since the original
                    # attempt if new prior-allocation context (e.g. an
                    # external fill) appeared in between.
                    try:
                        dup_amount = float(
                            (dup_order or {}).get("dollar_amount")
                            or (dup_order or {}).get("notional")
                            or 0
                        ) or prior_submitted_amounts.get(ticker, notional)
                    except (TypeError, ValueError):
                        dup_amount = prior_submitted_amounts.get(ticker, notional)
                    if not math.isfinite(dup_amount) or dup_amount <= 0:
                        dup_amount = prior_submitted_amounts.get(ticker, notional)
                    outcomes[ticker] = {
                        "status": "submitted",
                        "order_id": (dup_order or {}).get("id") or (dup_order or {}).get("order_id"),
                        "skip_reason": duplicate_reason,
                        "submitted_amount_usd": dup_amount,
                        # This amount is a DUPLICATE of an already-submitted
                        # order, not a fresh placement -- it's already
                        # reflected in the cap-seeding scan of this same
                        # dated-slot artifact (_compute_top_up), so run()'s
                        # submitted_candidates filter must exclude it from
                        # record_buy_candidates()'s ledger booking, or the
                        # amount gets double-counted every retry (Codex
                        # review, PR #88: "Avoid booking same-stream top-up
                        # retries a second time").
                        "duplicate_preserved": True,
                    }
                    submitted_today += dup_amount
                    submitted_this_week += dup_amount
                    # Do NOT add to reserved_buying_power: available_buying_power()
                    # is a fresh broker snapshot fetched (lazily, below) AFTER the
                    # earlier order already consumed it, so crediting dup_amount
                    # again here would double-subtract it and could incorrectly
                    # block a later candidate in this retry batch even when real
                    # buying power is sufficient (Codex review, PR #51: "Do not
                    # reserve buying power for existing duplicates").
                    #
                    # Settle the cumulative credit ledger for the RECOVERED
                    # order too. reconcile_preserved_credit already durably
                    # updated this slot's preserve entry BEFORE the original
                    # broker call, ahead of the confirmed-submission settle()
                    # below (which only runs on a fresh placement this run,
                    # not on this duplicate-recovery path). If the process
                    # crashed after that broker call succeeded but before the
                    # original attempt's own settle() ran, every subsequent
                    # retry would keep finding this same duplicate and never
                    # settle -- permanently stranding the preserved/minimum
                    # credit as pending even though its order already went
                    # through, letting a later pass (e.g. the 15:30 aggregate
                    # top-up) repurchase it (Codex review: "Settle reconciled
                    # preserves during duplicate recovery"). This second
                    # settle() call is NOT naturally idempotent: it subtracts
                    # dup_amount from the CURRENT shared pending balance and
                    # provenance pools regardless of whether the original
                    # attempt's own settle() already applied that exact
                    # order, so a retry that reaches this path after the
                    # original settle() succeeded (but crashed before its run
                    # artifact was persisted) would double-subtract the same
                    # dollars (Codex review: "Make duplicate-recovery
                    # settlement idempotent"). Pass the recovered broker
                    # order's own id so settle() can recognize and skip an
                    # order it has already applied.
                    # A supported legacy boolean-style duplicate checker
                    # normalizes to {} (see _same_stream_order_exists), so
                    # neither id field is ever present for it -- leaving
                    # recovered_order_id as None would disable settle()'s
                    # own order_id dedup (`if order_id:`) entirely, letting
                    # a later retry that finds the same boolean duplicate
                    # subtract the same order amount from the remaining
                    # credit again. Fall back to a synthetic key derived
                    # from this call's own deterministic (stream,
                    # trading_day, ticker) identity -- stable across
                    # retries of this exact recovery path even without a
                    # real broker order id (Codex review discussion_
                    # r3837494237: "Require an ID for idempotent duplicate
                    # settlement").
                    recovered_order_id = (
                        (dup_order or {}).get("id")
                        or (dup_order or {}).get("order_id")
                        or f"legacy-dup-{self._idempotency_stream()}-{trading_day.isoformat()}-{ticker}"
                    )
                    if top_up is not None and top_up.cumulative_credit_decline_usd is not None:
                        try:
                            self.credit_ledger.settle(
                                ticker,
                                trading_day.isoformat(),
                                dup_amount,
                                final_session=False,
                                stream=self._idempotency_stream(),
                                order_id=recovered_order_id,
                            )
                        except Exception as exc:  # noqa: BLE001
                            _log.warning(
                                "cumulative credit ledger settlement failed for %s: %s",
                                ticker,
                                exc,
                            )
                elif has_prior_allocation and manual:
                    # A MANUAL/recovery retry always writes to its own fresh,
                    # uuid-suffixed artifact directory (see run_id()) and never
                    # overwrites the original submission's run_state.json. If
                    # this were also recorded as "submitted" with a fresh
                    # notional, load_prior_same_day_allocations() would find
                    # both files and double-count the same broker order as two
                    # separate same-day allocations, understating what an
                    # afternoon/final top-up is still owed. The original
                    # submission's own artifact already carries the real
                    # notional, so this retry contributes nothing further.
                    outcomes[ticker] = {
                        "status": "skipped",
                        "skip_reason": f"{duplicate_reason} (duplicate top-up retry; already recorded by the original run)",
                        # The original (non-duplicate) attempt already settled
                        # this ticker's credit via _place_broker_orders'
                        # confirmed-submission path, so the post-loop credit
                        # preservation pass below must not treat this as an
                        # unresolved block and record the decline a second time.
                        "credit_already_resolved": True,
                    }
                else:
                    outcomes[ticker] = {"status": "skipped", "skip_reason": duplicate_reason}
                continue

            # Shared daily order-ATTEMPT ceiling (config/budgets.json
            # circuit_breakers.max_orders_per_day, seeded cross-stream in
            # _compute_top_up above) -- a pure local check, ahead of the
            # broker round-trip, mirroring the per-order ceiling above (Codex
            # review: "Enforce the shared daily order-count cap in new
            # executors"). Runs AFTER the duplicate-recovery check above, not
            # before: a preserved duplicate creates no new attempt (it was
            # already counted when the earlier attempt actually placed the
            # order), so enforcing this cap first could wrongly block the
            # recovery path and overwrite the dated-slot artifact's real
            # "submitted" row with a fabricated "blocked" one on a retry that
            # itself brought the shared attempt count to the cap (Codex
            # review: "Check for preserved duplicates before enforcing the
            # attempt cap").
            _max_orders_per_day = self.config.max_orders_per_day
            if _max_orders_per_day is not None and self.ledger.day_attempts(trading_day) >= _max_orders_per_day:
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"exceeds {_max_orders_per_day} order daily attempt cap",
                    "deliberate_skip": True,
                    "budget_blocked": True,
                }
                continue

            # Enforce daily/weekly caps against pre-run remaining budget.
            if submitted_today + notional > remaining_daily:
                outcomes[ticker] = {"status": "cap_exceeded", "skip_reason": "daily cap would be exceeded"}
                continue
            if submitted_this_week + notional > remaining_weekly:
                outcomes[ticker] = {"status": "cap_exceeded", "skip_reason": "weekly cap would be exceeded"}
                continue

            # This ticker qualified on the ladder and top-up rules, so it reaches
            # the spread gate. Every row that gets this far records its own spread
            # outcome, and only rows that get this far can be counted as blocked
            # for want of a spread (config/scheduled_tasks.json:
            # spread_data_availability_policy.evaluation_order).
            max_spread = self.config.max_bid_ask_spread_pct
            if data is not None and now is not None:
                try:
                    spread_pct = quote_spread_pct(data, ticker, now)
                except Exception as exc:  # noqa: BLE001
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": f"spread quote error: {exc}",
                        **spread_gate_fields(spread_pct=None, max_spread_pct=max_spread),
                    }
                    _log.warning("Spread quote error for %s: %s", ticker, exc)
                    continue
                if raw_persistence_failed():
                    # See the duplicate-check raw_persistence_failed() break above:
                    # record a blocked outcome so the post-loop credit safeguard
                    # still sees this ticker.
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": "raw input persistence failed before placement",
                    }
                    # Also stamp every later, not-yet-evaluated candidate --
                    # see _mark_unreached_candidates_blocked's docstring
                    # (Codex review: "Preserve credits for candidates after
                    # the failed one").
                    _mark_unreached_candidates_blocked(_candidate_idx)
                    break
                spread_fields = spread_gate_fields(
                    spread_pct=spread_pct, max_spread_pct=max_spread
                )
                if spread_pct is None and not manual:
                    # A scheduled run is pre-authorized to place without review, so
                    # the spread breaker is the only thing standing between an
                    # unproven quote and a live order: fail this row closed rather
                    # than placing on an unproven spread. Only THIS row fails; every
                    # other ticker keeps being evaluated. Manual runs still go
                    # through review_market_buy as a human-reviewed safety net, so
                    # they are not blocked on a missing spread alone (mirrors
                    # near_close_dca's scheduled spread gate).
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": (
                            "bid/ask spread unavailable; scheduled runs require spread_ok"
                        ),
                        "deliberate_skip": True,
                        # The spread gate is what stopped this row. Recorded
                        # explicitly rather than inferred from spread_status,
                        # which a manual run can carry on a row the gate let
                        # through (Codex review).
                        "blocked_by": BLOCKED_BY_SPREAD_GATE,
                        **spread_fields,
                    }
                    continue
                if spread_pct is not None and spread_pct > max_spread:
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": (
                            f"bid/ask spread {spread_pct:.2f}% exceeds {max_spread:.2f}% limit"
                        ),
                        "deliberate_skip": True,
                        **spread_fields,
                    }
                    continue
                spread_by_ticker[ticker] = spread_fields
            else:
                spread_by_ticker[ticker] = spread_gate_fields(
                    spread_pct=None, max_spread_pct=max_spread, evaluated=False
                )
            try:
                tradable = broker.is_tradable_for_fractional_buy(ticker)
            except Exception as exc:  # noqa: BLE001
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"tradability lookup failed: {exc}",
                }
                _log.warning("Tradability lookup error for %s: %s", ticker, exc)
                continue
            if raw_persistence_failed():
                # See the duplicate-check raw_persistence_failed() break above:
                # record a blocked outcome so the post-loop credit safeguard
                # still sees this ticker.
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": "raw input persistence failed before placement",
                }
                # Also stamp every later, not-yet-evaluated candidate -- see
                # _mark_unreached_candidates_blocked's docstring (Codex
                # review: "Preserve credits for candidates after the failed
                # one").
                _mark_unreached_candidates_blocked(_candidate_idx)
                break
            if not tradable:
                outcomes[ticker] = {"status": "blocked", "skip_reason": "not tradable for fractional buy", "deliberate_skip": True}
                continue
            if batch_buying_power is None:
                try:
                    batch_buying_power = broker.available_buying_power()
                except Exception as exc:  # noqa: BLE001 - adapters raise arbitrary connector errors
                    if manual:
                        raise
                    raise _BuyingPowerLookupError(str(exc)) from exc
            if raw_persistence_failed():
                # See the duplicate-check raw_persistence_failed() break above:
                # record a blocked outcome so the post-loop credit safeguard
                # still sees this ticker.
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": "raw input persistence failed before placement",
                }
                # Also stamp every later, not-yet-evaluated candidate -- see
                # _mark_unreached_candidates_blocked's docstring (Codex
                # review: "Preserve credits for candidates after the failed
                # one").
                _mark_unreached_candidates_blocked(_candidate_idx)
                break
            if batch_buying_power - reserved_buying_power < notional:
                outcomes[ticker] = {"status": "blocked", "skip_reason": "insufficient buying power", "deliberate_skip": True}
                continue
            # Scheduled runs are pre-authorized to call place_equity_order directly
            # (config/budgets.json execution_authorization_policy: calls_review_equity_order=false).
            # Manual runs still call review_market_buy before placement.
            if manual:
                try:
                    review_ok, review_reason = broker.review_market_buy(ticker, notional)
                except Exception as exc:  # noqa: BLE001
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": f"review failed: {exc}",
                    }
                    _log.warning("Review error for %s: %s", ticker, exc)
                    continue
                if not review_ok:
                    outcomes[ticker] = {"status": "blocked", "skip_reason": f"review blocked: {review_reason}", "deliberate_skip": True}
                    continue
            # Stable across retries so the broker's idempotency key can collapse
            # duplicate submissions; guarded top-ups use a distinct suffix to
            # avoid colliding with the earlier same-day run's ref_id for the
            # ticker. A manual run without a prior allocation (e.g. a MORNING
            # manual recovery run, which disables per-ticker top-up computation
            # in _compute_top_up) gets its own "-manual-" ref_id segment too:
            # without it, a manual fill's ref_id is indistinguishable from a
            # plain scheduled order's ref_id, so a LATER scheduled run that
            # finds this manual fill as a prior allocation and computes a
            # guarded top-up would have its _same_stream_order_exists ordinary-
            # prefix check match the manual fill and wrongly treat the fresh
            # incremental top-up as an already-submitted retry -- recording it
            # as "submitted" without ever calling place_market_buy (Codex
            # review, PR #42).
            idempotency_stream = self._idempotency_stream()
            if has_prior_allocation:
                ref_id = f"{idempotency_stream}-dca-topup-{trading_day.isoformat()}-{ticker}"
            elif manual:
                ref_id = f"{idempotency_stream}-dca-manual-{trading_day.isoformat()}-{ticker}"
            else:
                ref_id = f"{idempotency_stream}-dca-{trading_day.isoformat()}-{ticker}"
            # Counted as a buy ATTEMPT the moment the broker call is actually
            # made, regardless of outcome -- mirrors near_close_dca's own
            # max_orders_per_day semantics (a cancelled/rejected/failed buy
            # still consumes one of the shared slots), and is what
            # _compute_top_up's cross-stream seeding above counts too.
            self.ledger.record_attempt(trading_day)
            try:
                order_id = broker.place_market_buy(ticker, notional, ref_id=ref_id)
            except OrderConfirmationRequiredError:
                outcomes[ticker] = {
                    "status": "not_submitted_confirmation_required",
                    "order_id": None,
                    "skip_reason": "broker requires post-review confirmation before submission",
                    "submitted_amount_usd": notional,
                }
                continue
            except Exception as exc:  # noqa: BLE001
                outcomes[ticker] = {
                    "status": "failed",
                    "order_id": None,
                    "skip_reason": f"placement failed: {exc}",
                    "submitted_amount_usd": notional,
                }
                _log.error("Placement failed for %s: %s", ticker, exc)
                continue
            # DryRunBroker returns a DRY-RUN-... order id; record as dry_run so
            # load_prior_same_day_allocations never treats it as real capital.
            if order_id.startswith("DRY-RUN"):
                outcomes[ticker] = {
                    "status": "dry_run",
                    "order_id": order_id,
                    "skip_reason": "dry_run: order not placed",
                    "submitted_amount_usd": notional,
                }
                continue
            outcomes[ticker] = {
                "status": "submitted",
                "order_id": order_id,
                "submitted_amount_usd": notional,
            }
            submitted_today += notional
            submitted_this_week += notional
            reserved_buying_power += notional
            _log.info("Placed morning buy for %s: $%.2f, order_id=%s", ticker, notional, order_id)
            # Durably settle the cumulative sub-dollar credit ledger ONLY now
            # that the order is actually confirmed submitted -- not during
            # _evaluate_top_up's earlier, pre-placement computation, which
            # runs before the cap/spread/buying-power/duplicate gates above
            # could still block this candidate (Codex review: "Defer ledger
            # updates until the submitted amount is known", the same fix
            # already applied to near_close_dca.evaluate_top_up_guard).
            # Records the ACTUAL submitted notional, not the originally
            # computed credit amount. A corrupt/locked/unwritable ledger must
            # never mask this real, already-placed and already-published
            # order -- log and continue rather than raise.
            if top_up is not None and top_up.cumulative_credit_decline_usd is not None:
                try:
                    self.credit_ledger.settle(
                        ticker,
                        trading_day.isoformat(),
                        notional,
                        final_session=False,
                        stream=self._idempotency_stream(),
                        order_id=order_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "cumulative credit ledger settlement failed for %s: %s", ticker, exc
                    )
        # Preserve threshold-crossing credits that a LATER gate blocked before
        # placement. _evaluate_top_up's cumulative_credit_decline_usd is only
        # durably recorded above in two places: _compute_top_up (when the
        # credit stays fully pending) and the confirmed-submission settle()
        # call just above (when the order actually went through). A candidate
        # whose combined pending+this-run credit crossed the order threshold
        # (incremental_order_amount > 0) but was then blocked by the per-order
        # ceiling, the shared attempt cap, a duplicate-check error, the daily/
        # weekly cap, the spread gate, tradability, buying power, review, or
        # the broker call itself falls through neither write -- the new
        # decline this run computed would otherwise be silently lost rather
        # than staying pending for a later slot or the 3:30 PM final session
        # (Codex review: "Preserve threshold-crossing credits when placement
        # is blocked"). Excludes "submitted"/"dry_run" (settled above) and
        # outcomes explicitly marked credit_already_resolved (a same-stream
        # duplicate whose credit was settled by the ORIGINAL attempt this slot).
        for signal in candidates:
            ticker = signal.ticker
            top_up = (top_up_outcomes or {}).get(ticker)
            if (
                top_up is None
                or top_up.cumulative_credit_decline_usd is None
                or top_up.incremental_order_amount <= 0
            ):
                continue
            outcome = outcomes.get(ticker)
            if not isinstance(outcome, dict):
                continue
            if outcome.get("status") in ("submitted", "dry_run") or outcome.get(
                "credit_already_resolved"
            ):
                continue
            try:
                self.credit_ledger.preserve_pending(
                    ticker,
                    trading_day.isoformat(),
                    top_up.cumulative_credit_decline_usd,
                    stream=self._idempotency_stream(),
                    recorded_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
                    idempotency_key=self._credit_idempotency_key(now, manual=manual) if now is not None else None,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "cumulative credit ledger preserve_pending failed for %s: %s", ticker, exc
                )
        # Stamp the spread verdict onto every outcome for a ticker that reached the
        # gate but was decided after it (tradability, buying power, review,
        # placement). The rows blocked AT the gate already carry their own verdict
        # and are left untouched.
        for gate_ticker, gate_fields in spread_by_ticker.items():
            outcome = outcomes.get(gate_ticker)
            if isinstance(outcome, dict) and "spread_status" not in outcome:
                outcome.update(gate_fields)
        return outcomes

    def run(
        self,
        now: datetime,
        data: Any,
        *,
        broker: Optional[BrokerAdapter] = None,
        data_source: Optional[str] = None,
        manual: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate the stream and write its artifacts for the active mode.

        Returns ``{"artifacts": <run_state dict>, "paths": <written paths>}``.
        The watchlist scan places no orders; the live mode surfaces
        auto-buy-eligible vs. manual_review candidates per the confidence rule.
        When ``broker`` is supplied in live mode, auto-buy-eligible candidates
        are submitted via the broker to the account configured in
        ``target_account_number`` and outcomes recorded in the artifacts.
        Alpaca stays read-only.

        Thin wrapper around ``_run_impl`` so the model_usage_log append (see
        scripts/model_usage_log.py) happens exactly once per call, regardless
        of which of ``_run_impl``'s several early-return paths was taken,
        without touching any of that method's trading/safety logic.

        Scheduled live invocations run under ``dca_alerts.guarded_run``: any
        escaping exception is logged, alerted to the operator Slack webhook,
        and pinged as a failed heartbeat before re-raising, and a completed
        run pings this stream's success heartbeat (dead-man's switch for the
        missed-fire failure class). Watchlist/preflight/manual runs only get
        a log handler ensured.
        """

        def _invoke() -> Dict[str, Any]:
            result = self._run_impl(now, data, broker=broker, data_source=data_source, manual=manual)
            # bb4f6ef1 shares one cron_id across both its 9:45 AM sub-invocations
            # (preflight mode, then watchlist mode), so the manifest's
            # model_usage_log directory (keyed by cron_id) cannot tell them apart
            # on its own and always resolves to its single stream="watchlist"
            # entry. Pass the mode itself (MORNING_PREFLIGHT_MODE="preflight" /
            # MORNING_WATCHLIST_MODE="watchlist" are both valid
            # schemas/model_usage_log.schema.json stream values) so the
            # preflight execution is not misattributed as a watchlist run.
            # MORNING_LIVE_MODE ("live") is not a stream value; live runs keep
            # falling back to the manifest's per-cron_id lookup (42173154 ->
            # "morning") -- codex review discussion_r3657271722.
            mode_stream = self.mode if self.mode in (MORNING_PREFLIGHT_MODE, MORNING_WATCHLIST_MODE) else None
            log_model_usage(
                root=self.config.model_usage_log_root,
                run_id=result.get("artifacts", {}).get("run_id"),
                cron_id=self.profile.get("cron_id"),
                stream=mode_stream,
                run_origin="manual" if manual else "scheduled",
                artifact_path=result.get("paths", {}).get("run_state"),
                triggered_at_utc=now.astimezone(timezone.utc),
                **_model_usage_routing_overrides(self.config),
            )
            return result

        is_scheduled_live = self.mode == MORNING_LIVE_MODE and self.trades and not manual
        # Snapshotted BEFORE _invoke() runs, so the probe below can tell this
        # invocation's own write from a stale run_state.json an earlier
        # attempt already left in this same run_state_dir (e.g. an
        # external-scheduler retry) -- codex review discussion_r3660858013,
        # discussion_r3660858010.
        run_artifact_written = artifact_freshness_probe(self.run_state_dir(now) / "run_state.json")
        # Clear any in-flight placement state from a PREVIOUS invocation before
        # anything in this run can fail. These attributes live on the executor,
        # which is reused across calls, so a startup failure on a later run (a
        # later trading day, even) would otherwise copy the prior run's outcomes
        # into the new day's artifact and record those orders as current-day
        # allocations (Codex review).
        self._in_flight_broker_outcomes = {}
        self._in_flight_signals_by_ticker = {}
        # Likewise for the manual credit-ledger idempotency key
        # (_credit_idempotency_key): it is cached on first use WITHIN one
        # run() call so every credit-ledger read/write inside that call
        # agrees, but the executor instance is reused across calls, so
        # leaving it set would hand a LATER manual invocation the FIRST
        # invocation's key despite having its own distinct run_id. A later,
        # genuinely lower-price manual poll would then look like an
        # idempotent replay of the earlier one and have its new pending
        # credit silently discarded (Codex review: "Reset the manual credit
        # key for every run invocation").
        self._cached_manual_credit_idempotency_key = None
        # Reset the "this invocation already published its failure artifacts"
        # latch for the same reason as the two attribute resets above: the
        # executor is reused across calls, so a latch left set by an earlier
        # failed invocation would make THIS invocation's own startup-failure
        # handler skip publication entirely and lose real placement evidence
        # (Codex review: "Keep the slot lock during exception artifact
        # publication" -- the latch that fix introduces).
        self._startup_failure_artifacts_published = False
        # Same reasoning for the companion "we tried inside the lock and could
        # not publish at all" latch (Codex review: "Retry failure publication
        # before releasing the slot lock"). Left set from an earlier
        # invocation it would suppress THIS invocation's publication entirely.
        self._startup_failure_publication_abandoned = False
        try:
            return guarded_run(
                self.stream,
                _invoke,
                config=GuardedRunConfig(
                    enabled=is_scheduled_live,
                    context=f"cron_id={self.profile.get('cron_id')}",
                    # Keep the failed-start marker under the same custom base as a
                    # configured model_usage_log_root, so a non-default artifacts
                    # root doesn't split the writer and the report's reader across
                    # two different attempt_log locations -- codex review
                    # discussion_r3658373966.
                    attempt_log_root=attempt_log_root_sibling_to(self.config.model_usage_log_root),
                    # A scheduled morning/midday run that persists run_state.json and
                    # only then fails writing morning_signals.csv/submitted_orders.csv/
                    # a same-day pointer left the marker unconditionally recording
                    # failure_phase='before_run_artifact', fabricating an earlier
                    # failed start plus a successful retry -- codex review
                    # discussion_r3660858010 (the afternoon call already had this
                    # probe; this call never did).
                    run_artifact_written=run_artifact_written,
                    # A run whose raw-persistence recorder failed -- or one that
                    # published no canonical run_state for the slot at all, as the
                    # slot-lock timeout branch deliberately does -- is surfaced as
                    # data on the result rather than raised; it must ping a FAILED
                    # heartbeat, not clear the dead-man's switch. See
                    # _scheduled_live_result_ok's own docstring.
                    result_ok=_scheduled_live_result_ok,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - see below; always re-raised
            # The 2026-07-27 12:30 failure mode: the run raised BEFORE reaching any
            # writer (evaluate() can raise, as can the broker/account lookups), so
            # guarded_run alerted and re-raised and the slot produced no artifact
            # root at all. Every consumer that discovers a run by path then saw a
            # slot that never fired. Write the complete root-level artifact set
            # here, recording the failure, then re-raise so the scheduler still
            # sees it (Codex review: the writer had no production caller).
            #
            # Artifact writing must never mask the original failure, so this is
            # best-effort: a writer error is logged and the original exception is
            # what propagates.
            #
            # This is now the FALLBACK publication site, not the only one. A
            # scheduled-live attempt that fails while holding the slot lock
            # publishes from inside the locked region instead (see
            # _run_impl's `else:` branch) and latches
            # _startup_failure_artifacts_published, so the call below becomes
            # a no-op for it. It is ALSO a no-op when that in-lock publication
            # was attempted repeatedly and never succeeded
            # (_startup_failure_publication_abandoned): retrying it from here,
            # with the slot lock already released, is exactly the unsynchronized
            # write the in-lock retry loop exists to avoid (Codex review:
            # "Retry failure publication before releasing the slot lock").
            #
            # This handler still runs unchanged for every failure that never
            # reached (or never held) the slot lock: the non-scheduled-live and
            # unlockable-raw_dir paths (holiday/preflight early returns
            # included), the lock-setup branches, and anything guarded_run
            # itself raises after the body returned. For a scheduled-live
            # attempt that DID hold the lock, the calendar gate now runs inside
            # the locked region too (Codex review: "Acquire the slot lock
            # before calendar-gate publication"), so a failure there publishes
            # from in-lock rather than here.
            if is_scheduled_live:
                self._publish_startup_failure_artifacts_once(now, exc)
            raise

    def _publish_startup_failure_artifacts_in_lock(self, now: datetime, exc: BaseException) -> None:
        """Publish the failure artifact set while the slot lock is still held.

        Called only from ``_run_impl``'s locked region, whose ``finally``
        releases the lock immediately afterwards, so this is the last chance
        to write this attempt's accepted-order evidence with the slot
        serialized against every other invocation.

        ``_publish_startup_failure_artifacts_once`` is best-effort and
        swallows its writer error, so a single failed attempt used to fall
        through to ``run()``'s outer handler -- which retried the identical
        write with the lock ALREADY RELEASED, letting a waiting same-slot
        invocation acquire the lock in that gap and clean raw/ or publish its
        own run_state concurrently with the retry (Codex review: "Retry
        failure publication before releasing the slot lock"). Retrying here
        instead keeps every publication attempt inside the locked region.

        The retry budget is bounded (``MORNING_STARTUP_FAILURE_PUBLICATION_ATTEMPTS``)
        because the lock hold is not free: a waiter for this slot is blocked
        for as long as we keep trying, and a destination that is genuinely
        unwritable (disk full, revoked permissions) will still be unwritable
        after any number of immediate retries.

        When the budget is exhausted the honest end state is: **this slot's
        failure artifacts were never published, and the run fails loudly.**
        ``_startup_failure_publication_abandoned`` latches so ``run()``'s
        outer handler does NOT quietly retry the write once the lock is gone,
        the original exception keeps propagating to ``guarded_run`` (operator
        alert + failed heartbeat), and the missing artifact is what a watchdog
        sees. That is deliberately preferred over a silently racing write: a
        missing artifact is diagnosable, a half-overwritten one that erased a
        concurrent holder's accepted-order evidence is not.
        """
        for attempt in range(1, MORNING_STARTUP_FAILURE_PUBLICATION_ATTEMPTS + 1):
            if self._publish_startup_failure_artifacts_once(now, exc):
                return
            _log.warning(
                "startup-failure artifact publication attempt %d/%d failed for the %s "
                "stream while holding the slot lock",
                attempt,
                MORNING_STARTUP_FAILURE_PUBLICATION_ATTEMPTS,
                self.stream,
            )
        self._startup_failure_publication_abandoned = True
        _log.error(
            "could not publish startup-failure artifacts for the %s stream after %d "
            "in-lock attempts; releasing the slot lock WITHOUT publishing rather than "
            "retrying the write unsynchronized, and letting the original run failure "
            "propagate",
            self.stream,
            MORNING_STARTUP_FAILURE_PUBLICATION_ATTEMPTS,
        )

    def _publish_startup_failure_artifacts_once(self, now: datetime, exc: BaseException) -> bool:
        """Publish this invocation's failure artifact set at most once.

        ``_write_startup_failure_artifacts`` has two call sites for a
        scheduled-live attempt -- inside ``_run_impl``'s locked region and in
        ``run()``'s outer handler -- and exactly one of them must actually
        write. Publishing twice for one invocation is not merely wasteful: the
        second pass sees the first pass's own run_state on disk and takes the
        "preserve the existing state, complete its companions" merge branch,
        which is the branch written for an artifact belonging to an EARLIER
        attempt. It would re-derive companions from a state this invocation
        just synthesized and lose the ``state_is_synthesized`` regeneration
        semantics that branch deliberately does not apply.
        Only a SUCCESSFUL publication latches, so a writer error in the locked
        region can be retried -- but that retry belongs INSIDE the locked
        region (see ``_publish_startup_failure_artifacts_in_lock``), not in
        ``run()``'s outer handler with the lock already dropped. When the
        in-lock retries are exhausted, ``_startup_failure_publication_abandoned``
        latches instead and short-circuits this method for the rest of the
        invocation, so the outer handler never performs that unlocked write.

        Best-effort by contract (Codex review): artifact writing must never
        mask the original failure, so a writer error is logged here and the
        caller's original exception is what propagates.

        Returns True when the artifacts are published (now or by an earlier
        call in this invocation), False when the write was attempted and
        failed -- the in-lock retry loop drives off that return value.
        """
        if getattr(self, "_startup_failure_artifacts_published", False):
            return True
        if getattr(self, "_startup_failure_publication_abandoned", False):
            # The locked region already tried and gave up; anything reaching
            # here is run()'s outer handler, which no longer holds the slot
            # lock and must not race the next holder for this slot.
            return False
        try:
            self._write_startup_failure_artifacts(now, exc)
        except Exception:  # noqa: BLE001
            _log.exception(
                "failed to write startup-failure artifacts for the %s stream; "
                "re-raising the original run failure",
                self.stream,
            )
            return False
        else:
            self._startup_failure_artifacts_published = True
            return True

    def _write_startup_failure_artifacts(self, now: datetime, exc: BaseException) -> None:
        """Write the complete root-level artifact set for a run that raised.

        A failed run is still evidence that the scheduled slot fired, so it gets
        the same four root-level files as any other outcome: a run_state carrying
        a non-null ``run_classification`` (so the run stays attributable to its
        stream), a mirroring run_summary, and the CSVs. It is NOT valid scheduled
        evidence.

        Three properties matter more than the artifact itself (Codex review):

        * **Real placements are never erased.** This handler wraps the WHOLE live
          run, so an order can be accepted by the broker and a later candidate,
          ledger update, or artifact build can then raise before run_state.json
          exists. Asserting ``orders_submitted_count=0`` there would hide a real
          fill from the same-day top-up guard and let a later run re-allocate that
          capital. The placement loop publishes its outcomes by reference before
          placing anything, so whatever landed is recorded here, with matching
          buy_candidates rows the prior-allocation loader can read.
        * **A partial set is completed, not accepted.** ``write_artifacts`` can
          write run_state.json and then raise on a later file. An existing
          run_state is preserved untouched, but every missing companion is still
          written.
        * **The same-day pointer is refreshed**, so a pointer-based watchdog sees
          today's slot as fired-and-failed rather than still showing yesterday.
        """
        out = self.run_state_dir(now)
        out.mkdir(parents=True, exist_ok=True)
        state_path = out / "run_state.json"
        trading_day = now.astimezone(EASTERN).date()

        # Orders this run had already placed when it raised, if any.
        placed: Dict[str, Any] = {
            ticker: outcome
            for ticker, outcome in (getattr(self, "_in_flight_broker_outcomes", None) or {}).items()
            if isinstance(outcome, dict)
        }
        submitted = sum(1 for oc in placed.values() if oc.get("status") == "submitted")
        signals_by_ticker = getattr(self, "_in_flight_signals_by_ticker", None) or {}
        buy_candidates = []
        for ticker, outcome in placed.items():
            signal = signals_by_ticker.get(ticker)
            row = {
                "ticker": ticker,
                "status": outcome.get("status", "not_submitted"),
                "order_id": outcome.get("order_id"),
                # run_state.schema.json requires buy_multiplier on every
                # candidate row, and validate_run_artifacts does not run schema
                # validation, so omitting it produces an artifact that reads as
                # a complete failure record while schema-based reporting rejects
                # it (Codex review, raised against the afternoon sibling; the
                # same omission was here). The in-flight signal already carries
                # the real multiplier.
                "buy_multiplier": getattr(signal, "multiplier", None),
                "planned_amount_usd": outcome.get("submitted_amount_usd"),
                "skip_reason": outcome.get("skip_reason"),
            }
            if signal is not None and signal.current_price:
                row["decision_price"] = signal.current_price
            buy_candidates.append(row)

        state_was_merged = False
        existing_state: Optional[Dict[str, Any]] = None
        if state_path.exists():
            # A failure AFTER the run wrote its own state: that record is
            # authoritative and is never overwritten. Its companions may still be
            # missing if write_artifacts raised part-way, so they are completed
            # from what is on disk.
            try:
                loaded = json.loads(state_path.read_bytes())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                existing_state = loaded
            _log.warning(
                "%s run failed after run_state.json was written; preserving it and "
                "completing any missing companion artifacts",
                self.stream,
            )

        if existing_state is not None:
            # The preserved file can belong to an EARLIER invocation at this same
            # slot. If this retry placed a new ticker and then raised before
            # overwriting it, discarding `placed` would make that accepted order
            # invisible to the same-day guards and let a later run buy it again
            # (Codex review). Merge this invocation's placements in instead;
            # everything else about the earlier state is left exactly as it was.
            run_state = _merge_placements_into_state(
                existing_state,
                placed,
                buy_candidates,
                error_detail=f"{type(exc).__name__}: {exc}",
            )
            state_was_merged = placed and run_state is not existing_state
        else:
            classification = dict(self.run_classification(now, manual=False))
            classification["valid_scheduled_evidence"] = False
            run_state = {
                "run_id": self.run_id(now, manual=False),
                "trading_day": trading_day.isoformat(),
                "generated_at": now.isoformat(),
                # broker/budget/counts are REQUIRED by run_state.schema.json.
                # validate_run_artifacts does not run schema validation, so
                # omitting them let this handler advertise a complete failure
                # artifact set whose canonical state schema-based reporting then
                # rejects (Codex review). Real values from the executor's own
                # configuration, not placeholders.
                "broker": classification.get("execution_broker") or "robinhood",
                "budget": {
                    "budget_tier": "normal",
                    "daily_cap_usd": self.ledger.daily_cap_usd,
                    "weekly_cap_usd": self.ledger.weekly_cap_usd,
                },
                "counts": {
                    "evaluated": len(buy_candidates),
                    "buy_candidates": len(buy_candidates),
                    "submitted": submitted,
                },
                "status": "failed",
                "run_classification": classification,
                # NOT the placement-failure outcome unless placement really was
                # reached and failed: reporting keyed on that would otherwise
                # misattribute a startup or data failure as a broker failure.
                # The aggregate must never contradict orders_submitted_count and
                # the submitted_orders.csv row: a run that failed AFTER placing an
                # order did submit, so it takes the after-submission outcome and
                # the submitted status (Codex review).
                "run_classification_outcome": (
                    RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION
                    if submitted
                    else RUN_CLASSIFICATION_STARTUP_FAILED
                ),
                "order_submit_status": (
                    ORDER_SUBMIT_STATUS_SUBMITTED if submitted else ORDER_SUBMIT_STATUS_ERROR
                ),
                "error_class": (
                    ERROR_CLASS_POST_SUBMISSION_FAILURE if submitted else ERROR_CLASS_STARTUP_FAILURE
                ),
                "error_detail": f"{type(exc).__name__}: {exc}",
                "is_trading_failure": True,
                # Explicit denial the derivation honors. select_same_day_run_state
                # prefers this date-specific state over the pending pointer, so
                # without the marker every consumer would read a failed run as
                # valid scheduled evidence (Codex review).
                "scheduled_evidence_denied": True,
                "orders_submitted_count": submitted,
                "candidates_reaching_spread_gate": 0,
                "candidates_blocked_missing_spread": 0,
                "signal_rows": [],
                "buy_candidates": buy_candidates,
                "broker_outcomes": placed,
            }
            if placed:
                # The run died mid-flight, so this record is complete only as far
                # as it got. Flagged so a consumer treats it as a floor on what
                # was placed, never as proof that nothing else was.
                run_state["placement_evidence_incomplete"] = True
            atomic_write_text(state_path, json.dumps(run_state, indent=2, sort_keys=True))

        paths: Dict[str, str] = {"run_state": str(state_path)}
        # A SYNTHESIZED state describes this failure and nothing else, so every
        # companion in a reused directory belongs to some earlier attempt.
        # Preserving them on existence alone left stale, zero-byte, or
        # mismatched files advertised as this run's record -- and because
        # existing_state is None the companion check below was skipped too, so
        # nothing caught it (Codex review). Regenerate them all instead.
        state_is_synthesized = existing_state is None
        signals_path = out / self.signals_csv_name
        signal_rows = run_state.get("signal_rows")
        if not isinstance(signal_rows, list):
            signal_rows = []
        if state_is_synthesized or not any((out / name).exists() for name in SIGNAL_CSV_NAMES):
            _write_csv(signals_path, signal_rows)
        paths["signals"] = str(signals_path)
        orders_path = out / "submitted_orders.csv"
        broker_outcomes = run_state.get("broker_outcomes")
        if not isinstance(broker_outcomes, dict):
            broker_outcomes = {}
        buy_candidates = run_state.get("buy_candidates")
        if not isinstance(buy_candidates, list):
            buy_candidates = []
        order_rows = submitted_order_rows(broker_outcomes)
        order_rows.extend(
            # order_rows is passed in for the same reason write_artifacts()
            # passes submitted_rows: a candidate folded into an outcome row
            # merges its notional there (Codex review, PR #593: "Preserve
            # candidate notional when folding into outcome rows").
            _carried_forward_order_rows(broker_outcomes, buy_candidates, order_rows)
        )
        # Rewrite whenever the state records an order the CSV may not: a stale
        # file from an earlier attempt would otherwise be advertised as the
        # canonical order record for a state that knows about more (Codex review).
        # order_csv_covers returns True for an empty expected set without ever
        # reading the file, so on a synthesized state that placed nothing a stale
        # CSV would survive untouched.
        if (
            state_is_synthesized
            or not orders_path.exists()
            or not order_csv_covers(orders_path, order_rows)
        ):
            write_submitted_orders_csv(out, order_rows)
        paths["submitted_orders"] = str(orders_path)
        summary_path = out / "run_summary.json"
        # Regenerate whenever the merge mutated the state: the companion
        # validator compares only the nested classification, so a pre-merge
        # summary passes while reporting the previous attempt's order count and
        # status (Codex review).
        if state_is_synthesized or state_was_merged or not summary_path.exists():
            atomic_write_text(
                summary_path,
                json.dumps(
                    build_run_summary(
                        run_state, extra={"stream": self.stream, "mode": run_state.get("mode")}
                    ),
                    indent=2,
                    sort_keys=True,
                ),
            )
        paths["run_summary"] = str(summary_path)

        # Refresh the stream's same-day pointer so a pointer-based watchdog sees
        # today's slot as fired-and-failed rather than still showing the prior
        # trading day. The payload derives valid_scheduled_evidence from this
        # state, which is False.
        try:
            # Validated for a synthesized state too. Short-circuiting to True
            # there meant the one case that rewrites every companion was also
            # the one case that never checked them (Codex review).
            companions_ok = self._companions_match_state(out, run_state)
            if not companions_ok:
                # select_same_day_run_state prefers this date-specific state over
                # the pointer and calls derive_valid_scheduled_evidence on it
                # directly, so downgrading only the pointer would still hand the
                # run to consumers as valid evidence despite its broken companion
                # set. Fail the state closed too (Codex review).
                self._deny_evidence_in_state(state_path, run_state)
            if existing_state is not None and companions_ok:
                # The run had already written a complete, valid state and only a
                # later step (e.g. the pointer write itself) raised. Forcing a
                # pending/invalid pointer here would make validate_pointer reject
                # it and a completed run read as unavailable, so its evidence is
                # derived normally (Codex review).
                #
                # Guarded on the FULL artifact set validating, not just on the
                # state file existing: in a reused same-day directory the summary
                # or CSVs left in place can belong to an earlier attempt, and
                # advertising a mismatched summary or a stale order CSV as valid
                # scheduled evidence is exactly what this contract forbids.
                payload = self._same_day_pointer_payload(run_state, state_path)
            else:
                # A freshly synthesized failure state: the slot fired, but a
                # failed run is never scheduled evidence. The generic derivation
                # only inspects the scheduled/manual identity, so it is told.
                payload = self._same_day_pointer_payload(
                    run_state,
                    state_path,
                    valid_scheduled_evidence=False,
                    pointer_status=POINTER_STATUS_PENDING,
                )
            self._write_same_day_pointers(
                self._scheduled_pointer_names(is_preflight=False), payload, paths
            )
        except Exception:  # noqa: BLE001 - never mask the original run failure
            _log.exception("failed to refresh the %s same-day pointer", self.stream)

        _log.error(
            "%s run failed; wrote the failure artifact set to %s (orders already placed: %d)",
            self.stream,
            state_path,
            submitted,
        )

    @staticmethod
    def _deny_evidence_in_state(state_path: Path, run_state: Dict[str, Any]) -> None:
        """Persist the contract failure INTO a preserved state whose companions broke.

        Mirrors what near_close_dca does on a failed contract check: a consumer
        that discovers the run_state directly must refuse it, not just one that
        goes through the latest-run pointer.
        """
        try:
            from scripts.scheduled_artifacts import artifact_contract_failure

            run_state["artifact_contract_failure"] = artifact_contract_failure(
                "scheduled-run artifact contract failed",
                detail="companion artifacts do not match the run_state written for this slot",
            )
            run_state["scheduled_evidence_denied"] = True
            rc = run_state.get("run_classification")
            if isinstance(rc, dict):
                rc["valid_scheduled_evidence"] = False
            atomic_write_text(state_path, json.dumps(run_state, indent=2, sort_keys=True))
        except Exception:  # noqa: BLE001 - never mask the original run failure
            _log.exception("could not record the contract failure in %s", state_path)

    @staticmethod
    def _companions_match_state(run_dir: Path, run_state: Dict[str, Any]) -> bool:
        """True when the run directory passes the contract AND its orders agree.

        Existence alone is not enough for a reused same-day directory: a summary
        or order CSV left from an earlier attempt can disagree with the state
        just written. validate_run_artifacts catches a mismatched summary through
        the classification mirror, but it only checks the order CSV's HEADER, so a
        stale order CSV that omits orders the fresh state records would still pass
        and be advertised as the canonical order record (Codex review).
        """
        try:
            from scripts.scheduled_artifacts import validate_run_artifacts

            if not validate_run_artifacts(run_dir, require_summary=True):
                return False
            expected = submitted_order_rows(run_state.get("broker_outcomes") or {})
            expected.extend(
                # `expected` is passed in so this expectation is built exactly
                # the way the writers build their rows (Codex review, PR #593:
                # "Preserve candidate notional when folding into outcome
                # rows"). order_csv_covers() compares order IDENTITIES only, so
                # the merged notional does not change this verdict today --
                # matching the writers keeps it that way if it ever does.
                _carried_forward_order_rows(
                    run_state.get("broker_outcomes") or {},
                    run_state.get("buy_candidates") or [],
                    expected,
                )
            )
            return order_csv_covers(run_dir / "submitted_orders.csv", expected)
        except Exception:  # noqa: BLE001 - fail closed; never mask the run failure
            _log.exception("could not validate the artifact set at %s", run_dir)
            return False

    def _unusable_raw_dir_result(self, now: datetime, raw_dir: Path) -> Optional[Dict[str, Any]]:
        """An attempt-scoped blocked result when this slot's ``raw/`` is unusable.

        Returns ``None`` when ``raw_dir`` is safe to record through -- it does
        not exist yet, or it already is a real directory. Otherwise returns a
        blocked stub with ``paths == {}``, recording the failure only through
        the append-only failed-start marker and never through this slot's
        canonical artifacts.

        Split out of ``_run_impl`` so this shape check can run INSIDE the
        locked region. The slot lock no longer lives under ``raw_dir`` -- nor
        under the dated run directory (see ``_run_impl``'s lock-location
        comment and :data:`SLOT_LOCK_ROOT_DIRNAME`) -- so the lock is acquired FIRST
        and this check then runs serialized against every other invocation
        for the same dated slot.
        """
        if not (raw_dir.is_symlink() or (raw_dir.exists() and not raw_dir.is_dir())):
            return None
        # ``raw_dir`` is not safely usable as this attempt's raw archive:
        #
        #   * A SYMLINK -- _RawInputRecorder's writes and the stale-cleanup
        #     loop both FOLLOW the link (Path.is_dir()/open()/iterdir() all
        #     do), so they would create, enumerate and unlink files inside
        #     whatever directory it points at: anywhere this process's
        #     account can write (Codex review: "Check symlinks before
        #     preserving the raw archive").
        #   * An existing NON-DIRECTORY (e.g. a stray plain file at this
        #     path) -- the ``mkdir(parents=True, exist_ok=True)`` that
        #     prepares raw/ raises ``FileExistsError`` here (``exist_ok``
        #     only tolerates an existing *directory*), which would crash this
        #     attempt outright instead of letting it fail closed the way it
        #     always has for this exact malformed-raw_dir case.
        #
        # Codex review, PR #593: "Avoid publishing malformed-raw failures
        # without the slot lock". Failing closed says nothing about which
        # artifacts a failing attempt writes on its way out, and this branch
        # used to run both the calendar gate and _run_impl_body() -- either
        # of which publishes this slot's canonical blocked artifacts
        # (run_state.json, the companion CSVs/summary, the same-day
        # pointers). Two things now keep that safe:
        #
        #   1. The check runs with the slot lock HELD, so even a concurrent
        #      invocation that lands here while another is mid-placement is
        #      serialized against it rather than racing it (Codex review,
        #      PR #593: "Move the slot lock outside the replaceable raw
        #      directory", then "Place the slot lock outside the replaceable
        #      run directory" -- the lock lives in its own ``slot_locks/``
        #      root now, so a malformed raw/ no longer prevents taking it at
        #      all).
        #   2. The failure is still recorded in an ATTEMPT-SCOPED location,
        #      mirroring the slot-lock TIMEOUT branch verbatim: a
        #      best-effort, append-only write_failed_start_marker() under
        #      attempt_log_root's <day>/<stream>(.json|.attempts.jsonl) path
        #      -- which can never collide with whatever a real lock holder
        #      publishes under this slot's run_state_dir -- plus an in-memory
        #      blocked stub with ``paths == {}``. Nothing whatsoever under
        #      run_state_dir(now) is published from this branch -- not even a
        #      lock sidecar, now that the slot lock lives in its own
        #      ``slot_locks/`` root (Codex review, PR #593: "Place the slot
        #      lock outside the replaceable run directory") -- and
        #      _run_impl_body() -- whose raw
        #      recording and order placement this path must never reach --
        #      is not called at all, which is strictly MORE fail-closed than
        #      routing through it and relying on its internal checks.
        #
        # _scheduled_live_result_ok() classifies this as UNHEALTHY via its
        # "no canonical ``paths['run_state']`` was published" rule -- the
        # same rule that catches the timeout branch, and for the same
        # honest reason: this invocation evaluated nothing, placed
        # nothing, and cannot vouch for the slot. It carries its own
        # ``error_class`` rather than borrowing "slot_lock_unavailable",
        # since no lock was contended here; the error-class check in that
        # predicate is a layer ON TOP of the path-absence rule, not a
        # replacement for it, so a distinct class stays correctly
        # classified while keeping the two causes distinguishable in the
        # marker log.
        block_reason = (
            f"scheduled-slot raw directory {raw_dir} is a symlink or is not a directory -- "
            "refusing to record raw inputs or publish artifacts through it"
        )
        _log.warning("%s: %s", self.stream, block_reason)
        # write_failed_start_marker() is itself best-effort and never
        # raises (see its own docstring), so no extra try/except is
        # needed here to protect this block-and-return path.
        write_failed_start_marker(
            self.stream,
            OSError(block_reason),
            triggered_at_utc=now.astimezone(timezone.utc),
            root=attempt_log_root_sibling_to(self.config.model_usage_log_root),
            run_artifact_written=False,
        )
        return {
            "artifacts": {
                "status": "blocked",
                "block_reason": block_reason,
                "error_class": "raw_dir_unusable",
                "stream": self.stream,
                "mode": self.mode,
            },
            "paths": {},
        }

    def _slot_lock_path(self, now: datetime) -> Path:
        """Path of the scheduled-live slot lock for the dated slot ``now`` falls in.

        Lives under :data:`SLOT_LOCK_ROOT_DIRNAME` in the shared artifact root
        (see that constant for why the lock must sit outside every directory
        artifact code manages), one file per stream per dated slot:
        ``<root>/slot_locks/morning_20260708_1030ET.lock``.

        The key is ``f"{self.stream}_{self.run_state_dir(now).name}"`` -- the
        stream identity plus the EXACT directory name this attempt would publish
        into. Deriving it from ``run_state_dir(now)`` rather than re-deriving a
        date/slot string here is deliberate: the lock and the thing it protects
        then agree by construction, so two invocations that would publish into
        the same directory always compute the same lock file and two that would
        not never share one.

        Why that cannot collide across the four streams sharing this class
        (``MorningDCAExecutor`` plus the ``market_open``/``midday``/``intraday``
        subclasses, which override identity constants only):

          * ``run_state_dir(now).name`` is ``f"{YYYYMMDD}_{folder_suffix}"``, and
            ``folder_suffix`` is per-stream AND per-slot -- ``0945ET``/``1030ET``
            (morning watchlist/live), ``0935ET`` (market-open), ``1230ET``
            (midday), and ``1130ET``/``1330ET``/``1430ET`` for the intraday
            stream's three daily firings, which are separate profiles rather
            than one shared one. So the intraday slots already key apart from
            each other, which the run directory alone would not guarantee if
            they ever shared a root without differing suffixes.
          * ``self.stream`` is prepended anyway, so even if two streams were
            ever given the same fire time (and therefore the same suffix) they
            would still serialize independently, exactly as their separate
            ``state_root`` trees already keep their artifacts apart.

        Both components are fixed identifiers -- class-level stream constants
        and a ``strftime``-derived directory name -- so neither can smuggle a
        path separator into the filename.

        The root is ``self.slot_lock_root``, resolved once in ``__init__`` from
        ``self.state_root`` -- the artifact tree this lock protects -- rather
        than re-read from ``$DCA_CRON_TRACKING_ROOT`` on every call (Codex
        review, PR #593: "Anchor the slot lock to the configured artifact
        root"). See that assignment for why a call-time resolution let two
        workers publishing into one configured root lock two different files.
        """
        slot_key = f"{self.stream}_{self.run_state_dir(now).name}"
        return self.slot_lock_root / f"{slot_key}.lock"

    def _run_impl(
        self,
        now: datetime,
        data: Any,
        *,
        broker: Optional[BrokerAdapter] = None,
        data_source: Optional[str] = None,
        manual: bool = False,
    ) -> Dict[str, Any]:
        # live_schedule_hardening_2026_07_20 (config/morning_dca.json): a
        # SCHEDULED live run must persist every raw connector response it uses
        # under raw/ before that response is used to compute a signal or place
        # an order, and must never escalate without writing a valid run_state.
        # Manual/watchlist/preflight runs are unaffected -- the policy's
        # "applies_to" is scoped to the scheduled live task (42173154/a6239dae).
        #
        # Codex review: "Acquire the slot lock before calendar-gate
        # publication". The weekend/holiday/outside-window gate
        # (_maybe_block_for_holiday) used to run BEFORE any of this, so its
        # canonical writes -- run_state.json, the companion CSVs/summary, and
        # the same-day pointers -- happened with no slot lock held at all. A
        # later same-slot invocation that saw an outside-window condition
        # (trading_window.json rewritten mid-session, or a delayed fire past
        # window_end_et) could therefore publish a blocked stub over the top
        # of an EARLIER invocation that was still inside the locked region
        # actively placing orders, without ever waiting for it. The gate's
        # own "preserve existing PLACED evidence" guard cannot close that: it
        # reads run_state.json, which the active holder has not written yet.
        #
        # So the gate now runs INSIDE the locked region for a scheduled-live
        # attempt (see the `else:` branch below), and the lock is taken before
        # it. `is_scheduled_live` and `raw_dir` are both derivable without the
        # gate -- the first from mode/trades/manual, the second from `now`
        # alone -- so hoisting the lock above the gate changes no gate
        # semantics; it only decides whether the gate is serialized.
        is_scheduled_live = self.mode == MORNING_LIVE_MODE and self.trades and not manual
        if not is_scheduled_live:
            # Manual/watchlist/preflight runs never touch the slot's
            # the slot lock at all -- only a genuinely scheduled-live attempt
            # does, per the "applies_to" scoping above. The calendar gate runs
            # unlocked for them, exactly as it always has.
            blocked = self._maybe_block_for_holiday(now, manual=manual)
            if blocked is not None:
                return blocked
            return self._run_impl_body(
                now,
                data,
                broker=broker,
                data_source=data_source,
                manual=manual,
                is_scheduled_live=False,
                raw_dir=None,
            )
        run_dir = self.run_state_dir(now)
        raw_dir = run_dir / "raw"
        # Codex review, PR #593: "Move the slot lock outside the replaceable
        # raw directory", then "Place the slot lock outside the replaceable
        # run directory". The lock used to live at ``raw_dir / ".slot.lock"``
        # -- INSIDE the very directory whose runtime replacement this path
        # already defends against -- and then at ``run_dir / ".slot.lock"``,
        # one level up. flock() (and the portable O_EXCL fallback) locks an
        # INODE, not a name, so if the directory holding the lock file is
        # renamed away and replaced with ANOTHER REAL DIRECTORY while an
        # invocation holds the lock, an overlapping invocation for the same
        # slot opens the lock at the NEW inode, acquires it uncontended, and
        # both invocations then resolve raw/ and this slot's canonical
        # artifact writes through the replacement directory and clean, place
        # and publish concurrently. That is exactly the duplicate-allocation
        # hole the lock exists to close, and it applied to raw/ and to the
        # dated run directory alike: run_dir is this stream's artifact
        # PUBLICATION directory, so it will always be a directory something
        # writes into and could in principle rotate.
        #
        # The recursion terminates at a directory NO artifact code manages, so
        # the lock now lives there: ``_slot_lock_path(now)``, under the
        # dedicated ``slot_locks/`` root beside the per-stream artifact trees
        # (see SLOT_LOCK_ROOT_DIRNAME). Nothing cleans, publishes into,
        # enumerates or rotates that tree -- the only operation any code here
        # performs on it is creating it -- so there is no further level to
        # move up to. The filename keys on stream + the dated slot directory
        # name, so two invocations for the same slot always agree and two
        # different slots (including the intraday stream's three daily
        # firings) never share a lock; see _slot_lock_path.
        #
        # A useful side effect kept from the raw/ -> run_dir move: with the
        # lock out of raw/, ``list_raw_inputs_saved()`` can no longer mistake
        # it for a persisted raw input. It is now also out of run_dir, so
        # run-state discovery (``run_state*.json``), the order-CSV scanners
        # (``*orders.csv``) and the credit-usage inventory's recursive scan
        # never see it at all rather than merely tolerating it.
        #
        # `.raw_seq.lock` deliberately STAYS inside raw/: it serializes
        # filename allocation *within* that archive, so the archive really is
        # its scope. The two remain SEPARATE lock files (never the same one),
        # which is what lets this attempt hold the slot lock for its whole
        # lifetime without self-deadlocking against
        # _RawInputRecorder.persist()'s brief, repeated acquisitions of the
        # other one. The stale-cleanup loop's `*.lock` skip stays too -- it
        # still has `.raw_seq.lock` to protect.
        #
        # The ordering follows from the relocation: the lock path no longer
        # runs through raw_dir (nor through run_dir), so the lock is
        # acquired FIRST and the
        # raw_dir shape check (_unusable_raw_dir_result) runs INSIDE the
        # locked region -- the malformed-raw failure is now detected while
        # serialized, while still failing closed and still recording only
        # attempt-scoped evidence (see that helper).
        # Codex review: "Hold the slot lock through raw recording" -- a
        # follow-up on the stale-cleanup serialization below. That fix only
        # serialized cleanup-vs-cleanup: the lock used to be released as soon
        # as this attempt's _RawInputRecorder was constructed, so a SECOND
        # overlapping scheduled invocation for this same dated slot could
        # still acquire the slot lock immediately afterward and run its own
        # stale-file cleanup while THIS attempt is actively recording raw
        # connector responses via evaluate()/_place_broker_orders below --
        # deleting files this attempt is still writing. The lock must
        # instead be held for this WHOLE scheduled-live attempt (raw-dir
        # setup through evaluate(), placement, and artifact/run-state
        # publication), so cleanup is serialized against ACTIVE RECORDING,
        # not just against another cleanup. `_run_impl_body` (all of this
        # attempt's real work, every early-return path included) runs
        # entirely inside this `with` block so the lock is released via the
        # context manager's own `finally` on every path out -- a clean
        # return, an early blocked return, or an uncaught exception.
        #
        # This is still a SEPARATE lock file from `.raw_seq.lock`
        # (_RawInputRecorder.persist()'s own per-write critical section), so
        # holding the slot lock for the whole attempt cannot self-deadlock
        # against persist()'s brief, repeated acquisitions of the other
        # lock file.
        # Codex review discussion_r3840403557: "Limit the lock-error handler
        # to acquisition failures". This used to be a single
        # ``try: with cross_process_lock(...): return self._run_impl_body(...)``
        # whose ``except OSError`` wrapped the ENTIRE ``with`` body -- so it
        # also caught an OSError raised from deep INSIDE _run_impl_body()
        # itself (evaluate(), order placement, artifact/run-state
        # publication), not just a failure acquiring the lock. Concretely: if
        # a broker order is accepted and THEN an artifact write inside
        # _run_impl_body() raises OSError (disk full, permissions) before
        # run_state.json is durably published, the old handler intercepted
        # that as a lock problem and returned a generic
        # "slot_lock_unavailable" blocked result -- discarding whatever
        # in-flight broker-outcome evidence _run_impl_body() had already
        # accumulated (_run_impl_body has its own internal handling for
        # placement/publication failures that preserves that evidence; this
        # outer catch-all used to bypass it entirely).
        #
        # Fix: acquire the lock in its own narrow try/except (only lock SETUP
        # can raise here) and run the body OUTSIDE that except's reach, so a
        # body-level OSError propagates uncaught to run()/guarded_run() --
        # exactly the behavior this method had before any of this locking
        # existed.
        slot_lock_path = self._slot_lock_path(now)
        try:
            # Creating the dedicated lock root is the ONLY thing any code in
            # this repository does to that tree (see SLOT_LOCK_ROOT_DIRNAME).
            # cross_process_lock() would create the parent itself, but doing it
            # here keeps the one write this tree ever sees explicit at the call
            # site, and it is inside this narrow try so a root that cannot be
            # created fails closed through the acquisition-failure branches
            # below rather than escaping as an unhandled OSError.
            slot_lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_cm = cross_process_lock(slot_lock_path, timeout_seconds=MORNING_SLOT_LOCK_TIMEOUT_SECONDS)
            lock_cm.__enter__()
        except TimeoutError as exc:
            # Another process is genuinely still mid-attempt for this exact
            # slot (evaluating signals, placing orders, or writing its
            # artifact). This USED to call _blocked_live_result() here, but
            # that helper WRITES this slot's canonical run_state.json (+
            # submitted-orders CSVs + same-day pointer + a drift report) via
            # write_artifacts() -- and it does so WITHOUT holding
            # the slot lock, precisely because failing to acquire that lock is
            # what put us in this branch. The OTHER process genuinely still
            # holds the lock and can be actively publishing ITS OWN final
            # run_state.json/pointer/CSVs for this exact same slot at this
            # exact moment, so this handler's unlocked write could race that
            # publication -- overwriting the real holder's just-published
            # submitted-order evidence with a stale "lock unavailable" stub,
            # or vice versa, depending on write order. That defeats the
            # entire purpose of the lock this attempt just failed to acquire
            # (Codex review discussion_r3840378333: "Do not publish the
            # timeout result outside the slot lock").
            #
            # Fail closed WITHOUT touching this slot's canonical artifact
            # path at all: no run_state.json, no CSVs, no same-day pointer,
            # no drift report are written here. The contention is instead
            # recorded through write_failed_start_marker() -- the SAME
            # attempt-scoped, append-only marker `run()`'s own
            # startup-failure handler relies on for "this invocation never
            # got far enough to write its own artifact" -- which lives under
            # attempt_log_root's <day>/<stream>(.json|.attempts.jsonl) path,
            # never under this slot's run_state_dir, so it can never collide
            # with whatever the actual lock holder publishes there. Losing
            # this attempt's own run_state.json for the slot is the
            # deliberately conservative trade-off: the OTHER, still-running
            # attempt is expected to publish the real evidence for this slot
            # itself; if it never does, the missing artifact (rather than a
            # corrupted one) is what a watchdog should see.
            block_reason = (
                f"could not acquire the scheduled-slot lock {slot_lock_path} for {run_dir} within "
                f"{MORNING_SLOT_LOCK_TIMEOUT_SECONDS:g}s -- another invocation for "
                f"this slot appears to still be in progress: {exc}"
            )
            _log.warning("%s: %s", self.stream, block_reason)
            # write_failed_start_marker() is itself best-effort and never
            # raises (see its own docstring), so no extra try/except is
            # needed here to protect this block-and-return path.
            write_failed_start_marker(
                self.stream,
                exc,
                triggered_at_utc=now.astimezone(timezone.utc),
                root=attempt_log_root_sibling_to(self.config.model_usage_log_root),
                run_artifact_written=False,
            )
            return {
                "artifacts": {
                    "status": "blocked",
                    "block_reason": block_reason,
                    "error_class": "slot_lock_unavailable",
                    "stream": self.stream,
                    "mode": self.mode,
                },
                "paths": {},
            }
        except OSError as exc:
            # TimeoutError (handled above) is itself an OSError subclass, so
            # this only ever catches a DIFFERENT lock-setup failure -- e.g.
            # the dedicated ``slot_locks/`` root could not be created, or the
            # lock file itself became unreadable. This except clause wraps
            # ONLY the ``mkdir`` and ``lock_cm.__enter__()`` above (lock
            # setup and acquisition), never the body below, so it can no longer
            # intercept an OSError raised from inside _run_impl_body().
            #
            # Fail closed EXACTLY like the TimeoutError branch above --
            # attempt-scoped marker only, no canonical write. This branch
            # used to call _blocked_live_result(), which publishes this
            # slot's run_state.json + companion CSVs + same-day pointer +
            # drift report. Failing to acquire the lock is precisely what
            # put us here, so that write happened WITHOUT serialization: a
            # non-timeout acquisition failure does NOT imply the slot is
            # idle (the lock file can become unreadable while another
            # invocation is mid-placement and the run directory stays
            # writable), so the stub could race the active holder and
            # overwrite or mismatch its accepted-order evidence -- the same
            # hole already closed for the timeout path (Codex review: "Keep
            # lock-setup failures out of canonical artifacts").
            #
            # Same conservative trade-off as the timeout branch: a missing
            # artifact for this attempt is diagnosable, a corrupted one that
            # erased a real order is not. _scheduled_live_result_ok()
            # classifies the returned shape unhealthy via its "no
            # paths['run_state']" rule, so the dead-man switch still fires.
            block_reason = (
                f"could not set up the scheduled-slot lock {slot_lock_path} for {run_dir}: {exc}"
            )
            _log.warning("%s: %s", self.stream, block_reason)
            # Best-effort and never raises (see its own docstring), so this
            # block-and-return path needs no extra try/except.
            write_failed_start_marker(
                self.stream,
                exc,
                triggered_at_utc=now.astimezone(timezone.utc),
                root=attempt_log_root_sibling_to(self.config.model_usage_log_root),
                run_artifact_written=False,
            )
            return {
                "artifacts": {
                    "status": "blocked",
                    "block_reason": block_reason,
                    "error_class": "slot_lock_unavailable",
                    "stream": self.stream,
                    "mode": self.mode,
                },
                "paths": {},
            }
        else:
            # Lock acquired. Run the whole scheduled-live attempt with the
            # lock held, releasing it via ``finally`` on every path out -- a
            # clean return, an early blocked return, or an uncaught
            # exception. A body-level exception (e.g. an OSError from an
            # artifact write after a broker order was already accepted) must
            # still propagate uncaught to run()/guarded_run(), never be
            # swallowed as a lock failure -- the ``except`` below therefore
            # re-raises unconditionally and returns nothing.
            #
            # Codex review: "Keep the slot lock during exception artifact
            # publication". Releasing the lock in ``finally`` and letting the
            # exception travel up to run()'s handler meant the failure
            # publication -- which MERGES and writes this attempt's
            # accepted-order evidence (_write_startup_failure_artifacts reads
            # _in_flight_broker_outcomes) -- happened with the slot lock
            # ALREADY RELEASED. A waiting same-slot invocation could acquire
            # the lock the instant it was dropped, run its stale-raw/ cleanup,
            # record into raw/, and publish its own run_state while this
            # attempt was still writing the evidence for an order the broker
            # had already accepted: exactly the artifact race the slot-wide
            # lock exists to prevent, and the one shape of it where a real
            # placement can be lost.
            #
            # The invariant established here: this attempt's accepted-order
            # evidence is published BEFORE the slot lock is released. Python
            # runs an ``except`` clause before the ``finally``, so publishing
            # from the handler puts the write inside the locked region while
            # leaving the caller-visible behavior identical -- the same
            # exception propagates, and run()'s outer handler finds the
            # artifacts already published (the latch makes its own call a
            # no-op) rather than writing them a second time.
            #
            # ``Exception``, not ``BaseException``, deliberately mirrors
            # run()'s outer handler exactly: publication moves earlier, it
            # does not start covering exception classes that never triggered
            # it before.
            #
            # And publication RETRIES stay inside this locked region too --
            # _publish_startup_failure_artifacts_in_lock, not the bare
            # ..._once helper. A single swallowed writer error used to leave
            # the latch clear so run()'s outer handler wrote again with the
            # lock already released (Codex review: "Retry failure publication
            # before releasing the slot lock").
            #
            # The calendar gate is inside this region as well (Codex review:
            # "Acquire the slot lock before calendar-gate publication"), so a
            # blocked scheduled-live result -- weekend, holiday, outside the
            # approved date window, outside the slot's own time window --
            # publishes its run_state/companions/pointers while serialized
            # against any other invocation for this same dated slot, instead
            # of racing one that is mid-placement.
            #
            # And so is the raw_dir shape check (Codex review, PR #593:
            # "Move the slot lock outside the replaceable raw directory").
            # It used to run BEFORE the lock, because the lock path itself
            # ran through raw_dir and could not be built when raw_dir was
            # malformed; with the lock in its own ``slot_locks/`` root that
            # constraint is gone, and checking under the lock is strictly
            # safer -- an overlapping invocation that finds a symlinked or
            # replaced raw/ now waits for the real holder instead of
            # evaluating the slot's shape while that holder is mid-placement.
            # The failure it reports stays attempt-scoped either way (no
            # canonical write); see _unusable_raw_dir_result.
            try:
                unusable = self._unusable_raw_dir_result(now, raw_dir)
                if unusable is not None:
                    return unusable
                blocked = self._maybe_block_for_holiday(now, manual=manual)
                if blocked is not None:
                    return blocked
                return self._run_impl_body(
                    now,
                    data,
                    broker=broker,
                    data_source=data_source,
                    manual=manual,
                    is_scheduled_live=True,
                    raw_dir=raw_dir,
                )
            except Exception as exc:  # noqa: BLE001 - always re-raised below
                self._publish_startup_failure_artifacts_in_lock(now, exc)
                raise
            finally:
                lock_cm.__exit__(None, None, None)

    def _run_impl_body(
        self,
        now: datetime,
        data: Any,
        *,
        broker: Optional[BrokerAdapter],
        data_source: Optional[str],
        manual: bool,
        is_scheduled_live: bool,
        raw_dir: Optional[Path],
    ) -> Dict[str, Any]:
        """The actual work of one ``_run_impl`` attempt.

        Split out of ``_run_impl`` so the whole thing -- raw-dir setup through
        evaluate(), placement, and artifact/run-state publication, every
        early-return path included -- can run inside ``_run_impl``'s
        ``with cross_process_lock(...)`` block for a scheduled-live attempt,
        without re-indenting this entire (originally ~600-line) method body
        by hand (Codex review: "Hold the slot lock through raw recording").
        ``is_scheduled_live``/``raw_dir`` are passed in already resolved by
        the caller rather than recomputed here, since the caller needs
        ``raw_dir`` itself to build the lock path before this method ever
        runs.
        """
        raw_recorder: Optional[_RawInputRecorder] = None
        if is_scheduled_live:
            assert raw_dir is not None
            # A scheduled retry at this same dated slot reuses raw_dir while the new
            # recorder resets its filename sequence to 1 -- files overwritten by the
            # new call sequence are current, but any file from a kind/key not
            # repeated on the retry (e.g. a ticker dropped from this attempt's
            # candidate set) would otherwise linger from the earlier attempt and mix
            # multiple attempts' evidence into one archive, violating the
            # no_stale_reuse contract (Codex review: "Reset the raw archive before a
            # scheduled retry"). Clearing here (rather than deferring to
            # _RawInputRecorder) keeps the reset scoped to genuinely scheduled-live
            # retries.
            #
            # BUT skipped entirely when this dated slot already has a preserved
            # PLACED order (_existing_placed_run_state -- submitted, filled,
            # pending, or unconfirmed; every real order status in
            # ORDER_ATTEMPT_STATUSES except the non-order
            # "not_submitted_confirmation_required") -- those raw files
            # are the actual evidence a real broker order was justified by, and
            # _blocked_live_result()'s raw_inputs_saved MERGE (Codex review, PR
            # #538: "Retain earlier raw filenames when recording a failed
            # retry") assumes they are still readable on disk, not just named
            # in a preserved JSON list. Wiping them out from under a real
            # submission would silence the audit trail this preservation path
            # exists to keep, and _RawInputRecorder's own sequence numbering
            # already continues past whatever is on disk, so this retry's OWN
            # fresh writes still land under distinct, non-colliding filenames.
            stale_cleanup_error: Optional[str] = None
            # Codex review: "Check symlinks before preserving the raw archive"
            # -- Path.is_dir() FOLLOWS symlinks, so if raw_dir were ever
            # replaced with a symlink to another directory (a stale
            # artifact-tree link, or a malicious one) the cleanup block below
            # would enumerate and unlink files in that TARGET directory --
            # anywhere this process's account can write. That refusal must
            # hold NO MATTER WHAT: the preserved-placed-run-state
            # short-circuit below only skips the stale-file *cleanup* loop
            # (those raw files are real evidence a prior order was justified
            # by -- see the comment above this block), it must never skip the
            # symlink check itself, or a preserved run state for this slot
            # would let a symlinked raw_dir sail through un-checked while
            # _PersistingMarketDataProvider/_PersistingBrokerAdapter below
            # then write real connector responses straight through the link.
            if raw_dir.is_symlink():
                stale_cleanup_error = f"refusing to clean symlinked raw directory {raw_dir}"
            elif self._existing_placed_run_state(now) is not None:
                pass
            elif raw_dir.is_dir():
                # Codex review: "Serialize stale cleanup with active raw
                # recorders" -- two overlapping scheduled invocations for the
                # same dated slot can both reach this point before either has
                # written a submitted run state. Without serialization, the
                # second process's cleanup loop below can unlink files the
                # first process is actively writing as part of its own
                # legitimate current attempt, or unlink `.raw_seq.lock`
                # itself -- the very lock file persist()'s cross-process
                # critical section above depends on -- corrupting the
                # locking guarantee (two processes could end up holding
                # locks on different inodes for what they both believe is
                # "the" lock file).
                #
                # No SEPARATE ``with cross_process_lock(...)`` here anymore:
                # `_run_impl` (this method's caller) already holds
                # the slot lock for this attempt's entire scheduled-live
                # lifetime -- cleanup below runs serialized against it
                # already (Codex review: "Hold the slot lock through raw
                # recording"). The slot lock remains a SEPARATE lock file from
                # `.raw_seq.lock` -- it lives in the dedicated ``slot_locks/``
                # root now, outside both this replaceable raw/ and the
                # replaceable dated run directory (Codex review, PR #593:
                # "Move the slot lock outside the replaceable raw directory",
                # then "Place the slot lock outside the replaceable run
                # directory") -- specifically so holding it
                # for this whole attempt can never deadlock against
                # persist()'s own brief, repeated acquisitions of that other
                # lock file below.
                try:
                    stale_files = list(raw_dir.iterdir())
                except OSError as exc:
                    _log.warning("could not list stale raw inputs in %s: %s", raw_dir, exc)
                    stale_files = []
                    stale_cleanup_error = f"could not list stale raw inputs in {raw_dir}: {exc}"
                for stale_file in stale_files:
                    # Never unlink a lock file. The slot lock no longer lives
                    # here (Codex review, PR #593: "Move the slot lock
                    # outside the replaceable raw directory", then "Place the
                    # slot lock outside the replaceable run directory" -- it
                    # sits in the dedicated ``slot_locks/`` root now, which
                    # this loop never walks), but `.raw_seq.lock`
                    # still does and still guards persist()'s critical
                    # section, so the skip stays: removing it out from under
                    # a concurrent holder lets a second process reuse the
                    # same inode's lock as free, or lets two processes each
                    # create/hold a *different* inode for what they both
                    # think is "the" lock file, defeating flock(). The
                    # pattern stays `*.lock` rather than the one literal
                    # filename so any future sidecar lock in this archive is
                    # protected by default rather than silently deleted.
                    if stale_file.name.endswith(".lock"):
                        continue
                    try:
                        # Codex review discussion_r3840403554: "Fail closed
                        # on unhandled stale raw entries" -- this used to
                        # only handle stale_file.is_file() (unlink it) and
                        # silently SKIP anything else: a stale child
                        # directory, FIFO, socket, block/char device, or
                        # other non-regular entry left in raw/ from an
                        # earlier attempt survived cleanup silently, and the
                        # run continued to live placement with a "reset"
                        # archive that still mixed content from an earlier
                        # attempt. Handle every entry kind explicitly instead
                        # of falling through:
                        #   * a real directory (not a symlink) is safely
                        #     removed with shutil.rmtree -- entirely this
                        #     attempt's own raw/ tree, never anything else's;
                        #   * a symlinked entry is refused, consistent with
                        #     the existing symlink-refusal policy for raw_dir
                        #     itself above (never follow it to remove
                        #     whatever it actually points at);
                        #   * anything else (FIFO, socket, device, or a kind
                        #     Path can't classify) is neither a plain file
                        #     nor a directory we can safely rmtree, so it
                        #     fails closed the same way a resistant unlink()
                        #     already does below.
                        if stale_file.is_symlink():
                            raise OSError(f"refusing to remove symlinked stale raw entry {stale_file}")
                        elif stale_file.is_file():
                            stale_file.unlink()
                        elif stale_file.is_dir():
                            shutil.rmtree(stale_file)
                        else:
                            raise OSError(f"stale raw entry {stale_file} is neither a file nor a directory")
                    except OSError as exc:
                        _log.warning("could not clear stale raw input %s: %s", stale_file, exc)
                        if stale_cleanup_error is None:
                            stale_cleanup_error = f"could not clear stale raw input {stale_file}: {exc}"
                raw_recorder = _RawInputRecorder(raw_dir, now.astimezone(EASTERN).date().isoformat())
            if raw_recorder is None:
                raw_recorder = _RawInputRecorder(raw_dir, now.astimezone(EASTERN).date().isoformat())
            if stale_cleanup_error is not None:
                # Fail closed rather than silently continuing with a recorder
                # whose .error is unset: a file that resisted removal (existing
                # but not writable, a permissions/lock issue, or any transient
                # failure) can leave the prior attempt's evidence in raw/ even
                # though THIS attempt's repeated sequence files overwrite
                # cleanly -- exactly the mixed-attempt archive this cleanup
                # exists to prevent (Codex review: "Fail closed when stale raw
                # inputs cannot be removed").
                #
                # Also skip wrapping data/broker with the persisting
                # decorators below: save_raw_input() writes through raw_dir
                # exactly as given, so for the symlink case it would still
                # follow the link and write real connector responses into
                # that untrusted target BEFORE the raw_recorder.error check
                # further down ever blocks the run (Codex review: "Stop
                # before recording through a symlinked raw directory"). With
                # raw_dir already known unsafe/unusable, nothing must be
                # recorded through it at all.
                raw_recorder.error = stale_cleanup_error
                # ...and STOP HERE, without fetching or computing anything
                # (Codex review, PR #593: "Stop before evaluating when raw
                # cleanup fails"). This used to leave data/broker unwrapped
                # and fall through into evaluate() anyway, blocking only
                # afterwards on the raw_recorder.error check below. Two things
                # were wrong with that:
                #   * the blocked artifact it published carried signal rows
                #     computed from live connector responses that were never
                #     persisted -- a direct violation of the persist-before-
                #     compute raw-input contract this whole subsystem exists
                #     to enforce (config/morning_dca.json:
                #     live_schedule_hardening_2026_07_20.raw_input_generation);
                #   * a connector raising during that pointless evaluation
                #     propagated out to _run_impl's except clause and replaced
                #     the concrete cleanup diagnosis with the generic
                #     startup-failure path, losing the real cause.
                # The archive is already known unusable, so no evaluation this
                # attempt could perform is admissible evidence; publish the
                # structured raw-persistence block immediately with NO signal
                # rows. Same shape as every other raw-persistence block in
                # this method: _blocked_live_result() stamps block_reason,
                # error_class="missing_or_invalid_raw_inputs", and the
                # raw_input_dir/raw_inputs_saved/raw_persistence_error trio,
                # preserves any earlier attempt's PLACED order evidence
                # wholesale, classifies the run as a trading failure, and
                # denies scheduled evidence -- exactly as the post-evaluate()
                # check below did. `source` is read off the unwrapped provider
                # by plain attribute access only; it makes no connector call.
                # Returning normally keeps the slot-lock lifecycle untouched:
                # _run_impl still releases it in its own ``finally``.
                return self._blocked_live_result(
                    now,
                    f"raw input persistence failed: {raw_recorder.error}",
                    [],
                    data_source or getattr(getattr(data, "source", None), "value", "unknown"),
                    error_class="missing_or_invalid_raw_inputs",
                    raw_recorder=raw_recorder,
                )
            else:
                data = _PersistingMarketDataProvider(data, raw_recorder)
                if broker is not None:
                    broker = cast(BrokerAdapter, _PersistingBrokerAdapter(broker, raw_recorder))
        signals = self.evaluate(now, data, manual=manual)
        source = data_source or getattr(getattr(data, "source", None), "value", "unknown")
        # NON-BLOCKING (Geoff, 2026-08-02: "should not block any later runs"):
        # a missing/invalid/forbidden same-day pre-flight artifact used to end
        # the run here with a blocked stub before any signal evaluation could
        # even use the full universe. morning_preflight_eligible_tickers()
        # (called from _apply_preflight_gate() inside evaluate() above) now
        # always degrades to the full configured universe with a warning
        # instead -- preflight_missing_block is never set anymore, so this
        # branch is dead and has been removed. The pre-flight artifact stays
        # a coarse pre-screen only, surfaced via self.preflight_gate /
        # artifacts["preflight_gate"] for observability, never a hard gate the
        # 10:30 slot depends on to run at all.
        if raw_recorder is not None and raw_recorder.error:
            # Raw-input persistence failed before any order could be placed --
            # evaluate() above only reads price data and never places an
            # order, so nothing has been submitted yet. Fail closed rather
            # than compute/place from a connector response that was never
            # durably recorded (config/morning_dca.json:
            # live_schedule_hardening_2026_07_20.raw_input_generation).
            return self._blocked_live_result(
                now,
                f"raw input persistence failed: {raw_recorder.error}",
                signals,
                source,
                error_class="missing_or_invalid_raw_inputs",
                raw_recorder=raw_recorder,
            )
        trading_day = now.astimezone(EASTERN).date()
        top_up_outcomes = self._compute_top_up(now, signals, manual=manual)
        # Sort by multiplier DESC, then drop_pct DESC, then ticker ASC so tied
        # multipliers are resolved by the larger-drop ticker first, matching the
        # allocation-priority policy in config/budgets.json.
        auto_buy_candidates = sorted(
            (s for s in self._buy_candidates(signals) if s.execution_status != EXEC_MANUAL_REVIEW_CANDIDATE),
            key=lambda s: (-s.multiplier, -(s.drop_pct or 0.0), s.ticker),
        )
        # Fold already-spent same-day capital from an earlier run of this same
        # stream (retry or manual rerun) into the ledger BEFORE snapshotting
        # remaining budget, so a fresh-process retry never re-spends the cap.
        # This only seeds WEEKLY spend from earlier days in the ISO week (plus
        # today's carry-forward candidate rows) -- today's own-stream spend is
        # already seeded above by _compute_top_up's cap_seed_roots scan, so
        # _seed_ledger_from_prior_runs must not also seed today's amount here
        # or the same on-disk artifact would be double-counted.
        self._seed_ledger_from_prior_runs(now)
        # Subtract prior same-day spend (recorded by _compute_top_up) from the
        # pre-run budget so caps account for earlier manual/recovery fills that
        # already consumed part of the daily/weekly cap on other tickers.
        # Restrict to the morning stream: midday has its own independent ledger
        # namespace and daily/weekly budget, so morning fills must not reduce
        # the midday cap even when prior_cap_seed_roots is configured.
        prior_daily_seeded = self._prior_daily_spend_seeded if self.stream == MORNING_STREAM else 0.0
        pre_run_remaining_daily = max(
            0.0, self.ledger.remaining_daily(trading_day) - prior_daily_seeded
        )
        pre_run_remaining_weekly = max(
            0.0, self.ledger.remaining_weekly(trading_day) - prior_daily_seeded
        )
        broker_outcomes: Optional[Dict[str, Any]] = None
        # Set below when raw persistence fails AFTER at least one candidate in
        # this call already reached "submitted" -- the artifact still needs to
        # be written (with that real submission intact) but stamped as blocked
        # rather than treated as a clean run. None on every other path.
        raw_persistence_partial_block: Optional[str] = None
        # Placement switch (MorningConfig.live_execution_enabled). Dropping the
        # broker reference here rather than short-circuiting the whole run routes
        # a disabled stream through the existing no-broker branch below, so it
        # still records hypothetical spend for cap/budget reporting and still
        # writes the complete artifact set -- a shadow run, not a skipped one.
        # A missing broker is only a blocking error when placement is enabled.
        execution_enabled = getattr(self.config, "live_execution_enabled", True)
        if not execution_enabled:
            broker = None
        if (
            execution_enabled
            and self.mode == MORNING_LIVE_MODE
            and self.trades
            and not manual
            and broker is None
        ):
            return self._blocked_live_result(
                now,
                "missing broker adapter for scheduled live execution",
                signals,
                source,
                # evaluate() may already have persisted market-data connector
                # responses via this recorder before broker wiring is even
                # checked -- omitting it here left the blocked artifact with
                # no raw_input_dir/raw_inputs_saved despite raw files
                # genuinely existing, which drift_detection.py's legacy-
                # exemption check (keyed on raw_input_dir's ABSENCE) then
                # misread as a pre-raw-persistence-contract artifact (Codex
                # review, PR #538: "Carry raw metadata through early broker
                # blocks").
                raw_recorder=raw_recorder,
            )
        if broker is not None and self.mode == MORNING_LIVE_MODE:
            expected_account = getattr(self.config, "target_account_number", None)
            # Fail closed BEFORE the comparison below. `expected_account and ...`
            # short-circuits on a falsy value, so an empty/absent
            # target_account_number used to skip account pinning altogether and let
            # a live run place against whatever account the adapter was wired to.
            # near_close_dca.build_broker refuses to even construct a live adapter
            # in that state; this path shares its predicate so both live streams
            # enforce the same rule.
            pinning_failure = account_pinning_failure(expected_account)
            if pinning_failure:
                blocked = f"live account pinning unavailable: {pinning_failure}"
                if not manual:
                    return self._blocked_live_result(now, blocked, signals, source, raw_recorder=raw_recorder)
                raise RuntimeError(blocked)
            try:
                actual_account = broker.account_number()
            except Exception as exc:  # noqa: BLE001 - broker adapters raise arbitrary connector errors
                if not manual:
                    return self._blocked_live_result(
                        now,
                        f"failed to retrieve broker account number: {exc}",
                        signals,
                        source,
                        raw_recorder=raw_recorder,
                    )
                raise RuntimeError(f"Failed to retrieve broker account number: {exc}") from exc
            if raw_recorder is not None and raw_recorder.error:
                return self._blocked_live_result(
                    now,
                    f"raw input persistence failed: {raw_recorder.error}",
                    signals,
                    source,
                    error_class="missing_or_invalid_raw_inputs",
                    raw_recorder=raw_recorder,
                )
            if expected_account and actual_account != expected_account:
                mismatch = f"broker account mismatch: expected {expected_account}, got {actual_account}"
                if not manual:
                    return self._blocked_live_result(now, mismatch, signals, source, raw_recorder=raw_recorder)
                raise RuntimeError(mismatch)
            try:
                broker_outcomes = self._place_broker_orders(
                    auto_buy_candidates,
                    trading_day,
                    broker,
                    top_up_outcomes,
                    remaining_daily=pre_run_remaining_daily,
                    remaining_weekly=pre_run_remaining_weekly,
                    manual=manual,
                    data=data,
                    now=now,
                )
            except _BuyingPowerLookupError as exc:
                if raw_recorder is not None and raw_recorder.error:
                    # The connector wrapper already recorded this exact failure
                    # on raw_recorder.error (available_buying_power() raised
                    # through the persisted connector). Preferring the raw-
                    # persistence classification -- and passing raw_recorder so
                    # raw_input_dir/raw_inputs_saved/raw_persistence_error are
                    # stamped -- keeps this consistent with every other raw-
                    # persistence-failure path; without it the drift detector
                    # would classify this as an empty/missing-order problem
                    # instead of the raw-input failure that actually stopped
                    # placement (Codex review: "Preserve raw-failure metadata
                    # on buying-power exceptions").
                    return self._blocked_live_result(
                        now,
                        f"raw input persistence failed: {raw_recorder.error}",
                        signals,
                        source,
                        error_class="missing_or_invalid_raw_inputs",
                        raw_recorder=raw_recorder,
                    )
                # Market data and earlier broker checks are already persisted via
                # raw_recorder by the time buying-power lookup fails here -- pass
                # it through like the other raw-persistence-failure branches so
                # the blocked artifact still carries raw_input_dir/raw_inputs_saved
                # for the raw files that actually exist on disk, instead of
                # silently dropping them from the audit/reporting contract (Codex
                # review, PR #538 discussion_r3837449175: "Retain raw-input
                # metadata on buying-power failures").
                return self._blocked_live_result(
                    now,
                    f"failed to retrieve broker buying power: {exc}",
                    signals,
                    source,
                    error_class="buying_power_lookup_failed",
                    raw_recorder=raw_recorder,
                )
            if raw_recorder is not None and raw_recorder.error:
                # _place_broker_orders checks after every persisted broker or
                # market-data input and stops before the next submission.
                # Convert that stopped execution into the promised valid
                # failure-state artifact rather than merely annotating a run
                # that may otherwise look eligible for execution -- UNLESS an
                # earlier candidate in this same call already reached
                # "submitted" before persistence failed on a later one.
                # _blocked_live_result() writes broker_outcomes={} and no
                # buy_candidates rows, so calling it here would silently drop
                # that already-placed order from the ledger and same-day
                # duplicate-check artifact. Fail closed with an empty
                # broker_outcomes only when nothing was actually submitted;
                # otherwise fall through to the normal record/build/write path
                # below so the real submission is recorded, and stamp the
                # block reason onto that artifact afterward so it is never
                # mistaken for a fully clean run (Codex/Copilot review,
                # PR #420: "Preserve orders submitted before a later
                # persistence failure").
                any_submitted = any(
                    isinstance(oc, dict) and oc.get("status") == "submitted"
                    for oc in broker_outcomes.values()
                )
                # A placement attempt that RAISED (the injected _place_fn
                # itself failing, e.g. an ambiguous broker timeout) has
                # already recorded a structured "failed" outcome in
                # broker_outcomes by the time raw_recorder.error is checked
                # here. _blocked_live_result()'s pre-placement stub always
                # writes broker_outcomes={} and routes through
                # _apply_blocked_before_placement_fields' "never reached
                # placement" classification -- calling it here would discard
                # that already-recorded broker failure and its error detail,
                # reporting a generic raw-input failure instead of the real
                # placement outcome (Codex review, PR #538
                # discussion_r3837630723: "Preserve broker failures when
                # connector calls raise"). Any recorded placement outcome
                # (submitted OR failed) must fall through to the normal
                # record/build/write path below, which passes broker_outcomes
                # into build_artifacts() and lets write_artifacts's
                # _apply_no_order_classification() derive the correct
                # placement-failure classification for a zero-submission
                # batch via classify_no_order_run's
                # placement_attempted_and_failed flag.
                any_placement_outcome = any(
                    isinstance(oc, dict) and oc.get("status") in ("submitted", "failed")
                    for oc in broker_outcomes.values()
                )
                if not any_placement_outcome:
                    return self._blocked_live_result(
                        now,
                        f"raw input persistence failed: {raw_recorder.error}",
                        signals,
                        source,
                        error_class="missing_or_invalid_raw_inputs",
                        raw_recorder=raw_recorder,
                    )
                if any_submitted:
                    # A real order was submitted and IS recorded in this
                    # artifact -- see the raw_persistence_partial_block
                    # handling below, which stamps the FAILED_AFTER_SUBMISSION
                    # outcome onto it explicitly.
                    raw_persistence_partial_block = f"raw input persistence failed: {raw_recorder.error}"
                # else: a "failed" placement outcome with no submission at all
                # -- nothing to stamp here. write_artifacts's
                # _apply_no_order_classification() sees the "failed" row via
                # placement_attempted_and_failed and records the correct
                # RUN_CLASSIFICATION_PLACEMENT_FAILED/is_trading_failure/
                # scheduled-evidence-denying outcome on its own; forcing the
                # submission-flavored raw_persistence_partial_block stamp here
                # too would mislabel a run that never actually submitted
                # anything as "failed_after_partial_submission".
        # In live mode record only candidates whose orders were actually submitted
        # so cap-exceeded orders do not inflate the ledger and corrupt budget reporting.
        # A "duplicate_preserved" outcome (a same-stream top-up duplicate retry)
        # is excluded too: its amount is already reflected in the cap-seeding
        # filesystem scan of this same dated-slot artifact, so booking it again
        # here would double-count it on every retry (Codex review, PR #88:
        # "Avoid booking same-stream top-up retries a second time").
        if broker_outcomes is not None:
            submitted_candidates = [
                c for c in auto_buy_candidates
                if broker_outcomes.get(c.ticker, {}).get("status") == "submitted"
                and not broker_outcomes.get(c.ticker, {}).get("duplicate_preserved")
            ]
            self.record_buy_candidates(
                trading_day,
                submitted_candidates,
                top_up_outcomes,
                broker_outcomes,
                _track_own="manual" if manual else "scheduled",
            )
            if not manual:
                # record_buy_candidates's REPLACE-semantics reset for
                # "scheduled" only accumulates over submitted_candidates
                # (excluding duplicate_preserved), since those must not be
                # booked into self.ledger a second time. But the dated-slot
                # artifact this call is about to write to disk (write_artifacts,
                # below) carries BOTH freshly placed and duplicate-preserved
                # "submitted" rows -- the cap-seeding watermark needs the FULL
                # on-disk submitted total to correctly credit back everything
                # a future scan of this same file would see, not just the
                # subset that was freshly booked into the ledger this call.
                self._own_recorded_scheduled[trading_day] = sum(
                    oc.get("submitted_amount_usd", 0.0) or 0.0
                    for oc in broker_outcomes.values()
                    if isinstance(oc, dict) and oc.get("status") == "submitted"
                )
        else:
            # No broker in live mode: nothing was submitted, so record nothing in
            # the ledger. A phantom hypothetical amount would inflate the cap and
            # block cap-seeding's later pickup of a real external fill. Scoped to
            # the morning stream specifically: MIDDAY_LIVE_MODE == MORNING_LIVE_MODE
            # == "live", so mode alone can't tell the midday executor apart, and it
            # still needs hypothetical spend recorded for its own cap/budget
            # reporting. Watchlist/preflight modes always reach this branch and
            # still need hypothetical spend recorded for cap/budget reporting.
            morning_live_no_broker = self.mode == MORNING_LIVE_MODE and self.stream == MORNING_STREAM
            candidates_to_record = [] if morning_live_no_broker else auto_buy_candidates
            self.record_buy_candidates(trading_day, candidates_to_record, top_up_outcomes, _track_own=None)
        artifacts = self.build_artifacts(now, signals, source, manual=manual, top_up_outcomes=top_up_outcomes, broker_outcomes=broker_outcomes)
        if raw_persistence_partial_block:
            # A real order above was submitted and IS recorded in this
            # artifact -- unlike _blocked_live_result()'s empty stub -- but a
            # later candidate never got evaluated because its raw audit write
            # failed. Mark the run blocked so operators/watchdogs see the
            # failure instead of a run that looks fully clean.
            artifacts["status"] = "blocked"
            artifacts["block_reason"] = raw_persistence_partial_block
            artifacts["error_class"] = "missing_or_invalid_raw_inputs"
            # _apply_no_order_classification() (below, via write_artifacts) sees
            # a "submitted" outcome already present with no failed/blocked rows
            # of its own -- the never-evaluated later candidate simply has no
            # broker_outcomes entry at all -- so it returns EARLY without ever
            # setting run_classification_outcome/is_trading_failure, and
            # derive_valid_scheduled_evidence() has no scheduled_evidence_denied
            # marker to fail closed on. Left unset, this partial-persistence-
            # failure-after-submission run reads as ordinary valid "the
            # scheduled slot ran successfully" evidence to every downstream
            # watchdog/recovery consumer, even though a real failure occurred.
            # Stamp all three explicitly here so classification is correct
            # however _apply_no_order_classification treats the submitted rows
            # (Codex review, PR #538 discussion_r3837449173: "Deny scheduled
            # evidence for partial persistence failures").
            artifacts["run_classification_outcome"] = RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION
            artifacts["is_trading_failure"] = True
            # deny_scheduled_evidence() sets both the top-level
            # scheduled_evidence_denied flag AND the nested
            # run_classification.valid_scheduled_evidence=False. A bare
            # top-level assignment left the nested field at its earlier
            # `true` value, so scripts/generate_credit_usage_proxy_report.py's
            # _classify_run (which reads the nested field directly) still
            # counted this partial-persistence-failure run as valid scheduled
            # evidence (Codex review, PR #538 discussion_r3837630717: "Clear
            # nested evidence validity on partial failures").
            artifacts.update(deny_scheduled_evidence(artifacts))
        if broker_outcomes is not None:
            # Update buy_candidate rows with broker outcome so downstream
            # load_prior_same_day_allocations can read "submitted" status,
            # planned_amount_usd, and decision_price for same-day top-up guards.
            signal_by_ticker = {s.ticker: s for s in auto_buy_candidates}
            for row in artifacts.get("buy_candidates", []):
                ticker = row.get("ticker")
                if ticker and ticker in broker_outcomes:
                    oc = broker_outcomes[ticker]
                    row["status"] = oc.get("status", "not_submitted")
                    row["order_id"] = oc.get("order_id")
                    if "submitted_amount_usd" in oc:
                        row["planned_amount_usd"] = oc["submitted_amount_usd"]
                    # Persist the decision price so _candidate_logged_reference_price
                    # can resolve the top-up guard's reference price for later runs.
                    sig = signal_by_ticker.get(ticker)
                    if sig and sig.current_price and "decision_price" not in row:
                        row["decision_price"] = sig.current_price
            artifacts["broker_outcomes"] = broker_outcomes
            # Reconcile auto_buy_signal in signal rows against final placement
            # outcomes. Pre-placement eligibility sets it True, but top-up guards
            # (price_not_below, no_incremental) and duplicate-skip on retry leave
            # no submitted order for that ticker. The drift detector treats every
            # True row without a submitted order as missing.
            #
            # Only clear for deliberate non-submissions:
            #   "skipped"/"cap_exceeded" — guard fired or budget cap blocked
            #   "dry_run" — DryRunBroker intentionally placed no real order;
            #     write_artifacts() only emits submitted_orders.csv rows for
            #     placed/in-flight statuses (submitted/filled/pending/unconfirmed)
            #     or an order_id, so a dry-run candidate never gets an order row
            #     and would otherwise false-alert every run
            #   blocked with deliberate_skip=True — safety gate (ceiling, spread,
            #     tradability, buying power, review blocked); these are explicit
            #     strategy decisions, not unexpected failures
            # Keep auto_buy_signal=True for genuine failures ("failed",
            # "not_submitted_confirmation_required", and "blocked" without
            # deliberate_skip, e.g. spread-quote errors) so drift alerts fire.
            _deliberate_skip_tickers = {
                t for t, oc in broker_outcomes.items()
                if oc.get("status") in ("skipped", "cap_exceeded", "dry_run")
                or (oc.get("status") == "blocked" and oc.get("deliberate_skip"))
            }
            for _row in artifacts.get("signal_rows", []):
                if _row.get("auto_buy_signal") and _row.get("ticker") in _deliberate_skip_tickers:
                    _row["auto_buy_signal"] = False
        if not manual and self._prior_same_day_candidate_rows:
            # A scheduled retry is about to overwrite the fixed scheduled
            # path; carry forward any earlier same-day submitted/filled rows
            # (found by _seed_ledger_from_prior_runs) for tickers this run
            # didn't itself re-submit, so the persisted artifact keeps the
            # afternoon top-up guard's visibility into that earlier fill.
            # Only suppress carry-forward when the new artifact already has a
            # submitted/filled row for that ticker. A skipped/cap_exceeded retry
            # row must NOT shadow the real submitted row from the first run.
            submitted_tickers = {
                row.get("ticker")
                for row in artifacts["buy_candidates"]
                if row.get("status") in ("submitted", "filled")
            }
            carried_forward = [
                dict(row)
                for row in self._prior_same_day_candidate_rows
                if row.get("ticker") not in submitted_tickers
            ]
            if carried_forward:
                artifacts["buy_candidates"] = carried_forward + artifacts["buy_candidates"]
                artifacts["counts"]["buy_candidates"] = len(artifacts["buy_candidates"])
                # These rows are this stream's OWN earlier scheduled submissions
                # being re-persisted into the artifact this call is about to
                # write, not a fresh submission this call placed. Fold their
                # total into _own_recorded_scheduled too -- otherwise a later
                # cap_seed_roots scan of this same on-disk file (_compute_top_up)
                # would see them as if they were a brand-new external fill and
                # double-count them into the ledger.
                carried_forward_total = 0.0
                for _cf_row in carried_forward:
                    _cf_amt = _cf_row.get("planned_amount_usd")
                    if _cf_amt is None:
                        _cf_amt = _cf_row.get("hypothetical_amount_usd")
                    if _cf_amt is None:
                        continue
                    try:
                        _cf_amt = float(_cf_amt)
                    except (TypeError, ValueError):
                        continue
                    if _cf_amt > 0:
                        carried_forward_total += _cf_amt
                if carried_forward_total > 0:
                    self._own_recorded_scheduled[trading_day] = (
                        self._own_recorded_scheduled.get(trading_day, 0.0) + carried_forward_total
                    )
        if raw_recorder is not None:
            artifacts["raw_input_dir"] = str(raw_dir)
            artifacts["raw_inputs_saved"] = raw_recorder.saved_filenames
            if raw_recorder.error:
                # Persistence failed only after order placement had already
                # begun (evaluate()-stage failures are caught above, before
                # any order is placed) -- the safeguards that validated each
                # already-placed order (budget, tradability, spread, trend)
                # are unaffected by a failure to also write its audit copy to
                # disk, so this does not retroactively block the run; it is
                # surfaced here for operator visibility instead.
                artifacts["raw_persistence_error"] = raw_recorder.error
        if self.mode == MORNING_LIVE_MODE and self.trades:
            _apply_no_order_classification(artifacts, scheduled=not manual)
        paths = self.write_artifacts(artifacts, now)
        # Fold this run's own run_id into the seen-set so a LATER run() call on
        # this SAME executor instance never re-reads this just-written artifact
        # from disk and double-counts spend already reflected in self.ledger
        # (via record_buy_candidates above).
        self._seeded_prior_run_ids.add(artifacts["run_id"])
        # execution_enabled=False (market_open_dca.json/intraday_dca.json's
        # "SHIPS DISABLED" shadow mode) drops the broker above, so
        # broker_outcomes stays None and the auto_buy_signal reconciliation
        # block that clears it for deliberate skips/dry-runs never runs --
        # every eligible signal row keeps auto_buy_signal=True while
        # submitted_orders.csv is empty by design. Comparing the two would
        # report every deliberate shadow candidate as an unexpectedly missing
        # order (and potentially alert Slack) purely because placement itself
        # is switched off, not because anything actually went wrong (Codex
        # review: "Suppress drift alerts for deliberate shadow runs").
        if self.mode == MORNING_LIVE_MODE and self.trades and not manual and execution_enabled:
            _run_drift_check(Path(paths["run_state"]).parent)
        return {"artifacts": artifacts, "paths": paths}




def _merge_placements_into_state(
    state: Dict[str, Any],
    placed: Dict[str, Any],
    buy_candidates: Sequence[Dict[str, Any]],
    *,
    error_detail: str = "",
) -> Dict[str, Any]:
    """Fold this invocation's placements into a state written by an earlier one.

    A same-slot retry can raise before overwriting the previous attempt's
    run_state.json. Preserving that file is right, but dropping the orders this
    attempt actually placed is not: they would be invisible to the same-day
    allocation guards and a later run would buy them again.
    """
    if not placed:
        return state
    merged = dict(state)
    added = False
    outcomes = dict(merged.get("broker_outcomes") or {})
    for ticker, outcome in placed.items():
        if ticker not in outcomes:
            outcomes[ticker] = outcome
            added = True
    merged["broker_outcomes"] = outcomes

    existing_rows = list(merged.get("buy_candidates") or [])
    seen = {
        row.get("order_id") or f"ticker:{row.get('ticker')}"
        for row in existing_rows
        if isinstance(row, dict)
    }
    for row in buy_candidates:
        key = row.get("order_id") or f"ticker:{row.get('ticker')}"
        if key not in seen:
            seen.add(key)
            existing_rows.append(row)
            added = True
    if not added:
        # Every placement is already recorded in the preserved state: this run
        # wrote its own artifacts and only a later step raised. There is nothing
        # to add and nothing to re-label, so the record stands untouched.
        return state
    merged["buy_candidates"] = existing_rows
    merged["orders_submitted_count"] = len(_submitted_keys(outcomes, existing_rows))
    merged["counts"] = {
        **(merged.get("counts") or {}),
        "submitted": merged["orders_submitted_count"],
    }
    # THIS invocation raised, so the merged state is a failure. Leaving the
    # earlier attempt's successful or no-order classification in place would let
    # it be republished as valid even though the retry failed after accepting an
    # order (Codex review). Shared with near_close_dca so the two cannot drift.
    return stamp_failure_classification(
        merged,
        submitted=merged["orders_submitted_count"],
        error_detail=error_detail,
    )


def _unmatched_outcome_order_ids(
    broker_outcomes: Dict[str, Any], buy_candidates: Sequence[Dict[str, Any]]
) -> Dict[Any, List[Any]]:
    """Placed broker-outcome order ids that no candidate row already claims.

    Codex review, PR #593: "Deduplicate ID-bearing outcomes against id-less
    pending rows". The mirror image of :func:`_id_less_order_identity`. Giving
    a repeated id-less row its own sequenced identity is right when nothing
    else describes that order, but a ``pending``/``unconfirmed`` candidate row
    can legitimately lack an order id while the broker outcome for that SAME
    physical order already carries one: the outcome loop records the real id,
    the candidate line then minted a separate ``"ticker:AAA"`` identity, and
    one order was counted twice -- ``orders_submitted_count`` overstated and an
    extra id-less row duplicated into the canonical submitted_orders.csv.

    Returns, per ticker, the placed outcomes' order ids in ``broker_outcomes``
    iteration order, so :func:`_id_less_order_identity` can consume one for an
    id-less row instead of inventing an identity. An id already carried by ANY
    candidate row is excluded: that row is the outcome's own candidate, and
    an id-less row alongside it is therefore a DIFFERENT order that must keep
    its own identity (this is deliberately not screened on
    ``PLACED_ORDER_STATUSES`` -- a claim is a claim whatever status the
    claiming row reports, and the conservative reading never merges two real
    orders).

    The outcome's own ``ticker`` field wins over the mapping key so a
    composite ``"<ticker>:<order-id>"`` key -- the shape
    ``scripts/scheduled_artifacts.submitted_order_rows`` supports for a
    same-ticker retry's second order -- still lines up with the plain ticker a
    candidate row carries.
    """
    claimed = {
        row.get("order_id") for row in buy_candidates or [] if isinstance(row, dict) and row.get("order_id")
    }
    unmatched: Dict[Any, List[Any]] = {}
    for key, outcome in (broker_outcomes or {}).items():
        if not isinstance(outcome, dict) or outcome.get("status") not in PLACED_ORDER_STATUSES:
            continue
        order_id = outcome.get("order_id")
        if not order_id or order_id in claimed:
            continue
        unmatched.setdefault(outcome.get("ticker") or key, []).append(order_id)
    return unmatched


def _id_less_order_identity(
    ticker: Any,
    sequence: Dict[Any, int],
    unmatched_outcome_order_ids: Optional[Dict[Any, List[Any]]] = None,
) -> str:
    """Identity for a placed candidate row that carries no order id yet.

    Codex review, PR #593: "Preserve repeated id-less placed rows separately".
    ``_submitted_keys``/``_carried_forward_order_rows`` keyed every id-less row
    as the bare ``"ticker:AAA"``, so two DISTINCT placed rows for one ticker
    with no order id -- the shape a same-slot retry legitimately produces while
    both orders are still ``pending``/``unconfirmed`` -- collapsed into one
    key: the slot was recorded as a single submission and the canonical
    submitted_orders.csv lost the second order and its notional entirely.
    Reached through ``_blocked_live_result``'s malformed-state graft, which
    carries an earlier attempt's rows forward verbatim.

    The FIRST id-less row for a ticker keeps the historic bare
    ``"ticker:AAA"`` key: ``broker_outcomes`` is keyed by ticker and so can
    hold at most one id-less entry per ticker, and that entry must still
    dedupe against the matching candidate row exactly as it always did. Only
    the repeats are sequence-numbered, mirroring the weekly dashboard's
    ``candidate_sequence`` (``"<ticker>:candidate#<n>"``, see
    ``dca_weekly_dashboard.load_json_files``) so the two code paths that solve
    this same problem stay recognisably the same shape.

    The synthetic suffix cannot collide with a real order id: a real id is used
    as the key verbatim, with no ``"ticker:"`` prefix -- the assumption these
    helpers have always made -- and the ``":candidate#"`` infix narrows it
    further.

    ``sequence`` is the caller's per-collection counter, mutated in place; each
    collection starts a fresh one so a candidate row and the broker outcome
    that describes the SAME order still agree on their key.

    ``unmatched_outcome_order_ids`` (see :func:`_unmatched_outcome_order_ids`)
    is consulted FIRST and consumed in place: when a placed broker outcome for
    this ticker carries an order id no candidate row claims, this row is that
    outcome's candidate and takes its real id as the identity, so the pair
    counts as the one physical order it is (Codex review, PR #593:
    "Deduplicate ID-bearing outcomes against id-less pending rows"). Only once
    that pool is empty does a row fall through to the bare/sequenced synthetic
    identity -- so more id-less rows than outcomes still yields one distinct
    identity per row, and more outcomes than id-less rows leaves the surplus
    outcomes keyed by their own ids: neither direction drops a real order.
    """
    pool = (unmatched_outcome_order_ids or {}).get(ticker)
    if pool:
        return pool.pop(0)
    seen = sequence.get(ticker, 0)
    sequence[ticker] = seen + 1
    if seen == 0:
        return f"ticker:{ticker}"
    return f"ticker:{ticker}:candidate#{seen}"


def _submitted_keys(
    broker_outcomes: Dict[str, Any], buy_candidates: Sequence[Dict[str, Any]]
) -> set:
    """Distinct submissions for this slot, from this call AND earlier attempts.

    A scheduled retry overwrites the fixed-path artifact, and _run_impl prepends
    the earlier attempt's submitted rows so their evidence survives. Counting only
    the current ``broker_outcomes`` would let a retry that places nothing new be
    recorded as a zero-order run for a slot that did place orders, and would drop
    those orders from submitted_orders.csv (Codex review).

    Keyed by order_id when present, else by ticker, so a row appearing in both
    collections is counted once -- except that a REPEATED id-less placed row
    for the same ticker gets its own sequence-numbered identity rather than
    collapsing into the first row's key (see :func:`_id_less_order_identity`),
    so two in-flight orders for one ticker count as two submissions -- and
    except that an id-less row whose order ALREADY has an id-bearing broker
    outcome adopts that real id instead of an identity of its own, so the pair
    still counts once (see :func:`_unmatched_outcome_order_ids`).

    Both loops screen on ``PLACED_ORDER_STATUSES`` -- every real placed status,
    not the old ``submitted``/``filled`` literals (Codex review, PR #593:
    "Preserve pending rows through the malformed-state fallback"). This helper
    is what ``_blocked_live_result``'s graft fallback and the failure-merge
    path use to size ``orders_submitted_count``, so with the narrow literals a
    genuinely in-flight ``pending``/``unconfirmed`` order -- exactly the kind
    ``_existing_placed_run_state`` now admits -- was counted as zero
    submissions and the slot was republished as a no-order run.
    """
    keys: set = set()
    for ticker, outcome in (broker_outcomes or {}).items():
        if isinstance(outcome, dict) and outcome.get("status") in PLACED_ORDER_STATUSES:
            keys.add(outcome.get("order_id") or f"ticker:{ticker}")
    # Fresh per-collection counter: the candidate loop's FIRST id-less row for
    # a ticker mints the same bare "ticker:AAA" key the outcomes loop above
    # would have, so the two collections still describe one order once.
    candidate_sequence: Dict[Any, int] = {}
    # ...and the id-bearing outcomes that no candidate row claims, so an
    # id-less row for one of those tickers reconciles onto the real order id
    # instead of minting a second identity for one physical order.
    unmatched_outcome_order_ids = _unmatched_outcome_order_ids(broker_outcomes, buy_candidates)
    for row in buy_candidates or []:
        if not isinstance(row, dict) or row.get("status") not in PLACED_ORDER_STATUSES:
            continue
        keys.add(
            row.get("order_id")
            or _id_less_order_identity(row.get("ticker"), candidate_sequence, unmatched_outcome_order_ids)
        )
    return keys


def _merge_candidate_amount_into_row(
    outcome_rows: Optional[List[Dict[str, Any]]],
    key: Any,
    candidate: Dict[str, Any],
) -> None:
    """Fill a placed candidate's notional into the outcome row that replaced it.

    ``key`` is the order identity ``_carried_forward_order_rows`` computed for
    ``candidate`` -- a real ``order_id``, or the ``ticker:<symbol>`` stand-in an
    id-less row gets -- and it is already present in that function's ``current``
    set, so some row in ``outcome_rows`` describes this same physical order.
    Rows are keyed here exactly as ``current`` keys them, so the two agree.

    Writes only into a row whose ``submitted_amount_usd`` is missing or empty:
    an amount the broker itself reported is the authoritative one and is never
    overwritten by a planned amount. Silently does nothing when the caller had
    no row list to merge into (``outcome_rows is None``).
    """
    if outcome_rows is None:
        return
    amount = candidate.get("submitted_amount_usd")
    if amount in (None, ""):
        amount = candidate.get("planned_amount_usd")
    if amount in (None, ""):
        return
    for target in outcome_rows:
        if not isinstance(target, dict):
            continue
        if (target.get("order_id") or f"ticker:{target.get('ticker')}") != key:
            continue
        if target.get("submitted_amount_usd") in (None, ""):
            target["submitted_amount_usd"] = amount
        return


def _carried_forward_order_rows(
    broker_outcomes: Dict[str, Any],
    buy_candidates: Sequence[Dict[str, Any]],
    outcome_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """submitted_orders.csv rows for submissions only the candidate rows carry.

    The canonical order record must include an earlier attempt's accepted orders,
    or a retry that placed nothing new would rewrite the CSV without them.

    Screens on ``PLACED_ORDER_STATUSES`` for the same reason ``_submitted_keys``
    does (Codex review, PR #593: "Preserve pending rows through the
    malformed-state fallback"): a carried-forward ``pending``/``unconfirmed``
    row is a real order at the broker, and dropping it here rewrote the
    canonical submitted_orders.csv as header-only for a slot that had one
    in flight.

    Repeated id-less rows for one ticker are sequence-numbered exactly as
    ``_submitted_keys`` numbers them (see :func:`_id_less_order_identity`):
    keyed on the bare ticker alone, a second pending row for the same ticker
    matched the first row's key and was dropped from the canonical CSV
    together with its notional (Codex review, PR #593: "Preserve repeated
    id-less placed rows separately").

    ``outcome_rows`` is the ``submitted_order_rows()`` list this function's
    return value is appended to. Pass it wherever that list is available: a
    candidate skipped here as already-recorded can be the ONLY carrier of its
    own notional, and the outcome row standing in for it need not have one.
    That is exactly the reconciled shape -- an id-less placed candidate
    adopting an ID-bearing outcome's order id (Codex review, PR #593:
    "Reconcile ID-bearing outcomes with id-less candidate rows") lands in
    ``current`` and is skipped, while ``submitted_order_rows()`` builds the
    sole canonical row for that one order out of the outcome, which omits
    ``submitted_amount_usd`` -- so a real order's notional disappeared from
    submitted_orders.csv. Rather than emit a second row for one physical
    order, the candidate's amount is MERGED into the outcome row it folded
    into (Codex review, PR #593: "Preserve candidate notional when folding
    into outcome rows"). Only an outcome row carrying no amount of its own is
    filled in: the broker's own reported notional always wins.
    """
    current = {
        outcome.get("order_id") or f"ticker:{ticker}"
        for ticker, outcome in (broker_outcomes or {}).items()
        if isinstance(outcome, dict) and outcome.get("status") in PLACED_ORDER_STATUSES
    }
    extra: List[Dict[str, Any]] = []
    # Fresh per-collection counter, for the same reason _submitted_keys keeps
    # one: the first id-less candidate row for a ticker must still match the
    # id-less broker outcome for that ticker in `current` above and be skipped
    # as already recorded, while a second one is a distinct order and is
    # carried forward.
    candidate_sequence: Dict[Any, int] = {}
    # Same reconciliation pool _submitted_keys() builds: an id-less row whose
    # order already appears in broker_outcomes under a real id takes that id,
    # lands in `current` above, and is skipped here -- submitted_order_rows()
    # already emits the outcome's own row, so carrying the candidate forward
    # too would duplicate the order and its notional in the canonical CSV.
    unmatched_outcome_order_ids = _unmatched_outcome_order_ids(broker_outcomes, buy_candidates)
    for row in buy_candidates or []:
        if not isinstance(row, dict) or row.get("status") not in PLACED_ORDER_STATUSES:
            continue
        key = row.get("order_id") or _id_less_order_identity(
            row.get("ticker"), candidate_sequence, unmatched_outcome_order_ids
        )
        if key in current:
            # Already recorded -- by an outcome row, or by an earlier candidate
            # row carried forward above. Before dropping this row, hand its
            # notional to the row that stands in for it when that row has none
            # (see the ``outcome_rows`` paragraph in the docstring).
            _merge_candidate_amount_into_row(outcome_rows, key, row)
            continue
        current.add(key)
        extra.append(
            {
                "ticker": row.get("ticker"),
                "order_id": row.get("order_id"),
                "submitted_amount_usd": row.get("planned_amount_usd"),
                "status": row.get("status"),
            }
        )
    return extra


def _apply_blocked_before_placement_fields(artifacts: Dict[str, Any]) -> None:
    """Stamp the outcome for a live run stopped BEFORE placement.

    Used by the paths that write and return without reaching
    :func:`_apply_no_order_classification`: a missing same-day pre-flight
    artifact, an absent broker adapter, a wrong live account, or a raw-input
    persistence failure. None of these is a completed no-order run, and none is a
    placement failure either, so they take the "never reached placement" outcome
    and the specific cause stays in ``error_class`` (a concrete one the caller
    already set is kept).
    """
    artifacts.update(
        {
            "run_classification_outcome": RUN_CLASSIFICATION_STARTUP_FAILED,
            "order_submit_status": ORDER_SUBMIT_STATUS_ERROR,
            "completion_reason": None,
            "is_trading_failure": True,
            "orders_submitted_count": 0,
            "candidates_reaching_spread_gate": 0,
            "candidates_blocked_missing_spread": 0,
        }
    )
    if artifacts.get("error_class") is None:
        artifacts["error_class"] = ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT
    # None of these paths reached placement, so none is evidence the slot
    # executed. build_artifacts() has already stamped the scheduled identity and
    # the callers write immediately, so without the denial the pointer publishes
    # a missing pre-flight or an account mismatch as a valid scheduled run
    # (Codex review). Same denial the escaping-exception path applies, through
    # the same helper.
    artifacts.update(deny_scheduled_evidence(artifacts))


def _apply_no_order_classification(
    artifacts: Dict[str, Any], *, scheduled: bool = True
) -> None:
    """Record how a live run that submitted zero orders is classified.

    Separates no-order logic from failures per
    config/scheduled_tasks.json:spread_data_availability_policy. The spread verdict
    is carried per row by ``_place_broker_orders``, so the two counts this needs --
    how many candidates reached the spread gate, and how many of those were failed
    closed for want of a proven spread -- are read straight off the outcomes rather
    than re-derived from skip-reason prose.

    Also mirrors each row's spread verdict onto the matching signal row so the
    signals CSV carries it. A run that DID submit an order is left untouched: it is
    not a no-order run and keeps whatever status the placement path recorded.
    """
    outcomes = artifacts.get("broker_outcomes") or {}
    if not isinstance(outcomes, dict):
        outcomes = {}

    spread_by_ticker = {
        ticker: outcome
        for ticker, outcome in outcomes.items()
        if isinstance(outcome, dict) and outcome.get("spread_status")
    }
    for row in artifacts.get("signal_rows") or []:
        outcome = spread_by_ticker.get(row.get("ticker"))
        if outcome is None:
            continue
        for key in ("spread_ok", "spread_pct", "spread_status"):
            row[key] = outcome.get(key)

    rows = [outcome for outcome in outcomes.values() if isinstance(outcome, dict)]

    failed = 0
    connector_errors = 0
    for outcome in rows:
        status = outcome.get("status")
        if status == "failed":
            failed += 1
        elif status == "blocked" and not outcome.get("deliberate_skip"):
            connector_errors += 1

    # A pre-placement broker check that RAISED (duplicate lookup, spread quote,
    # tradability, review) is recorded as a non-deliberate block, not as 'failed'.
    # Both zero-order and submitted paths count these as errors, so batches that
    # lose a ticker to connector failure are classified as failures instead of
    # ordinary successes (Codex review). Deliberate safety gates carry
    # deliberate_skip=True and are never errors.
    # Count THIS call's submissions plus any carried forward from an earlier
    # attempt at the same scheduled slot. _run_impl prepends those rows so the
    # fixed-path artifact keeps their evidence; counting only the current
    # broker_outcomes would let a retry that places nothing new be recorded as a
    # zero-order run for a slot that did place orders (Codex review).
    submitted = len(
        _submitted_keys(outcomes, artifacts.get("buy_candidates") or [])
    )
    if submitted:
        artifacts["orders_submitted_count"] = submitted
        # build_artifacts does not set this field, so without it even a fully
        # successful batch wrote a run_state with no aggregate submit status and
        # a run_summary whose order_submit_status was null (Codex review).
        artifacts["order_submit_status"] = ORDER_SUBMIT_STATUS_SUBMITTED
        if failed or connector_errors:
            # A partially submitted batch: some orders landed and another ticker
            # was lost -- either its placement raised, or a pre-placement broker
            # check did. Returning here as an ordinary success would leave the
            # run with no is_trading_failure and no failure outcome even though
            # an intended order never happened (Codex review). The submitted
            # count and status are preserved; only the failure is added.
            artifacts["run_classification_outcome"] = RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION
            artifacts["is_trading_failure"] = True
            artifacts["orders_failed_count"] = failed
            artifacts.setdefault(
                "error_class",
                ERROR_CLASS_PARTIAL_PLACEMENT_FAILURE
                if failed
                else ERROR_CLASS_DEGRADED_MARKET_DATA,
            )
        return

    reaching_gate = sum(
        1
        for outcome in spread_by_ticker.values()
        if outcome.get("spread_status") != SPREAD_STATUS_NOT_EVALUATED
    )
    # Only rows the SPREAD GATE itself stopped count here. A manual run does not
    # fail closed on an unproven spread, so a row can carry
    # spread_status='spread_unavailable' and still be stopped later by human
    # review or broker confirmation; counting that as a spread block would record
    # error_class='missing_spread_data' for a non-spread cause (Codex review).
    blocked_missing_spread = sum(
        1 for outcome in rows if outcome.get("blocked_by") == BLOCKED_BY_SPREAD_GATE
    )
    # Degraded inputs, from two sources that both leave the run looking empty
    # without being a clean ladder no-op (Codex review):
    #   * a market-data provider that RAISED, which evaluate_signal() catches and
    #     degrades to a NO_DATA signal carrying a "provider error" warning, so the
    #     ticker was never genuinely evaluated; and
    #   * a pre-placement broker check that raised (same-day order lookup,
    #     tradability, review), which the placement loop records as a
    #     non-deliberate block. Deliberate safety skips carry deliberate_skip=True
    #     and are NOT errors.
    data_errors = sum(
        1
        for row in artifacts.get("signal_rows") or []
        if "provider error" in str(row.get("warnings") or "")
    ) + sum(
        1
        for row in rows
        if row.get("status") == "blocked" and not row.get("deliberate_skip")
    )
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=reaching_gate,
        candidates_blocked_missing_spread=blocked_missing_spread,
        # Candidates stopped by a cap/ceiling/duplicate check BEFORE the spread
        # lookup did qualify on the ladder, so they must not be reported as
        # 'no_ladder_candidates' (Codex review).
        candidates_evaluated=len(rows),
        # Every placement raised: a genuine failure, not a healthy no-order run.
        placement_attempted_and_failed=any(row.get("status") == "failed" for row in rows),
        data_errors=data_errors,
        # A manual run never runs the scheduled connector-native recovery policy,
        # so its broker failures must not be recorded under that outcome.
        scheduled=scheduled,
    )
    fields = outcome.as_run_state_fields()
    # A concrete terminal status the placement path produced is more specific
    # than the generic no-order result and must survive: replacing
    # 'not_submitted_confirmation_required' would make is_confirmation_blocked()
    # return false and lose the fact that broker confirmation is pending, and a
    # dry run deliberately places nothing (Codex review).
    concrete = _concrete_order_submit_status(rows)
    if concrete is not None:
        fields["order_submit_status"] = concrete
    artifacts.update(fields)


def _concrete_order_submit_status(rows: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The specific terminal status a zero-order run must keep, if any.

    Mirrors near_close_dca._derive_order_submit_status's precedence for the cases
    that are more specific than "no orders were submitted".
    """
    # Dry-run mode is authoritative and outranks everything, matching
    # near_close_dca._derive_order_submit_status, which checks config.dry_run
    # first. A single dry_run row proves the run used DryRunBroker -- there is one
    # broker per run -- so requiring EVERY row to be dry_run meant one cap-blocked
    # or skipped ticker beside a simulated order persisted the run as a genuine
    # live no-order evaluation (Codex review).
    if any(row.get("status") == "dry_run" for row in rows):
        return ORDER_SUBMIT_STATUS_DRY_RUN
    # A genuine placement failure outranks a confirmation block: reporting must
    # not treat a batch where another order RAISED as an ordinary confirmation
    # hold (Codex review).
    if any(row.get("status") == "failed" for row in rows):
        return None
    if any(row.get("status") == "not_submitted_confirmation_required" for row in rows):
        return ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED
    return None


def _run_drift_check(run_dir: Path) -> None:
    """Invoke drift detection after a live run writes its artifacts.

    Writes ``drift_report.json`` and ``drift_report.md`` alongside the run's
    artifacts, logs a warning when drift is detected, and sends a Slack alert
    when ``DCA_DRIFT_SLACK_WEBHOOK`` is set and drift is detected.
    """
    _log = logging.getLogger("morning_dca.drift")
    try:
        from scripts.drift_detection import (  # lazy — stdlib only dep
            DEFAULT_SLACK_WEBHOOK_ENV,
            detect_drift,
            send_slack_alert,
        )
    except Exception as exc:  # pragma: no cover
        _log.warning("drift detection unavailable: %s", exc)
        return
    try:
        report = detect_drift(run_dir)
        atomic_write_text(run_dir / "drift_report.json", report.to_json(compact=False))
        atomic_write_text(run_dir / "drift_report.md", report.to_markdown())
        if report.drift_detected:
            _log.warning(
                "DRIFT DETECTED in %s: %d symbol(s) expected but no order record — %s",
                run_dir,
                len(report.affected_symbols),
                ", ".join(s.symbol for s in report.affected_symbols),
            )
            try:
                send_slack_alert(report, env_var=DEFAULT_SLACK_WEBHOOK_ENV)
            except Exception as slack_exc:  # pragma: no cover
                _log.warning("drift Slack alert failed: %s", slack_exc)
        else:
            _log.info("drift check OK for %s", run_dir)
    except Exception as exc:  # pragma: no cover
        _log.warning("drift check failed for %s: %s", run_dir, exc)


def _same_stream_order_exists(
    broker: BrokerAdapter, ticker: str, trading_day: date, stream: str
) -> Optional[Dict[str, Any]]:
    """Return the broker order dict when this stream's idempotency ref id already exists, else None.

    Guarded midday top-ups intentionally allow earlier cross-stream orders for the
    same ticker, so the broad same-day duplicate check would be too strict. This
    narrower hook lets live brokers fail closed on retries of the current stream
    without treating the morning/manual prior allocation as a duplicate.

    Returning the matched order dict (rather than a plain bool) lets callers
    preserve the real broker order_id and amount so retried run_state writes do
    not fabricate phantom values.

    Both the plain and topup scheduled prefixes are checked here to cover:
    - A retry where the first attempt placed {stream}-dca-{date}-{ticker} (no prior
      allocation at the time) and a manual fill appeared before the retry elevating
      has_prior_allocation to True; we must detect the already-live plain order.
    - A retry where the first attempt already placed the topup variant.
    Manual fills use {stream}-dca-manual-{date}-{ticker} (see ref_id construction
    in _place_broker_orders) and therefore never collide with either prefix here.
    """
    checker = getattr(broker, "same_day_strategy_order_exists_with_ref_id_prefix", None)
    if checker is None:
        raise RuntimeError(
            "broker does not support same-stream ref-id duplicate checks for guarded top-ups"
        )
    prefixes = (
        f"{stream}-dca-topup-{trading_day.isoformat()}-{ticker}",
        f"{stream}-dca-{trading_day.isoformat()}-{ticker}",
    )
    result = checker(ticker, trading_day, prefixes)
    if not isinstance(result, dict):
        # Normalize legacy boolean adapters: False/None → None (no duplicate).
        if not result:
            return None
        # True → treat as found but without real order details. Synthesize a
        # deterministic "id" from the same ticker/day/stream inputs used to
        # build `prefixes` above rather than returning a bare `{}`: the
        # caller's duplicate-recovery settle() call passes this id through as
        # settle()'s order_id, and settle() skips a submitted_amount_usd it
        # has already applied for a given order_id. Without an id here,
        # recovered_order_id is None, settle()'s dedup never engages, and a
        # retry that finds the same legacy-boolean duplicate again subtracts
        # the same dollars from the shared pending balance a second time
        # (Codex review, PR #656 discussion_r3837494237: "Require an ID for
        # idempotent duplicate settlement"). This id is stable across
        # retries for the SAME ticker/day/stream regardless of which of the
        # two prefixes actually matched -- the legacy adapter contract
        # cannot report that, and any single deterministic value is
        # sufficient for settle()'s own per-order_id dedup set.
        return {"id": f"legacy-bool-dup:{stream}:{trading_day.isoformat()}:{ticker}"}
    return result


def _run_state_after_boundary(state: Dict[str, Any], boundary: datetime) -> bool:
    """True when ``state["generated_at"]`` is strictly after ``boundary``.

    Used by ``_seed_ledger_from_prior_runs`` to keep a replayed/backfilled run
    (``boundary`` = that replay's own ``now``) from seeding cap spend out of
    an artifact -- manual rerun OR the scheduled slot's own fixed-path file --
    that was actually written later in the day than the decision being
    reproduced. Strict ``>`` (not ``>=``) so a same-instant retry, where a
    fresh executor instance calls run() with the identical ``now`` an earlier
    attempt used for its own generated_at, still sees that attempt's spend --
    see test_morning_live_ledger_seeds_cap_spend_from_earlier_same_process_day_run.
    A missing/unparseable generated_at is conservatively treated as NOT after
    the boundary (more prior context only ever reduces a later buy).
    """
    gen_at_str = state.get("generated_at")
    if not gen_at_str:
        return False
    try:
        gen_at = datetime.fromisoformat(gen_at_str)
    except (ValueError, TypeError):
        return False
    if gen_at.tzinfo is None:
        # Match _prior_run_before_boundary in near_close_dca.py: a naive
        # generated_at is a local Eastern timestamp, not UTC.
        gen_at = gen_at.replace(tzinfo=EASTERN)
    return gen_at.astimezone(EASTERN) > boundary.astimezone(EASTERN)


def _write_csv(
    path: Path,
    rows: Sequence[Dict[str, Any]],
    *,
    empty_fieldnames: Optional[Sequence[str]] = None,
) -> None:
    """Write ``rows`` as CSV, ALWAYS emitting a header row.

    A no-row run previously wrote a zero-byte file. That is not a valid
    "evaluated nothing" artifact: a reader cannot tell it from a truncated write,
    the drift detector sees no columns, and the scheduled artifact contract
    requires the header even at zero rows. ``empty_fieldnames`` supplies the
    columns for that case (defaulting to the signal-row schema).
    """
    if not rows:
        fieldnames = list(empty_fieldnames or SIGNAL_CSV_FIELDS)
        with atomic_open_w(path, newline="") as handle:
            csv.DictWriter(handle, fieldnames=fieldnames, restval="").writeheader()
        return
    seen: Dict[str, None] = {}
    for row in rows:
        seen.update(dict.fromkeys(row))
    fieldnames = list(seen)
    with atomic_open_w(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)
