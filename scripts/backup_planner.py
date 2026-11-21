#!/usr/bin/env python3
"""The planner the Perplexity backup prompts call: read a plan, compute one, or finalize one.

Three subcommands, each printing ONE JSON document and exiting 0 when the result is usable and
2 on anything else, in which case the prompt places nothing (the contract is the table in
``claude/dca-executor-prompts/perplexity/README.md``)::

    plan      --source mac_plan --trading-day D --slot S [--plan-file F]
    plan      --source mac_rec|published_rec --trading-day D --slot S --now T [--published F]
    compute   --trading-day D --slot S --quotes F --orders-pages F --buying-power USD [--now T]
    finalize  --trading-day D --slot S --plan F --orders-pages F --fresh-quotes F
              --fresh-spreads F --buying-power USD [--fresh-prior-closes F] [--now T]
    classify  --trading-day D --slot S --orders-pages F [--executor-ids F] [--published F]
    executor-ids --trading-day D --slot S [--rec-root R] [--cron-root C]   (Mac only)
    check-placement --trading-day D --slot S --response F --symbol T --amount USD --sent-ref-id R
              --submitted-after T --prior-orders-pages F [--placed-responses F ...]

``mac_rec`` / ``published_rec`` read the plan-first recommendation (PR #1667, ``docs/plan_first_recommendations.md``):
the primary's own {ticker, usd, ref_id} set for the slot, from the Mac's store or from the copy ``dca_rec_publish``
mirrors to Notion and the ``recommendations`` branch, under the local reader's freshness rule. ``classify`` recognises
the plan-first reader's orders, which carry no ``dca-runner-`` reference at the broker.

WHAT THIS IS
------------
A thin I/O wrapper over :mod:`dca_order_plan`. All sizing, cap, reconciliation, and validation
logic lives there and is tested there. This script adds only: file loading, proof that the saved
broker pages are complete and pinned to the trading day, the raw-row to ``BrokerOrder`` mapping,
and the JSON report.

WHAT THIS IS NOT
----------------
It never places, cancels, or modifies an order, imports no broker adapter, and never prints the
account number. Saved order pages are the only evidence of what was bought; a plan is input only.
The gated ``compute --market-data`` and ``finalize`` pick the budget tier the way the primary does, from the count
of 3x-or-greater signals alone (``near_close_dca.select_budget_tier``); the shadow ``compute --quotes`` path still
applies the Normal tier and never escalates it.

PLACEMENT GATES
---------------
* ``finalize`` on a plan written by ``compute`` (generator ``backup``) returns ``SHADOW_ONLY``: the
  would-be orders are reported and nothing is placed, because ``compute`` does not run the stream
  generator's trend and data-confidence gates (Gate 5). One switch, ``COMPUTE_PLACEMENT_ENABLED``,
  below. A plan written by the Claude slot (generator ``claude``) is unaffected.
* Buying power and per-ticker bid/ask spread are required inputs and fail closed when missing.
* A Claude plan is reconciled against THIS slot's window only (a later slot may top a ticker up,
  per ``config/budgets.json``); a computed plan excludes any ticker bought earlier that day.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 11)  # mirrors ``requires-python`` in pyproject.toml and the .pyz bootstrap's floor


def require_supported_python(version_info) -> None:
    """Exit 2 with the planner's ABORT document when the interpreter is older than ``MIN_PYTHON``.

    ``backup_planner.pyz`` already refuses an old interpreter in its bootstrap; this covers the
    ``python3 -m scripts.backup_planner`` form the Mac prompts also run. 2026-10-16 09:49 ET: the Mac shell fallback ran
    Python 3.9, so the planner would have died on an import or syntax error with no usable reason. The document and the
    exit code are the ones the .pyz bootstrap emits (the planner's contract: anything but exit 0 places nothing).
    Called before any project import.
    """
    if tuple(version_info[:2]) >= MIN_PYTHON:
        return
    detail = "python>=%d.%d required, found %d.%d" % (MIN_PYTHON + tuple(version_info[:2]))
    print(json.dumps({"status": "ABORT", "plan": None, "errors": [{"code": "python_too_old", "detail": detail}]},
                     separators=(",", ":")))
    sys.exit(2)


require_supported_python(sys.version_info)

import dca_config  # noqa: E402  (every project import must follow the version guard)
import dca_order_plan as dop  # noqa: E402
from dca_number_check import plan_grounding_errors  # noqa: E402
from dca_order_plan import PlanError, PlanResult  # noqa: E402
from dca_paths import cron_tracking_root  # noqa: E402
from dca_rec_models import RecommendationSet  # noqa: E402
from dca_rec_store import RecommendationStore, load_settings  # noqa: E402
from dca_runner import ET, MAX_PER_ORDER_USD, SLOTS, BrokerOrder, BudgetDataError  # noqa: E402
from near_close_dca import (  # noqa: E402
    BUDGET_TIERS,
    BudgetTier,
    kill_switch_engaged,
    select_first_allocation_base_usd,
)
from scripts import backup_stream_specs, stream_candidates  # noqa: E402
from scripts import generate_buy_candidates as gbc  # noqa: E402
from scripts.assemble_order_pages import AssemblyRefused, _order_rows, _validate_rows  # noqa: E402
from scripts.broker_reconciliation import _has_continuation, _unwrap  # noqa: E402
from scripts.reason_codes import UNSUPPORTED_NUMBER  # noqa: E402
from scripts.same_day_top_up_gate import _bind_saved_pages  # noqa: E402
from scripts.validate_native_placement import native_ref_id, validate_native_placement  # noqa: E402
from scripts.verify_schedule import slot_run_dir_relative  # noqa: E402

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NORMAL_TIER = next(tier for tier in BUDGET_TIERS if tier.name == "normal")


def _tier_for(multipliers: Any, config: Any) -> tuple[BudgetTier, float | None]:
    """The budget tier and effective weekly cap for a backup batch: always Normal ($180 daily, $900 weekly).

    Operator decision 2026-10-22: a backup stays Normal-only like the Claude slots, which never escalate (only the
    3:30 final run does, ``docs/safeguards.md``). The count of 3x-or-greater signals therefore no longer raises a
    cap; a batch over the Normal caps is trimmed to the prefix that fits (``dca_order_plan`` ``trim_to_fit``)
    instead. This reverses the 2026-10-15 escalation (backup sessions placed $205.90 against the $180 cap on
    2026-10-16). ``multipliers`` and ``config`` are kept so the call sites and their signature stay unchanged.
    """
    return _NORMAL_TIER, None


_DEAD_STATES = frozenset({"cancelled", "canceled", "rejected", "failed", "voided"})

# Operator decision 2026-10-13: a backup that cannot reach the Mac places its own computed plan (ADR "Open design
# gaps", gap 2, which kept this off until a shadow run). The gate itself (`compute --market-data`) is unchanged; the
# switch alone no longer places: `finalize --local-status` must also say the Mac is out of the picture.
COMPUTE_PLACEMENT_ENABLED = True

# local_status values (step 3 of the prompts) under which the backup may act: no heartbeat (off), a failed or stale
# run (errored), or a Mac that could not be reached (unreachable). Anything else, or no value, keeps a computed
# plan shadow-only so an unknown or active Mac is never raced.
BACKUP_MAY_ACT_LOCAL_STATUSES = ("off", "errored", "unreachable")
LOCAL_STATUSES = BACKUP_MAY_ACT_LOCAL_STATUSES + ("unknown", "in_progress", "succeeded")

# Slots whose Gate 5 the safety gate can run: runner slot id -> the candidate generator's slot label. All seven.
# 15:30 runs scripts.generate_buy_candidates (the final-buy engine); the six executor-class slots run the stream
# CLI path in scripts/stream_candidates.py, scored identically from Robinhood (scripts/backup_stream_specs.py).
GATED_SLOTS = {"0935": "0935ET", "1030": "1030ET", "1130": "1130ET", "1230": "1230ET", "1330": "1330ET",
               "1430": "1430ET", "1530": "1530ET"}

# Plan-first (PR #1667): the program on the Mac computes {ticker, usd, ref_id} per slot with the PRIMARY's own Gate 5,
# 6 and 6.5 chain (dca_recommender) and the Claude reader places exactly that. A backup that reads the same set places
# the same orders, so the set is treated like a Claude plan (sized by the primary: this slot's window only, no
# kill-switch re-derivation, places on PROCEED without --local-status). The plan document ``plan --source
# mac_rec|published`` emits carries this generator so ``finalize`` can tell it from a dca_order_plan document, which
# needs price fields a price-blind recommendation never has.
PLAN_FIRST_GENERATOR = "plan_first"
_PRIMARY_SIZED_GENERATORS = frozenset({"claude", PLAN_FIRST_GENERATOR})
_PLAN_FIRST_KEYS = frozenset({"schema_version", "trading_day", "slot", "generated_at_et", "generator", "ladder_version",
                              "universe_hash", "source", "orders", "sizing_guard"})
_PLAN_FIRST_ORDER_KEYS = frozenset({"symbol", "notional_usd", "ref_id"})
_PLAN_FIRST_SOURCES = ("mac_rec", "published_rec")
# The published payload dca_rec_publish.build_document writes (Notion code block, GitHub `recommendations` branch).
_PUBLISHED_KEYS = frozenset({"schema_version", "generated_at", "slots"})
# placed_agent values that are the account owner's own activity: noted, never blocking (step 2 of the prompts).
_USER_AGENTS = frozenset({"user", "recurring", "drip"})


class _Refused(Exception):
    """Carries structured errors out of a loader; never shown raw."""

    def __init__(self, *errors: PlanError) -> None:
        super().__init__(errors[0].code if errors else "refused")
        self.errors = errors


def _error_dicts(errors: Any) -> list[dict[str, str]]:
    return [{"code": e.code, "detail": e.detail} for e in errors]


def _skipped_dicts(skipped: Any) -> list[dict[str, str]]:
    return [{"symbol": symbol, "reason": reason} for symbol, reason in skipped]


def _emit(report: dict[str, Any], code: int) -> int:
    # One compact line (2026-10-31, credit plan step 4): the Perplexity agent re-reads every tool output on each later
    # turn, and indent=2 roughly doubled the 91-symbol universe list.
    print(json.dumps(report, separators=(",", ":")))
    return code


# ---------------------------------------------------------------------------
# Input loading (every failure is a structured refusal, never a raise)
# ---------------------------------------------------------------------------


def _read_json(path: Path, *, missing: str, corrupt: str) -> Any:
    if not path.is_file():
        raise _Refused(PlanError(missing, path.name))
    try:
        # Duplicate keys are refused the same way config loading refuses them.
        return json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise _Refused(PlanError(corrupt, f"{path.name}: {type(exc).__name__}")) from exc


def _parse_money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return float(number) if number.is_finite() else None


def _row_amount(row: dict[str, Any], key: str) -> float | None:
    nested = row.get(key)
    return _parse_money(nested.get("amount")) if isinstance(nested, dict) else None


def _to_broker_order(row: Any) -> BrokerOrder:
    """One raw ``get_equity_orders`` row as a ``BrokerOrder``; ValueError on anything unusable."""
    if not isinstance(row, dict):
        raise ValueError("row is not an object")
    symbol, side, state = row.get("symbol"), row.get("side"), row.get("state")
    if not (isinstance(symbol, str) and symbol.strip() and isinstance(side, str) and isinstance(state, str)):
        raise ValueError("row lacks symbol, side, or state")
    created = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("created_at is not timezone-aware")
    amount = _row_amount(row, "dollar_based_amount")
    if amount is None and side.lower() == "buy":
        # A quantity-based buy still commits money; value it or refuse, never read it as zero.
        quantity, price = _parse_money(row.get("quantity")), _parse_money(row.get("price") or row.get("average_price"))
        if not (quantity and price and quantity > 0 and price > 0):
            raise ValueError("buy has no dollar amount and no quantity and price")
        amount = quantity * price
    executed = None
    if state.lower() in _DEAD_STATES:
        executed = _row_amount(row, "executed_notional")
        cumulative, average = _parse_money(row.get("cumulative_quantity")), _parse_money(row.get("average_price"))
        if executed is None and cumulative and average and cumulative > 0 and average > 0:
            executed = cumulative * average
    reference = row.get("ref_id") if isinstance(row.get("ref_id"), str) and row.get("ref_id") else None
    return BrokerOrder(
        symbol=symbol.strip().upper(),
        side=side.lower(),
        dollar_amount=0.0 if amount is None else amount,
        created_at=created,
        placed_agent=row.get("placed_agent") if isinstance(row.get("placed_agent"), str) else None,
        state=state.lower(),
        executed_dollar_amount=executed,
        client_order_id=reference,
        ref_id=reference,
    )


def _load_history(path: Path, trading_day: date, week_last4: str | None = None) -> list[BrokerOrder]:
    """Saved order pages as broker history, only once they are proven pinned and complete."""
    try:
        return [_to_broker_order(row) for row in _load_rows(path, trading_day, week_last4)]
    except (ValueError, TypeError) as exc:
        raise _Refused(PlanError("history_unusable", type(exc).__name__)) from exc


# ---------------------------------------------------------------------------
# The Mac-side week envelope (Perplexity backup "hybrid", 2026-11-14)
# ---------------------------------------------------------------------------
# 2026-11-13: the 9:35 Claude slot did not fire and the 10:30 slot's full-week fetch was too large to save
# (output_cap_exceeded, 0 orders). The backups fetch SEVEN days through the Perplexity sandbox, the same oversized
# shape. When the Mac is reachable, the backup now looks up the newest complete week envelope on the Mac
# (scripts.backup_week_history -> scripts.find_order_baseline --kind week), fetches only the increment since it,
# merges with scripts.merge_order_pages, and runs every history-reading command here, ON THE MAC, against that
# merged file. These are the only rules under which the planner reads an envelope NOT dated today. The today-binding
# of every other envelope (_bind_saved_pages) is untouched, so a hand-built or Monday-dated file passed without
# --week-envelope is still refused with orders_pages_trading_day_mismatch.
#
# What a week envelope proves: every order from this ISO week's Monday 00:00 ET to its snapshot. What it does NOT
# prove is the trailing seven days the kill switch sums (operator decision 2026-11-14, option 1): last week's newest
# envelope ends before that week's final 3:30 orders and before any weekend order, and get_equity_orders has no
# created_at_lte to fetch only that seam, so the days before Monday are never sourced from it. Instead the planner
# treats the kill switch as ENGAGED whenever it reads a week envelope: compute sizes at 1x at most, and finalize
# refuses an over-1x plan with kill_switch_unproven (the prompt then falls back to the full seven-day fetch). That can
# only shrink a buy; caps, Gate 4 and the freshness bound are unchanged and need only Monday onward.
WEEK_ENVELOPE_WRITER = "scripts.merge_order_pages"
# The writers whose output proves a complete page chain (scripts.find_order_baseline.TRUSTED_WRITERS, restated so the
# pyz closure does not grow by a Mac-only module). A merge onto a base from any other writer is refused.
_TRUSTED_BASE_WRITERS = ("scripts.assemble_order_pages", "scripts.merge_order_pages")
ACCOUNT_ENV = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"
_LAST4_RE = re.compile(r"^\d{4}$")


def _canonical(obj: Any) -> bytes:
    # Byte-identical to assemble_order_pages / merge_order_pages, so the recomputed hash is the one they recorded.
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _bind_week_envelope(payload: Any, trading_day: date, last4: str) -> list[Any]:
    """The pages of a merged ISO-week envelope, once every binding below holds; else a structured refusal.

    * account: the envelope's ``account_number`` is EXACTLY the Mac's configured account (``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER``,
      which dca_paths loads from the checkout's .env) and ends in ``--account-last4``, the last four of the account the
      backup itself resolved. Unlike the today-binding (pinned to the file's own account), this pins to an account the
      file did not supply, and no account number ever appears on a command line.
    * day: ``trading_day`` is this ISO week's Monday and ``snapshot_at`` is offset-aware and falls on ``trading_day`` in ET
      (the merge ran in this run; an older merged file is a base, never history).
    * writer: ``ingest.tool`` is scripts.merge_order_pages, its recorded ``base_ingest_tool`` is a trusted writer, and the
      pages still hash to ``ingest.payload_sha256`` (a file edited after the merge is refused).
    * rows: every row passes assemble_order_pages' row checks (no placeholder id, no condensed row).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        raise _Refused(PlanError("orders_pages_missing_query_metadata"))
    account = os.environ.get(ACCOUNT_ENV, "").strip()
    if not account:
        raise _Refused(PlanError("week_envelope_account_unset", f"{ACCOUNT_ENV} is not set on this machine"))
    if str(payload.get("account_number") or "").strip() != account:
        raise _Refused(PlanError("orders_pages_account_mismatch"))
    if not account.endswith(last4):
        raise _Refused(PlanError("week_envelope_last4_mismatch", "the configured account is not the resolved one"))
    monday = trading_day - timedelta(days=trading_day.weekday())
    if payload.get("trading_day") != monday.isoformat():
        raise _Refused(PlanError("week_envelope_not_this_week", f"expected trading_day {monday.isoformat()}"))
    ingest = payload.get("ingest")
    if not isinstance(ingest, dict) or ingest.get("tool") != WEEK_ENVELOPE_WRITER:
        raise _Refused(PlanError("week_envelope_not_merged", "ingest.tool is not scripts.merge_order_pages"))
    if ingest.get("base_ingest_tool") not in _TRUSTED_BASE_WRITERS:
        raise _Refused(PlanError("week_envelope_untrusted_base", "the merge's base was not script-built"))
    if hashlib.sha256(_canonical(payload["pages"])).hexdigest() != ingest.get("payload_sha256"):
        raise _Refused(PlanError("week_envelope_payload_mismatch", "pages were changed after the merge wrote them"))
    try:
        _validate_rows(payload["pages"])
    except AssemblyRefused as exc:
        raise _Refused(PlanError("week_envelope_bad_rows", exc.reason)) from exc
    snapshot = payload.get("snapshot_at")
    try:
        moment = datetime.fromisoformat(str(snapshot).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise _Refused(PlanError("week_envelope_bad_snapshot", "snapshot_at is not ISO-8601")) from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise _Refused(PlanError("week_envelope_bad_snapshot", "snapshot_at has no UTC offset"))
    if moment.astimezone(ET).date() != trading_day:
        raise _Refused(PlanError("week_envelope_snapshot_not_today", str(snapshot)))
    return payload["pages"]


def _engaged_floor(config: Any) -> float | None:
    """The smallest trailing-7-day figure that engages the kill switch, or None when the guard is disabled."""
    allocation = config.monthly_cash_allocation_usd
    fraction = config.kill_switch_7d_deploy_fraction
    if allocation <= 0 or not 0 <= fraction <= 1:
        return None
    return math.nextafter(fraction * allocation, math.inf)


def _load_rows(path: Path, trading_day: date, week_last4: str | None = None) -> list[Any]:
    """The raw order rows of saved pages, de-duplicated by order id, once proven pinned and complete.

    Split out of :func:`_load_history` for ``classify``, which needs the raw ``id``, ``placed_agent`` and
    ``ref_id`` a ``BrokerOrder`` does not keep. Same proof, same de-duplication, so both read the same orders.
    ``week_last4`` (``--week-envelope --account-last4``) switches to the Mac-side week-envelope binding,
    :func:`_bind_week_envelope`; without it the envelope must be dated today, exactly as before.
    """
    payload = _read_json(path, missing="orders_pages_missing", corrupt="orders_pages_corrupt")
    if week_last4 is not None:
        pages = _bind_week_envelope(payload, trading_day, week_last4)
    else:
        saved_account = str(payload.get("account_number") or "") if isinstance(payload, dict) else ""
        # The envelope is pinned to ITS OWN account and the requested day: it proves the pages
        # carry both, not that the account is the right one (the prompt's own poll wrote the file).
        pages, problem = _bind_saved_pages(payload, account_number=saved_account, trading_day=trading_day.isoformat())
        if problem is not None:
            raise _Refused(PlanError(problem))
    chain = [_unwrap(page) for page in pages]
    if not chain or not all(isinstance(page, dict) and page for page in chain):
        raise _Refused(PlanError("orders_pages_incomplete", "no pages, or an empty page object"))
    if not all(_has_continuation(page) for page in chain[:-1]) or _has_continuation(chain[-1]):
        raise _Refused(PlanError("orders_pages_incomplete", "the saved chain does not end where the broker's does"))
    seen: set[str] = set()
    rows: list[Any] = []
    try:
        for page in pages:
            for row in _order_rows(page):
                order_id = str(row.get("id")) if isinstance(row, dict) else ""
                if order_id and order_id in seen:
                    continue  # an order repeated across pages must not count twice
                seen.add(order_id)
                rows.append(row)
    except (ValueError, TypeError) as exc:
        raise _Refused(PlanError("history_unusable", type(exc).__name__)) from exc
    return rows


_DECIMAL_TEXT = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


def _as_number(value: Any) -> float | None:
    """A finite float from a JSON number or a plain decimal string, else ``None``.

    2026-10-20 and 2026-10-21 (13:50, 13:30 backup passes): the connector hands quotes over as strings
    ("101.25"), the old strict number check aborted a sound run, and the operator hand-rewrote the files to get
    past it. Plain decimal text is now accepted; bools, NaN/inf, exponents, hex and padded text still fail closed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not _DECIMAL_TEXT.fullmatch(value):
            return None
        value = float(value)
    if not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _load_fresh_quotes(path: Path, *, missing: str = "fresh_quotes_missing",
                       corrupt: str = "fresh_quotes_corrupt") -> dict[str, float]:
    """``{"SYMBOL": price}``, every price positive and finite. Also reads ``--fresh-prior-closes`` (same shape)."""
    data = _read_json(path, missing=missing, corrupt=corrupt)
    ok = isinstance(data, dict) and all(isinstance(k, str) for k in data)
    prices = {k.upper(): _as_number(v) for k, v in data.items()} if ok else {}
    if not ok or not all(v is not None and v > 0 for v in prices.values()):
        raise _Refused(PlanError(corrupt, path.name))
    return {k: float(v) for k, v in prices.items()}  # type: ignore[arg-type]


def _load_fresh_spreads(path: Path) -> dict[str, tuple[Any, Any] | None]:
    """``{"SYMBOL": {"bid": x, "ask": y}}`` as ``(bid, ask)``; a null entry means no usable quote for it."""
    data = _read_json(path, missing="fresh_spreads_missing", corrupt="fresh_spreads_corrupt")

    ok = isinstance(data, dict) and all(
        isinstance(k, str)
        and (v is None or (isinstance(v, dict) and _as_number(v.get("bid")) is not None
                           and _as_number(v.get("ask")) is not None))
        for k, v in data.items()
    )
    if not ok:
        raise _Refused(PlanError("fresh_spreads_corrupt", path.name))
    return {k.upper(): None if v is None else (_as_number(v["bid"]), _as_number(v["ask"])) for k, v in data.items()}


def _parse_buying_power(text: str) -> float:
    """The available buying power as a positive finite number, else a structured refusal."""
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise _Refused(PlanError("invalid_buying_power", "not a number")) from exc
    if not math.isfinite(value) or value <= 0:
        raise _Refused(PlanError("invalid_buying_power", "must be a positive finite amount"))
    return value


def _parse_deployed(text: Any, history: list[BrokerOrder], now: datetime) -> float:
    """The trailing-7-day deployment figure as a finite non-negative number, else a structured refusal.

    ``auto`` derives it from the same broker history the caps use (:func:`dca_order_plan.trailing_deployed_usd`),
    so the task sums nothing. A number is taken as given. Required either way: silently assuming zero would
    leave the kill switch off on exactly the day it exists to catch (the primary's generator makes the same choice).
    """
    if text == "auto":
        try:
            return dop.trailing_deployed_usd(history, now=now)
        except BudgetDataError as exc:
            raise _Refused(PlanError("history_unusable", type(exc).__name__)) from exc
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise _Refused(PlanError("invalid_deployed_figure", "not a number")) from exc
    if not math.isfinite(value) or value < 0:
        raise _Refused(PlanError("invalid_deployed_figure", "must be a finite, non-negative amount"))
    return value


def _load_market_data(path: Path) -> dict[str, Any]:
    """The assembled market-data payload, shape-checked the way the primary's own CLI checks it."""
    payload = _read_json(path, missing="market_data_missing", corrupt="market_data_corrupt")
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
        raise _Refused(PlanError("market_data_invalid", "expected an object with a 'sources' object"))
    if "ticker_errors" in payload and payload["ticker_errors"] is None:
        raise _Refused(PlanError("market_data_invalid", "'ticker_errors' must be an object, not null"))
    return payload


def _prior_bar(sources: dict[str, Any], ticker: str, price_source: str) -> dict[str, Any] | None:
    """The previous-close bar from the source that produced the decision, else any source that has one."""
    names = [n for n in sources if price_source.startswith(n)] + list(sources)
    for name in names:
        entry = sources.get(name)
        bar = entry.get(ticker, {}).get("previous_close_bar") if isinstance(entry, dict) and isinstance(entry.get(ticker), dict) else None
        if isinstance(bar, dict):
            return bar
    return None


def _context(config_dir: Path | None) -> tuple[Any, str, Path]:
    directory = config_dir if config_dir is not None else dca_config.get_config_dir()
    return dca_config.load_strategy_config(directory), dop.current_ladder_version(directory), directory


def _plan_context(config: Any, ladder_version: str, trading_day: date, slot: str) -> dict[str, Any]:
    return {
        "trading_day": trading_day,
        "slot": slot,
        "ladder_version": ladder_version,
        "active_tickers": tuple(config.tickers),
        "excluded_tickers": frozenset(config.excluded_tickers),
        "max_per_order_usd": MAX_PER_ORDER_USD,
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _grounding_report(args: argparse.Namespace, plan: PlanResult, config: Any) -> tuple[dict[str, Any] | None, list[PlanError]]:
    """Unsupported-number findings for a validated plan, as ``(report, blocking_errors)``.

    REPORT-ONLY by default (``dca_number_check``): findings go into the emitted JSON under
    ``number_grounding`` with reason code ``UNSUPPORTED_NUMBER`` and ``blocking_errors`` is empty, so
    placement is unchanged. ``--enforce-number-grounding`` (default off) turns the same findings
    into blocking errors. Needs ``--market-data``; without it nothing is checked and ``None`` is returned,
    so existing invocations emit byte-identical output. A plan-first set is price-blind (no
    decision_price to ground) and is skipped. The grounding check never raises into the caller.
    """
    market_path = getattr(args, "market_data", None)
    if market_path is None or plan.plan is None or plan.plan.get("generator") == PLAN_FIRST_GENERATOR:
        return None, []
    try:
        payload = _load_market_data(market_path)

        def base_for(order: Any) -> float | None:
            # The same call dop._size_order sizes with, so a computed or primary plan reproduces exactly.
            return select_first_allocation_base_usd(
                order["decision_price"], order["prior_close"], order.get("prior_low"), config, order["symbol"])[0]

        findings = plan_grounding_errors(plan.plan, payload, base_usd_for=base_for)
    except _Refused as refusal:
        findings = list(refusal.errors)
    except Exception as exc:  # noqa: BLE001 - report-only: a checker defect must not break placement
        findings = [PlanError("number_check_error", type(exc).__name__)]
    report = {"reason_code": UNSUPPORTED_NUMBER, "enforced": bool(args.enforce_number_grounding),
              "findings": _error_dicts(findings)}
    return report, (findings if args.enforce_number_grounding else [])


def _cmd_plan(args: argparse.Namespace, trading_day: date) -> int:
    if args.source in _PLAN_FIRST_SOURCES:
        return _cmd_plan_first(args, trading_day)
    config, ladder_version, _ = _context(args.config_dir)
    path = args.plan_file if args.plan_file is not None else dop.order_plan_path(trading_day, args.slot)
    result = dop.load_plan(path, **_plan_context(config, ladder_version, trading_day, args.slot))
    if result.errors:
        return _emit({"status": "NO_VALID_PLAN", "plan": None, "errors": _error_dicts(result.errors)}, 2)
    report, blocking = _grounding_report(args, result, config)
    if blocking:
        return _emit({"status": "NO_VALID_PLAN", "plan": None, "errors": _error_dicts(blocking),
                      "number_grounding": report}, 2)
    out: dict[str, Any] = {"status": "VALID_PLAN", "plan": result.plan, "errors": []}
    if report is not None:
        out["number_grounding"] = report
    return _emit(out, 0)


# ---------------------------------------------------------------------------
# Plan-first recommendations (PR #1667): read the primary's own set, recognise its orders
# ---------------------------------------------------------------------------


def _published_set(document: Any, slot: str) -> RecommendationSet | None:
    """``slot``'s set from a published payload, or ``None`` when the payload carries none for it.

    The payload is ``dca_rec_publish.build_document``'s: ``{schema_version, generated_at, slots}``. Anything else,
    or a set that fails ``RecommendationSet``'s own strict contract, is a refusal: a malformed file is never read
    around.
    """
    version = document.get("schema_version") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or set(document) != _PUBLISHED_KEYS or isinstance(version, bool) or version != 1
            or not isinstance(document["slots"], dict)):
        raise _Refused(PlanError("published_invalid", "not a published recommendations payload"))
    entry = document["slots"].get(slot)
    if entry is None:
        return None
    try:
        recommendation = RecommendationSet.from_json(entry)
    except (ValueError, TypeError, KeyError) as exc:
        raise _Refused(PlanError("published_invalid", type(exc).__name__)) from exc
    if recommendation.slot != slot:
        raise _Refused(PlanError("published_invalid", "the slot key and its set disagree"))
    return recommendation


def _freshness_reason(recommendation: RecommendationSet, slot: str, now: datetime, max_age: timedelta) -> str | None:
    """``None`` when the set may be used now, else the reason, by the local reader's rule.

    Same rule and reason codes as ``RecommendationStore.select_for`` (the operator's 2026-10-13 rule): the set for this
    slot, stamped on today's ET date, at or before ``now``, and no older than ``max_age`` from
    ``config/recommendations.json``. A published copy holds one set per slot, so there is no older one to fall back to.
    """
    current, as_of = now.astimezone(ET), recommendation.as_of.astimezone(ET)
    if recommendation.slot != slot or as_of.date() != current.date() or recommendation.trading_day != current.date():
        return "file_identity_mismatch"
    if as_of > current:
        return "only_future_stamped_files"
    if (current - as_of).total_seconds() > max_age.total_seconds():
        return "stale"
    return None


def _plan_first_document(recommendation: RecommendationSet, source: str) -> dict[str, Any]:
    """The set as the plan document ``finalize`` reads. A copy of the set's own fields; nothing is sized here."""
    return {
        "schema_version": dop.SCHEMA_VERSION,
        "trading_day": recommendation.trading_day.isoformat(),
        "slot": recommendation.slot,
        "generated_at_et": recommendation.as_of.astimezone(ET).isoformat(),
        "generator": PLAN_FIRST_GENERATOR,
        "ladder_version": recommendation.ladder_version,
        "universe_hash": recommendation.universe_hash,
        "source": source,
        "orders": [{"symbol": item.ticker, "notional_usd": item.usd, "ref_id": item.ref_id}
                   for item in recommendation.items],
        "sizing_guard": recommendation.sizing_guard,
    }


def current_config_reason(recommendation: RecommendationSet, *, trading_day: date, slot: str,
                          config_dir: Path | None = None, source: str = "mac_rec") -> str | None:
    """The first code ``_validate_plan_first`` rejects ``recommendation`` with, or None when it is valid NOW.

    The local readers (``read_recommendation`` and ``plan_first_slot``) share this with the Perplexity planner so no
    reader emits executable orders from a cached set that the current ladder version, universe hash, exclusions or
    per-order ceiling would refuse (PR #1815 review).
    """
    config, ladder_version, _directory = _context(config_dir)
    result = _validate_plan_first(_plan_first_document(recommendation, source), config=config,
                                  ladder_version=ladder_version, trading_day=trading_day, slot=slot)
    return result.errors[0].code if result.errors else None


def _validate_plan_first(plan: Any, *, config: Any, ladder_version: str, trading_day: date, slot: str) -> PlanResult:
    """``dca_order_plan.validate_plan``'s checks for a plan-first document, against the READER's own config.

    Day, slot, sizing version, universe hash, symbols in the active universe and not excluded, positive whole-cent
    amounts within the per-order cap, no repeated symbol, and each ref_id exactly the native UUID the Claude reader
    sends for this slot, day and ticker (``validate_native_placement.native_ref_id``). Any defect rejects the whole
    plan. Run by ``plan`` when the set is read and again by ``finalize`` on the saved file.
    """
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIRST_KEYS:
        return dop._fail(PlanError("schema_violation", "plan must be an object with exactly the schema's fields"))
    errors: list[PlanError] = []
    version = plan["schema_version"]
    if isinstance(version, bool) or version != dop.SCHEMA_VERSION:
        errors.append(PlanError("schema_version_mismatch", f"expected {dop.SCHEMA_VERSION}"))
    if plan["trading_day"] != trading_day.isoformat():
        errors.append(PlanError("wrong_trading_day", f"expected {trading_day.isoformat()}"))
    if plan["slot"] != slot:
        errors.append(PlanError("wrong_slot", f"expected {slot}"))
    if plan["generator"] != PLAN_FIRST_GENERATOR or plan["source"] not in _PLAN_FIRST_SOURCES:
        errors.append(PlanError("schema_violation", "generator or source"))
    if not dop._aware_timestamp(plan["generated_at_et"]):
        errors.append(PlanError("schema_violation", "generated_at_et"))
    if plan["ladder_version"] != ladder_version:
        errors.append(PlanError("stale_ladder_version", f"expected {ladder_version}"))
    if plan["universe_hash"] != dop.universe_hash(config.tickers):
        errors.append(PlanError("universe_hash_mismatch"))
    guard = plan["sizing_guard"]
    # None is allowed here (an older, unstamped set still parses) and fails closed later, at finalize's recheck.
    if guard is not None and not (
            isinstance(guard, dict) and set(guard) == {"max_multiplier", "deployed_7d_usd"}
            and dop._is_positive_finite(guard["max_multiplier"]) and guard["max_multiplier"] >= 1.0
            and isinstance(guard["deployed_7d_usd"], (int, float)) and not isinstance(guard["deployed_7d_usd"], bool)
            and math.isfinite(guard["deployed_7d_usd"]) and guard["deployed_7d_usd"] >= 0):
        errors.append(PlanError("schema_violation", "sizing_guard"))
    orders = plan["orders"]
    if not isinstance(orders, list):
        return dop._fail(*errors, PlanError("schema_violation", "orders must be a list"))
    # A dated no_buy pause (wash-sale window, docs/adr-no-buy-window.md) refuses the symbol like an excluded one.
    active, excluded, seen = set(config.tickers), set(config.excluded_tickers) | set(config.no_buy_tickers), set()
    for order in orders:
        if not isinstance(order, dict) or set(order) != _PLAN_FIRST_ORDER_KEYS:
            errors.append(PlanError("schema_violation", "order must be an object with exactly the schema's fields"))
            continue
        symbol, notional = order["symbol"], order["notional_usd"]
        if not isinstance(symbol, str) or not dop._SYMBOL_RE.match(symbol):
            errors.append(PlanError("schema_violation", "order symbol is not a ticker"))
            continue
        if symbol in excluded:
            errors.append(PlanError("excluded_symbol", symbol))
        elif symbol not in active:
            errors.append(PlanError("unknown_symbol", symbol))
        if not dop._is_positive_finite(notional) or not dop._is_cent_amount(notional):
            errors.append(PlanError("invalid_notional", symbol))
        elif notional > MAX_PER_ORDER_USD:
            errors.append(PlanError("notional_over_cap", symbol))
        if order["ref_id"] != native_ref_id(slot, trading_day.isoformat(), symbol):
            errors.append(PlanError("bad_ref_id", symbol))
        if symbol in seen:
            errors.append(PlanError("duplicate_symbol", symbol))
        seen.add(symbol)
    if errors:
        return dop._fail(*errors)
    return PlanResult(plan=plan, orders=tuple(orders))


def _cmd_plan_first(args: argparse.Namespace, trading_day: date) -> int:
    """``plan --source mac_rec`` (the Mac's local store) or ``--source published_rec`` (the cloud-readable copy).

    Selects the slot's set by the local reader's freshness rule and emits it as a plan document, VALID_PLAN only when
    it is fresh AND passes :func:`_validate_plan_first`. A missing, stale or invalid set is NO_VALID_PLAN, and the
    prompt falls back to the legacy Mac plan and then to compute.
    """
    config, ladder_version, directory = _context(args.config_dir)
    now = _parse_now(args.now)
    if now is None or now.astimezone(ET).date() != trading_day:
        raise _Refused(PlanError("bad_now", "expected a timezone-aware ISO timestamp on the trading day"))
    try:
        max_age = load_settings(directory).max_age
    except ValueError as exc:
        # No silent default: a missing freshness rule must stop buying from a set, not loosen it.
        raise _Refused(PlanError("settings_invalid", type(exc).__name__)) from exc
    if args.source == "mac_rec":
        selection = RecommendationStore(args.rec_root).select_for(args.slot, now, max_age)
        recommendation, reason = selection.recommendation, (None if selection.status == "OK" else selection.reason)
    else:
        if args.published is None:
            raise _Refused(PlanError("published_missing", "--published is required with --source published_rec"))
        try:
            recommendation = _published_set(
                _read_json(args.published, missing="published_missing", corrupt="published_corrupt"), args.slot)
        except _Refused as refusal:
            # An unreadable copy is no published set, as an absent one is: the run falls back to the Mac plan and
            # then to compute, which need no copy. Never read around the defect (no salvaging of a partial set).
            return _emit({"status": "NO_VALID_PLAN", "plan": None, "errors": _error_dicts(refusal.errors)}, 2)
        reason = "no_set_for_slot" if recommendation is None else _freshness_reason(recommendation, args.slot, now,
                                                                                    max_age)
    if reason is not None or recommendation is None:
        return _emit({"status": "NO_VALID_PLAN", "plan": None,
                      "errors": _error_dicts([PlanError(reason or "no_set_for_slot")])}, 2)
    result = _validate_plan_first(_plan_first_document(recommendation, args.source), config=config,
                                  ladder_version=ladder_version, trading_day=trading_day, slot=args.slot)
    if result.errors:
        return _emit({"status": "NO_VALID_PLAN", "plan": None, "errors": _error_dicts(result.errors)}, 2)
    return _emit({"status": "VALID_PLAN", "plan": result.plan, "errors": []}, 0)


def _placement_order_id(document: Any) -> str | None:
    """The broker order id in one saved placement response, read the way ``plan_first_slot finish`` does."""
    data = document.get("data", document) if isinstance(document, dict) else {}
    row = data.get("order", data) if isinstance(data, dict) else {}
    value = row.get("id") or row.get("order_id") if isinstance(row, dict) else None
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) else None


def _cmd_executor_ids(args: argparse.Namespace, trading_day: date) -> int:
    """The broker order ids Claude's slot saved for this slot today, from either writer (Mac only).

    Robinhood echoes its OWN ref_id, never the UUID that was sent (scripts/validate_native_placement.py), so the
    order id in each saved response is the one exact link between a broker row and Claude. Two writers save them:

    * the plan-first reader, under ``<cron root>/recommendations/<day>/placements/<slot>/<TICKER>.json``;
    * the gate-sequence live prompt every slot still runs (``claude/dca-executor-prompts/live-mirror/``), as
      ``$ROOT/place_<TICKER>.json`` in the slot's run directory (:func:`_live_prompt_placements`).

    Reading only the first was a gap found in the 2026-10-26 09:35 stand-down (the 25 orders there were a manual
    backup run's own, not Claude's: docs/incident_2026-10-26_0935_unrecognized_orders.md): the reader prompts were
    never pasted live, so on a day a live-prompt slot buys no ``placements/`` directory exists, the list comes back
    empty, and Claude's own orders would read as UNRECOGNIZED. Neither directory existing means no placements (an
    empty list). A response that cannot be read is a
    refusal: a recognition list with a hole in it would turn Claude's own order into an unrecognized one, and the
    prompt fails closed on that, but silently is worse. ``sources`` counts the files each writer contributed, so a
    slot that bought with both counts at zero is visible in the saved output, not only in the stand-down.
    """
    store = RecommendationStore(args.rec_root)
    plan_first = sorted(store._placed_tickers(store.placements_dir(trading_day, args.slot)))
    cron_root = Path(args.cron_root) if args.cron_root is not None else Path(cron_tracking_root())
    live_prompt = _live_prompt_placements(cron_root, trading_day, args.slot)
    ids: set[str] = set()
    for path in plan_first + live_prompt:
        order_id = _placement_order_id(_read_json(path, missing="placement_missing", corrupt="placement_corrupt"))
        if order_id:
            ids.add(order_id)  # a refused placement saved no order id; there is nothing at the broker to recognise
    return _emit({"status": "OK", "trading_day": trading_day.isoformat(), "slot": args.slot,
                  "order_ids": sorted(ids),
                  "sources": {"plan_first_placements": len(plan_first), "live_prompt_placements": len(live_prompt)},
                  "errors": []}, 0)


# The live prompt's `--redacted-response $ROOT/place_<TICKER>.json`; the ticker shape is dca_rec_store's own.
_LIVE_PROMPT_PLACEMENT = re.compile(r"^place_[A-Z][A-Z0-9.\-]{0,9}\.json$")


def _live_prompt_placements(cron_root: Path, trading_day: date, slot: str) -> list[Path]:
    """The ``place_<TICKER>.json`` responses the live prompt saved in this slot's run directory today.

    The directory is Gate 9's (``verify_schedule.slot_run_dir_relative``: ``<stream>/<YYYYMMDD>_<slot>ET``) plus
    the dashed ``<YYYY-MM-DD>_<slot>ET`` beside it, because the 15:30 prompt resolves its $ROOT with the dashed date
    (1530ET-330-final-buying-run.txt, Gate 6). The redacted copy is written whether or not the native check passed,
    and that is wanted: any order id in it is an order Claude created, so it is executor traffic either way.
    """
    relative = slot_run_dir_relative(slot, trading_day)
    folders = {relative.name, f"{trading_day.isoformat()}_{slot}ET"}
    paths: list[Path] = []
    for folder in sorted(folders):
        directory = cron_root / relative.parent / folder
        if directory.is_dir():
            paths.extend(path for path in directory.iterdir() if _LIVE_PROMPT_PLACEMENT.match(path.name))
    return sorted(paths)


def _load_executor_ids(path: Path, trading_day: date, slot: str) -> set[str]:
    """``executor-ids`` output saved verbatim by the prompt, checked against this run's day and slot."""
    data = _read_json(path, missing="executor_ids_missing", corrupt="executor_ids_corrupt")
    ids = data.get("order_ids") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("status") != "OK" or data.get("trading_day") != trading_day.isoformat()
            or data.get("slot") != slot or not isinstance(ids, list)
            or not all(isinstance(value, str) and value for value in ids)):
        raise _Refused(PlanError("executor_ids_invalid", path.name))
    return set(ids)


def _row_dollars(row: dict[str, Any]) -> float | None:
    amount = _row_amount(row, "dollar_based_amount")
    return amount if amount is not None and math.isfinite(amount) else None


def _classify_row(row: dict[str, Any], *, slot: str, trading_day: date, executor_ids: set[str],
                  published: dict[str, float]) -> tuple[str, str]:
    """``(class, basis)`` for one in-window order: ``user``, ``executor`` or ``unrecognized``.

    Executor evidence, strongest first: the order id the Claude reader saved (exact); a reference this repo derives
    for this slot (the legacy ``dca-runner-<slot>-`` client id, or the native UUID should the broker ever echo it);
    and, when the Mac is out of reach, a buy of a symbol the published set for this slot lists, for exactly that
    set's dollar amount to the cent. The last is the weakest: a set recomputed after Claude's buy can list another
    amount, or drop the ticker, and the order then reads as unrecognized, so the backup stands down (fail closed).
    """
    if row.get("placed_agent") in _USER_AGENTS:
        return "user", "placed_agent"
    if str(row.get("id") or "") in executor_ids:
        return "executor", "plan_first_placement"
    symbol = str(row.get("symbol") or "").strip().upper()
    reference = row.get("ref_id") if isinstance(row.get("ref_id"), str) else ""
    if reference.startswith(f"dca-runner-{slot}-"):
        return "executor", "runner_reference"
    if symbol and reference == native_ref_id(slot, trading_day.isoformat(), symbol):
        return "executor", "native_reference"
    dollars = _row_dollars(row)
    if (str(row.get("side") or "").lower() == "buy" and symbol in published and dollars is not None
            and abs(dollars - published[symbol]) < 0.005):
        return "executor", "published_recommendation"
    return "unrecognized", ""


def _cmd_classify(args: argparse.Namespace, trading_day: date) -> int:
    """Classify every order created inside this slot's placement window: the step 2 rule, in code.

    CLEAR (exit 0) when every one is the user's own activity or recognised executor traffic; UNRECOGNIZED (exit 2)
    when any other order is there, so the prompt stands down. The output names symbols, times and the recognition
    basis only, never an account number or an order id.
    """
    rows = _load_rows(args.orders_pages, trading_day, _week_last4(args))
    executor_ids = _load_executor_ids(args.executor_ids, trading_day, args.slot) if args.executor_ids else set()
    published: dict[str, float] = {}
    if args.published is not None and args.executor_ids is None:
        try:
            recommendation = _published_set(
                _read_json(args.published, missing="published_missing", corrupt="published_corrupt"), args.slot)
        except _Refused:
            recommendation = None
        # Recognition does not need a FRESH set (Claude may have bought from an older one), only today's.
        if recommendation is not None and recommendation.trading_day == trading_day:
            published = {item.ticker: item.usd for item in recommendation.items}
    start, end = dop.placement_window(args.slot, trading_day)
    executor, user, unrecognized = [], 0, []
    for row in rows:
        if not isinstance(row, dict):
            raise _Refused(PlanError("history_unusable", "row is not an object"))
        try:
            created = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise _Refused(PlanError("history_unusable", "created_at")) from exc
        if created.tzinfo is None:
            raise _Refused(PlanError("history_unusable", "created_at is not timezone-aware"))
        if not start <= created.astimezone(ET) < end:
            continue
        kind, basis = _classify_row(row, slot=args.slot, trading_day=trading_day, executor_ids=executor_ids,
                                    published=published)
        entry = {"symbol": str(row.get("symbol") or "").upper(), "created_at": created.isoformat(),
                 "side": str(row.get("side") or "").lower()}
        if kind == "user":
            user += 1
        elif kind == "executor":
            executor.append({**entry, "basis": basis})
        else:
            unrecognized.append({**entry, "placed_agent": row.get("placed_agent") if isinstance(row.get("placed_agent"), str) else None})
    status = "UNRECOGNIZED" if unrecognized else "CLEAR"
    return _emit({"status": status, "window": [start.isoformat(), end.isoformat()], "user_orders": user,
                  "executor_orders": executor, "unrecognized_orders": unrecognized,
                  "published_set_used": bool(published), "errors": []}, 2 if unrecognized else 0)


def _cmd_check_placement(args: argparse.Namespace, trading_day: date) -> int:
    """Step 7e: did this one place call create a NEW order for exactly the request? (``validate_native_placement``)

    The broker echoes its own ref_id, never the UUID that was sent, so the old "returned ref_id must match" rule
    recorded every accepted order as a failure (scripts/validate_native_placement.py). This runs the repo's own
    native check instead: an accepted state, the requested symbol, side and dollar amount, an order id absent from
    the history fetched before the call, and a ``created_at`` at or after the sampled submit instant.

    The prior history is poll 2 (complete, pinned pages: the same proof ``finalize`` needs) plus the saved responses
    of this run's earlier orders, so the task never retypes an order id. PLACED (exit 0) prints the order id for the
    state record; anything else is NOT_PLACED (exit 2), a failure the prompt never retries.
    """
    prior: set[str] = set()
    for row in _load_rows(args.prior_orders_pages, trading_day, _week_last4(args)):
        order_id = row.get("id") if isinstance(row, dict) else None
        if isinstance(order_id, str) and order_id.strip():
            prior.add(order_id.strip())
    for path in args.placed_responses or ():
        order_id = _placement_order_id(_read_json(path, missing="placement_missing", corrupt="placement_corrupt"))
        if order_id:
            prior.add(order_id)
    response = _read_json(args.response, missing="placement_missing", corrupt="placement_corrupt")
    amount = _parse_money(args.amount)
    if amount is None or amount <= 0:
        raise _Refused(PlanError("invalid_amount", "must be a positive amount"))
    try:
        verdict = validate_native_placement(
            response, expected_symbol=args.symbol.strip().upper(), expected_side="buy", expected_amount_usd=amount,
            sent_ref_id=args.sent_ref_id, prior_order_ids=frozenset(prior), submitted_after=args.submitted_after,
        )
    except ValueError as exc:
        raise _Refused(PlanError("bad_submitted_after", type(exc).__name__)) from exc
    if verdict["ok"]:
        return _emit({"status": "PLACED", "symbol": args.symbol.strip().upper(), "order_id": verdict["order_id"],
                      "errors": []}, 0)
    # The validator's reason names fields and states, never the account (it redacts diagnostic text itself).
    return _emit({"status": "NOT_PLACED", "symbol": args.symbol.strip().upper(), "order_id": None,
                  "errors": _error_dicts([PlanError("placement_unconfirmed", str(verdict["reason"]))])}, 2)


def _cmd_compute(args: argparse.Namespace, trading_day: date) -> int:
    if args.market_data is not None:
        return _cmd_compute_gated(args, trading_day)
    config, ladder_version, _ = _context(args.config_dir)
    now = _parse_now(args.now)
    if now is None:
        raise _Refused(PlanError("bad_now", "expected a timezone-aware ISO timestamp"))
    buying_power = _parse_buying_power(args.buying_power)
    history = _load_history(args.orders_pages, trading_day, _week_last4(args))
    result = dop.compute_plan_from_file(
        args.quotes, config=config, trading_day=trading_day, slot=args.slot, generated_at_et=now,
        ladder_version=ladder_version, orders_today=history, tier=_NORMAL_TIER, buying_power_usd=buying_power,
        trim_to_fit=True,
    )
    if result.errors or result.plan is None:
        return _emit({"status": "FAILED", "plan": None, "errors": _error_dicts(result.errors)}, 2)
    return _emit({"status": "VALID_PLAN", "plan": result.plan, "skipped": _skipped_dicts(result.skipped), "errors": []}, 0)


def _cmd_compute_gated(args: argparse.Namespace, trading_day: date) -> int:
    """The safety gate: the primary's own Gate 5 decides what to buy, the plan only carries it out.

    Nothing is sized here. The slot's own generator (the 15:30 engine, or the stream CLI path for the six
    executor-class slots) reaches each decision with its trend, data-confidence and provider-error handling,
    per-ticker calibration and kill switch; this wraps the BUYs in the same capped plan the quotes path builds.
    """
    if args.slot not in GATED_SLOTS:
        raise _Refused(PlanError("slot_not_supported", f"{args.slot}: the safety gate covers {', '.join(GATED_SLOTS)} only"))
    now = _parse_now(args.now)
    if now is None or now.astimezone(ET).date() != trading_day:
        raise _Refused(PlanError("bad_now", "expected a timezone-aware ISO timestamp on the trading day"))
    buying_power = _parse_buying_power(args.buying_power)
    week_last4 = _week_last4(args)
    history = _load_history(args.orders_pages, trading_day, week_last4)
    deployed = _parse_deployed(args.deployed_last_7d_usd, history, now)
    payload = _load_market_data(args.market_data)
    directory = args.config_dir if args.config_dir is not None else dca_config.get_config_dir()
    extra: dict[str, Any] = {}
    if week_last4 is not None:
        # A week envelope cannot prove the days before Monday, so the kill switch is treated as engaged (module note
        # above): the figure is raised to the threshold, never lowered, and the generators then size at 1x at most.
        floor = _engaged_floor(dca_config.load_strategy_config(directory))
        if floor is not None and deployed < floor:
            deployed = floor
        extra = {"kill_switch_basis": "week_envelope_treated_as_engaged"}
    try:
        config = dca_config.load_strategy_config(directory, deployed_last_7d_usd=deployed)
        # Computed plans cannot resize a top-up, so any buy already made today keeps the ticker out (the same
        # day-wide rule finalize applies to a backup plan). Only universe tickers: the generators reject others.
        covered = sorted(dop.already_placed_symbols(history, trading_day=trading_day, slot=args.slot, scope="day") & set(config.tickers))
        if args.slot == "1530":
            decisions = _decisions_final_buy(payload, config, directory, trading_day, deployed, covered)
        else:
            decisions = _decisions_stream(args.slot, payload, directory, trading_day, deployed, covered)
    except ValueError as exc:
        # Type name only: the message can carry untrusted payload content (a source name, a path).
        raise _Refused(PlanError("market_data_invalid", type(exc).__name__)) from exc
    # Normal-only (2026-10-22): the Claude slots never escalate, so a backup does not either. A batch over the Normal
    # caps is trimmed to the prefix that fits and the rest is reported (2026-10-16: $205.90 against a $180 cap).
    tier, weekly_cap = _tier_for((d["multiplier"] for d in decisions), config)
    result = dop.plan_from_decisions(
        decisions, config=config, trading_day=trading_day, slot=args.slot, generated_at_et=now,
        ladder_version=dop.current_ladder_version(directory), orders_today=history, tier=tier,
        weekly_cap_usd=weekly_cap, buying_power_usd=buying_power, trim_to_fit=True,
    )
    if result.errors or result.plan is None:
        return _emit({"status": "FAILED", "plan": None, "errors": _error_dicts(result.errors), **extra}, 2)
    return _emit({"status": "VALID_PLAN", "plan": result.plan, "skipped": _skipped_dicts(result.skipped), "errors": [],
                  **extra}, 0)


def _decisions_final_buy(payload: dict[str, Any], config: Any, directory: Path, trading_day: date,
                         deployed: float, covered: list[str]) -> list[dict[str, Any]]:
    """15:30: the final-buy engine's BUY rows, minus those its Gate 6 would withhold as planner-incompatible.

    The compatibility row carries the ticker's calibration bounds (``_calibration_fields``), as the stream document's
    rows do. A bare 4-field row is judged against the GLOBAL ladder and so flags nearly every calibrated ticker,
    which would plan nothing; with its bounds it flags only a row whose multiplier, notional and drop disagree.
    """
    stream_candidates._require_matching_runner_config_dir(directory)
    gbc._require_populated_config(directory)
    decision_time = datetime.combine(trading_day, gbc.SLOT_SPECS["1530ET"]["decision_time_et"], tzinfo=ET)
    config = dca_config.load_strategy_config(directory, now=decision_time, deployed_last_7d_usd=deployed)
    rows = gbc.generate_candidates(
        slot="1530ET", now=decision_time, config=config, market_data_by_source=payload["sources"],
        source_errors=payload.get("source_errors", []), ticker_errors=payload.get("ticker_errors"),
        already_covered_tickers=covered,
    )
    decisions = []
    for row in rows:
        if row["action"] != "BUY":
            continue
        planner_row = [row["ticker"], row["suggested_amount_usd"], row["buy_multiplier"], row["multiplier_drop_pct"]]
        calibration = stream_candidates._calibration_fields(row["ticker"], row["buy_multiplier"], row["selected_base_amount_usd"])
        if calibration:
            planner_row += [calibration["sizing_base_usd"], calibration["sizing_min_drop_pct"], calibration["sizing_max_drop_pct"]]
        if gbc._planner_incompatible(planner_row):
            continue
        bar = _prior_bar(payload["sources"], row["ticker"], str(row.get("target_price_source") or ""))
        low = bar.get("low") if bar else None
        decisions.append({
            "symbol": row["ticker"], "notional_usd": row["suggested_amount_usd"], "multiplier": row["buy_multiplier"],
            "drop_pct": row["multiplier_drop_pct"], "decision_price": row["target_price"],
            "prior_close": bar.get("close") if bar else None, "prior_low": low if isinstance(low, (int, float)) and low > 0 else None,
        })
    return decisions


def _decisions_stream(slot: str, payload: dict[str, Any], directory: Path, trading_day: date,
                      deployed: float, covered: list[str]) -> list[dict[str, Any]]:
    """An executor-class slot: the primary's own stream CLI path, run on the payload, and its ``cands`` BUY rows.

    The empty ``finance`` placeholder the assembler writes is dropped (these slots score Robinhood); a non-empty one is
    left in so the stream CLI refuses it exactly as it refuses an unsupported source.
    """
    spec = backup_stream_specs.stream_spec(slot)
    sources = {name: block for name, block in payload["sources"].items() if not (name == "finance" and block == {})}
    ticker_errors = {name: errs for name, errs in (payload.get("ticker_errors") or {}).items() if name == "robinhood" or errs}
    stream_payload = {"sources": sources, "source_errors": payload.get("source_errors", []), "ticker_errors": ticker_errors}
    with tempfile.TemporaryDirectory(prefix="backup-stream-") as scratch:
        path = Path(scratch) / "market.json"
        path.write_text(json.dumps(stream_payload), encoding="utf-8")
        namespace = argparse.Namespace(
            trading_day=trading_day.isoformat(), market_data=path, config_dir=directory, decision_time_et=None,
            already_covered_tickers=covered,
            deployed_last_7d_usd=deployed if spec.requires_deployed_last_7d_usd else None,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stream_candidates._run_with_args(spec, namespace)
    document = json.loads(out.getvalue())
    withheld = set(document.get("planner_incompatible_tickers") or ())
    decisions = []
    for row in document["cands"]:
        if row["ticker"] in withheld:
            continue
        low = row.get("prior_low")
        decisions.append({
            "symbol": row["ticker"], "notional_usd": row["desired_usd"], "multiplier": row["multiplier"],
            "drop_pct": row["drop_pct"], "decision_price": row["current_price"], "prior_close": row["prior_close"],
            "prior_low": low if isinstance(low, (int, float)) and low > 0 else None,
        })
    return decisions


def _cmd_procedure(args: argparse.Namespace) -> int:
    """Print one part of this task's backup instructions as plain text.

    Part 0 carries the rules and preflight; parts 1-3 carry steps 1-9. They ship inside this build and are covered
    by the same content_sha256 stamp. Any failure is one JSON document and exit 2, which the prompt treats as
    place-nothing.
    """
    from scripts import backup_procedure  # lazy: it imports this module for GATED_SLOTS

    try:
        text = backup_procedure.render_part(args.task, args.part, token=args.token)
    except (KeyError, ValueError) as exc:
        return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("bad_procedure_request", str(exc))])}, 2)
    sys.stdout.write(text)
    return 0


def _cmd_gates(args: argparse.Namespace) -> int:
    """Step 1 as code: one JSON status (PROCEED, skipped_market_closed, skipped_outside_window).

    The clock is the system clock converted to America/New_York; ``--now`` exists for tests and must carry an
    offset. A normal skip exits 0 (it is a recorded status, not an error); anything unusable exits 2.
    """
    from datetime import timezone

    from scripts import backup_gates  # lazy: it imports backup_procedure, which imports this module

    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        calendar = json.loads((dca_config.get_config_dir() / "market_calendar.json").read_text(encoding="utf-8"))
        report = backup_gates.evaluate(args.task, now, calendar)
    except (KeyError, ValueError, OSError) as exc:
        return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("bad_gates_request", str(exc))])}, 2)
    return _emit(report, 0)


def _read_build_info() -> dict | None:
    """The BUILD_INFO.json stamped inside the ``backup_planner.pyz`` zipapp this code runs from, or None.

    A checkout (the Mac) has no archive and so no stamp; the prompt only runs ``preflight`` from the sandbox file.
    The planner reports its build and never judges it: the expected hash cannot live inside the build it names, so
    the comparison stays in the pasted prompt.
    """
    import zipfile

    archive = getattr(getattr(sys.modules[__name__], "__loader__", None), "archive", None)
    if not archive:
        return None
    try:
        with zipfile.ZipFile(archive) as bundle:
            info = json.loads(bundle.read("BUILD_INFO.json").decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    return info if isinstance(info, dict) else None


def _cmd_preflight(args: argparse.Namespace) -> int:
    """STEP B in one call (2026-11-09): the stamped build plus the step 1 gates, as one JSON document.

    The 2026-11-09 final backup spent 18 minutes on setup calls before its gates ran and skipped at 16:03 ET. This
    replaces ``--build-info`` and ``gates`` with one command, and a skip ends the run before part 0 is requested.
    A skip exits 0 (a recorded status); an unreadable build, an unknown task, or an unusable clock exits 2.
    """
    from datetime import timezone

    from scripts import backup_gates  # lazy: it imports backup_procedure, which imports this module

    build = _read_build_info()
    sha = build.get("content_sha256") if build else None
    if not isinstance(sha, str) or not sha:
        return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("build_info_unavailable", "no stamped build")])}, 2)
    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        calendar = json.loads((dca_config.get_config_dir() / "market_calendar.json").read_text(encoding="utf-8"))
        report = backup_gates.evaluate(args.task, now, calendar)
    except (KeyError, ValueError, OSError) as exc:
        return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("bad_gates_request", str(exc))])}, 2)
    build_report = {"content_sha256": sha, "source_commit": build.get("source_commit")}
    return _emit({**report, "build": build_report}, 0)


def _cmd_universe(args: argparse.Namespace) -> int:
    """The buyable symbols from the packaged config: the task fetches exactly these, so no prompt names a stock."""
    directory = args.config_dir if args.config_dir is not None else dca_config.get_config_dir()
    if not Path(directory).is_dir():
        return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("config_missing", Path(directory).name)])}, 2)
    try:
        gbc._require_populated_config(Path(directory))
    except ValueError as exc:
        return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("config_invalid", type(exc).__name__)])}, 2)
    config = dca_config.load_strategy_config(directory)
    whole_share = set(getattr(config, "whole_share_tickers", ()) or ())
    symbols = sorted(set(config.tickers) - set(config.excluded_tickers) - whole_share)
    if not symbols:
        return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("universe_unavailable", "no buyable symbols")])}, 2)
    return _emit({"status": "OK", "symbols": symbols, "count": len(symbols), "universe_hash": dop.universe_hash(config.tickers)}, 0)


def _collect(errors: list[PlanError], load: Any) -> Any:
    """Run one loader; on refusal record its errors and return ``None`` so every bad input is reported."""
    try:
        return load()
    except _Refused as refusal:
        errors.extend(refusal.errors)
        return None


def _load_plan_any(path: Path, *, config: Any, ladder_version: str, trading_day: date, slot: str) -> PlanResult:
    """A dca_order_plan document (Claude's legacy order_plan or a computed plan) or a plan-first one, validated."""
    try:
        document = _read_json(path, missing="plan_missing", corrupt="plan_corrupt")
    except _Refused as refusal:
        return dop._fail(*refusal.errors)
    if isinstance(document, dict) and document.get("generator") == PLAN_FIRST_GENERATOR:
        return _validate_plan_first(document, config=config, ladder_version=ladder_version, trading_day=trading_day,
                                    slot=slot)
    return dop.validate_plan(document, **_plan_context(config, ladder_version, trading_day, slot))


def _cmd_finalize(args: argparse.Namespace, trading_day: date) -> int:
    config, ladder_version, _ = _context(args.config_dir)
    plan = _load_plan_any(args.plan, config=config, ladder_version=ladder_version, trading_day=trading_day,
                          slot=args.slot)
    errors: list[PlanError] = list(plan.errors)
    week_last4 = _week_last4(args)
    history = _collect(errors, lambda: _load_history(args.orders_pages, trading_day, week_last4))
    fresh = _collect(errors, lambda: _load_fresh_quotes(args.fresh_quotes))
    spreads = _collect(errors, lambda: _load_fresh_spreads(args.fresh_spreads))
    buying_power = _collect(errors, lambda: _parse_buying_power(args.buying_power))
    plan_first = plan.plan is not None and plan.plan["generator"] == PLAN_FIRST_GENERATOR
    prior_closes: dict[str, float] = {}
    if plan_first:
        # A recommendation is price-blind by design, so rule 6's "below the prior close" needs the close from the
        # task's own fresh fetch. Required: without it no order could prove rule 6.
        if args.fresh_prior_closes is None:
            errors.append(PlanError("fresh_prior_closes_missing", "required for a plan_first plan"))
        else:
            prior_closes = _collect(errors, lambda: _load_fresh_quotes(
                args.fresh_prior_closes, missing="fresh_prior_closes_missing", corrupt="fresh_prior_closes_corrupt")) or {}
    if errors:
        return _abort(*errors)
    if plan.plan["generator"] not in _PRIMARY_SIZED_GENERATORS:
        guard = _kill_switch_error(plan.orders, history, config, trading_day, args.now, trailing_proven=week_last4 is None)
        if guard is not None:
            return _abort(guard)
    elif plan_first:
        guard = _plan_first_kill_switch_error(plan.plan, history, config, trading_day, args.now,
                                              trailing_proven=week_last4 is None)
        if guard is not None:
            return _abort(guard)
    # A Claude or plan-first plan already carries any top-up resize, so only THIS slot's window counts as
    # already placed. A computed plan cannot resize, so it keeps the conservative day-wide exclusion.
    scope = "slot" if plan.plan["generator"] in _PRIMARY_SIZED_GENERATORS else "day"
    candidates: list[dict[str, Any]] = list(plan.orders)
    skipped: list[dict[str, str]] = []
    if plan_first:
        candidates = [{**o, "prior_close": prior_closes[o["symbol"]]} for o in plan.orders if o["symbol"] in prior_closes]
        skipped = [{"symbol": o["symbol"], "reason": "no_prior_close"} for o in plan.orders if o["symbol"] not in prior_closes]
    remaining = dop.reconcile(candidates, history, trading_day=trading_day, slot=args.slot, scope=scope)
    remaining_symbols = {o["symbol"] for o in remaining}
    skipped += [{"symbol": o["symbol"], "reason": "already_placed"} for o in candidates if o["symbol"] not in remaining_symbols]
    # Normal-only (2026-10-22), whatever tier the plan was sized under: a set over the Normal caps places the prefix
    # that fits and reports the rest in ``skipped`` (never shrunk, never back-filled, never placed over a proven cap).
    tier, weekly_cap = _tier_for((o.get("multiplier", 0.0) for o in plan.orders), config)
    result = dop.revalidate_for_placement(
        remaining, broker_orders=history, fresh_quotes=fresh, trading_day=trading_day, tier=tier,
        weekly_cap_usd=weekly_cap, config=config, slot=args.slot, scope=scope, buying_power_usd=buying_power,
        fresh_spreads=spreads, trim_to_fit=True,
    )
    if result.errors:
        return _abort(*result.errors)
    # Number grounding runs on the plan as WRITTEN (before reconcile/prior-close overlays), report-only unless enforced.
    grounding, blocking = _grounding_report(args, plan, config)
    if blocking:
        return _abort(*blocking)
    extra = {} if grounding is None else {"number_grounding": grounding}
    skipped += [{"symbol": symbol, "reason": reason} for symbol, reason in result.skipped]
    orders = [{"symbol": o["symbol"], "notional_usd": o["notional_usd"], "ref_id": o["ref_id"]} for o in result.orders]
    mac_out_of_the_picture = args.local_status in BACKUP_MAY_ACT_LOCAL_STATUSES
    if plan.plan["generator"] not in _PRIMARY_SIZED_GENERATORS and not (COMPUTE_PLACEMENT_ENABLED and mac_out_of_the_picture):
        return _emit({"status": "SHADOW_ONLY", "to_place": [], "would_place": orders, "skipped": skipped, "errors": [], **extra}, 0)
    return _emit({"status": "PROCEED", "to_place": orders, "skipped": skipped, "errors": [], **extra}, 0)


def _kill_switch_error(orders: Any, history: list[BrokerOrder], config: Any, trading_day: date,
                       now_text: str | None, *, trailing_proven: bool = True) -> PlanError | None:
    """Poll 2's second look at the trailing-7-day guard for a computed plan, else ``None`` when it still holds.

    A computed plan was sized from poll 1's figure; spend that lands between the polls can trip the kill switch
    while the plan still carries a multiplier above 1x, so the figure is re-derived from the fresh pull and an
    over-1x plan is refused (place nothing; the plan is stale). ``now`` must be on the trading day in ET: it is
    the window's upper bound, and an earlier day would classify today's orders as future and leave the guard off.
    A Claude plan is sized by the primary and is not rechecked here: its own earlier fills would count toward the
    figure and wrongly abort a partly finished batch.

    ``trailing_proven`` is False for a week envelope (days before Monday unproven): the switch is then treated as
    engaged, so an over-1x plan is refused with ``kill_switch_unproven`` unless the proven part alone engages it.
    """
    now = _parse_now(now_text)
    if now is None:
        return PlanError("bad_now", "expected a timezone-aware ISO timestamp")
    if now.astimezone(ET).date() != trading_day:
        return PlanError("now_not_trading_day", trading_day.isoformat())
    try:
        deployed = dop.trailing_deployed_usd(history, now=now)
    except BudgetDataError as exc:
        return PlanError("history_unusable", type(exc).__name__)
    over_1x = any(o["multiplier"] > 1.0 for o in orders)
    if kill_switch_engaged(dataclasses.replace(config, deployed_last_7d_usd=deployed)) and over_1x:
        return PlanError("kill_switch_engaged_since_plan", "trailing seven-day spend now exceeds the kill-switch threshold")
    if not trailing_proven and over_1x and _engaged_floor(config) is not None:
        return PlanError("kill_switch_unproven", "a week envelope cannot prove the days before Monday; over-1x refused")
    return None


def _plan_first_kill_switch_error(plan: dict[str, Any], history: list[BrokerOrder], config: Any, trading_day: date,
                                  now_text: str | None, *, trailing_proven: bool = True) -> PlanError | None:
    """Poll 2's recheck of the trailing-7-day guard for a plan-first set, else ``None`` when it still holds.

    The set was sized under the kill-switch state its ``sizing_guard`` records. Other slots' spend can land inside
    the freshness window and engage the switch, and a set sized above 1x must not then be placed. The payload carries
    no per-order multiplier (it is price-blind), so the stamp's ``max_multiplier`` stands in for it: at 1x the set
    cannot be over the cap and needs no recheck. The figure is re-derived from the fresh pull MINUS this set's own
    orders (matched by their native ref_id), because a partly placed batch must not trip the switch on its own
    fills -- the reason a Claude plan is exempt from :func:`_kill_switch_error`. A set with no stamp (older writer) or
    a history that cannot prove the figure fails closed and places nothing.
    """
    guard = plan.get("sizing_guard")
    if not isinstance(guard, dict):
        return PlanError("sizing_guard_missing", "an unstamped plan-first set cannot be rechecked against the kill switch")
    if guard["max_multiplier"] <= 1.0:
        return None
    now = _parse_now(now_text)
    if now is None:
        return PlanError("bad_now", "expected a timezone-aware ISO timestamp")
    if now.astimezone(ET).date() != trading_day:
        return PlanError("now_not_trading_day", trading_day.isoformat())
    own_refs = {order["ref_id"] for order in plan["orders"]}
    others = [o for o in history if not ({o.client_order_id, o.ref_id} & own_refs)]
    try:
        deployed = dop.trailing_deployed_usd(others, now=now)
    except BudgetDataError as exc:
        return PlanError("history_unusable", type(exc).__name__)
    if kill_switch_engaged(dataclasses.replace(config, deployed_last_7d_usd=deployed)):
        return PlanError("kill_switch_engaged_since_plan", "trailing seven-day spend now exceeds the kill-switch threshold")
    if not trailing_proven and _engaged_floor(config) is not None:
        # Treated as engaged (week envelope): an over-1x set is refused; the prompt falls back to the full fetch.
        return PlanError("kill_switch_unproven", "a week envelope cannot prove the days before Monday; over-1x refused")
    return None


def _abort(*errors: PlanError) -> int:
    return _emit({"status": "ABORT", "to_place": [], "skipped": [], "errors": _error_dicts(errors)}, 2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_now(text: str | None) -> datetime | None:
    if text is None:
        return datetime.now(ET)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _grounding_args(parser: argparse.ArgumentParser) -> None:
    """The two unsupported-number-check flags (``dca_number_check``); report-only unless enforced."""
    parser.add_argument("--market-data", type=Path, default=None,
                        help="The assembled market-data payload; when given, the plan's prices, drop and notional are "
                             "checked against it and findings reported under 'number_grounding'.")
    parser.add_argument("--enforce-number-grounding", action="store_true", default=False,
                        help="Block on unsupported numbers instead of only reporting them (default OFF: report-only).")


def _week_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--week-envelope", action="store_true",
                   help="MAC ONLY: the orders file is this run's merged ISO-week envelope (scripts.backup_week_history), "
                        "not a today-dated poll. Requires --account-last4. The kill switch is then treated as engaged.")
    p.add_argument("--account-last4", default=None,
                   help="With --week-envelope: the last four digits of the account this run resolved.")


def _week_last4(args: argparse.Namespace) -> str | None:
    return args.account_last4 if getattr(args, "week_envelope", False) else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD, America/New_York.")
        p.add_argument("--slot", required=True, help="Slot id: one of " + ", ".join(SLOTS))
        p.add_argument("--config-dir", type=Path, default=None)

    plan = sub.add_parser("plan", help="Read and validate a plan: the Mac's plan file or a plan-first recommendation.")
    common(plan)
    plan.add_argument("--source", required=True, choices=("mac_plan",) + _PLAN_FIRST_SOURCES,
                      help="mac_plan: the legacy order_plan file. mac_rec: the Mac's local plan-first store. "
                           "published_rec: a published plan-first payload saved by the task (--published).")
    plan.add_argument("--plan-file", type=Path, default=None)
    plan.add_argument("--published", type=Path, default=None,
                      help="The published recommendations payload, saved untouched (with --source published_rec).")
    plan.add_argument("--rec-root", default=None, help="Recommendation store root (default: the cron root's).")
    plan.add_argument("--now", default=None, help="ET-day ISO timestamp the freshness rule is applied at.")
    _grounding_args(plan)
    classify = sub.add_parser("classify", help="Classify every order inside this slot's window; stand down on any unknown.")
    common(classify)
    classify.add_argument("--orders-pages", required=True, type=Path)
    classify.add_argument("--executor-ids", type=Path, default=None, help="The Mac's executor-ids output, saved untouched.")
    classify.add_argument("--published", type=Path, default=None, help="The published recommendations payload, if saved.")
    _week_args(classify)
    executor_ids = sub.add_parser("executor-ids", help="Mac only: order ids Claude's slot saved for this slot.")
    common(executor_ids)
    executor_ids.add_argument("--rec-root", default=None, help="Recommendation store root (default: the cron root's).")
    executor_ids.add_argument("--cron-root", default=None,
                              help="Cron-tracking root holding the live prompt's run directories (default: "
                                   "$DCA_CRON_TRACKING_ROOT, as dca_paths resolves it).")
    check = sub.add_parser("check-placement", help="Step 7e: validate one saved placement response (native ref_id rule).")
    common(check)
    check.add_argument("--response", required=True, type=Path, help="The raw placement response, saved untouched.")
    check.add_argument("--symbol", required=True)
    check.add_argument("--amount", required=True, help="The notional_usd that was sent.")
    check.add_argument("--sent-ref-id", required=True, help="The ref_id that was sent.")
    check.add_argument("--submitted-after", required=True, help="Offset-aware instant sampled just before the call.")
    check.add_argument("--prior-orders-pages", required=True, type=Path, help="Poll 2's saved orders envelope.")
    check.add_argument("--placed-responses", type=Path, nargs="*", default=(),
                       help="This run's earlier saved placement responses (their order ids are prior history too).")
    _week_args(check)
    compute = sub.add_parser("compute", help="Derive a plan: from quotes (ladder only) or through the safety gate.")
    common(compute)
    source = compute.add_mutually_exclusive_group(required=True)
    source.add_argument("--quotes", type=Path, help="Quotes only: the price ladder, no signal gates (shadow use).")
    source.add_argument("--market-data", type=Path,
                        help="The assembled payload (scripts/assemble_market_data.py): runs the primary's Gate 5.")
    compute.add_argument("--deployed-last-7d-usd", default=None,
                         help="Trailing-7-day deployment for the kill switch, or 'auto' to derive it from --orders-pages. "
                              "Required with --market-data.")
    compute.add_argument("--orders-pages", required=True, type=Path)
    compute.add_argument("--now", default=None)
    compute.add_argument("--buying-power", required=True, help="Fresh available buying power in USD.")
    _week_args(compute)
    procedure = sub.add_parser("procedure", help="Print one stamped part (0-3) of a backup task's instructions.")
    procedure.add_argument("--task", required=True, help="The backup task id named in the prompt's header.")
    procedure.add_argument("--part", required=True, type=int,
                           help="0 = rules and preflight, 1 = steps 1-3, 2 = steps 4-6, 3 = steps 7-9.")
    procedure.add_argument("--token", default=None, help="Parts 1-3: the token the previous part printed at its end.")
    gates = sub.add_parser("gates", help="Step 1 as code: slot, market day and window from the real clock.")
    gates.add_argument("--task", required=True, help="The backup task id named in the prompt's header.")
    gates.add_argument("--now", default=None, help="TESTS ONLY: an offset-aware ISO instant instead of the clock.")
    preflight = sub.add_parser("preflight", help="STEP B in one call: the stamped build and the step 1 gates.")
    preflight.add_argument("--task", required=True, help="The backup task id named in the prompt's header.")
    preflight.add_argument("--now", default=None, help="TESTS ONLY: an offset-aware ISO instant instead of the clock.")
    universe = sub.add_parser("universe", help="Print the buyable symbols from config; the task fetches exactly these.")
    universe.add_argument("--config-dir", type=Path, default=None)
    finalize = sub.add_parser("finalize", help="Reconcile and re-validate a plan; print what to place.")
    common(finalize)
    finalize.add_argument("--plan", required=True, type=Path)
    finalize.add_argument("--orders-pages", required=True, type=Path)
    finalize.add_argument("--fresh-quotes", required=True, type=Path)
    finalize.add_argument("--fresh-spreads", required=True, type=Path,
                          help='JSON {"SYMBOL": {"bid": x, "ask": y}} fetched just before placement.')
    finalize.add_argument("--buying-power", required=True, help="Fresh available buying power in USD.")
    finalize.add_argument("--fresh-prior-closes", type=Path, default=None,
                          help='JSON {"SYMBOL": prior regular-session close}; required for a plan_first plan.')
    finalize.add_argument("--local-status", default=None, choices=LOCAL_STATUSES,
                          help="Step 3's local_status. A computed plan places only for off, errored or unreachable.")
    finalize.add_argument("--now", default=None,
                          help="ET-day ISO timestamp for poll 2's kill-switch recheck of a computed plan (default: the clock).")
    _week_args(finalize)
    _grounding_args(finalize)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "gates":
        try:
            return _cmd_gates(args)
        except Exception as exc:  # noqa: BLE001 - last fail-closed boundary; type name only
            return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("unexpected_error", type(exc).__name__)])}, 2)
    if args.command == "preflight":
        try:
            return _cmd_preflight(args)
        except Exception as exc:  # noqa: BLE001 - last fail-closed boundary; type name only
            return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("unexpected_error", type(exc).__name__)])}, 2)
    if args.command == "procedure":
        # No trading day or slot: the procedure text is per task, not per run.
        try:
            return _cmd_procedure(args)
        except Exception as exc:  # noqa: BLE001 - last fail-closed boundary; type name only
            return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("unexpected_error", type(exc).__name__)])}, 2)
    if args.command == "universe":
        try:
            return _cmd_universe(args)
        except Exception as exc:  # noqa: BLE001 - last fail-closed boundary; type name only
            return _emit({"status": "FAILED", "errors": _error_dicts([PlanError("unexpected_error", type(exc).__name__)])}, 2)
    if args.command == "compute":
        if args.market_data is not None and args.deployed_last_7d_usd is None:
            parser.error("--deployed-last-7d-usd is required with --market-data")
        if args.quotes is not None and args.deployed_last_7d_usd is not None:
            parser.error("--deployed-last-7d-usd only applies with --market-data")
    if getattr(args, "week_envelope", False) and not _LAST4_RE.match(args.account_last4 or ""):
        parser.error("--week-envelope requires --account-last4 with exactly four digits")
    if not getattr(args, "week_envelope", False) and getattr(args, "account_last4", None) is not None:
        parser.error("--account-last4 only applies with --week-envelope")
    failed = _abort if args.command == "finalize" else (lambda *e: _emit({"status": "FAILED", "plan": None, "errors": _error_dicts(e)}, 2))
    if not _DAY_RE.match(args.trading_day):
        return failed(PlanError("bad_trading_day", "expected YYYY-MM-DD"))
    try:
        trading_day = date.fromisoformat(args.trading_day)
    except ValueError:
        return failed(PlanError("bad_trading_day", "not a calendar date"))
    if args.slot not in SLOTS:
        return failed(PlanError("unknown_slot", args.slot))
    handler = {"plan": _cmd_plan, "compute": _cmd_compute, "finalize": _cmd_finalize, "classify": _cmd_classify,
               "executor-ids": _cmd_executor_ids, "check-placement": _cmd_check_placement}[args.command]
    try:
        return handler(args, trading_day)
    except _Refused as refusal:
        return failed(*refusal.errors)
    except Exception as exc:  # noqa: BLE001 - the last fail-closed boundary: the prompt places nothing on exit 2
        # Type name only: a message can carry paths, config contents, or account data.
        return failed(PlanError("unexpected_error", type(exc).__name__))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
