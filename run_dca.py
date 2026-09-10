#!/usr/bin/env python3
"""Orchestrator and CLI for the near-close graded DCA strategy.

This is the single runnable entry point that ties the pieces together:

    config (dca_config) -> providers/broker -> run_daily_compact (near_close_dca)
        -> post-trade reports (dca_reporting) -> compact Notion sync (dca_notion_sync)

Two ways to run:

* **Live/scheduled** — the scheduler (which holds MCP/connector runtime access) builds
  the providers and broker from injected Robinhood MCP callables and calls
  :func:`run_pipeline` directly. Real placement stays gated behind ``config.dry_run`` and
  ``RobinhoodBroker.enable_live_orders``; the CLI never flips either.

* **Local/CI smoke test** — ``python run_dca.py --demo`` runs the whole pipeline against a
  deterministic in-memory data provider in dry-run mode, writing real artifacts and
  reports to a temp/output dir. Nothing contacts a broker or the network.

The CLI is intentionally dry-run only: placing real orders requires live MCP callables
that a bare Python process does not have, so live trading is driven from the scheduler
via :func:`run_pipeline`, not from this command line.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import math
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import dca_paths
from dca_alerts import (
    GuardedRunConfig,
    artifact_freshness_probe,
    attempt_log_root_sibling_to,
    guarded_run,
)
from dca_config import DEFAULT_CONFIG_DIR, load_strategy_config
from dca_fileio import atomic_write_text
from midday_dca import (
    DEFAULT_MIDDAY_LIVE_STATE_ROOT,
    MIDDAY_LIVE_CRON_ID,
    load_midday_config,
)
from morning_dca import (
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    MORNING_LIVE_CRON_ID,
    MORNING_LIVE_MODE,
    MORNING_STREAM,
    load_morning_config,
    morning_preflight_eligible_tickers,
)
from near_close_dca import (
    _TS_FLOOR,
    _UNRECONSTRUCTABLE_SUBMISSION_AMOUNT_USD,
    EASTERN,
    Bar,
    BrokerAdapter,
    CompactRunOptions,
    MarketDataProvider,
    PriorSameDayAllocation,
    SourceName,
    StrategyConfig,
    StrategyState,
    _earlier_selected_base,
    _existing_csv_artifacts,
    _redacted_diagnostic_text,
    _settle_usage_bytes,
    _write_and_validate_scheduled_contract,
    build_broker,
    load_prior_same_day_allocations,
    load_prior_same_day_allocations_multi,
    merge_manual_top_up_context,
    run_daily_compact,
)
from scripts.correlation import build_correlation_id
from scripts.model_usage_log import log_model_usage
from scripts.run_classification import (
    CANONICAL_RETIME_DATE_YYYYMMDD,
    ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT,
    ERROR_CLASS_RECOVERY_FAILURE,
    LEGACY_SCHEDULED_WINDOW_START_ET,
    ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
    ORDER_SUBMIT_STATUS_SUBMITTED,
    RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION,
    SCHEDULED_WINDOW_START_ET,
    build_raw_persistence_state,
    build_recovery_state,
    load_same_day_top_up_context,
    raw_persistence_failure_run_state,
    save_raw_input,
    update_latest_scheduled_run_pointer,
)
from scripts.run_classification import (
    CRON_ID as AFTERNOON_LIVE_CRON_ID,
)
from scripts.scheduled_artifacts import (
    ORDER_ATTEMPT_STATUSES,
    RUN_STATE_FILENAME,
    write_signals_csv,
)

logger = logging.getLogger("near_close_dca.run")

# Root for ad hoc intraday scheduled-recovery streams (e.g. the Jul 24 2026
# 2:30 PM ET intraday_1430 recovery -- see config/scheduled_tasks.json
# canonical_first_buy_and_topup_rule -> intraday_dca_scheduled_recovery_2026_07_24),
# which have no dedicated *_dca.py module/config of their own but follow the
# same ``cron_tracking/<stream>_dca/`` layout as the morning/midday roots.
# Declared as its own root (rather than folded into
# _CROSS_STREAM_PRIOR_ALLOCATION_ROOTS below, which also drives each stream's
# OWN configured-root lookup in _resolve_cross_stream_prior_allocation_roots --
# there is no IntradayConfig to look up) and paired with
# near_close_dca._prior_run_is_scheduled recognizing any ``stream`` starting
# with ``"intraday"`` as eligible prior context (Codex review: "Include the
# intraday recovery in later top-up context" -- without this, that recovery's
# $72 in fills would never be loaded by a later same-day run's top-up guard,
# which could then re-submit those tickers' full first allocations).
#
# Overridable via the DCA_INTRADAY_LIVE_STATE_ROOT environment variable (see
# _resolve_cross_stream_prior_allocation_roots): since there is no
# IntradayConfig loader to source a deployment-specific root from the way
# morning/midday do, a deployment that relocates its afternoon and intraday
# artifact trees independently would otherwise have no way to point this root
# anywhere but the bundled sibling-of-afternoon-root convention (Codex review:
# "Make the intraday recovery root configurable").
#
# Unlike DEFAULT_MORNING_LIVE_STATE_ROOT/DEFAULT_MIDDAY_LIVE_STATE_ROOT (each
# a real, independently-configurable production path), this constant is only
# ever consumed for its basename (see sibling_roots below and the ``!=``
# check that drops it once an env override replaces it) -- there is no
# stand-alone intraday root to point at outside the sibling-of-afternoon-root
# convention. Keep it a bare relative name rather than a host-specific
# absolute path so the module stays portable across deployments (Codex
# review: "Derive the intraday root without a local absolute path").
DEFAULT_INTRADAY_LIVE_STATE_ROOT = "intraday_dca"

# Root for the recurring 9:35 AM ET market-open live task (2ba19526), which
# like the intraday task above has no dedicated *_dca.py module/config of its
# own but follows the same ``cron_tracking/<stream>_dca/`` layout. Declared
# for the same reason as DEFAULT_INTRADAY_LIVE_STATE_ROOT and paired with
# near_close_dca._prior_run_is_scheduled recognizing ``stream == "market_open"``
# as eligible prior context (Codex review: "Feed market-open fills into later
# top-up guards" -- without this, a 9:35 fill would never be loaded by a
# later same-day run's top-up guard, which could then re-submit that
# ticker's full first allocation). Overridable via the
# DCA_MARKET_OPEN_LIVE_STATE_ROOT environment variable, mirroring
# DCA_INTRADAY_LIVE_STATE_ROOT, for the same reason: there is no
# MarketOpenConfig loader to source a deployment-specific root from. Kept a
# bare relative name for the same portability reason as the intraday root.
DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT = "market_open_dca"

# Out-of-tree manual final-buy override root: scripts/run_classification.py
# explicitly treats final_manual_review/** as same-day allocation context, and
# real budget-block-override fills live here (config/scheduled_tasks.json's
# jul17_manual_budget_block_overrides). morning_dca.py's own
# default_cap_seed_roots() already includes this sibling root (Codex review:
# "Include out-of-tree manual fills in pooled roots"); without it here too, a
# fresh afternoon run's cross-stream scan -- both same-day and the
# earlier-week scan in _compute_earlier_week_cross_stream_notional -- misses a
# manual override that filled budget-blocked signals after a scheduled run,
# letting the shared weekly cap be exceeded (Codex review: "Include
# final-manual overrides in afternoon weekly seeding"). Like intraday_dca/
# market_open_dca above, there is no per-stream config loader for this root,
# so it has no override mechanism of its own -- it is always the fixed
# sibling-of-afternoon-root directory.
DEFAULT_FINAL_MANUAL_REVIEW_ROOT = "final_manual_review"

# Stream label for the scheduled 3:30 PM ET final live run, used for the
# failure-alert / heartbeat identity (DCA_HEARTBEAT_URL_AFTERNOON). Matches the
# "afternoon" stream name recorded by scripts/run_classification.py pointers.
AFTERNOON_STREAM = "afternoon"

SLOT_MAP: Dict[str, tuple[str, str]] = {
    "1030ET": ("morning", "1030ET"),
    "1230ET": ("midday", "1230ET"),
    "1530ET": ("afternoon", "1530ET"),
}
DISABLED_SLOTS: tuple[str, ...] = ("0935ET", "1130ET", "1330ET", "1430ET")

# The live task id for each canonical slot's stream, so log_model_usage's
# telemetry attributes a 1030ET/1230ET run to its own task instead of always
# recording the afternoon task id (Antigravity review: "run_slot() does not
# pass cron_id to run_pipeline()", discussion on run_dca.py's run_slot).
_SLOT_CRON_ID: Dict[str, str] = {
    "morning": MORNING_LIVE_CRON_ID,
    "midday": MIDDAY_LIVE_CRON_ID,
    "afternoon": AFTERNOON_LIVE_CRON_ID,
}


class UnsupportedSlotError(ValueError):
    """Raised when a requested slot is unsupported or intentionally disabled."""


class OffScheduleSlotError(ValueError):
    """Raised when a slot fires outside its scheduled +/-5-minute window."""


def _slot_time_et(window: str) -> time:
    """'1030ET' -> time(10, 30). ``window`` is always a SLOT_MAP value."""
    digits = window[:-2] if window.endswith("ET") else window
    return time(int(digits[:2]), int(digits[2:]))


# Matches the +/-5-minute scheduled_window_start_et/scheduled_window_end_et
# _slot_classification_overrides already stamps onto the run artifact.
_SLOT_SCHEDULE_TOLERANCE = timedelta(minutes=5)

# 1530ET's own approved trigger_window (config/scheduled_tasks.json's "10ce362c"
# entry) narrows the START bound to 15:30-15:32 ET specifically, because the
# 15:35 post-trade sync task (67aa802b) neither waits nor retries for an
# in-progress run: a late-but-permitted start under the shared +/-5-minute
# tolerance (as late as 15:35) could still race that sync and make it report a
# missing or partial final-run (Codex review: "Enforce the final slot's 15:32
# start cutoff"). Every other slot keeps the shared symmetric tolerance.
_SLOT_SCHEDULE_BOUNDS: Dict[str, tuple[timedelta, timedelta]] = {
    "1530ET": (timedelta(0), timedelta(minutes=2)),
}


def _validate_slot_schedule(now: datetime, window: str) -> None:
    """Abort a roster slot invocation whose wall-clock time misses its window.

    ``run_pipeline``'s generic execution gates only ever receive the trading
    DATE, so without this, ``run_slot("1030ET", now=<14:00 ET>)`` would merely
    anchor signal history to 10:30 (via ``decision_time_et``) and still place
    a live market order hours off-schedule. Every live executor contract
    (``claude/dca-executor-prompts/live-mirror/*``) treats an off-schedule
    firing as a scheduler fault that must abort, not a normal business
    condition to size around (Codex review: "Reject slot invocations outside
    their scheduled window").
    """
    slot_time = _slot_time_et(window)
    anchor = datetime(2000, 1, 1, slot_time.hour, slot_time.minute)
    now_et = now.astimezone(EASTERN)
    now_anchor = now_et.replace(year=2000, month=1, day=1, tzinfo=None)
    delta = now_anchor - anchor
    before_tolerance, after_tolerance = _SLOT_SCHEDULE_BOUNDS.get(
        window, (_SLOT_SCHEDULE_TOLERANCE, _SLOT_SCHEDULE_TOLERANCE)
    )
    if delta < -before_tolerance or delta > after_tolerance:
        raise OffScheduleSlotError(
            f"slot {window} fired at {now_et.time().isoformat()} ET, outside its "
            f"scheduled -{before_tolerance}/+{after_tolerance} window around "
            f"{slot_time.isoformat()} ET; aborting as a scheduler fault rather "
            "than placing an off-schedule order"
        )


def _slot_classification_overrides(
    stream_name: str, window: str, trading_day: str
) -> Optional[Dict[str, Any]]:
    """Per-slot ``run_classification`` identity for a non-afternoon roster slot.

    ``near_close_dca.build_run_artifacts`` stamps every scheduled run through
    ``scripts.run_classification.build_scheduled_classification``, which
    defaults to the afternoon (15:30 ET, cron_id ``10ce362c``) identity. Left
    unoverridden, a 1030ET/1230ET slot routed through that same afternoon
    pipeline stamped itself ``stream="afternoon"``, ``scheduled_for_et="15:30"``,
    and the afternoon cron_id -- misattributing telemetry, hiding the slot's own
    fills from a later slot's prior-allocation scan (which keys off
    ``run_classification.stream``, see ``_prior_run_is_scheduled``), and risking
    an early update of the afternoon ``latest_scheduled_run.json`` pointer
    (Codex review, discussion_r3679391261: "Stamp each slot with its own
    scheduled classification"). Returns ``None`` for the afternoon slot itself,
    leaving the existing default (and every other, non-roster caller of
    ``build_run_artifacts``) byte-for-byte unchanged.

    Also stamps ``valid_afternoon_scheduled_evidence=False``: a morning/midday
    classification must never be mistaken for afternoon evidence by a
    flag-based slippage/reliability consumer that reads this field without
    also cross-checking ``stream`` (Codex review: "Deny afternoon evidence in
    non-afternoon schema branches").
    """
    if stream_name == AFTERNOON_STREAM:
        return None
    slot_time = _slot_time_et(window)
    anchor = datetime(2000, 1, 1, slot_time.hour, slot_time.minute)
    window_start = (anchor - timedelta(minutes=5)).strftime("%H:%M")
    window_end = (anchor + timedelta(minutes=5)).strftime("%H:%M")
    compact_day = trading_day.replace("-", "")
    return {
        "stream": stream_name,
        "cron_id": _SLOT_CRON_ID[stream_name],
        "supersedes_cron_id": None,
        "scheduled_for_et": slot_time.strftime("%H:%M"),
        "scheduled_window_start_et": window_start,
        "scheduled_window_end_et": window_end,
        "run_id": f"scheduled_{compact_day}_{window}",
        "valid_afternoon_scheduled_evidence": False,
    }


# Sibling stream roots the scheduled afternoon top-up guard folds in as prior
# same-day allocation context (in addition to its own task root): the market-open,
# morning, midday, intraday, and final-manual-review-override live roots -- the
# midday one also holds manual override reruns under ``manual_reruns/``. Absent
# roots contribute nothing, so this is safe on days a stream did not run. These
# are prior-allocation context ONLY -- never scheduled afternoon evidence,
# slippage, reliability, fill counts, or cost basis.
#
# Used both as the sibling-of-afternoon-root fallback and as the default when a
# stream's own config can't be loaded (see
# _resolve_cross_stream_prior_allocation_roots).
_CROSS_STREAM_PRIOR_ALLOCATION_ROOTS: Tuple[str, ...] = (
    DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT,
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    DEFAULT_MIDDAY_LIVE_STATE_ROOT,
    DEFAULT_INTRADAY_LIVE_STATE_ROOT,
    DEFAULT_FINAL_MANUAL_REVIEW_ROOT,
)


def _is_within(path: Path, ancestor: Path) -> bool:
    """True if resolved ``path`` is ``ancestor`` itself or nested under it."""
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _resolve_cross_stream_prior_allocation_roots(
    config_dir: Optional[str | Path],
    afternoon_root: Path,
    *,
    for_fallback_discovery: bool = False,
) -> Tuple[Path, ...]:
    """Resolve the morning/midday live roots the afternoon top-up guard scans.

    Reads each stream's OWN configured artifact root (``MorningConfig``/
    ``MiddayConfig.morning_run_state_root``, sourced from
    ``artifact_paths.live_run_state_root`` in ``config/morning_dca.json`` /
    ``config/midday_dca.json``) rather than ONLY assuming the
    sibling-of-afternoon-root directory-naming convention, so a deployment that
    relocates a stream's artifact root (staging, CI, or a future config change)
    is scanned correctly instead of silently missing that stream's fills
    (Codex/Copilot review: "Honor the configured morning artifact root").

    Also still includes the sibling-of-``afternoon_root`` roots derived from
    ``afternoon_root.parent`` -- a caller that relocates the WHOLE
    cron-tracking tree via a non-production ``artifacts_dir`` (tests, demo,
    staging) without also pointing ``config_dir`` at a matching relocated
    config would otherwise have its real sibling morning/midday artifacts
    missed, since the configured root would still resolve to the bundled
    production default (Codex follow-up review: "Preserve the sibling
    stream-root fallback"). Both sets of roots are folded in and deduped by
    resolved path, so the common case where they coincide doesn't scan the
    same tree twice.

    Two further guards on top of that:

    - When no ``config_dir`` is given, a stream's configured root is only
      trusted if it actually lives under this run's cron base (i.e. matches
      the sibling-of-``afternoon_root`` convention). Without an explicit
      ``config_dir`` the loader falls back to the bundled production
      config, so its ``live_run_state_root`` would otherwise resolve to a
      real production path (e.g. ``/home/user/workspace/cron_tracking/...``)
      even when this run's whole tree has been relocated for tests, demos,
      or staging -- silently folding another environment's real same-day
      fills in as this run's prior-allocation context (Codex follow-up
      review: "Keep relocated runs from scanning production roots"). An
      explicit ``config_dir`` is trusted only once the corresponding stream
      config actually supplied a non-empty root -- the same requirement
      already applied to the ``DCA_CONFIG_DIR`` path below -- since a
      relocated/staging ``config_dir`` that lacks ``morning_dca.json`` or
      ``midday_dca.json`` (or whose stream config fails to load) still
      falls back to the bundled production default, which must not be
      admitted just because *some* config_dir was given (Codex review:
      "Keep fallback roots contained for incomplete configs").
    - Any resolved root that is a descendant of another resolved root is
      pruned, keeping only the ancestor. ``load_prior_same_day_allocations``/
      ``..._multi`` scan every given root recursively (``rglob``), so keeping
      both a root and its child would load the same ``run_state.json`` files
      twice and double the afternoon top-up guard's prior-allocation amount
      (Codex review: "Avoid double-counting nested stream roots").
    - Any resolved root that is ``afternoon_root`` itself, an ancestor of it,
      OR a descendant of it is also dropped. The caller scans
      ``afternoon_root`` separately and sums that result with this
      function's cross-stream roots (see ``run_pipeline``), so keeping a
      root that contains -- or equals -- ``afternoon_root`` (e.g. a shared
      ``cron_tracking`` root configured as a stream's
      ``morning_run_state_root``) would recursively re-scan the same
      ``afternoon_root`` tree a second time and double-count every manual
      afternoon allocation beneath it (Codex review: "Exclude the afternoon
      tree from cross-stream roots"). The descendant direction matters too:
      a root NESTED under ``afternoon_root`` (e.g. an operator setting
      ``DCA_INTRADAY_LIVE_STATE_ROOT`` to a subdirectory of the afternoon
      tree by mistake) would already be covered by the separate
      ``afternoon_root`` scan, so keeping it as an ALSO-scanned cross-stream
      root would double-count every artifact beneath it just the same
      (Codex review: "Exclude intraday roots nested under the afternoon
      tree").
    - An explicit ``DCA_INTRADAY_LIVE_STATE_ROOT`` override REPLACES the
      conventional ``cron_base/intraday_dca`` sibling fallback rather than
      adding to it, decided UPFRONT (before the fallback is even added to
      the candidate list) whenever the override doesn't conflict with
      ``afternoon_root`` the same way any other cross-stream root would
      (Codex review, several rounds: "Keep the intraday fallback when
      filtering the override", "Drop the intraday fallback when an
      ancestor covers the override", "Preserve an override that equals the
      intraday fallback", "Preserve a nested intraday override when
      removing its fallback", "Scan only the nested intraday override").
      Deciding this after the fact -- by checking whether the override
      "survived" the general containment/pruning passes below -- kept
      running into the same family of errors: those passes are built to keep
      the BROADEST surviving root and prune redundant descendants (correct
      for the general nested-config case), but for intraday specifically
      that direction is backwards whenever the override is narrower than
      the fallback (e.g. a subdirectory of it) -- the operator's whole
      point in setting the override is often to scan ONLY that narrower,
      relocated tree, not the possibly-stale broader one. Deciding upfront
      means the fallback is simply never a candidate at all once an active
      override exists, so the general pruning passes never get a chance to
      keep the wrong one of the two.
    """
    cron_base = afternoon_root.parent

    # DCA_CONFIG_DIR, when set and pointing to an existing directory, has the
    # same trust level as an explicit config_dir: deployment deliberately
    # selected that environment's config, so a root that stream's own config
    # actually set must be honoured even when the caller did not forward
    # config_dir to run_pipeline(). But trust is evaluated PER STREAM: if
    # DCA_CONFIG_DIR exists yet a stream's own JSON (morning_dca.json /
    # midday_dca.json) doesn't override live_run_state_root, that stream's
    # loader silently falls back to the bundled production default root --
    # exactly the untrusted case this containment check exists to catch, and
    # merely checking that the DCA_CONFIG_DIR directory exists would still
    # admit it. Only bypass containment for a stream once loading actually
    # produced an explicit (non-None) root for it.
    _env_config_dir = os.environ.get("DCA_CONFIG_DIR")
    _env_config_dir_valid = bool(_env_config_dir and Path(_env_config_dir).is_dir())

    def _configured_root(
        loader_root: Optional[str], default_root: str
    ) -> Optional[Path]:
        root = Path(loader_root or default_root)
        # An empty string is treated as "not configured" everywhere else this
        # field is consumed (e.g. MorningDCAExecutor.state_root's own
        # `self.config.morning_run_state_root or self.profile["state_root"]`
        # fallback), so it must not count as this stream having an explicit
        # root either -- `loader_root is not None` alone would still trust an
        # empty override, and `root` above already silently fell back to
        # default_root (the production path) for it.
        trusted = bool(loader_root) and (
            config_dir is not None or _env_config_dir_valid
        )
        if not trusted and not _is_within(root.resolve(), cron_base.resolve()):
            # Neither an explicit config_dir argument nor a DCA_CONFIG_DIR
            # override for this specific stream was in play, and the resolved
            # root doesn't belong to this run's cron-tracking tree -- almost
            # certainly the bundled production default surviving a relocated
            # (test/demo/staging) tree. Drop it; the sibling-root fallback
            # below still covers this stream if it actually ran under the
            # relocated tree.
            return None
        return root

    try:
        morning_root = load_morning_config(
            config_dir, mode=MORNING_LIVE_MODE
        ).morning_run_state_root
    except (
        Exception
    ):  # noqa: BLE001 - never let a bad morning config block the afternoon run
        morning_root = None
    try:
        midday_root = load_midday_config(config_dir).morning_run_state_root
    except (
        Exception
    ):  # noqa: BLE001 - never let a bad midday config block the afternoon run
        midday_root = None

    # There is no IntradayConfig loader (see DEFAULT_INTRADAY_LIVE_STATE_ROOT
    # above), so the intraday root has no per-stream JSON config to read the
    # way morning/midday do. An explicit DCA_INTRADAY_LIVE_STATE_ROOT is the
    # deployment's own configuration for this root and is trusted directly,
    # matching how an explicit config_dir/DCA_CONFIG_DIR override is trusted
    # for morning/midday above -- a relocated intraday tree living outside
    # cron_base is exactly the case this override exists for.
    _env_intraday_root = os.environ.get("DCA_INTRADAY_LIVE_STATE_ROOT")
    intraday_root = Path(_env_intraday_root) if _env_intraday_root else None

    # Same reasoning as the intraday root above, but for the 9:35 AM
    # market-open live task (2ba19526): there is no MarketOpenConfig loader
    # either, so DCA_MARKET_OPEN_LIVE_STATE_ROOT is the deployment's own
    # configuration for this root (Codex review: "Feed market-open fills
    # into later top-up guards").
    _env_market_open_root = os.environ.get("DCA_MARKET_OPEN_LIVE_STATE_ROOT")
    market_open_root = Path(_env_market_open_root) if _env_market_open_root else None

    # Resolve UPFRONT whether the override is CONFIGURED at all, and
    # separately whether it needs its own scan entry (ACTIVE) -- these are
    # NOT the same question, and conflating them was itself an issue (Codex
    # review: "Drop the legacy fallback for covered nested overrides"). An
    # override that is a STRICT ANCESTOR of afternoon_root is a genuine
    # misconfiguration -- practically the whole cron tree, not a meaningful
    # "intraday root" -- so it is invalid and treated exactly as if unset.
    # But an override NESTED under (or EQUAL TO) afternoon_root is a real,
    # deliberately-configured location; it just doesn't need a separate
    # scan entry because the caller's own recursive afternoon scan already
    # covers it -- including the equal case, which is trivially covered by
    # that same scan (Codex review: "Treat an override equal to the
    # afternoon root as covered"; an ancestor-or-equal check previously
    # treated the equal case as the ancestor misconfiguration too).
    # Suppressing the legacy cron_base/intraday_dca fallback must track the
    # former (was a valid override configured), not the latter (does it
    # need its own scan entry) -- otherwise a nested (or equal) override
    # incorrectly resurrects the legacy fallback, and if that stale tree
    # still holds artifacts from before the relocation, they get aggregated
    # alongside the (already-covered) real data.
    _intraday_is_ancestor_of_afternoon = (
        intraday_root is not None
        and _is_within(afternoon_root.resolve(), intraday_root.resolve())
        and intraday_root.resolve() != afternoon_root.resolve()
    )
    _intraday_is_nested_under_afternoon = intraday_root is not None and _is_within(
        intraday_root.resolve(), afternoon_root.resolve()
    )
    intraday_override_configured = (
        intraday_root is not None and not _intraday_is_ancestor_of_afternoon
    )
    intraday_override_active = (
        intraday_override_configured and not _intraday_is_nested_under_afternoon
    )

    # Same ancestor/nested/equal reasoning as intraday above, applied to the
    # market-open override.
    _market_open_is_ancestor_of_afternoon = (
        market_open_root is not None
        and _is_within(afternoon_root.resolve(), market_open_root.resolve())
        and market_open_root.resolve() != afternoon_root.resolve()
    )
    _market_open_is_nested_under_afternoon = (
        market_open_root is not None
        and _is_within(market_open_root.resolve(), afternoon_root.resolve())
    )
    market_open_override_configured = (
        market_open_root is not None and not _market_open_is_ancestor_of_afternoon
    )
    market_open_override_active = (
        market_open_override_configured and not _market_open_is_nested_under_afternoon
    )

    # For FALLBACK discovery, include an override that is merely "configured"
    # rather than "active". The active gate drops an override nested under
    # afternoon_root because the core recursive scan of that tree already covers
    # its state files -- true for the core scan, false for the bare-CSV fallback,
    # which never scans afternoon_root and looks only inside `*_recovery_*`
    # directories (Codex review: "Preserve nested configured roots for fallback
    # discovery"). An override that is an ANCESTOR of afternoon_root stays
    # excluded either way: *_override_configured is already false for it.
    _intraday_included = (
        intraday_override_configured
        if for_fallback_discovery
        else intraday_override_active
    )
    _market_open_included = (
        market_open_override_configured
        if for_fallback_discovery
        else market_open_override_active
    )
    configured_roots = tuple(
        r
        for r in (
            _configured_root(morning_root, DEFAULT_MORNING_LIVE_STATE_ROOT),
            _configured_root(midday_root, DEFAULT_MIDDAY_LIVE_STATE_ROOT),
            intraday_root if _intraday_included else None,
            market_open_root if _market_open_included else None,
        )
        if r is not None
    )
    # The conventional intraday/market-open sibling fallbacks are included
    # only when there is no CONFIGURED override for that specific root --
    # any valid override (whether it needs its own scan entry or is already
    # covered by the afternoon tree, equal to the fallback, or covered by
    # some other ancestor) replaces it outright: the operator explicitly
    # pointed that root somewhere, and letting the fallback remain a
    # candidate would risk double-counting real artifacts, or -- for a
    # narrower/covered override -- scanning a broader, possibly-stale tree
    # than the operator intended.
    _cross_stream_defaults = tuple(
        r
        for r in _CROSS_STREAM_PRIOR_ALLOCATION_ROOTS
        if not (r == DEFAULT_INTRADAY_LIVE_STATE_ROOT and intraday_override_configured)
        and not (
            r == DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT and market_open_override_configured
        )
    )
    sibling_roots = tuple(cron_base / Path(r).name for r in _cross_stream_defaults)

    # Pass 1: dedupe exact-duplicate resolved paths, preserving first-seen order.
    seen: set = set()
    deduped: List[Tuple[Path, Path]] = []
    for root in (*configured_roots, *sibling_roots):
        key = root.resolve()
        if key not in seen:
            seen.add(key)
            deduped.append((root, key))

    # Pass 2: drop any root that has ANY containment relationship with
    # afternoon_root -- it is afternoon_root itself, an ancestor of it, or a
    # DESCENDANT of it. The caller scans afternoon_root separately and sums
    # that result with this function's roots (see run_pipeline), so keeping
    # a root on either side of that relationship would recursively re-scan
    # (and double-count) the overlapping part of that tree. This MUST happen
    # before Pass 3's descendant pruning below, not folded into the same
    # comparison: if a stream is configured to the shared cron_base itself,
    # cron_base contains afternoon_root (dropped here) AND is the ancestor
    # that would otherwise cause the sibling-root fallback (a genuine
    # descendant of cron_base) to be pruned as "redundant" in Pass 3 --
    # leaving nothing scanned for that stream at all. Excluding
    # afternoon-containing roots first means Pass 3 never sees cron_base as
    # an ancestor candidate, so the sibling fallback survives.
    if for_fallback_discovery:
        # Passes 2 and 3 exist to stop the CORE recursive scan double-counting:
        # the caller scans afternoon_root separately and sums the results, and
        # Pass 3 drops descendants of another surviving root for the same
        # reason. Neither applies to the bare-CSV fallback
        # (run_classification.load_same_day_top_up_context), which does NOT
        # recursively scan afternoon_root and only looks inside `*_recovery_*`
        # directories, deduping rows by order_id.
        #
        # With DCA_INTRADAY_LIVE_STATE_ROOT or DCA_MARKET_OPEN_LIVE_STATE_ROOT
        # configured NESTED under afternoon_root, Pass 2 would drop that root
        # here even though the fallback still needs it: a recovery whose fills
        # exist only in an unreferenced sibling CSV is invisible to the core
        # loader by construction and to the conventional recovery globs by
        # location, so the combined loader would return no allocation AND no
        # scan failure -- and the full amount gets bought again (Codex review:
        # "Preserve nested configured roots for fallback discovery").
        return tuple(orig for orig, _ in deduped)

    afternoon_key = afternoon_root.resolve()
    non_afternoon: List[Tuple[Path, Path]] = [
        (orig, key)
        for orig, key in deduped
        if not _is_within(afternoon_key, key) and not _is_within(key, afternoon_key)
    ]

    # Pass 3: prune any root that is a descendant of another surviving root.
    resolved: List[Path] = [
        orig
        for orig, key in non_afternoon
        if not any(
            key != other_key and _is_within(key, other_key)
            for _, other_key in non_afternoon
        )
    ]

    return tuple(resolved)


def _parse_raw_json(path):
    import json
    try:
        return path.name, json.loads(path.read_bytes()), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return path.name, None, e


def _validated_corporate_action_result(result, as_of: "date"):
    """Enforce ``get_corporate_action_factor``'s own contract -- a finite
    positive factor and a cursor that lands exactly on ``as_of`` -- on a
    ``(factor, cursor)`` result before it is persisted or returned, whether
    that result just came from a live/injected provider or was read back
    from a persisted ``corporate_action_SYMBOL.json``.

    A non-conforming result (nonpositive/NaN factor, or a cursor that
    doesn't cover ``as_of``) is not merely untrustworthy metadata -- passed
    through uncaught, ``evaluate_inverse_martingale_step`` can ignore an
    invalid factor while still advancing the durable cursor past it,
    permanently skipping the action window a later corrected response could
    have confirmed (Codex review: "Validate cached action results before
    moving the cursor"; "Validate fresh action results before persisting
    them"). Returns ``None`` for anything that fails this check, including a
    malformed ``result`` shape.
    """
    if result is None:
        return None
    try:
        factor, cursor = result
        factor = float(factor)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(cursor, str):
        try:
            cursor = date.fromisoformat(cursor)
        except ValueError:
            return None
    if not isinstance(cursor, date) or not math.isfinite(factor) or factor <= 0:
        return None
    if cursor != as_of:
        return None
    return factor, cursor


def _build_frozen_providers_from_raw(
    original_finance: "MarketDataProvider",
    original_robinhood: "MarketDataProvider",
    raw_dir: "Path",
    *,
    strict_finance: bool = False,
    strict_robinhood: bool = False,
) -> "Tuple[MarketDataProvider, MarketDataProvider]":
    """Return frozen (finance_data, robinhood_data) backed by the persisted raw files.

    Reads all batch files from ``raw_dir`` and constructs providers whose callables
    serve the exact archived data so ``run_daily_compact()`` computes from the same
    snapshot that was persisted, not from potentially-stale live MCP calls.

    Robinhood files (``historicals_batchN.json``, ``quotes_batchN.json``):
        Frozen ``RobinhoodDataProvider`` filters by ticker+interval so bars from
        different granularities are never mixed.

    Finance files (``finance_bars_batchN.json``, ``finance_close_batchN.json``):
        Frozen ``FinanceDataProvider`` keyed by ``(ticker, interval_minutes)`` for
        bars and by ticker for previous-close.  When no finance files are present,
        the original live provider is returned for that source and a warning is
        logged; the scheduler should persist finance connector responses under these
        names to satisfy the full persist-before-compute contract.

    Falls back to both original providers when ``raw_dir`` is absent or empty.
    """
    hist_results: List[Dict[str, Any]] = []
    quotes_results: List[Dict[str, Any]] = []
    # finance_bars keyed by (ticker, interval_minutes) → list of raw bar dicts
    finance_bars: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    finance_close: Dict[str, Any] = {}  # ticker → raw close value / dict
    # Track file presence separately from parsed content: a persisted batch file
    # with empty results is a valid archived snapshot (upstream returned no data)
    # and must satisfy the strict check. Only raise when NO matching files exist.
    hist_files_seen = False
    quotes_files_seen = False
    finance_files_seen = False

    if raw_dir.is_dir():
        # Serial on purpose: a scheduled run parses a handful to low hundreds of
        # small archived JSON snapshots, where ProcessPoolExecutor startup and
        # inter-process pickling dominate over the tiny per-file parse cost
        # (benchmarked slower than the plain loop below). A process pool also
        # raises AssertionError when this runs inside a daemonic multiprocessing
        # worker, which some scheduling hosts require -- see .jules/bolt.md's
        # "Parallelize JSON loading in frozen providers (reverted)" entry.
        paths = sorted(raw_dir.iterdir())

        for path in paths:
            name, payload, error = _parse_raw_json(path)
            if error is not None:
                continue
            # Unwrap the data envelope so both save paths read the same way:
            # (a) normalize_connector_payload() resolves output_path pointers and
            #     embeds the result under payload["data"] (normalized=true).
            # (b) Inline adapter responses may also use {"data": {"results": [...]}}
            #     as their carrier shape without an output_path; save_raw_input()
            #     preserves that envelope as-is since there is nothing to resolve.
            # In both cases, step into "data" when it is a dict and "results" is
            # not already at the top level of the outer envelope. This can nest
            # more than one layer deep -- e.g. an output_path pointing at a raw
            # response that is itself {"data": {"results": [...]}} normalizes to
            # {"data": {"data": {"results": [...]}}} on disk (the same doubly
            # wrapped pointer shape dca_reporting._order_list_from_payload
            # already handles) -- so keep stepping in, not just once.
            seen_ids: set = set()
            while (
                isinstance(payload, dict)
                and "results" not in payload
                and isinstance(payload.get("data"), dict)
            ):
                if id(payload) in seen_ids:
                    break  # defend against a cyclic/self-referential payload
                seen_ids.add(id(payload))
                payload = payload["data"]
            if not isinstance(payload, dict):
                continue
            if name.startswith("historicals_batch") and name.endswith(".json"):
                hist_files_seen = True
                hist_results.extend(payload.get("results") or [])
            elif name.startswith("quotes_batch") and name.endswith(".json"):
                quotes_files_seen = True
                quotes_results.extend(payload.get("results") or [])
            elif name.startswith("finance_bars_batch") and name.endswith(".json"):
                finance_files_seen = True
                for entry in payload.get("results") or []:
                    tkr = str(entry.get("ticker") or "").strip().upper()
                    ivl = int(entry.get("interval_minutes") or 0)
                    if tkr and ivl:
                        finance_bars.setdefault((tkr, ivl), []).extend(
                            entry.get("bars") or []
                        )
            elif name.startswith("finance_close_batch") and name.endswith(".json"):
                finance_files_seen = True
                for entry in payload.get("results") or []:
                    tkr = str(entry.get("ticker") or "").strip().upper()
                    if tkr:
                        finance_close[tkr] = entry.get("previous_close")

    # --- Robinhood frozen provider -------------------------------------------
    if hist_results or quotes_results or hist_files_seen or quotes_files_seen:
        _h = hist_results
        _q = quotes_results

        def _frozen_historicals(symbols=None, interval=None, **_kw):
            sym_set = set(symbols or [])
            return {
                "results": [
                    r
                    for r in _h
                    if (not sym_set or r.get("symbol") in sym_set)
                    and (interval is None or r.get("interval") == interval)
                ]
            }

        def _frozen_quotes(symbols=None, **_kw):
            sym_set = set(symbols or [])
            return {
                "results": [
                    r
                    for r in _q
                    if not sym_set or (r.get("quote") or {}).get("symbol") in sym_set
                ]
            }

        from dca_live_adapters import RobinhoodDataProvider as _RDP

        frozen_robinhood: "MarketDataProvider" = _RDP(
            historicals_fn=_frozen_historicals,
            quotes_fn=_frozen_quotes if quotes_results else None,
            bounds=getattr(original_robinhood, "_bounds", "regular"),
            adjustment_type=getattr(original_robinhood, "_adjustment_type", "none"),
        )
        # get_bid_ask backs the final pre-placement spread circuit breaker (see
        # near_close_dca.quote_spread_pct), a last-moment check meant to catch
        # price movement between signal computation and order placement.
        # Leaving it on the frozen provider would serve that breaker from this
        # archived quote snapshot instead, defeating its purpose -- delegate to
        # whatever live capability the originally-injected provider has, and
        # expose none at all (fail closed) when it has none either, rather than
        # silently falling back to frozen data.
        _live_get_bid_ask = getattr(original_robinhood, "get_bid_ask", None)
        frozen_robinhood.get_bid_ask = (  # type: ignore[attr-defined]
            _live_get_bid_ask
            if callable(_live_get_bid_ask)
            else (lambda ticker, as_of: None)
        )
    else:
        if strict_robinhood:
            raise RuntimeError(
                "Robinhood raw files (historicals_batchN.json / quotes_batchN.json) "
                f"absent from {raw_dir} — a scheduled live run requires a frozen Robinhood "
                "provider to satisfy the persist-before-compute contract; persist Robinhood "
                "connector responses under those filenames or add them to raw_connector_inputs"
            )
        logger.warning(
            "Robinhood raw files (historicals_batchN.json / quotes_batchN.json) "
            "absent from %s — robinhood_data will use the live provider; "
            "persist Robinhood connector responses to satisfy the full "
            "persist-before-compute contract",
            raw_dir,
        )
        frozen_robinhood = original_robinhood

    # --- Finance frozen provider ----------------------------------------------
    if finance_bars or finance_close or finance_files_seen:
        _fb = finance_bars
        _fc = finance_close

        def _frozen_finance_bars(ticker, _start, _end, interval_minutes):
            return _fb.get((str(ticker).strip().upper(), int(interval_minutes)), [])

        def _frozen_finance_close(ticker, _as_of):
            return _fc.get(str(ticker).strip().upper())

        from dca_live_adapters import FinanceDataProvider as _FDP

        frozen_finance: "MarketDataProvider" = _FDP(
            bars_fn=_frozen_finance_bars,
            previous_close_fn=_frozen_finance_close,
            # Propagate the original provider's opt-out rather than taking
            # _FDP's own True default: an original_finance already wired with
            # supports_corporate_actions=False (because its bars_fn is known
            # to never carry split/dividend fields) would otherwise silently
            # regain the capability on this freshly constructed frozen
            # instance, sending it through the strict live-action branch and
            # the OHLC-only provider below straight back to the exact
            # always-held-latch failure mode supports_corporate_actions was
            # added to prevent (Codex review: "Preserve the corporate-action
            # opt-out when freezing providers").
            supports_corporate_actions=getattr(original_finance, "supports_corporate_actions", True),
        )

        def _persist_archive_corporate_action_factor_unlocked(
            ticker: str, since: "date", as_of: "date", result: object, *, _raw_dir=raw_dir
        ) -> bool:
            """Snapshot archive fallback evidence before the strategy consumes it."""
            validated = _validated_corporate_action_result(result, as_of)
            if validated is None:
                return False
            symbol = ticker.strip().upper()
            payload = {
                "ticker": symbol,
                "since": since.isoformat(),
                "as_of": as_of.isoformat(),
                "factor": validated[0],
                "cursor": validated[1].isoformat(),
                "source": "ticker_history_archive",
            }
            try:
                save_raw_input("", f"corporate_action_{symbol}.json", payload, raw_dir=_raw_dir)
            except (OSError, ValueError):
                return False
            return True

        def _persist_archive_corporate_action_factor(
            ticker: str, since: "date", as_of: "date", result: object, *, _raw_dir=raw_dir
        ) -> bool:
            """Persist archive evidence without crossing a retry's raw/ swap."""
            from scripts.file_lock import cross_process_lock

            try:
                with cross_process_lock(_raw_dir.parent / ".corporate_actions.lock"):
                    return _persist_archive_corporate_action_factor_unlocked(
                        ticker, since, as_of, result, _raw_dir=_raw_dir
                    )
            except (OSError, TimeoutError):
                return False

        # near_close_dca invokes this only when a live lookup could not
        # confirm the action window and the mutable ticker-history archive did.
        # Keeping the callback on the frozen provider makes persistence part of
        # the evidence hand-off rather than an after-the-fact manifest refresh.
        frozen_finance.persist_archive_corporate_action_factor = (  # type: ignore[attr-defined]
            _persist_archive_corporate_action_factor
        )
        # Corporate actions are intentionally a same-run live query, not a
        # property of the intraday OHLC snapshot above.  Preserve that live
        # capability just as the Robinhood spread breaker above preserves its
        # own last-moment quote capability; otherwise the frozen provider's
        # 1440-minute lookup would see no persisted rows and silently disable
        # split rebasing in the unattended persist-before-compute path.
        #
        # Unlike the spread breaker (whose result never itself authorizes a
        # buy -- it only vetoes one already decided), this live query CAN
        # move a durable latch's reference/cursor and thereby authorize a
        # fire.  Wiring the bare live callable straight onto the frozen
        # provider would let that decision run on data never captured under
        # raw_dir, so replaying the persisted raw snapshot could not
        # reproduce or audit it -- exactly the persist-before-compute gap
        # this frozen-provider machinery exists to close (Codex review:
        # "Persist corporate-action evidence before using it").  Wrap the
        # live callable so every query's evidence is written to raw_dir
        # before the result is handed back, and fail closed (refuse the
        # evidence, keep the durable reference/cursor unchanged) when that
        # write itself fails, rather than silently using unpersisted data.
        #
        # A later replay/reconstruction of THIS SAME raw_dir (e.g. a retry
        # that rebuilds frozen providers from an already-persisted snapshot,
        # possibly with a different or absent live original_finance) must
        # reproduce the identical decision, not re-issue a fresh live query
        # that could answer differently -- otherwise the same persisted
        # snapshot could authorize a different fire on replay, defeating the
        # audit guarantee raw_dir exists to provide (Codex review: "Replay
        # the saved corporate-action evidence"). So a persisted
        # corporate_action_SYMBOL.json matching this exact (ticker, since,
        # as_of) query is read back and returned as-is, live query skipped
        # entirely; only a query this raw_dir has never seen falls through
        # to the live callable (and gets persisted for the next replay).
        _live_actions = getattr(original_finance, "get_corporate_action_factor", None)
        if callable(_live_actions):
            from scripts.file_lock import cross_process_lock

            def _persisted_corporate_action_factor_under_lock(
                ticker: str, since: "date", as_of: "date", *, _live=_live_actions, _raw_dir=raw_dir
            ):
                symbol = ticker.strip().upper()
                since_iso = since.isoformat()
                as_of_iso = as_of.isoformat()
                persisted_path = _raw_dir / f"corporate_action_{symbol}.json"
                query_path = _raw_dir / (
                    f"corporate_action_{symbol}_{since_iso}_{as_of_iso}.json"
                )
                persisted_file_present = persisted_path.is_file()
                # Reset on every call before deciding: near_close_dca.py reads
                # this right after invoking us to decide whether to count a
                # connector attempt, and a cache hit below must not leave a
                # PRIOR call's False (real attempt) value lying around for
                # this one to be mistakenly credited with (Codex review:
                # "Count cached action evidence as zero connector calls").
                _persisted_corporate_action_factor.served_from_cache = False  # type: ignore[attr-defined]
                for candidate_path in (persisted_path, query_path):
                    if not candidate_path.is_file():
                        continue
                    try:
                        cached = json.loads(candidate_path.read_bytes())
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        cached = None
                    if (
                        isinstance(cached, dict)
                        and cached.get("ticker") == symbol
                        and cached.get("since") == since_iso
                        and cached.get("as_of") == as_of_iso
                    ):
                        # Served entirely from the persisted snapshot -- the
                        # live connector was never invoked, so the caller must
                        # not count this as a connector attempt (Codex review:
                        # "Count cached action evidence as zero connector
                        # calls"): incrementing usage_proxy.finance_calls_
                        # attempted here would overstate connector usage on
                        # every replay of already-saved evidence.
                        _persisted_corporate_action_factor.served_from_cache = True  # type: ignore[attr-defined]
                        cached_factor = cached.get("factor")
                        cached_cursor = cached.get("cursor")
                        if cached_factor is None or cached_cursor is None:
                            return None
                        return _validated_corporate_action_result(
                            (cached_factor, cached_cursor), as_of
                        )

                # Validated the same way a cached replay is, before either
                # persisting or returning it: a non-conforming fresh result
                # from a custom/injected provider (a nonpositive/NaN factor,
                # or a cursor that doesn't cover as_of) must not silently
                # advance the durable cursor past an action a later corrected
                # response could still confirm (Codex review: "Validate
                # fresh action results before persisting them").
                result = _validated_corporate_action_result(_live(ticker, since, as_of), as_of)
                if since == as_of and persisted_file_present:
                    # A same-artifact-directory retry whose tracker state
                    # already advanced past the query that produced the
                    # existing file asks a trivial since == as_of no-op --
                    # always (1.0, since) regardless of what was persisted
                    # (see get_corporate_action_factor's own short-circuit).
                    # Persisting it here would overwrite the only record of
                    # the real factor/cursor the earlier, non-trivial query
                    # captured with a result that carries no new evidence
                    # (Codex review: "Keep prior action queries when retry
                    # parameters change"). The trivial answer is still
                    # correct to return -- only the write is skipped.
                    return result
                payload = {
                    "ticker": symbol,
                    "since": since_iso,
                    "as_of": as_of_iso,
                    "factor": result[0] if result is not None else None,
                    "cursor": result[1].isoformat() if result is not None else None,
                }
                try:
                    # The original per-symbol filename remains the first
                    # query's immutable evidence.  A retry with different
                    # window parameters gets its own snapshot rather than
                    # replacing evidence an in-flight run already consumed.
                    output_name = (
                        query_path.name if persisted_file_present else persisted_path.name
                    )
                    save_raw_input(
                        "",
                        output_name,
                        payload,
                        raw_dir=_raw_dir,
                    )
                except (OSError, ValueError):
                    return None
                return result

            def _persisted_corporate_action_factor(
                ticker: str, since: "date", as_of: "date", *, _raw_dir=raw_dir
            ):
                """Serialize snapshot selection, lookup, and persistence.

                Retries can construct separate frozen providers over the same
                artifact directory.  Holding a cross-process lock across the
                cache check and live lookup ensures only the first retry can
                establish evidence for a query; later retries replay that
                exact persisted answer instead of overwriting it with a
                different response before the first retry publishes state.

                The lock file lives in ``_raw_dir``'s PARENT (the stable
                per-slot artifacts directory), not inside ``_raw_dir`` itself:
                ``_persist_raw_and_freeze`` renames the old ``raw/`` to
                ``raw.bak`` and swaps a freshly staged directory into its
                place on every attempt, so a lock sidecar placed inside
                ``raw/`` would sit on a brand-new inode each retry -- two
                concurrent retries would each acquire their own independent
                lock on their own ``raw/.corporate_actions.lock`` and never
                actually contend, reopening the exact same race this lock
                exists to close (Codex review: "Keep the action lock outside
                the swapped raw tree"). The parent directory itself is never
                renamed or swapped, only its ``raw``/``raw.bak``/``raw.tmp``
                children, so a lock file there is the same inode across every
                retry sharing this artifacts directory.
                """
                try:
                    with cross_process_lock(_raw_dir.parent / ".corporate_actions.lock"):
                        return _persisted_corporate_action_factor_under_lock(ticker, since, as_of)
                except (OSError, TimeoutError):
                    return None

            frozen_finance.get_corporate_action_factor = (  # type: ignore[attr-defined]
                _persisted_corporate_action_factor
            )
        else:
            # ``frozen_finance`` is a real ``FinanceDataProvider`` instance, so
            # it always exposes ``get_corporate_action_factor`` as a class
            # method regardless of what ``original_finance`` (a legacy/test
            # provider without the new capability) supports.  Leaving it in
            # place would make near_close_dca's ``callable(...)`` capability
            # check see a live lookup that is always callable, so it would
            # never fall through to ``archived_split_factor`` -- and the
            # frozen 1440-minute bars query, seeing only the persisted
            # intraday snapshot above, always returns no rows and therefore
            # ``None``, silently disabling the documented legacy archive
            # fallback (Codex review: "Preserve archive fallback for frozen
            # legacy providers").  Shadow the class method with an explicit
            # ``None`` instance attribute so the capability check correctly
            # reports "unavailable" and near_close_dca uses the archive path.
            frozen_finance.get_corporate_action_factor = None  # type: ignore[attr-defined]
    else:
        if strict_finance:
            raise RuntimeError(
                "finance raw files (finance_bars_batchN.json / finance_close_batchN.json) "
                f"absent from {raw_dir} — a scheduled live run requires a frozen finance "
                "provider to satisfy the persist-before-compute contract; persist finance "
                "connector responses under those filenames or add them to raw_connector_inputs"
            )
        logger.warning(
            "finance raw files (finance_bars_batchN.json / finance_close_batchN.json) "
            "absent from %s — finance_data will use the live provider; "
            "persist finance connector responses to satisfy the full "
            "persist-before-compute contract",
            raw_dir,
        )
        frozen_finance = original_finance

    return frozen_finance, frozen_robinhood


def _run_drift_check(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Invoke the artifact drift detector after a live run writes its artifacts.

    Writes ``drift_report.json`` and ``drift_report.md`` alongside the run's
    artifacts and logs a warning when drift is detected. Sends a Slack alert
    when ``DCA_DRIFT_SLACK_WEBHOOK`` is set and drift is detected. Returns the
    parsed drift report dict, or None when the directory is missing/unreadable.
    """
    try:
        from scripts.drift_detection import (  # lazy — stdlib only dep
            DEFAULT_SLACK_WEBHOOK_ENV,
            detect_drift,
            send_slack_alert,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("drift detection unavailable: %s", exc)
        return None
    try:
        report = detect_drift(run_dir)
        atomic_write_text(run_dir / "drift_report.json", report.to_json(compact=False))
        atomic_write_text(run_dir / "drift_report.md", report.to_markdown())
        if report.drift_detected:
            logger.warning(
                "DRIFT DETECTED in %s: %d symbol(s) expected but no order record — %s",
                run_dir,
                len(report.affected_symbols),
                ", ".join(s.symbol for s in report.affected_symbols),
            )
            try:
                send_slack_alert(report, env_var=DEFAULT_SLACK_WEBHOOK_ENV)
            except Exception as slack_exc:  # pragma: no cover
                logger.warning("drift Slack alert failed: %s", slack_exc)
        else:
            logger.info("drift check OK for %s", run_dir)
        return report.to_dict()
    except Exception as exc:  # pragma: no cover
        logger.warning("drift check failed for %s: %s", run_dir, exc)
        return None


def configure_logging(level: str = "INFO") -> None:
    """Configure a single stream handler for CLI/scheduler use (idempotent)."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _fills_from_orders_csv(orders_csv: Path) -> List[Dict[str, Any]]:
    """Read filled orders from a compact-run orders.csv into report-ready fill dicts."""
    if not orders_csv.exists():
        return []
    fills: List[Dict[str, Any]] = []
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("status") or "").lower() != "filled":
                continue
            fill_price = (row.get("avg_fill_price") or "").strip()
            filled_qty = (row.get("quantity_filled") or "").strip()
            if fill_price == "" or fill_price == "-" or filled_qty == "" or filled_qty == "-":
                continue
            fills.append(
                {
                    "ticker": row.get("ticker"),
                    "fill_price": fill_price,
                    "filled_qty": filled_qty,
                    "order_id": row.get("order_id"),
                }
            )
    return fills


def _execution_targets_from_orders_csv(orders_csv: Path) -> Dict[str, Optional[float]]:
    """Map ticker -> the execution-record target price (signal-time quote) from orders.csv.

    The durable orders.csv records ``decision.target_price`` (the signal-time quote),
    while run_state's ``buy_candidates`` store the decision-bar close. Reconciling to the
    execution target keeps the full slippage report/Notion summary consistent with the
    orders CSV instead of ranking against a different price.
    """
    targets: Dict[str, Optional[float]] = {}
    if not orders_csv.exists():
        return targets
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ticker = row.get("ticker")
            if not ticker:
                continue
            raw = row.get("target_price")
            try:
                targets[ticker] = float(raw) if raw not in (None, "", "-") else None
            except (TypeError, ValueError):
                targets[ticker] = None
    return targets


def _execution_targets_by_order_id_from_orders_csv(orders_csv: Path) -> Dict[str, Optional[float]]:
    """Map order_id -> the execution-record target price from orders.csv.

    A recovered order and a separate current-cycle order for the same ticker can both
    appear in orders.csv; the ticker-keyed ``_execution_targets_from_orders_csv`` map only
    retains one of them. Keying by order_id lets the candidate-target reconciliation below
    tell the two apart instead of assigning one order's target to both (Codex review:
    "Match execution targets by order ID").
    """
    targets: Dict[str, Optional[float]] = {}
    if not orders_csv.exists():
        return targets
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            order_id = row.get("order_id")
            if not order_id:
                continue
            raw = row.get("target_price")
            try:
                targets[order_id] = float(raw) if raw not in (None, "", "-") else None
            except (TypeError, ValueError):
                targets[order_id] = None
    return targets


def _apply_execution_targets_to_candidates(
    buy_candidates: List[Dict[str, Any]],
    targets: Mapping[str, Optional[float]],
    targets_by_order_id: Mapping[str, Optional[float]],
) -> None:
    """Reconcile each candidate's target_price against the durable orders.csv record.

    Prefers the order-id-keyed target when the candidate carries one (so a recovered
    order and a distinct current-cycle order for the same ticker each keep their own
    target), falling back to the ticker-keyed map otherwise.

    A recovered crash-lease execution row deliberately stores target_price=0.0 as a
    no-target sentinel (near_close_dca's recovery-publish loop has no
    ReconciledDecision to draw a real target from). Accepting that 0.0 here marked
    the candidate's target as PRESENT, so compute_slippage_rows() went on to compute
    a meaningless slippage_abs against zero and build_latest_run_summary() counted
    the fill as neither priced nor target_price_missing (Codex review: "Keep zero
    recovery targets marked as missing"). Nonpositive targets are treated as
    unavailable, same as an absent one.
    """
    for candidate in buy_candidates:
        candidate_order_id = candidate.get("order_id")
        candidate_ticker = candidate.get("ticker")
        if candidate_order_id and candidate_order_id in targets_by_order_id:
            target = targets_by_order_id[candidate_order_id]
        elif isinstance(candidate_ticker, str):
            target = targets.get(candidate_ticker)
        else:
            target = None
        if target is not None and target > 0:
            candidate["target_price"] = target
            candidate["target_price_missing"] = False


def _refresh_fills_from_broker(
    orders_csv: Path, broker: BrokerAdapter, fills: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Re-query the broker for submitted-but-unfilled orders and append any fresh fills.

    The immediate post-trade fill lookup can miss a fill that settles moments later; by the
    3:45 reporting step it is available. Only orders not already recorded with a COMPLETE
    fill (status='filled' AND a usable avg_fill_price/quantity_filled) are re-queried, and
    lookup failures are swallowed so reporting never crashes.

    A row whose CSV status is already 'filled' but whose avg_fill_price/quantity_filled is
    still blank is NOT treated as already known: ``_fills_from_orders_csv`` excludes such a
    row from ``fills`` for exactly this reason (its data is not yet usable), so skipping it
    here too -- solely because the status column says 'filled' -- would make it permanently
    unrecoverable: never counted as a fill, and never re-queried once ``get_order_fill()``
    actually has complete data (Codex review: "Refresh filled rows whose execution details
    are missing").

    Lookups run concurrently only when ``broker.supports_concurrent_fill_lookup`` is
    ``True``. ``get_order_fill`` is commonly a thin wrapper around a scheduler-owned MCP
    callable that is not guaranteed thread-safe (see docs/live_wiring.md), so the default
    is a plain sequential loop; brokers must opt in once their wiring is verified safe for
    concurrent calls.
    """
    if not orders_csv.exists():
        return fills
    known = {f.get("order_id") for f in fills}
    refreshed = list(fills)
    queries = []

    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            order_id = row.get("order_id")
            status = (row.get("status") or "").lower()
            if not order_id:
                continue
            fill_price = (row.get("avg_fill_price") or "").strip()
            filled_qty = (row.get("quantity_filled") or "").strip()
            has_complete_fill_data = fill_price not in ("", "-") and filled_qty not in ("", "-")
            # A terminal crash-lease recovery valued ONLY through
            # executed_notional (action_reason ==
            # "recovered_inverse_martingale_lease" -- the only place
            # force_filled is ever passed, see near_close_dca.
            # build_execution_record) is DELIBERATELY left unpriced by the
            # recovery logger itself, which already prefers the retained
            # terminal quantity over a get_order_fill() lookup for exactly
            # this reason: that lookup can be eventually consistent and
            # still return an earlier PARTIAL fill. Re-querying it again
            # HERE, independently, at report-generation time would accept
            # that same stale partial as if it were the terminal execution
            # detail, undoing the recovery logger's own protection (Codex
            # review: "Keep terminal notional-only recoveries unpriced").
            #
            # Restricted to a genuinely notional-only row (no granular data
            # in the CSV): when terminal history supplied a valid price AND
            # quantity, the recovery logger already wrote a complete row --
            # blanking it here unconditionally would discard valid quantity/
            # slippage for a PRICED recovery, not just protect an unpriced
            # one (Codex review: "Preserve granular details for priced
            # recovery fills").
            #
            # Checked BEFORE the `order_id in known` skip below: a failed
            # attempt's preserved raw/orders_after_place.json can carry an
            # EARLIER PARTIAL fill for this exact order_id, loaded into
            # `fills` (and therefore `known`) before this function ever
            # runs. Checking the recovery marker only after that skip let
            # the stale persisted partial win outright -- this path never
            # calls get_order_fill() at all, so nothing else would ever
            # override it (Codex review: "Override stale persisted fills
            # for recovery rows"). Any such stale entry is dropped from
            # `refreshed` here so the authoritative recovery row -- unpriced
            # on purpose -- is the only one that survives for this order_id.
            if (
                status == "filled"
                and row.get("action_reason") == "recovered_inverse_martingale_lease"
                and not has_complete_fill_data
            ):
                if order_id in known:
                    refreshed = [f for f in refreshed if f.get("order_id") != order_id]
                known.add(order_id)
                refreshed.append(
                    {
                        "ticker": row.get("ticker"),
                        "fill_price": "",
                        "filled_qty": "",
                        "order_id": order_id,
                    }
                )
                continue
            if order_id in known or (status == "filled" and has_complete_fill_data):
                continue
            # Deduplicate locally before fetching, regardless of lookup outcome, so a CSV
            # with repeated rows for the same order_id never queries it twice.
            known.add(order_id)
            queries.append((order_id, row.get("ticker"), status))

    if not queries:
        return refreshed

    def _fetch(query_tuple):
        oid, tkr, row_status = query_tuple
        try:
            return oid, tkr, row_status, broker.get_order_fill(oid)
        except Exception:  # noqa: BLE001 - a fill re-query must never crash reporting
            logger.exception("fill refresh failed for %s", oid)
            return oid, tkr, row_status, None

    if getattr(broker, "supports_concurrent_fill_lookup", False):
        with ThreadPoolExecutor(max_workers=10) as executor:
            # Use map to preserve the order of elements from queries
            results = list(executor.map(_fetch, queries))
    else:
        results = [_fetch(query) for query in queries]

    for order_id, ticker, row_status, fill in results:
        if fill is not None and fill.quantity_filled > 0 and fill.avg_fill_price > 0:
            refreshed.append(
                {
                    "ticker": ticker,
                    "fill_price": fill.avg_fill_price,
                    "filled_qty": fill.quantity_filled,
                    "order_id": order_id,
                }
            )
        elif row_status == "filled":
            # A crash-lease recovery valued only through executed_notional
            # (force_filled, no avg_fill_price/quantity_filled breakdown --
            # see near_close_dca.build_execution_record's force_filled
            # parameter) is durably "filled" in orders.csv, but
            # get_order_fill() will never expose more than notional for it
            # -- there is no granular breakdown to recover. Leaving such a
            # row permanently excluded from `fills` made
            # build_missing_fills() (which keys presence purely off ticker
            # membership) report a broker-confirmed recovery as a missing/
            # uncovered candidate, understating coverage (Codex review:
            # "Propagate notional-only fills to full reporting"). Added as
            # an UNPRICED fill instead -- present for coverage purposes,
            # with fill_price/filled_qty left blank so slippage computation
            # (which needs both) still correctly skips it rather than
            # fabricating a $0 price. A row still genuinely working (CSV
            # status not yet "filled") gets no such fallback: an absent
            # fill there means "not yet confirmed", not "confirmed but
            # unpriced", and must keep reporting as missing until it
            # actually settles.
            refreshed.append(
                {
                    "ticker": ticker,
                    "fill_price": "",
                    "filled_qty": "",
                    "order_id": order_id,
                }
            )

    return refreshed


def _default_prior_allocation_state_root(artifacts_dir: Path) -> Path:
    """Derive the cron task root from a scheduled run's artifacts_dir.

    Per config/scheduled_tasks.json the live scheduled path writes into
    ``<task_root>/scheduled_runs/YYYYMMDD_1530ET/``, and prior same-day
    manual/morning allocations live under the sibling
    ``<task_root>/manual_runs/<timestamp>/``. A plain ``.parent`` would land on
    ``scheduled_runs/`` itself (which never contains ``manual_runs/``), so walk
    up looking for a ``scheduled_runs`` path segment and return its parent
    instead; fall back to the immediate parent when the layout doesn't match.
    """
    for candidate in (artifacts_dir, *artifacts_dir.parents):
        if candidate.name == "scheduled_runs":
            return candidate.parent
    return artifacts_dir.parent


def _compute_auto_derived_prior_allocations(
    now: datetime,
    trading_day: date,
    artifacts_dir: str | Path,
    config_dir: Optional[str | Path],
    *,
    max_per_order_usd: float = _UNRECONSTRUCTABLE_SUBMISSION_AMOUNT_USD,
    unreconstructable_sink: Optional[List[str]] = None,
) -> Tuple[Dict[str, PriorSameDayAllocation], bool]:
    """Aggregate same-day prior-allocation context from the auto-derived roots.

    Folds the afternoon task root, the morning/midday cross-stream roots (incl.
    manual override reruns), and manual CSV-only context
    (``scripts.run_classification.load_same_day_top_up_context``) into one
    per-ticker :class:`PriorSameDayAllocation` map. Pure function of the
    artifact filesystem state at call time -- callers may invoke this more than
    once for the same run (see the bounded-reload wrapper around this
    function's call site, canonical rule item 13).

    Returns ``(allocations, scan_had_failures)``. ``scan_had_failures`` is True
    when any ``run_state*.json`` candidate under the scanned roots could not be
    read or parsed this pass (see ``load_prior_same_day_allocations``'s
    ``skip_sink``). Such a file contributes no ticker at all to ``allocations``,
    so a ticker whose ONLY same-day artifact hit this race is indistinguishable
    from a ticker with no prior run at all if a caller looks only at
    ``allocations`` -- the caller must check this flag too to know a reload
    could still resolve that ticker (Codex review: "Detect artifacts skipped
    during the initial context scan").

    ``unreconstructable_sink``, when given, has each ticker whose amount was
    ever seeded from the conservative ``max_per_order_usd`` sentinel appended
    to it (forwarded to each per-root call; see
    :func:`near_close_dca.load_prior_same_day_allocations`). That ticker's
    unknown reference already blocks a fresh buy of IT specifically, but the
    estimated dollar amount also feeds the shared daily/weekly totals via
    ``_seed_prior_allocations``, and this run's own ceiling is not proof
    about whatever ceiling governed the EARLIER same-day run that actually
    placed the order -- config can change between an earlier same-day run
    and this one (Codex review: "Fail same-day unknown submission amounts
    closed globally"). Deliberately NOT folded into this function's own
    returned ``scan_had_failures`` (which drives only the PER-TICKER bounded
    reload/poisoning below): the caller instead folds this sink into
    ``earlier_week_scan_had_failures`` to force the SAME global
    worst-case-ceiling fail-closed
    :func:`_compute_earlier_week_cross_stream_notional` already applies for
    the analogous earlier-WEEK case, without changing this function's
    existing skip_sink-only reload trigger.
    """
    # Each stream's root is read from its OWN config rather than assumed from
    # directory-naming convention, so a relocated artifact root is still found.
    afternoon_root = _default_prior_allocation_state_root(Path(artifacts_dir))
    cross_stream_roots = _resolve_cross_stream_prior_allocation_roots(
        config_dir, afternoon_root
    )
    # Unpruned companion set for the bare-CSV fallback below; see that call site.
    fallback_discovery_roots = _resolve_cross_stream_prior_allocation_roots(
        config_dir, afternoon_root, for_fallback_discovery=True
    )
    skip_sink: List[Path] = []
    # All roots are capped at the actual run time so replayed/backfilled runs
    # never load fills created after the scheduled execution window. The
    # current run's own artifact is excluded by classification (run_origin !=
    # "scheduled" or stream mismatch) not by time, so passing `now` here is
    # safe and captures late manual fills (e.g. a 15:05 manual override under
    # ce16ba6b/manual_runs).
    prior_same_day_allocations = load_prior_same_day_allocations_multi(
        (afternoon_root,),
        trading_day,
        boundary_et=now.astimezone(EASTERN).time(),
        skip_sink=skip_sink,
        unreconstructable_submission_amount_usd=max_per_order_usd,
        unreconstructable_sink=unreconstructable_sink,
    )
    cross_stream_allocs = load_prior_same_day_allocations_multi(
        cross_stream_roots,
        trading_day,
        boundary_et=now.astimezone(EASTERN).time(),
        skip_sink=skip_sink,
        unreconstructable_submission_amount_usd=max_per_order_usd,
        unreconstructable_sink=unreconstructable_sink,
    )
    for ticker, alloc in cross_stream_allocs.items():
        existing = prior_same_day_allocations.get(ticker)
        if existing is None:
            prior_same_day_allocations[ticker] = alloc
            continue
        # Most-recent reference wins: cross-stream vs the afternoon-own
        # allocation, compared by generated_at (untimestamped -> floor).
        if existing.reference_price is None or alloc.reference_price is None:
            ref = None
            ref_ts = None
            ref_run_id = alloc.run_id or existing.run_id
            ref_source = alloc.source or existing.source
        else:
            existing_ts = existing.reference_ts or _TS_FLOOR
            alloc_ts = alloc.reference_ts or _TS_FLOOR
            if alloc_ts >= existing_ts:
                ref = alloc.reference_price
                ref_ts = alloc.reference_ts
                # The run/source that actually supplied the winning reference
                # price travels with it, not whichever side is scanned first --
                # otherwise a top-up audit trail can pair a newer reference
                # price with an older run_id.
                ref_run_id = alloc.run_id or existing.run_id
                ref_source = alloc.source or existing.source
            else:
                ref = existing.reference_price
                ref_ts = existing.reference_ts
                ref_run_id = existing.run_id or alloc.run_id
                ref_source = existing.source or alloc.source
        # Pick the chronologically FIRST allocation's base rather than always
        # keeping `existing` (the afternoon root, scanned first above) -- an
        # earlier morning/midday allocation must win over a later same-day
        # manual override recorded under the afternoon root, even though the
        # afternoon root loads first (Codex review: "Select the base from the
        # chronologically first allocation").
        base, base_at = _earlier_selected_base(
            existing.selected_base_amount_usd,
            existing.selected_base_amount_usd_at,
            alloc.selected_base_amount_usd,
            alloc.selected_base_amount_usd_at,
        )
        # Same dedup rule as near_close_dca.load_prior_same_day_allocations_
        # multi's own cross-root merge (Codex review: "Apply mirrored-run
        # dedup across the afternoon merge boundary"): a manual override
        # mirrored under BOTH the afternoon root and a sibling root (e.g.
        # ce16ba6b/manual_runs and midday_dca/manual_reruns) is the SAME
        # run, not two independent contributions. Skip re-adding
        # alloc.amount_usd/attempt_count only when alloc's contributing
        # run_ids are a full, non-empty subset of existing's -- an
        # unambiguous complete duplicate; a partial overlap falls through
        # to the additive merge, the conservative direction.
        #
        # Deliberately NOT also gated on order/uncertain-contributor
        # identity coverage (Codex review, PR badMade/all-stock-dca#1171
        # discussion: "Do not claim skipped anonymous dollars as
        # represented") -- see the identical, more detailed note on the
        # sibling near_close_dca.load_prior_same_day_allocations_multi
        # merge this mirrors: that gate was tried and broke
        # test_compute_auto_derived_prior_allocations_full_duplicate_merge_
        # unions_order_ids, an existing, deliberately-tested contract that
        # already accepts this exact tradeoff for contributing_order_ids.
        is_full_duplicate = bool(
            alloc.contributing_run_ids
        ) and alloc.contributing_run_ids <= existing.contributing_run_ids
        if is_full_duplicate:
            merged_amount = existing.amount_usd
            merged_contributing_run_ids = existing.contributing_run_ids
        else:
            merged_amount = existing.amount_usd + alloc.amount_usd
            merged_contributing_run_ids = (
                existing.contributing_run_ids | alloc.contributing_run_ids
            )
        # order_ids are ALWAYS unioned, independent of is_full_duplicate:
        # is_full_duplicate is decided on run-id subset alone, but one
        # run_id can expose multiple distinct orders for the same ticker
        # (see PriorSameDayAllocation.contributing_order_ids' own
        # docstring), so a root whose run_ids are a full subset of
        # existing's can still carry an order identity existing has never
        # seen (e.g. an uncertain order discovered only in this root under
        # the same run_id). Restricting the union to the non-duplicate
        # branch would drop that identity and let a later reload's equal
        # order-id set wrongly look like a provenance match (Codex review:
        # "Preserve uncertain order IDs across duplicate-root merges").
        merged_contributing_order_ids = (
            existing.contributing_order_ids | alloc.contributing_order_ids
        )
        # Same reasoning, same union, for id-less uncertain contributors
        # (no order_id at all -- see PriorSameDayAllocation.
        # uncertain_contributor_ids' own docstring): a root whose run_ids
        # are a full subset of existing's can still carry a synthetic
        # identity existing has never seen, and dropping it would let a
        # later reload's equal uncertain_contributor_ids set wrongly look
        # like proof this root's own anonymous contributor was re-verified.
        merged_uncertain_contributor_ids = (
            existing.uncertain_contributor_ids | alloc.uncertain_contributor_ids
        )
        # Attempt-count dedup is decided INDEPENDENTLY of the dollar dedup
        # above, using attempt_run_ids (a strict superset of
        # contributing_run_ids that also covers zero-dollar attempts). A
        # mirrored zero-notional attempt never enters contributing_run_ids at
        # all (it contributed no dollars), so is_full_duplicate above is
        # always False for it and a bare additive sum double-counts the same
        # broker attempt against max_orders_per_day (Codex review:
        # "Deduplicate mirrored zero-notional attempts"). Attempt counts must
        # be summed (not defaulted back to 1 by a bare
        # PriorSameDayAllocation(...) construction) when not a duplicate --
        # otherwise _seed_prior_allocations reconstructs too few attempts
        # against the shared max_orders_per_day breaker and can permit an
        # extra live order (Codex review: "Carry attempt counts through the
        # final cross-stream merge").
        is_full_attempt_duplicate = bool(
            alloc.attempt_run_ids
        ) and alloc.attempt_run_ids <= existing.attempt_run_ids
        if is_full_attempt_duplicate:
            merged_attempt_count = existing.attempt_count
            merged_attempt_run_ids = existing.attempt_run_ids
        else:
            merged_attempt_count = existing.attempt_count + alloc.attempt_count
            merged_attempt_run_ids = existing.attempt_run_ids | alloc.attempt_run_ids
        prior_same_day_allocations[ticker] = PriorSameDayAllocation(
            ticker=ticker,
            amount_usd=merged_amount,
            reference_price=ref,
            run_id=ref_run_id,
            source=ref_source,
            reference_ts=ref_ts,
            selected_base_amount_usd=base,
            selected_base_amount_usd_at=base_at,
            attempt_count=merged_attempt_count,
            contributing_run_ids=merged_contributing_run_ids,
            contributing_order_ids=merged_contributing_order_ids,
            uncertain_contributor_ids=merged_uncertain_contributor_ids,
            attempt_run_ids=merged_attempt_run_ids,
            unresolved_live_exposure=(
                existing.unresolved_live_exposure or alloc.unresolved_live_exposure
            ),
            # Either source may only know a conservative sentinel for this
            # ticker.  Preserve that uncertainty through the cross-stream
            # merge: losing it makes the later bounded reload regard the
            # sentinel as an authoritative amount and reopen shared-cap
            # headroom.
            notional_uncertain=(
                existing.notional_uncertain or alloc.notional_uncertain
            ),
        )
    # Manual-context discovery (scripts.run_classification.load_same_day_top_up_context)
    # only runs for the auto-derived root: it scans SIBLINGS of afternoon_root's
    # parent (the production cron_tracking layout), which would silently pull in
    # unrelated trees for a caller that passed an explicit prior_allocation_state_root
    # to scan ONLY that tree (Codex review: "Respect explicit prior-allocation root
    # isolation"). Capped at the run time for the same replay-safety reason as above
    # (Codex review: "Bound manual top-up context to the scheduled execution time").
    merge_manual_top_up_context(
        prior_same_day_allocations,
        load_same_day_top_up_context(
            trading_day.isoformat(),
            base=afternoon_root,
            boundary_et=now.astimezone(EASTERN).time(),
            # A bare manual CSV (referenced by no run_state at all, the sole
            # case this loader exists to handle) that fails to read/parse is
            # exactly as unverifiable as a core-loader completeness failure
            # -- feed it into the SAME skip_sink so scan_had_failures
            # reflects it too (Codex review: "Include bare manual CSV
            # failures in scan completeness").
            skip_sink=skip_sink,
            # The configured stream roots for FALLBACK discovery, so a
            # scheduled-recovery directory under a relocated
            # morning/midday/intraday/market-open root is picked up here as
            # well. Without this, a recovery whose fills live only in a bare
            # sibling CSV is invisible to both loaders in such a deployment
            # and the ticker is re-allocated in full (Codex review: "Thread
            # configured roots into top-up loading").
            #
            # Deliberately NOT `cross_stream_roots`: that tuple is pruned of
            # roots contained by afternoon_root because the core scan covers
            # that tree, but this fallback never scans afternoon_root, so a
            # configured root nested there would go undiscovered (Codex
            # review: "Preserve nested configured roots for fallback
            # discovery").
            stream_roots=fallback_discovery_roots,
        ),
    )
    return prior_same_day_allocations, bool(skip_sink)


def _compute_earlier_week_cross_stream_notional(
    now: datetime,
    trading_day: date,
    artifacts_dir: str | Path,
    config_dir: Optional[str | Path],
    *,
    max_per_order_usd: Optional[float] = None,
) -> Tuple[float, bool]:
    """Sum EARLIER-weekday cross-stream spend for the afternoon guard's shared weekly cap.

    ``_compute_auto_derived_prior_allocations`` only scans ``trading_day`` itself,
    so a fresh afternoon run started after Monday never saw Monday's
    market-open/morning/midday/intraday spend against the shared weekly
    ceiling -- the newly-shared $900 weekly pool could be exceeded (Codex
    review: "Include prior-week sibling spend in the afternoon ledger").

    Mirrors ``morning_dca.py``'s own earlier-weekday ledger scan
    (``MorningDCAExecutor._seed_ledger_from_prior_runs``): a PURE DOLLAR
    total per earlier completed day, decoupled from any specific ticker's
    same-day top-up reference price (which stays scoped to TODAY, exactly
    as ``_compute_auto_derived_prior_allocations`` already handles it --
    conflating an earlier day's spend into a ticker's
    ``PriorSameDayAllocation`` would make the same-day top-up guard require
    today's price to beat an unrelated day's price instead of evaluating a
    fresh, independent DCA occasion). Reuses
    ``load_prior_same_day_allocations_multi`` per earlier day rather than
    re-implementing per-run CSV/candidate parsing -- that loader already
    handles every artifact shape (embedded candidates, filled/submitted
    order CSVs, the canonical ``submitted_orders.csv`` fallback, manual
    reruns/recovery, nested afternoon ``scheduled_runs/``) via its
    recursive ``rglob`` scan, so this only needs to sum the per-ticker
    amounts it returns for each earlier day and does not need its own
    manual-subdir/glob logic the way morning_dca.py's scan did (that scan
    predates the shared loader's own earlier-weekday support).

    ``include_scheduled_afternoon=True`` for every earlier day scanned here
    (unlike the caller's SAME-day scan, which always leaves it False): a
    completed earlier day's OWN afternoon buy is real, already-settled
    spend that must count toward this week's shared ledger, whereas
    excluding it is only correct for TODAY's not-yet-run afternoon slot.

    Returns ``(total_usd, scan_had_failures)``. ``scan_had_failures`` is
    True when any artifact under an earlier day could not be read/parsed
    this pass -- the caller must fail closed (force the weekly bucket to
    the worst-case tier ceiling) rather than trust an unproven, possibly
    understated total.

    ``max_per_order_usd``, when given, is forwarded to
    :func:`near_close_dca.load_prior_same_day_allocations_multi` as the bound
    for an unreconstructable submission's weight against this weekly total --
    the run's OWN configured ceiling, not the loader's flat $10 default.
    schemas/budgets.schema.json does not cap ``max_per_order_usd`` at $10, so
    omitting this would under-count an unreconstructable submission on a run
    configured above it (Codex review: "Derive the unknown-submission bound
    from its actual ceiling"). The caller already has a loaded
    ``StrategyConfig`` in scope, so this is threaded through rather than
    reloading config here.

    That figure is still only THIS run's own ceiling, though, not proof
    about the ORIGINATING run's -- an earlier weekday's config may have
    allowed a larger ``max_per_order_usd`` than today's if it was lowered
    since, undercounting that day's real spend against the shared weekly cap
    (Codex review: "Preserve the originating ceiling for historical unknown
    orders"). Rather than guess at a bound that can be proven to dominate
    every config this scan has no visibility into, any unreconstructable
    submission encountered while scanning an earlier day is folded into
    ``scan_had_failures`` the same as an unparseable artifact: the caller
    already fails closed to the worst-case tier ceiling on that signal
    (:meth:`near_close_dca.StrategyState.force_week_notional_to_worst_case_ceiling`),
    which is unconditionally safe regardless of what any earlier day's
    config actually was.
    """
    afternoon_root = _default_prior_allocation_state_root(Path(artifacts_dir))
    cross_stream_roots = _resolve_cross_stream_prior_allocation_roots(
        config_dir, afternoon_root
    )
    # Unpruned companion set for the bare-CSV fallback below -- mirrors
    # _compute_auto_derived_prior_allocations's own fallback_discovery_roots:
    # cross_stream_roots is pruned of roots contained by afternoon_root
    # because the core scan above covers that tree, but the fallback below
    # never scans afternoon_root, so a configured root nested there would go
    # undiscovered otherwise.
    fallback_discovery_roots = _resolve_cross_stream_prior_allocation_roots(
        config_dir, afternoon_root, for_fallback_discovery=True
    )
    all_roots = (afternoon_root, *cross_stream_roots)
    week_start = trading_day - timedelta(days=trading_day.weekday())
    total = 0.0
    skip_sink: List[Path] = []
    unreconstructable_sink: List[str] = []
    earlier_day = week_start
    while earlier_day < trading_day:
        day_allocations = load_prior_same_day_allocations_multi(
            all_roots,
            earlier_day,
            boundary_et=time(23, 59, 59),
            include_scheduled_afternoon=True,
            skip_sink=skip_sink,
            unreconstructable_submission_amount_usd=max_per_order_usd,
            unreconstructable_sink=unreconstructable_sink,
        )
        # The same manual/recovery bare-CSV fallback the same-day path merges
        # in (_compute_auto_derived_prior_allocations), applied per earlier
        # day: an earlier weekday can have the supported manual/recovery
        # shape -- a run_state.json with no embedded buy_candidates or
        # artifacts.orders_csv_path, plus a sibling filled_orders.csv/
        # orders.csv the core loader's recursive scan doesn't recognize on
        # its own -- whose spend was otherwise omitted from this prior-week
        # total entirely (Codex review: "Include unreferenced manual CSVs in
        # prior-week totals").
        merge_manual_top_up_context(
            day_allocations,
            load_same_day_top_up_context(
                earlier_day.isoformat(),
                base=afternoon_root,
                boundary_et=time(23, 59, 59),
                skip_sink=skip_sink,
                stream_roots=fallback_discovery_roots,
            ),
        )
        total += sum(alloc.amount_usd for alloc in day_allocations.values())
        earlier_day += timedelta(days=1)
    return total, bool(skip_sink) or bool(unreconstructable_sink)


def _tickers_with_unresolved_same_day_context(
    prior_same_day_allocations: Dict[str, PriorSameDayAllocation],
) -> List[str]:
    """Tickers with a known prior same-day amount but no resolvable reference price or base.

    Canonical rule item 13 (config/scheduled_tasks.json
    canonical_first_buy_and_topup_rule): "if same-day context cannot be
    resolved for one ticker, perform one bounded same-day context reload, then
    fail only that ticker closed." A nonzero amount_usd with reference_price
    still None means at least one contributing artifact poisoned the
    aggregate reference to unknown (see load_prior_same_day_allocations) --
    the case a bounded reload targets, since an artifact still being flushed
    mid-scan (or a transient parse race with a sibling stream's writer) can
    resolve on a second pass moments later. A known amount with
    selected_base_amount_usd still None is the same kind of unresolved context
    (Codex review: "Fail closed when the first allocation base is unknown") --
    without this, evaluate_top_up_guard's own fallback would silently
    recompute the day's target from this run's freshly selected base instead
    of getting a chance to resolve the real one via the bounded reload.
    """
    return sorted(
        ticker
        for ticker, alloc in prior_same_day_allocations.items()
        if alloc.amount_usd > 0
        and (
            alloc.reference_price is None
            or alloc.selected_base_amount_usd is None
            or alloc.selected_base_amount_usd <= 0
        )
    )


# Recovery triggers whose failure mode a SECOND IN-PROCESS ATTEMPT can plausibly
# clear. Deliberately narrow: this process cannot re-fetch market data (see
# _recovery_trigger_for), so the only genuinely transient class it can retry is
# a filesystem/IO fault while staging, normalizing, or swapping raw/ -- a
# partial write, an interrupted rename, a transient permission or ENOSPC blip.
# Everything else is deterministic on the same in-memory payload and would fail
# identically, so it escalates immediately rather than burning the single
# permitted attempt (config/scheduled_tasks.json: failure_recovery_policy).
RECOVERY_RETRY_TRIGGERS = frozenset({"raw_file_write_failure"})


def _recovery_trigger_for(exc: BaseException) -> Optional[str]:
    """Classify a raw-persistence failure into a ``RECOVERY_TRIGGER_REASONS`` value.

    Returns ``None`` when the failure is not a recovery trigger at all (e.g. the
    contract error raised for absent/empty ``raw_connector_inputs``, which is a
    caller/configuration fault, not a transient one -- retrying it in-process
    cannot help and stamping it as a recovery would misreport a config bug as a
    transient blip).

    IMPORTANT SCOPE NOTE. ``failure_recovery_policy`` in
    ``config/scheduled_tasks.json`` describes recovery as "rerun the required
    read-only inputs once, persist all raw responses first, recompute only from
    the persisted files." Only the *second and third* clauses are implementable
    here. ``run_dca.py`` receives ``raw_connector_inputs`` as an
    already-fetched, immutable mapping and holds no MCP/broker/market-data
    access of its own (docs/live_wiring.md: providers "arrive as injected
    callables"), so it CANNOT re-fetch. ``transient_connector_throttling``
    therefore classifies but never retries in-process -- clearing it requires a
    fresh fetch, which only the invoking agent session can perform. This
    function's job is to keep that boundary explicit instead of silently
    pretending a full recovery ran.
    """
    # OSError covers PermissionError/FileNotFoundError/OSError(ENOSPC) raised by
    # save_raw_input's write, the staging rmtree, and the raw/ <-> raw.bak
    # renames -- the retryable class.
    if isinstance(exc, OSError):
        return "raw_file_write_failure"
    if isinstance(exc, json.JSONDecodeError):
        return "parsing_failure"
    message = str(exc)
    if "normalization failed" in message:
        return "wrapper_normalization_failure"
    return None


def _prior_submitted_order_rows(run_dir: Path) -> List[Dict[str, Any]]:
    """Submitted-order rows already recorded for this slot by an earlier attempt.

    Read from the run directory's own submitted_orders.csv, which a prior
    successful attempt wrote. Used so a fail-closed retry state does not overwrite
    the slot's canonical record with a zero-order one.
    """
    path = run_dir / "submitted_orders.csv"
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                # Same bar submitted_order_rows() applies: a concrete order_id, or
                # a placed/in-flight status. Accepting only submitted/filled would
                # drop a pending or unconfirmed order the broker may well have
                # taken, letting a later run allocate it again (Codex review).
                status = str(row.get("status", "")).strip()
                # Stripped BEFORE the gate, so a whitespace-only id cannot pass as
                # a concrete order id and then be normalized away to None below.
                order_id = str(row.get("order_id") or "").strip() or None
                if not order_id and status not in ORDER_ATTEMPT_STATUSES:
                    continue
                # These dicts are assigned straight to run_state["buy_candidates"],
                # so they must satisfy run_state.schema.json's candidate shape.
                # csv.DictReader hands back every field as a STRING, and the
                # schema requires planned_amount_usd to be a number and
                # buy_multiplier to be present, so carrying the raw row through
                # produced a canonical failure state that schema-based consumers
                # reject while the lightweight validator still passed (Codex
                # review).
                try:
                    amount = float(
                        str(row.get("submitted_amount_usd") or "").strip() or 0.0
                    )
                except (TypeError, ValueError):
                    amount = 0.0
                # A blank ticker would produce a schema-valid but meaningless
                # candidate row in the canonical state, and the same-day guards
                # key on the ticker (Copilot review). Whitespace is stripped for
                # the same reason: " AAA" is not the ticker any consumer matches.
                ticker = str(row.get("ticker") or "").strip()
                if not ticker:
                    continue
                rows.append(
                    {
                        "ticker": ticker,
                        "status": status,
                        "order_id": order_id,
                        # Not recoverable from submitted_orders.csv, which does not
                        # carry it. Null is schema-valid and honest.
                        "buy_multiplier": None,
                        "planned_amount_usd": max(0.0, amount),
                    }
                )
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return rows


def _accepted_order_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The subset of prior rows that represent an order the broker actually took.

    ``not_submitted_confirmation_required`` earns a submitted_orders.csv row --
    the attempt is part of the slot's record -- but it means the opposite of an
    accepted order: the placement stopped at the confirmation gate and nothing
    reached the broker, which is why such a row has no order id. Counting it
    would report ``orders_submitted_count=1`` and a post-submission failure for
    a slot that submitted nothing (Codex review).

    A row carrying a concrete order id is kept whatever its status: an id can
    only come from the broker.
    """
    return [
        row
        for row in rows
        if row.get("order_id")
        or str(row.get("status", "")).strip()
        != ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED
    ]


def _pipeline_result_ok(result: Any) -> bool:
    """Predicate for guarded_run to determine if a pipeline run succeeded.

    The raw-persistence failure path and the preflight-blocked path
    both report their failure as a returned result (each with its own
    targeted alert) instead of raising; either must ping a FAILED
    heartbeat, not clear the dead-man's switch (Codex review: "Route
    the preflight-blocked result through guarded_run"). A directory-
    ownership conflict (an overlapping invocation or a different
    account claimed the shared artifacts directory) is the same
    shape of silent failure: _run_pipeline_impl returns a normal-
    looking result with directory_ownership_verified=False and
    otherwise no error field this predicate already checks, so
    without this clause a scheduled live run whose artifact
    publication was rejected -- any live placements possibly
    surviving only in a manual-reconciliation sidecar -- still
    cleared the success heartbeat and sent no failure alert (Codex
    review: "Fail the heartbeat on directory ownership conflicts").
    """
    return not (
        isinstance(result, dict)
        and (
            result.get("raw_persistence_error")
            or result.get("error_class") == ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT
            or result.get("directory_ownership_verified") is False
        )
    )


def _log_pipeline_model_usage(
    result: Dict[str, Any],
    now: datetime,
    scheduled: bool,
    cron_id: str,
    model_usage_log_root: Optional[str | Path],
    model_usage_model_requested: Optional[str],
    model_usage_model_actual: Optional[str],
    model_usage_model_routing_mode: Optional[str],
    model_usage_reason_for_premium_model: Optional[str],
) -> None:
    """Extract run_id from run_state_path and log model usage."""
    run_state_path = result.get("run_state_path")
    run_id = None
    if run_state_path:
        try:
            run_id = json.loads(
                # ⚡ Bolt Optimization: Keep I/O in binary; json.loads accepts bytes and decodes internally before parsing
                Path(run_state_path).read_bytes()
            ).get("run_id")
        except (OSError, json.JSONDecodeError):
            run_id = None
    routing_overrides = {
        "model_requested": model_usage_model_requested,
        "model_actual": model_usage_model_actual,
        "model_routing_mode": model_usage_model_routing_mode,
        "reason_for_premium_model": model_usage_reason_for_premium_model,
    }
    log_model_usage(
        root=model_usage_log_root,
        run_id=run_id,
        cron_id=cron_id,
        run_origin="scheduled" if scheduled else "manual",
        artifact_path=run_state_path,
        triggered_at_utc=now.astimezone(timezone.utc),
        **{k: v for k, v in routing_overrides.items() if v is not None},
    )


def _redacted_strings(node: Any) -> Any:
    """Copy of ``node`` with every string value AND every string key passed
    through :func:`near_close_dca._redacted_diagnostic_text`; containers
    recurse, other scalars pass through unchanged. Keys too: a broker payload
    can carry the identifier in a key (``{"account_number=<n>": ...}``), and
    scrubbing values alone left it in ``raw/orders_after_place.json`` (Copilot
    review, PR #1394)."""
    if isinstance(node, str):
        return _redacted_diagnostic_text(node)
    if isinstance(node, dict):
        return {
            (_redacted_diagnostic_text(key) if isinstance(key, str) else key): _redacted_strings(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_redacted_strings(value) for value in node]
    return node


def run_pipeline(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    *,
    artifacts_dir: str | Path,
    run_label: str = "daily",
    scheduled: bool = True,
    generate_full_reports: bool = False,
    ignore_trend_filter: bool = False,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None,
    prior_allocation_state_root: Optional[str | Path] = None,
    deployment_history_root: Optional[str | Path] = None,
    classification_state_root: Optional[str | Path] = None,
    config_dir: Optional[str | Path] = None,
    raw_connector_inputs: Optional[Mapping[str, Any]] = None,
    required_raw_filenames: Optional[frozenset] = None,
    model_usage_log_root: Optional[str | Path] = None,
    model_usage_model_requested: Optional[str] = None,
    model_usage_model_actual: Optional[str] = None,
    model_usage_model_routing_mode: Optional[str] = None,
    model_usage_reason_for_premium_model: Optional[str] = None,
    stream_name: str = AFTERNOON_STREAM,
    cron_id: str = AFTERNOON_LIVE_CRON_ID,
    classification_overrides: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    preflight_gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Thin wrapper around ``_run_pipeline_impl``: same signature plus the
    opt-in ``model_usage_log_root`` (see scripts/model_usage_log.py,
    config/scheduled_tasks.json -> model_usage_log). None (the default)
    skips the audit-log append entirely, matching every other opt-in root in
    this function's signature (e.g. ``classification_state_root``).

    Wrapping rather than inlining the append into ``_run_pipeline_impl``
    means the append happens exactly once per call regardless of which of
    that function's several early-return paths was taken, without touching
    any of its trading/safety logic.

    The ``model_usage_model_*``/``model_usage_reason_for_premium_model``
    kwargs override ``log_model_usage``'s cost-conscious defaults -- this
    function has no visibility into which model/agent actually drove the
    run, so they stay unset unless the scheduler invoking this call
    explicitly knows it used a permitted premium escalation and passes them.

    The whole call runs under ``dca_alerts.guarded_run``: on a scheduled
    non-dry-run invocation, any escaping exception is logged, alerted to the
    operator Slack webhook, and pinged as a failed heartbeat before
    re-raising, and a completed run pings the afternoon success heartbeat
    (dead-man's switch for the missed-fire failure class). Demo/dry-run and
    unscheduled calls only get a log handler ensured.

    ``preflight_gate`` (``run_slot``'s morning-preflight diagnostic dict, see
    ``morning_preflight_eligible_tickers``) is forwarded to
    ``_run_pipeline_impl``, which checks it from INSIDE this guarded scope --
    after its own calendar gate, so a weekend/holiday absence of the same-day
    09:45 artifact is never misdiagnosed as a failed prerequisite -- rather
    than ``run_slot`` short-circuiting before ``run_pipeline`` is even
    called. That earlier short-circuit bypassed this function's heartbeat
    and model-usage-log append entirely, letting the dead-man's switch read
    a blocked-but-written slot as one that never fired at all (Codex
    review: "Route the preflight-blocked result through guarded_run").
    """

    # Snapshotted BEFORE _invoke() runs, so the probe passed to guarded_run
    # below can tell this invocation's own write from a stale run_state.json
    # a retry's directory reuse left behind from an earlier attempt -- codex
    # review discussion_r3660858013.
    run_artifact_written = artifact_freshness_probe(
        Path(artifacts_dir) / RUN_STATE_FILENAME
    )

    def _invoke() -> Dict[str, Any]:
        result = _run_pipeline_impl(
            now,
            config,
            state,
            finance_data,
            robinhood_data,
            broker,
            artifacts_dir=artifacts_dir,
            run_label=run_label,
            scheduled=scheduled,
            generate_full_reports=generate_full_reports,
            ignore_trend_filter=ignore_trend_filter,
            prior_same_day_allocations=prior_same_day_allocations,
            prior_allocation_state_root=prior_allocation_state_root,
            deployment_history_root=deployment_history_root,
            classification_state_root=classification_state_root,
            config_dir=config_dir,
            raw_connector_inputs=raw_connector_inputs,
            required_raw_filenames=required_raw_filenames,
            classification_overrides=classification_overrides,
            correlation_id=correlation_id,
            stream=stream_name,
            preflight_gate=preflight_gate,
        )
        _log_pipeline_model_usage(
            result,
            now,
            scheduled,
            cron_id,
            model_usage_log_root,
            model_usage_model_requested,
            model_usage_model_actual,
            model_usage_model_routing_mode,
            model_usage_reason_for_premium_model,
        )
        return result

    return guarded_run(
        stream_name,
        _invoke,
        GuardedRunConfig(
            enabled=scheduled and not config.dry_run,
            context=f"artifacts_dir={artifacts_dir}",
            # Keep the failed-start marker under the same custom base as a
            # configured model_usage_log_root, so a non-default artifacts root
            # doesn't split the writer and the report's reader across two
            # different attempt_log locations -- codex review
            # discussion_r3658373966.
            attempt_log_root=attempt_log_root_sibling_to(model_usage_log_root),
            # _run_pipeline_impl persists run_state.json directly under
            # artifacts_dir (see the `out = Path(artifacts_dir)` writers above)
            # before generate_full_reports=True's later report-generation phase
            # runs; probing that path lets guarded_run tell a crash AFTER the run
            # completed its work from a genuine failed start, instead of always
            # recording the latter -- codex review discussion_r3660464501. The
            # probe is mtime-snapshotted (not existence-only) so a retry reusing
            # this slot's directory after an earlier invocation already wrote it
            # is not credited with a write it never made itself -- codex review
            # discussion_r3660858013.
            run_artifact_written=run_artifact_written,
            # The raw-persistence failure path and the preflight-blocked path
            # both report their failure as a returned result (each with its own
            # targeted alert) instead of raising; either must ping a FAILED
            # heartbeat, not clear the dead-man's switch (Codex review: "Route
            # the preflight-blocked result through guarded_run"). A directory-
            # ownership conflict (an overlapping invocation or a different
            # account claimed the shared artifacts directory) is the same
            # shape of silent failure: _run_pipeline_impl returns a normal-
            # looking result with directory_ownership_verified=False and
            # otherwise no error field this predicate already checks, so
            # without this clause a scheduled live run whose artifact
            # publication was rejected -- any live placements possibly
            # surviving only in a manual-reconciliation sidecar -- still
            # cleared the success heartbeat and sent no failure alert (Codex
            # review: "Fail the heartbeat on directory ownership conflicts").
            result_ok=_pipeline_result_ok,
        ),
    )


def prior_slots(slot: str) -> tuple[str, ...]:
    """Canonical slots that occur before ``slot`` in the live roster."""
    normalized = slot.strip().upper()
    ordered = tuple(SLOT_MAP)
    try:
        index = ordered.index(normalized)
    except ValueError as exc:
        raise UnsupportedSlotError(f"unsupported slot label: {slot!r}") from exc
    return ordered[:index]


def run_slot(
    slot: str,
    *,
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    artifacts_dir: str | Path,
    generate_full_reports: bool = False,
    ignore_trend_filter: bool = False,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None,
    prior_allocation_state_root: Optional[str | Path] = None,
    deployment_history_root: Optional[str | Path] = None,
    classification_state_root: Optional[str | Path] = None,
    config_dir: Optional[str | Path] = None,
    raw_connector_inputs: Optional[Mapping[str, Any]] = None,
    required_raw_filenames: Optional[frozenset] = None,
    model_usage_log_root: Optional[str | Path] = None,
    model_usage_model_requested: Optional[str] = None,
    model_usage_model_actual: Optional[str] = None,
    model_usage_model_routing_mode: Optional[str] = None,
    model_usage_reason_for_premium_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one canonical roster slot with slot-scoped identity and guardrails."""
    normalized_slot = slot.strip().upper()
    if normalized_slot in DISABLED_SLOTS:
        raise UnsupportedSlotError(
            f"slot {normalized_slot} is disabled: prior-allocation chain is not modeled for it"
        )
    if normalized_slot not in SLOT_MAP:
        raise UnsupportedSlotError(f"unsupported slot label: {slot!r}")
    stream_name, window = SLOT_MAP[normalized_slot]
    _validate_slot_schedule(now, window)
    trading_day = now.astimezone(EASTERN).date().isoformat()
    correlation_id = build_correlation_id(trading_day, stream_name, window)
    slot_tickers = config.tickers
    preflight_gate: Optional[Dict[str, Any]] = None
    if stream_name == MORNING_STREAM:
        # The 10:30 slot admits its universe through the SAME
        # morning_preflight_eligible_tickers() MorningDCAExecutor.
        # _apply_preflight_gate() uses, so the two entry points cannot drift on
        # ticker admission. Its contract is NON-BLOCKING as of 2026-08-02 (see
        # the branch below): a missing, forbidden-root or empty same-day 9:45
        # preflight artifact degrades to the full configured universe with a
        # recorded warning, not to zero. Routed only through the generic
        # pipeline, run_slot("1030ET") once skipped the gate entirely (Codex
        # review: "Route the morning slot through its required preflight
        # gate"). allow_unpinned_account=True: this
        # call only needs the preflight root/forbidden-roots/bounded-recovery
        # fields, not an account -- the broker/account this slot actually
        # trades through is the caller-supplied ``broker`` above.
        morning_config = load_morning_config(
            config_dir=config_dir, mode=MORNING_LIVE_MODE, allow_unpinned_account=True
        )
        slot_tickers, preflight_gate = morning_preflight_eligible_tickers(
            now,
            config.tickers,
            preflight_run_state_root=morning_config.preflight_run_state_root,
            preflight_forbidden_roots=morning_config.preflight_forbidden_roots,
            preflight_bounded_recovery=morning_config.preflight_bounded_recovery,
        )
        if preflight_gate.get("warning"):
            # NON-BLOCKING as of 2026-08-02: morning_preflight_eligible_
            # tickers() no longer sets preflight_missing_block (that flag is
            # never set by anything anymore) -- a missing, forbidden-root, or
            # valid-but-empty same-day preflight artifact now degrades to the
            # full configured universe and reports it via this "warning"
            # field instead. Log it here so it is not silently indistinguishable
            # from a normal evaluation, and it is also persisted onto
            # artifacts["preflight_gate"] by run_pipeline() below for durable
            # observability (Codex review: "Emit the advisory preflight
            # warning from run_slot").
            logger.warning("1030ET preflight gate: %s", preflight_gate["warning"])
        elif preflight_gate.get("gated_out_tickers"):
            logger.info(
                "1030ET preflight gate excluded tickers not admitted by the 9:45 pre-flight: %s",
                preflight_gate["gated_out_tickers"],
            )
    slot_config = replace(
        config,
        tickers=slot_tickers,
        # Human decision (2026-07-30, formerly documented in the now-removed GATE5
        # proposal doc's #4 item 2): guarded
        # top-up, not "first slot wins". False (rather than omitting the
        # override, which would leave whatever config/budgets.json's own
        # order_rules.one_order_per_ticker_per_day says) lets
        # near_close_dca._process_buy_candidate's existing, already-tested
        # strictly-lower-price top-up guard run for these slots specifically,
        # without changing the flag's default for any other caller of
        # StrategyConfig. This was previously forced True here -- see the
        # (now superseded) rationale on the old
        # test_run_slot_enforces_one_order_per_ticker_per_day -- because the
        # prior-allocation chain isn't modeled for the four DISABLED_SLOTS;
        # it stays correct for the three canonical slots because each one's
        # prior allocation is auto-discovered from the other two's own state
        # roots (run_pipeline's scheduled-run discovery), which the guard
        # already understands.
        one_order_per_ticker_per_day=False,
        # Anchor signal evaluation to THIS slot's own decision time. Left at the
        # bundled config's 15:30 default, a 10:30/12:30 invocation would query
        # and anchor its lookback/downtrend window against afternoon bars,
        # potentially against a future timestamp the provider rejects, or
        # against the same latest bar for both ends (making the 60-minute
        # downtrend check trivially false and silently suppressing buys) --
        # Codex review, discussion_r3679391268: "Anchor signal evaluation to
        # the requested slot".
        decision_time_et=_slot_time_et(window),
    )
    result = run_pipeline(
        now,
        slot_config,
        state,
        finance_data,
        robinhood_data,
        broker,
        artifacts_dir=artifacts_dir,
        run_label=normalized_slot.lower(),
        scheduled=True,
        generate_full_reports=generate_full_reports,
        ignore_trend_filter=ignore_trend_filter,
        prior_same_day_allocations=prior_same_day_allocations,
        prior_allocation_state_root=prior_allocation_state_root,
        deployment_history_root=deployment_history_root,
        classification_state_root=classification_state_root,
        config_dir=config_dir,
        raw_connector_inputs=raw_connector_inputs,
        required_raw_filenames=required_raw_filenames,
        model_usage_log_root=model_usage_log_root,
        model_usage_model_requested=model_usage_model_requested,
        model_usage_model_actual=model_usage_model_actual,
        model_usage_model_routing_mode=model_usage_model_routing_mode,
        model_usage_reason_for_premium_model=model_usage_reason_for_premium_model,
        stream_name=stream_name,
        # Antigravity review: "run_slot() does not pass cron_id to
        # run_pipeline()" -- left at the default, log_model_usage() recorded
        # every 1030ET/1230ET slot under the afternoon task id.
        cron_id=_SLOT_CRON_ID[stream_name],
        classification_overrides=_slot_classification_overrides(
            stream_name, window, trading_day
        ),
        correlation_id=correlation_id,
        preflight_gate=preflight_gate,
    )
    return {**result, "correlation_id": correlation_id}


def _final_buy_trend_override_window_start(now: datetime) -> time:
    """Lower bound of the final-buy trend-override window for ``now``'s ET date.

    Pre-retime dates (before ``CANONICAL_RETIME_DATE_YYYYMMDD``) keep the
    legacy 15:15 ET bound so a legacy 3:20 PM run stays covered. On/after the
    retime, the bound narrows to the canonical classification window start
    (15:25 ET) so a delayed/replayed or misfired scheduled invocation between
    15:15 and 15:24 ET does not skip the downtrend check outside the window
    the manifest actually permits (15:30-15:32 ET starts) -- codex review
    discussion_r3657271692.
    """
    now_et = now.astimezone(EASTERN)
    window_start_et = (
        LEGACY_SCHEDULED_WINDOW_START_ET
        if now_et.strftime("%Y%m%d") < CANONICAL_RETIME_DATE_YYYYMMDD
        else SCHEDULED_WINDOW_START_ET
    )
    hh, mm = (int(part) for part in window_start_et.split(":"))
    return time(hh, mm)



def _persist_raw_inputs(
    scheduled: bool,
    now: datetime,
    config: StrategyConfig,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    artifacts_dir: str | Path,
    raw_connector_inputs: Optional[Mapping[str, Any]],
    required_raw_filenames: Optional[frozenset[str]],
    run_label: str,
    state: StrategyState,
    classification_overrides: Optional[Dict[str, Any]],
    correlation_id: Optional[str],
    classification_state_root: Optional[str | Path],
) -> Tuple[Optional[Dict[str, object]], MarketDataProvider, MarketDataProvider, Optional[Dict[str, Any]]]:
    """Persist raw inputs for a scheduled live run. Returns (persistence_state, finance_data, robinhood_data, failure_result)."""
    if not scheduled:
        return None, finance_data, robinhood_data, None

    trading_day_et = now.astimezone(EASTERN).date()
    trading_day_raw = trading_day_et.isoformat()

    # Only persist raw inputs on approved trading days; unapproved days let
    # run_daily_compact write the normal blocked stub without side effects.
    if trading_day_et not in config.approved_test_dates:
        return None, finance_data, robinhood_data, None

    # Always save raw inputs directly under artifacts_dir/raw so the raw
    # directory is co-located with run_state.json regardless of layout.
    raw_dir = Path(artifacts_dir) / "raw"
    # New raw inputs are staged here first and only swapped into raw_dir
    # once every file has saved and normalized cleanly. A same-day retry
    # that fails partway through must not destroy a prior successful
    # attempt's raw/ files -- those are the audit/replay record for any
    # live orders that attempt already placed.
    staging_dir = Path(artifacts_dir) / "raw.tmp"

    # Live runs (dry_run=False) require non-empty raw_connector_inputs.
    # Dry-run callers that have not yet been updated may omit entirely.
    # Both None and empty-dict on live runs go through the fail-closed path
    # (inside the try) so a durable no-order run_state is always written.
    _needs_raw = raw_connector_inputs is not None or not config.dry_run

    if not _needs_raw:
        return None, finance_data, robinhood_data, None

    # Captured BEFORE any attempt: _build_frozen_providers_from_raw
    # WRAPS the providers it is given, so a retry must start from the
    # originals. Re-freezing an already-frozen provider would stack a
    # second replay layer over the first.
    _base_finance_data = finance_data
    _base_robinhood_data = robinhood_data
    # Set only when a recovery retry is actually ATTEMPTED, so the
    # escalation state below can distinguish "retried once and still
    # failed" from "failed deterministically, no retry warranted".
    _recovery_trigger: Optional[str] = None

    def _persist_raw_and_freeze() -> Tuple[Dict[str, Any], Any, Any]:
        """Stage, normalize and swap ``raw/``, then freeze providers from it.

        Extracted so the automatic-failure-recovery path below can run
        it a second time. It is idempotent by construction: it rmtree's
        any leftover staging directory on entry, restores ``raw.bak``
        from an interrupted prior swap, and reads only the immutable
        ``raw_connector_inputs`` mapping plus the pre-attempt providers
        captured above -- so a second call neither sees nor leaves
        state from the first.
        """
        if raw_connector_inputs is None or (not config.dry_run and not raw_connector_inputs):
            raise RuntimeError(
                "scheduled live run requires non-empty raw_connector_inputs; "
                "omitting or passing an empty mapping on a live run allows orders "
                "without persisting connector responses first"
            )
        if required_raw_filenames and not config.dry_run:
            missing = required_raw_filenames - set(raw_connector_inputs.keys())
            if missing:
                raise RuntimeError(
                    f"scheduled live run: required raw inputs absent from "
                    f"raw_connector_inputs: {sorted(missing)}"
                )

        # Stage this attempt's raw saves in a separate directory so a
        # failure below never touches the existing raw_dir.
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        for filename, payload in raw_connector_inputs.items():
            path = save_raw_input(trading_day_raw, filename, payload, raw_dir=staging_dir)
            written = json.loads(path.read_bytes())
            if "normalize_error" in written:
                raise RuntimeError(
                    f"normalization failed for {filename!r}: {written['normalize_error']}"
                )

        # Every file staged and normalized cleanly -- swap the staging
        # directory into place as this run's raw/, replacing whatever
        # a prior attempt left there. Preserve any post-placement
        # order history from the prior attempt first: if that run
        # placed an order and this retry is deduplicated (broker
        # dedup means no new order), the fill snapshot must survive
        # for slippage reporting and is not re-produced this run.
        #
        # Held under the same .corporate_actions.lock a concurrent
        # retry's frozen-provider corporate-action lookup takes
        # (scripts.file_lock.cross_process_lock, sited in raw_dir's
        # PARENT so it survives this very swap): without it, this
        # copy-and-swap can interleave with another retry's
        # in-flight query/persist -- e.g. this swap copies raw/'s
        # corporate_action_SYMBOL.json into staging/ before that
        # retry finishes writing its own newer evidence there, or
        # this swap discards raw.bak (with the evidence that retry's
        # own decision was actually based on) out from under it --
        # reopening the exact persist-before-compute audit mismatch
        # the lock exists to prevent (Codex review: "Serialize raw
        # swaps with the action lock").
        from scripts.file_lock import cross_process_lock

        with cross_process_lock(raw_dir.parent / ".corporate_actions.lock"):
            if raw_dir.exists():
                _prior_oap = raw_dir / "orders_after_place.json"
                _staged_oap = staging_dir / "orders_after_place.json"
                if _prior_oap.exists() and not _staged_oap.exists():
                    shutil.copy2(_prior_oap, _staged_oap)
                # Same reasoning, for a durable rearm latch's live
                # corporate-action evidence: it is written DURING
                # compute (after this staging swap, once run_daily_
                # compact actually evaluates the ticker), not from
                # raw_connector_inputs, so a same-artifacts-dir retry
                # would otherwise silently drop the prior attempt's
                # file the instant this swap replaces raw/ -- taking
                # with it the very evidence that authorized whatever
                # order that attempt already placed, or letting a
                # fresh no-op query overwrite a real confirmed factor
                # (Codex review: "Preserve action evidence across raw
                # snapshot swaps"). This run's own compute step below
                # still overwrites any of these with fresher evidence
                # once it actually re-queries -- this only prevents
                # the retry from starting with LESS evidence than the
                # prior attempt already had.
                for _prior_action in raw_dir.glob("corporate_action_*.json"):
                    _staged_action = staging_dir / _prior_action.name
                    if _prior_action.is_file() and not _staged_action.exists():
                        shutil.copy2(_prior_action, _staged_action)

            # Keep a backup until the rename succeeds so a filesystem error
            # can't destroy the prior audit record: rename raw/ → raw.bak,
            # rename staging/ → raw/, then remove raw.bak. On any failure,
            # restore raw.bak → raw/.
            #
            # A stale raw.bak can be left by a prior interrupted swap:
            # - If raw/ also exists the prior swap completed but cleanup did
            #   not; raw.bak is stale -- remove it so the rename below can
            #   proceed.
            # - If raw/ is absent the staging rename failed last time; raw.bak
            #   holds the real prior data -- restore it before proceeding.
            raw_backup = raw_dir.with_name("raw.bak")
            if raw_backup.exists():
                if raw_dir.exists():
                    # Stale backup from an interrupted prior swap: raw/ was
                    # already restored so the backup is redundant. Remove it
                    # explicitly (no ignore_errors) so a failure surfaces here
                    # rather than silently causing the rename below to fail.
                    shutil.rmtree(raw_backup)
                else:
                    raw_backup.rename(raw_dir)

            _had_backup = False
            if raw_dir.exists():
                raw_dir.rename(raw_backup)
                _had_backup = True
                try:
                    staging_dir.rename(raw_dir)
                except Exception:
                    raw_backup.rename(raw_dir)
                    raise
                # raw_backup is deliberately NOT removed yet: it is the
                # prior successful run's raw audit/replay record, and
                # this swap alone does not guarantee this attempt's
                # raw/ is actually usable -- strict replay validation
                # happens next, in _build_frozen_providers_from_raw.
                # Removing the backup here would let a failure in that
                # validation (or in building the persistence state)
                # permanently destroy the prior good snapshot even
                # though this attempt never produced a usable
                # replacement (Codex review: "Keep the prior raw
                # snapshot until replay validation succeeds").
            else:
                staging_dir.rename(raw_dir)

            # Keep validation, rollback, and backup cleanup under the
            # same lock as the swap. A concurrent corporate-action
            # lookup persists into raw/ under this lock, so releasing
            # it before rollback could delete or replace its evidence.
            try:
                _frozen_finance, _frozen_robinhood = _build_frozen_providers_from_raw(
                    _base_finance_data,
                    _base_robinhood_data,
                    raw_dir,
                    strict_finance=not config.dry_run,
                    strict_robinhood=not config.dry_run,
                )
                _persistence_state = build_raw_persistence_state(
                    trading_day_raw, raw_dir=raw_dir
                )
            except Exception:
                if _had_backup:
                    # This attempt's raw/ never became usable -- restore
                    # the prior successful run's snapshot rather than
                    # leaving this attempt's (unusable) files in place.
                    shutil.rmtree(raw_dir, ignore_errors=True)
                    raw_backup.rename(raw_dir)
                raise

            if _had_backup:
                shutil.rmtree(raw_backup, ignore_errors=True)

        return (_persistence_state, _frozen_finance, _frozen_robinhood)

    try:
        # AUTOMATIC FAILURE RECOVERY (failure_recovery_policy in
        # config/scheduled_tasks.json; docs/run_classification.md
        # #recovery-reruns). Exactly ONE in-process retry, and only for
        # a failure class a retry can plausibly clear -- see
        # _recovery_trigger_for for why that is filesystem/IO faults
        # only and why re-fetching connector data is out of scope here.
        # Nothing is loosened for the retry: it re-runs the identical
        # persist/normalize/swap/freeze sequence, and every downstream
        # signal, budget, spread, confidence, top-up and circuit-breaker
        # check still runs unchanged afterward. On success the run
        # continues in the SAME run directory and is stamped
        # recovery_rerun=true; it is never reclassified as manual.
        try:
            (raw_persistence_state, finance_data, robinhood_data) = _persist_raw_and_freeze()
        except Exception as _first_exc:  # noqa: BLE001
            _first_trigger = _recovery_trigger_for(_first_exc)
            if _first_trigger is None or _first_trigger not in RECOVERY_RETRY_TRIGGERS:
                # Deterministic on the same in-memory payload (or not a
                # recovery trigger at all): a second identical attempt
                # would fail identically, so escalate now rather than
                # spend the single permitted attempt.
                raise
            _recovery_trigger = _first_trigger
            logger.warning(
                "raw-persistence attempt 1 failed (%s: %s); running the single "
                "permitted automatic recovery retry in the same run directory",
                _first_trigger,
                _first_exc,
            )
            (raw_persistence_state, finance_data, robinhood_data) = _persist_raw_and_freeze()
            # Reached only when the retry itself succeeded, so both
            # flags are truthful: raw/ was rewritten before any compute,
            # and the providers below read only from those files.
            raw_persistence_state.update(
                build_recovery_state(
                    _first_trigger,
                    persisted_raw_first=True,
                    recomputed_from_persisted=True,
                )
            )
            logger.warning(
                "automatic recovery retry SUCCEEDED (%s); continuing through the "
                "unchanged guarded path with recovery_rerun=true",
                _first_trigger,
            )
    except Exception as exc:  # noqa: BLE001
        # Remove only the staging directory; raw/ has been restored
        # from raw.bak (if a prior attempt populated it) so its audit
        # trail survives this failed retry.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        out = Path(artifacts_dir)
        out.mkdir(parents=True, exist_ok=True)

        # Remove stale derived/report artifacts from any prior
        # attempt so they are not mistaken for this failed run's output.
        # Include drift reports: a prior clean run may have written
        # drift_report.json/md; leaving them next to a new raw-persistence-
        # failure run_state would mislead operators about the current status.
        # Do NOT delete orders.csv or buy_candidates.csv: a retry in the
        # same directory may follow a previously-successful attempt whose
        # real fill records must survive for reporting and cap accounting.
        for _stale in (
            "signals.csv",
            "latest_run_summary.json",
            "slippage_and_coverage_report.md",
            "slippage_ranked.csv",
            "missing_fills_report.csv",
            "notion_latest_run_compact.md",
            "notion_latest_run_payload.json",
            "drift_report.json",
            "drift_report.md",
        ):
            (out / _stale).unlink(missing_ok=True)

        failure_state = raw_persistence_failure_run_state(
            trading_day_raw,
            str(exc),
            raw_dir=raw_dir,
            broker=config.broker.value,
            triggered_at_utc=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            generated_at=now.astimezone(EASTERN).strftime("%Y-%m-%dT%H:%M:%S"),
            budget={
                "budget_tier": state.current_tier().name,
                "daily_cap_usd": state.current_tier().daily_cap_usd,
                "weekly_cap_usd": state.effective_weekly_cap(),
            },
            classification_overrides=classification_overrides,
            correlation_id=correlation_id,
        )

        # Record the escalation when the single permitted recovery
        # retry was actually ATTEMPTED and still failed. Stamped only
        # in that case: a deterministic first failure escalates without
        # retrying (see the classifier above), and claiming
        # recovery_rerun/attempt=1 there would misreport a config or
        # payload fault as an exhausted transient recovery. The block
        # matches recovery_escalation_run_state()'s shape, which
        # run_state.schema.json requires whenever recovery_rerun=true;
        # it is merged into the existing failure state rather than
        # replacing it so the hardened prior-order preservation below
        # is kept intact.
        if _recovery_trigger is not None:
            failure_state["recovery_rerun"] = True
            failure_state["recovery"] = {
                "trigger_reason": _recovery_trigger,
                "attempt": 1,
                "persisted_raw_first": False,
                "recomputed_from_persisted": False,
                "escalated": True,
                "blocking_reason": str(exc),
            }
            # raw_persistence_failure_run_state() above always
            # stamps error_class=ERROR_CLASS_MISSING_RAW_INPUTS,
            # the generic first-attempt classification. An
            # exhausted recovery is a DIFFERENT failure mode --
            # the recovery contract's own recovery_escalation_run_state()
            # classifies it as ERROR_CLASS_RECOVERY_FAILURE -- so
            # without overriding it here, reporting keyed by
            # error_class misgroups an exhausted recovery as an
            # ordinary raw-persistence block on the very first
            # attempt (Codex review: "Stamp exhausted recovery
            # with recovery failure").
            failure_state["error_class"] = ERROR_CLASS_RECOVERY_FAILURE
            logger.error(
                "automatic recovery retry ALSO FAILED (%s: %s); escalating with "
                "a durable scheduled run_state rather than retrying further",
                _recovery_trigger,
                exc,
            )

        # Preserve orders.csv from a prior successful run so that
        # load_prior_same_day_allocations() can still find the fills
        # even when this retry's raw-persistence fails.
        _prior_orders_csv = out / "orders.csv"
        if _prior_orders_csv.exists():
            failure_state["artifacts"] = {"orders_csv_path": str(_prior_orders_csv)}

        # Carry forward the SLOT's prior order evidence BEFORE the state
        # is serialized. This state overwrites the immutable-path
        # run_state.json, and the contract writer regenerates
        # submitted_orders.csv from its buy_candidates, so a retry after
        # a successful attempt would otherwise erase real accepted orders
        # from the canonical scheduled record. Mutating after the write
        # left the on-disk state without them while the companions had
        # them, which is worse than either (Codex review).
        _prior_rows = _prior_submitted_order_rows(out)
        # Every prior row is kept as the slot's record; only the ones
        # the broker actually took are COUNTED. A confirmation-required
        # attempt belongs in the record and is not a submission
        # (Codex review).
        _accepted = _accepted_order_rows(_prior_rows)
        if _prior_rows:
            failure_state["buy_candidates"] = _prior_rows
            failure_state["placement_evidence_incomplete"] = True
            failure_state["orders_submitted_count"] = len(_accepted)
            _counts = failure_state.get("counts")
            _counts_dict = dict(_counts) if isinstance(_counts, Mapping) else {}
            _counts_dict.update({
                "submitted": len(_accepted),
                "buy_candidates": len(_prior_rows),
            })
            failure_state["counts"] = _counts_dict

        if _accepted:
            # The denial STAYS. This run failed, so it is not
            # evidence the slot executed -- and its accepted orders
            # are still attributable to the slot, because
            # is_scheduled_state rejects a denied state only when it
            # has no order to attribute. Clearing the marker here to
            # keep the fills reportable was the same seam handled by
            # hand in one place; it now lives in the predicate.
            # Keep the aggregate fields consistent with the carried
            # count. Leaving order_submit_status='not_submitted_error'
            # and the startup outcome beside a non-zero count makes the
            # state claim simultaneously that orders were and were not
            # submitted, so status-based reliability reporting reads a
            # partially submitted slot as a pre-submission failure
            # (Codex review). It is still a failure, just an honest one.
            failure_state["order_submit_status"] = ORDER_SUBMIT_STATUS_SUBMITTED
            failure_state["run_classification_outcome"] = RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION

        run_state_path = out / "run_state.json"
        _failure_state_text = json.dumps(failure_state, indent=2) + "\n"
        atomic_write_text(run_state_path, _failure_state_text)

        # A stale signals.csv from a prior attempt was deleted above so
        # it is not mistaken for this run's output, but the scheduled
        # contract requires all four root-level files on EVERY outcome.
        # Write a fresh header-only one: honest (no signal was computed)
        # and contract-valid, where an absent file would publish valid
        # latest scheduled evidence with a required file missing (Codex
        # review). detect_drift still finds an empty expected set, so the
        # direct raw-persistence alert below remains the operator signal.
        write_signals_csv(out, [], fieldnames=("ticker",))
        # Route this fail-closed raw-persistence outcome through the same
        # scheduled artifact contract as the normal finalizer: the
        # header-only submitted_orders.csv and the mirrored canonical
        # run_summary.json, overwriting any stale copies from a prior
        # attempt. Without this the early return bypassed the contract
        # entirely while still updating the scheduled pointer. A contract
        # breach here (persisted as a blocked/invalid state) also blocks
        # the pointer, so it is not recorded as valid scheduled evidence.
        # known_run_state_bytes binds the validated snapshot to the exact
        # bytes this writer just put on disk -- a plain re-read here would
        # leave a window after this write releases its lock and before the
        # read runs, during which an overlapping retry could replace
        # run_state.json and have ITS bytes silently adopted as this
        # invocation's own snapshot (Codex review: "Bind the snapshot to
        # this invocation's state").
        contract_ok = bool(
            _write_and_validate_scheduled_contract(
                failure_state,
                out,
                known_run_state_bytes=_failure_state_text.encode("utf-8"),
            )
        )
        if classification_state_root is not None and contract_ok:
            update_latest_scheduled_run_pointer(
                failure_state,
                run_state_path,
                base=Path(classification_state_root),
            )
        elif classification_state_root is not None:
            logger.error(
                "Not updating latest_scheduled_run.json: raw-persistence "
                "failure-state artifact contract failed."
            )

        # signals.csv carries only its header, so detect_drift() finds an
        # empty expected set and returns drift_detected=False:
        # _run_drift_check() would log nothing and send no Slack alert.
        # Emit the raw-persistence alert directly instead so operators
        # are paged on the documented missing-raw failure mode.
        failure_result: Dict[str, Any] = {
            "run_label": run_label,
            "scheduled": scheduled,
            "compact": True,
            "blocked": False,
            "raw_persistence_error": str(exc),
            "artifacts_dir": str(out),
            "run_state_path": str(run_state_path),
        }

        try:
            from scripts.drift_detection import (
                CAUSE_RAW_MISSING,
                DEFAULT_SLACK_WEBHOOK_ENV,
                DriftReport,
                send_slack_alert,
            )

            _fail_report = DriftReport(
                run_dir=str(out),
                drift_detected=True,
                affected_symbols=[],
                run_suspected_cause=CAUSE_RAW_MISSING,
                recommended_action=(
                    "Raw input persistence failed before signal computation; no orders "
                    "were placed. Re-run the live task after confirming connector "
                    "responses are available and raw/ can be written."
                ),
                signals_present=False,
                orders_present=False,
                submitted_orders_empty=True,
                raw_dir_present=False,
                expected_symbol_count=0,
                submitted_symbol_count=0,
                notes=[f"raw_persistence_error: {exc}"],
            )
            logger.warning(
                "RAW-PERSISTENCE FAILURE in %s — no signals computed, no orders placed: %s",
                out,
                exc,
            )
            atomic_write_text(
                out / "drift_report.json",
                _fail_report.to_json(compact=False),
            )
            atomic_write_text(
                out / "drift_report.md", _fail_report.to_markdown()
            )
            if scheduled and not config.dry_run:
                send_slack_alert(_fail_report, env_var=DEFAULT_SLACK_WEBHOOK_ENV)
            failure_result["drift_report"] = _fail_report.to_dict()
        except Exception as _alert_exc:  # pragma: no cover
            logger.warning("raw-persistence failure alert failed: %s", _alert_exc)

        return None, finance_data, robinhood_data, failure_result

    return raw_persistence_state, finance_data, robinhood_data, None


def _resolve_prior_allocations(
    now: datetime,
    scheduled: bool,
    config: StrategyConfig,
    artifacts_dir: str | Path,
    config_dir: Optional[str | Path],
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]],
    prior_allocation_state_root: Optional[str | Path],
) -> Tuple[float, bool, Optional[Dict[str, PriorSameDayAllocation]]]:
    """Resolve same-day and earlier-week allocations."""
    earlier_week_notional_usd = 0.0
    earlier_week_scan_had_failures = False
    if scheduled and prior_same_day_allocations is None:
        trading_day = now.astimezone(EASTERN).date()
        if prior_allocation_state_root is None:
            # Earlier-weekday cross-stream spend (Codex review: "Include
            # prior-week sibling spend in the afternoon ledger") -- only
            # meaningful alongside the auto-derived cross-stream roots this
            # branch already resolves; an explicit prior_allocation_state_root
            # override (the else branch below) scans only the tree the
            # caller named and must stay isolated to it, same as
            # _compute_auto_derived_prior_allocations already does.
            (
                earlier_week_notional_usd,
                earlier_week_scan_had_failures,
            ) = _compute_earlier_week_cross_stream_notional(
                now, trading_day, artifacts_dir, config_dir,
                max_per_order_usd=config.max_per_order_usd,
            )
            # Auto-derive the afternoon task root and fold in the morning/midday
            # stream roots (incl. manual override reruns), so morning, midday,
            # and manual override same-day fills all offset the afternoon buy.
            # An explicit prior_allocation_state_root overrides all of this and
            # scans only the tree the caller named (see the else branch below).
            # Accumulates across BOTH this call and the bounded reload below
            # (a fresh list is not needed per pass -- only truthiness is
            # checked). A same-day unreconstructable submission's estimated
            # amount is not proof against whatever ceiling actually governed
            # the earlier same-day run that placed it, so any entry here
            # folds into earlier_week_scan_had_failures below to force the
            # SAME global worst-case-ceiling fail-closed the earlier-week
            # scan already applies for the analogous case (Codex review:
            # "Fail same-day unknown submission amounts closed globally").
            _first_pass_unreconstructable_sink: List[str] = []
            # Populated only if the bounded reload below actually runs;
            # stays empty otherwise so the final union computed after this
            # whole branch can always reference it unconditionally.
            _reload_unreconstructable_sink: List[str] = []
            first_pass, scan_had_failures = _compute_auto_derived_prior_allocations(
                now, trading_day, artifacts_dir, config_dir, max_per_order_usd=config.max_per_order_usd,
                unreconstructable_sink=_first_pass_unreconstructable_sink,
            )
            # Default/final answer when no reload ends up running below: the
            # first pass IS this run's only evidence, so its own sink is
            # unconditionally final. Overwritten with a more precise,
            # merge-aware union only inside the reload branch, where a
            # ticker's FINAL retained allocation is meaningful to check.
            _same_day_unreconstructable_sink = _first_pass_unreconstructable_sink
            unresolved = _tickers_with_unresolved_same_day_context(first_pass)
            # Trigger the bounded reload on EITHER signal, not just `unresolved`:
            # a ticker whose only same-day artifact hit a transient read/parse
            # race is entirely absent from first_pass (no amount, no price), so
            # it never shows up in `unresolved` at all -- only scan_had_failures
            # catches that case (Codex review: "Detect artifacts skipped during
            # the initial context scan").
            if unresolved or scan_had_failures:
                # Canonical rule item 13: perform one bounded same-day context
                # reload when a ticker's context cannot be resolved (a known
                # prior amount with a poisoned/unknown reference price), then
                # fail only that ticker closed -- never block the whole run.
                # A second immediate scan can pick up a sibling stream's
                # artifact that was still being flushed (or reads a completed
                # file past a transient parse race) during the first pass.
                logger.info(
                    "same-day context unresolved for %s (scan_had_failures=%s); "
                    "performing one bounded reload (canonical rule 13)",
                    unresolved,
                    scan_had_failures,
                )
                # A SEPARATE sink for this pass, not the first pass's --
                # reusing it accumulated a stale hit from the FIRST scan
                # even when the reload went on to read that exact ticker's
                # artifact cleanly and reconstruct a usable amount. The
                # merge below decides, ticker by ticker, whether the reload
                # or the first pass is the more complete answer; the
                # fail-closed signal computed after this whole branch (see
                # the final union below) follows that SAME decision by
                # checking each candidate ticker's FINAL merged allocation,
                # not either pass's sink alone (Codex review: "Clear
                # recovered unknown-notional signals after reload"; "Preserve
                # uncertainty when the merge keeps first-pass evidence" -- a
                # ticker unreconstructable only on the first pass, whose
                # artifact then simply disappeared from this reload rather
                # than resolving, is retained VERBATIM from first_pass by the
                # merge below and is exactly as uncertain as before; using
                # only this reload's own sink lost that ticker entirely).
                second_pass, second_scan_had_failures = (
                    _compute_auto_derived_prior_allocations(
                        now, trading_day, artifacts_dir, config_dir, max_per_order_usd=config.max_per_order_usd,
                        unreconstructable_sink=_reload_unreconstructable_sink,
                    )
                )
                # Merge by COMPLETENESS rather than replacing wholesale or
                # trusting the reload just because a ticker was unresolved:
                # amount_usd is the sum across every contributing artifact
                # for that ticker, so whichever pass aggregated the LARGER
                # amount read strictly more of that ticker's artifacts and
                # is the more complete answer -- whether or not its
                # reference price happens to be resolved (an unknown-price
                # contributor can raise the amount while correctly
                # poisoning the reference, which is itself the safe,
                # conservative outcome). A SMALLER second-pass amount means
                # the reload lost an artifact the first pass had -- e.g. an
                # unconfirmed submission that made the first pass
                # conservatively unresolved vanished from the reload, which
                # must not be read as "resolved," only as "incomplete
                # again" (Codex review: "Preserve unresolved context when
                # the reload is incomplete"). Equal amounts fall back to
                # resolution: take the reload ONLY if it turned an unknown
                # price into a known one, never the reverse (Codex review:
                # "Preserve resolved ticker context across the reload"). A
                # ticker entirely absent from the first pass always takes
                # the reload's result, since there is nothing to protect
                # there.
                #
                # BUT that completeness reasoning only holds when the
                # RELOAD (second pass) itself had zero skips anywhere.
                # amount_usd alone cannot tell "the same contributor(s), now
                # resolved/more complete" apart from "each scan missed a
                # DIFFERENT real contributor" -- e.g. pass one reads only an
                # unknown-price $5 submission (a $6 filled artifact for the
                # SAME ticker fails to parse that pass), and the reload
                # reads only that $6 fill (the $5 submission fails to parse
                # THIS pass instead). $6 > $5 looks strictly more complete
                # by amount alone, but the real aggregate is $11/unknown
                # (Codex review: "Preserve uncertainty when unequal/equal
                # contributors swap"). There is no per-artifact identity
                # tracked here to rule this out directly, so the guard is
                # gated on `second_scan_had_failures` specifically, NOT on
                # whether the FIRST pass also had a skip: if the reload
                # itself is clean, it read every artifact currently on disk
                # for every ticker with zero exceptions, so its total is
                # definitionally complete regardless of what the first pass
                # missed (a first pass that skipped an artifact and a clean
                # reload that then reads it is exactly the recovery this
                # bounded reload exists to enable -- gating on the first
                # pass's flag too would refuse that legitimate recovery
                # whenever pass one had ANY unrelated skip, Codex review:
                # "Use a clean reload after a failed first scan"). Only a
                # reload that ITSELF skipped something can produce the
                # swapped-contributor false confidence above.
                _reload_verified_complete = not second_scan_had_failures
                prior_same_day_allocations = dict(first_pass)
                for ticker, alloc in second_pass.items():
                    existing = first_pass.get(ticker)
                    if existing is None:
                        if second_scan_had_failures:
                            # A ticker invisible on the first pass now shows
                            # up on the reload, but the reload itself wasn't
                            # fully clean -- a SIBLING artifact for this same
                            # ticker could be exactly what still failed to
                            # parse elsewhere in this same scan, so `alloc`
                            # may only be a PARTIAL recovery, not this
                            # ticker's whole context (Codex review: "Keep
                            # recovered tickers uncertain after an
                            # incomplete reload"). Keep the recovered amount
                            # (still more information than none) but poison
                            # the reference so the guard routes through its
                            # conservative missing_prior_price path instead
                            # of trusting a possibly-partial total as complete.
                            # reference_ts must be poisoned to None alongside
                            # reference_price -- it is documented/used as "the
                            # timestamp that established reference_price," so
                            # a non-None reference_ts paired with an unknown
                            # price is an inconsistent artifact (Copilot
                            # review: "reference_ts left intact when poisoning
                            # a recovered ticker").
                            import dataclasses as _dc

                            prior_same_day_allocations[ticker] = _dc.replace(
                                alloc, reference_price=None, reference_ts=None
                            )
                        else:
                            # The reload's own scan had zero skips anywhere,
                            # so it read everything currently on disk for
                            # this ticker -- a complete, trustworthy answer.
                            prior_same_day_allocations[ticker] = alloc
                    elif not _reload_verified_complete:
                        # An incomplete reload for a ticker present in both
                        # passes is never fully trustworthy, even when every
                        # observed field matches. A prior version of this
                        # branch trusted a matching `run_id` as proof the two
                        # passes read the SAME contributing artifact -- but
                        # `run_id` identifies only the WINNING (most recent)
                        # contributor to amount_usd, not every artifact
                        # summed into it. Two passes can coincidentally agree
                        # on amount/reference/base/run_id while each having
                        # missed a DIFFERENT non-winning, equal-dollar fill
                        # for the same ticker -- the true aggregate is higher
                        # than either pass saw, and trusting the match would
                        # let the top-up guard under-count what was already
                        # deployed and submit excess capital (Codex review:
                        # "Track all contributors before trusting an
                        # incomplete reload"). There is no per-artifact
                        # contributor identity tracked on
                        # PriorSameDayAllocation to prove read-set equality
                        # directly, so always poison here instead of trusting
                        # a field match: take the larger observed amount as a
                        # conservative floor on what was actually deployed --
                        # never smaller than either pass saw -- and force the
                        # reference unknown so the guard routes through its
                        # conservative missing_prior_price path.
                        #
                        # This is deliberately scoped to EVERY ticker present
                        # in both passes, not just the ticker(s) whose own
                        # artifact failed to parse (Codex review r3657960333:
                        # "Scope incomplete reload failures to affected
                        # tickers"). Narrower scoping isn't safely possible
                        # with the data this loader has: a single
                        # run_state.json's `buy_candidates` (and a single
                        # manual override's orders CSV rows) can name MANY
                        # tickers per file, so the one file that failed to
                        # parse this pass could, for all we know, be the file
                        # that would have added another contributor to THIS
                        # ticker too -- we cannot enumerate the ticker set of
                        # a file we failed to read. Any ticker in the active
                        # universe is therefore a candidate, not just the
                        # ticker(s) whose OWN artifact is the one visibly
                        # missing/corrupt.
                        import dataclasses as _dc

                        base = (
                            alloc
                            if alloc.amount_usd >= existing.amount_usd
                            else existing
                        )
                        prior_same_day_allocations[ticker] = _dc.replace(
                            base,
                            reference_price=None,
                            reference_ts=None,
                            notional_uncertain=(
                                existing.notional_uncertain or alloc.notional_uncertain
                            ),
                        )
                    elif alloc.amount_usd > existing.amount_usd:
                        # A clean, larger reload naturally supersedes
                        # `existing` for every field EXCEPT
                        # notional_uncertain -- the equal-amount branch
                        # below protects that flag with an order-level
                        # provenance requirement, but a bare `= alloc` here
                        # bypassed the guard entirely. A larger total does
                        # not by itself prove every order `existing`
                        # counted is still represented in it: `existing`'s
                        # own uncertain order can have vanished from the
                        # reload (e.g. a since-corrected/rotated artifact)
                        # while an unrelated, larger order was newly
                        # discovered, producing a bigger total that still
                        # never accounts for the original uncertain
                        # order's dollars (Codex review: "Preserve
                        # uncertainty when the reload amount increases").
                        # Only trust alloc's own notional_uncertain when
                        # its order set is a proven SUPERSET of
                        # existing's -- proof every order existing counted
                        # is still represented in alloc's larger total --
                        # otherwise carry existing's uncertainty forward
                        # regardless of what alloc itself reports. An
                        # empty existing.contributing_order_ids can never
                        # establish that proof, so it fails closed the
                        # same way every other branch here does.
                        #
                        # A subset check on contributing_order_ids ALONE is
                        # not enough: it only proves every KNOWN order
                        # existing counted survived into alloc, and says
                        # nothing about an id-less UNCERTAIN contributor
                        # existing also counted (one with no order_id at
                        # all never enters contributing_order_ids -- see
                        # that field's own docstring). A first scan that
                        # combined one known order with one id-less
                        # uncertain candidate, followed by a cleaner reload
                        # that swaps in a DIFFERENT known order while never
                        # re-observing the uncertain one, would otherwise
                        # satisfy the known-order subset check purely by
                        # coincidence and wrongly clear the uncertainty a
                        # still-possibly-live unidentified order deserves
                        # (Codex review, PR badMade/all-stock-dca#1168
                        # discussion r3900047294: "Track identity for
                        # id-less uncertain contributors, not just a
                        # known-ID subset check"). uncertain_contributor_ids
                        # is the same kind of finer-grained identity for
                        # THOSE contributions, so both sets must be proven
                        # subsets before trusting alloc's own flag.
                        _prior_order_ids_proven = (
                            bool(existing.contributing_order_ids)
                            or bool(existing.uncertain_contributor_ids)
                        ) and (
                            existing.contributing_order_ids <= alloc.contributing_order_ids
                        ) and (
                            existing.uncertain_contributor_ids <= alloc.uncertain_contributor_ids
                        )
                        if (
                            existing.notional_uncertain
                            and not alloc.notional_uncertain
                            and not _prior_order_ids_proven
                        ):
                            alloc = replace(alloc, notional_uncertain=True)
                        prior_same_day_allocations[ticker] = alloc
                    elif alloc.amount_usd == existing.amount_usd:
                        # A clean reload has authoritative evidence for every
                        # field it returns. In particular, an equal reconstructed
                        # amount resolves a first-pass sentinel uncertainty even
                        # when neither the reference nor base amount changed --
                        # but ONLY when the reload actually re-observed the SAME
                        # contributing order(s), not merely an equal dollar
                        # total. Two different orders can coincidentally sum to
                        # the same figure across two scans: an uncertain $3
                        # submission can disappear between passes while an
                        # unrelated $3 fill for the same ticker appears, and
                        # trusting that coincidental match would clear the
                        # worst-case cap poisoning for a submission that is
                        # still genuinely unresolved (Codex review: "Preserve
                        # uncertainty unless reload provenance matches").
                        # contributing_run_ids alone is NOT enough proof: it
                        # only records which RUN/artifact contributed, and a
                        # single mutable run_state.json can expose MULTIPLE
                        # distinct same-ticker orders under that one run_id
                        # across two scans (see load_prior_same_day_
                        # allocations' own attempt-counting comment) -- equal
                        # run_id sets can hold even when the two passes
                        # actually reconstructed two DIFFERENT orders that
                        # happen to sum to the same total (Codex review:
                        # "Match order-level provenance before clearing
                        # uncertainty"). contributing_order_ids is the finer-
                        # grained, genuinely order-level identity, so exact
                        # set equality on THAT is the provenance proof this
                        # needs -- a partial overlap only proves ONE shared
                        # order, not that the reload's total is built from
                        # the exact same set the first pass saw (a new order
                        # could have joined while a different one dropped
                        # out, coincidentally leaving the dollar total
                        # unchanged). An empty set on either side (an
                        # older/legacy artifact, or a row with no known
                        # order_id) can never establish that proof, so it
                        # always keeps the existing uncertainty too -- the
                        # same fail-closed direction every other branch here
                        # already takes.
                        #
                        # Exact equality on contributing_order_ids ALONE has
                        # the same gap the ">" branch above closes: an
                        # id-less uncertain contributor (no order_id at all)
                        # never enters contributing_order_ids, so a first
                        # scan combining one known order with one id-less
                        # uncertain candidate can coincidentally match a
                        # reload that swapped in a different known order
                        # while never re-observing the uncertain one,
                        # wrongly clearing the uncertainty (Codex review, PR
                        # badMade/all-stock-dca#1168 discussion
                        # r3900047294: "Track identity for id-less uncertain
                        # contributors, not just a known-ID subset check").
                        # uncertain_contributor_ids must ALSO match exactly
                        # -- since alloc.notional_uncertain is False here,
                        # alloc's own uncertain_contributor_ids is always
                        # empty, so this reduces to requiring existing's be
                        # empty too: the reload genuinely re-observed
                        # nothing anonymous for this ticker, not merely a
                        # coincidental known-order match.
                        if (
                            existing.notional_uncertain
                            and not alloc.notional_uncertain
                            and (existing.contributing_order_ids or existing.uncertain_contributor_ids)
                            and alloc.contributing_order_ids == existing.contributing_order_ids
                            and alloc.uncertain_contributor_ids == existing.uncertain_contributor_ids
                        ):
                            existing = replace(existing, notional_uncertain=False)
                            prior_same_day_allocations[ticker] = existing
                        # Take the reload's reference whenever it's KNOWN and
                        # DIFFERENT from the first pass's -- not just a
                        # None-to-known resolution, but also a known-to-
                        # different-known correction (e.g. a full-report
                        # CSV-only allocation reconciled between scans from a
                        # preliminary/estimated fill price to the confirmed
                        # broker price for the SAME notional). A clean reload
                        # (guaranteed by _reload_verified_complete above)
                        # reflects the current state of that -- potentially
                        # mutable -- underlying artifact, so an updated
                        # reference for an unchanged amount is a legitimate
                        # correction to trust (Codex review: "Accept updated
                        # references from a clean reload"), never the
                        # reverse: alloc.reference_price is None here always
                        # keeps existing, protecting the same
                        # already-resolved-ticker case addressed earlier in this
                        # PR (Codex review: "Preserve resolved ticker context
                        # across the reload").
                        if (
                            alloc.reference_price is not None
                            and alloc.reference_price != existing.reference_price
                        ):
                            # A bare `alloc` assignment would silently drop
                            # `existing.notional_uncertain` through a path the
                            # provenance check above never guards -- the check
                            # only protects the FIRST branch's own explicit
                            # clearing, not this metadata-replacement branch.
                            # An equal-dollar reload with disjoint provenance
                            # (still uncertain) but a genuinely new reference
                            # price would otherwise replace the whole entry
                            # with alloc's own notional_uncertain=False,
                            # letting a different $3 order (run-2) erase the
                            # cap poisoning an unresolved $3 order (run-1)
                            # still deserves, merely because run-2 happens to
                            # carry a known price (Codex review: "Preserve
                            # uncertainty through reload metadata
                            # replacement"). existing.notional_uncertain
                            # already reflects the provenance check's own
                            # verdict by this point (cleared to False only
                            # when it matched), so carrying it forward here
                            # -- unioned with alloc's own flag -- keeps that
                            # verdict intact through this replacement too.
                            prior_same_day_allocations[ticker] = replace(
                                alloc,
                                notional_uncertain=(
                                    existing.notional_uncertain or alloc.notional_uncertain
                                ),
                            )
                        elif (
                            alloc.selected_base_amount_usd
                            != existing.selected_base_amount_usd
                            or alloc.selected_base_amount_usd_at
                            != existing.selected_base_amount_usd_at
                        ):
                            # The reference price didn't change (or the reload
                            # doesn't know it), but selected_base_amount_usd is
                            # ALSO sourced from this same underlying artifact,
                            # and resolve_top_up_desired_amount_usd multiplies
                            # it directly to compute the top-up target -- a
                            # stale base corrected between scans (e.g. $2 ->
                            # $1) would otherwise let the top-up guard target
                            # double what the clean reload actually supports
                            # (Codex review: "Accept corrected allocation
                            # bases from a clean reload"). The same clean-
                            # reload trust already established for reference
                            # corrections above extends to this field.
                            import dataclasses as _dc

                            if alloc.reference_price is None:
                                # We only reach this branch when alloc's price
                                # is None or equal to existing's (a DIFFERENT
                                # known price already took the `if` above).
                                # alloc.reference_price is None means the
                                # artifact backing THIS base correction
                                # doesn't currently resolve a price -- e.g. a
                                # confirmed amount-only fill with a valid base
                                # but no confirmed price yet. Keeping
                                # existing's known price (necessarily from a
                                # DIFFERENT read) paired with the reload's
                                # corrected base would combine two fields
                                # from different snapshots into a combination
                                # NEITHER pass actually produced (Codex
                                # review: "Avoid combining a stale price with
                                # a reloaded base") -- if the afternoon price
                                # happens to be below that stale known price,
                                # the guard would compute atop the doubled
                                # base instead of taking the reload's own
                                # missing_prior_price path. Poison the
                                # reference alongside accepting the base.
                                prior_same_day_allocations[ticker] = _dc.replace(
                                    existing,
                                    reference_price=None,
                                    reference_ts=None,
                                    selected_base_amount_usd=alloc.selected_base_amount_usd,
                                    selected_base_amount_usd_at=alloc.selected_base_amount_usd_at,
                                )
                            else:
                                # alloc.reference_price == existing.reference_price
                                # here -- same price, corrected base, nothing
                                # inconsistent to guard against.
                                prior_same_day_allocations[ticker] = _dc.replace(
                                    existing,
                                    selected_base_amount_usd=alloc.selected_base_amount_usd,
                                    selected_base_amount_usd_at=alloc.selected_base_amount_usd_at,
                                )
                    else:
                        # alloc.amount_usd < existing.amount_usd: the reload
                        # recovered LESS for this ticker than the first pass
                        # had, so the first pass's larger amount is still the
                        # more complete floor -- keep it (we don't have
                        # per-artifact identity to confirm whether this is a
                        # legitimate downward correction of the SAME artifact
                        # or the reload losing one entirely, Codex review:
                        # "Recognize smaller fills resolved by the reload",
                        # already declined for that reason -- the failure
                        # mode of keeping the larger amount is SAFE, only
                        # ever understating the allowed top-up). But a CLEAN
                        # reload's own KNOWN reference_price disagreeing with
                        # existing's is still direct, authoritative evidence
                        # that reference is stale, regardless of which
                        # amount is more complete: a $5 fill at $100
                        # reconciled to a confirmed $4 partial fill at $90 is
                        # a genuine price correction, and keeping the stale,
                        # higher $100 reference could pass the
                        # price-below-prior check and permit a top-up the
                        # clean reload's own data would reject (Codex
                        # review: "Discard stale references when a clean
                        # reload is smaller"). Poison the reference (an
                        # already-None existing reference no-ops here) but
                        # keep existing's amount as the conservative floor.
                        # selected_base_amount_usd is sourced from the SAME
                        # underlying artifact and resolve_top_up_desired_
                        # amount_usd multiplies it directly to compute the
                        # top-up target -- a clean reload correcting the base
                        # downward while the reference stays unchanged (e.g.
                        # base $2 -> $1, reference unchanged at $100) is
                        # still direct evidence the stale base is wrong: kept
                        # unpoisoned, a stale LARGER base combined with the
                        # kept LARGER prior amount could still compute a
                        # target that permits a top-up the reload's own
                        # (corrected, smaller) base would not (Codex review:
                        # "Handle base corrections on smaller clean
                        # reloads").
                        if (
                            (
                                alloc.reference_price is not None
                                and alloc.reference_price != existing.reference_price
                            )
                            or alloc.selected_base_amount_usd
                            != existing.selected_base_amount_usd
                            or alloc.selected_base_amount_usd_at
                            != existing.selected_base_amount_usd_at
                        ):
                            import dataclasses as _dc

                            prior_same_day_allocations[ticker] = _dc.replace(
                                existing, reference_price=None, reference_ts=None
                            )
                        # else: alloc's reference is unknown or matches
                        # existing's, and the base matches too -- nothing to
                        # poison, keep existing entirely unchanged.
                if not _reload_verified_complete:
                    # The loop above only visits tickers present in
                    # second_pass -- a ticker that appears ONLY in first_pass
                    # (entirely absent from an INCOMPLETE reload) never goes
                    # through any of the poisoning logic above at all, and
                    # simply keeps its first-pass value untouched via the
                    # `dict(first_pass)` seed. But an incomplete reload's
                    # silence about a ticker is not proof nothing exists for
                    # it -- the reload may simply have failed to read this
                    # exact ticker's artifact(s), matching every other
                    # disagreement this bounded reload already treats as
                    # uncertain rather than trustworthy (Codex review:
                    # "Poison first-only entries after an incomplete
                    # reload"). Poison any first-pass-resolved ticker with no
                    # counterpart in second_pass; an already-unresolved one
                    # has nothing to poison.
                    import dataclasses as _dc

                    for ticker, existing in first_pass.items():
                        if ticker in second_pass or existing.reference_price is None:
                            continue
                        prior_same_day_allocations[ticker] = _dc.replace(
                            existing, reference_price=None, reference_ts=None
                        )
                still_unresolved = _tickers_with_unresolved_same_day_context(
                    prior_same_day_allocations
                )
                if still_unresolved:
                    logger.warning(
                        "same-day context still unresolved for %s after bounded reload; "
                        "affected tickers route to the top-up guard's conservative "
                        "missing_prior_price / manual_review $0 path",
                        still_unresolved,
                    )
                if second_scan_had_failures:
                    # The reload itself could not fully verify this run's
                    # same-day context either. A ticker whose ONLY artifact is
                    # unreadable/mid-flush on BOTH passes never appears as a
                    # key in either mapping, so it is invisible to
                    # `still_unresolved` above (which only inspects existing
                    # keys) even though its prior context genuinely could not
                    # be confirmed -- that ticker will still be evaluated as
                    # `no_prior_run` downstream (Codex review: "Retain the
                    # second-pass scan failure signal"). There is no ticker
                    # name to route conservatively here (the skipped file's
                    # own content -- and therefore which ticker it belonged to
                    # -- was exactly what failed to be read), and unilaterally
                    # blocking the entire run over an unattributed scan
                    # failure would be a much larger behavior change than a
                    # bounded-reload patch should make on its own; escalate
                    # loudly instead so an operator can decide whether the
                    # affected artifacts warrant a manual hold.
                    logger.error(
                        "same-day context scan for %s still had unreadable/unparseable "
                        "artifacts after the bounded reload; a ticker whose ONLY "
                        "same-day artifact was affected would not appear in "
                        "prior_same_day_allocations at all and may be evaluated "
                        "as no_prior_run -- verify this run's same-day artifact "
                        "trees manually before trusting its top-up guard results",
                        artifacts_dir,
                    )
                # Inspect every FINAL merged allocation for uncertainty --
                # checked via
                # near_close_dca.PriorSameDayAllocation.notional_uncertain,
                # the explicit provenance flag set only where the
                # unreconstructable sentinel actually seeded amount_usd.
                # `reference_price is None` was used here previously, but
                # that is also true of an ordinary "submitted"/
                # "unconfirmed" allocation with a genuine, fully
                # reconstructed dollar amount that simply has no fill price
                # yet -- treating that as still-uncertain forced the shared
                # weekly bucket closed even after the reload cleanly
                # resolved the dollar amount (Codex review: "Track unknown
                # notionals separately from missing prices"). A ticker
                # recovered by name into a clean, resolved reload entry no
                # longer needs to poison the shared weekly bucket; one
                # retained verbatim from an unresolved first pass (its
                # artifact simply absent from this reload) still does. This
                # deliberately does not rely on either loader's sink: manual
                # fallback rows can carry the same explicit uncertainty
                # without having passed through the core loader that fills
                # those sinks (Codex review: "Include fallback uncertainty
                # when poisoning shared caps").
                _same_day_unreconstructable_sink = [
                    ticker
                    for ticker, allocation in prior_same_day_allocations.items()
                    if allocation.notional_uncertain
                ]
            else:
                prior_same_day_allocations = first_pass
            if _same_day_unreconstructable_sink:
                earlier_week_scan_had_failures = True
        else:
            # Same fail-closed treatment as the auto-derived branch above:
            # an unreconstructable submission's amount, seeded from THIS
            # run's own max_per_order_usd, is not proof about whatever
            # ceiling governed the earlier same-day run that actually
            # placed it. Without capturing that signal here too, this
            # explicit-root override path left the shared daily/weekly
            # totals silently understated instead of forcing the same
            # global worst-case-ceiling fail-closed (Codex review: "Fail
            # explicit-root unknown submissions closed").
            _explicit_root_unreconstructable_sink: List[str] = []
            prior_same_day_allocations = load_prior_same_day_allocations(
                prior_allocation_state_root,
                trading_day,
                boundary_et=now.astimezone(EASTERN).time(),
                unreconstructable_submission_amount_usd=config.max_per_order_usd,
                unreconstructable_sink=_explicit_root_unreconstructable_sink,
            )
            if _explicit_root_unreconstructable_sink:
                earlier_week_scan_had_failures = True

    return earlier_week_notional_usd, earlier_week_scan_had_failures, prior_same_day_allocations


def _run_pipeline_impl(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    *,
    artifacts_dir: str | Path,
    run_label: str = "daily",
    scheduled: bool = True,
    generate_full_reports: bool = False,
    ignore_trend_filter: bool = False,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None,
    prior_allocation_state_root: Optional[str | Path] = None,
    deployment_history_root: Optional[str | Path] = None,
    classification_state_root: Optional[str | Path] = None,
    config_dir: Optional[str | Path] = None,
    raw_connector_inputs: Optional[Mapping[str, Any]] = None,
    required_raw_filenames: Optional[frozenset] = None,
    classification_overrides: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    stream: str = "afternoon",
    preflight_gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one compact DCA cycle and (optionally) the full post-trade report set.

    Returns the merged dict of artifact/report paths and summary metrics. The compact
    daily path always runs; ``generate_full_reports=True`` additionally renders the
    slippage/coverage reports and the compact Notion sync from the run artifacts
    (heavier work reserved for Friday/manual runs).

    The same-day prior-run offset/top-up guard is scheduled-run-only. For a
    scheduled run, ``prior_allocation_state_root`` defaults to the cron task root
    derived from ``artifacts_dir`` (see ``_default_prior_allocation_state_root``)
    so the guard activates without every caller having to wire it explicitly;
    pass ``prior_same_day_allocations`` directly to override, or an explicit
    ``prior_allocation_state_root`` to scan a different tree. Manual runs never
    auto-load prior allocations, so a manual invocation is never guard-active.

    ``deployment_history_root`` feeds the trailing-7-day kill-switch (see
    ``dca_risk.apply_risk_guard``): it should point at the directory containing
    prior days' ``run_state.json`` artifacts. Defaults to ``artifacts_dir``'s
    parent, since the live task's per-day directories are its siblings.

    ``raw_connector_inputs`` lets the scheduler hand in raw connector responses
    before signal computation. Scheduled runs persist these under the run's
    ``raw/`` directory and stamp the saved-file manifest into ``run_state.json``;
    if persistence or normalization fails, the pipeline writes the documented
    no-order error run_state and returns without computing or placing.

    ``required_raw_filenames`` declares the complete set of filenames the
    scheduler expects to be present in ``raw_connector_inputs`` for a live run.
    When provided and ``dry_run=False``, any missing filename is treated as a
    persistence failure and routes through the fail-closed error state rather
    than proceeding to compute with an incomplete raw payload set.

    ``classification_state_root`` is passed through to ``near_close_dca.run_once``
    for a scheduled run: it is where ``latest_scheduled_run.json`` is
    written/updated (see ``scripts.run_classification``). Opt-in only: when not
    given, no ``latest_scheduled_run.json`` write is attempted at all, so a
    caller must pass the real cron_tracking root explicitly to enable it.

    ``config_dir`` is forwarded to ``load_morning_config``/``load_midday_config``
    when resolving the sibling morning/midday roots below (see
    ``_resolve_cross_stream_prior_allocation_roots``); ``None`` uses their own
    default resolution (``DCA_CONFIG_DIR`` env var, then the bundled config/
    directory), matching how ``config`` itself is normally loaded.

    ``classification_overrides`` and ``correlation_id`` are forwarded to
    ``near_close_dca.run_daily_compact`` so a non-afternoon roster slot (see
    ``run_slot``) stamps its OWN ``run_classification`` identity and a
    deterministic ``correlation_id`` into ``run_state.json``, instead of the
    afternoon-only defaults ``build_run_artifacts`` otherwise falls back to.
    ``None`` (the default for both) reproduces the unmodified afternoon path.

    ``preflight_gate`` is ``run_slot``'s morning-preflight diagnostic dict
    (see ``morning_preflight_eligible_tickers``). It is stamped onto the
    returned result dict as ``result["preflight_gate"]`` -- NOT forwarded into
    ``build_run_artifacts``/``run_daily_compact`` itself, which accepts no
    such parameter (a prior version of this docstring claimed otherwise; that
    was aspirational, not actual behavior -- Codex review: "Emit the advisory
    preflight warning from run_slot"). For durable observability it is also
    read back into the ``run_state.json`` that ``build_run_artifacts`` already
    wrote to disk (best-effort read-modify-write via ``result["run_state_path"]``,
    since ``run_daily_compact`` returns after that file is already published --
    Codex review: "Persist preflight diagnostics in the canonical artifact").
    ``run_slot`` also logs ``preflight_gate["warning"]`` when present, so a
    degraded full-universe evaluation (missing, forbidden-root, or
    valid-but-empty same-day preflight) is never silently indistinguishable
    from a normal one, whether an operator reads the log, the in-memory
    result, or the persisted artifact.

    NON-BLOCKING as of 2026-08-02 (Geoff: "should not block any later runs"):
    this function used to short-circuit into a startup-failure-shaped blocked
    run_state here when ``preflight_gate`` reported ``preflight_missing_block``
    (Codex review: "Route the preflight-blocked result through guarded_run",
    "Apply the calendar gate before the preflight early return"). That
    diagnostic flag is never set anymore --
    ``morning_preflight_eligible_tickers`` degrades to the full configured
    universe with a warning instead of blocking -- so this function no longer
    special-cases it at all; a missing/invalid same-day preflight artifact
    now just means ``run_slot`` passed through the full universe already, and
    this function evaluates it normally like any other run.
    """
    # Drain any leftover raw placement responses from a prior run on this broker
    # instance (e.g. a manual run_pipeline() call that preceded this scheduled one).
    # Without this, a non-scheduled manual call would leave its responses in the
    # buffer and the next scheduled run would write them into its orders_after_place.json.
    getattr(broker, "get_raw_placement_responses", lambda: [])()
    if ignore_trend_filter and not config.ignore_trend_filter:
        import dataclasses as _dc

        config = _dc.replace(config, ignore_trend_filter=True)
    # Auto-apply the final-buy trend override for scheduled runs in the final-buy
    # window (task 10ce362c, retimed to 3:30 PM ET on 2026-07-27). The lower bound
    # is 15:15 ET only for pre-retime legacy dates, so a legacy 3:20 PM run stays
    # attributable to the scheduled window; on/after the retime it narrows to the
    # canonical 15:25 ET classification window start, so a delayed/replayed or
    # misfired invocation between 15:15 and 15:24 ET does not skip the downtrend
    # check outside the window the manifest actually permits (15:30-15:32 ET
    # starts) -- codex review discussion_r3657271692.
    # This is restricted to scheduled live runs so manual, demo, and morning/midday callers
    # are unaffected — they run outside this window and/or with scheduled=False.
    elif scheduled and not config.dry_run and not config.ignore_trend_filter:
        _now_et = now.astimezone(EASTERN).time()
        _window_start = _final_buy_trend_override_window_start(now)
        if _window_start <= _now_et <= time(15, 35):
            import dataclasses as _dc

            config = _dc.replace(config, ignore_trend_filter=True)
            logger.info(
                "final-buy window (%s ET): applied ignore_trend_filter=True for scheduled run",
                _now_et.strftime("%H:%M"),
            )
    # _persist_raw_inputs() below computes its own LOCAL trading_day_raw for its
    # own internal use, but this function needs the same value again much later
    # (the post-compute raw-input manifest rescan) -- computed here too, once,
    # from the same `now` so the two never drift, since a function-local value
    # inside the helper is invisible to this caller's own later scope.
    trading_day_raw = now.astimezone(EASTERN).date().isoformat()
    (
        raw_persistence_state,
        finance_data,
        robinhood_data,
        failure_result,
    ) = _persist_raw_inputs(
        scheduled,
        now,
        config,
        finance_data,
        robinhood_data,
        artifacts_dir,
        raw_connector_inputs,
        required_raw_filenames,
        run_label,
        state,
        classification_overrides,
        correlation_id,
        classification_state_root,
    )
    if failure_result is not None:
        return failure_result

    earlier_week_notional_usd, earlier_week_scan_had_failures, prior_same_day_allocations = _resolve_prior_allocations(
        now,
        scheduled,
        config,
        artifacts_dir,
        config_dir,
        prior_same_day_allocations,
        prior_allocation_state_root,
    )
    if deployment_history_root is None:
        deployment_history_root = Path(artifacts_dir).parent
    result = run_daily_compact(
        now,
        config,
        state,
        finance_data,
        robinhood_data,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir,
            run_label=run_label,
            scheduled=scheduled,
            prior_same_day_allocations=prior_same_day_allocations,
            deployment_history_root=deployment_history_root,
            classification_state_root=classification_state_root,
            raw_persistence_state=raw_persistence_state,
            classification_overrides=classification_overrides,
            correlation_id=correlation_id,
            stream=stream,
            earlier_week_notional_usd=earlier_week_notional_usd,
            earlier_week_scan_had_failures=earlier_week_scan_had_failures,
        ),
    )
    if result.get("directory_ownership_verified") is False:
        # run_daily_compact() itself already refused to merge/backfill into
        # this shared directory once it discovered a DIFFERENT invocation
        # (or account) claimed it in the gap around run_once() -- but
        # everything below this point still trusted result["run_state_path"]
        # unconditionally as THIS invocation's own output: the raw-input
        # manifest refresh, the preflight_gate persistence rewrite, and the
        # raw-placement-response merge all read-modify-write files under
        # that same path/artifacts_dir. Skipping them here, the same way the
        # `blocked` branch below already skips reporting, is what makes
        # run_daily_compact's own "leave it untouched" guarantee actually
        # hold at the pipeline level (Codex review: "Propagate foreign
        # ownership to the outer pipeline").
        logger.warning(
            "run_daily_compact could not verify ownership of the shared artifacts "
            "directory for %s; skipping the raw-input manifest refresh, "
            "preflight_gate persistence, and raw placement-response merge to avoid "
            "corrupting another run's artifacts",
            result.get("run_state_path"),
        )
        return result
    # Captured HERE -- immediately after run_daily_compact() returned with
    # ownership verified, before either of the two best-effort rewrites
    # below (the raw-input manifest refresh, then the preflight_gate
    # persistence) does ANY further work of its own. Each rewrite's own
    # in-lock byte comparison used to read its "initial" snapshot for the
    # very first time only after acquiring its lock -- which verifies
    # nothing changed WHILE the lock was held, but says nothing about
    # whether that first read was already foreign: another slot or account
    # can replace run_state.json in the gap between run_daily_compact()
    # returning and either lock being acquired (the raw-manifest refresh's
    # own rescan above is explicitly documented as "potentially slow"), and
    # a same-identity re-read of an already-foreign file trivially compares
    # equal to itself, so this invocation's rewrite still lands in the
    # foreign run (Codex review: "Recheck artifact identity inside each
    # post-run lock"). Binding both rewrites to the SAME known-good bytes
    # from this single point -- the moment ownership was last confirmed --
    # closes that gap; each rewrite advances this snapshot to its own
    # newly-written bytes on a successful write, so the second rewrite's
    # comparison is against the first rewrite's own legitimate output, not
    # stale pre-rewrite content.
    #
    # Preferring `result["_run_state_bytes_under_lock"]` -- the exact bytes
    # `run_daily_compact()`'s own ownership-verification critical section
    # last wrote/read while it STILL held `run_state.json.lock` -- over a
    # fresh read of the shared path here. An unconditional re-read (an
    # earlier version of this fix) closed the window the "Recheck artifact
    # identity inside each post-run lock" fix above was about, but opened a
    # narrower one of its own: another invocation sharing this identity can
    # still replace run_state.json in the gap between run_daily_compact()
    # releasing its lock and this read running, so `_known_run_state_bytes`
    # would silently capture the PEER's payload as "this invocation's last
    # verified state" -- which the raw-manifest/preflight_gate rewrites
    # below then accept and go on to mutate the peer's artifact against
    # (Codex review: "Bind the post-run snapshot to the ownership check").
    # `run_daily_compact()` returns this key on every call. It is non-None
    # on the ownership-verified success path through its own unified
    # critical section (see that function's own comment on the field), AND
    # -- since "Return a bound snapshot for owned blocked runs" -- on the
    # calendar-blocked-but-ownership-verified branch too, which re-verifies
    # its own last-read bytes under `run_state.json.lock` immediately
    # before returning rather than leaving this caller to fall back to an
    # unlocked read performed well after that ownership check ran. Every
    # OTHER path (foreign directory, first-write displaced by peer, lock/
    # reinjection exhaustion) still has no such lock-held snapshot to bind
    # to, so this falls back to the same unlocked read those paths always
    # used, rather than trusting a fabricated value.
    _run_state_path_for_pipeline = result.get("run_state_path")
    # Popped, not merely read: this key is an internal-only handoff between
    # run_daily_compact()'s own critical section and this one capture point
    # here, never part of the documented, caller-facing result shape (every
    # OTHER key on this dict is). Leaving it in `result` would leak a raw
    # bytes blob into `run_dca.main()`'s own `--report` JSON dump
    # (`json.dumps(result, ..., default=str)`) and any other caller that
    # persists or logs this return value.
    _run_state_bytes_under_lock_b64: Optional[str] = result.pop("_run_state_bytes_under_lock", None)
    # `run_daily_compact()` now base64-encodes this key so its documented
    # result dict stays plain-`json.dumps()`-safe for direct callers (Codex
    # review: "Keep raw snapshot bytes out of the public result"). Decode
    # back to the raw bytes this pipeline actually compares against.
    _known_run_state_bytes: Optional[bytes] = (
        base64.b64decode(_run_state_bytes_under_lock_b64) if _run_state_bytes_under_lock_b64 is not None else None
    )
    if _known_run_state_bytes is None and _run_state_path_for_pipeline:
        try:
            _known_run_state_bytes = Path(_run_state_path_for_pipeline).read_bytes()
        except OSError:
            _known_run_state_bytes = None
    # Set by either rewrite below the moment its in-lock byte comparison
    # finds run_state.json no longer matches `_known_run_state_bytes` --
    # i.e. a DIFFERENT invocation replaced this run's canonical state in the
    # gap since ownership was last confirmed. Each rewrite already skips ITS
    # OWN write when this happens, but previously nothing stopped the rest
    # of this function (the raw-placement-response merge and reporting
    # below) from continuing to treat `artifacts_dir`/`run_state_path` as
    # this invocation's own output and merge/report against the peer's run
    # (Codex review: "Stop post-processing after ownership changes").
    _ownership_lost_mid_pipeline = False
    if raw_persistence_state is not None:
        # The frozen finance provider's live corporate-action lookups (see
        # _build_frozen_providers_from_raw) can write NEW raw/corporate_
        # action_SYMBOL.json files DURING the compute step just above --
        # AFTER raw_persistence_state's manifest was already captured, pre-
        # compute, by build_raw_persistence_state() in _persist_raw_and_
        # freeze(). Left unrefreshed, run_state.json's raw_inputs_saved list
        # would omit a file that genuinely exists under raw/ and genuinely
        # influenced this run's decision, making the persistence audit
        # manifest inaccurate (Codex review: "Refresh the raw-input manifest
        # after action persistence"). Re-scan now that compute has finished
        # and, only if the saved-file list actually changed, patch the
        # already-written run_state.json in place -- the identical best-
        # effort read-modify-write pattern the preflight_gate rewrite just
        # below uses, including re-settling usage_proxy's byte counters
        # against the (slightly larger) rewritten payload.
        _raw_input_dir = raw_persistence_state.get("raw_input_dir")
        if _raw_input_dir:
            # The rescan itself (directory iteration/stat, not just the final
            # write) must be best-effort too -- a transient filesystem error
            # or permissions change here must not turn an already-completed
            # trading run (orders may already be placed) into an exception
            # that skips the remaining raw-response persistence and
            # reporting below, purely because an audit-manifest refresh
            # failed (Codex review: "Guard the post-compute manifest
            # rescan").
            try:
                from scripts.file_lock import cross_process_lock

                _raw_input_dir_obj = Path(str(_raw_input_dir))
                # Keep the canonical raw snapshot stable from the directory
                # rescan through this invocation's decision about whether the
                # manifest changed at all. A concurrent retry uses the same
                # sidecar while swapping raw.tmp -> raw, so without this lock
                # the rescan can observe a different invocation's snapshot and
                # publish its filenames into this run's state.
                with cross_process_lock(_raw_input_dir_obj.parent / ".corporate_actions.lock"):
                    _refreshed_persistence = build_raw_persistence_state(
                        trading_day_raw, raw_dir=_raw_input_dir_obj
                    )
                if _refreshed_persistence.get("raw_inputs_saved") != raw_persistence_state.get(
                    "raw_inputs_saved"
                ):
                    _run_state_path = result.get("run_state_path")
                    if _run_state_path:
                        _run_state_path_obj = Path(_run_state_path)
                        # Hold the same per-run cross_process_lock() convention
                        # every other shared-artifact read-modify-write in this
                        # codebase uses (near_close_dca.py's fingerprint-key
                        # publish, dca_cumulative.py, dca_whole_share_
                        # accumulator.py, ...) for the read + compare + write
                        # below. Comparing bytes-then-writing without a lock is
                        # NOT an atomic compare-and-swap: a concurrent retry
                        # could still replace run_state.json after the equality
                        # check but before this invocation's own replace,
                        # letting the stale invocation clobber the newer
                        # retry's canonical state despite the guard (Codex
                        # review: "Serialize the manifest update with
                        # concurrent retries"). The lock is scoped to just this
                        # read-compare-write span, not the (potentially slow)
                        # rescan above, so it is held only as long as the
                        # actual critical section.
                        with cross_process_lock(_run_state_path_obj.with_name(f"{_run_state_path_obj.name}.lock")):
                            try:
                                _initial_bytes = _run_state_path_obj.read_bytes()
                                _run_state_obj = json.loads(_initial_bytes)
                            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                                _initial_bytes = None
                                _run_state_obj = None
                            if isinstance(_run_state_obj, dict):
                                _run_state_obj.update(_refreshed_persistence)
                                _usage = _run_state_obj.get("usage_proxy")
                                if isinstance(_usage, dict):
                                    _csv_bytes = sum(
                                        path.stat().st_size
                                        for path in _existing_csv_artifacts(_run_state_path_obj.parent)
                                    )
                                    _serialized = _settle_usage_bytes(_run_state_obj, _usage, _csv_bytes)
                                else:
                                    _serialized = json.dumps(_run_state_obj, indent=2, sort_keys=True)
                                # Guard against a lost-update race with a
                                # concurrent retry of this same slot also read-
                                # modify-writing run_state.json -- the identical
                                # re-read-before-write CAS check the intraday-
                                # archive annotation path already uses (Codex
                                # review: "Guard the manifest rewrite against
                                # concurrent retries"). This rewrite is
                                # best-effort, so it must never cost a
                                # genuinely newer on-disk state written by
                                # another invocation in between. Belt-and-
                                # braces alongside the lock above: a
                                # process that somehow writes run_state.json
                                # without taking the lock (e.g. an older
                                # binary mid-rollout) still cannot silently
                                # clobber a change this check would catch.
                                #
                                # Compare the actual bytes rather than a stat
                                # tuple: st_ctime_ns is not a portable change
                                # counter (on Windows it is the file's creation
                                # time, not a metadata-change time, so an
                                # in-place rewrite that preserves size/mtime --
                                # e.g. via os.utime -- would go undetected and
                                # let this refresh clobber a concurrent retry's
                                # newer state; Codex review: "Use a portable
                                # change detector for concurrent writes").
                                #
                                # Compared against `_known_run_state_bytes` --
                                # the snapshot captured right after
                                # run_daily_compact() itself last confirmed
                                # ownership, BEFORE the (potentially slow)
                                # rescan above -- not against `_initial_bytes`,
                                # this call's own first read a few lines up.
                                # `_initial_bytes` is read only AFTER this
                                # lock is already held, so if a different
                                # slot or account claimed this directory in
                                # the gap between run_daily_compact()
                                # returning and this lock being acquired,
                                # `_initial_bytes` is already that foreign
                                # payload -- comparing `_current_bytes`
                                # against it would trivially match (nothing
                                # changes while THIS invocation holds the
                                # lock) and this rewrite would land in the
                                # foreign run without ever revalidating whose
                                # run this still is (Codex review: "Recheck
                                # artifact identity inside each post-run
                                # lock").
                                try:
                                    _current_bytes = _run_state_path_obj.read_bytes()
                                except OSError:
                                    _current_bytes = None
                                if (
                                    _known_run_state_bytes is not None
                                    and _current_bytes == _known_run_state_bytes
                                ):
                                    atomic_write_text(_run_state_path, _serialized)
                                    # Advance the shared pipeline-level
                                    # snapshot to what THIS rewrite itself
                                    # just legitimately published, so the
                                    # preflight_gate rewrite below (which
                                    # reuses `_known_run_state_bytes` as its
                                    # OWN expected baseline) compares against
                                    # this rewrite's real output rather than
                                    # the now-stale pre-rewrite bytes.
                                    _known_run_state_bytes = _serialized.encode("utf-8")
                                else:
                                    if _known_run_state_bytes is not None:
                                        # A genuine detected change, not just
                                        # a missing baseline -- some OTHER
                                        # invocation holds this directory now
                                        # (Codex review: "Stop post-
                                        # processing after ownership
                                        # changes").
                                        _ownership_lost_mid_pipeline = True
                                    logger.warning(
                                        "run_state at %s changed concurrently; skipping "
                                        "raw-input manifest refresh to avoid clobbering a "
                                        "newer retry's state",
                                        _run_state_path,
                                    )
            except OSError:
                logger.warning(
                    "failed to refresh raw-input manifest for %s", _raw_input_dir, exc_info=True
                )
    if preflight_gate is not None:
        # Persist run_slot's morning-preflight diagnostic dict onto every
        # return path below, not just log it: this docstring already
        # promised "forwarded through to build_run_artifacts for
        # observability", but build_run_artifacts (near_close_dca.py) never
        # actually accepted or stored this parameter, so the dict -- and any
        # advisory "warning" it carries when the same-day preflight is
        # missing or found empty -- was silently discarded, leaving
        # operators unable to tell a degraded full-universe evaluation from
        # a normal one after the fact (Codex review: "Emit the advisory
        # preflight warning from run_slot").
        result["preflight_gate"] = preflight_gate
        # The above only updates this function's in-memory return value.
        # build_run_artifacts/run_daily_compact already wrote run_state.json
        # to disk (via write_run_artifacts) before this line runs, so without
        # this read-modify-write the canonical, durable artifact -- the one
        # operators and reporting actually consult -- still could not
        # distinguish a degraded full-universe evaluation from a normal one
        # (Codex review: "Persist preflight diagnostics in the canonical
        # artifact"). Best-effort: a missing/corrupt run_state.json here is
        # already a distinct, logged failure mode elsewhere, and must not
        # block the rest of this function's return.
        run_state_path = result.get("run_state_path")
        if run_state_path:
            # Same lock + re-read-before-write CAS pattern as the raw-input
            # manifest refresh just above -- not a bare read/modify/write.
            # Two overlapping live invocations of the same slot can both
            # pass the ownership check earlier in this function; without
            # serializing this rewrite, invocation A can read run_state.json
            # here, invocation B can publish its own newer state in the
            # meantime, and A's write below would then overwrite B's state
            # with a payload built from A's stale read -- silently dropping
            # B's own submitted candidates from the canonical state and
            # deployment accounting (Codex review: "Reverify ownership
            # before rewriting post-run state"). Best-effort throughout, as
            # already promised: a missing/corrupt run_state.json, a lock
            # timeout, or a concurrent change detected here must not turn an
            # already-completed trading run (orders may already be placed)
            # into an exception that skips the remaining raw-response
            # persistence and reporting below.
            try:
                from scripts.file_lock import cross_process_lock

                _preflight_run_state_path_obj = Path(run_state_path)
                with cross_process_lock(
                    _preflight_run_state_path_obj.with_name(f"{_preflight_run_state_path_obj.name}.lock")
                ):
                    try:
                        _preflight_initial_bytes = _preflight_run_state_path_obj.read_bytes()
                        run_state_obj = json.loads(_preflight_initial_bytes)
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        _preflight_initial_bytes = None
                        run_state_obj = None
                    if isinstance(run_state_obj, dict):
                        run_state_obj["preflight_gate"] = preflight_gate
                        usage = run_state_obj.get("usage_proxy")
                        if isinstance(usage, dict):
                            # This rewrite grows run_state.json by the serialized
                            # preflight_gate dict, but run_daily_compact() already
                            # settled usage_proxy.state_file_bytes/total_artifact_
                            # bytes against the SMALLER pre-rewrite payload, so
                            # those counters would otherwise understate this run's
                            # size -- dca_cost_tracker.py consumes
                            # total_artifact_bytes directly as its artifact-I/O
                            # proxy (Codex review: "Recompute usage bytes after
                            # rewriting the run state"). Re-settle them the same
                            # way _backfill_orders_csv_usage() does after orders.csv
                            # lands post-write_run_artifacts: re-derive csv_bytes
                            # from whatever CSV artifacts already exist on disk
                            # (unaffected by this rewrite) and let _settle_usage_bytes
                            # recompute the state-file size against the NEW payload.
                            csv_bytes = sum(
                                path.stat().st_size
                                for path in _existing_csv_artifacts(_preflight_run_state_path_obj.parent)
                            )
                            serialized = _settle_usage_bytes(run_state_obj, usage, csv_bytes)
                        else:
                            serialized = json.dumps(run_state_obj, indent=2, sort_keys=True)
                        try:
                            _preflight_current_bytes = _preflight_run_state_path_obj.read_bytes()
                        except OSError:
                            _preflight_current_bytes = None
                        # Compared against `_known_run_state_bytes` -- the
                        # pipeline-level snapshot captured right after
                        # run_daily_compact() last confirmed ownership,
                        # advanced by the raw-manifest rewrite above to ITS
                        # own output on a successful write -- not against
                        # `_preflight_initial_bytes`, this block's own first
                        # read taken only after THIS lock was already
                        # acquired. A same-identity peer that claimed this
                        # directory between run_daily_compact() returning and
                        # this lock being acquired would already be what
                        # `_preflight_initial_bytes` reads back, so
                        # comparing against it self-confirms nothing (Codex
                        # review: "Recheck artifact identity inside each
                        # post-run lock").
                        if (
                            _known_run_state_bytes is not None
                            and _preflight_current_bytes == _known_run_state_bytes
                        ):
                            atomic_write_text(run_state_path, serialized)
                            _known_run_state_bytes = serialized.encode("utf-8")
                        else:
                            if _known_run_state_bytes is not None:
                                _ownership_lost_mid_pipeline = True
                            logger.warning(
                                "run_state at %s changed concurrently; skipping preflight_gate "
                                "persistence to avoid clobbering a newer retry's state",
                                run_state_path,
                            )
            except (TimeoutError, OSError):
                logger.warning(
                    "failed to persist preflight_gate onto %s",
                    run_state_path,
                    exc_info=True,
                )
    if _ownership_lost_mid_pipeline:
        # A different invocation replaced run_state.json somewhere between
        # the two best-effort rewrites above -- the same loss of ownership
        # `result.get("directory_ownership_verified") is False` already
        # guards against earlier in this function, just discovered later.
        # Both rewrites above already skipped their OWN write once they
        # detected it; stop here too, the same way the `blocked` branch
        # below skips reporting, so the raw-placement-response merge and
        # report generation that follow never treat this directory as this
        # invocation's own output and merge/report against a peer's run
        # (Codex review: "Stop post-processing after ownership changes").
        result["directory_ownership_verified"] = False
        logger.warning(
            "run_state at %s changed concurrently mid-pipeline; skipping raw "
            "placement-response merge and reporting to avoid corrupting "
            "another run's artifacts",
            result.get("run_state_path"),
        )
        return result
    if result.get("blocked"):
        logger.warning(
            "run blocked by date gate for %s; skipping reports",
            result.get("run_state_path"),
        )
        return result
    # Codex review (finding: "Recheck ownership immediately before downstream
    # writes"): the `_ownership_lost_mid_pipeline` check above only catches a
    # peer replacement detected by one of the TWO rewrites that precede it
    # (the raw-manifest refresh and the preflight_gate persistence) --
    # each only runs its own in-lock compare when it actually has something
    # to rewrite (`raw_persistence_state`/`preflight_gate` set). A peer can
    # still replace run_state.json in the window between whichever of those
    # rewrites last confirmed ownership (or `run_daily_compact()`'s own
    # ownership check, if neither rewrite ran at all) and here, immediately
    # before the raw-response merge below reads and mutates
    # `raw/orders_after_place.json` and the full-report path further down
    # reads and republishes `run_state.json` itself -- neither of which
    # re-verifies ownership on its own. Left unguarded, both would run
    # against a directory this invocation no longer owns (Codex review:
    # "Recheck ownership immediately before downstream writes"). Mirrors the
    # exact lock/compare pattern used twice already above -- see either
    # `_known_run_state_bytes is not None` comparison for the full reasoning
    # on why the comparison must happen under the SAME lock as the read, not
    # against an earlier unlocked snapshot.
    _pre_merge_run_state_path = result.get("run_state_path")
    if _pre_merge_run_state_path and _known_run_state_bytes is not None:
        try:
            from scripts.file_lock import cross_process_lock

            _pre_merge_run_state_path_obj = Path(_pre_merge_run_state_path)
            with cross_process_lock(
                _pre_merge_run_state_path_obj.with_name(f"{_pre_merge_run_state_path_obj.name}.lock")
            ):
                try:
                    _pre_merge_current_bytes = _pre_merge_run_state_path_obj.read_bytes()
                except OSError:
                    _pre_merge_current_bytes = None
            if _pre_merge_current_bytes != _known_run_state_bytes:
                _ownership_lost_mid_pipeline = True
        except (TimeoutError, OSError):
            # Codex review (finding: "Fail closed when the ownership lock
            # cannot be checked"): an unverifiable check is NOT the same as
            # a verified one. A lock timeout or OSError can happen exactly
            # BECAUSE a concurrent invocation is actively publishing its own
            # first write right now -- precisely the scenario this recheck
            # exists to catch -- so treating "couldn't check" as "ownership
            # still fine, proceed" defeats the whole point of the recheck.
            # Previously this branch only logged and fell through, leaving
            # `_ownership_lost_mid_pipeline` False and letting the raw-
            # response merge and full-report generation run unguarded
            # against a directory whose ownership was never actually
            # confirmed. Setting it here routes execution through the exact
            # same early-return guard immediately below that a confirmed
            # loss already uses -- fail closed, not fail open.
            logger.warning(
                "failed to recheck ownership of %s before the raw-response merge",
                _pre_merge_run_state_path,
                exc_info=True,
            )
            _ownership_lost_mid_pipeline = True
    if _ownership_lost_mid_pipeline:
        # Same treatment as the identical branch above: a different
        # invocation now owns this directory, so nothing below -- the raw-
        # response merge, drift check, or full-report generation -- may
        # treat it as this invocation's own output.
        result["directory_ownership_verified"] = False
        logger.warning(
            "run_state at %s changed concurrently immediately before the raw "
            "placement-response merge; skipping it and reporting to avoid "
            "corrupting another run's artifacts",
            result.get("run_state_path"),
        )
        return result
    if scheduled and not config.dry_run:
        # Persist raw placement responses so fill data is replayable from
        # durable storage without a live broker re-query at reporting time.
        raw_responses = getattr(broker, "get_raw_placement_responses", lambda: [])()
        if raw_responses:
            raw_dir = Path(artifacts_dir) / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            oap_path = raw_dir / "orders_after_place.json"

            def _merge_and_write_orders_after_place() -> None:
                # Merge with any prior placement history so a partial retry (where some
                # orders are broker-deduplicated and others are newly placed) preserves
                # fill snapshots from both attempts. New responses take precedence on
                # order_id collision so the freshest broker state always wins.
                prior_orders: list = []
                if oap_path.exists():
                    try:
                        prior_orders = (
                            json.loads(oap_path.read_bytes()).get(
                                "orders", []
                            )
                            or []
                        )
                    except (json.JSONDecodeError, OSError, AttributeError):
                        prior_orders = []
                new_ids = {r.get("order_id") or r.get("id") for r in raw_responses} - {None}
                merged = raw_responses + [
                    o
                    for o in prior_orders
                    if (o.get("order_id") or o.get("id")) not in new_ids
                ]
                # The rows spread the broker's whole order dict (**order in
                # RobinhoodBroker.place_market_buy), which can echo the account
                # number and account URLs; this file lives under cron_tracking/
                # and is synced onward by reporting tooling, so every string in
                # it goes through the same redaction the run artifacts get
                # (2026-09-07 diagnostic, S:ADP-05).
                atomic_write_text(
                    oap_path, json.dumps({"orders": _redacted_strings(merged)}, indent=2) + "\n"
                )
                result["orders_after_place_path"] = str(oap_path)

            # Codex review (finding: "Keep ownership through the downstream
            # artifact writes"): the recheck above can still go stale by the
            # time THIS write actually runs -- fetching `raw_responses` from
            # the broker and building `raw_dir` both take real time, and a
            # peer can claim the directory in that gap even though the
            # recheck just confirmed ownership moments earlier. Adding yet
            # another standalone check immediately before this one would
            # only narrow that same race, never close it (Codex would keep
            # finding a narrower gap indefinitely -- see the long comment on
            # this function's earlier recheck for the full reasoning). So
            # this write reverifies identity itself, INSIDE the exact lock
            # acquisition that immediately precedes it, and performs the
            # merge/write while STILL HOLDING that lock -- compare and act
            # share one uninterrupted critical section, with no gap at all
            # between them, mirroring the pattern `write_run_artifacts` and
            # the two rewrites above it already use.
            if _pre_merge_run_state_path and _known_run_state_bytes is not None:
                try:
                    from scripts.file_lock import cross_process_lock

                    _oap_lock_path_obj = Path(_pre_merge_run_state_path)
                    with cross_process_lock(
                        _oap_lock_path_obj.with_name(f"{_oap_lock_path_obj.name}.lock")
                    ):
                        try:
                            _oap_current_bytes = _oap_lock_path_obj.read_bytes()
                        except OSError:
                            _oap_current_bytes = None
                        if _oap_current_bytes != _known_run_state_bytes:
                            _ownership_lost_mid_pipeline = True
                        else:
                            _merge_and_write_orders_after_place()
                except (TimeoutError, OSError):
                    # Fail closed here too (Finding 5's same reasoning): an
                    # unverifiable check right at the write site must not be
                    # read as "ownership fine".
                    logger.warning(
                        "failed to reverify ownership of %s immediately "
                        "before the raw-response merge write",
                        _pre_merge_run_state_path,
                        exc_info=True,
                    )
                    _ownership_lost_mid_pipeline = True
            else:
                # No bound snapshot to compare against at all (e.g. an
                # unowned/never-verified directory) -- write directly,
                # matching this function's behavior for that case before
                # this fix.
                _merge_and_write_orders_after_place()
        drift = _run_drift_check(Path(artifacts_dir))
        if drift is not None:
            result["drift_report"] = drift
    if _ownership_lost_mid_pipeline:
        # The in-lock write reverification just above can itself discover a
        # peer takeover that the earlier recheck missed (the gap it closes).
        # Same guard, same reasoning as every other ownership-loss return in
        # this function: nothing past this point may treat the directory as
        # this invocation's own.
        result["directory_ownership_verified"] = False
        logger.warning(
            "run_state at %s changed concurrently during the raw "
            "placement-response merge; skipping reporting to avoid "
            "corrupting another run's artifacts",
            result.get("run_state_path"),
        )
        return result
    if not generate_full_reports:
        return result

    # Imported lazily so the compact daily path has no dependency on the report layer.
    from dca_notion_sync import write_compact_sync
    from dca_reporting import generate_reports

    out = Path(artifacts_dir)
    # Codex review (finding: "Keep ownership through the downstream artifact
    # writes"): reads `run_state.json` INSIDE the same lock+compare that
    # verifies this invocation still owns the directory -- the compared
    # bytes and the bytes parsed into `run_state` below are the SAME bytes
    # from the SAME read, so there is no separate "check, release, then
    # act" step for a peer to land in between; the verification IS the read
    # that report generation then uses. Mirrors the raw-response merge's
    # own reverification immediately above, and the same
    # `write_run_artifacts` "read+compare inside the lock, only then use
    # it" convention.
    if _pre_merge_run_state_path and _known_run_state_bytes is not None:
        try:
            from scripts.file_lock import cross_process_lock

            _report_lock_path_obj = Path(_pre_merge_run_state_path)
            with cross_process_lock(
                _report_lock_path_obj.with_name(f"{_report_lock_path_obj.name}.lock")
            ):
                try:
                    _report_run_state_bytes: Optional[bytes] = _report_lock_path_obj.read_bytes()
                except OSError:
                    _report_run_state_bytes = None
            if _report_run_state_bytes is None or _report_run_state_bytes != _known_run_state_bytes:
                result["directory_ownership_verified"] = False
                logger.warning(
                    "run_state at %s changed concurrently immediately before "
                    "full-report generation; skipping it to avoid corrupting "
                    "another run's artifacts",
                    _pre_merge_run_state_path,
                )
                return result
            run_state = json.loads(_report_run_state_bytes)
        except (TimeoutError, OSError):
            # Fail closed (Finding 5's same reasoning): an unverifiable
            # check immediately before generating and publishing reports
            # must not be treated as "ownership fine, proceed".
            logger.warning(
                "failed to reverify ownership of %s immediately before "
                "full-report generation",
                _pre_merge_run_state_path,
                exc_info=True,
            )
            result["directory_ownership_verified"] = False
            return result
    else:
        # No bound snapshot to compare against at all -- read directly,
        # matching this function's behavior for that case before this fix.
        run_state = json.loads((out / "run_state.json").read_bytes())
    orders_csv = out / "orders.csv"
    fills = _fills_from_orders_csv(orders_csv)
    # When raw/orders_after_place.json was persisted at order time, use it for
    # fill data so the slippage report is replayable from the same durable file
    # rather than re-querying the broker at reporting time.
    orders_after_place = out / "raw" / "orders_after_place.json"
    if orders_after_place.exists():
        from dca_reporting import fills_from_order_history_payload

        order_id_to_ticker: Dict[str, str] = {}
        if orders_csv.exists():
            with orders_csv.open(newline="", encoding="utf-8") as _fh:
                for _row in csv.DictReader(_fh):
                    _oid = _row.get("order_id")
                    _tkr = _row.get("ticker")
                    if _oid and _tkr:
                        order_id_to_ticker[_oid] = _tkr
        persisted_fills = fills_from_order_history_payload(
            json.loads(orders_after_place.read_bytes()),
            order_id_to_ticker=order_id_to_ticker,
        )
        persisted_ids = {
            f.get("order_id") for f in persisted_fills if f.get("order_id")
        }
        fills = [
            f for f in fills if f.get("order_id") not in persisted_ids
        ] + persisted_fills
        # The persisted snapshot captures order state at placement time; orders that
        # were accepted/open then may have settled by now. Refresh any order ID that
        # appears in orders.csv but not in persisted_fills (i.e. not yet filled at
        # placement time) so they are not permanently reported as execution gaps.
        fills = _refresh_fills_from_broker(orders_csv, broker, fills)
    else:
        # Fallback: re-query the broker for any submitted-but-unfilled order so
        # it is not reported as a missing execution gap.
        fills = _refresh_fills_from_broker(orders_csv, broker, fills)
    # Align candidate target prices with the durable execution records so the slippage
    # report ranks against the same target the orders CSV uses (the signal-time quote).
    targets = _execution_targets_from_orders_csv(orders_csv)
    targets_by_order_id = _execution_targets_by_order_id_from_orders_csv(orders_csv)
    _apply_execution_targets_to_candidates(
        run_state.get("buy_candidates") or [], targets, targets_by_order_id
    )
    # Codex review (finding: "Keep ownership while publishing full
    # reports"): the lock+compare a little further up (`_report_run_state_
    # bytes`) only protects the READ that produced `run_state` -- it
    # released the moment that `with` block ended, several lines before
    # `fills`/`targets` were even computed. Everything from there through
    # here -- `_refresh_fills_from_broker` (network calls to the broker)
    # and the two DURABLE WRITES below, `generate_reports()` (writes
    # `latest_run_summary.json`, slippage reports, etc. into `out`) and
    # `write_compact_sync()` (writes the Notion sync payload, also into
    # `out`) -- ran completely unprotected. If a peer claims this
    # directory (replaces `run_state.json`) any time after that earlier
    # lock released, these writes would land in the peer's directory and
    # corrupt/overwrite its evidence -- exactly the class of bug the last
    # three rounds of fixes closed everywhere else in this function. A
    # standalone recheck immediately before the writes would only narrow
    # this gap, not close it (the raw-response merge above already learned
    # that lesson the hard way -- see its own comment). So this mirrors
    # `_merge_and_write_orders_after_place` exactly: a single closure that
    # verifies ownership and then performs BOTH durable writes while STILL
    # HOLDING the same `run_state.json.lock` acquisition that verified it
    # -- no gap at all between the check and the writes it protects. The
    # broker calls above are deliberately left OUTSIDE this lock -- there
    # is no correctness reason to hold a file lock across a network call,
    # and doing so risks the 30s default `cross_process_lock` timeout under
    # a slow broker response.
    report_paths: Dict[str, Any] = {}
    summary: Dict[str, Any] = {}
    sync_paths: Dict[str, Any] = {}
    _reports_ownership_verified = True

    def _verify_ownership_and_publish_reports() -> None:
        nonlocal report_paths, summary, sync_paths
        report_paths = generate_reports(run_state, fills, out)
        summary = json.loads(
            # ⚡ Bolt Optimization: json.loads accepts bytes directly, skipping string allocation
            Path(report_paths["latest_run_summary"]).read_bytes()
        )
        sync_paths = write_compact_sync(summary, out)

    if _pre_merge_run_state_path and _known_run_state_bytes is not None:
        from scripts.file_lock import cross_process_lock

        _final_lock_path_obj = Path(_pre_merge_run_state_path)
        # Codex review (finding: "Do not swallow report write failures as
        # ownership loss"): a single `try/except (TimeoutError, OSError):`
        # wrapped around the WHOLE `with cross_process_lock(...):` block
        # (an earlier version of this fix) could not tell "the lock itself
        # could not be acquired/verified" apart from "the lock was fine,
        # but generate_reports()/write_compact_sync() then raised OSError
        # publishing the reports" (a full disk, a permission change, an
        # atomic rename failing) -- both landed in the same except and got
        # misreported as `directory_ownership_verified = False`, so a
        # genuine publication failure returned a normal-looking result
        # instead of propagating, exactly the failure mode `guarded_run`
        # relied on this exception escaping to catch before this lock
        # existed. The lock is entered manually here (not via `with`) so
        # only ITS acquisition is covered by this except; the write call
        # below runs in a separate `try/finally` that guarantees the lock
        # is released but lets any OSError it raises propagate untouched.
        try:
            _final_lock_ctx = cross_process_lock(
                _final_lock_path_obj.with_name(f"{_final_lock_path_obj.name}.lock")
            )
            _final_lock_ctx.__enter__()
        except (TimeoutError, OSError):
            # Fail closed (the same reasoning as every other ownership-loss
            # branch in this function): an unverifiable check immediately
            # before publishing reports must not be treated as "ownership
            # fine, proceed".
            logger.warning(
                "failed to reverify ownership of %s immediately before "
                "publishing full reports",
                _pre_merge_run_state_path,
                exc_info=True,
            )
            _reports_ownership_verified = False
        else:
            try:
                try:
                    _final_current_bytes: Optional[bytes] = _final_lock_path_obj.read_bytes()
                except OSError:
                    _final_current_bytes = None
                if _final_current_bytes != _known_run_state_bytes:
                    _reports_ownership_verified = False
                else:
                    _verify_ownership_and_publish_reports()
            finally:
                _final_lock_ctx.__exit__(None, None, None)
    else:
        # No bound snapshot to compare against at all -- publish directly,
        # matching this function's pre-existing behavior for that case.
        _verify_ownership_and_publish_reports()

    if not _reports_ownership_verified:
        result["directory_ownership_verified"] = False
        logger.warning(
            "run_state at %s changed concurrently immediately before "
            "publishing full reports; skipping report/Notion-sync writes "
            "to avoid corrupting another run's artifacts",
            _pre_merge_run_state_path,
        )
        return result

    result["reports"] = report_paths
    result["notion_sync"] = sync_paths
    logger.info("Generated full post-trade reports in %s", out)
    return result


class DemoDataProvider:
    """Deterministic in-memory :class:`MarketDataProvider` for ``--demo`` / CI.

    Generates a downward intraday path so some tickers trigger a graded buy. The drop
    depth is a stable function of the ticker so runs are reproducible without any random
    source. This is a self-test harness — it never represents real market data.
    """

    def __init__(self, source: SourceName, previous_close: float = 100.0) -> None:
        self.source = source
        self._previous_close = previous_close

    def _drop_fraction(self, ticker: str) -> float:
        # 0.0 .. ~4.5% depending on the ticker, stable across runs.
        return (sum(ord(ch) for ch in ticker) % 46) / 1000.0

    def get_intraday_bars(
        self, ticker: str, start: datetime, end: datetime, interval_minutes: int
    ) -> List[Bar]:
        drop = self._drop_fraction(ticker)
        decision_close = self._previous_close * (1.0 - drop)
        # Lookback (session open) sits above the decision close -> 60-minute trend down.
        open_price = self._previous_close * (1.0 + 0.002)
        bars: List[Bar] = []
        cursor = start
        steps = max(1, int((end - start).total_seconds() // (interval_minutes * 60)))
        for i in range(steps + 1):
            frac = i / steps
            price = open_price + (decision_close - open_price) * frac
            bars.append(
                Bar(
                    timestamp=cursor,
                    open=price,
                    high=price * 1.001,
                    low=price * 0.999,
                    close=price,
                    volume=1000.0,
                )
            )
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        timestamp = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1),
            time(16, 0),
            tzinfo=EASTERN,
        )
        p = self._previous_close
        return Bar(timestamp=timestamp, open=p, high=p, low=p, close=p, volume=0.0)


def _resolve_now(now_arg: Optional[str], config: StrategyConfig) -> datetime:
    """Return the decision-time datetime for the run (parsed --now, or today's gate time).

    A trailing RFC3339 ``Z`` is normalised to ``+00:00`` before parsing. The
    scheduled tasks pass ``--now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"``, and
    ``datetime.fromisoformat`` only accepts ``Z`` from Python 3.11 onward, so
    calling it bare makes this entry point silently interpreter-version
    dependent. This mirrors ``dca_live_adapters._parse_rfc3339`` and
    ``scripts/run_classification._parse_iso_datetime``. A naive value (no zone
    suffix) is still read as Eastern, the timezone the strategy operates in.
    """
    if now_arg:
        text = now_arg.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=EASTERN)
    today = datetime.now(tz=EASTERN).date()
    return datetime.combine(today, config.decision_time_et, tzinfo=EASTERN)


def _demo_runtime(
    config: StrategyConfig,
) -> Tuple[MarketDataProvider, MarketDataProvider, BrokerAdapter]:
    finance = DemoDataProvider(SourceName.FINANCE)
    robinhood = DemoDataProvider(SourceName.ROBINHOOD)
    broker = build_broker(config)  # DryRunBroker for the demo
    return finance, robinhood, broker


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Near-close graded DCA orchestrator (dry-run CLI)."
    )
    parser.add_argument(
        "--config-dir",
        default=str(DEFAULT_CONFIG_DIR),
        help="Directory with config/*.json (default: bundled config/).",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help="Output directory (default: artifacts/<trading-day>).",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Override decision time (ISO 8601, e.g. 2026-07-06T15:45). For testing.",
    )
    parser.add_argument(
        "--run-label", default="daily", help="Run label recorded in artifacts."
    )
    parser.add_argument(
        "--slot",
        default=None,
        help="Canonical live roster slot label (for scheduler/in-process use only; CLI refuses it).",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Also render the full slippage/coverage reports and Notion sync.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use the deterministic in-memory demo provider (no broker/network).",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Logging level (DEBUG/INFO/WARNING/ERROR)."
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        if isinstance(exc.code, str) and exc.code.isdigit():
            return int(exc.code)
        return 1

    configure_logging(args.log_level)

    # A preliminary load, used ONLY to resolve `now` below (decision_time_et,
    # for the no-`--now` fallback) -- NOT the config the rest of this run
    # uses. Its own ml_sizing_adjustment staleness check (if any) was just
    # evaluated against the real wall clock, which is wrong for a replay
    # (`--now <past timestamp>`); reloaded below, correctly, once the actual
    # reference time is known (Codex review: "Validate ML staleness against
    # the run timestamp").
    preliminary_config = load_strategy_config(args.config_dir)
    if not preliminary_config.dry_run:
        logger.error(
            "Refusing to run: config.dry_run is False. The CLI is dry-run only; drive live runs via run_pipeline()."
        )
        return 2
    if args.slot is not None:
        logger.error(
            "Refusing to run: --slot is not supported from the CLI. "
            "Scheduler/in-process runners must call run_dca.run_slot()."
        )
        return 2

    now = _resolve_now(args.now, preliminary_config)
    config = load_strategy_config(args.config_dir, now=now)
    trading_day = now.astimezone(EASTERN).date()
    artifacts_dir = (
        Path(args.artifacts_dir)
        if args.artifacts_dir
        else Path("artifacts") / trading_day.isoformat()
    )

    if not args.demo:
        logger.error(
            "No live data providers available to the CLI. Run with --demo for a local dry-run, "
            "or drive a live run from the scheduler via run_dca.run_pipeline() with wired MCP callables."
        )
        return 2

    logger.info("Starting DEMO dry-run for %s -> %s", trading_day, artifacts_dir)
    finance, robinhood, broker = _demo_runtime(config)
    state = StrategyState()
    # run_pipeline() defaults to scheduled=True (unset here, matching every
    # real scheduled invocation), and several trackers it touches along that
    # path -- InverseMartingaleLedger, CumulativeCreditLedger,
    # scripts/same_day_top_up_gate, etc. -- gate their bookkeeping on
    # `scheduled` alone, not `config.dry_run`. --artifacts-dir only isolates
    # THIS run's own run_state/report artifacts; dca_paths.stream_root()'s
    # separate $DCA_CRON_TRACKING_ROOT-derived tree those trackers read and
    # write is untouched by it, so an enrolled ticker's REAL, live
    # inverse-Martingale cycle (or credit ledger, or top-up-gate state)
    # would otherwise be read and MUTATED by this synthetic-data smoke test
    # -- directly contradicting both this module's own "Nothing contacts a
    # broker or the network" claim and AGENTS.md's "third standard
    # pre-handoff check" (Codex review: "Keep CLI demos out of the live
    # accumulator"). stream_root()/cron_tracking_root() read
    # $DCA_CRON_TRACKING_ROOT at CALL time, not at dca_paths' own import
    # time (see that module's own "Resolution timing" docstring section),
    # so overriding it here -- for exactly the duration of this one
    # run_pipeline() call, restored immediately after -- correctly isolates
    # every stream tracker this demo run touches without threading a new
    # override parameter through run_pipeline's own multi-layer call chain.
    # Restored (not left set) because main() is also called in-process,
    # repeatedly, from test_run_dca.py -- an unrestored override would leak
    # into whichever test runs next in the same pytest process.
    _demo_cron_tracking_root_backup = os.environ.get(dca_paths.CRON_TRACKING_ROOT_ENV)
    with tempfile.TemporaryDirectory(prefix="dca-demo-cron-tracking-") as demo_cron_tracking_root:
        os.environ[dca_paths.CRON_TRACKING_ROOT_ENV] = demo_cron_tracking_root
        try:
            result = run_pipeline(
                now,
                config,
                state,
                finance,
                robinhood,
                broker,
                artifacts_dir=artifacts_dir,
                run_label=args.run_label,
                generate_full_reports=args.report,
            )
        finally:
            if _demo_cron_tracking_root_backup is None:
                os.environ.pop(dca_paths.CRON_TRACKING_ROOT_ENV, None)
            else:
                os.environ[dca_paths.CRON_TRACKING_ROOT_ENV] = _demo_cron_tracking_root_backup
    logger.info("Done. Artifacts:\n%s", json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
