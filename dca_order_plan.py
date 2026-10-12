"""Plan-first backup executor: order-plan files, validation, reconciliation, compute fallback.

See ``docs/adr-plan-first-backup-executor.md`` and ``schemas/order_plan.schema.json``.

A *plan* is one slot's intended buys for one trading day. The primary Claude slot runner
writes it before placing; the backup executor reads it (Mac path) or re-derives it from
fetched quotes (compute paths). Three rules shape every function here:

* **A plan is input, never evidence.** Only broker order history says what was bought.
  ``reconcile`` and ``revalidate_for_placement`` take broker history as an argument and
  nothing in a plan can mark an order as placed.
* **Fail closed.** Bad input yields an empty plan plus structured ``PlanError`` values, never
  a partial buy. One defect rejects the WHOLE plan (D4 "literal"), and a plan that would
  breach a cap buys nothing rather than a trimmed batch.
* **Reuse the repo's own sizing.** Multipliers come from ``near_close_dca.graded_buy_multiplier``
  and the $1/$2 base from ``near_close_dca.select_first_allocation_base_usd``. Placement itself
  is still ``dca_runner.execute_plan``'s job, which re-runs ``plan_orders`` on whatever this
  module hands over; nothing here places an order.

Standard library plus the repo's own core modules only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import dca_config
import dca_paths
from dca_fileio import atomic_write_text
from dca_runner import (
    ET,
    MAX_ORDERS_PER_DAY,
    MAX_PER_ORDER_USD,
    SLOTS,
    BrokerOrder,
    BudgetDataError,
    BudgetSnapshot,
    _committed_dollar_amount,
    budget_snapshot,
)
from near_close_dca import (
    BudgetTier,
    StrategyConfig,
    _quote_bid_ask,
    graded_buy_multiplier,
    select_first_allocation_base_usd,
)

__all__ = [
    "MAC_PATH_BUDGET_S",
    "MAC_REACHABILITY_TIMEOUT_S",
    "LATEST_PLACEMENT_ET",
    "SCHEMA_VERSION",
    "PlanError",
    "PlanResult",
    "canonical_plan_json",
    "choose_plan_path",
    "compute_plan",
    "compute_plan_from_file",
    "current_ladder_version",
    "load_plan",
    "order_plan_path",
    "placement_window",
    "plan_from_decisions",
    "plan_from_survivors",
    "already_placed_symbols",
    "trailing_deployed_usd",
    "reconcile",
    "revalidate_for_placement",
    "universe_hash",
    "validate_plan",
    "write_plan",
]

SCHEMA_VERSION = 1
# D8: a hung device call must not eat the buy window.
MAC_REACHABILITY_TIMEOUT_S = 60
MAC_PATH_BUDGET_S = 180

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_GENERATORS = frozenset({"claude", "backup"})
_PLAN_KEYS = frozenset(
    {"schema_version", "trading_day", "slot", "generated_at_et", "generator", "ladder_version",
     "universe_hash", "orders"}
)
_ORDER_KEYS = frozenset(
    {"symbol", "notional_usd", "multiplier", "drop_pct", "decision_price", "prior_close", "prior_low",
     "ref_id"}
)


def _latest_placement_table() -> dict[str, str]:
    """Latest ET placement time per slot: the NEXT slot's nominal start, and the 16:00 close for the last.

    A backup that fires inside a slot's window may still finish before the next canonical slot's own
    gates run, so that slot sees the backup's buys. Derived from ``dca_runner.SLOTS``, never retyped.
    """
    slot_ids = sorted(SLOTS, key=lambda sid: SLOTS[sid].nominal_et)
    table: dict[str, str] = {}
    for index, slot_id in enumerate(slot_ids):
        hour, minute = SLOTS[slot_ids[index + 1]].nominal_et if index + 1 < len(slot_ids) else (16, 0)
        table[slot_id] = f"{hour:02d}:{minute:02d}"
    return table


LATEST_PLACEMENT_ET = _latest_placement_table()


def placement_window(slot: str, trading_day: date) -> tuple[datetime, datetime]:
    """The slot's ``[start, end)`` ET placement window on ``trading_day``.

    Start is the slot's nominal time and end is :data:`LATEST_PLACEMENT_ET`. Raises ``ValueError``
    for a slot that is not on the roster.
    """
    if slot not in SLOTS:
        raise ValueError(f"unknown slot {slot!r}")
    start_h, start_m = SLOTS[slot].nominal_et
    end_h, end_m = (int(part) for part in LATEST_PLACEMENT_ET[slot].split(":"))
    return (
        datetime.combine(trading_day, time(start_h, start_m), tzinfo=ET),
        datetime.combine(trading_day, time(end_h, end_m), tzinfo=ET),
    )


@dataclass(frozen=True)
class PlanError:
    """One structured reason a plan, input file, or order was refused.

    Attributes:
        code: Stable machine-readable reason, e.g. ``"wrong_slot"``.
        detail: Short human text. Never embeds raw input values that could be secrets.
    """

    code: str
    detail: str = ""


@dataclass(frozen=True)
class PlanResult:
    """Outcome of validating, loading, computing, or revalidating a plan.

    Attributes:
        plan: The full plan document when one was produced, else ``None``.
        orders: Orders that may proceed. Empty whenever ``errors`` is non-empty.
        errors: Whole-plan refusals. Any entry means no order may be placed.
        skipped: ``(symbol, reason)`` for individual orders or quotes that were left out.
    """

    plan: dict[str, Any] | None = None
    orders: tuple[dict[str, Any], ...] = ()
    errors: tuple[PlanError, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        """True when there is no whole-plan error."""
        return not self.errors


def _fail(*errors: PlanError) -> PlanResult:
    return PlanResult(errors=tuple(errors))


# ---------------------------------------------------------------------------
# Identity and serialization
# ---------------------------------------------------------------------------


def universe_hash(tickers: Iterable[str]) -> str:
    """sha256 over the sorted, de-duplicated tickers joined by newlines plus a trailing newline."""
    body = "\n".join(sorted(set(tickers))) + "\n"
    return hashlib.sha256(body.encode()).hexdigest()


def current_ladder_version(config_dir: str | Path | None = None) -> str:
    """Composite sizing version ``<sizing_ladder>+<per_ticker_sizing>``.

    Live sizing uses per-ticker calibration for every active ticker and the global ladder as
    the fallback, so a plan sized under either file going stale must reject.

    Args:
        config_dir: Directory holding the config JSON; defaults to ``dca_config.get_config_dir()``.

    Returns:
        The composite version string.

    Raises:
        ValueError: A file is missing, unparsable, or has no string ``version``. The caller
            must treat this as "cannot validate" and place nothing.
    """
    directory = Path(config_dir) if config_dir is not None else dca_config.get_config_dir()
    parts = []
    for name in ("sizing_ladder.json", "per_ticker_sizing.json"):
        data = _read_json_object(directory / name)
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"{name} has no string 'version'")
        parts.append(version)
    return "+".join(parts)


def canonical_plan_json(plan: Mapping[str, Any]) -> str:
    """Byte-deterministic JSON: sorted keys, compact separators, trailing newline."""
    return json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"


def write_plan(path: str | Path, plan: Mapping[str, Any]) -> None:
    """Atomically write ``plan`` in canonical form (temp file + rename).

    Side effects:
        Creates or replaces ``path``. Never leaves a temp file behind on failure.
    """
    atomic_write_text(path, canonical_plan_json(plan))


def _read_json_object(path: Path) -> dict[str, Any]:
    """Parse a JSON object, rejecting duplicate keys (a corrupt file must not load by last-wins)."""
    # dca_config's hook is private but is the repo's one duplicate-key guard; reuse over copy.
    data = json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_positive_finite(value: Any) -> bool:
    try:
        return _is_number(value) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _is_cent_amount(value: float) -> bool:
    return abs(value - round(value, 2)) <= 1e-9


def _expected_ref_id(slot: str, trading_day: date, symbol: str) -> str:
    # Same derivation as dca_runner.execute_plan (docs/live_wiring.md).
    return f"dca-runner-{slot}-{trading_day.isoformat()}-{symbol}"


def _order_errors(
    order: Any,
    *,
    slot: str,
    trading_day: date,
    active: Collection[str],
    excluded: Collection[str],
    max_per_order_usd: float,
) -> list[PlanError]:
    if not isinstance(order, dict) or set(order) != _ORDER_KEYS:
        return [PlanError("schema_violation", "order must be an object with exactly the schema's fields")]
    errors: list[PlanError] = []
    symbol = order["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol):
        return [PlanError("schema_violation", "order symbol is not a ticker")]
    if symbol in excluded:
        errors.append(PlanError("excluded_symbol", symbol))
    elif symbol not in active:
        errors.append(PlanError("unknown_symbol", symbol))
    notional = order["notional_usd"]
    if not _is_positive_finite(notional) or not _is_cent_amount(notional):
        errors.append(PlanError("invalid_notional", symbol))
    elif notional > max_per_order_usd:
        errors.append(PlanError("notional_over_cap", symbol))
    for field_name in ("multiplier", "drop_pct", "decision_price", "prior_close"):
        if not _is_positive_finite(order[field_name]):
            errors.append(PlanError("schema_violation", f"{symbol}.{field_name}"))
    if order["prior_low"] is not None and not _is_positive_finite(order["prior_low"]):
        errors.append(PlanError("schema_violation", f"{symbol}.prior_low"))
    if order["ref_id"] != _expected_ref_id(slot, trading_day, symbol):
        errors.append(PlanError("bad_ref_id", symbol))
    return errors


def validate_plan(
    plan: Any,
    *,
    trading_day: date,
    slot: str,
    ladder_version: str,
    active_tickers: Collection[str],
    excluded_tickers: Collection[str],
    max_per_order_usd: float = MAX_PER_ORDER_USD,
) -> PlanResult:
    """Validate a plan against the READER's own view of day, slot, sizing, universe, and cap.

    Any defect rejects the whole plan; a partly valid plan is never salvaged. Never raises.

    Args:
        plan: Decoded plan document (any type; non-objects are refused).
        trading_day: The ET trading date the reader is running for.
        slot: The slot id the reader is running (``"0935"`` .. ``"1530"``).
        ladder_version: The reader's current :func:`current_ladder_version`.
        active_tickers: The reader's active universe (``config/tickers.json`` ``active``).
        excluded_tickers: Do-not-buy symbols (QBUT).
        max_per_order_usd: Per-order ceiling from live config.

    Returns:
        ``PlanResult`` with ``orders`` set and no errors, or empty ``orders`` and errors.
    """
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        return _fail(PlanError("schema_violation", "plan must be an object with exactly the schema's fields"))
    errors: list[PlanError] = []
    if slot not in SLOTS:
        # The reader's own slot must be a real roster slot, or ref_ids derived from it mean nothing.
        errors.append(PlanError("unknown_slot", str(slot)))
    version = plan["schema_version"]
    if not _is_number(version) or version != SCHEMA_VERSION:
        errors.append(PlanError("schema_version_mismatch", f"expected {SCHEMA_VERSION}"))
    if plan["trading_day"] != trading_day.isoformat():
        errors.append(PlanError("wrong_trading_day", f"expected {trading_day.isoformat()}"))
    if plan["slot"] != slot:
        errors.append(PlanError("wrong_slot", f"expected {slot}"))
    if plan["generator"] not in _GENERATORS:
        errors.append(PlanError("schema_violation", "generator"))
    if not _aware_timestamp(plan["generated_at_et"]):
        errors.append(PlanError("schema_violation", "generated_at_et"))
    if plan["ladder_version"] != ladder_version:
        errors.append(PlanError("stale_ladder_version", f"expected {ladder_version}"))
    if plan["universe_hash"] != universe_hash(active_tickers):
        errors.append(PlanError("universe_hash_mismatch"))
    orders = plan["orders"]
    if not isinstance(orders, list):
        errors.append(PlanError("schema_violation", "orders must be a list"))
        return _fail(*errors)
    active, excluded = set(active_tickers), set(excluded_tickers)
    seen: set[str] = set()
    for order in orders:
        errors.extend(
            _order_errors(order, slot=slot, trading_day=trading_day, active=active, excluded=excluded,
                          max_per_order_usd=max_per_order_usd)
        )
        symbol = order.get("symbol") if isinstance(order, dict) else None
        if isinstance(symbol, str):
            if symbol in seen:
                errors.append(PlanError("duplicate_symbol", symbol))
            seen.add(symbol)
    if errors:
        return _fail(*errors)
    return PlanResult(plan=plan, orders=tuple(orders))


def _aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def load_plan(path: str | Path, **context: Any) -> PlanResult:
    """Read and validate a plan file. Missing or corrupt input yields an empty plan, never a raise.

    Args:
        path: The plan file.
        **context: Keyword arguments for :func:`validate_plan`.

    Returns:
        ``PlanResult``; ``plan_missing`` or ``plan_corrupt`` when the file cannot be used.
    """
    target = Path(path)
    if not target.exists():
        return _fail(PlanError("plan_missing", target.name))
    try:
        document = json.loads(target.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
    except (OSError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return _fail(PlanError("plan_corrupt", target.name))
    return validate_plan(document, **context)


# ---------------------------------------------------------------------------
# Reconciliation and placement-time re-validation
# ---------------------------------------------------------------------------


def _buy_attempts_on(broker_orders: Iterable[BrokerOrder], trading_day: date) -> list[BrokerOrder]:
    """Buy attempts created on ``trading_day`` (ET), dead ones included.

    A cancelled or rejected buy still consumed the ticker's one order for the day
    (``BrokerOrder.is_buy_attempt``), so it counts as placed here too.
    """
    return [o for o in broker_orders if o.is_buy_attempt and o.created_at.astimezone(ET).date() == trading_day]


def _placed_markers(
    attempts: Iterable[BrokerOrder],
    *,
    trading_day: date,
    slot: str | None,
    scope: str,
) -> tuple[set[str], set[str]]:
    """``(references, symbols)`` of buy attempts that count as "already placed".

    An exact reference always counts (it names this slot and day). For symbols, ``scope="day"``
    counts every attempt that day, the conservative rule for a plan that cannot resize a top-up.
    ``scope="slot"`` counts only attempts inside this slot's placement window, because
    ``config/budgets.json`` ``one_order_per_ticker_per_day`` is false: a later slot may buy a
    ticker again, resized by the same-day top-up guard, and a Claude plan already carries that
    resized notional.
    """
    if scope not in ("day", "slot"):
        raise ValueError(f"unknown scope {scope!r}")
    window = None
    if scope == "slot":
        if slot is None:
            raise ValueError("scope='slot' needs a slot")
        window = placement_window(slot, trading_day)
    refs: set[str] = set()
    symbols: set[str] = set()
    for attempt in attempts:
        refs.update(r for r in (attempt.client_order_id, attempt.ref_id) if r)
        if window is None or window[0] <= attempt.created_at.astimezone(ET) < window[1]:
            symbols.add(str(attempt.symbol).upper())
    return refs, symbols


def reconcile(
    orders: Sequence[Mapping[str, Any]],
    broker_orders: Iterable[BrokerOrder],
    *,
    trading_day: date,
    slot: str | None = None,
    scope: str = "day",
) -> list[dict[str, Any]]:
    """Return the planned orders the BROKER does not already show as placed (D2).

    Matched by ``ref_id`` first, then by symbol. ``scope`` decides which same-symbol attempts
    count (see :func:`_placed_markers`): ``"day"`` (default, conservative) or ``"slot"`` (this
    slot's window only, for a Claude plan). Order of ``orders`` is kept.

    Args:
        orders: Validated plan orders.
        broker_orders: A fresh, fully paginated order history. The only evidence of purchases.
        trading_day: The ET trading date.
        slot: Required when ``scope="slot"``.
        scope: ``"day"`` or ``"slot"``.

    Returns:
        The still-unplaced orders.

    Raises:
        ValueError: Unknown ``scope``, or ``scope="slot"`` without a roster ``slot``.
        AttributeError: A history row is not a ``BrokerOrder``. Treat as "cannot reconcile".
    """
    refs, symbols = _placed_markers(
        _buy_attempts_on(broker_orders, trading_day), trading_day=trading_day, slot=slot, scope=scope
    )
    return [dict(o) for o in orders if o["ref_id"] not in refs and str(o["symbol"]).upper() not in symbols]


def _buying_power_errors(buying_power_usd: Any, total: float) -> list[PlanError]:
    """Fail closed on an unusable buying power, or one below the batch total.

    ``None`` means the caller did not ask for the check (library use only; the planner CLI always does).
    """
    if buying_power_usd is None:
        return []
    if not _is_number(buying_power_usd) or not math.isfinite(buying_power_usd) or buying_power_usd < 0:
        return [PlanError("invalid_buying_power")]
    return [PlanError("insufficient_buying_power")] if total > buying_power_usd else []


def _cap_errors(
    snapshot: BudgetSnapshot,
    tier: BudgetTier,
    notionals: Sequence[float],
    weekly_cap_usd: float | None,
    buying_power_usd: Any = None,
) -> list[PlanError]:
    """Cap and buying-power breaches for adding ``notionals`` to ``snapshot``.

    ``dca_runner.plan_orders`` is not called here: it stops at a cap boundary but keeps the
    orders before it, whereas D4 is all-or-nothing, and its multiplier-consistency check is
    bound to the calibration files read at import time. The same limits are applied
    (``MAX_ORDERS_PER_DAY`` and the tier's daily and weekly caps, inclusive), and
    ``execute_plan`` re-runs ``plan_orders`` at placement regardless.
    """
    total = sum(notionals)
    errors = _buying_power_errors(buying_power_usd, total)
    if not notionals:
        # Nothing to add cannot breach a cap; an account already over a cap must not turn a
        # "nothing left to place" run into an abort.
        return errors
    if tier.uses_rollover_reserve and weekly_cap_usd is None:
        # The tier's nominal weekly cap overstates the real ceiling without a verified reserve.
        return [*errors, PlanError("weekly_cap_required", tier.name)]
    weekly_cap = tier.weekly_cap_usd if weekly_cap_usd is None else min(weekly_cap_usd, tier.weekly_cap_usd)
    if snapshot.day_order_count + len(notionals) > MAX_ORDERS_PER_DAY:
        errors.append(PlanError("over_order_count_cap"))
    if snapshot.day_notional_usd + total > tier.daily_cap_usd:
        errors.append(PlanError("over_daily_cap"))
    if snapshot.week_notional_usd + total > weekly_cap:
        errors.append(PlanError("over_weekly_cap"))
    return errors


class _FixedQuote:
    """One already-fetched bid/ask, shaped like the provider ``near_close_dca._quote_bid_ask`` expects."""

    def __init__(self, quote: Any) -> None:
        self._quote = quote

    def get_bid_ask(self, ticker: str, as_of: datetime) -> Any:
        return self._quote


def _spread_reason(quote: Any, max_spread_pct: float) -> str | None:
    """``None`` when the spread is proven within tolerance, else the skip reason (Gate 6.7).

    Validity comes from the repo's own ``near_close_dca._quote_bid_ask`` (both sides present, finite,
    positive, ask not below bid); a quote it rejects is ``spread_unavailable``. A spread above the
    threshold, as a percent of the mid price, is ``spread_not_ok``; exactly at it passes.
    """
    pair = _quote_bid_ask(_FixedQuote(quote), "", datetime.now(ET))
    if pair is None:
        return "spread_unavailable"
    bid, ask = pair
    spread_pct = (ask - bid) / ((bid + ask) / 2.0) * 100.0
    return "spread_not_ok" if spread_pct > max_spread_pct else None


def revalidate_for_placement(
    orders: Sequence[Mapping[str, Any]],
    *,
    broker_orders: Iterable[BrokerOrder],
    fresh_quotes: Mapping[str, float],
    trading_day: date,
    tier: BudgetTier,
    config: StrategyConfig,
    weekly_cap_usd: float | None = None,
    slot: str | None = None,
    scope: str = "day",
    buying_power_usd: Any = None,
    fresh_spreads: Mapping[str, Any] | None = None,
) -> PlanResult:
    """Re-check every order against a fresh order pull and fresh quotes, on every path.

    Per order: in the universe, not already bought (``scope``, see :func:`reconcile`), a fresh
    quote still below the prior close (D5: Claude's notional is kept, never re-sized), and, when
    ``fresh_spreads`` is given, a proven bid/ask spread within ``config.max_bid_ask_spread_pct``
    (the primaries' Gate 6.7 circuit breaker, applied per ticker). Then, all-or-nothing, the
    per-order, order-count, daily, and weekly caps against the fresh history (D4) and, when
    ``buying_power_usd`` is given, that the batch total fits the account's buying power.

    Args:
        orders: Orders about to be placed (from a plan or a compute path).
        broker_orders: Fresh, full-day-and-week order history.
        fresh_quotes: Symbol to latest price.
        trading_day: The ET trading date.
        tier: The budget tier in force.
        config: The strategy config (active and excluded universe).
        weekly_cap_usd: Effective weekly cap for rollover-reserve tiers.
        slot: Required when ``scope="slot"``.
        scope: ``"day"`` (default, conservative) or ``"slot"``.
        buying_power_usd: Fresh available buying power; ``None`` skips the check (the planner CLI
            always passes it and fails closed when it is missing or unusable).
        fresh_spreads: Symbol to ``(bid, ask)``; ``None`` skips the check (the CLI always passes it).

    Returns:
        ``PlanResult`` whose ``orders`` are safe to hand to ``dca_runner.execute_plan``.
    """
    history = list(broker_orders)
    try:
        snapshot = budget_snapshot(history, trading_day)
    except BudgetDataError as exc:
        return _fail(PlanError("history_unusable", type(exc).__name__))
    try:
        refs, bought = _placed_markers(
            _buy_attempts_on(history, trading_day), trading_day=trading_day, slot=slot, scope=scope
        )
    except ValueError as exc:
        return _fail(PlanError("bad_scope", str(exc)))
    active, excluded = set(config.tickers), set(config.excluded_tickers)
    keep: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    for order in orders:
        symbol = str(order["symbol"]).upper()
        reason = None
        if symbol in excluded:
            reason = "excluded_symbol"
        elif symbol not in active:
            reason = "unknown_symbol"
        elif symbol in bought or order["ref_id"] in refs:
            reason = "already_bought"
        elif not _is_positive_finite(fresh_quotes.get(symbol)):
            reason = "no_fresh_quote"
        elif fresh_quotes[symbol] >= order["prior_close"]:
            reason = "quote_not_below_prior_close"
        elif fresh_spreads is not None:
            reason = _spread_reason(fresh_spreads.get(symbol), config.max_bid_ask_spread_pct)
        if reason:
            skipped.append((symbol, reason))
        else:
            keep.append(dict(order))
    errors = [PlanError("notional_over_cap", o["symbol"]) for o in keep if o["notional_usd"] > MAX_PER_ORDER_USD]
    errors += _cap_errors(snapshot, tier, [o["notional_usd"] for o in keep], weekly_cap_usd, buying_power_usd)
    if errors:
        return PlanResult(errors=tuple(errors), skipped=tuple(skipped))
    return PlanResult(orders=tuple(keep), skipped=tuple(skipped))


# ---------------------------------------------------------------------------
# Compute fallback
# ---------------------------------------------------------------------------


def _usable_quote(row: Any) -> bool:
    if not isinstance(row, Mapping) or not isinstance(row.get("symbol"), str):
        return False
    low = row.get("prior_low")
    return (
        _is_positive_finite(row.get("prior_close"))
        and _is_positive_finite(row.get("decision_price"))
        and (low is None or _is_positive_finite(low))
    )


def compute_plan(
    quotes: Iterable[Any],
    *,
    config: StrategyConfig,
    trading_day: date,
    slot: str,
    generated_at_et: datetime,
    ladder_version: str,
    orders_today: Iterable[BrokerOrder] = (),
    tier: BudgetTier,
    weekly_cap_usd: float | None = None,
    buying_power_usd: Any = None,
) -> PlanResult:
    """Derive a plan from fetched quotes using the repo's own ladder and base selection.

    Each quote row needs ``symbol``, ``prior_close``, ``prior_low`` (may be ``None``), and
    ``decision_price``. A row that is malformed, outside the universe, or excluded is skipped
    and named in ``skipped``; the rest are still planned. Orders are ordered by allocation
    priority (``config/budgets.json``: higher multiplier, larger drop, then ticker). If the
    batch would breach any cap, NOTHING is planned (D4 literal) and ``plan`` is ``None``.

    Args:
        quotes: Rows as described above.
        config: Strategy config; its sizing, universe, and exclusions drive the result.
        trading_day: ET trading date.
        slot: Slot id.
        generated_at_et: Timestamp recorded in the plan (injected, so output is deterministic).
        ladder_version: Composite sizing version to stamp (see :func:`current_ladder_version`).
        orders_today: Fresh broker history used for the caps.
        tier: Budget tier in force.
        weekly_cap_usd: Effective weekly cap for rollover-reserve tiers.
        buying_power_usd: Fresh available buying power; the batch total must fit it. ``None`` skips the
            check (the planner CLI always passes it).

    Returns:
        ``PlanResult`` with the plan document (generator ``"backup"``) or a cap error.
    """
    active, excluded = set(config.tickers), set(config.excluded_tickers)
    skipped: list[tuple[str, str]] = []
    chosen: dict[str, dict[str, Any]] = {}
    for row in quotes:
        if not _usable_quote(row):
            label = row["symbol"].upper() if isinstance(row, Mapping) and isinstance(row.get("symbol"), str) else "<invalid>"
            skipped.append((label, "invalid_quote"))
            continue
        symbol = row["symbol"].upper()
        if symbol in chosen:
            continue
        if symbol in excluded:
            skipped.append((symbol, "excluded_symbol"))
            continue
        if symbol not in active:
            skipped.append((symbol, "unknown_symbol"))
            continue
        order = _size_order(row, symbol, config=config, slot=slot, trading_day=trading_day)
        if order is not None:
            chosen[symbol] = order
    return _finish_backup_plan(
        list(chosen.values()), skipped, config=config, trading_day=trading_day, slot=slot,
        generated_at_et=generated_at_et, ladder_version=ladder_version, orders_today=orders_today,
        tier=tier, weekly_cap_usd=weekly_cap_usd, buying_power_usd=buying_power_usd,
    )


def _finish_backup_plan(
    orders_in: list[dict[str, Any]],
    skipped: list[tuple[str, str]],
    *,
    config: StrategyConfig,
    trading_day: date,
    slot: str,
    generated_at_et: datetime,
    ladder_version: str,
    orders_today: Iterable[BrokerOrder],
    tier: BudgetTier,
    weekly_cap_usd: float | None,
    buying_power_usd: Any,
) -> PlanResult:
    """Order, cap-check and wrap sized orders as a ``generator: "backup"`` plan.

    Shared by :func:`compute_plan` (sized from quotes) and :func:`plan_from_decisions` (sized by the
    primary's Gate 5), so the two cannot drift on priority, the per-order cap, the daily and weekly
    caps (all-or-nothing, D4) or buying power.
    """
    # config/budgets.json allocation_priority: multiplier, then drop, then ticker alphabetical.
    # Written out rather than via dca_runner._allocation_priority_key, which re-estimates the
    # multiplier from the notional and misreads interpolated per-ticker multipliers.
    orders = sorted(orders_in, key=lambda o: (-o["multiplier"], -o["drop_pct"], o["symbol"]))
    errors = [PlanError("notional_over_cap", o["symbol"]) for o in orders if o["notional_usd"] > MAX_PER_ORDER_USD]
    try:
        snapshot = budget_snapshot(list(orders_today), trading_day)
    except BudgetDataError as exc:
        return _fail(PlanError("history_unusable", type(exc).__name__))
    errors += _cap_errors(snapshot, tier, [o["notional_usd"] for o in orders], weekly_cap_usd, buying_power_usd)
    if errors:
        return PlanResult(errors=tuple(errors), skipped=tuple(skipped))
    plan = {
        "schema_version": SCHEMA_VERSION,
        "trading_day": trading_day.isoformat(),
        "slot": slot,
        "generated_at_et": generated_at_et.isoformat(),
        "generator": "backup",
        "ladder_version": ladder_version,
        "universe_hash": universe_hash(config.tickers),
        "orders": orders,
    }
    return PlanResult(plan=plan, orders=tuple(orders), skipped=tuple(skipped))


def plan_from_decisions(
    decisions: Iterable[Mapping[str, Any]],
    *,
    config: StrategyConfig,
    trading_day: date,
    slot: str,
    generated_at_et: datetime,
    ladder_version: str,
    orders_today: Iterable[BrokerOrder] = (),
    tier: BudgetTier,
    weekly_cap_usd: float | None = None,
    buying_power_usd: Any = None,
) -> PlanResult:
    """A backup plan from decisions the primary's own Gate 5 already reached (the safety gate).

    Unlike :func:`compute_plan`, nothing is sized here: each decision carries the notional,
    multiplier and drop the signal engine produced, so the backup can never size differently from
    the primary. Decisions are ``symbol``, ``notional_usd``, ``multiplier``, ``drop_pct``,
    ``decision_price``, ``prior_close`` and ``prior_low`` (``None`` when unknown). A decision that is
    malformed, outside the universe or excluded is skipped and named in ``skipped``; the rest are
    planned, then the same all-or-nothing caps as :func:`compute_plan` apply.

    Returns:
        ``PlanResult`` with the plan document (generator ``"backup"``) or a cap error.
    """
    active, excluded = set(config.tickers), set(config.excluded_tickers)
    skipped: list[tuple[str, str]] = []
    chosen: dict[str, dict[str, Any]] = {}
    for row in decisions:
        symbol = (
            row.get("symbol").strip().upper()
            if isinstance(row, Mapping) and isinstance(row.get("symbol"), str)
            else None
        )
        low = row.get("prior_low") if isinstance(row, Mapping) else None
        if (
            symbol is None
            or not all(
                _is_positive_finite(row.get(k))
                for k in ("notional_usd", "multiplier", "drop_pct", "decision_price", "prior_close")
            )
            or (low is not None and not _is_positive_finite(low))
        ):
            skipped.append((symbol or "<invalid>", "invalid_decision"))
            continue
        if symbol in chosen:
            continue
        if symbol in excluded:
            skipped.append((symbol, "excluded_symbol"))
            continue
        if symbol not in active:
            skipped.append((symbol, "unknown_symbol"))
            continue
        chosen[symbol] = {
            "symbol": symbol,
            "notional_usd": round(float(row["notional_usd"]), 2),
            "multiplier": round(float(row["multiplier"]), 6),
            "drop_pct": round(float(row["drop_pct"]), 6),
            "decision_price": float(row["decision_price"]),
            "prior_close": float(row["prior_close"]),
            "prior_low": float(low) if _is_positive_finite(low) else None,
            "ref_id": _expected_ref_id(slot, trading_day, symbol),
        }
    return _finish_backup_plan(
        list(chosen.values()), skipped, config=config, trading_day=trading_day, slot=slot,
        generated_at_et=generated_at_et, ladder_version=ladder_version, orders_today=orders_today,
        tier=tier, weekly_cap_usd=weekly_cap_usd, buying_power_usd=buying_power_usd,
    )


def trailing_deployed_usd(orders: Iterable[BrokerOrder], *, now: datetime, days: int = 7) -> float:
    """Committed buy spend in the ``days`` days up to ``now``: the kill switch's input, from broker history.

    The primary derives its figure from run-state artifacts on the Mac, which the cloud cannot read. This
    counts EVERY placed agent's committed buys, an upper bound of the primary's afternoon-only figure, so
    any difference can only trip the 1x cap earlier, never later. A dead buy counts only its executed part.
    A window edge is inclusive on both sides.

    Raises:
        BudgetDataError: A counted order's amount is non-numeric, non-finite or not positive. An unknown
            fill is not a zero fill; reading it as zero would keep the kill switch off on the day it matters.
    """
    start = now - timedelta(days=days)
    total = 0.0
    for order in orders:
        if not (start <= order.created_at <= now) or not order.counts_toward_budget:
            continue
        amount = _committed_dollar_amount(order)
        try:
            unusable = isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0
        except OverflowError:
            unusable = True
        if unusable:
            raise BudgetDataError(f"broker order {order.symbol!r} has an unusable committed amount; true spend is unknown")
        total += amount
    return total


def already_placed_symbols(
    broker_orders: Iterable[BrokerOrder], *, trading_day: date, slot: str, scope: str = "day"
) -> set[str]:
    """Symbols with a buy attempt (dead ones included) that ``reconcile`` would treat as already placed.

    Lets a caller exclude them BEFORE scoring, with the same ``scope`` rule ``reconcile`` applies later.
    """
    _, symbols = _placed_markers(_buy_attempts_on(broker_orders, trading_day), trading_day=trading_day, slot=slot, scope=scope)
    return set(symbols)


def _size_order(
    row: Mapping[str, Any], symbol: str, *, config: StrategyConfig, slot: str, trading_day: date
) -> dict[str, Any] | None:
    """One plan order for ``row``, or ``None`` when the ladder says no buy."""
    prior_close = float(row["prior_close"])
    decision = float(row["decision_price"])
    if not decision < prior_close:
        return None
    drop = (prior_close - decision) / prior_close
    multiplier = graded_buy_multiplier(drop, config, symbol)
    if multiplier <= 0:
        return None
    base, _reason = select_first_allocation_base_usd(decision, prior_close, row.get("prior_low"), config, symbol)
    notional = round(base * multiplier, 2)
    if notional <= 0:
        return None
    prior_low = row.get("prior_low")
    return {
        "symbol": symbol,
        "notional_usd": notional,
        "multiplier": round(multiplier, 6),
        "drop_pct": round(drop * 100.0, 6),
        "decision_price": decision,
        "prior_close": prior_close,
        "prior_low": None if prior_low is None else float(prior_low),
        "ref_id": _expected_ref_id(slot, trading_day, symbol),
    }


def compute_plan_from_file(path: str | Path, **kwargs: Any) -> PlanResult:
    """:func:`compute_plan` over a quotes file ``{"quotes": [...]}``; missing or corrupt input fails closed.

    Returns:
        ``inputs_missing`` or ``inputs_corrupt`` errors with no orders, else ``compute_plan``'s result.
    """
    target = Path(path)
    if not target.exists():
        return _fail(PlanError("inputs_missing", target.name))
    try:
        document = _read_json_object(target)
    except (OSError, ValueError):
        return _fail(PlanError("inputs_corrupt", target.name))
    quotes = document.get("quotes")
    if not isinstance(quotes, list):
        return _fail(PlanError("inputs_corrupt", target.name))
    return compute_plan(quotes, **kwargs)


# ---------------------------------------------------------------------------
# Path selection
# ---------------------------------------------------------------------------


def choose_plan_path(mac_status: str, *, plan: PlanResult | None, elapsed_s: float) -> str:
    """Pick ``plan_source``: ``mac_plan``, ``mac_compute``, or ``cloud_compute`` (D8).

    The Mac is used only when it is ``"online"`` and the Mac-path time budget is unspent.
    Offline, not found, timeout, device error, or any unrecognized status goes to the cloud
    path, even if a plan file is lying around: a plan from an unreachable Mac is not trusted.
    Mac content is never purchase evidence on any path.

    Args:
        mac_status: ``"online"``, ``"offline"``, ``"not_found"``, ``"timeout"``, or ``"device_error"``.
        plan: The result of loading the Mac's plan, or ``None`` if none was read.
        elapsed_s: Seconds already spent on the Mac path.

    Returns:
        One of the three recorded ``plan_source`` values.
    """
    if mac_status != "online" or elapsed_s > MAC_PATH_BUDGET_S:
        return "cloud_compute"
    if plan is not None and plan.ok:
        return "mac_plan"
    return "mac_compute"


# ---------------------------------------------------------------------------
# Primary-side emit
# ---------------------------------------------------------------------------


def order_plan_path(trading_day: date, slot: str) -> Path:
    """Default plan location: ``<cron root>/order_plans/<YYYY-MM-DD>/order_plan_<slot>.json``.

    A tree of its own, not the slot's run directory: creating ``<stream>/<day>_<slot>ET``
    before a slot has run is read by the schedule verifier and same-day scans as "this slot
    ran" (the rule the retired prefetch planner also used). The root honours
    ``$DCA_CRON_TRACKING_ROOT`` through ``dca_paths``.
    """
    return Path(dca_paths.stream_root("order_plans", trading_day.isoformat(), f"order_plan_{slot}.json"))


def _as_float(value: Any) -> Any:
    """Normalize a JSON number to float so int and float inputs serialize identically.

    Anything else is returned unchanged for ``validate_plan`` to refuse.
    """
    return float(value) if _is_number(value) else value


def _survivor_order(row: Mapping[str, Any], *, slot: str, trading_day: date) -> dict[str, Any]:
    ticker = str(row.get("ticker", "")).strip().upper()
    return {
        "symbol": ticker,
        "notional_usd": _as_float(row.get("buy_usd")),
        "multiplier": _as_float(row.get("multiplier")),
        "drop_pct": _as_float(row.get("drop_pct")),
        "decision_price": _as_float(row.get("current_price")),
        "prior_close": _as_float(row.get("prior_close")),
        "prior_low": _as_float(row.get("prior_low")),
        "ref_id": _expected_ref_id(slot, trading_day, ticker),
    }


def _priority_key(order: Mapping[str, Any]) -> tuple[float, float, str]:
    """config/budgets.json allocation priority; malformed numerics sort last and fail validation."""
    multiplier, drop = order["multiplier"], order["drop_pct"]
    return (
        -(multiplier if _is_positive_finite(multiplier) else 0.0),
        -(drop if _is_positive_finite(drop) else 0.0),
        order["symbol"],
    )


def plan_from_survivors(
    rows: Any,
    *,
    config: StrategyConfig,
    trading_day: date,
    slot: str,
    generated_at_et: datetime,
    ladder_version: str,
) -> PlanResult:
    """Turn the primary's resolved buy list into a plan, before it places anything.

    ``rows`` are the Gate 6.5/6.7 survivors the live prompts carry: ``ticker``, ``buy_usd``
    (the amount that will actually be placed, which Gate 6.5 may have shrunk below the desired
    amount), ``multiplier``, ``drop_pct``, ``current_price``, ``prior_close`` and ``prior_low``.
    Other fields are ignored. Rows with ``buy_usd`` of exactly zero are dropped (a ticker the
    top-up guard zeroed out). Any other defect fails the WHOLE emit: no plan is produced.

    The result is checked with :func:`validate_plan` against the same universe, cap and
    ladder the backup reader will use, so a plan this function returns is one a reader accepts.

    Args:
        rows: A list of survivor mappings. Anything else is refused.
        config: Strategy config supplying the active and excluded universe.
        trading_day: ET trading date.
        slot: Slot id.
        generated_at_et: Timestamp recorded in the plan.
        ladder_version: Composite sizing version (see :func:`current_ladder_version`).

    Returns:
        ``PlanResult`` with the plan, or errors and ``plan`` of ``None``. Never raises.
    """
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        return _fail(PlanError("schema_violation", "survivors must be a list of objects"))
    orders = [
        _survivor_order(row, slot=slot, trading_day=trading_day)
        for row in rows
        if not (_is_number(row.get("buy_usd")) and row.get("buy_usd") == 0)
    ]
    orders.sort(key=_priority_key)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "trading_day": trading_day.isoformat(),
        "slot": slot,
        "generated_at_et": generated_at_et.isoformat(),
        "generator": "claude",
        "ladder_version": ladder_version,
        "universe_hash": universe_hash(config.tickers),
        "orders": orders,
    }
    return validate_plan(
        plan,
        trading_day=trading_day,
        slot=slot,
        ladder_version=ladder_version,
        active_tickers=config.tickers,
        excluded_tickers=config.excluded_tickers,
        max_per_order_usd=MAX_PER_ORDER_USD,
    )
