#!/usr/bin/env python3
"""The planner the Perplexity backup prompts call: read a plan, compute one, or finalize one.

Three subcommands, each printing ONE JSON document and exiting 0 when the result is usable and
2 on anything else, in which case the prompt places nothing (the contract is the table in
``claude/dca-executor-prompts/perplexity/README.md``)::

    plan      --source mac_plan --trading-day D --slot S [--plan-file F]
    compute   --trading-day D --slot S --quotes F --orders-pages F --buying-power USD [--now T]
    finalize  --trading-day D --slot S --plan F --orders-pages F --fresh-quotes F
              --fresh-spreads F --buying-power USD [--now T]

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
It always applies the Normal budget tier and never escalates it.

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
import io
import json
import math
import re
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import dca_config
import dca_order_plan as dop
from dca_order_plan import PlanError
from dca_runner import ET, MAX_PER_ORDER_USD, SLOTS, BrokerOrder, BudgetDataError
from near_close_dca import BUDGET_TIERS, kill_switch_engaged
from scripts import backup_stream_specs, stream_candidates
from scripts import generate_buy_candidates as gbc
from scripts.assemble_order_pages import _order_rows
from scripts.broker_reconciliation import _has_continuation, _unwrap
from scripts.same_day_top_up_gate import _bind_saved_pages

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NORMAL_TIER = next(tier for tier in BUDGET_TIERS if tier.name == "normal")
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


class _Refused(Exception):
    """Carries structured errors out of a loader; never shown raw."""

    def __init__(self, *errors: PlanError) -> None:
        super().__init__(errors[0].code if errors else "refused")
        self.errors = errors


def _error_dicts(errors: Any) -> list[dict[str, str]]:
    return [{"code": e.code, "detail": e.detail} for e in errors]


def _emit(report: dict[str, Any], code: int) -> int:
    print(json.dumps(report, indent=2))
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


def _load_history(path: Path, trading_day: date) -> list[BrokerOrder]:
    """Saved order pages as broker history, only once they are proven pinned and complete."""
    payload = _read_json(path, missing="orders_pages_missing", corrupt="orders_pages_corrupt")
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
    history: list[BrokerOrder] = []
    try:
        for page in pages:
            for row in _order_rows(page):
                order_id = str(row.get("id")) if isinstance(row, dict) else ""
                if order_id and order_id in seen:
                    continue  # an order repeated across pages must not count twice
                seen.add(order_id)
                history.append(_to_broker_order(row))
    except (ValueError, TypeError) as exc:
        raise _Refused(PlanError("history_unusable", type(exc).__name__)) from exc
    return history


def _load_fresh_quotes(path: Path) -> dict[str, float]:
    data = _read_json(path, missing="fresh_quotes_missing", corrupt="fresh_quotes_corrupt")
    ok = isinstance(data, dict) and all(
        isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
        for k, v in data.items()
    )
    if not ok:
        raise _Refused(PlanError("fresh_quotes_corrupt", path.name))
    return {k.upper(): float(v) for k, v in data.items()}


def _load_fresh_spreads(path: Path) -> dict[str, tuple[Any, Any] | None]:
    """``{"SYMBOL": {"bid": x, "ask": y}}`` as ``(bid, ask)``; a null entry means no usable quote for it."""
    data = _read_json(path, missing="fresh_spreads_missing", corrupt="fresh_spreads_corrupt")

    def _num(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    ok = isinstance(data, dict) and all(
        isinstance(k, str)
        and (v is None or (isinstance(v, dict) and _num(v.get("bid")) and _num(v.get("ask"))))
        for k, v in data.items()
    )
    if not ok:
        raise _Refused(PlanError("fresh_spreads_corrupt", path.name))
    return {k.upper(): None if v is None else (v["bid"], v["ask"]) for k, v in data.items()}


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


def _cmd_plan(args: argparse.Namespace, trading_day: date) -> int:
    config, ladder_version, _ = _context(args.config_dir)
    path = args.plan_file if args.plan_file is not None else dop.order_plan_path(trading_day, args.slot)
    result = dop.load_plan(path, **_plan_context(config, ladder_version, trading_day, args.slot))
    if result.errors:
        return _emit({"status": "NO_VALID_PLAN", "plan": None, "errors": _error_dicts(result.errors)}, 2)
    return _emit({"status": "VALID_PLAN", "plan": result.plan, "errors": []}, 0)


def _cmd_compute(args: argparse.Namespace, trading_day: date) -> int:
    if args.market_data is not None:
        return _cmd_compute_gated(args, trading_day)
    config, ladder_version, _ = _context(args.config_dir)
    now = _parse_now(args.now)
    if now is None:
        raise _Refused(PlanError("bad_now", "expected a timezone-aware ISO timestamp"))
    buying_power = _parse_buying_power(args.buying_power)
    history = _load_history(args.orders_pages, trading_day)
    result = dop.compute_plan_from_file(
        args.quotes, config=config, trading_day=trading_day, slot=args.slot, generated_at_et=now,
        ladder_version=ladder_version, orders_today=history, tier=_NORMAL_TIER, buying_power_usd=buying_power,
    )
    if result.errors or result.plan is None:
        return _emit({"status": "FAILED", "plan": None, "errors": _error_dicts(result.errors)}, 2)
    return _emit({"status": "VALID_PLAN", "plan": result.plan, "errors": []}, 0)


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
    history = _load_history(args.orders_pages, trading_day)
    deployed = _parse_deployed(args.deployed_last_7d_usd, history, now)
    payload = _load_market_data(args.market_data)
    directory = args.config_dir if args.config_dir is not None else dca_config.get_config_dir()
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
    result = dop.plan_from_decisions(
        decisions, config=config, trading_day=trading_day, slot=args.slot, generated_at_et=now,
        ladder_version=dop.current_ladder_version(directory), orders_today=history, tier=_NORMAL_TIER,
        buying_power_usd=buying_power,
    )
    if result.errors or result.plan is None:
        return _emit({"status": "FAILED", "plan": None, "errors": _error_dicts(result.errors)}, 2)
    return _emit({"status": "VALID_PLAN", "plan": result.plan, "errors": []}, 0)


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


def _cmd_finalize(args: argparse.Namespace, trading_day: date) -> int:
    config, ladder_version, _ = _context(args.config_dir)
    plan = dop.load_plan(args.plan, **_plan_context(config, ladder_version, trading_day, args.slot))
    errors: list[PlanError] = list(plan.errors)
    history = _collect(errors, lambda: _load_history(args.orders_pages, trading_day))
    fresh = _collect(errors, lambda: _load_fresh_quotes(args.fresh_quotes))
    spreads = _collect(errors, lambda: _load_fresh_spreads(args.fresh_spreads))
    buying_power = _collect(errors, lambda: _parse_buying_power(args.buying_power))
    if errors:
        return _abort(*errors)
    if plan.plan["generator"] != "claude":
        guard = _kill_switch_error(plan.orders, history, config, trading_day, args.now)
        if guard is not None:
            return _abort(guard)
    # A Claude plan already carries any top-up resize, so only THIS slot's window counts as already
    # placed. A computed plan cannot resize, so it keeps the conservative day-wide exclusion.
    scope = "slot" if plan.plan["generator"] == "claude" else "day"
    remaining = dop.reconcile(plan.orders, history, trading_day=trading_day, slot=args.slot, scope=scope)
    remaining_symbols = {o["symbol"] for o in remaining}
    skipped = [{"symbol": o["symbol"], "reason": "already_placed"} for o in plan.orders if o["symbol"] not in remaining_symbols]
    result = dop.revalidate_for_placement(
        remaining, broker_orders=history, fresh_quotes=fresh, trading_day=trading_day, tier=_NORMAL_TIER,
        config=config, slot=args.slot, scope=scope, buying_power_usd=buying_power, fresh_spreads=spreads,
    )
    if result.errors:
        return _abort(*result.errors)
    skipped += [{"symbol": symbol, "reason": reason} for symbol, reason in result.skipped]
    orders = [{"symbol": o["symbol"], "notional_usd": o["notional_usd"], "ref_id": o["ref_id"]} for o in result.orders]
    mac_out_of_the_picture = args.local_status in BACKUP_MAY_ACT_LOCAL_STATUSES
    if plan.plan["generator"] != "claude" and not (COMPUTE_PLACEMENT_ENABLED and mac_out_of_the_picture):
        return _emit({"status": "SHADOW_ONLY", "to_place": [], "would_place": orders, "skipped": skipped, "errors": []}, 0)
    return _emit({"status": "PROCEED", "to_place": orders, "skipped": skipped, "errors": []}, 0)


def _kill_switch_error(orders: Any, history: list[BrokerOrder], config: Any, trading_day: date,
                       now_text: str | None) -> PlanError | None:
    """Poll 2's second look at the trailing-7-day guard for a computed plan, else ``None`` when it still holds.

    A computed plan was sized from poll 1's figure; spend that lands between the polls can trip the kill switch
    while the plan still carries a multiplier above 1x, so the figure is re-derived from the fresh pull and an
    over-1x plan is refused (place nothing; the plan is stale). ``now`` must be on the trading day in ET: it is
    the window's upper bound, and an earlier day would classify today's orders as future and leave the guard off.
    A Claude plan is sized by the primary and is not rechecked here: its own earlier fills would count toward the
    figure and wrongly abort a partly finished batch.
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
    if kill_switch_engaged(dataclasses.replace(config, deployed_last_7d_usd=deployed)) and any(o["multiplier"] > 1.0 for o in orders):
        return PlanError("kill_switch_engaged_since_plan", "trailing seven-day spend now exceeds the kill-switch threshold")
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD, America/New_York.")
        p.add_argument("--slot", required=True, help="Slot id: one of " + ", ".join(SLOTS))
        p.add_argument("--config-dir", type=Path, default=None)

    plan = sub.add_parser("plan", help="Read and validate the Mac's plan file.")
    common(plan)
    plan.add_argument("--source", required=True, choices=("mac_plan",))
    plan.add_argument("--plan-file", type=Path, default=None)
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
    finalize.add_argument("--local-status", default=None, choices=LOCAL_STATUSES,
                          help="Step 3's local_status. A computed plan places only for off, errored or unreachable.")
    finalize.add_argument("--now", default=None,
                          help="ET-day ISO timestamp for poll 2's kill-switch recheck of a computed plan (default: the clock).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
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
    failed = _abort if args.command == "finalize" else (lambda *e: _emit({"status": "FAILED", "plan": None, "errors": _error_dicts(e)}, 2))
    if not _DAY_RE.match(args.trading_day):
        return failed(PlanError("bad_trading_day", "expected YYYY-MM-DD"))
    try:
        trading_day = date.fromisoformat(args.trading_day)
    except ValueError:
        return failed(PlanError("bad_trading_day", "not a calendar date"))
    if args.slot not in SLOTS:
        return failed(PlanError("unknown_slot", args.slot))
    handler = {"plan": _cmd_plan, "compute": _cmd_compute, "finalize": _cmd_finalize}[args.command]
    try:
        return handler(args, trading_day)
    except _Refused as refusal:
        return failed(*refusal.errors)
    except Exception as exc:  # noqa: BLE001 - the last fail-closed boundary: the prompt places nothing on exit 2
        # Type name only: a message can carry paths, config contents, or account data.
        return failed(PlanError("unexpected_error", type(exc).__name__))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
