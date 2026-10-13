#!/usr/bin/env python3
"""Validate an operator's ad hoc buy list and, optionally, submit it through an injected callable.

WHAT THIS IS FOR
----------------
When the normal scheduled path is unavailable (agent permission gate, a dead slot, a half-finished
batch), the operator still needs to place a known list of (ticker, usd) buys safely. This module
turns that list into a checked order sheet and a manual-run artifact; it never talks to a broker.

DESIGN RULES (all pinned by ``tests/test_adhoc_orders.py``)
-----------------------------------------------------------
* All-or-nothing validation: any problem yields an empty plan plus structured errors, never a
  partial buy list. Every problem is reported, not just the first.
* The universe comes from ``config/tickers.json``; excluded symbols (QBUT) are always refused.
* Per-order, daily and weekly caps, the account-wide daily attempt cap and available buying power
  (``docs/safeguards.md``) are enforced, in whole cents, against figures the caller supplies.
  Nothing here reads broker history, so the caller owns their accuracy; the CLI therefore refuses to
  run without them rather than defaulting to zero. Every figure and cap is validated, so a NaN or
  bool cannot silently disable a ceiling.
* A broker response counts as submitted only if it positively confirms an order; anything else
  halts the batch. Exception text from the broker is never stored, only its type, because connector
  errors can echo account data.
* The CLI is dry-run only (``CLAUDE.md`` invariant). Placement exists only as :func:`place_adhoc`,
  which needs an injected callable AND ``confirm=True``, matching ``dca_live_adapters``.
* Output is a manual run: it lands under ``manual_runs/<timestamp>/``, never writes a ``latest_*``
  pointer, and never overwrites an earlier run (``docs/run_classification.md``). The plan is written
  before placement; :func:`write_execution_state` records the outcome and broker order ids after,
  so manual orders can be excluded from scheduled metrics. The account number is never written.

Usage::

    python -m scripts.adhoc_orders --orders orders.csv --trading-day 2026-10-13 \\
        --spent-today 40.00 --spent-week 310.00 --attempts-today 12 --buying-power 500.00

Exit codes: 0 plan valid and written, 2 invalid input or plan (nothing written).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = 1
PLAN_FILENAME = "adhoc_order_plan.json"
EXECUTION_STATE_FILENAME = "adhoc_execution_state.json"
_REJECTED_STATES = frozenset({"rejected", "cancelled", "canceled", "failed"})
_TIMESTAMP_RE = re.compile(r"^[0-9A-Za-z_-]+$")

# Same resolution order as dca_config: $DCA_CONFIG_DIR, else config/ beside the modules.
_CONFIG_DIR = Path(os.environ.get("DCA_CONFIG_DIR") or Path(__file__).resolve().parent.parent / "config")
TICKERS_CONFIG_PATH = _CONFIG_DIR / "tickers.json"
BUDGETS_CONFIG_PATH = _CONFIG_DIR / "budgets.json"


class AdhocInputError(ValueError):
    """Raised for unusable input files and for attempts to use an invalid plan."""


@dataclass(frozen=True)
class Policy:
    """The limits a plan is checked against."""

    universe: frozenset
    excluded: frozenset
    daily_cap_usd: float
    weekly_cap_usd: float
    max_per_order_usd: float
    max_orders_per_day: int


@dataclass
class AdhocPlan:
    """Result of :func:`build_plan`. ``orders`` is empty whenever ``ok`` is False."""

    trading_day: str
    ok: bool
    orders: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(_to_cents(o["usd"]) for o in self.orders) / 100


@dataclass
class PlacementResult:
    """Outcome of :func:`place_adhoc`."""

    dry_run: bool
    submitted: list = field(default_factory=list)
    failed: dict | None = None
    not_attempted: list = field(default_factory=list)


def load_policy() -> Policy:
    """Build the policy from repo config and constants (Normal budget tier caps)."""
    # Lazy imports: these pull in the 40k-line strategy core, which the validator itself never needs.
    import dca_runner
    import near_close_dca

    with open(TICKERS_CONFIG_PATH, encoding="utf-8") as handle:
        tickers = json.load(handle)
    with open(BUDGETS_CONFIG_PATH, encoding="utf-8") as handle:
        breakers = json.load(handle)["circuit_breakers"]
    normal = near_close_dca.BUDGET_TIERS[0]
    return Policy(
        universe=frozenset(tickers["active"]),
        excluded=frozenset(tickers["excluded"]),
        daily_cap_usd=normal.daily_cap_usd,
        weekly_cap_usd=normal.weekly_cap_usd,
        max_per_order_usd=dca_runner.MAX_PER_ORDER_USD,
        max_orders_per_day=breakers["max_orders_per_day"],
    )


def _is_number(value: Any) -> bool:
    # bool is an int subclass; True must not slip through as $1.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    if not _is_number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float is not a usable figure
        return False


def _to_cents(usd: float) -> int:
    # Decimal(str()) keeps 0.1 + 0.2 style drift out of cap comparisons.
    return int((Decimal(str(usd)) * 100).to_integral_value(ROUND_HALF_UP))


def _is_whole_cents(usd: float) -> bool:
    return Decimal(str(usd)) == Decimal(str(usd)).quantize(Decimal("0.01"))


def _error(code: str, message: str, ticker: str | None = None) -> dict:
    return {"code": code, "ticker": ticker, "message": message}


def _check_figure(label: str, value: Any, errors: list, *, code: str, positive: bool = False, integer: bool = False) -> bool:
    """Append an error unless ``value`` is a finite number >= 0 (> 0 when ``positive``)."""
    valid = _is_finite_number(value) and (value > 0 if positive else value >= 0)
    if valid and integer:
        valid = isinstance(value, int)
    if not valid:
        errors.append(_error(code, f"{label} must be a finite {'positive ' if positive else ''}{'integer' if integer else 'number'}, got {value!r}"))
    return valid


def build_plan(
    orders: Iterable[Sequence[Any]],
    *,
    trading_day: str,
    universe: frozenset,
    excluded: frozenset,
    spent_today_usd: float,
    spent_week_usd: float,
    daily_cap_usd: float,
    weekly_cap_usd: float,
    max_per_order_usd: float,
    attempts_today: int,
    max_orders_per_day: int,
    buying_power_usd: float,
) -> AdhocPlan:
    """Validate ``orders`` (``(ticker, usd)`` pairs) against the limits, all-or-nothing.

    Returns an :class:`AdhocPlan`. On any problem ``ok`` is False, ``orders`` is empty and
    ``errors`` lists every problem found. Raises :class:`AdhocInputError` only for a malformed
    ``trading_day``, because that would corrupt the idempotency key.
    """
    try:
        date.fromisoformat(trading_day)
    except (TypeError, ValueError) as exc:
        raise AdhocInputError(f"trading_day must be YYYY-MM-DD, got {trading_day!r}") from exc

    errors: list = []
    rows = list(orders)
    if not rows:
        errors.append(_error("empty_order_list", "no orders supplied"))

    figures_ok = all(
        [
            _check_figure("spent_today_usd", spent_today_usd, errors, code="invalid_spend"),
            _check_figure("spent_week_usd", spent_week_usd, errors, code="invalid_spend"),
            _check_figure("attempts_today", attempts_today, errors, code="invalid_figure", integer=True),
            _check_figure("buying_power_usd", buying_power_usd, errors, code="invalid_figure"),
            _check_figure("daily_cap_usd", daily_cap_usd, errors, code="invalid_limit", positive=True),
            _check_figure("weekly_cap_usd", weekly_cap_usd, errors, code="invalid_limit", positive=True),
            _check_figure("max_per_order_usd", max_per_order_usd, errors, code="invalid_limit", positive=True),
            _check_figure("max_orders_per_day", max_orders_per_day, errors, code="invalid_limit", positive=True, integer=True),
        ]
    )

    seen: set = set()
    accepted: list = []
    max_cents = _to_cents(max_per_order_usd) if _is_finite_number(max_per_order_usd) else 0
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            errors.append(_error("invalid_row", f"expected (ticker, usd), got {row!r}"))
            continue
        raw_ticker, usd = row
        ticker = raw_ticker.strip().upper() if isinstance(raw_ticker, str) else ""
        row_ok = True

        if ticker in excluded:
            errors.append(_error("excluded_ticker", f"{ticker} is on the do-not-buy list", ticker))
            row_ok = False
        elif ticker not in universe:
            errors.append(_error("unknown_ticker", f"{raw_ticker!r} is not in the active universe", ticker or None))
            row_ok = False

        if ticker and ticker in seen:
            errors.append(_error("duplicate_ticker", f"{ticker} appears more than once", ticker))
            row_ok = False
        seen.add(ticker)

        if not _is_finite_number(usd) or usd <= 0 or not _is_whole_cents(usd):
            errors.append(_error("invalid_amount", f"amount must be a positive whole-cent number, got {usd!r}", ticker or None))
            row_ok = False
        elif figures_ok and _to_cents(usd) > max_cents:
            errors.append(_error("over_per_order_cap", f"{usd} exceeds the {max_per_order_usd} per-order cap", ticker))
            row_ok = False

        if row_ok:
            accepted.append({"ticker": ticker, "usd": _to_cents(usd) / 100})

    total_cents = sum(_to_cents(o["usd"]) for o in accepted)
    if figures_ok:
        if len(accepted) + attempts_today > max_orders_per_day:
            errors.append(_error("over_attempt_cap", f"{len(accepted)} orders plus {attempts_today} attempts today exceeds the {max_orders_per_day} daily attempt cap"))
        if total_cents > _to_cents(buying_power_usd):
            errors.append(_error("over_buying_power", f"batch total exceeds the {buying_power_usd} available buying power"))
        if _to_cents(spent_today_usd) + total_cents > _to_cents(daily_cap_usd):
            errors.append(_error("over_daily_cap", f"batch plus {spent_today_usd} already spent exceeds the {daily_cap_usd} daily cap"))
        if _to_cents(spent_week_usd) + total_cents > _to_cents(weekly_cap_usd):
            errors.append(_error("over_weekly_cap", f"batch plus {spent_week_usd} already spent exceeds the {weekly_cap_usd} weekly cap"))

    if errors:
        return AdhocPlan(trading_day=trading_day, ok=False, orders=[], errors=errors)
    return AdhocPlan(trading_day=trading_day, ok=True, orders=accepted, errors=[])


def _reject_duplicate_keys(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AdhocInputError("duplicate key in JSON object")
    return dict(pairs)


def _parse_amount(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise AdhocInputError(f"amount is not a number: {text!r}") from exc
    if not math.isfinite(value):
        raise AdhocInputError(f"amount is not finite: {text!r}")
    return value


def parse_orders(path: str | Path) -> list:
    """Read ``(ticker, usd)`` pairs from a ``.csv`` (header ``ticker,usd``) or ``.json`` file.

    Fails closed with :class:`AdhocInputError` on malformed rows, non-finite amounts, duplicate
    JSON keys, or JSON that is not a list of objects.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise AdhocInputError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise AdhocInputError("JSON input must be a list of {ticker, usd} objects")
        parsed = []
        for item in data:
            if not isinstance(item, dict) or set(item) != {"ticker", "usd"}:
                raise AdhocInputError(f"each JSON row must be exactly {{ticker, usd}}, got {item!r}")
            if not _is_number(item["usd"]) or not math.isfinite(item["usd"]):
                raise AdhocInputError(f"usd must be a finite number, got {item['usd']!r}")
            parsed.append((item["ticker"], float(item["usd"])))
        return parsed
    if suffix == ".csv":
        rows = [r for r in csv.reader(text.splitlines()) if r]
        if not rows or [c.strip().lower() for c in rows[0]] != ["ticker", "usd"]:
            raise AdhocInputError("CSV header must be exactly: ticker,usd")
        parsed = []
        for row in rows[1:]:
            if len(row) != 2:
                raise AdhocInputError(f"CSV row must have exactly 2 fields, got {row!r}")
            parsed.append((row[0], _parse_amount(row[1].strip())))
        return parsed
    raise AdhocInputError(f"unsupported input type {suffix!r}; use .csv or .json")


def _confirmed_order_id(response: Any, ticker: str, key: str) -> str | None:
    """Return the broker order id only if ``response`` positively confirms this order, else None.

    A tool can return normally yet not have placed anything (None, an error payload, a rejected or
    cancelled state, a response for another symbol or idempotency key), so "did not raise" is not
    evidence of a submission.
    """
    if not isinstance(response, dict):
        return None
    order_id = response.get("id")
    if not isinstance(order_id, str) or not order_id.strip():
        return None
    if response.get("success") is False:
        return None
    state = response.get("state")
    if isinstance(state, str) and state.strip().lower() in _REJECTED_STATES:
        return None
    for field_name in ("symbol", "ticker"):
        if field_name in response and response[field_name] != ticker:
            return None
    for field_name in ("ref_id", "idempotency_key"):
        if field_name in response and response[field_name] != key:
            return None
    return order_id


def place_adhoc(plan: AdhocPlan, place_order: Callable[..., Any], *, confirm: bool = False) -> PlacementResult:
    """Submit a valid plan through ``place_order``, one order at a time.

    Dry-run unless ``confirm=True``. Stops at the first failure and never retries (a retry could
    double-buy); the remainder is reported as ``not_attempted``. ``place_order`` is called as
    ``place_order(ticker=, usd=, idempotency_key=)`` and should map onto the Robinhood tool. A raised
    exception is recorded by type only. A response that does not confirm an order is recorded as
    ``unconfirmed_response``: the order MAY still exist, so check broker history before retrying.
    """
    if not plan.ok:
        raise AdhocInputError("refusing to place an invalid plan")
    if not confirm:
        return PlacementResult(dry_run=True, not_attempted=list(plan.orders))

    result = PlacementResult(dry_run=False)
    for index, order in enumerate(plan.orders):
        key = f"dca-adhoc-{plan.trading_day}-{order['ticker']}"
        try:
            response = place_order(ticker=order["ticker"], usd=order["usd"], idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 - any broker/tool failure must halt the batch, not escape it
            # Type only: connector exceptions can echo account identifiers or credentials.
            result.failed = {"ticker": order["ticker"], "usd": order["usd"], "error": type(exc).__name__}
            result.not_attempted = list(plan.orders[index + 1 :])
            return result
        order_id = _confirmed_order_id(response, order["ticker"], key)
        if order_id is None:
            result.failed = {"ticker": order["ticker"], "usd": order["usd"], "error": "unconfirmed_response"}
            result.not_attempted = list(plan.orders[index + 1 :])
            return result
        result.submitted.append({**order, "order_id": order_id})
    return result


def write_plan(plan: AdhocPlan, *, root: str | Path, timestamp: str) -> Path:
    """Write the plan under ``<root>/manual_runs/<timestamp>/``; no pointer file is touched."""
    if not plan.ok:
        raise AdhocInputError("refusing to write an invalid plan")
    if not _TIMESTAMP_RE.match(timestamp):
        raise AdhocInputError(f"timestamp may only contain letters, digits, '_' and '-': {timestamp!r}")
    run_dir = Path(root) / "manual_runs" / timestamp
    try:
        # exist_ok=False: a second invocation in the same second must not overwrite the first run.
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise AdhocInputError(f"run directory already exists, refusing to overwrite: {run_dir}") from exc
    document = {
        "schema_version": SCHEMA_VERSION,
        "run_origin": "manual",
        "manual_run": True,
        "trading_day": plan.trading_day,
        "total_usd": plan.total_usd,
        "orders": plan.orders,
    }
    target = run_dir / PLAN_FILENAME
    scratch = target.with_suffix(".tmp")
    scratch.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(scratch, target)  # atomic: a reader never sees a half-written plan
    return target


def write_execution_state(plan: AdhocPlan, result: PlacementResult, *, root: str | Path, timestamp: str) -> Path:
    """Record what :func:`place_adhoc` did, beside the plan, so manual orders stay traceable.

    The pre-placement plan is never rewritten: ``adhoc_order_plan.json`` says what was intended,
    ``adhoc_execution_state.json`` says what the broker confirmed. Order ids let reporting exclude
    these manual orders from scheduled-live metrics. Refuses to overwrite an earlier state file.
    """
    run_dir = Path(root) / "manual_runs" / timestamp
    if not _TIMESTAMP_RE.match(timestamp) or not (run_dir / PLAN_FILENAME).is_file():
        raise AdhocInputError(f"no written plan to attach execution state to under {run_dir}")
    status = "dry_run" if result.dry_run else ("halted" if result.failed else "completed")
    document = {
        "schema_version": SCHEMA_VERSION,
        "run_origin": "manual",
        "manual_run": True,
        "trading_day": plan.trading_day,
        "status": status,
        "submitted": result.submitted,
        "failed": result.failed,
        "not_attempted": result.not_attempted,
    }
    target = run_dir / EXECUTION_STATE_FILENAME
    scratch = target.with_suffix(".tmp")
    scratch.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    try:
        os.link(scratch, target)  # atomic and fails if the file exists, unlike os.replace
    except FileExistsError as exc:
        raise AdhocInputError(f"execution state already recorded, refusing to overwrite: {target}") from exc
    finally:
        scratch.unlink(missing_ok=True)
    return target


def _format_sheet(plan: AdhocPlan) -> str:
    lines = [f"Ad hoc order sheet for {plan.trading_day} (dry run, nothing placed)", "ticker      usd"]
    lines += [f"{o['ticker']:<8} {o['usd']:>8.2f}" for o in plan.orders]
    lines.append(f"{len(plan.orders)} orders, total ${plan.total_usd:.2f}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, policy: Policy | None = None) -> int:
    """CLI entry point. Validates and writes a plan; has no way to place orders."""
    parser = argparse.ArgumentParser(description="Validate an ad hoc buy list (dry run only).")
    parser.add_argument("--orders", required=True, help="CSV (ticker,usd) or JSON list of {ticker, usd}")
    parser.add_argument("--trading-day", required=True, help="YYYY-MM-DD")
    # Required, no default: a silent 0 would let a sheet look valid right after a heavy spending day.
    # Take all four from a fresh, fully paginated get_equity_orders pull and get_accounts.
    parser.add_argument("--spent-today", type=float, required=True, help="USD already bought today")
    parser.add_argument("--spent-week", type=float, required=True, help="USD already bought this week")
    parser.add_argument("--attempts-today", type=int, required=True, help="buy attempts today, account-wide, incl. rejected")
    parser.add_argument("--buying-power", type=float, required=True, help="available buying power in USD")
    parser.add_argument("--out-root", default=None, help="artifact root (default: the cron tracking root)")
    parser.add_argument("--timestamp", default=None, help="run directory name (default: current UTC time)")
    args = parser.parse_args(argv)

    policy = policy or load_policy()
    try:
        orders = parse_orders(args.orders)
        plan = build_plan(
            orders,
            trading_day=args.trading_day,
            universe=policy.universe,
            excluded=policy.excluded,
            spent_today_usd=args.spent_today,
            spent_week_usd=args.spent_week,
            daily_cap_usd=policy.daily_cap_usd,
            weekly_cap_usd=policy.weekly_cap_usd,
            max_per_order_usd=policy.max_per_order_usd,
            attempts_today=args.attempts_today,
            max_orders_per_day=policy.max_orders_per_day,
            buying_power_usd=args.buying_power,
        )
        if not plan.ok:
            print("PLAN INVALID, nothing written:")
            for err in plan.errors:
                print(f"  {err['code']}: {err['message']}")
            return 2
        root = args.out_root
        if root is None:
            import dca_paths

            root = dca_paths.cron_tracking_root()
        timestamp = args.timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = write_plan(plan, root=root, timestamp=timestamp)
    except (AdhocInputError, OSError) as exc:
        print(f"INPUT ERROR: {exc}")
        return 2

    print(_format_sheet(plan))
    print(f"plan written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
