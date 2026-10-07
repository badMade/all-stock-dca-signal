#!/usr/bin/env python3
"""The planner the Perplexity backup prompts call: read a plan, compute one, or finalize one.

Three subcommands, each printing ONE JSON document and exiting 0 when the result is usable and
2 on anything else, in which case the prompt places nothing (the contract is the table in
``claude/dca-executor-prompts/perplexity/README.md``)::

    plan      --source mac_plan --trading-day D --slot S [--plan-file F]
    compute   --trading-day D --slot S --quotes F --orders-pages F --buying-power USD [--now T]
    finalize  --trading-day D --slot S --plan F --orders-pages F --fresh-quotes F
              --fresh-spreads F --buying-power USD

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
import json
import math
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import dca_config
import dca_order_plan as dop
from dca_order_plan import PlanError
from dca_runner import ET, MAX_PER_ORDER_USD, SLOTS, BrokerOrder
from near_close_dca import BUDGET_TIERS
from scripts.assemble_order_pages import _order_rows
from scripts.broker_reconciliation import _has_continuation, _unwrap
from scripts.same_day_top_up_gate import _bind_saved_pages

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NORMAL_TIER = next(tier for tier in BUDGET_TIERS if tier.name == "normal")
_DEAD_STATES = frozenset({"cancelled", "canceled", "rejected", "failed", "voided"})

# Off until the compute path is rebuilt on scripts/stream_candidates.py (ADR "Open design gaps", gap 2).
COMPUTE_PLACEMENT_ENABLED = False


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
    if plan.plan["generator"] != "claude" and not COMPUTE_PLACEMENT_ENABLED:
        return _emit({"status": "SHADOW_ONLY", "to_place": [], "would_place": orders, "skipped": skipped, "errors": []}, 0)
    return _emit({"status": "PROCEED", "to_place": orders, "skipped": skipped, "errors": []}, 0)


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
    compute = sub.add_parser("compute", help="Derive a plan from fetched quotes.")
    common(compute)
    compute.add_argument("--quotes", required=True, type=Path)
    compute.add_argument("--orders-pages", required=True, type=Path)
    compute.add_argument("--now", default=None)
    compute.add_argument("--buying-power", required=True, help="Fresh available buying power in USD.")
    finalize = sub.add_parser("finalize", help="Reconcile and re-validate a plan; print what to place.")
    common(finalize)
    finalize.add_argument("--plan", required=True, type=Path)
    finalize.add_argument("--orders-pages", required=True, type=Path)
    finalize.add_argument("--fresh-quotes", required=True, type=Path)
    finalize.add_argument("--fresh-spreads", required=True, type=Path,
                          help='JSON {"SYMBOL": {"bid": x, "ask": y}} fetched just before placement.')
    finalize.add_argument("--buying-power", required=True, help="Fresh available buying power in USD.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
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
