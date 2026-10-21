#!/usr/bin/env python3
"""Gate 9 finalizer: write a live slot's scheduled evidence and refresh its pointer, in one command.

Why this exists (2026-09-22): every live-mirror prompt told the agent to
"write run_state.json ... and refresh <stream>/latest_<stream>_run.json" but
never named the code that does either, so each session improvised. That day's
run directories held Gate 5 documents and a free-form ``gate9_report_*.json``
but no ``run_state.json``, and the pointers were hand-written in a private
``{slot, trading_day, run_dir, outcome}`` shape that
``same_day_pointer.validate_pointer`` rejects as malformed. Every slot fired;
none of them could be verified (see ``scripts/verify_schedule.py``).

This command is the one Gate 9 step the prompts now call, on EVERY outcome
(orders placed, zero orders, blocked, END_SILENTLY, abort after the run
directory exists). It:

  1. refuses a ``--started-at`` outside the slot's ``dca_runner.SLOTS``
     acceptance window -- that run was not a scheduled fire and belongs under
     ``manual_runs/``, which never updates a pointer;
  2. stamps the run_classification the stream's OWN writer would
     (``<Executor>(mode="live").run_classification()`` for 09:35-14:30,
     ``scheduled_artifacts.build_afternoon_live_run_classification`` for
     15:30), so the identity -- cron id, run_type, schema version, account
     pinning -- cannot drift from the executors;
  3. writes ``run_state.json``/``run_summary.json`` plus header-only CSVs when
     none exist, and validates the directory with
     ``scheduled_artifacts.validate_run_artifacts``;
  4. refreshes the pointer through ``same_day_pointer.refresh_latest_pointer``
     (or ``run_classification.update_latest_scheduled_run_pointer`` for 15:30);
  5. re-reads both through ``validate_pointer`` and
     ``verify_schedule.classify_slot_evidence`` and exits non-zero unless the
     slot now verifies.

An existing ``run_state.json`` in the slot directory (e.g. one an executor's
own ``run()`` wrote) is never overwritten: step 3 is skipped and only the
pointer is refreshed from it.

Writes only under the cron-tracking root. No broker, no network, no orders.

Usage (from the prompts; run from the repo root)::

    python -m scripts.finalize_run_evidence --slot 1430 \\
        --started-at 2026-09-22T14:30:41-04:00 \\
        --outcome NO_ORDERS_GATE6_5_ZEROED --order-submit-status no_orders_submitted \\
        --orders-json <run_dir>/orders.json --report-json <run_dir>/gate9_report.json

Exit codes: 0 verified; 1 written but not verifiable (message says why);
2 refused before writing anything (bad arguments or off-window start).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from dca_fileio import atomic_open_w
from dca_paths import cron_tracking_root
from scripts import reason_codes
from scripts.file_lock import cross_process_lock
from scripts.run_classification import (
    ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
    ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
    ORDER_SUBMIT_STATUS_ERROR,
    ORDER_SUBMIT_STATUS_NO_CANDIDATES,
    ORDER_SUBMIT_STATUS_NO_ORDERS,
    ORDER_SUBMIT_STATUS_SUBMITTED,
    scheduled_run_id,
    update_latest_scheduled_run_pointer,
    validate_classification,
)
from scripts.same_day_pointer import (
    derive_valid_scheduled_evidence,
    refresh_latest_pointer,
    validate_pointer,
)
from scripts.scheduled_artifacts import (
    ORDER_ATTEMPT_STATUSES,
    ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE,
    SIGNAL_CSV_NAMES,
    SUBMITTED_ORDERS_CSV_FILENAME,
    build_afternoon_live_run_classification,
    build_run_summary,
    validate_run_artifacts,
    write_signals_csv,
    write_submitted_orders_csv,
)
from scripts.verify_schedule import (
    ET,
    SLOT_EVIDENCE_LAYOUT,
    STATUS_VERIFIED,
    classify_slot_evidence,
    slot_run_dir_relative,
)

# error_class a live prompt passes when place_equity_order is refused by the
# session's permission layer before the broker answers (Group C, 2026-10-17).
PLATFORM_REFUSAL_ERROR_CLASS = "platform_refusal"

# The live-run vocabulary only: dry_run is deliberately absent, since a dry run
# is never scheduled evidence and must not be finalized as one.
ORDER_SUBMIT_STATUSES = (
    ORDER_SUBMIT_STATUS_SUBMITTED,
    ORDER_SUBMIT_STATUS_NO_ORDERS,
    ORDER_SUBMIT_STATUS_NO_CANDIDATES,
    ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
    ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
    ORDER_SUBMIT_STATUS_ERROR,
    ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE,
)

# Pointer file per stream, beside the stream's run directories. Each name is
# the executor class's own latest_pointer_name (pinned by a test); the 3:30
# pointer goes through run_classification's own helper instead.
POINTER_NAMES = {
    "market_open": "latest_market_open_run.json",
    "morning": "latest_morning_run.json",
    "midday": "latest_midday_run.json",
    "intraday": "latest_intraday_run.json",
}
# The stream-specific signals filename each writer emits; any SIGNAL_CSV_NAMES
# entry satisfies the contract, so this only picks the header-only file's name.
SIGNALS_FILENAMES = {
    "market_open": "market_open_signals.csv",
    "morning": "morning_signals.csv",
    "midday": "midday_signals.csv",
    "intraday": "intraday_signals.csv",
    "afternoon": "signals.csv",
}


_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


class FinalizeRefused(ValueError):
    """Raised before anything is written; maps to exit code 2."""


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_slot(value: str) -> str:
    slot = value.strip().upper().removesuffix("ET")
    if slot not in SLOT_EVIDENCE_LAYOUT:
        raise FinalizeRefused(f"unknown slot {value!r}; expected one of {sorted(SLOT_EVIDENCE_LAYOUT)}")
    return slot


def parse_started_at(value: str) -> datetime:
    """Parse the run's start instant; a naive value is ET, like every other clock in this repo."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalizeRefused(f"--started-at {value!r} is not ISO-8601") from exc
    return (parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed).astimezone(ET)


# The live prompts' own Gate 1 opens EARLY_START_MINUTES before the nominal
# time (e.g. 2:25-2:55 PM for the 2:30 slot, 12:25 PM-1:00 PM for 12:30),
# while dca_runner.SLOTS opens AT nominal for every slot (09:35 included, and
# it gets no early allowance here). A fire
# that passed the prompt's Gate 1 at 14:26 is a real scheduled fire, so this
# accepts the union of the two: never refuse evidence Gate 1 already accepted.
EARLY_START_MINUTES = 5


def acceptance_window(slot: str, day: date) -> tuple[datetime, datetime]:
    """The union of the slot's dca_runner window and its live prompt's Gate 1 window, in ET."""
    from datetime import timedelta

    from dca_runner import SLOTS

    spec = SLOTS[slot]
    nominal = datetime.combine(day, time(*spec.nominal_et), tzinfo=ET)
    start = datetime.combine(day, time(*spec.window_start_et), tzinfo=ET)
    # 0935 must never fire before 9:35, so it gets no early-start allowance.
    if slot != "0935":
        start = min(start, nominal - timedelta(minutes=EARLY_START_MINUTES))
    return start, datetime.combine(day, time(*spec.window_end_et), tzinfo=ET)


def check_window(slot: str, started: datetime) -> None:
    """Refuse a start outside every window Gate 1 could have accepted for this slot."""
    start, end = acceptance_window(slot, started.date())
    # End EXCLUSIVE, exactly as dca_runner.gate_time_sanity checks it
    # (start <= local < end): a fire at 11:00:00 for the 10:30 slot was
    # rejected by Gate 1, so it must not be finalized as scheduled evidence
    # (Copilot review, PR #1514).
    if not start <= started < end:
        raise FinalizeRefused(
            f"--started-at {started.isoformat()} is outside slot {slot}'s acceptance window "
            f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} ET; an off-window run is not a scheduled "
            "fire and must be written under the stream's manual_runs/ instead"
        )


def build_classification(slot: str, started: datetime) -> Dict[str, Any]:
    """The run_classification the stream's own writer stamps for a scheduled live run at ``started``."""
    stream, _ = SLOT_EVIDENCE_LAYOUT[slot]
    if stream == "afternoon":
        day = started.date().isoformat()
        # valid_scheduled_evidence is finalized from the completed state below.
        return build_afternoon_live_run_classification(
            trading_day=day,
            run_id=scheduled_run_id(day),
            valid_scheduled_evidence=False,
            triggered_at_utc=_utc_iso(started),
        )
    import intraday_dca
    import market_open_dca
    import midday_dca
    import morning_dca

    executor = {
        "market_open": lambda: market_open_dca.MarketOpenDCAExecutor(mode="live"),
        "morning": lambda: morning_dca.MorningDCAExecutor(mode="live"),
        "midday": lambda: midday_dca.MiddayDCAExecutor(mode="live"),
        "intraday": lambda: intraday_dca.IntradayDCAExecutor(mode="live", slot=slot),
    }[stream]()
    # Constructing an executor reads config only; it writes nothing and opens
    # no ledger file, so this is identity lookup, not a run.
    return executor.run_classification(started)


def _load_json(path: Optional[str], kind: type, label: str) -> Any:
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise FinalizeRefused(f"{label} {path!r} is not readable JSON: {exc}") from exc
    if not isinstance(value, kind) or (kind is list and not all(isinstance(row, dict) for row in value)):
        raise FinalizeRefused(f"{label} must be a JSON {'array of objects' if kind is list else 'object'}")
    return value


def _load_inline_or_file_json(value: Optional[str], label: str) -> Any:
    """``--code-version-json`` takes the object itself or a path to a file holding it.

    The other ``--*-json`` flags take a path only. A Gate 9 command line already
    has the pinned SHA in an environment variable, and a ten-line object is
    easier to pass inline than to write to a scratch file first, so a value that
    opens with ``{`` is parsed in place; anything else is read as a path exactly
    like its siblings. Both forms reject a duplicate key: ``json.loads`` keeps
    the last one silently, which would make the release identity ambiguous
    (Copilot review, PR #1571: "Reject duplicate git_sha keys").
    """
    if value is None:
        return None
    inline = value.lstrip().startswith("{")
    try:
        text = value if inline else Path(value).read_text(encoding="utf-8")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError) as exc:
        where = "is not valid JSON" if inline else f"{value!r} is not readable JSON"
        raise FinalizeRefused(f"{label} {where}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FinalizeRefused(f"{label} must be a JSON object")
    return parsed


BUDGET_TIERS = ("normal", "elevated", "high_volatility", "stress")


def validate_orders(orders: Optional[List[Dict[str, Any]]]) -> None:
    """Refuse order rows that cannot be submitted-order evidence (Copilot review, PR #1514).

    Every row must name a ticker and a broker order_id, carry a positive
    finite submitted_amount_usd, and a status the drift detector counts as a
    placed or in-flight order (scheduled_artifacts.ORDER_ATTEMPT_STATUSES).
    A rejected or dry-run row is not an order and would otherwise inflate
    orders_submitted_count and mark the pointer valid on nothing. Duplicate
    order_ids are refused rather than silently collapsed.
    """
    if orders is None:
        return
    seen: set = set()
    for index, row in enumerate(orders):
        ticker, order_id, status = row.get("ticker"), row.get("order_id"), row.get("status")
        amount = row.get("submitted_amount_usd")
        if not isinstance(ticker, str) or not ticker.strip():
            raise FinalizeRefused(f"order row {index} has no ticker")
        if not isinstance(order_id, str) or not order_id.strip():
            raise FinalizeRefused(f"order row {index} ({ticker}) has no broker order_id")
        if status not in ORDER_ATTEMPT_STATUSES:
            raise FinalizeRefused(
                f"order row {index} ({ticker}) status {status!r} is not a placed or in-flight "
                f"order; expected one of {sorted(ORDER_ATTEMPT_STATUSES)}"
            )
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            raise FinalizeRefused(
                f"order row {index} ({ticker}) submitted_amount_usd {amount!r} is not a positive number"
            )
        if order_id in seen:
            raise FinalizeRefused(f"order_id {order_id!r} appears more than once")
        seen.add(order_id)


def validate_budget(budget: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a supplied Gate 6 budget block to schemas/run_state.schema.json's ``budget`` shape.

    ``{"budget_evaluated": false}`` is the schema's own form for a fire that
    exited before Gate 6. Anything else must carry the tier and both caps Gate
    6 selected; the tier is lower-cased because the Gate 9 reports spell it
    "Normal" while the schema enum is lower-case.
    """
    if budget is None or budget.get("budget_evaluated") is False:
        return None if budget is None else dict(budget)
    tier = budget.get("budget_tier")
    normalized = {**budget, "budget_tier": tier.strip().lower() if isinstance(tier, str) else tier}
    if normalized["budget_tier"] not in BUDGET_TIERS:
        raise FinalizeRefused(f"--budget-json budget_tier {tier!r} is not one of {list(BUDGET_TIERS)}")
    for key in ("daily_cap_usd", "weekly_cap_usd"):
        value = budget.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise FinalizeRefused(f"--budget-json {key} {value!r} is not a non-negative number")
    return normalized


CODE_VERSION_KEYS = ("git_sha", "tag", "pinned_by")


def validate_code_version(code_version: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Check the optional ``code_version`` block that names the release which produced this evidence.

    A cloud run trades from a checkout pinned by a full commit SHA
    (scripts/cloud_preflight.py). The evidence has to say which commit that was,
    or nobody can tell afterwards what code placed an order. Only a full
    lower-case 40-character SHA is accepted: an abbreviated one is ambiguous and
    a mixed-case one never equals what ``git rev-parse`` prints. Unknown keys are
    refused so the block cannot grow into a free-form note that hides a secret.
    """
    if code_version is None:
        return None
    if not isinstance(code_version, dict):
        raise FinalizeRefused("--code-version-json must be a JSON object")
    unknown = sorted(set(code_version) - set(CODE_VERSION_KEYS))
    if unknown:
        raise FinalizeRefused(f"--code-version-json has unknown key(s) {unknown}; allowed: {list(CODE_VERSION_KEYS)}")
    sha = code_version.get("git_sha")
    if not isinstance(sha, str) or not _GIT_SHA_RE.fullmatch(sha):
        raise FinalizeRefused("--code-version-json git_sha must be a full lower-case 40-character commit SHA")
    for key in ("tag", "pinned_by"):
        if key in code_version and not (isinstance(code_version[key], str) and code_version[key].strip()):
            raise FinalizeRefused(f"--code-version-json {key} must be a non-empty string")
    return dict(code_version)


PINNED_SHA_ENV = "DCA_PINNED_SHA"


def check_code_version_pin(code_version: Optional[Dict[str, Any]], env: Mapping[str, str]) -> None:
    """Refuse a ``code_version`` whose SHA is not the release ``$DCA_PINNED_SHA`` names.

    ``validate_code_version`` proves only the SHA's shape, so without this a
    caller could stamp any valid 40-character SHA on a cloud run and produce
    authoritative-looking but false provenance. The pin lives in the run
    environment, outside the repo (scripts/cloud_preflight.py). Where it is
    unset (a local or non-cloud run) there is nothing to bind to.
    """
    pin = env.get(PINNED_SHA_ENV) or ""
    if code_version is None or not pin or not isinstance(code_version, dict):
        return
    if code_version.get("git_sha") != pin:
        raise FinalizeRefused(f"--code-version-json git_sha is not the pinned release named by {PINNED_SHA_ENV}")


def _merge_code_version(run_dir: Path, state: Dict[str, Any], code_version: Dict[str, Any]) -> None:
    """Add the immutable ``code_version`` to an executor-written state and its summary.

    Summary first, state last: a crash between them leaves a state without the
    field, which the next call simply adds again, never a state that claims a
    release its summary does not carry.
    """
    summary_path = run_dir / "run_summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_bytes())
        except (OSError, ValueError):
            summary = None
        if isinstance(summary, dict):
            summary["code_version"] = code_version
            with atomic_open_w(summary_path) as handle:
                json.dump(summary, handle, indent=2, sort_keys=True, default=str)
    state["code_version"] = code_version
    with atomic_open_w(run_dir / "run_state.json") as handle:
        json.dump(state, handle, indent=2, sort_keys=True, default=str)


# The zero-order statuses a session could use to dress a data outage up as a
# quiet tape. On 2026-09-18 the 12:30 and 3:30 sessions both lost market data,
# wrote an empty candidates file by hand, and ended silently as though the
# universe had simply not qualified -- no alert, and a Run Log that read "no-op"
# for what was an outage (Notion DCA Issues: fabricated_empty_candidates,
# logged_as_quiet_tape). Every prompt already states the quiet-tape test in
# prose; nothing enforced it, so this command -- the one step every slot runs
# -- now does.
ZERO_ORDER_STATUSES = (ORDER_SUBMIT_STATUS_NO_ORDERS, ORDER_SUBMIT_STATUS_NO_CANDIDATES)
# Count fields both Gate 5 generators publish (scripts/stream_candidates.py's
# build_document for 09:35-14:30, scripts/generate_buy_candidates.py for
# 15:30). A file lacking any of these was not written by a generator -- which
# is exactly what a hand-typed ``[]`` or ``{"candidates": []}`` looks like.
GATE5_COUNT_KEYS = ("evaluated_count", "buy_count", "provider_error_count")
NO_DATA_ACTION = "NO_DATA"


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reject_duplicate_keys(pairs: List[Any]) -> Dict[str, Any]:
    """``object_pairs_hook`` that raises on a duplicate key, like ``dca_config``'s.

    ``json.loads`` silently keeps the LAST of two same-named keys, so a
    hand-edited evidence file could carry two conflicting ``evaluated_count``
    (or ``trading_day``) values and be judged on whichever came last --
    ambiguous precisely on the fabrication path this check exists for
    (Copilot review, PR #1519). Such a file is invalid evidence, not a document.
    """
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r} in JSON document")
        result[key] = value
    return result


def _candidate_rows(doc: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    rows = doc.get("candidates")
    if isinstance(rows, list) and all(isinstance(row, dict) and "action" in row for row in rows):
        return rows
    return None


def gate5_usable_counts(doc: Dict[str, Any]) -> tuple[int, int]:
    """``(usable, no_data)`` for a generator document.

    Counted from the per-ticker ``candidates`` rows whenever the document
    carries them: a row is usable when its action is not ``NO_DATA`` and it is
    not a provider error. Both generators publish those rows, but only
    stream_candidates publishes ``no_data_count`` -- the 15:30 document does
    not, so reading that field with a 0 default made an all-``NO_DATA`` 15:30
    document look fully usable, the very outage this guard exists to catch
    (Copilot review, PR #1519). Counting rows also stops a provider-error
    ticker, which the executor scores as ``NO_DATA``, from being subtracted
    twice. Only a document without rows falls back to its counts.
    """
    rows = _candidate_rows(doc)
    if rows is not None:
        no_data = sum(1 for row in rows if row["action"] == NO_DATA_ACTION)
        usable = sum(1 for row in rows if row["action"] != NO_DATA_ACTION and not row.get("provider_error"))
        return usable, no_data
    no_data = doc["no_data_count"]
    return doc["evaluated_count"] - doc["provider_error_count"] - no_data, no_data


def find_gate5_documents(run_dir: Path, slot: str, day: date) -> tuple[List[tuple[Path, Dict[str, Any]]], List[str]]:
    """This slot's Gate 5 generator documents in ``run_dir`` (newest first), plus the names of
    candidate-looking JSON files that are NOT one.

    Matched by content, never by filename: the live sessions have saved the
    generator's stdout as ``candidates_<day>_<slot>.json``, ``gate5_document.json``,
    ``cands_output.json`` and more. A document for another slot or day (a
    copied-in file) does not count as this run's evidence, and neither does
    one that says nothing about missing data (no ``candidates`` rows and no
    ``no_data_count``). The second list is filenames containing "cand" that
    fail that test, plus ANY file with duplicate JSON keys; beside a real
    document a "cand" file is a harmless extract (the ``cands`` array saved on
    its own), but on its own it is the 2026-09-18 fabrication signature.
    """
    documents: List[tuple[float, Path, Dict[str, Any]]] = []
    strays: List[str] = []
    if not run_dir.is_dir():
        return [], []
    for path in sorted(run_dir.glob("*.json")):
        if path.name in ("run_state.json", "run_summary.json"):
            continue
        try:
            loaded = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
        except ValueError as exc:
            if "duplicate key" in str(exc):
                strays.append(path.name)
                continue
            loaded = None
        except OSError:
            loaded = None
        is_document = (
            isinstance(loaded, dict)
            and all(_is_count(loaded.get(key)) for key in GATE5_COUNT_KEYS)
            and (_candidate_rows(loaded) is not None or _is_count(loaded.get("no_data_count")))
            and str(loaded.get("runner_slot_id")) == slot
            and loaded.get("trading_day") == day.isoformat()
        )
        if is_document:
            documents.append((path.stat().st_mtime, path, loaded))
        elif "cand" in path.name.lower():
            strays.append(path.name)
    documents.sort(key=lambda item: item[0], reverse=True)
    return [(path, doc) for _, path, doc in documents], strays


RELABEL_HINT = (
    "if market data failed, finalize as --order-submit-status not_submitted_error "
    '--error-class market_data_unavailable --error-detail "<what failed>" and alert per Gate 9'
)


def check_zero_order_gate5_evidence(
    run_dir: Path, slot: str, day: date, *, order_submit_status: str, error_class: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Refuse a zero-order finalize whose Gate 5 evidence cannot tell a quiet tape from a data outage.

    Returns the ``gate5_evidence`` block recorded in run_state.json, or None
    when the check does not apply. It applies only to a zero-order status
    WITHOUT ``--error-class``: a declared failure is already an alert, and
    ``submitted`` has orders to show for itself.

    * ``not_submitted_no_candidates`` claims Gate 5 ran and produced nothing,
      so it needs this slot's generator document.
    * ``no_orders_submitted`` also covers endings before Gate 5 (a Gate 4
      stand-down), so a missing document is accepted -- unless a hand-made
      candidates file sits in the directory instead, which is the
      2026-09-18 signature.
    * With a document and ``buy_count == 0``, at least one evaluated ticker
      must have had usable data (see ``gate5_usable_counts``). One provider
      error is routine (it is in most real documents), so the test is
      "anything usable", not "no errors". ``evaluated_count == 0`` with
      ``already_covered_tickers`` is Gate 4's served case, not an outage.
      A document with ``buy_count > 0`` (Gate 6.5 zeroed every buy) passes.

    A session that forges a whole generator document with invented counts
    gets through; this catches the shortcut that actually happened, not
    deliberate forgery.
    """
    if order_submit_status not in ZERO_ORDER_STATUSES or error_class:
        return None
    documents, strays = find_gate5_documents(run_dir, slot, day)
    if not documents:
        if order_submit_status == ORDER_SUBMIT_STATUS_NO_CANDIDATES:
            raise FinalizeRefused(
                "not_submitted_no_candidates needs this slot's Gate 5 generator document in the run "
                f"directory (none for runner_slot_id {slot} / {day.isoformat()}); save the generator's "
                f"output there, or {RELABEL_HINT}"
            )
        if strays:
            raise FinalizeRefused(
                f"{', '.join(strays)} look like candidate files but none is a Gate 5 generator document "
                f"for runner_slot_id {slot} / {day.isoformat()}; a hand-written candidates file (or the "
                f"cands array saved without its document, or a file with duplicate JSON keys) is not "
                f"evidence of a quiet tape -- save the generator's full output document in the run "
                f"directory, or {RELABEL_HINT}"
            )
        return None
    path, doc = documents[0]
    evaluated = doc["evaluated_count"]
    usable, no_data = gate5_usable_counts(doc)
    evidence = {
        "document": path.name,
        "evaluated_count": evaluated,
        "usable_count": max(usable, 0),
        "buy_count": doc["buy_count"],
        "provider_error_count": doc["provider_error_count"],
        "no_data_count": no_data,
    }
    if doc["buy_count"] > 0:
        return evidence
    if evaluated == 0 and doc.get("already_covered_tickers"):
        return {**evidence, "served_by_compatible_executor": True}
    if usable <= 0:
        raise FinalizeRefused(
            f"{path.name} shows zero candidates from zero usable tickers (evaluated {evaluated}, "
            f"provider errors {doc['provider_error_count']}, no data {no_data}): "
            f"that is a data outage, not a quiet tape -- {RELABEL_HINT}"
        )
    return evidence


def check_existing_state_gate5_evidence(run_dir: Path, slot: str, day: date, state: Dict[str, Any]) -> None:
    """The same outage check for a run_state.json this command did not write.

    Without it, an executor-owned (or hand-written) zero-order state beside a
    hand-made candidates file verified untouched: that path only refreshed the
    pointer (Copilot review, PR #1519). An executor's own ``run()`` records its
    evaluation in ``counts`` -- ``evaluated`` and ``no_data``, where a provider
    error is already a ``NO_DATA`` signal -- so that is the evidence when
    present; a state without it (hand-written) must pass the run directory's
    generator-document check instead.
    """
    status = state.get("order_submit_status")
    if status not in ZERO_ORDER_STATUSES or state.get("error_class"):
        return
    counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
    evaluated, no_data = counts.get("evaluated"), counts.get("no_data")
    if not (_is_count(evaluated) and _is_count(no_data)):
        check_zero_order_gate5_evidence(run_dir, slot, day, order_submit_status=status, error_class=None)
        return
    buys = counts.get("buy_candidates")
    if _is_count(buys) and buys > 0:
        return
    if evaluated - no_data <= 0:
        raise FinalizeRefused(
            f"run_state.json (not written by this command) records zero candidates from zero usable "
            f"tickers (evaluated {evaluated}, no data {no_data}): that is a data outage, not a quiet "
            f"tape -- {RELABEL_HINT}"
        )


def _csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)]


def build_run_state(
    slot: str,
    started: datetime,
    *,
    outcome: str,
    order_submit_status: str,
    orders_submitted_count: int,
    error_class: Optional[str] = None,
    error_detail: Optional[str] = None,
    report: Optional[Dict[str, Any]] = None,
    budget: Optional[Dict[str, Any]] = None,
    code_version: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    stream, _ = SLOT_EVIDENCE_LAYOUT[slot]
    classification = build_classification(slot, started)
    # The executors stamp the pinned account number into their classification;
    # the live prompts' secret rule says it never goes into an artifact, and
    # this command's artifacts are read by the console and copied into reports
    # (Copilot review, PR #1514). Keep only the fact that it was pinned.
    if classification.pop("account_number", None) is not None:
        classification["account_number_redacted"] = True
    state: Dict[str, Any] = {
        "run_id": classification["run_id"],
        "trading_day": started.date().isoformat(),
        "generated_at": _utc_iso(now or datetime.now(timezone.utc)),
        "broker": "robinhood",
        "dry_run": False,
        "stream": stream,
        "slot": f"{slot}ET",
        "runner_slot_id": slot,
        "cron_id": classification.get("cron_id"),
        "status": outcome,
        "order_submit_status": order_submit_status,
        "orders_submitted_count": orders_submitted_count,
        # schemas/run_state.schema.json requires both (Copilot review, PR #1514).
        # A missing budget means Gate 6 never ran on this fire -- the schema's
        # own budget_evaluated=false form; finalize() refuses that claim for a
        # run that placed orders.
        "budget": budget if budget is not None else {"budget_evaluated": False},
        "counts": {"submitted": orders_submitted_count},
        "run_classification": classification,
        # Names the writer so a reader can tell this state from one an
        # executor's own run() produced (which carries candidates, ledger
        # seeding and the rest of the executor-specific payload).
        "finalized_by": "scripts.finalize_run_evidence",
    }
    if error_class:
        state["error_class"] = error_class
    if error_class == PLATFORM_REFUSAL_ERROR_CLASS:
        # A distinct, machine-readable marker so a permission refusal is never
        # confused with a cap breach or a rogue-executor halt, and so the
        # alert path has something to key on.
        state["reason_code"] = reason_codes.PLATFORM_REFUSAL
        state["alert_required"] = True
    if error_detail:
        state["error_detail"] = error_detail
    if report is not None:
        state["gate9_report"] = report
    if code_version is not None:
        state["code_version"] = code_version
    # Finalize the placeholder from the completed state, exactly as the
    # executors' build_artifacts() does -- never asserted by the caller.
    evidence = derive_valid_scheduled_evidence(state)
    classification["valid_scheduled_evidence"] = evidence
    if stream == "afternoon":
        classification["valid_afternoon_scheduled_evidence"] = evidence
    return state


def finalize(
    slot: str,
    started: datetime,
    *,
    outcome: str,
    order_submit_status: str,
    root: Optional[Path] = None,
    orders: Optional[List[Dict[str, Any]]] = None,
    signals: Optional[List[Dict[str, Any]]] = None,
    report: Optional[Dict[str, Any]] = None,
    budget: Optional[Dict[str, Any]] = None,
    error_class: Optional[str] = None,
    error_detail: Optional[str] = None,
    code_version: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Write (or reuse) the slot's run_state, refresh its pointer, and report whether it verifies.

    Every argument check that needs no disk state runs first, so a bad call
    refuses before anything -- even the lock file -- is created. The rest runs
    under one per-run ``cross_process_lock``: two overlapping Gate 9 retries of
    the same fire would otherwise both read the prior submitted_orders.csv,
    merge different order sets, and let the last writer silently drop the
    other's orders (Copilot review, PR #1514).
    """
    slot = normalize_slot(slot)
    started = started.astimezone(ET)
    if dry_run:
        budget = validate_budget(budget)
        code_version = validate_code_version(code_version)
        return _finalize_dry_run(
            slot, started, outcome=outcome, order_submit_status=order_submit_status,
            root=root, orders=orders, report=report, budget=budget,
            error_class=error_class, error_detail=error_detail, code_version=code_version,
        )
    check_window(slot, started)
    if order_submit_status not in ORDER_SUBMIT_STATUSES:
        raise FinalizeRefused(
            f"--order-submit-status {order_submit_status!r} is not one of {list(ORDER_SUBMIT_STATUSES)}"
        )
    if not outcome.strip():
        raise FinalizeRefused("--outcome must name the Gate 9 outcome")
    if error_class == PLATFORM_REFUSAL_ERROR_CLASS and order_submit_status not in (
        ORDER_SUBMIT_STATUS_ERROR,
        ORDER_SUBMIT_STATUS_SUBMITTED,
    ):
        # Earlier orders in the batch may have filled before the refusal
        # ("submitted"); with none, it is an error -- never a quiet no-op.
        raise FinalizeRefused(
            f"a platform refusal must finalize as {ORDER_SUBMIT_STATUS_ERROR!r} (or "
            f"{ORDER_SUBMIT_STATUS_SUBMITTED!r} if orders were placed first), not {order_submit_status!r}"
        )
    validate_orders(orders)
    budget = validate_budget(budget)
    code_version = validate_code_version(code_version)
    # cron_tracking_root() returns a str; `base / ...` below needs a Path (3:30 PM 2026-10-19 TypeError).
    base = Path(root) if root is not None else Path(cron_tracking_root())
    run_dir = base / slot_run_dir_relative(slot, started.date())
    # Beside the run directory, not inside it: the lock must not be mistaken
    # for evidence that the slot started.
    with cross_process_lock(run_dir.parent / f".{run_dir.name}.finalize.lock"):
        return _finalize_locked(
            slot,
            started,
            base,
            run_dir,
            outcome=outcome,
            order_submit_status=order_submit_status,
            orders=orders,
            signals=signals,
            report=report,
            budget=budget,
            error_class=error_class,
            error_detail=error_detail,
            code_version=code_version,
            now=now,
        )


DRY_RUN_DIR = Path("manual_runs") / "dry_run"


def _finalize_dry_run(
    slot: str,
    started: datetime,
    *,
    outcome: str,
    order_submit_status: str,
    root: Optional[Path],
    orders: Optional[List[Dict[str, Any]]],
    report: Optional[Dict[str, Any]],
    budget: Optional[Dict[str, Any]],
    error_class: Optional[str],
    error_detail: Optional[str],
    code_version: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Record a dry run under manual_runs/dry_run/, outside every scheduled path.

    Skips the acceptance-window check (an off-hours smoke test is the point),
    never touches the scheduled run directory or any latest-run pointer, and
    refuses orders: a dry run places none, so nothing may claim otherwise.
    """
    if orders or order_submit_status == ORDER_SUBMIT_STATUS_SUBMITTED:
        raise FinalizeRefused("a dry-run records no orders; drop --orders-json and use a non-'submitted' status")
    if order_submit_status not in ORDER_SUBMIT_STATUSES:
        raise FinalizeRefused(
            f"--order-submit-status {order_submit_status!r} is not one of {list(ORDER_SUBMIT_STATUSES)}"
        )
    if not outcome.strip():
        raise FinalizeRefused("--outcome must name the Gate 9 outcome")
    # cron_tracking_root() returns a str; `base / ...` below needs a Path (3:30 PM 2026-10-19 TypeError).
    base = Path(root) if root is not None else Path(cron_tracking_root())
    run_dir = base / DRY_RUN_DIR / slot_run_dir_relative(slot, started.date()).name
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "dry_run": True,
        "slot": f"{slot}ET",
        "started_at": started.isoformat(),
        "outcome": outcome,
        "order_submit_status": order_submit_status,
        "orders_submitted_count": 0,
        "finalized_by": "scripts.finalize_run_evidence",
    }
    if report is not None:
        state["gate9_report"] = report
    if budget is not None:
        state["budget"] = budget
    if error_class:
        state["error_class"] = error_class
    if error_detail:
        state["error_detail"] = error_detail
    if code_version is not None:
        state["code_version"] = code_version
    with atomic_open_w(run_dir / "run_state.json") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    return {
        "dry_run": True,
        "slot": f"{slot}ET",
        "trading_day": started.date().isoformat(),
        "run_dir": run_dir.relative_to(base).as_posix(),
        "verified": True,
        "pointer": None,
        "errors": [],
        "warnings": ["dry run: not scheduled evidence, no pointer written"],
    }


def _repair_companions(run_dir: Path, state: Dict[str, Any], stream: str) -> List[str]:
    """Write only the MISSING companion files of an executor-written state; never touch one that exists.

    An executor interrupted between run_state.json and its summary/CSVs leaves
    a state that classifies as scheduled evidence beside an incomplete
    artifact set (Copilot review, PR #1514). A header-only CSV and a summary
    mirrored from the state are exactly what the contract requires for "no
    rows known", so filling the gap is safe; anything already on disk is the
    executor's and is left alone.
    """
    repaired: List[str] = []
    if not (run_dir / SUBMITTED_ORDERS_CSV_FILENAME).is_file():
        write_submitted_orders_csv(run_dir, [])
        repaired.append(SUBMITTED_ORDERS_CSV_FILENAME)
    if not any((run_dir / name).is_file() for name in SIGNAL_CSV_NAMES):
        write_signals_csv(run_dir, [], fieldnames=["ticker"], filename=SIGNALS_FILENAMES[stream])
        repaired.append(SIGNALS_FILENAMES[stream])
    if not (run_dir / "run_summary.json").is_file():
        with atomic_open_w(run_dir / "run_summary.json") as handle:
            json.dump(build_run_summary(state), handle, indent=2, sort_keys=True, default=str)
        repaired.append("run_summary.json")
    return repaired


def _finalize_locked(
    slot: str,
    started: datetime,
    base: Path,
    run_dir: Path,
    *,
    outcome: str,
    order_submit_status: str,
    orders: Optional[List[Dict[str, Any]]],
    signals: Optional[List[Dict[str, Any]]],
    report: Optional[Dict[str, Any]],
    budget: Optional[Dict[str, Any]],
    error_class: Optional[str],
    error_detail: Optional[str],
    code_version: Optional[Dict[str, Any]],
    now: Optional[datetime],
) -> Dict[str, Any]:
    stream, _ = SLOT_EVIDENCE_LAYOUT[slot]
    day = started.date()
    state_path = run_dir / "run_state.json"
    orders_path = run_dir / SUBMITTED_ORDERS_CSV_FILENAME

    existing = None
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_bytes())
            existing = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            existing = None
        if existing is None:
            raise FinalizeRefused(
                f"{state_path.relative_to(base)} exists but is not a JSON object; "
                "refusing to overwrite it -- inspect it by hand"
            )

    if code_version is not None and existing is not None:
        recorded = existing.get("code_version")
        # The release that produced a run cannot change afterwards; refuse before
        # anything below writes a companion file or the pointer.
        if recorded is not None and recorded != code_version:
            raise FinalizeRefused(
                f"{state_path.relative_to(base)} already records a different code_version; "
                "the release that produced a run is immutable"
            )

    warnings: List[str] = []
    # A state an executor's own run() wrote is authoritative and carries far
    # more than this command knows; only a state THIS command wrote may be
    # rebuilt, which is what a scheduler retry of the same firing needs (the
    # retry may place orders the first attempt did not).
    ours = existing is not None and existing.get("finalized_by") == "scripts.finalize_run_evidence"
    if existing is not None and not ours:
        # Before anything below writes a companion file or the pointer, so a
        # refusal leaves the directory exactly as it was.
        check_existing_state_gate5_evidence(run_dir, slot, day, existing)
        state = existing
        wrote = False
        if orders is not None or signals is not None or report is not None or budget is not None:
            warnings.append(
                "run_state.json was written by the executor, not this command; it was kept "
                "unchanged and the supplied orders/signals/report/budget were NOT recorded"
            )
        if code_version is not None and state.get("code_version") is None:
            # The one field this command may add: provenance is immutable and the executors
            # do not know the pin, so leaving it out would record a cloud run with no release.
            _merge_code_version(run_dir, state, code_version)
            warnings.append("code_version was added to the executor's run_state.json and run_summary.json")
        repaired = _repair_companions(run_dir, state, stream)
        if repaired:
            warnings.append(f"wrote missing companion file(s) beside the executor's run_state: {', '.join(repaired)}")
    else:
        prior_rows = _csv_rows(orders_path) if orders_path.is_file() else []
        if orders is None:
            order_rows = prior_rows
        else:
            # Union by order_id so a retry that re-supplies the first attempt's
            # orders does not double them, and one that supplies only its own
            # new orders does not drop the first attempt's.
            supplied_ids = {str(row.get("order_id")) for row in orders if row.get("order_id")}
            order_rows = [
                row for row in prior_rows if not row.get("order_id") or str(row.get("order_id")) not in supplied_ids
            ] + orders
        count = len(order_rows)
        # The status and the order rows must tell the same story, or the drift
        # detector and the evidence disagree about whether money moved.
        if order_submit_status == ORDER_SUBMIT_STATUS_SUBMITTED and count == 0:
            raise FinalizeRefused(
                "order_submit_status 'submitted' needs at least one order row "
                "(--orders-json or an existing submitted_orders.csv)"
            )
        if order_submit_status != ORDER_SUBMIT_STATUS_SUBMITTED and orders:
            raise FinalizeRefused(
                f"{len(orders)} order row(s) supplied with order_submit_status {order_submit_status!r}; use 'submitted'"
            )
        if order_submit_status != ORDER_SUBMIT_STATUS_SUBMITTED and count:
            # An earlier attempt of this firing already placed orders; a retry
            # that placed nothing more must not relabel the slot as order-free.
            order_submit_status = ORDER_SUBMIT_STATUS_SUBMITTED
            warnings.append(
                f"{count} order row(s) already recorded for this slot; order_submit_status kept as 'submitted'"
            )
        if budget is None and ours and isinstance(existing.get("budget"), dict):
            budget = existing["budget"]  # a retry keeps the first attempt's Gate 6 budget
        if code_version is None and ours and isinstance(existing.get("code_version"), dict):
            code_version = existing["code_version"]  # ...and the release it traded from
        if order_submit_status == ORDER_SUBMIT_STATUS_SUBMITTED and (
            budget is None or budget.get("budget_evaluated") is False
        ):
            # Placing an order means Gate 6 ran and selected a tier; writing
            # budget_evaluated=false for it would be false evidence.
            raise FinalizeRefused(
                "orders were placed, so Gate 6 ran: pass --budget-json with budget_tier, "
                "daily_cap_usd and weekly_cap_usd"
            )
        # After the status is final (a retry above may have promoted it to
        # 'submitted', which this check never blocks) and before anything is
        # written, so a refusal leaves the directory as the session left it.
        gate5_evidence = check_zero_order_gate5_evidence(
            run_dir, slot, day, order_submit_status=order_submit_status, error_class=error_class
        )
        state = build_run_state(
            slot,
            started,
            outcome=outcome,
            order_submit_status=order_submit_status,
            orders_submitted_count=count,
            error_class=error_class,
            error_detail=error_detail,
            report=report,
            budget=budget,
            code_version=code_version,
            now=now,
        )
        if gate5_evidence is not None:
            state["gate5_evidence"] = gate5_evidence
        run_dir.mkdir(parents=True, exist_ok=True)
        # CSVs first, run_state last among the evidence files: a crash between
        # them leaves a directory verify_schedule reports as incomplete, never
        # a run_state that claims files which are not there.
        if orders is not None or not orders_path.is_file():
            write_submitted_orders_csv(run_dir, [dict(row) for row in order_rows])
        if signals is not None:
            write_signals_csv(run_dir, signals, filename=SIGNALS_FILENAMES[stream])
        elif not any((run_dir / name).is_file() for name in SIGNAL_CSV_NAMES):
            write_signals_csv(run_dir, [], fieldnames=["ticker"], filename=SIGNALS_FILENAMES[stream])
        with atomic_open_w(run_dir / "run_summary.json") as handle:
            json.dump(build_run_summary(state), handle, indent=2, sort_keys=True, default=str)
        with atomic_open_w(state_path) as handle:
            json.dump(state, handle, indent=2, sort_keys=True, default=str)
        wrote = True

    errors: List[str] = []
    contract = validate_run_artifacts(run_dir, require_summary=True)
    errors.extend(contract.errors)

    pointer: Optional[Path] = None
    if stream == "afternoon":
        pointer_root = base / "ce16ba6b"
        check = validate_classification(state.get("run_classification"))
        if not check.ok:
            errors.extend(f"run_classification: {error}" for error in check.errors)
        elif contract.ok:
            pointer = update_latest_scheduled_run_pointer(state, state_path, base=pointer_root)
    else:
        pointer_root = run_dir.parent
        if contract.ok:
            rc = state.get("run_classification") or {}
            pointer = refresh_latest_pointer(
                pointer_root / POINTER_NAMES[stream],
                run_id=rc.get("run_id") or state.get("run_id"),
                run_state_path=state_path,
                stream=stream,
                trading_day=day.isoformat(),
                run_state=state,
                extra={"slot": f"{slot}ET", "runner_slot_id": slot},
            )

    pointer_status = None
    if pointer is not None:
        validation = validate_pointer(
            pointer, expected_stream=stream, expected_root=pointer_root, today=day.isoformat()
        )
        pointer_status = validation.status
        if not validation.is_valid:
            errors.extend(f"pointer: {reason}" for reason in validation.reasons)
    slot_row = classify_slot_evidence(
        slot,
        day,
        now or datetime.now(ET),
        run_dir_present=True,
        run_state=state,
        run_state_present=True,
        market_closed=None,
        artifact_errors=contract.errors,
    )
    errors.extend(f"slot: {reason}" for reason in slot_row["reasons"])
    verified = slot_row["status"] == STATUS_VERIFIED and pointer_status == "valid" and contract.ok
    return {
        "slot": f"{slot}ET",
        "stream": stream,
        "trading_day": day.isoformat(),
        "run_dir": slot_run_dir_relative(slot, day).as_posix(),
        "run_state_written": wrote,
        "pointer": pointer.relative_to(base).as_posix() if pointer is not None else None,
        "pointer_status": pointer_status,
        "slot_status": slot_row["status"],
        "verified": verified,
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--slot", required=True, help="roster slot: 0935 1030 1130 1230 1330 1430 1530 (ET suffix ok)")
    parser.add_argument(
        "--started-at", required=True, help="ISO-8601 instant this run started (Gate 1's clock); naive values are ET"
    )
    parser.add_argument("--outcome", required=True, help="Gate 9 outcome label, e.g. ORDERS_PLACED, END_SILENTLY")
    parser.add_argument("--order-submit-status", required=True, choices=ORDER_SUBMIT_STATUSES)
    parser.add_argument("--orders-json", help="JSON array of {ticker, order_id, submitted_amount_usd, status}")
    parser.add_argument("--signals-json", help="JSON array of signal rows for the stream's signals CSV")
    parser.add_argument("--report-json", help="JSON object embedded in run_state as gate9_report")
    parser.add_argument(
        "--budget-json",
        help='Gate 6 budget object {"budget_tier","daily_cap_usd","weekly_cap_usd",...}; '
        "required when orders were placed; omit only when Gate 6 never ran",
    )
    parser.add_argument(
        "--code-version-json",
        help='{"git_sha": <40 hex>, "tag": ..., "pinned_by": ...} inline, or a path to a file holding it: '
        "the pinned release this run traded from",
    )
    parser.add_argument("--error-class", help="concrete error class for a blocked/failed run")
    parser.add_argument("--error-detail", help="one-line detail for a blocked/failed run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="record an off-schedule smoke test under manual_runs/dry_run/: no window check, no pointer, no orders",
    )
    parser.add_argument(
        "--root", help="cron-tracking root (default: $DCA_CRON_TRACKING_ROOT or <checkout>/cron_tracking)"
    )
    args = parser.parse_args(argv)
    try:
        code_version = _load_inline_or_file_json(args.code_version_json, "--code-version-json")
        check_code_version_pin(code_version, os.environ)
        result = finalize(
            args.slot,
            parse_started_at(args.started_at),
            outcome=args.outcome,
            order_submit_status=args.order_submit_status,
            root=Path(args.root) if args.root else None,
            orders=_load_json(args.orders_json, list, "--orders-json"),
            signals=_load_json(args.signals_json, list, "--signals-json"),
            report=_load_json(args.report_json, dict, "--report-json"),
            budget=_load_json(args.budget_json, dict, "--budget-json"),
            code_version=code_version,
            error_class=args.error_class,
            error_detail=args.error_detail,
            dry_run=args.dry_run,
        )
    except FinalizeRefused as exc:
        print(json.dumps({"verified": False, "refused": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
