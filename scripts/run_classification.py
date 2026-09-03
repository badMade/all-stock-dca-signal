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
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set
from zoneinfo import ZoneInfo

from dca_fileio import atomic_write_text

# Aliased: this module defines its own cron_tracking_root(base=...) below
# (a distinct signature -- this one supports a test-only base override),
# which shadows this import at module scope once that def executes. The
# alias lets the local function still reach dca_paths's version at CALL
# time instead of recursing into itself.
from dca_paths import cron_tracking_root as _dca_cron_tracking_root
from dca_paths import stream_root

# --- Contract constants (mirror config/run_classification.json) --------------

STATE_SCHEMA_VERSION = "run-classification-v1"
# Stricter successor introduced 2026-07-10 for the pre-authorized direct-placement
# live task (no review_equity_order): a scheduled run written under the
# pre-authorized execution policy tags itself v2. Both versions are accepted for
# scheduled/manual evidence as long as the required scheduled identity fields are
# correct; v2 is treated as a stricter successor of v1, not a different contract.
STATE_SCHEMA_VERSION_V2 = "run-classification-v2-preauthorized"
# The in-repo market_open/intraday executors (morning_dca.MorningDCAExecutor
# subclasses) stamp their own state_schema_version rather than one of the two
# generic values above; both must be accepted here too or their scheduled
# artifacts fail validate_classification() on version alone before the
# stream-specific identity checks even run (Codex review: "Register intraday
# identities with the runtime validator").
MARKET_OPEN_LIVE_SCHEMA_VERSION = "market-open-live-v1"
INTRADAY_LIVE_SCHEMA_VERSION = "intraday-live-v1"
ACCEPTED_STATE_SCHEMA_VERSIONS = (
    STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION_V2,
    MARKET_OPEN_LIVE_SCHEMA_VERSION,
    INTRADAY_LIVE_SCHEMA_VERSION,
)
# run_origin of a pass that recovers a FAILED scheduled slot into its own
# directory (e.g. the Jul 27 2026 12:30 pass, config/scheduled_tasks.json ->
# midday_dca_scheduled_recovery_2026_07_27). Deliberately distinct from
# 'scheduled': such a pass is never scheduled evidence for the slot it recovered
# -- it is same-day, ticker-scoped top-up context only. Its state_schema_version
# is likewise kept OUT of ACCEPTED_STATE_SCHEMA_VERSIONS above so no
# scheduled-evidence consumer can ingest it; see
# schemas/recovery_run_classification.schema.json for what it must validate
# against instead.
RUN_ORIGIN_SCHEDULED_RECOVERY = "scheduled_recovery"

# The America/New_York trading day format both recovery schemas require. A
# non-blank check alone accepted trading_day='bogus', so a caller that omitted
# the expected day -- the DEFAULT for collect_recovery_context_order_ids() --
# trusted a schema-invalid state and excluded its order IDs from watchdog
# discrepancy reporting (Codex review: "Validate the recovery trading-day
# format"). Shape only; a real-calendar check is the caller's business.
# \Z, not $: in Python `$` also matches immediately before a trailing newline,
# so '2026-07-27\n' would satisfy an otherwise-anchored pattern while the
# schema's anchored JSON regex rejects it.
TRADING_DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

# HH:MM Eastern wall clock, matching the recovery schema's pattern for
# scheduled_slot_et / recovery_pass_started_et. \Z rather than $ for the same
# trailing-newline reason as TRADING_DAY_RE.
ET_CLOCK_RE = re.compile(r"^[0-9]{2}:[0-9]{2}\Z")

# The scheduled-recovery identity contract, keyed by recovery stream. Each entry
# is (run_type, state_schema_version); a conforming classification must carry
# ALL THREE of the matching values, so a stream cannot be paired with another
# stream's run_type or schema version. Mirrors the enums in
# schemas/recovery_run_classification.schema.json -- test_run_classification.py
# asserts the two stay in sync, so this cannot silently drift from the schema.
#
# No afternoon entry: an afternoon recovery is the afternoon run's own repair,
# never prior context for it.
# The intraday stream carries a per-slot suffix (the 11:30/13:30/14:30 slots of
# task 7be82b97), so its recovery stream is slot-qualified too --
# 'intraday_1430_recovery' is what the Jul 27 2:32 PM recovery pass actually
# persisted. A bare 'intraday_recovery' is accepted as well for a pass that does
# not name its slot. Enumerating the slots rather than pattern-matching keeps
# this table, the schema enums, and the pointer streams a single finite set that
# tests can hold in sync (Codex review: "Use a recovery stream accepted by the
# recovery contract").
_INTRADAY_RECOVERY_CONTRACT = ("intraday_dca_live_recovery", "intraday-live-v2-recovery")
INTRADAY_RECOVERY_SLOTS = ("1130", "1330", "1430")
RECOVERY_STREAM_CONTRACTS = {
    "market_open_recovery": ("market_open_dca_live_recovery", "market-open-live-v2-recovery"),
    "morning_recovery": ("morning_dca_live_recovery", "morning-live-v2-recovery"),
    "midday_recovery": ("midday_dca_live_recovery", "midday-live-v2-recovery"),
    "intraday_recovery": _INTRADAY_RECOVERY_CONTRACT,
    **{
        f"intraday_{slot}_recovery": _INTRADAY_RECOVERY_CONTRACT
        for slot in INTRADAY_RECOVERY_SLOTS
    },
}
RECOVERY_STREAMS = tuple(RECOVERY_STREAM_CONTRACTS)
RECOVERY_RUN_TYPES = tuple(
    dict.fromkeys(run_type for run_type, _ in RECOVERY_STREAM_CONTRACTS.values())
)
RECOVERY_STATE_SCHEMA_VERSIONS = tuple(
    dict.fromkeys(version for _, version in RECOVERY_STREAM_CONTRACTS.values())
)


def is_valid_recovery_context_state(
    state: Optional[Mapping[str, Any]], trading_day: Optional[str] = None
) -> bool:
    """THE state-level scheduled-recovery check. Single source of truth.

    Wraps :func:`validate_recovery_classification` with the checks that belong
    to the whole run_state rather than its classification block:

    * a top-level ``artifact_contract_failure`` marker fails closed, exactly as
      it does for scheduled evidence in
      ``same_day_pointer.derive_valid_scheduled_evidence``. A classification-only
      check would accept a contract-failed artifact as prior context, letting
      its fills reduce or block a later allocation and letting its order IDs be
      classified as recovery context and hidden from discrepancy reporting
      (Codex review: "Reject artifact-contract failures as recovery context");
    * the classification's ``trading_day`` must agree with the state's own
      top-level ``trading_day`` when the state carries one, so an artifact
      accepted on today's state day cannot name a different day in its
      classification.

    Every state-level consumer should call THIS rather than reaching for the
    classification-only validator: the afternoon top-up guard, the recovery
    pointer validator, and :func:`is_top_up_context_state` all do.
    """
    if not isinstance(state, Mapping):
        return False
    if state.get("artifact_contract_failure") is not None:
        return False
    # A simulated run moved no capital, so its "fills" are not recovery context
    # and its order IDs are not real broker orders. near_close_dca's allocation
    # loader already skips dry runs; without this, validate_recovery_pointer
    # advertised the simulation as usable context and
    # collect_recovery_context_order_ids could classify simulated IDs as
    # recovery orders, hiding genuinely unexplained ones (Codex review: "Reject
    # dry runs as valid recovery context").
    # `is True` rejected only the literal boolean, so a schema-invalid dry_run=1
    # or dry_run="false" was accepted as real recovery context here while the
    # allocation loaders -- which test truthiness -- skipped those same states.
    # That is the consumer disagreement the dry-run check was added to close, so
    # a PRESENT dry_run must be boolean False and nothing else (Codex review:
    # "Reject non-boolean dry-run markers").
    if "dry_run" in state and state.get("dry_run") is not False:
        return False

    # The top-level state day is validated WHENEVER present, not only as a
    # fallback when no expected day was supplied. Deriving `day` from
    # trading_day-or-state meant a caller passing trading_day=today never had
    # the state's own day inspected, so a state whose classification said today
    # but whose top-level day was stale still passed -- exactly the divergence
    # this state-level validator exists to prevent (Codex review: "Compare the
    # top-level state day when an expected day is supplied").
    # Same rule as the classification day below: the PERSISTED value is checked,
    # never a stripped copy, so a padded ' 2026-07-27 ' cannot be repaired into
    # validity here while the schema rejects it.
    # REQUIRED, not merely validated-when-present.
    # near_close_dca.load_prior_same_day_allocations filters on the top-level
    # day outright (`if run_state.get("trading_day") != trading_day_iso:
    # continue`), so a recovery state without one is rejected there. Treating
    # its absence as acceptable here let collect_recovery_context_order_ids()
    # classify that artifact's broker IDs as explained while the allocation
    # loader omitted its deployed capital -- the order looks accounted for and
    # the ticker gets bought again (Codex review: "Require a top-level trading
    # day on recovery states"). The two consumers must agree on which artifacts
    # exist.
    state_day = state.get("trading_day")
    if not isinstance(state_day, str) or not TRADING_DAY_RE.match(state_day):
        return False
    if trading_day is not None and state_day != trading_day:
        return False

    day = trading_day or state_day
    classification = state.get("run_classification")
    classification_map = classification if isinstance(classification, Mapping) else None
    return validate_recovery_classification(classification_map, day)


def validate_recovery_classification(
    classification: Optional[Mapping[str, Any]], trading_day: Optional[str] = None
) -> bool:
    """THE scheduled-recovery contract check. Single source of truth.

    Every consumer that decides whether a recovery artifact may be trusted --
    the afternoon top-up guard (``near_close_dca._is_scheduled_recovery_context``),
    the recovery pointer validator
    (``scripts.same_day_pointer.derive_valid_recovery_context``), and
    :func:`is_top_up_context_state` -- delegates here rather than re-deriving
    the rules. Three separate hand-rolled copies had already drifted apart into
    different subsets of the contract, each trusting artifacts the others
    rejected (Codex review, several rounds: "Validate all required recovery
    fields", "Enforce the full recovery contract before loading fills",
    "Fully validate states before excluding recovery orders").

    Requires, all as EXPLICIT values so a partial or truncated classification
    fails closed:

    * ``run_origin='scheduled_recovery'`` and ``manual_run=false``;
    * ``valid_recovery_context=true``, ``same_day_top_up_context_only=true``,
      and ``valid_scheduled_evidence=false``;
    * ``recovery_rerun`` absent, or present as boolean ``false`` -- a
      separate-directory pass never satisfies failure_recovery_policy's in-place
      recovery_rerun contract. An explicit ``null``/``0``/``0.0`` is a present
      non-boolean and is rejected, not read as absence;
    * ``stream``/``run_type``/``state_schema_version`` all present AND mutually
      consistent per :data:`RECOVERY_STREAM_CONTRACTS`, so arbitrary values such
      as ``run_type='bogus'`` are rejected rather than merely non-blank;
    * a non-blank ``run_id``;
    * a ``trading_day`` on the CLASSIFICATION itself (a top-level state
      ``trading_day`` does not substitute), matching ``trading_day`` when given.

    Passing here NEVER makes the artifact scheduled evidence. It remains valid
    only as same-day, ticker-scoped top-up context for later same-day streams.
    """
    if not isinstance(classification, Mapping):
        return False
    if classification.get("run_origin") != RUN_ORIGIN_SCHEDULED_RECOVERY:
        return False
    if classification.get("manual_run") is not False:
        return False
    if classification.get("valid_recovery_context") is not True:
        return False
    if classification.get("same_day_top_up_context_only") is not True:
        return False
    if classification.get("valid_scheduled_evidence") is not False:
        return False
    # `not in (None, False)` compared by EQUALITY, so 0, 0.0, and an explicitly
    # persisted JSON null all passed as if the field were absent or boolean
    # false -- the schema permits only boolean false when the property is
    # present. Absence is checked by key membership; a present value must be
    # `is False` (Codex review: "Reject non-boolean recovery-rerun values").
    if "recovery_rerun" in classification and classification.get("recovery_rerun") is not False:
        return False
    # Optional proof flags. The schema pins each to an exact constant when
    # present, so a non-boolean (the STRING "false", 0, None) is schema-invalid
    # too -- rejecting only a literal True left such an artifact trusted here
    # while a schema-aware consumer refused it. exclude_from_watchdog_scheduled_confirmation
    # is pinned to TRUE: a recovery pass that claims it may confirm the
    # scheduled slot ran is contradicting the whole contract (Codex review:
    # "Enforce all optional recovery-proof flags").
    for flag, required in (
        ("valid_afternoon_scheduled_evidence", False),
        ("use_for_slippage_scheduled_proof", False),
        ("exclude_from_watchdog_scheduled_confirmation", True),
    ):
        # Key ABSENCE is the only exemption. `.get(flag) is not None` made a
        # missing key and an explicitly persisted JSON null indistinguishable,
        # so a null passed while the schema requires the boolean constant
        # whenever the property is present (Codex review: "Reject explicit null
        # recovery proof flags").
        if flag not in classification:
            continue
        if classification.get(flag) is not required:
            return False

    # isinstance-check before the dict lookup: persisted JSON can hold a list or
    # object here, and an unhashable key raises TypeError rather than returning
    # False. Every caller runs this over artifacts read off disk on a
    # safety-critical path, so one corrupt file would abort the afternoon guard,
    # the pointer validator, and the watchdog collector instead of failing
    # closed as this contract promises (Codex review: "Reject unhashable
    # recovery stream values").
    stream = classification.get("stream")
    if not isinstance(stream, str):
        return False
    contract = RECOVERY_STREAM_CONTRACTS.get(stream)
    if contract is None:
        return False
    expected_run_type, expected_version = contract
    if classification.get("run_type") != expected_run_type:
        return False
    if classification.get("state_schema_version") != expected_version:
        return False

    run_id = classification.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return False

    # Checked against the PERSISTED value, not day.strip(). The schema's pattern
    # is anchored, so ' 2026-07-27 ' is schema-invalid; stripping it here meant
    # this validator accepted an artifact a schema-aware consumer refuses, which
    # is the divergence the whole contract exists to prevent. Validation must
    # not repair malformed metadata (Codex review: "Reject padded recovery
    # trading days").
    day = classification.get("trading_day")
    if not isinstance(day, str) or not TRADING_DAY_RE.match(day):
        return False
    if trading_day is not None and day != trading_day:
        return False

    # Optional metadata the schema CONSTRAINS when present. Returning true
    # without checking these left scheduled_slot_et='bogus',
    # recovery_pass_started_et={} and artifact_root=[] trusted by the allocation
    # loader, the pointer validator, and the watchdog collector while a
    # schema-aware consumer rejected the same artifact -- the divergence this
    # validator exists to prevent (Codex review: "Validate optional recovery
    # fields before returning true"). Absence remains fine; a PRESENT value must
    # satisfy the schema.
    for field_name in ("scheduled_slot_et", "recovery_pass_started_et"):
        if field_name not in classification:
            continue
        value = classification.get(field_name)
        if not isinstance(value, str) or not ET_CLOCK_RE.match(value):
            return False
    for field_name in ("cron_id", "recovered_slot_artifact_root", "artifact_root"):
        if field_name not in classification:
            continue
        if not isinstance(classification.get(field_name), str):
            return False
    return True
# Repaired live task id. The deleted ce16ba6b is superseded by 10ce362c; the
# artifact root stays under .../ce16ba6b for continuity (see STATE_ROOT).
CRON_ID = "10ce362c"
SUPERSEDES_CRON_ID = "ce16ba6b"
SCHEDULED_RUN_TYPE = "scheduled_live_test"
SCHEDULED_FOR_ET = "15:30"
SCHEDULED_WINDOW_START_ET = "15:25"
SCHEDULED_WINDOW_END_ET = "15:35"
# 10ce362c was retimed from 3:20 PM to the canonical 3:30 PM ET on 2026-07-27. Runs
# that fired before then carry the old identity, so validation accepts it as legacy
# and existing artifacts stay valid. Writers only ever emit the canonical values.
LEGACY_SCHEDULED_FOR_ET = "15:20"
LEGACY_SCHEDULED_WINDOW_START_ET = "15:15"
# The seven-slot roster's own morning/midday identities (run_dca.run_slot()'s
# 1030ET/1230ET via scripts.run_classification.build_scheduled_classification's
# stream override -- see run_dca._SLOT_CRON_ID). validate_classification below
# accepts these as their OWN coherent identity, scoped to stream="morning"/
# "midday" so they are never accepted as afternoon lineage.
AFTERNOON_STREAM = "afternoon"
MORNING_SLOT_STREAM = "morning"
MORNING_SLOT_CRON_ID = "42173154"
MORNING_SLOT_SCHEDULED_FOR_ET = "10:30"
MORNING_SLOT_SCHEDULED_WINDOW_START_ET = "10:25"
MORNING_SLOT_SCHEDULED_WINDOW_END_ET = "10:35"
MIDDAY_SLOT_STREAM = "midday"
MIDDAY_SLOT_CRON_ID = "a6239dae"
MIDDAY_SLOT_SCHEDULED_FOR_ET = "12:30"
MIDDAY_SLOT_SCHEDULED_WINDOW_START_ET = "12:25"
MIDDAY_SLOT_SCHEDULED_WINDOW_END_ET = "12:35"
# The in-repo executor streams (market_open_dca.py / intraday_dca.py, both
# subclassing morning_dca.MorningDCAExecutor). Unlike MORNING_SLOT_STREAM/
# MIDDAY_SLOT_STREAM above (the seven-slot roster's run_dca.run_slot() path,
# which reuses the afternoon task's own run_type/state_schema_version via
# build_scheduled_classification), these two streams are their own executor
# classes with their own run_type and state_schema_version -- neither matches
# SCHEDULED_RUN_TYPE nor the generic ACCEPTED_STATE_SCHEMA_VERSIONS tuple, so
# they need their own dedicated identity checks below rather than reusing the
# morning/midday branch shape (Codex review: "Register intraday identities
# with the runtime validator" -- the standalone
# schemas/intraday_run_classification.schema.json already validated this
# shape, but this runtime function, which is what dca_risk.classify_stream()
# actually calls, did not).
MARKET_OPEN_STREAM = "market_open"
MARKET_OPEN_LIVE_CRON_ID = "2ba19526"
MARKET_OPEN_LIVE_RUN_TYPE = "market_open_dca_live"
MARKET_OPEN_LIVE_SCHEDULED_FOR_ET = "09:35"
MARKET_OPEN_LIVE_SCHEDULED_WINDOW_START_ET = "09:30"
MARKET_OPEN_LIVE_SCHEDULED_WINDOW_END_ET = "09:50"
INTRADAY_STREAM = "intraday"
INTRADAY_LIVE_CRON_ID = "7be82b97"
INTRADAY_LIVE_RUN_TYPE = "intraday_dca_live"
# Date the retime took effect, as the same zero-padded YYYYMMDD string used in
# run_id (so it can be compared lexicographically without a date parse). The
# legacy identity tuple is accepted ONLY for a run_id dated strictly before
# this: a run dated on/after 2026-07-27 must use the canonical tuple, so a
# stale or incorrectly written post-retime legacy artifact cannot be treated
# as valid scheduled evidence and mask a missed canonical run (Codex review
# r3654117261: "Restrict legacy identities to pre-retime dates").
CANONICAL_RETIME_DATE_YYYYMMDD = "20260727"
# Order-attribution window. Deliberately wider than the current classification
# window so orders from legacy 3:20 PM runs are still attributed to their run.
ORDER_ATTRIBUTION_WINDOW_START_ET = LEGACY_SCHEDULED_WINDOW_START_ET

# Artifact root remains the ce16ba6b path for continuity across the task repair.
STATE_ROOT = stream_root("ce16ba6b")
# Parent directory holding every per-task cron_tracking tree (ce16ba6b, morning_dca,
# midday_dca, final_manual_review, ...). Used to reach the out-of-task manual-exclusion
# roots below.
CRON_TRACKING_ROOT = _dca_cron_tracking_root()
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
# (67aa802b) and weekly/monthly reporting must treat every run_state/orders CSV under
# these roots as manual and exclude their order IDs from all scheduled-live metrics.
# (The 3:40 PM watchdog, a46bf007, also did until it was removed from the schedule
# entirely on 2026-08-02.) Paths are relative to CRON_TRACKING_ROOT.
MANUAL_EXCLUSION_SUBROOTS = (
    "final_manual_review",
    "midday_dca/manual_reruns",
    "morning_dca/manual_recovery",
    "midday_dca/manual_recovery",
    "midday_dca/manual_runs",
)

SCHEDULED_RUN_ID_RE = re.compile(r"^scheduled_[0-9]{8}_1530ET$")
# Pre-2026-07-27 legacy id, paired with LEGACY_SCHEDULED_FOR_ET/LEGACY_SCHEDULED_WINDOW_START_ET
# below as one coherent legacy identity tuple -- see validate_classification's
# combined identity check. Captures the YYYYMMDD group so that check can also
# enforce CANONICAL_RETIME_DATE_YYYYMMDD.
LEGACY_SCHEDULED_RUN_ID_RE = re.compile(r"^scheduled_([0-9]{8})_1520ET$")
MORNING_SLOT_RUN_ID_RE = re.compile(r"^scheduled_[0-9]{8}_1030ET$")
MIDDAY_SLOT_RUN_ID_RE = re.compile(r"^scheduled_[0-9]{8}_1230ET$")
MARKET_OPEN_LIVE_RUN_ID_RE = re.compile(r"^market_open_live_[0-9]{8}_0935ET$")
# Keyed by scheduled_for_et: intraday is a single stream shared across all
# three fire times (morning_dca.MorningDCAExecutor.run_id() stamps the slot
# into both scheduled_window_start_et/end_et and run_id via the profile's
# folder_suffix), so a coherent identity check needs the slot-specific window
# and run_id shape that scheduled_for_et actually selects, not one fixed tuple.
INTRADAY_SLOT_IDENTITIES: Dict[str, tuple] = {
    "11:30": ("11:25", "11:35", re.compile(r"^intraday_live_[0-9]{8}_1130ET$")),
    "13:30": ("13:25", "13:35", re.compile(r"^intraday_live_[0-9]{8}_1330ET$")),
    "14:30": ("14:25", "14:35", re.compile(r"^intraday_live_[0-9]{8}_1430ET$")),
}
# Accepts the canonical 1530ET id, the legacy 1520ET id (pre-2026-07-27 runs),
# and the seven-slot roster's own 1030ET/1230ET ids -- every official
# scheduled-slot run_id shape, so a manual run can never claim one of these
# (Codex review: "Exclude the new scheduled IDs from manual classifications",
# mirrored from the same fix in schemas/run_classification.schema.json).
SCHEDULED_RUN_ID_ANY_RE = re.compile(r"^scheduled_[0-9]{8}_(?:15(?:3|2)0|1030|1230)ET$")

# order_submit_status values (mirror schemas/run_state.schema.json). The scheduled
# 3:30 task always writes a run_state (and updates the pointer) even when nothing is
# submitted; this field records why.
ORDER_SUBMIT_STATUS_SUBMITTED = "submitted"
ORDER_SUBMIT_STATUS_NO_CANDIDATES = "not_submitted_no_candidates"
# Robinhood review passed but submission was blocked by required post-review user
# confirmation: valid scheduled-run evidence the task ran, but NOT an order/fill.
ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED = "not_submitted_confirmation_required"
ORDER_SUBMIT_STATUS_ERROR = "not_submitted_error"
ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED = "not_submitted_budget_exhausted"
ORDER_SUBMIT_STATUS_DRY_RUN = "dry_run"
# A run that fired, evaluated every ticker, and deliberately submitted nothing --
# distinct from an error and from budget exhaustion. See classify_no_order_run.
ORDER_SUBMIT_STATUS_NO_ORDERS = "no_orders_submitted"

# --- No-order outcome contract (config/scheduled_tasks.json:
# spread_data_availability_policy) -------------------------------------------
#
# A scheduled run that submits nothing is NOT a failure. The Jul 27 2026 1:30 PM
# run recorded error_class='missing_spread_data' when in fact no ticker had
# qualified on the ladder at all, so nothing ever reached the spread gate: the
# missing spread was irrelevant to the outcome. The policy fixes the evaluation
# order -- ladder FIRST, then the spread gate -- and this module is the single
# place that turns those two counts into a recorded outcome.
RUN_CLASSIFICATION_COMPLETED_NO_ORDERS = "completed_no_orders"
RUN_CLASSIFICATION_PLACEMENT_FAILED = "failed_after_connector_native_recovery_attempt"
# A run that never reached placement: it raised at startup, or a precondition /
# wiring gate stopped it (missing same-day pre-flight artifact, absent broker,
# wrong live account). Distinct from RUN_CLASSIFICATION_PLACEMENT_FAILED, which
# means placement WAS attempted and failed: reporting keyed on that outcome would
# otherwise misattribute a startup, data, or precondition failure as a broker
# placement failure. The specific cause is carried by error_class.
RUN_CLASSIFICATION_STARTUP_FAILED = "failed_scheduled_run_startup"
# A run that raised AFTER submitting at least one order. Distinct from
# RUN_CLASSIFICATION_STARTUP_FAILED so the aggregate outcome never contradicts a
# non-zero orders_submitted_count and a submitted_orders.csv row.
RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION = "failed_after_partial_submission"
# Placement failure on a MANUAL run. The manual path never runs the scheduled
# connector-native recovery policy, so recording its broker failures under
# RUN_CLASSIFICATION_PLACEMENT_FAILED would misattribute an ordinary manual
# failure as a failed scheduled recovery attempt.
RUN_CLASSIFICATION_MANUAL_PLACEMENT_FAILED = "failed_placement"

# completion_reason values for a completed_no_orders run (error_class stays null).
COMPLETION_REASON_NO_LADDER_CANDIDATES = "no_ladder_candidates"
COMPLETION_REASON_NO_QUALIFYING_ORDERS = "no_qualifying_orders_after_safety_checks"
# Candidates DID qualify on the ladder but every one was stopped by a safety or
# budget check that runs BEFORE the spread lookup (daily/weekly cap, per-order
# ceiling, already-bought-today, same-day duplicate). Recording that as
# 'no_ladder_candidates' would claim the ladder produced nothing when it did.
COMPLETION_REASON_BLOCKED_BEFORE_SPREAD_GATE = "blocked_before_spread_gate"
# The date/holiday gate stopped the run before any evaluation.
COMPLETION_REASON_DATE_GATE_BLOCKED = "date_or_holiday_gate_blocked"

# error_class for a run whose candidates DID reach the spread gate and were all
# failed closed there. Never used when no candidate reached the gate.
ERROR_CLASS_MISSING_SPREAD_DATA = "missing_spread_data"
# A run that completed with no orders while market-data providers were raising:
# tickers degraded to NO_DATA/manual review rather than being genuinely evaluated,
# so the zero-order result is NOT a clean ladder no-op and must not read as one.
ERROR_CLASS_DEGRADED_MARKET_DATA = "degraded_market_data"
# At least one order landed and another ticker's placement raised in the same
# batch. The run is a partial success AND a failure; both must be recorded.
ERROR_CLASS_PARTIAL_PLACEMENT_FAILURE = "partial_placement_failure"
# A scheduled live run that raised before reaching any writer.
ERROR_CLASS_STARTUP_FAILURE = "scheduled_run_startup_failure"
# Raw connector responses were missing, unwritable, or failed normalization, so
# the run fail-closed before computing anything. Mirrors the concrete code the
# morning runner already emits (morning_dca's missing_or_invalid_raw_inputs).
ERROR_CLASS_MISSING_RAW_INPUTS = "missing_or_invalid_raw_inputs"
# The single permitted recovery attempt itself failed, or a safety/account/order
# rejection ended it. Distinct from the raw-input class: the cause is the failed
# recovery, not the inputs.
ERROR_CLASS_RECOVERY_FAILURE = "recovery_failure"
# A run that raised AFTER at least one order was submitted. Kept distinct from
# the startup class so a reliability consumer grouping by error_class never
# reports a late post-trade failure as startup trouble.
ERROR_CLASS_POST_SUBMISSION_FAILURE = "scheduled_run_failed_after_submission"
# A scheduled live run stopped by a precondition or wiring gate before placement:
# no same-day pre-flight artifact, no broker adapter, or the wrong live account.
ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT = "scheduled_run_blocked_before_placement"

# Marker stamped on a candidate outcome by the spread gate itself, so the run
# classifier counts a row as spread-blocked only when the spread gate is what
# blocked it. Inferring that from ``spread_status`` alone is wrong on a manual
# run, where an unproven spread does NOT block placement and the row can later be
# stopped by human review while still carrying spread_status='spread_unavailable'.
BLOCKED_BY_SPREAD_GATE = "spread_gate"

# Row-level spread outcome recorded on every evaluated signal/candidate row.
SPREAD_STATUS_OK = "spread_ok"
SPREAD_STATUS_UNAVAILABLE = "spread_unavailable"
SPREAD_STATUS_NOT_OK = "spread_not_ok"
SPREAD_STATUS_NOT_EVALUATED = "not_evaluated"
# Legacy alias persisted by the Jul 27 2026 intraday runs; accepted on read.
SPREAD_STATUS_MISSING_BID_ASK = "missing_bid_ask"
SPREAD_UNAVAILABLE_STATUSES = frozenset(
    {SPREAD_STATUS_UNAVAILABLE, SPREAD_STATUS_MISSING_BID_ASK}
)


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


def _et_minutes(value: Any) -> Optional[int]:
    """Minutes-since-midnight in **ET** for an order timestamp.

    Accepts a bare ET wall-clock ('15:30', '15:30:33'), a naive ISO datetime
    (assumed already ET), or a tz-aware ISO string / ``datetime`` — which is
    converted to America/New_York first, so a UTC ``'...T19:30:00Z'`` correctly
    reads as 15:30 ET rather than being dropped. Returns None if unparseable.
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


def _et_seconds(value: Any) -> Optional[int]:
    """Seconds-since-midnight in **ET** for an order timestamp.

    Same source handling as :func:`_et_minutes`, but preserves sub-minute
    precision -- needed when comparing recency for same-day reference merging
    (see :func:`_merge_prior`): two manual artifacts generated in the same ET
    minute at different seconds must not collapse to a tie decided by scan
    order (Codex review: "Retain sub-minute generated-at ordering").
    """
    dt = value if isinstance(value, datetime) else _parse_iso_datetime(str(value))
    if dt is not None:
        if dt.tzinfo is not None:
            dt = dt.astimezone(_EASTERN)
        return dt.hour * 3600 + dt.minute * 60 + dt.second
    # Bare 'HH:MM[:SS]' wall-clock with no date -> treated as ET.
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(value))
    if not match:
        return None
    hh, mm = int(match.group(1)), int(match.group(2))
    ss = int(match.group(3)) if match.group(3) else 0
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
        return None
    return hh * 3600 + mm * 60 + ss


_canonical_start = _et_minutes(SCHEDULED_WINDOW_START_ET)
assert _canonical_start is not None
_CANONICAL_WINDOW_START_MIN: int = _canonical_start

_legacy_start = _et_minutes(LEGACY_SCHEDULED_WINDOW_START_ET)
assert _legacy_start is not None
_LEGACY_WINDOW_START_MIN: int = _legacy_start

_end_min = _et_minutes(SCHEDULED_WINDOW_END_ET)
assert _end_min is not None
_WINDOW_END_MIN: int = _end_min


def _attribution_window_start_min(rc: Any) -> int:
    """Choose the order-attribution window start from the validated state's
    OWN scheduled identity, not a single window-wide constant.

    Codex review r3654117272 ("Select the fallback attribution window from
    the run identity"): the window previously always started at the wider
    legacy 15:15 boundary, even for a canonical 1530ET run whose canonical
    classification window starts at 15:25. That let an unrelated broker order
    submitted at 15:15-15:24 be wrongly attributed to a canonical run. Use
    15:25 whenever the validated state carries the canonical scheduled_for_et
    ('15:30'); fall back to the wider legacy 15:15 boundary only for a
    validated legacy ('15:20') identity (or anything unexpected, to stay at
    least as permissive as prior behavior for data this function has already
    accepted as a valid scheduled state).
    """
    if isinstance(rc, Mapping) and rc.get("scheduled_for_et") == SCHEDULED_FOR_ET:
        return _CANONICAL_WINDOW_START_MIN
    return _LEGACY_WINDOW_START_MIN


def _in_scheduled_window_et(submitted_at_et: Any, window_start_min: int) -> bool:
    """True only if an ET timestamp falls within the scheduled window (inclusive)."""
    minutes = _et_minutes(submitted_at_et)
    if minutes is None:
        return False
    return window_start_min <= minutes <= _WINDOW_END_MIN

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
    """'2026-07-06' -> 'scheduled_20260706_1530ET'."""
    compact = trading_day.replace("-", "")
    if not re.fullmatch(r"[0-9]{8}", compact):
        raise ValueError(f"trading_day must be YYYY-MM-DD, got {trading_day!r}")
    return f"scheduled_{compact}_1530ET"


def _scheduled_run_id_for_time(trading_day: str, scheduled_for_et: str) -> str:
    compact = trading_day.replace("-", "")
    if not re.fullmatch(r"[0-9]{8}", compact):
        raise ValueError(f"trading_day must be YYYY-MM-DD, got {trading_day!r}")
    hhmm = re.sub(r"[^0-9]", "", scheduled_for_et)
    if not re.fullmatch(r"[0-9]{4}", hhmm):
        return scheduled_run_id(trading_day)
    return f"scheduled_{compact}_{hhmm}ET"


def manual_run_id(triggered_at_utc: Optional[str] = None) -> str:
    """Distinct id that can never match the scheduled pattern."""
    ts = triggered_at_utc or _utc_now_iso()
    compact = re.sub(r"[^0-9]", "", ts)[:14] or "00000000000000"
    return f"manual_{compact[:8]}_{compact[8:14]}Z"


def build_scheduled_classification(
    trading_day: str,
    triggered_at_utc: Optional[str] = None,
    *,
    stream: str = "afternoon",
    cron_id: str = CRON_ID,
    supersedes_cron_id: Optional[str] = SUPERSEDES_CRON_ID,
    scheduled_for_et: str = SCHEDULED_FOR_ET,
    scheduled_window_start_et: str = SCHEDULED_WINDOW_START_ET,
    scheduled_window_end_et: str = SCHEDULED_WINDOW_END_ET,
    run_id: Optional[str] = None,
    valid_afternoon_scheduled_evidence: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return a fully-populated scheduled run_classification block.

    Stamps ``stream="afternoon"`` (this builder is the 15:30 ET scheduled live
    task) so ``scripts.same_day_pointer.select_same_day_run_state``'s
    direct-resolution path -- which fails closed when a target's own
    ``run_classification.stream`` is absent -- accepts a real scheduled
    afternoon run_state instead of rejecting it for a missing field (Codex
    review: "Accept the valid afternoon dated state").

    The keyword-only ``stream``/``cron_id``/``scheduled_for_et``/window/``run_id``
    parameters default to the afternoon identity above, reproducing the original
    unconditional return byte-for-byte for every existing caller. They exist so
    ``near_close_dca.build_run_artifacts`` can stamp a DIFFERENT roster slot's own
    identity (e.g. the 10:30/12:30 morning/midday live tasks, when routed through
    this same afternoon-shaped artifact writer via ``run_dca.run_slot``) instead
    of always claiming to be the 15:30 afternoon run -- which would misattribute
    ``cron_id``/``scheduled_for_et`` for telemetry, hide the slot's own fills from
    a later slot's prior-allocation scan (keyed off ``run_classification.stream``),
    and risk an early update of the afternoon ``latest_scheduled_run.json`` pointer
    (Codex review, discussion_r3679391261: "Stamp each slot with its own scheduled
    classification"). ``supersedes_cron_id`` is omitted entirely (rather than set
    to the afternoon-specific ``SUPERSEDES_CRON_ID``) when explicitly passed as
    ``None``, since it names a task lineage that is meaningless for another
    stream. Note ``scripts.run_classification.validate_classification`` (the
    strict validator gating ``update_latest_scheduled_run_pointer``) still only
    accepts the canonical afternoon identity -- a non-afternoon block from this
    function is valid scheduled evidence for ITS OWN stream but is correctly
    rejected as afternoon evidence, so it never updates that pointer.

    ``valid_afternoon_scheduled_evidence`` defaults to ``None`` (omitted from
    the block entirely), reproducing the unmodified afternoon path byte for
    byte -- omitted/true there means "counts as afternoon evidence" per this
    field's own documented default. A non-afternoon roster caller
    (``run_dca._slot_classification_overrides``) passes ``False`` explicitly,
    since a morning/midday-shaped block must never be mistaken for afternoon
    evidence by a flag-based slippage/reliability consumer that does not also
    cross-check ``stream`` (Codex review: "Deny afternoon evidence in
    non-afternoon schema branches").
    """
    block: Dict[str, Any] = {
        "run_origin": "scheduled",
        "run_type": SCHEDULED_RUN_TYPE,
        "cron_id": cron_id,
        "supersedes_cron_id": supersedes_cron_id,
        "scheduled_for_et": scheduled_for_et,
        "scheduled_window_start_et": scheduled_window_start_et,
        "scheduled_window_end_et": scheduled_window_end_et,
        "manual_run": False,
        "run_id": run_id or _scheduled_run_id_for_time(trading_day, scheduled_for_et),
        "stream": stream,
        "triggered_at_utc": triggered_at_utc or _utc_now_iso(),
        "state_schema_version": STATE_SCHEMA_VERSION,
    }
    if supersedes_cron_id is None:
        del block["supersedes_cron_id"]
    if valid_afternoon_scheduled_evidence is not None:
        block["valid_afternoon_scheduled_evidence"] = valid_afternoon_scheduled_evidence
    return block


def build_manual_classification(
    triggered_at_utc: Optional[str] = None, run_id: Optional[str] = None
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
    """Return a manual run_classification block for a final manual buy override.

    The final-buy behavior (full active-universe scan, trend filter ignored for the
    final-buy decision only) may be invoked manually after the scheduled 3:30 PM run.
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

def state_root(base: Optional[Path | str] = None) -> Path:
    # stream_root("ce16ba6b") at CALL time, not the import-time STATE_ROOT
    # constant: generate_credit_usage_proxy_report.py already resolves the
    # shared $DCA_CRON_TRACKING_ROOT override per call, so a
    # $DCA_CRON_TRACKING_ROOT set after this module's import left every
    # no-base write_run_artifacts()/pointer update here writing under the
    # OLD tree while that reader looked in the new one -- valid afternoon
    # scheduled evidence then appeared to be missing (Codex review, PR #219:
    # "Resolve the classification state root at call time").
    return Path(base) if base is not None else Path(stream_root("ce16ba6b"))


def scheduled_run_dir(trading_day: str, base: Optional[Path | str] = None) -> Path:
    compact = trading_day.replace("-", "")
    return state_root(base) / "scheduled_runs" / f"{compact}_1530ET"


def legacy_scheduled_run_dir(trading_day: str, base: Optional[Path | str] = None) -> Path:
    """The pre-retime (3:20 PM ET) scheduled run directory for ``trading_day``.

    10ce362c was retimed from 3:20 PM to the canonical 3:30 PM ET on
    2026-07-27 (see LEGACY_SCHEDULED_FOR_ET); every run before that date wrote
    its scheduled_runs/ directory under this legacy ``_1520ET`` suffix, not
    :func:`scheduled_run_dir`'s canonical ``_1530ET`` one. Writers must never
    use this -- they only ever emit the canonical directory -- it exists
    solely for reporting/read paths that need to locate a historical
    pre-retime artifact (see :func:`select_scheduled_state_for_reporting`).
    """
    compact = trading_day.replace("-", "")
    return state_root(base) / "scheduled_runs" / f"{compact}_1520ET"


def manual_run_dir(timestamp: str, base: Optional[Path | str] = None) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "", timestamp) or "unknown"
    return state_root(base) / "manual_runs" / safe


def latest_scheduled_run_path(base: Optional[Path | str] = None) -> Path:
    return state_root(base) / LATEST_SCHEDULED_RUN_FILENAME


def cron_tracking_root(base: Optional[Path | str] = None) -> Path:
    """Parent directory holding every per-task cron_tracking tree.

    With an explicit ``base`` (tests point ``base`` at a temporary ce16ba6b root) this
    is ``base.parent``; otherwise the canonical, freshly-resolved cron-tracking root.
    Used to reach the out-of-task manual-exclusion roots (``final_manual_review``,
    ``midday_dca/manual_reruns``).

    Resolves ``dca_paths.cron_tracking_root()`` at CALL time, not the
    import-time ``CRON_TRACKING_ROOT`` constant -- same staleness reasoning
    as :func:`state_root` above (Codex review, PR #219).
    """
    if base is not None:
        return Path(base).parent
    return Path(_dca_cron_tracking_root())


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
    return f"quotes_batch{n}.json"


def historicals_batch_filename(n: int) -> str:
    """Deterministic filename for a persisted historicals batch (1-based)."""
    return f"historicals_batch{n}.json"


def place_filename(symbol: str) -> str:
    """Deterministic filename for a persisted per-symbol place response."""
    safe = re.sub(r"[^0-9A-Za-z._-]", "", symbol.upper()) or "UNKNOWN"
    return f"place_{safe}.json"


def normalize_connector_payload(
    payload: Any, *, read_pointer: bool = True, base_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Normalize a raw connector response into an inline, parseable JSON object.

    Some connectors return a large result indirectly as
    ``{"output_path": "/.../foo.json", ...}`` (or with the equivalent
    ``current_session_context`` pointer) rather than inline data. Compute
    must read from persisted, parseable files -- not chase pointers at compute
    time -- so when a payload carries an ``output_path`` we resolve it to the
    referenced file's parsed contents (embedded under ``data``) while preserving
    the original pointer under ``output_path`` and flagging ``normalized=true``.

    A pointer that cannot be read/parsed raises ``RuntimeError`` so the
    caller knows immediately and cannot silently persist a broken payload.
    """
    if not isinstance(payload, Mapping):
        return {"data": payload, "normalized": False}

    result: Dict[str, Any] = dict(payload)
    pointer = result.get("output_path")
    if not (isinstance(pointer, str) and pointer):
        session_pointer = result.get("current_session_context")
        if isinstance(session_pointer, str) and session_pointer:
            pointer = session_pointer
            # Canonicalize the pointer name while retaining the connector's original
            # field.  Raw-input consumers have one pointer field to audit regardless
            # of which connector transport produced the response.
            result["output_path"] = session_pointer
    if not (read_pointer and isinstance(pointer, str) and pointer):
        if isinstance(pointer, str) and pointer:
            # Has an output_path but caller asked not to resolve it — flag as not normalized.
            result.setdefault("normalized", False)
        return result

    path = Path(pointer)
    if base_dir is not None and not path.is_absolute():
        path = Path(base_dir) / path
    try:
        result["data"] = json.loads(path.read_bytes())
        result["normalized"] = True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"normalize_connector_payload: could not read output_path {pointer!r}: {exc}"
        ) from exc
    return result


def save_raw_input(
    trading_day: str,
    filename: str,
    payload: Any,
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
    safe = Path(filename).name
    if not safe or safe != filename:
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
    atomic_write_text(path, json.dumps(to_write, indent=2) + "\n")
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
) -> Dict[str, Any]:
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
        "recovery_from_escalation": recovery_from_escalation,
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
    classification_overrides: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
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

    ``classification_overrides``/``correlation_id`` let a non-afternoon roster
    slot (``run_dca.run_slot``'s 1030ET/1230ET) stamp its OWN
    stream/cron_id/scheduled_for_et/run_id and deterministic correlation id
    onto this failure artifact, the same way ``near_close_dca.build_run_artifacts``
    does for the normal compact path. Without this, a 10:30/12:30 run whose raw
    persistence failed always stamped the afternoon cron/stream/time/run_id --
    misattributing watchdog/telemetry diagnosis for the slot that actually
    failed and losing its deterministic correlation id (Codex review: "Preserve
    slot identity on early failure artifacts").
    """
    out_dir = raw_dir if raw_dir is not None else raw_input_dir(trading_day, base)
    ts = triggered_at_utc or _utc_now_iso()
    _generated_at = generated_at or datetime.now(_EASTERN).strftime("%Y-%m-%dT%H:%M:%S")
    run_classification = build_scheduled_classification(trading_day, ts, **(classification_overrides or {}))
    state = {
        # Derived from run_classification's OWN run_id (identical to
        # scheduled_run_id(trading_day) for the unoverridden afternoon
        # default) rather than computed separately, so the top-level run_id
        # can never drift from the nested run_classification.run_id a
        # classification_overrides caller stamped.
        "run_id": run_classification["run_id"],
        "generated_at": _generated_at,
        "trading_day": trading_day,
        "broker": broker,
        "order_submit_status": ORDER_SUBMIT_STATUS_ERROR,
        # The run never reached placement, so it carries the same outcome fields
        # every other blocked-before-placement path stamps. Without these the
        # state and its summary pass validation while saying nothing about the
        # outcome, and could still update the latest-run pointer (Codex review).
        "run_classification_outcome": RUN_CLASSIFICATION_STARTUP_FAILED,
        "completion_reason": None,
        # A concrete cause, not just the generic outcome: build_run_summary only
        # mirrors error_class when present, so without it the state and summary
        # carry a startup-failure outcome with no identifiable failure mode and
        # error-class-based reporting cannot group it (Codex review).
        "error_class": ERROR_CLASS_MISSING_RAW_INPUTS,
        "is_trading_failure": True,
        # Deliberately NO orders_submitted_count. This state overwrites the
        # SLOT's canonical run_state.json, and a same-slot retry whose earlier
        # attempt already placed orders would have those orders erased by an
        # asserted zero: the reporting selector does not read the preserved
        # orders.csv and disables broker fallback for not_submitted_error, so
        # they would vanish from scheduled fill and slippage results. THIS
        # attempt placed nothing, but the slot's count is unknown here, and an
        # absent field is honest where a zero is wrong (Codex review). The caller
        # fills it in when it can establish the real figure.
        "candidates_reaching_spread_gate": 0,
        "candidates_blocked_missing_spread": 0,
        "budget": budget or {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 0},
        "counts": {"evaluated": 0, "buy_candidates": 0, "submitted": 0},
        "buy_candidates": [],
        "run_classification": run_classification,
        "raw_input_dir": str(out_dir),
        "raw_inputs_saved": [],
        "recovery_from_escalation": recovery_from_escalation,
        "raw_persistence_error": error,
    }
    if correlation_id is not None:
        state["correlation_id"] = correlation_id
    # The pointer writer publishes whatever this state derives, and the
    # derivation reads only run_origin/manual_run -- both of which say
    # "scheduled", because the slot really did fire. Denying evidence is what
    # separates "the slot fired and failed" from "the slot executed", and
    # without it a raw-persistence failure that never computed a signal is
    # published as a successful slot to every watchdog and reliability consumer
    # (Codex review). The same-day prior-allocation loader reads these
    # artifacts as context regardless of the marker, so denying evidence here
    # cannot hide a carried order from the top-up guard.
    return deny_scheduled_evidence(state)


def morning_preflight_blocked_run_state(
    trading_day: str,
    block_reason: str,
    *,
    triggered_at_utc: Optional[str] = None,
    generated_at: Optional[str] = None,
    broker: str = "robinhood",
    budget: Optional[Dict[str, object]] = None,
    classification_overrides: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
) -> Dict[str, object]:
    """Build a valid scheduled run_state for a missing/invalid morning preflight.

    Mirrors :func:`raw_persistence_failure_run_state`'s blocked-before-placement
    shape (``run_classification_outcome=RUN_CLASSIFICATION_STARTUP_FAILED``,
    ``is_trading_failure=True``, ``completion_reason=None``, evidence denied) --
    the same outcome ``morning_dca._apply_blocked_before_placement_fields``
    stamps for its own preflight-block path -- rather than the calendar gate's
    completed-no-orders shape: a missing safety prerequisite is a failure, not
    a clean no-op, and reporting it as one hides it from failure/watchdog
    consumers entirely (Codex review: "Classify missing preflight as a startup
    failure"). Deliberately its own function rather than reusing
    ``raw_persistence_failure_run_state`` directly: that function's
    ``raw_persistence_error`` field is a type discriminator several consumers
    (``dca_alerts``, ``generate_credit_usage_proxy_report``) key off of
    specifically for raw-persistence failures, and stamping it here would
    misreport an unrelated failure mode to those consumers.

    ``classification_overrides``/``correlation_id`` let a non-afternoon roster
    slot (``run_dca.run_slot``'s 1030ET) stamp its own stream/cron_id/
    scheduled_for_et/run_id and deterministic correlation id onto this failure
    artifact, the same way ``raw_persistence_failure_run_state`` does.
    """
    ts = triggered_at_utc or _utc_now_iso()
    _generated_at = generated_at or datetime.now(_EASTERN).strftime("%Y-%m-%dT%H:%M:%S")
    run_classification = build_scheduled_classification(trading_day, ts, **(classification_overrides or {}))
    state = {
        "run_id": run_classification["run_id"],
        "generated_at": _generated_at,
        "trading_day": trading_day,
        "broker": broker,
        "order_submit_status": ORDER_SUBMIT_STATUS_ERROR,
        "run_classification_outcome": RUN_CLASSIFICATION_STARTUP_FAILED,
        "completion_reason": None,
        "error_class": ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT,
        "is_trading_failure": True,
        "candidates_reaching_spread_gate": 0,
        "candidates_blocked_missing_spread": 0,
        "budget": budget or {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 0},
        "counts": {"evaluated": 0, "buy_candidates": 0, "submitted": 0},
        "buy_candidates": [],
        "run_classification": run_classification,
        "orders_submitted_count": 0,
        "block_reason": block_reason,
    }
    if correlation_id is not None:
        state["correlation_id"] = correlation_id
    return deny_scheduled_evidence(state)


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
) -> Dict[str, Any]:
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
            "persisted_raw_first": persisted_raw_first,
            "recomputed_from_persisted": recomputed_from_persisted,
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
    budget: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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
    state = {
        "run_id": scheduled_run_id(trading_day),
        "generated_at": _generated_at,
        "trading_day": trading_day,
        "broker": broker,
        "order_submit_status": ORDER_SUBMIT_STATUS_ERROR,
        # The run never reached placement, so it carries the same outcome fields
        # every other blocked-before-placement path stamps. Without these the
        # state and its summary pass validation while saying nothing about the
        # outcome, and could still update the latest-run pointer (Codex review).
        "run_classification_outcome": RUN_CLASSIFICATION_STARTUP_FAILED,
        "completion_reason": None,
        # A concrete cause, not just the generic outcome: build_run_summary only
        # mirrors error_class when present, so without it the state and summary
        # carry a startup-failure outcome with no identifiable failure mode and
        # error-class-based reporting cannot group it (Codex review).
        "error_class": ERROR_CLASS_RECOVERY_FAILURE,
        "is_trading_failure": True,
        # Deliberately NO orders_submitted_count. This state overwrites the
        # SLOT's canonical run_state.json, and a same-slot retry whose earlier
        # attempt already placed orders would have those orders erased by an
        # asserted zero: the reporting selector does not read the preserved
        # orders.csv and disables broker fallback for not_submitted_error, so
        # they would vanish from scheduled fill and slippage results. THIS
        # attempt placed nothing, but the slot's count is unknown here, and an
        # absent field is honest where a zero is wrong (Codex review). The caller
        # fills it in when it can establish the real figure.
        "candidates_reaching_spread_gate": 0,
        "candidates_blocked_missing_spread": 0,
        "budget": budget or {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 0},
        "counts": {"evaluated": 0, "buy_candidates": 0, "submitted": 0},
        "buy_candidates": [],
        "run_classification": build_scheduled_classification(trading_day, ts),
        "recovery_rerun": True,
        "recovery": {
            "trigger_reason": trigger_reason,
            "attempt": 1,
            "persisted_raw_first": persisted_raw_first,
            "recomputed_from_persisted": recomputed_from_persisted,
            "escalated": True,
            "blocking_reason": blocking_reason,
        },
    }
    # Same denial as the raw-persistence builder, and for the same reason: an
    # escalated recovery attempt fired but never completed the slot, so it must
    # not be published as the slot's valid scheduled evidence.
    return deny_scheduled_evidence(state)


# --- Validation --------------------------------------------------------------

@dataclass
class ValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # allow `if validate(...):`
        return self.ok


def validate_classification(rc: Optional[Mapping[str, Any]]) -> ValidationResult:
    """Validate a run_classification block against the contract.

    Mirrors run_classification.schema.json but usable at runtime with no
    third-party dependency, including the seven-slot roster's own morning/
    midday identities (run_dca.run_slot()'s 1030ET/1230ET) alongside the
    afternoon/legacy identity. This function -- not the JSON schema -- is
    what is_scheduled_state(), dca_risk.classify_stream(), and
    near_close_dca's scheduled-pointer-update gate actually call at runtime,
    so widening only the JSON schema left every one of those still rejecting
    every roster classification, scheduled or blocked (Codex review: "Mirror
    roster identities in the runtime validator").
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
        run_id = rc.get("run_id")
        run_id_is_str = isinstance(run_id, str)
        if not run_id_is_str:
            errors.append(f"scheduled run_id must be a string, got {run_id!r}")
        scheduled_for_et = rc.get("scheduled_for_et")
        window_start_et = rc.get("scheduled_window_start_et")
        stream = rc.get("stream")
        # Dispatch by stream FIRST, mirroring schemas/run_classification.schema.json's
        # unified per-slot anyOf branches (Codex review: "Mirror roster identities
        # in the runtime validator"). This function previously only ever accepted
        # the afternoon identity, so every 10:30/12:30 seven-slot-roster
        # classification (run_dca.run_slot()) failed here -- and is_scheduled_state(),
        # dca_risk.classify_stream(), and the pointer-update gate in
        # near_close_dca (which SKIPS its pointer update when this fails) all
        # rejected every roster classification, scheduled or blocked, as a
        # result. Dispatching by stream also means an afternoon-shaped record
        # that also claims stream="morning"/"midday" is validated against the
        # MORNING/MIDDAY branch instead of falling through to the afternoon
        # checks below, so it is correctly rejected there rather than accepted
        # as afternoon evidence.
        #
        # run_type is checked PER BRANCH, not once up front: the roster streams
        # (morning/midday/afternoon/legacy) all reuse the afternoon task's own
        # SCHEDULED_RUN_TYPE via build_scheduled_classification(), but
        # market_open_dca.py/intraday_dca.py are their own executor classes
        # with their own run_type (Codex review: "Register intraday identities
        # with the runtime validator") -- a single shared run_type check here
        # would reject every genuine market_open/intraday scheduled run before
        # its branch below ever ran.
        if stream == MARKET_OPEN_STREAM:
            if rc.get("run_type") != MARKET_OPEN_LIVE_RUN_TYPE:
                errors.append(f"market_open scheduled run_type must be {MARKET_OPEN_LIVE_RUN_TYPE!r}")
            if rc.get("cron_id") != MARKET_OPEN_LIVE_CRON_ID:
                errors.append(
                    f"market_open scheduled run must have cron_id={MARKET_OPEN_LIVE_CRON_ID!r}"
                )
            if supersedes is not None:
                errors.append(
                    "market_open scheduled run must not carry supersedes_cron_id "
                    "(that names the afternoon task's lineage)"
                )
            if rc.get("scheduled_window_end_et") != MARKET_OPEN_LIVE_SCHEDULED_WINDOW_END_ET:
                errors.append(
                    "market_open scheduled_window_end_et must be "
                    f"{MARKET_OPEN_LIVE_SCHEDULED_WINDOW_END_ET!r}"
                )
            is_market_open_identity = (
                scheduled_for_et == MARKET_OPEN_LIVE_SCHEDULED_FOR_ET
                and window_start_et == MARKET_OPEN_LIVE_SCHEDULED_WINDOW_START_ET
                and run_id_is_str
                and bool(MARKET_OPEN_LIVE_RUN_ID_RE.match(run_id))
            )
            if run_id_is_str and not is_market_open_identity:
                errors.append(
                    "market_open scheduled_for_et, scheduled_window_start_et, and run_id must "
                    f"form the coherent market_open tuple ({MARKET_OPEN_LIVE_SCHEDULED_FOR_ET!r}, "
                    f"{MARKET_OPEN_LIVE_SCHEDULED_WINDOW_START_ET!r}, "
                    "'market_open_live_YYYYMMDD_0935ET'); "
                    f"got scheduled_for_et={scheduled_for_et!r}, "
                    f"scheduled_window_start_et={window_start_et!r}, run_id={run_id!r}"
                )
        elif stream == INTRADAY_STREAM:
            if rc.get("run_type") != INTRADAY_LIVE_RUN_TYPE:
                errors.append(f"intraday scheduled run_type must be {INTRADAY_LIVE_RUN_TYPE!r}")
            if rc.get("cron_id") != INTRADAY_LIVE_CRON_ID:
                errors.append(f"intraday scheduled run must have cron_id={INTRADAY_LIVE_CRON_ID!r}")
            if supersedes is not None:
                errors.append(
                    "intraday scheduled run must not carry supersedes_cron_id "
                    "(that names the afternoon task's lineage)"
                )
            if not isinstance(scheduled_for_et, str) or scheduled_for_et not in INTRADAY_SLOT_IDENTITIES:
                errors.append(
                    "intraday scheduled_for_et must be one of "
                    f"{sorted(INTRADAY_SLOT_IDENTITIES)!r}, got {scheduled_for_et!r}"
                )
            else:
                expected_window_start_et, expected_window_end_et, slot_run_id_re = INTRADAY_SLOT_IDENTITIES[
                    scheduled_for_et
                ]
                if rc.get("scheduled_window_end_et") != expected_window_end_et:
                    errors.append(
                        f"intraday scheduled_window_end_et must be {expected_window_end_et!r} "
                        f"for scheduled_for_et={scheduled_for_et!r}"
                    )
                is_intraday_identity = (
                    window_start_et == expected_window_start_et
                    and run_id_is_str
                    and bool(slot_run_id_re.match(run_id))
                )
                if run_id_is_str and not is_intraday_identity:
                    errors.append(
                        "intraday scheduled_window_start_et and run_id must form a coherent "
                        f"tuple for scheduled_for_et={scheduled_for_et!r}: "
                        f"({expected_window_start_et!r}, "
                        f"'intraday_live_YYYYMMDD_{scheduled_for_et.replace(':', '')}ET'); "
                        f"got scheduled_window_start_et={window_start_et!r}, run_id={run_id!r}"
                    )
        elif stream == MORNING_SLOT_STREAM:
            if rc.get("run_type") != SCHEDULED_RUN_TYPE:
                errors.append(f"morning scheduled run_type must be {SCHEDULED_RUN_TYPE!r}")
            if rc.get("cron_id") != MORNING_SLOT_CRON_ID:
                errors.append(f"morning scheduled run must have cron_id={MORNING_SLOT_CRON_ID!r}")
            if supersedes is not None:
                errors.append(
                    "morning scheduled run must not carry supersedes_cron_id "
                    "(that names the afternoon task's lineage)"
                )
            if rc.get("scheduled_window_end_et") != MORNING_SLOT_SCHEDULED_WINDOW_END_ET:
                errors.append(
                    f"morning scheduled_window_end_et must be {MORNING_SLOT_SCHEDULED_WINDOW_END_ET!r}"
                )
            if rc.get("valid_afternoon_scheduled_evidence") is not False:
                errors.append(
                    "morning scheduled run must have valid_afternoon_scheduled_evidence=false"
                )
            is_morning_identity = (
                scheduled_for_et == MORNING_SLOT_SCHEDULED_FOR_ET
                and window_start_et == MORNING_SLOT_SCHEDULED_WINDOW_START_ET
                and run_id_is_str
                and bool(MORNING_SLOT_RUN_ID_RE.match(run_id))
            )
            if run_id_is_str and not is_morning_identity:
                errors.append(
                    "morning scheduled_for_et, scheduled_window_start_et, and run_id must "
                    f"form the coherent morning tuple ({MORNING_SLOT_SCHEDULED_FOR_ET!r}, "
                    f"{MORNING_SLOT_SCHEDULED_WINDOW_START_ET!r}, 'scheduled_YYYYMMDD_1030ET'); "
                    f"got scheduled_for_et={scheduled_for_et!r}, "
                    f"scheduled_window_start_et={window_start_et!r}, run_id={run_id!r}"
                )
        elif stream == MIDDAY_SLOT_STREAM:
            if rc.get("run_type") != SCHEDULED_RUN_TYPE:
                errors.append(f"midday scheduled run_type must be {SCHEDULED_RUN_TYPE!r}")
            if rc.get("cron_id") != MIDDAY_SLOT_CRON_ID:
                errors.append(f"midday scheduled run must have cron_id={MIDDAY_SLOT_CRON_ID!r}")
            if supersedes is not None:
                errors.append(
                    "midday scheduled run must not carry supersedes_cron_id "
                    "(that names the afternoon task's lineage)"
                )
            if rc.get("scheduled_window_end_et") != MIDDAY_SLOT_SCHEDULED_WINDOW_END_ET:
                errors.append(
                    f"midday scheduled_window_end_et must be {MIDDAY_SLOT_SCHEDULED_WINDOW_END_ET!r}"
                )
            if rc.get("valid_afternoon_scheduled_evidence") is not False:
                errors.append(
                    "midday scheduled run must have valid_afternoon_scheduled_evidence=false"
                )
            is_midday_identity = (
                scheduled_for_et == MIDDAY_SLOT_SCHEDULED_FOR_ET
                and window_start_et == MIDDAY_SLOT_SCHEDULED_WINDOW_START_ET
                and run_id_is_str
                and bool(MIDDAY_SLOT_RUN_ID_RE.match(run_id))
            )
            if run_id_is_str and not is_midday_identity:
                errors.append(
                    "midday scheduled_for_et, scheduled_window_start_et, and run_id must "
                    f"form the coherent midday tuple ({MIDDAY_SLOT_SCHEDULED_FOR_ET!r}, "
                    f"{MIDDAY_SLOT_SCHEDULED_WINDOW_START_ET!r}, 'scheduled_YYYYMMDD_1230ET'); "
                    f"got scheduled_for_et={scheduled_for_et!r}, "
                    f"scheduled_window_start_et={window_start_et!r}, run_id={run_id!r}"
                )
        elif stream in (None, AFTERNOON_STREAM):
            # stream, when present, must be exactly 'afternoon' here -- not added
            # to a hard requirement, since historical artifacts predating the
            # stream field must stay valid. Without this elif/else split, ANY
            # other value (a typo, or an unrelated string like
            # 'midday_recovery') fell through to this branch and validated
            # anyway whenever the rest of the identity was afternoon-shaped,
            # even though the JSON schema already rejects exactly this
            # (Codex review: "Reject unknown streams before using the
            # afternoon validator", mirroring schemas/run_classification.
            # schema.json's own 'stream, when present, must be afternoon'
            # constraint on these two branches).
            if rc.get("run_type") != SCHEDULED_RUN_TYPE:
                errors.append(f"scheduled run_type must be {SCHEDULED_RUN_TYPE!r}")
            # Lineage of the repaired live task: accept the current task id OR the
            # superseded-task marker so continuity artifacts stay valid.
            if rc.get("cron_id") != CRON_ID and rc.get("supersedes_cron_id") != SUPERSEDES_CRON_ID:
                errors.append(
                    f"scheduled run must have cron_id={CRON_ID!r} "
                    f"OR supersedes_cron_id={SUPERSEDES_CRON_ID!r}"
                )
            if rc.get("scheduled_window_end_et") != SCHEDULED_WINDOW_END_ET:
                errors.append(f"scheduled_window_end_et must be {SCHEDULED_WINDOW_END_ET!r}")
            # scheduled_for_et, scheduled_window_start_et, and run_id must form ONE
            # coherent identity -- either fully canonical or fully legacy -- rather
            # than being accepted independently (Codex review discussion_r3653976747:
            # "Validate canonical and legacy identity fields as a unit"). Checking
            # each field against (canonical OR legacy) separately let a hybrid
            # classification such as scheduled_for_et='15:30' paired with a legacy
            # '..._1520ET' run_id pass as valid scheduled evidence even though no
            # real scheduled slot ever produces that combination.
            run_id_ok_canonical = run_id_is_str and bool(SCHEDULED_RUN_ID_RE.match(run_id))
            legacy_match = LEGACY_SCHEDULED_RUN_ID_RE.match(run_id) if run_id_is_str else None
            # The legacy tuple is valid ONLY for a run dated strictly before the
            # 2026-07-27 retime (Codex review r3654117261); a legacy-shaped run_id
            # dated on/after that date is NOT run_id_ok_legacy, so it falls through
            # to the "neither identity" error below rather than passing as valid
            # scheduled evidence.
            run_id_ok_legacy = bool(legacy_match) and legacy_match.group(1) < CANONICAL_RETIME_DATE_YYYYMMDD
            is_canonical_identity = (
                scheduled_for_et == SCHEDULED_FOR_ET
                and window_start_et == SCHEDULED_WINDOW_START_ET
                and run_id_ok_canonical
            )
            is_legacy_identity = (
                scheduled_for_et == LEGACY_SCHEDULED_FOR_ET
                and window_start_et == LEGACY_SCHEDULED_WINDOW_START_ET
                and run_id_ok_legacy
            )
            if run_id_is_str and not (is_canonical_identity or is_legacy_identity):
                errors.append(
                    "scheduled_for_et, scheduled_window_start_et, and run_id must form a single "
                    f"coherent identity: either the canonical tuple ({SCHEDULED_FOR_ET!r}, "
                    f"{SCHEDULED_WINDOW_START_ET!r}, 'scheduled_YYYYMMDD_1530ET') or the legacy "
                    f"tuple ({LEGACY_SCHEDULED_FOR_ET!r}, {LEGACY_SCHEDULED_WINDOW_START_ET!r}, "
                    f"'scheduled_YYYYMMDD_1520ET'); got scheduled_for_et={scheduled_for_et!r}, "
                    f"scheduled_window_start_et={window_start_et!r}, run_id={run_id!r}"
                )
        else:
            errors.append(
                f"scheduled run must have stream in (None, {AFTERNOON_STREAM!r}, "
                f"{MORNING_SLOT_STREAM!r}, {MIDDAY_SLOT_STREAM!r}, {MARKET_OPEN_STREAM!r}, "
                f"{INTRADAY_STREAM!r}), got {stream!r}"
            )
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
        elif SCHEDULED_RUN_ID_ANY_RE.match(run_id):
            errors.append(
                "manual run_id must not match the scheduled_YYYYMMDD_1530ET "
                "(or legacy 1520ET) pattern"
            )

    return ValidationResult(not errors, errors)


def is_preauthorized_v2(state_or_rc: Mapping[str, Any]) -> bool:
    """True if a run_state (or its run_classification block) is tagged v2-preauthorized."""
    rc = state_or_rc
    if isinstance(state_or_rc, dict) and isinstance(state_or_rc.get("run_classification"), dict):
        rc = state_or_rc["run_classification"]
    return isinstance(rc, dict) and rc.get("state_schema_version") == STATE_SCHEMA_VERSION_V2


def is_manual_state(state: Mapping[str, Any]) -> bool:
    """True if a run_state is manual by EITHER origin or flag (defensive OR)."""
    rc = state.get("run_classification") if isinstance(state, dict) else None
    if not isinstance(rc, dict):
        return False
    return rc.get("run_origin") == "manual" or rc.get("manual_run") is True


def is_scheduled_state(state: Mapping[str, Any]) -> bool:
    """True only if a run_state is a *valid* scheduled run.

    A manual flag anywhere wins: a state that is both marked scheduled and
    carries manual_run=true is treated as NOT scheduled. A run the artifact
    contract rejected (top-level ``artifact_contract_failure`` marker) also fails
    closed here, so the 15:35 reporting selector never publishes a contract-rejected
    partial-placement run's submitted subset as valid scheduled fills.

    ``scheduled_evidence_denied`` is honored here too, but NOT unconditionally.
    This predicate answers a different question from
    ``derive_valid_scheduled_evidence``: that one asks whether the slot executed
    (reliability, watchdogs, missed-slot accounting), while this one decides
    whether a state's orders are attributable to the scheduled slot (fill and
    slippage reporting). A failed run can honestly answer no to the first and
    yes to the second, and the two must not be collapsed: a run that placed a
    real order and then raised is denied as evidence by both failure handlers,
    and rejecting it here as well made the reporting selector discard the whole
    state so the preserved fills vanished from scheduled reporting (Codex
    review). So a denied state is rejected only when it has no order to
    attribute -- which is exactly the date/holiday-gated stub this check was
    added for.
    """
    if isinstance(state, dict) and state.get("artifact_contract_failure") is not None:
        return False
    if isinstance(state, dict) and state.get("scheduled_evidence_denied") is True:
        if not _order_ids_from_state(state):
            return False
    rc = state.get("run_classification") if isinstance(state, dict) else None
    if not isinstance(rc, dict):
        return False
    if is_manual_state(state):
        return False
    return rc.get("run_origin") == "scheduled" and bool(validate_classification(rc))


def order_submit_status(state: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return the run_state's top-level order_submit_status, or None if absent."""
    if not isinstance(state, dict):
        return None
    value = state.get("order_submit_status")
    return value if isinstance(value, str) and value else None


def is_confirmation_blocked(state: Optional[Mapping[str, Any]]) -> bool:
    """True if the run passed review but submission was blocked awaiting confirmation.

    Such a run is valid scheduled-run evidence that the task fired but carries no
    order/fill: reporting must report zero scheduled fills/order IDs, and neither
    slippage nor the watchdog may mark the scheduled state invalid on this basis.
    """
    return order_submit_status(state) == ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED


@dataclass(frozen=True)
class NoOrderOutcome:
    """How a run that submitted zero orders must be recorded.

    ``run_classification`` here is the OUTCOME string that goes in the run_state's
    ``run_classification_outcome`` field; it is deliberately not the nested
    ``run_classification`` identity object, which stays a non-null object on every
    scheduled run (see scripts/scheduled_artifacts.py).
    """

    run_classification: str
    order_submit_status: str
    completion_reason: Optional[str]
    error_class: Optional[str]
    is_trading_failure: bool
    candidates_reaching_spread_gate: int
    candidates_blocked_missing_spread: int

    def as_run_state_fields(self) -> Dict[str, Any]:
        """The exact top-level run_state fields this outcome requires."""
        return {
            "run_classification_outcome": self.run_classification,
            "order_submit_status": self.order_submit_status,
            "completion_reason": self.completion_reason,
            "error_class": self.error_class,
            "is_trading_failure": self.is_trading_failure,
            "orders_submitted_count": 0,
            "candidates_reaching_spread_gate": self.candidates_reaching_spread_gate,
            "candidates_blocked_missing_spread": self.candidates_blocked_missing_spread,
        }


def classify_no_order_run(
    *,
    candidates_reaching_spread_gate: int,
    candidates_blocked_missing_spread: int,
    candidates_evaluated: Optional[int] = None,
    placement_attempted_and_failed: bool = False,
    data_errors: int = 0,
    scheduled: bool = True,
) -> NoOrderOutcome:
    """Classify a zero-order run, separating no-order logic from failures.

    Implements config/scheduled_tasks.json:spread_data_availability_policy, whose
    ``evaluation_order`` is: evaluate the ladder FIRST, then the spread gate. The
    recorded outcome depends on whether any ticker qualified BEFORE reaching the
    spread gate:

    * No ticker qualified at all -> ``completed_no_orders`` with
      ``completion_reason='no_ladder_candidates'`` and a NULL ``error_class``.
      Missing spread did not produce this outcome, so recording
      ``missing_spread_data`` here would be misleading. This is a normal, healthy
      zero-order run and valid scheduled evidence that the slot executed.
    * Candidates qualified but every one was stopped BEFORE the spread lookup, by
      a budget/cap/ceiling/duplicate check -> ``completed_no_orders`` with
      ``completion_reason='blocked_before_spread_gate'``. Those tickers DID
      qualify on the ladder, so calling it ``no_ladder_candidates`` would
      misreport what happened (Codex review). ``candidates_evaluated`` is what
      separates the two; when it is omitted the caller has no candidate count and
      the reaching-gate count stands in, preserving the older behavior.
    * Candidates reached the gate and EVERY one was failed closed for want of a
      proven spread -> ``completed_no_orders`` with
      ``error_class='missing_spread_data'``. Still a safe no-order skip, still
      valid scheduled evidence, still not a trading failure.
    * Candidates reached the gate, some were failed closed for spread and others
      were evaluated through -> ``completed_no_orders`` with a null
      ``error_class``: the spread-blocked rows failed closed individually
      (``candidates_blocked_missing_spread`` records how many) and the rest simply
      did not qualify. Never abort the whole run for a subset.
    * ``placement_attempted_and_failed`` -> placement ran on qualifying candidates
      and could not place them. That IS a failure; the complete artifact set is
      still written. A scheduled run records
      ``failed_after_connector_native_recovery_attempt``, a manual one the
      generic ``failed_placement``: the manual path never runs the
      connector-native recovery policy, so attributing its broker failures to
      that policy would be wrong (Codex review).

    None of these outcomes is a trading failure except the last, and none of them
    is a missed slot: the run fired and wrote its artifacts.

    ``data_errors`` is the run's provider/error count. A zero-order run that
    happened while market-data providers were raising is NOT a clean ladder no-op:
    its tickers degraded to NO_DATA/manual review rather than being genuinely
    evaluated, so it keeps its ``completed_no_orders`` classification but carries
    ``error_class='degraded_market_data'`` instead of a null one, and summary and
    reliability consumers can see the run was degraded (Codex review). A concrete
    spread error_class already established above wins, since that is the more
    specific cause.
    """
    reaching = max(0, candidates_reaching_spread_gate)
    blocked = max(0, candidates_blocked_missing_spread)
    # A row cannot be blocked at a gate it never reached.
    blocked = min(blocked, reaching)
    evaluated = reaching if candidates_evaluated is None else max(0, candidates_evaluated)
    # A candidate cannot reach the gate without having been evaluated.
    evaluated = max(evaluated, reaching)

    if placement_attempted_and_failed:
        # The connector-native outcome belongs to the SCHEDULED path only: a
        # manual run never runs that policy, so reporting keyed on it would
        # misattribute an ordinary manual broker failure as a failed scheduled
        # recovery attempt (Codex review).
        failure = (
            RUN_CLASSIFICATION_PLACEMENT_FAILED
            if scheduled
            else RUN_CLASSIFICATION_MANUAL_PLACEMENT_FAILED
        )
        return NoOrderOutcome(
            run_classification=failure,
            order_submit_status=ORDER_SUBMIT_STATUS_ERROR,
            completion_reason=None,
            error_class=failure,
            is_trading_failure=True,
            candidates_reaching_spread_gate=reaching,
            candidates_blocked_missing_spread=blocked,
        )
    # A zero-order run that ran while providers were raising is not a clean
    # no-op. It keeps its completed_no_orders classification (it did complete and
    # did submit nothing) but never a null error_class, so a summary or
    # reliability consumer can see the run was degraded. A concrete spread cause
    # below is more specific and wins.
    degraded = ERROR_CLASS_DEGRADED_MARKET_DATA if data_errors > 0 else None

    if reaching == 0:
        return NoOrderOutcome(
            run_classification=RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
            order_submit_status=ORDER_SUBMIT_STATUS_NO_ORDERS,
            completion_reason=(
                COMPLETION_REASON_NO_LADDER_CANDIDATES
                if evaluated == 0
                else COMPLETION_REASON_BLOCKED_BEFORE_SPREAD_GATE
            ),
            error_class=degraded,
            is_trading_failure=False,
            candidates_reaching_spread_gate=0,
            candidates_blocked_missing_spread=0,
        )
    if blocked == reaching:
        return NoOrderOutcome(
            run_classification=RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
            order_submit_status=ORDER_SUBMIT_STATUS_NO_ORDERS,
            completion_reason=None,
            error_class=ERROR_CLASS_MISSING_SPREAD_DATA,
            is_trading_failure=False,
            candidates_reaching_spread_gate=reaching,
            candidates_blocked_missing_spread=blocked,
        )
    return NoOrderOutcome(
        run_classification=RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
        order_submit_status=ORDER_SUBMIT_STATUS_NO_ORDERS,
        completion_reason=COMPLETION_REASON_NO_QUALIFYING_ORDERS,
        error_class=degraded,
        is_trading_failure=False,
        candidates_reaching_spread_gate=reaching,
        candidates_blocked_missing_spread=blocked,
    )


def deny_scheduled_evidence(state: Dict[str, Any]) -> Dict[str, Any]:
    """Mark ``state`` as something no consumer may count as scheduled evidence.

    Two markers, because two different consumers read two different places:
    the top-level ``scheduled_evidence_denied`` flag that
    ``derive_valid_scheduled_evidence`` honors, and the nested classification's
    own ``valid_scheduled_evidence``, which a consumer reading the block
    directly would otherwise trust.

    A synthesized failure artifact proves the slot FIRED. That is worth
    recording, and it is not the same claim as "the slot executed" -- without
    the denial the pointer publishes a run that never computed signals or
    reached placement as a successful slot.

    One implementation on purpose: every builder that fails a scheduled run
    closed must deny it the same way, and hand-copying the two markers is how
    ``raw_persistence_failure_run_state`` and ``recovery_escalation_run_state``
    ended up carrying neither (Codex review).
    """
    state = dict(state)
    state["scheduled_evidence_denied"] = True
    rc = state.get("run_classification")
    if isinstance(rc, dict):
        rc = dict(rc)
        rc["valid_scheduled_evidence"] = False
        state["run_classification"] = rc
    return state


def stamp_failure_classification(
    state: Dict[str, Any], *, submitted: int, error_detail: str
) -> Dict[str, Any]:
    """Mark ``state`` as a run that FAILED, choosing the label from ``submitted``.

    Both runners merge an in-flight placement into a state an earlier attempt at
    the same slot wrote. That merge must also re-label the state: the earlier
    attempt's successful or no-order classification would otherwise be
    republished as valid even though this attempt raised.

    The label is gated on the merged submitted count, not on merely having some
    outcome to merge. A retry whose only outcomes were blocked or failed placed
    nothing, so calling it a post-submission failure would claim an order landed
    when none did (Codex review).

    Shared by morning_dca and near_close_dca deliberately: keeping one copy per
    runner is how the two drifted apart in the first place.
    """
    state = dict(state)
    if submitted:
        state["run_classification_outcome"] = RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION
        state["order_submit_status"] = ORDER_SUBMIT_STATUS_SUBMITTED
        state["error_class"] = ERROR_CLASS_POST_SUBMISSION_FAILURE
    else:
        state["run_classification_outcome"] = RUN_CLASSIFICATION_STARTUP_FAILED
        state["order_submit_status"] = ORDER_SUBMIT_STATUS_ERROR
        state["error_class"] = ERROR_CLASS_STARTUP_FAILURE
    state["status"] = "failed"
    state["error_detail"] = error_detail
    state["is_trading_failure"] = True
    state["placement_evidence_incomplete"] = True
    return deny_scheduled_evidence(state)


def spread_gate_fields(
    *, spread_pct: Optional[float], max_spread_pct: float, evaluated: bool = True
) -> Dict[str, Any]:
    """Row-level spread fields for a signal/candidate row.

    Every row that reaches the spread gate records whether its own spread was
    proven, so a later reader can separate "no candidate ever reached the gate"
    from "this row was failed closed for want of a spread" without re-deriving it
    from prose skip reasons. A row that never reached the gate records
    ``not_evaluated`` -- never ``spread_ok=true``.
    """
    if not evaluated:
        return {"spread_ok": None, "spread_pct": None, "spread_status": SPREAD_STATUS_NOT_EVALUATED}
    if spread_pct is None:
        return {"spread_ok": False, "spread_pct": None, "spread_status": SPREAD_STATUS_UNAVAILABLE}
    if spread_pct > max_spread_pct:
        return {
            "spread_ok": False,
            "spread_pct": spread_pct,
            "spread_status": SPREAD_STATUS_NOT_OK,
        }
    return {"spread_ok": True, "spread_pct": spread_pct, "spread_status": SPREAD_STATUS_OK}


def row_blocked_for_missing_spread(row: Optional[Mapping[str, Any]]) -> bool:
    """True when a persisted row was failed closed because its spread was unproven.

    Accepts the legacy ``missing_bid_ask`` status persisted by the Jul 27 2026
    intraday runs alongside the canonical ``spread_unavailable``.
    """
    if not isinstance(row, Mapping):
        return False
    status = row.get("spread_status")
    return isinstance(status, str) and status in SPREAD_UNAVAILABLE_STATUSES


# --- Writing artifacts -------------------------------------------------------

def update_latest_scheduled_run_pointer(
    run_state: Dict[str, Any], run_state_path: str | Path, base: Optional[Path | str] = None
) -> Path:
    """Write/refresh latest_scheduled_run.json to point at an already-written scheduled run_state.

    Extracted from :func:`write_run_artifacts` so a caller that writes
    run_state.json to its own already-correct artifacts directory (e.g.
    near_close_dca's live path, whose ``artifacts_dir`` the scheduler already
    points at the canonical ``scheduled_runs/...`` directory per
    config/scheduled_tasks.json) can update the pointer without a second,
    potentially differently-located copy of run_state.json being written.
    Callers must validate ``run_state["run_classification"]`` themselves first.

    The pointer's own ``stream`` is read from ``run_state["run_classification"]``
    (falling back to ``"afternoon"`` when absent, matching
    ``build_scheduled_classification``'s own default), not hardcoded: a
    non-afternoon roster slot (``run_dca.run_slot``'s 1030ET/1230ET) stamps its
    own stream via ``classification_overrides``, and a pointer that always
    claimed ``"afternoon"`` regardless made
    ``scripts.same_day_pointer.validate_pointer(..., expected_stream="morning")``
    reject that freshly written pointer as ``wrong_stream`` -- the pointer-based
    watchdog could never observe a morning/midday run this way, including one
    blocked before placement (Codex review: "Write the blocked pointer with the
    morning stream identity").

    This always writes to ``latest_scheduled_run.json`` (via
    :func:`latest_scheduled_run_path`), never to ``morning_dca.py``/
    ``midday_dca.py``'s own ``latest_morning_run.json``/``latest_midday_run.json``
    (Codex review: "Write blocked slots through their stream-specific
    pointer"). That is a deliberate scope limit, not an oversight: those two
    files and their dedicated schemas (morning_run_classification.schema.json,
    midday_run_classification.schema.json) are written and read only by
    MorningDCAExecutor/MiddayDCAExecutor themselves (and their own tests) --
    no other code in this repository reads either pointer file or validates
    against either dedicated schema outside tests. The seven-slot roster's
    10:30/12:30 LIVE trading decision (this function's actual caller) has
    already superseded those executors' own live-mode code path entirely;
    MorningDCAExecutor's only confirmed remaining live caller is the 9:45 AM
    preflight scan (read-only, no trading -- see docs/morning_dca.md), and
    MiddayDCAExecutor has no live caller left at all. Writing a second,
    stream-specific pointer/schema pair here would be exercising a consumer
    that does not exist today. If an out-of-repo consumer (e.g. the external
    watchdog task documented in config/scheduled_tasks.json) is later found to
    depend on either file, that is the point to revisit this.
    """
    try:  # pragma: no cover - import shim for flat sys.path
        from scripts.same_day_pointer import derive_valid_scheduled_evidence
    except ImportError:  # pragma: no cover
        from same_day_pointer import derive_valid_scheduled_evidence  # type: ignore

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
    #
    # Routed through the SHARED derivation rather than repeating the rule here,
    # so this pointer honors artifact_contract_failure and the explicit
    # scheduled_evidence_denied marker too. A failure state pointed at by this
    # helper would otherwise be published as valid evidence by the one predicate
    # that still had its own inline copy (Codex review).
    valid_scheduled_evidence = derive_valid_scheduled_evidence(run_state)
    pointer_status = "valid" if valid_scheduled_evidence else "pending"
    atomic_write_text(
        pointer,
        json.dumps(
            {
                "run_id": rc.get("run_id"),
                "trading_day": day,
                "run_state_path": str(run_state_path),
                # rc.get("stream", "afternoon") looked equivalent but is not:
                # that default only applies when the KEY IS ABSENT, whereas
                # validate_classification() treats an explicit stream=null the
                # SAME as absent (both mean legacy/afternoon evidence). A
                # classification with "stream": null present therefore passed
                # validate_classification() and write_run_artifacts() but
                # wrote a null stream into the pointer instead of "afternoon",
                # which scripts.same_day_pointer.validate_pointer normalizes
                # to "" and rejects as wrong_stream -- publishing an unusable
                # pointer from a classification its own runtime validator just
                # accepted (Codex review: "Default null legacy streams before
                # writing the pointer"). `or` treats both absent and null the
                # same, matching validate_classification()'s own check.
                "stream": rc.get("stream") or "afternoon",
                "pointer_status": pointer_status,
                "same_day_required": True,
                "valid_scheduled_evidence": valid_scheduled_evidence,
                "cron_id": rc.get("cron_id"),
                "supersedes_cron_id": rc.get("supersedes_cron_id"),
                "triggered_at_utc": rc.get("triggered_at_utc"),
                "updated_at_utc": _utc_now_iso(),
                "state_schema_version": STATE_SCHEMA_VERSION,
            },
            indent=2,
        )
        + "\n",
    )
    return pointer


def write_run_artifacts(
    run_state: Dict[str, Any], base: Optional[Path] = None
) -> Dict[str, str]:
    """Persist a run_state to the correct directory for its origin.

    Scheduled runs also (atomically enough for this backup) update
    latest_scheduled_run.json. Manual runs NEVER touch that pointer.

    Returns a dict of the paths written.
    """
    rc = run_state.get("run_classification")
    if not isinstance(rc, dict):
        raise ValueError("refusing to write run_state missing run_classification dict")
    result = validate_classification(rc)
    if not result:
        raise ValueError("refusing to write invalid run_classification: " + "; ".join(result.errors))

    origin = rc.get("run_origin")
    written: Dict[str, str] = {}

    if origin == "scheduled":
        day = str(run_state.get("trading_day", "")).strip()
        out_dir = scheduled_run_dir(day, base)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = out_dir / RUN_STATE_FILENAME
        atomic_write_text(state_path, json.dumps(run_state, indent=2) + "\n")
        written["run_state"] = str(state_path)

        pointer = update_latest_scheduled_run_pointer(run_state, state_path, base)
        written["latest_scheduled_run"] = str(pointer)
    else:  # manual
        ts = re.sub(r"[^0-9]", "", str(rc.get("triggered_at_utc", ""))) or str(rc.get("run_id", ""))
        out_dir = manual_run_dir(ts, base)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = out_dir / RUN_STATE_FILENAME
        atomic_write_text(state_path, json.dumps(run_state, indent=2) + "\n")
        written["run_state"] = str(state_path)
        # Explicitly NOT updating latest_scheduled_run.json.

    return written


# --- Manual order exclusion --------------------------------------------------

def _iter_run_state_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("run_state*.json"))


def _order_ids_from_state(state: Mapping[str, Any]) -> Set[str]:
    # Both fields are read defensively: `state.get(k, []) or []` still yields the
    # raw value for a malformed non-list such as `buy_candidates: 1`, and
    # iterating it raised TypeError out of the whole watchdog collection loop --
    # one corrupt persisted artifact aborted the scan of every other run instead
    # of being skipped. A non-list contributes no IDs and the scan continues
    # (Codex review: "Reject malformed order collections before collecting IDs").
    ids: Set[str] = set()
    candidates = state.get("buy_candidates")
    if isinstance(candidates, list):
        for cand in candidates:
            if isinstance(cand, dict):
                oid = cand.get("order_id")
                if isinstance(oid, str) and oid:
                    ids.add(oid)
    # Some artifacts also carry a flat orders list.
    orders = state.get("orders")
    if isinstance(orders, list):
        for order in orders:
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
            reader = csv.reader(handle)
            try:
                header = next(reader)
                idx = header.index("order_id")
            except (StopIteration, ValueError):
                return ids

            for row in reader:
                if len(row) > idx:
                    oid = row[idx].strip()
                    if oid and oid != "-":
                        ids.add(oid)
    except (OSError, csv.Error, UnicodeDecodeError):
        return set()
    return ids


def _referenced_orders_csvs(state: Mapping[str, Any], run_dir: Path) -> List[Path]:
    """Resolve the orders CSV(s) a run_state points at or sits beside.

    Manual runs from ``near_close_dca.run_and_report()`` keep their order IDs only
    in the CSV named by ``artifacts.orders_csv_path`` (the state itself has no
    embedded candidates/orders), so scanning embedded fields alone misses them.
    A RELATIVE recorded path is resolved against ``run_dir``, matching
    ``near_close_dca.load_prior_same_day_allocations``. ``Path(recorded)`` alone
    resolved it against the PROCESS working directory, so a path naming a
    subdirectory (``artifacts/executions.csv``) was missed here while the core
    allocation loader found it -- the recovery's fills reduced a later top-up
    while its order IDs stayed out of this exclusion set and were reported as
    unexplained broker orders (Codex review: "Resolve relative recovery CSV
    paths from the run directory"). The basename retry below still covers a
    stale/absolute path whose file now sits directly beside the state.

    The recorded path may be stale/absolute, so we also try its basename inside
    the run directory and any sibling ``*orders.csv``.
    """
    candidates: List[Path] = []
    artifacts = state.get("artifacts") if isinstance(state, dict) else None
    if isinstance(artifacts, dict):
        recorded = artifacts.get("orders_csv_path")
        if isinstance(recorded, str) and recorded:
            recorded_path = Path(recorded)
            if not recorded_path.is_absolute():
                recorded_path = run_dir / recorded_path
            candidates.append(recorded_path)
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


def _referenced_orders_csvs_within(
    state: Mapping[str, Any], run_dir: Path, root: Path
) -> List[Path]:
    """:func:`_referenced_orders_csvs`, filtered to paths inside ``root``.

    A recovery state can carry an ABSOLUTE ``artifacts.orders_csv_path``
    pointing outside its own run directory -- a copied artifact whose stale
    path still names the run it was copied from. The unfiltered helper keeps
    that external path, so every order ID in an unrelated CSV would be
    classified as this recovery pass's context and excluded from watchdog
    discrepancy reporting, hiding genuinely unexplained broker orders (Codex
    review: "Constrain recovery CSVs to the validated run root").

    The local-basename fallback and sibling ``*orders.csv`` globs are retained
    (they resolve inside ``run_dir`` by construction), so a stale recorded path
    still finds the real CSV next to the state that referenced it.
    """
    try:
        root_key = root.resolve()
    except OSError:
        return []
    kept: List[Path] = []
    for candidate in _referenced_orders_csvs(state, run_dir):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == root_key or root_key in resolved.parents:
            kept.append(candidate)
    return kept


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
                state = json.loads(path.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
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


def collect_recovery_context_order_ids(
    base: Optional[Path] = None,
    trading_day: Optional[str] = None,
    stream_roots: Optional[Iterable[Path | str]] = None,
) -> Set[str]:
    """Union of every order_id found under a scheduled-recovery context root.

    The recovery-side counterpart to :func:`collect_manual_order_ids`. The
    watchdog's all-day broker reconciliation classifies every same-day order it
    sees; a recovery pass's orders have no valid *scheduled* run_state and sit
    outside every manual root, so without this set they reconcile against
    nothing and surface as an unexplained broker/log discrepancy rather than as
    recognized recovery context (Codex review: "Add the recovery root to
    watchdog classification").

    Like the manual set, these IDs are context only: they must never be counted
    as scheduled evidence, scheduled slippage, scheduled fill counts,
    reliability, or scheduled cost basis.

    Every state is validated with :func:`is_top_up_context_state` before its IDs
    are collected. A directory-name match alone is NOT sufficient evidence: the
    manual roots are fixed, curated paths, but these are glob-matched dated
    directories, so a malformed, manual, or even scheduled-evidence artifact
    accidentally written under a ``*_recovery_*`` directory would otherwise be
    labelled recovery context and silently excluded from discrepancy reporting,
    hiding orders that should stay unexplained or be classified elsewhere
    (Codex review: "Validate states before excluding recovery order IDs").

    ``trading_day``, when given, additionally requires each state to belong to
    that ET trading day, so a stale prior-day recovery directory left in the
    tree cannot explain away today's orders.
    """
    ids: Set[str] = set()
    for root in recovery_context_roots(base, stream_roots):
        for path in _iter_run_state_files(root):
            try:
                state = json.loads(path.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            if not is_top_up_context_state(state, trading_day):
                continue
            rc = state.get("run_classification")
            # is_top_up_context_state also accepts MANUAL runs; here only a
            # scheduled-recovery run belongs in the recovery bucket.
            if not isinstance(rc, dict) or rc.get("run_origin") != RUN_ORIGIN_SCHEDULED_RECOVERY:
                continue
            ids |= _order_ids_from_state(state)
            # Scoped to the recovery root this state was discovered under, so an
            # external/stale absolute CSV path cannot pull in another run's IDs.
            for csv_path in _referenced_orders_csvs_within(state, path.parent, root):
                ids |= _order_ids_from_orders_csv(csv_path)
    return ids


# Earlier-stream SCHEDULED trees, as globs relative to CRON_TRACKING_ROOT. The
# dated slot directories, excluding the *_recovery_* siblings that
# RECOVERY_CONTEXT_SUBROOT_GLOBS already covers. The afternoon tree is absent
# for the same reason it is absent there: it is the final run's own stream, not
# prior context for it.
_EARLIER_SCHEDULED_SUBROOT_GLOBS = (
    "market_open_dca/*",
    "morning_dca/*",
    "midday_dca/*",
    "intraday_dca/*",
)

# The same environment overrides RECOVERY_CONTEXT_SUBROOT_GLOBS honors, mapped
# to the earlier-scheduled patterns they REPLACE. morning/midday are absent for
# the same reason: their roots come from stream config, and a caller that
# relocates them passes the resolved roots via stream_roots.
_OVERRIDABLE_EARLIER_SCHEDULED_SUBROOT_ENV = {
    "market_open_dca/*": "DCA_MARKET_OPEN_LIVE_STATE_ROOT",
    "intraday_dca/*": "DCA_INTRADAY_LIVE_STATE_ROOT",
}


def _is_earlier_stream_scheduled_state(state: Any) -> bool:
    """True for a valid SCHEDULED run of any stream, not just the afternoon one.

    :func:`is_scheduled_state` cannot be used here: it delegates to
    :func:`validate_classification`, which hard-requires the AFTERNOON identity
    -- ``run_type='scheduled_live_test'``, the 15:30 cron lineage, and a
    ``scheduled_YYYYMMDD_1530ET`` run_id. A genuine market-open, morning,
    midday, or intraday run has its own run_type, cron_id, schema version, and
    slot, so every one of them was rejected and their order IDs stayed eligible
    for the final-run time fallback -- the exact bug the collector exists to
    prevent (Codex review: "Validate earlier streams with their own contracts").

    A per-stream contract does not exist in this module, so this applies the
    generic checks that DO hold across streams: not manual,
    ``run_origin='scheduled'``, and a non-empty run_id and trading day so a junk
    artifact cannot claim orders. It deliberately does NOT reject an
    ``artifact_contract_failure`` run -- see the note in the body. Being generous is the
    safe direction here specifically because the scan is already scoped to
    EARLIER stream directories -- the final run's own orders are never in them,
    so a looser check cannot hide the orders this selector must attribute.
    """
    if not isinstance(state, dict):
        return False
    # An artifact_contract_failure marker is deliberately NOT a rejection here.
    # That marker means the run is not valid scheduled EVIDENCE; it does not
    # mean the run failed to place orders, and a contract failure recorded
    # AFTER submission leaves real orders owned by that earlier stream. Treating
    # it as a rejection dropped those orders from every exclusion set, so one
    # landing inside 15:25-15:35 was attributed to the final run by the time
    # fallback -- corrupting final-run slippage and reliability rather than
    # leaving it owned by the earlier stream (Codex review: "Exclude
    # contract-failed earlier orders from final attribution"). Evidence validity
    # and order OWNERSHIP are different questions, and only ownership matters to
    # an exclusion set.
    if is_manual_state(state):
        return False
    rc = state.get("run_classification")
    if not isinstance(rc, dict):
        return False
    if rc.get("run_origin") != "scheduled":
        return False
    if rc.get("manual_run") is True:
        return False
    if not str(rc.get("run_id") or state.get("run_id") or "").strip():
        return False
    return bool(str(rc.get("trading_day") or state.get("trading_day") or "").strip())


def collect_earlier_scheduled_order_ids(
    base: Optional[Path] = None,
    trading_day: Optional[str] = None,
    stream_roots: Optional[Iterable[Path | str]] = None,
) -> Set[str]:
    """Order IDs claimed by a VALID earlier-stream scheduled run that day.

    The third exclusion set for the final-run broker time-window fallback,
    alongside the manual and recovery sets. That fallback attributes orders
    purely on their timestamp, and the reconciliation contract excludes any
    order already claimed by an earlier stream artifact -- but only recovery and
    manual claims were being collected. A delayed NORMAL 14:30 scheduled run
    placing at 15:27 lands inside the canonical 15:25-15:35 window, is claimed
    by neither set, and was therefore attributed to the final run, corrupting
    final-run slippage and reliability (Codex review: "Exclude all earlier-
    stream claims from the broker fallback").

    Every state is validated with :func:`is_scheduled_state` and, when
    ``trading_day`` is given, must belong to that ET day -- a directory-name
    match alone is not evidence, exactly as in
    :func:`collect_recovery_context_order_ids`.
    """
    ids: Set[str] = set()
    parent = cron_tracking_root(base)
    roots: List[Path] = []
    for pattern in _EARLIER_SCHEDULED_SUBROOT_GLOBS:
        # An environment override REPLACES its conventional subroot rather than
        # supplementing it, exactly as recovery_context_roots() treats the same
        # variables. Scanning only the conventional trees meant that in a
        # relocated deployment -- where both callers default stream_roots to
        # None -- a delayed normal run under the override tree was never
        # collected, stayed eligible for the final-run time fallback, and was
        # misattributed to the afternoon run (Codex review: "Honor relocated
        # roots when excluding earlier scheduled orders").
        env_var = _OVERRIDABLE_EARLIER_SCHEDULED_SUBROOT_ENV.get(pattern)
        if env_var:
            configured = (os.environ.get(env_var) or "").strip()
            # An override naming the cron-tracking root or an ancestor of it is
            # a misconfiguration, not a relocation. Same carve-out
            # recovery_context_roots() applies: keep the conventional fallback
            # rather than replacing the tree with a far broader one.
            if configured and not _override_is_too_broad(configured, parent):
                override = Path(configured)
                if override.is_dir():
                    roots.extend(
                        sorted(child for child in override.iterdir() if child.is_dir())
                    )
                continue
        try:
            roots.extend(sorted(p for p in parent.glob(pattern) if p.is_dir()))
        except OSError:
            continue
    for extra in stream_roots or ():
        extra_path = Path(extra)
        if extra_path.is_dir():
            roots.extend(sorted(child for child in extra_path.iterdir() if child.is_dir()))
    for root in roots:
        if not root.is_dir():
            continue
        for path in _iter_run_state_files(root):
            try:
                state = json.loads(path.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            if not _is_earlier_stream_scheduled_state(state):
                continue
            if trading_day is not None:
                rc = state.get("run_classification")
                state_day = str(
                    (rc.get("trading_day") if isinstance(rc, dict) else None)
                    or state.get("trading_day")
                    or ""
                ).strip()
                if state_day != trading_day:
                    continue
            ids |= _order_ids_from_state(state)
            for csv_path in _referenced_orders_csvs_within(state, path.parent, root):
                ids |= _order_ids_from_orders_csv(csv_path)
    return ids


def classify_non_scheduled_order_ids(
    base: Optional[Path] = None,
    trading_day: Optional[str] = None,
    stream_roots: Optional[Iterable[Path | str]] = None,
) -> Dict[str, Set[str]]:
    """Order IDs that are explainable but are NOT scheduled evidence.

    Returns ``{'manual': {...}, 'recovery_context': {...}}``. A watchdog
    reconciling the day's full broker order list should treat a hit in either
    set as classified-and-excluded rather than as a discrepancy; only an ID in
    neither set, and in no scheduled artifact, is genuinely unexplained.
    """
    return {
        "manual": collect_manual_order_ids(base),
        "recovery_context": collect_recovery_context_order_ids(
            base, trading_day, stream_roots
        ),
    }


# --- Same-day prior-allocation / top-up context ------------------------------
#
# The later same-day runs (12:30 midday, 3:30 afternoon/final) must fold earlier
# same-day MANUAL fills in as prior-allocation context for the top-up guard: a
# later run buys only max(0, desired - prior_same_day_amount) and only when its
# decision price is strictly below the ticker's most recent prior same-day
# reference price (resolved by generated_at; see _merge_prior).
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
# cost-basis evidence, watchdog proof, or proof the 12:30/3:30 tasks ran -- its
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

# Scheduled-recovery context roots, as GLOBS relative to CRON_TRACKING_ROOT.
# Unlike the manual subroots above these are not fixed directories: a recovery
# pass writes a dated sibling of the scheduled slot directory it recovered
# (midday_dca/20260727_1230ET_recovery_1305ET -- see config/scheduled_tasks.json
# -> recovery_context_run_outcomes), so the recovery runs cannot be named by a
# static prefix. A literal "midday_dca" subroot would instead pull the whole
# scheduled midday tree into the manual/context scan, which must never happen.
#
# These runs are NOT manual -- a scheduled slot failed and a separate pass
# placed real orders -- but they are top-up context for exactly the same reason
# the manual roots are: capital moved that day and a later run must not spend it
# twice. Matching them lets the watchdog's all-day broker reconciliation
# classify those order IDs as recovery context instead of reporting them as an
# unexplained broker/log discrepancy (Codex review: "Add the recovery root to
# watchdog classification").
#
# The afternoon tree is deliberately absent: an afternoon recovery is the
# afternoon run's own repair, not prior context for it.
RECOVERY_CONTEXT_SUBROOT_GLOBS = (
    "market_open_dca/*_recovery_*",
    "morning_dca/*_recovery_*",
    "midday_dca/*_recovery_*",
    "intraday_dca/*_recovery_*",
)

# Conventional subroots that an environment variable can REPLACE, mapped to the
# variable that replaces them. run_dca._resolve_cross_stream_prior_allocation_roots
# already drops the conventional sibling root outright when its override is
# configured -- the operator pointed that stream somewhere else, so scanning the
# old tree risks double-counting a relocated copy or reading stale artifacts.
# recovery_context_roots must honor the same replacement or it silently undoes
# it, re-adding exactly those directories (Codex review: "Honor stream overrides
# when scanning recovery fallbacks"). morning/midday are absent because their
# roots come from stream config rather than the environment; a caller that
# relocates them passes the resolved roots via stream_roots.
_OVERRIDABLE_RECOVERY_SUBROOT_ENV = {
    "market_open_dca/*_recovery_*": "DCA_MARKET_OPEN_LIVE_STATE_ROOT",
    "intraday_dca/*_recovery_*": "DCA_INTRADAY_LIVE_STATE_ROOT",
}

# Matches a recovery directory's own name, e.g. 20260727_1230ET_recovery_1305ET.
# Used when a caller passes a recovery directory directly instead of its parent
# stream root.
RECOVERY_DIR_RE = re.compile(r".*_recovery_.*")


# Glob applied to an explicitly-supplied stream root (see recovery_context_roots'
# stream_roots parameter), which already names the stream directory itself.
RECOVERY_DIR_GLOB = "*_recovery_*"

# How far above a run_state's directory to look for the enclosing recovery root.
# One level covers the common artifacts/ subfolder; three leaves headroom
# without letting the search wander up into a tree that merely happens to have
# "_recovery_" somewhere in an ancestor's name.
RECOVERY_ROOT_SEARCH_DEPTH = 3


def recovery_root_for(run_dir: Path | str) -> Path:
    """The enclosing ``*_recovery_*`` directory for a recovery run's state file.

    THE containment boundary for a recovery artifact's referenced CSVs. A
    recovery run may write ``artifacts/run_state.json`` beside a ROOT-level
    ``filled_orders.csv`` and record it as ``../filled_orders.csv`` -- a
    legitimate in-root path. Treating ``run_state.json``'s own directory as the
    boundary rejected exactly that file and then retried a basename that does
    not exist one level down, so both allocation loaders omitted real deployed
    capital while the watchdog collector -- which already scoped to the
    enclosing recovery root -- classified its order ID as explained. The later
    run then bought the ticker's full amount again (Codex review: "Resolve
    recovery CSVs against the artifact root").

    Falls back to ``run_dir`` when no ancestor looks like a recovery directory,
    which keeps the previous (narrower) boundary for any layout this does not
    recognize.
    """
    start = Path(run_dir)
    candidate = start
    for _ in range(RECOVERY_ROOT_SEARCH_DEPTH):
        if RECOVERY_DIR_RE.match(candidate.name):
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return start


def _override_is_too_broad(candidate: Path | str, cron_root: Path) -> bool:
    """True when an override names the cron tree or something above it.

    A stream root must be a SUBDIRECTORY of the cron-tracking tree
    (``intraday_dca`` and siblings). A value equal to ``cron_root`` -- or above
    it -- names the whole deployment, which
    ``run_dca._resolve_cross_stream_prior_allocation_roots`` treats as a
    misconfiguration and answers by KEEPING the conventional fallback.

    Equal is included, and an earlier version of this helper wrongly excluded
    it. That version reasoned by analogy with the resolver's
    equal-to-``afternoon_root`` carve-out, but applied it one level up: the
    resolver compares against the AFTERNOON root, and the cron-tracking root is
    a strict ancestor of that, so an override equal to the cron root is
    rejected there. Accepting it here suppressed the conventional
    ``intraday_dca/*_recovery_*`` glob and then scanned only the cron root's own
    direct ``*_recovery_*`` children, finding none -- so recoveries went missing
    from both fallback allocation and order-ID classification (Codex review:
    "Keep the fallback when an override equals the cron root").

    An unresolvable path is not treated as too broad; it simply matches no
    recovery directory later, which needs no special case.
    """
    try:
        candidate_key = Path(candidate).resolve()
        cron_key = cron_root.resolve()
    except OSError:
        return False
    return candidate_key == cron_key or candidate_key in cron_key.parents


def recovery_context_roots(
    base: Optional[Path] = None,
    stream_roots: Optional[Iterable[Path | str]] = None,
) -> List[Path]:
    """Existing scheduled-recovery directories usable as same-day context.

    Expands :data:`RECOVERY_CONTEXT_SUBROOT_GLOBS` against the filesystem, so
    unlike :func:`top_up_context_roots` (which returns fixed paths whether or
    not they exist) this yields only directories actually present. Sorted for
    deterministic scan order.

    ``stream_roots`` adds explicitly-supplied live stream roots to the search,
    each scanned for its own ``*_recovery_*`` children. The conventional
    layout below ``cron_tracking_root(base)`` is only ONE deployment shape:
    morning/midday roots come from each stream's configured
    ``artifact_paths.live_run_state_root``, and the intraday/market-open roots
    are overridable via ``DCA_INTRADAY_LIVE_STATE_ROOT`` /
    ``DCA_MARKET_OPEN_LIVE_STATE_ROOT`` (see
    ``run_dca._resolve_cross_stream_prior_allocation_roots``). A deployment
    that relocates a stream root outside the cron-tracking tree would
    otherwise have its recovery fills loaded by the core top-up scan but never
    discovered here, so the watchdog would report exactly those orders as
    unexplained (Codex review: "Honor configured stream roots when discovering
    recoveries"). A caller that already resolved its stream roots should pass
    them.

    Passing ``stream_roots`` is additive, but an ENVIRONMENT override REPLACES:
    a conventional subroot named in :data:`_OVERRIDABLE_RECOVERY_SUBROOT_ENV` is
    dropped when its variable is set and the override path is scanned in its
    place, mirroring ``_resolve_cross_stream_prior_allocation_roots``. Scanning
    both would read a tree the operator deliberately relocated away from;
    scanning neither -- which is what dropping without substituting did --
    leaves an override deployment with no recovery discovery at all for that
    stream on the default code path.
    """
    parent = cron_tracking_root(base)
    roots: List[Path] = []
    # Environment-override replacement paths, scanned in place of the
    # conventional subroots they replace.
    override_roots: List[str] = []
    for pattern in RECOVERY_CONTEXT_SUBROOT_GLOBS:
        # A conventional subroot whose stream has an explicit environment
        # override is REPLACED, not merely supplemented: the override stands in
        # for it, exactly as run_dca._resolve_cross_stream_prior_allocation_roots
        # treats it. Globbing it anyway re-added a tree the caller had
        # deliberately dropped, so a stale recovery left there could be merged
        # into the afternoon prior allocation -- duplicating a copy that also
        # lives under the configured root and suppressing a legitimate top-up.
        #
        # Replacement means scanning the override path HERE, not just skipping
        # the old one. Suppressing without substituting left every caller that
        # omits stream_roots -- classify_non_scheduled_order_ids() and
        # select_scheduled_state_for_reporting() both default it to None --
        # discovering neither tree in an override deployment, so recovery order
        # IDs went unexplained or were swept up by the reporting time-window
        # fallback (Codex review: "Scan the active override when stream roots
        # are omitted").
        env_var = _OVERRIDABLE_RECOVERY_SUBROOT_ENV.get(pattern)
        if env_var:
            configured = (os.environ.get(env_var) or "").strip()
            # An override that is a STRICT ANCESTOR of the cron-tracking root is
            # a misconfiguration, not a relocation -- it names practically the
            # whole tree rather than one stream's root.
            # _resolve_cross_stream_prior_allocation_roots rejects exactly that
            # shape and KEEPS the conventional fallback, so the core allocation
            # scan still reads intraday_dca; treating it as a valid replacement
            # here suppressed the conventional pattern and then scanned only the
            # parent's direct *_recovery_* children, finding none. Recovery IDs
            # went unexplained and bare recovery CSV fills were missed (Codex
            # review: "Keep the fallback for invalid ancestor overrides").
            if configured and not _override_is_too_broad(configured, parent):
                override_roots.append(configured)
                continue
        try:
            roots.extend(p for p in parent.glob(pattern) if p.is_dir())
        except OSError:
            continue
    # Override paths are scanned exactly like explicitly-supplied stream roots;
    # the dedupe below collapses the overlap when a caller passes them too.
    for supplied_root in (*override_roots, *(stream_roots or ())):
        try:
            candidate = Path(supplied_root)
            roots.extend(p for p in candidate.glob(RECOVERY_DIR_GLOB) if p.is_dir())
            # A caller may pass the recovery directory itself rather than its
            # parent stream root; accept that instead of silently ignoring it.
            if candidate.is_dir() and RECOVERY_DIR_RE.match(candidate.name):
                roots.append(candidate)
        except OSError:
            continue
    return sorted({p.resolve(): p for p in roots}.values())

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
# EXECUTED-quantity columns, in precedence order -- see the matching
# near_close_dca._EXECUTED_QUANTITY_COLS. `quantity` is the REQUESTED size in
# the broker-native shape, so it is not an executed column; only
# quantity_filled / cumulative_quantity are (Codex review: "Prefer cumulative
# quantity over requested quantity").
_EXECUTED_QUANTITY_COLS = ("quantity_filled", "cumulative_quantity")
# `quantity` stays a LAST-RESORT alias for the recovery artifact shape, which
# records the filled amount there and carries no cumulative_quantity column.
_QUANTITY_COLS = (*_EXECUTED_QUANTITY_COLS, "quantity")
# Broker states that END an order without a fill. Every OTHER non-filled state
# ('pending', 'queued', 'confirmed', a blank cell) is still live and must stay
# as conservative submitted context -- the order may yet fill.
#
# Sourced from the canonical broker normalizer rather than re-declared: the
# hand-copied version here omitted `voided` and `expired`, so an order that
# could no longer fill was still carried as live unknown context and forced its
# ticker onto the $0 manual-review path (Codex review: "Treat voided and
# expired broker orders as terminal"). near_close_dca reads the same source.
# The literal is an import fallback, not a second source of truth.
try:  # pragma: no cover - import path varies with how the module is loaded
    from scripts.broker_reconciliation import _TERMINAL_ORDER_STATES as _BROKER_TERMINAL
except ImportError:  # pragma: no cover
    try:
        from broker_reconciliation import _TERMINAL_ORDER_STATES as _BROKER_TERMINAL  # type: ignore
    except ImportError:
        _BROKER_TERMINAL = frozenset(
            {"cancelled", "canceled", "rejected", "failed", "voided", "expired"}
        )
_TERMINAL_NON_FILL_STATES = frozenset(_BROKER_TERMINAL)


def _executed_quantity(row: Mapping[str, Any]) -> Optional[float]:
    """Executed share count for a row, or None when no executed column exists.

    An EXPLICIT ZERO in an executed column is authoritative and stops the
    search: it says the order filled nothing. _row_number alone cannot express
    that -- it skips any value <= 0 and so falls through to the requested
    `quantity`, reading a canceled zero-fill row as fully executed, which is the
    trap broker_reconciliation._normalize_order() documents.
    """
    for col in _EXECUTED_QUANTITY_COLS:
        raw = row.get(col)
        if raw in (None, "", "-"):
            continue
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return _row_number(row, _QUANTITY_COLS)

def _executed_quantity_is_explicit_zero(row: Mapping[str, Any]) -> bool:
    """True only when a quantity column states a definitive numeric ZERO.

    Mirrors near_close_dca._executed_quantity_is_explicit_zero. Stricter than
    ``not _executed_quantity(row)``: that conflates an explicit 0 with a missing
    column and an unparsable cell, and only the first is evidence that nothing
    executed. Callers that DROP a row on this answer must not read "unknown" as
    "nothing filled".

    ``_executed_quantity`` cannot express this on its own -- its ``quantity``
    fallback goes through ``_row_number``, which skips any value <= 0 and so
    reports a genuine explicit zero as None.
    """
    for col in _EXECUTED_QUANTITY_COLS:
        raw = row.get(col)
        if raw in (None, "", "-"):
            continue
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return False
        return math.isfinite(value) and value == 0
    raw = row.get("quantity")
    if raw in (None, "", "-"):
        return False
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value == 0


# EXECUTION price aliases only, for sizing deployed capital. _PRICE_COLS above
# leads with target_price/decision_price because a REFERENCE price legitimately
# falls back to the quote a decision was made at; multiplying executed quantity
# by that quote sizes the cash by what we hoped to pay rather than what we paid
# (target 12, average fill 10, quantity 0.2 -> $2.40 instead of $2.00), which
# overstates prior allocation and suppresses part of the later top-up (Codex
# review: "Use the executed fill price when sizing partials"). Ambiguous 'price'
# and the decision-bar proxy are deliberately absent: only a column that
# unambiguously records what the broker filled at belongs here.
_EXECUTED_PRICE_COLS = ("avg_fill_price", "average_fill_price", "average_price")


def top_up_context_roots(
    base: Optional[Path] = None,
    stream_roots: Optional[Iterable[Path | str]] = None,
) -> List[Path]:
    """Directories scanned for same-day prior-allocation / top-up context.

    The fixed manual subroots plus any existing scheduled-recovery directories
    (see :func:`recovery_context_roots`), so a recovery pass's fills offset a
    later same-day run instead of being re-bought.
    """
    parent = cron_tracking_root(base)
    return [parent / sub for sub in TOP_UP_CONTEXT_SUBROOTS] + recovery_context_roots(
        base, stream_roots
    )


def is_top_up_context_state(
    state: Mapping[str, Any], trading_day: Optional[str] = None
) -> bool:
    """True only for a same-day run usable as prior-allocation context.

    Enforces the context markers before a run's fills may be folded into the
    top-up guard. Two shapes qualify:

    * a MANUAL run -- ``run_origin='manual'`` with ``manual_run=true``; and
    * a SCHEDULED-RECOVERY run -- ``run_origin='scheduled_recovery'`` with
      ``manual_run=false`` and ``valid_recovery_context=true``. Such a pass
      recovers a failed scheduled slot into its own directory and is never
      scheduled evidence for that slot, but it moved real capital and so must
      offset a later same-day run (Codex review: "Add the recovery root to
      watchdog classification").

    Both shapes additionally require ``same_day_top_up_context_only=true``,
    ``valid_scheduled_evidence=false``, and -- when the key is present --
    ``valid_afternoon_scheduled_evidence=false``. When ``trading_day`` is given,
    the run's ``trading_day`` must match it. A run that is (or claims to be)
    scheduled evidence fails closed and is never returned as top-up context.
    """
    if not isinstance(state, dict):
        return False
    rc = state.get("run_classification")
    if not isinstance(rc, dict):
        return False
    origin = rc.get("run_origin")
    if origin == RUN_ORIGIN_SCHEDULED_RECOVERY:
        # Delegate to the single source of truth rather than checking a subset
        # of the flags here: the recovery contract covers stream/run_type/
        # state_schema_version correspondence, run_id, and the classification
        # trading_day, none of which a local flag check caught. The STATE-level
        # entry point, so a top-level artifact_contract_failure marker also
        # fails closed.
        return is_valid_recovery_context_state(state, trading_day)
    if origin == "manual":
        if rc.get("manual_run") is not True:
            return False
    else:
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
    # ET seconds-since-midnight of the run whose reference_price currently owns
    # this allocation (see _merge_prior). Sub-minute precision matters: two
    # artifacts generated in the same ET minute at different seconds must not
    # tie. None when reference_price is unknown or came from a run with no
    # parseable generated_at.
    reference_ts: Optional[int] = None
    # Count of qualifying CSV rows (broker buy attempts) folded into this
    # ticker, one per row regardless of order_id/amount -- NOT one per
    # contributing run/source. A single bare manual/recovery CSV can contain
    # multiple distinct orders for the same ticker; near_close_dca.py's
    # merge_manual_top_up_context previously used len(sources) as its
    # attempt-count proxy, which counts RUNS, not ORDERS, so several attempts
    # from one fallback-only run were reconstructed as a single attempt
    # against the shared max_orders_per_day breaker (Codex review: "Count
    # each fallback CSV order as an attempt"). Rows with a duplicate order_id
    # across merged sources are already excluded before _prior_from_rows ever
    # sees them (see load_same_day_top_up_context's seen_order_ids/
    # _exclude_seen_order_ids), so summing each source's own row count here
    # does not double-count a mirrored order.
    attempt_count: int = 0


def _row_ticker(row: Mapping[str, Any]) -> Optional[str]:
    for col in _TICKER_COLS:
        value = row.get(col)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _row_number(row: Mapping[str, Any], cols: Iterable[str]) -> Optional[float]:
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


# Candidate statuses that are placed-or-in-flight order evidence, matching
# scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES minus
# "not_submitted_confirmation_required" (which never reached the broker --
# it is pending a separate pre-authorization step, not real capital).
# Defined locally rather than imported: this module is invoked in contexts
# where a package-relative import of a scripts/ sibling is not always safe
# (see the try/except scripts.scheduled_artifacts/scheduled_artifacts
# import pattern elsewhere in this codebase), and the set is small and
# stable enough not to need a shared source of truth, matching dca_risk.py's
# identically-scoped _DEPLOYED_ORDER_STATUSES. Used by both
# _rows_from_buy_candidates and _read_csv_rows below: a reconciled
# "pending"/"unconfirmed" row, or an ambiguous "potentially_submitted"
# placement (OrderPlacementAmbiguousError), is real outstanding submission
# evidence too, and this loader is the ONLY path that ever reaches
# final_manual_review/** artifacts -- excluding any of them let a later
# scheduled run see no prior notional for a still-live manual order and
# submit it again (Codex review, PR #1300 discussion_r3920912119: "Retain
# reconciled in-flight manual orders").
_PLACED_OR_IN_FLIGHT_STATUSES = frozenset(
    {"submitted", "filled", "pending", "unconfirmed", "potentially_submitted"}
)


def _rows_from_buy_candidates(state: Mapping[str, Any]) -> List[Dict[str, Any]]:
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
    rows: List[Dict[str, Any]] = []
    # `state.get(k, []) or []` still yields the raw value for a malformed
    # non-list such as buy_candidates: 1, and iterating it raised TypeError out
    # of load_same_day_top_up_context() -- aborting the whole afternoon context
    # scan over one corrupt artifact instead of ignoring that collection. Same
    # normalization _order_ids_from_state and
    # near_close_dca.load_prior_same_day_allocations already apply (Codex
    # review: "Validate final-review candidate arrays before iterating").
    candidates = state.get("buy_candidates")
    if not isinstance(candidates, list):
        candidates = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        ticker = cand.get("ticker")
        if not (isinstance(ticker, str) and ticker.strip()):
            continue
        status = cand.get("status")
        # See _PLACED_OR_IN_FLIGHT_STATUSES's own comment above for why this
        # is the full placed-or-in-flight set, not just "submitted"/
        # "filled"/"potentially_submitted". _confirmed_fill stays False for
        # every status here except "filled" -- only that one is a confirmed
        # fill.
        if status not in _PLACED_OR_IN_FLIGHT_STATUSES:
            continue
        row = dict(cand)
        row["ticker"] = ticker
        row["_confirmed_fill"] = status == "filled"
        rows.append(row)
    return rows


def _read_csv_rows(
    csv_path: Path, *, skip_sink: Optional[List[Path]] = None
) -> List[Dict[str, Any]]:
    """Read one orders CSV's rows, keeping only submitted/filled statuses.

    Each returned row is tagged with an internal ``_confirmed_fill`` bool: True
    for an explicit ``status='filled'`` row, OR a STATUSLESS row from a file
    named ``filled_orders.csv`` (or carrying an ``amount_usd`` column) -- the
    documented manual-fill shape ``near_close_dca._orders_csv_fills`` already
    treats as a confirmed fill without a status column (Codex review: "Treat
    statusless filled-orders rows as confirmed fills"). Only a confirmed row's
    price may be trusted as a reference.

    Pass ``skip_sink`` to record ``csv_path`` when the read/parse fails, so a
    missing, unreadable, or mid-rewrite CSV is not silently indistinguishable
    from a genuinely empty one (Codex review: "Include bare manual CSV
    failures in scan completeness").
    """
    try:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            raw_rows = list(reader)
            fieldnames = reader.fieldnames
    except (OSError, csv.Error, UnicodeDecodeError):
        if skip_sink is not None:
            skip_sink.append(csv_path)
        return []
    if fieldnames is None:
        # The file has no header line at all -- observed in the instant
        # right after a truncating open(path, "w") but before even the
        # header was flushed. csv.DictReader returns rows == [] here just
        # as it does for a legitimately empty (zero-orders) file, so
        # fieldnames is the only reliable signal to tell them apart (Codex
        # review: "Reject incomplete bare manual CSV snapshots", mirroring
        # the same check already added to near_close_dca._orders_csv_fills
        # and _orders_csv_submitted_notionals).
        if skip_sink is not None:
            skip_sink.append(csv_path)
        return []
    if raw_rows and any(value is None for value in raw_rows[-1].values()):
        # The final row has FEWER fields than the header -- csv.DictReader
        # fills any missing trailing columns with None (its `restval`)
        # rather than raising, so a row observed mid-write with its last
        # record only partially flushed silently looked like a normal,
        # complete row instead of a scan failure. Drop the untrustworthy
        # tail row but keep whatever was already fully flushed before it.
        if skip_sink is not None:
            skip_sink.append(csv_path)
        raw_rows = raw_rows[:-1]
    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        status = (row.get("status") or "").strip().lower()
        # See _PLACED_OR_IN_FLIGHT_STATUSES's own comment above for why this
        # is the full placed-or-in-flight set, not just "submitted"/
        # "filled"/"potentially_submitted": a reconciled "pending"/
        # "unconfirmed" row is real outstanding submission evidence too, not
        # a rejected one (Codex review, PR #1300 discussion_r3920912119:
        # "Retain reconciled in-flight manual orders").
        if status and status not in _PLACED_OR_IN_FLIGHT_STATUSES:
            continue
        # The broker-native recovery shape has no `status` column at all and
        # records its outcome in `state`, so every one of its rows reached the
        # statusless is_manual_filled_shape branch below and was tagged a
        # confirmed fill regardless. The same gate now guards this fallback
        # reader as near_close_dca._orders_csv_fills: when the column is present
        # it is authoritative, and a blank cell is not a fill. Without it a
        # recovery run discoverable only through the bare-CSV fallback still
        # counted a state='rejected' row as deployed capital and could suppress
        # the 3:30 allocation (Codex review: "Filter recovery state in the
        # fallback CSV reader").
        # EXECUTED QUANTITY decides, not the state name -- see the matching gate
        # in near_close_dca._orders_csv_fills. A 'partially_filled' row, or a
        # 'canceled' one that executed some quantity before ending, is real
        # deployed capital; discarding it made both allocation loaders report no
        # prior buy and let the later run re-purchase the full amount (Codex
        # review: "Preserve executed partial fills in recovery CSVs").
        row_state = (row.get("state") or "").strip().lower() if "state" in row else None
        # The BROKER's answer outranks the submission status: state='filled'
        # with status='submitted' is a confirmed fill, and treating it as
        # unconfirmed poisoned that ticker's reference price to unknown and
        # forced the conservative $0 manual-review path (Codex review: "Let the
        # broker state confirm filled recovery rows"). Mirrors the same rule in
        # near_close_dca._orders_csv_fills.
        state_confirms_fill = row_state == "filled"
        partial_execution = False
        nonterminal_unexecuted = False
        if row_state is not None and not state_confirms_fill:
            executed = _executed_quantity(row)
            has_execution = executed is not None and executed > 0
            # Only a TERMINAL non-fill state with zero execution is dropped.
            # This reader feeds _prior_from_rows, whose whole job is
            # CONSERVATIVE submitted-order context: a 'pending', 'queued', or
            # blank-cell row may still fill, so dropping it let a later run see
            # no prior allocation and submit the full amount while the first
            # order was still live -- the double-buy this loader exists to
            # prevent. near_close_dca._orders_csv_submitted_notionals already
            # draws the line here (Codex review: "Retain nonterminal submitted
            # rows in the fallback reader").
            # An EXPLICIT numeric zero is required to drop the row. `executed
            # is None` also covers a missing column and an unparsable cell, and
            # those say UNKNOWN, not "nothing filled" -- dropping an unresolved
            # terminal row removes its notional entirely, so a later run sees no
            # prior allocation and can buy the full amount again (Codex review:
            # "Require explicit zero before dropping submitted notionals").
            if row_state in _TERMINAL_NON_FILL_STATES and _executed_quantity_is_explicit_zero(row):
                continue
            partial_execution = has_execution
            nonterminal_unexecuted = not has_execution
        # A row retained ONLY because its state is still live must not be
        # promoted to a confirmed fill by the statusless manual-fill heuristic.
        # It carries no execution, so trusting its logged price as a reference
        # would be strictly worse than the drop it replaces: the guard would
        # take price_not_below against a price nothing ever paid.
        # A PARTIAL execution is excluded from the heuristic too. The order's
        # remainder is still live and may reprice or cancel, so its price is not
        # a settled reference -- near_close_dca._orders_csv_fills draws exactly
        # this line, and leaving the statusless shape out of it meant a recovery
        # discoverable only through this fallback could authorize a later top-up
        # against a half-settled price (Codex review: "Keep statusless partial
        # executions unconfirmed"). Its executed notional still counts; only the
        # reference price is withheld.
        is_manual_filled_shape = (
            not status
            and not nonterminal_unexecuted
            and not partial_execution
            and (csv_path.name == "filled_orders.csv" or "amount_usd" in row)
        )
        row = dict(row)
        # A PARTIAL execution is deliberately not forced to confirmed: the order
        # may still reprice its unexecuted remainder, and the existing policy
        # trusts a reference price only from a confirmed full fill.
        row["_confirmed_fill"] = status == "filled" or state_confirms_fill or is_manual_filled_shape
        # Tagged so _prior_from_rows can size the row by EXECUTED quantity rather
        # than its recorded amount. A row that never reached state='filled'
        # records the amount REQUESTED, so a $10 order with $2 executed would
        # otherwise load as $10 of prior allocation and suppress the remaining
        # $8 top-up -- diverging from near_close_dca._orders_csv_fills, which
        # recomputes it (Codex review: "Compute partial-fill notional from
        # executed quantity").
        row["_partial_execution"] = partial_execution
        rows.append(row)
    return rows


def _dedupe_rank(row: Mapping[str, object]) -> int:
    """How authoritative a row is about an order's outcome. Higher wins.

    A confirmed fill outranks everything. A PARTIAL execution outranks a
    submission-only row: the partial carries the executed quantity that
    ``_prior_from_rows`` sizes the notional from, while the submission row
    records only what was REQUESTED. Keeping the submission row bypassed that
    sizing entirely, so a $2 partial against a $10 request loaded as $10 of
    prior allocation and suppressed the remaining $8 top-up (Codex review:
    "Prefer partial executions over duplicate submission rows").

    Rank is deliberately separate from ``_confirmed_fill``: a partial stays
    UNCONFIRMED so the unknown-reference-price policy is preserved -- the
    unexecuted remainder may still reprice, and only a full fill is trusted as
    a reference.
    """
    if row.get("_confirmed_fill"):
        return 2
    if row.get("_partial_execution"):
        return 1
    return 0


def _dedupe_rows_by_order_id(
    rows: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Deduplicate order rows by ``order_id``, preferring a confirmed fill row.

    The same order can appear in more than one sibling CSV for a single manual
    run directory (e.g. ``orders.csv`` and ``filled_orders.csv``), and each
    would contribute its own row to ``_prior_from_rows`` -- which would
    double-count that order's notional (Codex review: "Deduplicate orders
    repeated in sibling CSVs"). Rows with no usable ``order_id`` can't be
    matched to a duplicate and are kept as-is.
    """
    by_order_id: Dict[str, Dict[str, Any]] = {}
    unkeyed: List[Dict[str, Any]] = []
    for row in rows:
        oid = (row.get("order_id") or "").strip()
        if not oid or oid == "-":
            unkeyed.append(row)
            continue
        existing = by_order_id.get(oid)
        if existing is None:
            by_order_id[oid] = row
            continue
        if _dedupe_rank(row) > _dedupe_rank(existing):
            by_order_id[oid] = row
    return [*by_order_id.values(), *unkeyed]


def _exclude_seen_order_ids(
    rows: Iterable[Dict[str, Any]], seen_order_ids: Set[str]
) -> List[Dict[str, Any]]:
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
    kept: List[Dict[str, Any]] = []
    for row in rows:
        oid = (row.get("order_id") or "").strip()
        if oid and oid != "-":
            if oid in seen_order_ids:
                continue
            seen_order_ids.add(oid)
        kept.append(row)
    return kept


def _prior_from_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
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
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        ticker = _row_ticker(row)
        if not ticker:
            continue
        fill_price = _row_number(row, _PRICE_COLS)
        # Sized on the EXECUTION price, not the reference-price preference order
        # (which leads with target/decision). The two answer different questions
        # and only one of them is "what did this cost".
        executed_price = _row_number(row, _EXECUTED_PRICE_COLS)
        quantity = _executed_quantity(row)
        if quantity is not None and quantity <= 0:
            quantity = None
        if row.get("_partial_execution") and executed_price is not None and quantity is not None:
            # A partially-executed row's recorded amount is what was REQUESTED;
            # only price * executed-quantity is the cash that moved. Preferring
            # the recorded amount overstated prior allocation and suppressed the
            # unexecuted remainder's top-up. Mirrors the same recompute in
            # near_close_dca._orders_csv_fills.
            amount = executed_price * quantity
        else:
            amount = _row_number(row, _AMOUNT_COLS)
            if amount is None and fill_price is not None and quantity is not None:
                amount = fill_price * quantity
        first_row_for_ticker = ticker not in out
        entry = out.setdefault(
            ticker,
            {"prior_usd": 0.0, "reference_price": None, "order_ids": [], "attempt_count": 0},
        )
        # Every qualifying row is a real broker buy attempt regardless of
        # order_id/amount -- counted per ROW, not per contributing run (Codex
        # review: "Count each fallback CSV order as an attempt").
        entry["attempt_count"] = int(entry["attempt_count"]) + 1
        if amount is not None:
            entry["prior_usd"] = float(entry["prior_usd"]) + amount
        price = _row_number(row, _PRICE_COLS) if row.get("_confirmed_fill") else None
        if first_row_for_ticker:
            entry["reference_price"] = price
        elif price is None or entry["reference_price"] is None:
            entry["reference_price"] = None
        else:
            # Rows are read in file order, so the last confirmed-fill row is the
            # ticker's most recent same-day reference and its price wins.
            entry["reference_price"] = price
        oid = (row.get("order_id") or "").strip()
        if oid and oid != "-":
            entry["order_ids"].append(oid)
    return out


def _state_generated_before(state: Mapping[str, Any], boundary_et: Optional[time]) -> bool:
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
    state: Mapping[str, Any], run_dir: Path
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
    # isinstance, not truthiness: the core loader normalizes a non-list
    # buy_candidates to [] and falls through to its orders-CSV path, so
    # reporting "covered" for a truthy non-list (a populated dict) dropped the
    # run from BOTH loaders and let its ticker be bought again. The two must
    # agree on what counts as a readable candidate list (Codex review:
    # "Propagate normalized candidates to fallback coverage").
    candidates = state.get("buy_candidates")
    if isinstance(candidates, list) and candidates:
        return True
    artifacts = state.get("artifacts")
    if isinstance(artifacts, dict):
        recorded = artifacts.get("orders_csv_path")
        if isinstance(recorded, str) and recorded:
            recorded_path = Path(recorded)
            if not recorded_path.is_absolute():
                recorded_path = run_dir / recorded_path
            # A RECOVERY state's out-of-root recorded path is no longer followed
            # by the core loader (near_close_dca applies run-root containment
            # there), so treating it as "covered" here would drop the run from
            # BOTH loaders -- the same double-miss this function's stale-path
            # rule already guards against. The core loader retries the basename
            # inside run_dir, which this function's own fallback also reaches,
            # so report covered only when the path the core loader will
            # ACTUALLY read exists (Codex review: "Fall back to the recovery
            # run's local CSV").
            rc = state.get("run_classification")
            is_recovery = (
                isinstance(rc, Mapping)
                and rc.get("run_origin") == RUN_ORIGIN_SCHEDULED_RECOVERY
            )
            if is_recovery:
                # Contained against the enclosing RECOVERY ROOT, the same
                # boundary the core loader and the fallback's own CSV resolution
                # now use. Comparing against run_dir alone made this the ONLY
                # site still rejecting a legitimate '../filled_orders.csv' from
                # an artifacts/run_state.json: it rewrote the path to a
                # nonexistent artifacts/filled_orders.csv, reported NOT covered,
                # and so load_same_day_top_up_context() read the root CSV that
                # the core loader had already read -- merge_manual_top_up_context()
                # then counted the same fill twice, overstating prior allocation
                # and suppressing a legitimate top-up (Codex review: "Use the
                # recovery root when checking loader coverage").
                try:
                    resolved = recorded_path.resolve()
                    root_key = recovery_root_for(run_dir).resolve()
                    within = resolved == root_key or root_key in resolved.parents
                except OSError:
                    within = False
                if not within:
                    recorded_path = run_dir / recorded_path.name
            if recorded_path.exists():
                return True
    return False


# Sentinel for a source run with no parseable generated_at. Sorts before every
# real ET seconds-since-midnight value (0-86399) so an untimestamped run always
# loses a recency comparison to any timestamped run, but still wins later
# scan/merge order against other untimestamped runs -- mirrors
# near_close_dca._TS_FLOOR.
_REFERENCE_TS_FLOOR = -1


def _merge_prior(
    dst: Dict[str, PriorAllocation],
    src: Dict[str, Dict[str, Any]],
    source_label: str,
    run_ts: Optional[int] = None,
) -> None:
    """In-place merge of a raw ticker map into the aggregate PriorAllocation map.

    ``run_ts`` is the contributing run's own ``generated_at`` (ET
    seconds-since-midnight, see :func:`_et_seconds` -- sub-minute precision
    matters here: two artifacts generated in the same ET minute at different
    seconds must not tie and fall through to scan order) so recency is
    resolved by actual timestamp rather than by artifact scan/path order: a
    backfilled or manually-copied run directory whose name happens to sort
    after a genuinely later run must not overwrite that later run's reference
    price just because it was merged last. Ties and untimestamped runs
    (``run_ts=None``, treated as :data:`_REFERENCE_TS_FLOOR`) fall back to
    merge order, matching ``near_close_dca.load_prior_same_day_allocations``.

    Same poisoning rule as :func:`_prior_from_rows`: an unknown reference from
    this or any earlier merged source makes the ticker's aggregate reference
    unknown, rather than silently keeping an earlier known price.
    """
    run_key = run_ts if run_ts is not None else _REFERENCE_TS_FLOOR
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
            alloc.reference_ts = run_key if alloc.reference_price is not None else None
        elif price is None or alloc.reference_price is None:
            alloc.reference_price = None
            alloc.reference_ts = None
        else:
            # Most-recent (by generated_at) reference wins: overwrite only when
            # this source is at least as recent as whichever source currently
            # owns the reference.
            existing_key = alloc.reference_ts if alloc.reference_ts is not None else _REFERENCE_TS_FLOOR
            if run_key >= existing_key:
                alloc.reference_price = float(price)
                alloc.reference_ts = run_key
        for oid in entry.get("order_ids", []) or []:
            if oid not in alloc.order_ids:
                alloc.order_ids.append(oid)
        # Summed, not deduplicated like order_ids: this is each contributing
        # run's OWN qualifying-row count for this ticker, and a row whose
        # order_id already appeared in an earlier-merged source was already
        # excluded before _prior_from_rows ever built this run's entry (see
        # load_same_day_top_up_context's seen_order_ids), so a genuinely
        # distinct run's attempt_count here reflects only its own,
        # not-yet-counted attempts.
        alloc.attempt_count += int(entry.get("attempt_count") or 0)
        if source_label not in alloc.sources:
            alloc.sources.append(source_label)


def _mtime_matches_trading_day(path: Path, trading_day: str) -> bool:
    """True if ``path``'s last-modified time falls ON OR AFTER ``trading_day`` (Eastern).

    Mirrors ``near_close_dca._mtime_matches_trading_day``: scopes skip_sink to
    PLAUSIBLY relevant artifacts. A ``run_state*.json`` that fails to
    read/parse can't have its own ``trading_day`` field inspected (that's
    exactly what failed), so a permanently corrupted historical artifact
    accumulated anywhere under a recursively (``rglob``) scanned root would
    otherwise poison ``scan_had_failures`` forever, forcing an unconditional
    bounded reload on every future run regardless of trading day (Codex
    review: "Scope parse failures to plausible same-day states"). mtime is an
    imperfect but cheap, universal proxy: a same-day artifact still being
    flushed necessarily has a same-day mtime, while old corrupted files do
    not.

    NOT a strict equality check: an earlier-weekday artifact being backfilled
    or repaired on a LATER day (e.g. Monday's ``run_state.json`` rewritten
    while a Tuesday run's earlier-week scan reads it) is exactly as
    unverifiable as a same-day race, but its mtime can never equal Monday --
    an exact-equality check silently dropped that failure from skip_sink,
    understating real earlier-week spend against the shared weekly cap
    (Codex review: "Fail closed on later-modified prior-day artifacts"). A
    file can never legitimately be modified BEFORE the trading day it claims
    to represent, so "on or after" still excludes genuinely stale historical
    garbage (whose mtime predates trading_day) while also covering a
    later-day rewrite of an earlier day's artifact. If the path can't even be
    stat'd, or ``trading_day`` doesn't parse, err toward still treating it as
    a possible failure rather than silently dropping the signal.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    try:
        target = datetime.strptime(trading_day, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return True
    return datetime.fromtimestamp(mtime, tz=_EASTERN).date() >= target


def load_same_day_top_up_context(
    trading_day: str,
    base: Optional[Path] = None,
    *,
    boundary_et: Optional[time] = None,
    skip_sink: Optional[List[Path]] = None,
    stream_roots: Optional[Iterable[Path | str]] = None,
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
    :class:`PriorAllocation` (summed ``prior_usd``; per ticker, the MOST RECENT
    valid reference price wins, resolved by each contributing run's own
    ``generated_at`` -- ties and untimestamped runs defer to scan/merge order,
    see :func:`_merge_prior` -- with any unconfirmed/unknown price poisoning
    that ticker's reference; see :func:`_prior_from_rows`).

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

    Pass ``skip_sink`` to record every orders CSV that failed to read/parse
    (see :func:`_read_csv_rows`). A missing, unreadable, or mid-rewrite CSV
    here is exactly as unverifiable as a core-loader completeness failure --
    without this signal a ticker whose only same-day fill lives ONLY in an
    unreferenced sibling CSV (the case this loader exists to handle) could be
    silently absent from the caller's completeness flag, skip the bounded
    reload, and be evaluated as ``no_prior_run`` (Codex review: "Include bare
    manual CSV failures in scan completeness").

    ``stream_roots`` forwards the caller's RESOLVED live stream roots to
    :func:`top_up_context_roots`, so a scheduled-recovery directory under a
    relocated morning/midday/intraday/market-open root is scanned here too.
    Without it, a recovery whose fills exist only in a bare sibling CSV -- the
    exact shape this loader exists to catch -- is missed by BOTH the core
    loader and this fallback in such a deployment, and the later run evaluates
    the ticker as ``no_prior_run`` and allocates the full amount again (Codex
    review: "Thread configured roots into top-up loading"). The conventional
    roots are always scanned, so passing this is additive.
    """
    result: Dict[str, PriorAllocation] = {}
    seen_order_ids: Set[str] = set()
    for root in top_up_context_roots(base, stream_roots):
        for path in _iter_run_state_files(root):
            try:
                state = json.loads(path.read_bytes())
            except (json.JSONDecodeError, OSError):
                # An unreadable or mid-write run_state under a manual
                # top-up-context root is exactly as unverifiable as a
                # referenced-CSV read failure below -- without recording it,
                # scan_had_failures (bool(skip_sink) in run_dca.py) stays
                # False even though this file's fills, if any, were never
                # actually read, silently defeating the bounded-reload
                # completeness signal (Codex review: "Record manual
                # run-state parse failures in the skip sink"). Scoped to
                # PLAUSIBLY same-day artifacts via mtime, matching the core
                # loader's own skip_sink scoping -- these roots are scanned
                # recursively (rglob), so an unconditional record would let a
                # single permanently corrupted historical file poison
                # scan_had_failures forever, forcing an unnecessary bounded
                # reload on every future run (Codex review: "Scope parse
                # failures to plausible same-day states").
                if skip_sink is not None and _mtime_matches_trading_day(path, trading_day):
                    skip_sink.append(path)
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
            all_rows: List[Dict[str, Any]] = []
            # A RECOVERY state's CSV candidates are constrained to its own run
            # directory, matching near_close_dca's core loader and
            # collect_recovery_context_order_ids. _already_covered_... above
            # only decides whether to PROCESS the run; without the same
            # containment here, a copied recovery artifact whose stale absolute
            # path still exists (and has no same-named local file) had that
            # unrelated CSV's fills merged into prior allocation, which can
            # reduce or block the 3:30 buy (Codex review: "Keep external
            # recovery CSVs out of the fallback loader").
            if isinstance(rc, Mapping) and rc.get("run_origin") == RUN_ORIGIN_SCHEDULED_RECOVERY:
                # Contained against the enclosing RECOVERY ROOT, not the
                # run_state's own directory: an artifacts/run_state.json
                # legitimately references a root-level sibling CSV.
                referenced_csvs = _referenced_orders_csvs_within(
                    state, path.parent, recovery_root_for(path.parent)
                )
            else:
                referenced_csvs = _referenced_orders_csvs(state, path.parent)
            for csv_path in referenced_csvs:
                all_rows.extend(_read_csv_rows(csv_path, skip_sink=skip_sink))
            if not referenced_csvs and skip_sink is not None:
                artifacts = state.get("artifacts")
                recorded = artifacts.get("orders_csv_path") if isinstance(artifacts, dict) else None
                if recorded:
                    # The run_state explicitly recorded an orders_csv_path,
                    # but _referenced_orders_csvs' own basename/glob fallback
                    # found NOTHING at all -- neither the recorded path, its
                    # basename in this run's directory, nor any *orders.csv
                    # sibling exist. Every candidate is filtered out via
                    # `not path.is_file()` before _read_csv_rows is ever
                    # called, so the read-failure skip_sink signal added
                    # last round never fires (Codex review: "Report missing
                    # manual CSVs before filtering them out"). Record the
                    # run_state path itself so scan_had_failures reflects
                    # that this run's fill evidence is completely
                    # unverifiable, matching the analogous fix already
                    # applied to the core loader's referenced-CSV handling.
                    skip_sink.append(path)
            if _outside_core_loader_reach(path.parent):
                all_rows.extend(_rows_from_buy_candidates(state))
            deduped_rows = _dedupe_rows_by_order_id(all_rows)
            deduped_rows = _exclude_seen_order_ids(deduped_rows, seen_order_ids)
            csv_prior = _prior_from_rows(deduped_rows)
            run_ts = _et_seconds(state.get("generated_at"))
            _merge_prior(result, csv_prior, str(label), run_ts)
    return result


def incremental_top_up_amount(
    desired_amount: float,
    prior_usd: float,
    current_price: Optional[float],
    reference_price: Optional[float],
    minimum_order_usd: float = 1.0,
) -> tuple[float, str]:
    """Apply the same-day top-up guard for one ticker.

    Returns ``(incremental_amount, guard_result)`` where ``guard_result`` is one
    of the ``top_up_guard_result`` codes in schemas/run_state.schema.json. A
    later run buys only ``max(0, desired - prior)`` and only when its
    ``current_price`` is strictly below the ``reference_price``. When prior
    allocation exists but no comparable price is available, it conservatively
    buys $0 and flags manual review (``missing_prior_price``).
    """
    if isinstance(minimum_order_usd, bool) or minimum_order_usd <= 0:
        raise ValueError(
            f"minimum_order_usd must be a positive number, got {minimum_order_usd!r}"
        )
    if prior_usd <= 0:
        return max(0.0, desired_amount), "no_prior_run"
    if reference_price is None or current_price is None:
        return 0.0, "missing_prior_price"
    if not (current_price < reference_price):
        return 0.0, "price_not_below_prior_run"
    # Quantize to cents via Decimal to avoid float subtraction precision issues
    # (e.g. 2.30 - 1.30 producing 0.9999999999999998, which would incorrectly
    # skip a valid $1.00 top-up when minimum_order_usd is 1.0).
    desired_dec = Decimal(str(round(desired_amount, 2)))
    prior_dec = Decimal(str(round(prior_usd, 2)))
    min_order_dec = Decimal(str(round(minimum_order_usd, 2)))
    incremental_dec = desired_dec - prior_dec
    if incremental_dec <= 0:
        return 0.0, "no_incremental_difference"
    if incremental_dec < min_order_dec:
        return 0.0, "below_broker_minimum_order_size"
    return float(incremental_dec), "incremental_top_up"


# --- Scheduled-state selection for reporting ---------------------------------

@dataclass
class ScheduledSelection:
    state: Optional[Mapping[str, Any]]
    state_path: Optional[str]
    order_ids: List[str]
    used_broker_fallback: bool
    fallback_reason: Optional[str]
    excluded_manual_order_ids: List[str]
    order_submit_status: Optional[str] = None
    confirmation_blocked: bool = False
    errors: List[str] = field(default_factory=list)
    # Recovery-context IDs withheld from the broker time-window fallback. Empty
    # unless that fallback ran, since it is the only path that attributes orders
    # the scheduled state never named.
    excluded_recovery_context_order_ids: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state is not None and not self.errors


def select_scheduled_state_for_reporting(
    trading_day: str,
    base: Optional[Path] = None,
    broker_window_orders: Optional[Iterable[Mapping[str, object]]] = None,
    scheduled_state_write_failed: bool = False,
    stream_roots: Optional[Iterable[Path | str]] = None,
) -> ScheduledSelection:
    """Pick the scheduled run_state for a trading day and derive its order IDs.

    Enforces the reporting rules:
      * Only a validated scheduled run_state may be used.
      * Manual order IDs are always excluded, and recovery-context IDs are
        excluded from the broker time-window fallback (``stream_roots`` is
        passed through to :func:`collect_recovery_context_order_ids` so a
        deployment with relocated stream roots is scanned correctly).
      * Never infer scheduled status from broker orders alone.
      * Broker time-window fallback is allowed ONLY when a valid scheduled
        state exists but lacks order IDs due to a known write failure; even
        then manual order IDs are excluded AND only broker orders whose ET
        timestamp lands inside the attribution window are attributed -- 15:25-15:35
        for a canonical 1530ET state, 15:15-15:35 only for a legacy 1520ET state
        (see _attribution_window_start_min).

    ``broker_window_orders`` items are mappings carrying at least ``order_id``
    (or ``broker_order_id``) and ``submitted_at_et``: a bare ET wall-clock
    (``"15:30"``), a naive ET ISO timestamp, or a tz-aware ISO timestamp /
    ``datetime`` (converted to ET before the window compare, so UTC order history
    is handled correctly). Orders with no parseable in-window time are dropped
    rather than attributed.
    """
    manual_ids = collect_manual_order_ids(base)
    out_dir = scheduled_run_dir(trading_day, base)
    state_path = out_dir / RUN_STATE_FILENAME
    if not state_path.exists():
        # Codex review discussion_r3653976743 ("Fall back to legacy
        # directories for historical reporting"): for every trading day
        # before the 2026-07-27 retime, the canonical _1530ET directory never
        # existed -- those runs wrote (and still live) under the legacy
        # _1520ET suffix. Falling back here lets is_scheduled_state's own
        # legacy-identity acceptance actually run against that file instead
        # of reporting "no scheduled run_state" for every pre-retime day.
        legacy_state_path = legacy_scheduled_run_dir(trading_day, base) / RUN_STATE_FILENAME
        if legacy_state_path.exists():
            state_path = legacy_state_path

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
        state = json.loads(state_path.read_bytes())
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
    # Stays empty unless the broker time-window fallback runs; collecting the
    # recovery set is a filesystem scan and no other path needs it.
    excluded_recovery_ids: List[str] = []
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
            # A completed no-order run submitted nothing by design. Without this,
            # a healthy zero-order afternoon state combined with
            # scheduled_state_write_failed would attribute unrelated broker orders
            # in the window to this run, corrupting fill and slippage reporting
            # (Codex review).
            ORDER_SUBMIT_STATUS_NO_ORDERS,
        }
    ):
        # Valid scheduled state exists but has no usable order IDs due to a known
        # write failure: attribute broker orders that fall inside the scheduled
        # ET window, still excluding manual IDs.
        used_fallback = True
        fallback_reason = "scheduled state present but order IDs missing (known write failure)"
        rc_for_window = state.get("run_classification") if isinstance(state, dict) else None
        window_start_min = _attribution_window_start_min(rc_for_window)
        # Recovery-context IDs are excluded alongside manual ones. This branch
        # attributes orders the scheduled state never named, purely on their
        # timestamp, so an earlier-stream recovery placed inside the afternoon
        # window -- a delayed 14:30 recovery running at 15:27, say -- would be
        # assigned to the afternoon scheduled run and flow into scheduled
        # slippage and reliability, contradicting the contract that declares
        # those IDs non-scheduled (Codex review: "Exclude recovery IDs from the
        # reporting window fallback"). Collected only here: the non-fallback
        # paths use the state's OWN order IDs, and filtering those against a
        # recovery set could drop a genuine scheduled order.
        recovery_ids = collect_recovery_context_order_ids(
            base, trading_day, stream_roots=stream_roots
        )
        excluded_recovery_ids = sorted(recovery_ids)
        # A NORMAL earlier-stream scheduled run's orders are excluded too. The
        # contract says any order already claimed by an earlier stream artifact
        # is out of scope for the final run; recovery and manual were the only
        # claims collected, so a delayed 14:30 run placing at 15:27 fell inside
        # the canonical window and was attributed here (Codex review: "Exclude
        # all earlier-stream claims from the broker fallback").
        earlier_scheduled_ids = collect_earlier_scheduled_order_ids(
            base, trading_day, stream_roots=stream_roots
        )
        seen_fallback: Set[str] = set()
        kept = []
        for order in broker_window_orders:
            if not isinstance(order, Mapping):
                continue
            oid = order.get("order_id") or order.get("broker_order_id")
            if not (isinstance(oid, str) and oid) or oid in manual_ids or oid in seen_fallback:
                continue
            if oid in recovery_ids or oid in earlier_scheduled_ids:
                continue
            if not _in_scheduled_window_et(order.get("submitted_at_et"), window_start_min):
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
        excluded_recovery_context_order_ids=excluded_recovery_ids,
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
    "ORDER_SUBMIT_STATUS_NO_ORDERS",
    "RUN_CLASSIFICATION_COMPLETED_NO_ORDERS",
    "RUN_CLASSIFICATION_PLACEMENT_FAILED",
    "RUN_CLASSIFICATION_STARTUP_FAILED",
    "RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION",
    "RUN_CLASSIFICATION_MANUAL_PLACEMENT_FAILED",
    "COMPLETION_REASON_NO_LADDER_CANDIDATES",
    "COMPLETION_REASON_NO_QUALIFYING_ORDERS",
    "COMPLETION_REASON_BLOCKED_BEFORE_SPREAD_GATE",
    "COMPLETION_REASON_DATE_GATE_BLOCKED",
    "ERROR_CLASS_MISSING_SPREAD_DATA",
    "ERROR_CLASS_DEGRADED_MARKET_DATA",
    "ERROR_CLASS_STARTUP_FAILURE",
    "ERROR_CLASS_POST_SUBMISSION_FAILURE",
    "ERROR_CLASS_MISSING_RAW_INPUTS",
    "ERROR_CLASS_RECOVERY_FAILURE",
    "ERROR_CLASS_PARTIAL_PLACEMENT_FAILURE",
    "ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT",
    "BLOCKED_BY_SPREAD_GATE",
    "SPREAD_STATUS_OK",
    "SPREAD_STATUS_UNAVAILABLE",
    "SPREAD_STATUS_NOT_OK",
    "SPREAD_STATUS_NOT_EVALUATED",
    "SPREAD_STATUS_MISSING_BID_ASK",
    "SPREAD_UNAVAILABLE_STATUSES",
    "NoOrderOutcome",
    "classify_no_order_run",
    "deny_scheduled_evidence",
    "stamp_failure_classification",
    "spread_gate_fields",
    "row_blocked_for_missing_spread",
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
    "morning_preflight_blocked_run_state",
    "RECOVERY_TRIGGER_REASONS",
    "build_recovery_state",
    "recovery_escalation_run_state",
    "validate_classification",
    "is_manual_state",
    "is_scheduled_state",
    "write_run_artifacts",
    "update_latest_scheduled_run_pointer",
    "collect_manual_order_ids",
    "collect_recovery_context_order_ids",
    "classify_non_scheduled_order_ids",
    "exclude_manual_orders",
    "TOP_UP_CONTEXT_SUBROOTS",
    "top_up_context_roots",
    "RECOVERY_CONTEXT_SUBROOT_GLOBS",
    "RECOVERY_STREAM_CONTRACTS",
    "RECOVERY_STREAMS",
    "RECOVERY_RUN_TYPES",
    "RECOVERY_STATE_SCHEMA_VERSIONS",
    "validate_recovery_classification",
    "is_valid_recovery_context_state",
    "recovery_context_roots",
    "RUN_ORIGIN_SCHEDULED_RECOVERY",
    "is_top_up_context_state",
    "PriorAllocation",
    "load_same_day_top_up_context",
    "incremental_top_up_amount",
    "select_scheduled_state_for_reporting",
    "ScheduledSelection",
    "ValidationResult",
]
