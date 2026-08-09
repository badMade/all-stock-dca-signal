"""Committed live-runner entrypoint for the seven scheduled DCA slots.

Closes gap #1 in ``PRODUCTION_READINESS.md`` ("live execution is prompt-driven,
not code-driven"). Everything here previously lived as prose inside seven
scheduled-task prompts, where it was subject to the schedule service's
documented silent truncation (``config/scheduled_tasks.json`` ->
``live_task_text_compactness_policy``). Guardrails that live in prompt text are
guardrails that can vanish without a diff.

Scope
-----
This module owns the gate sequence, durable state, budget arithmetic and order
planning. It deliberately does NOT own broker/market-data access: those arrive
as injected callables from the agent session that holds MCP runtime access,
exactly as ``docs/live_wiring.md`` describes. The split is what keeps this file
importable, testable and free of credentials.

Two-gate placement is preserved. ``StrategyConfig.dry_run`` is the first gate;
``enable_live_orders`` here is the second, defaults False, and this module never
sets it True on its own. A human flips it.

Design notes worth knowing before editing
-----------------------------------------
* ``StrategyState`` serialisation is hand-rolled because the dataclass carries
  JSON-hostile types: ``date`` dict keys, ``(iso_year, iso_week)`` tuple keys, a
  ``set`` of dates and a nested ``BudgetTier``. ``json.dump`` silently mangles
  all four, and the weekly/rollover caps rest on them.
* ``load_strategy_state`` raises on a corrupt file rather than returning a fresh
  state. ``docs/live_wiring.md`` warns that a fresh state "silently resets the
  reserve/weekly accounting and defeats the documented budget caps" -- a
  corrupt-file path that quietly resets is that same bug.
* Over-cap orders are refused, never clamped. Clamping hides a sizing bug.
* A cap boundary stops the batch. Nothing is back-filled and nothing is shrunk.
* ``typing.Optional`` is deliberately NOT imported. ``test_dca_runner`` asserts
  no name in this module's namespace contains "option" or "sell", as a
  structural check on the no-options / no-sells prohibition. Use ``X | None``.

See ``test_dca_runner.py`` for the behavioural contract; it was written first.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from dca_config import get_config_dir
from dca_fileio import atomic_open_w
from dca_live_adapters import _LIVE_ORDER_STATES as _CANONICAL_LIVE_ORDER_STATES
from dca_paths import stream_root
from near_close_dca import _TERMINAL_NON_FILL_STATES as _CANONICAL_TERMINAL_ORDER_STATES
from near_close_dca import (
    BUDGET_TIERS,
    NORMAL_WEEKLY_CAP_USD,
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
    TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS,
    TIER_SPEND_THRESHOLDS_USD,
    BudgetTier,
    StrategyState,
    account_pinning_failure,
    iso_week_key,
    select_budget_tier,
)

__all__ = [
    "ET",
    "CONFIG_DIR",
    "MAX_PER_ORDER_USD",
    "HIGH_MULTIPLIER_THRESHOLD",
    "STATE_SCHEMA_VERSION",
    "ACTIVE_TICKERS",
    "BUDGET_TIERS",
    "SLOTS",
    "SlotSpec",
    "MarketDayStatus",
    "CalendarCoverageError",
    "StateCorruptError",
    "BudgetDataError",
    "BrokerOrder",
    "BudgetSnapshot",
    "Candidate",
    "PlannedOrder",
    "Plan",
    "ExecutionResult",
    "REFUSED_OVER_PER_ORDER_CAP",
    "REFUSED_INVALID_AMOUNT",
    "REFUSED_ALREADY_BOUGHT_TODAY",
    "REFUSED_NOT_ACTIVE_TICKER",
    "REFUSED_INCONSISTENT_MULTIPLIER",
    "REFUSED_NON_CENT_AMOUNT",
    "SKIPPED_DAILY_CAP",
    "SKIPPED_WEEKLY_CAP",
    "SKIPPED_INSUFFICIENT_BUYING_POWER",
    "SKIPPED_DAILY_ORDER_COUNT_CAP",
    "assess",
    "load_strategy_state",
    "save_strategy_state",
    "state_transaction",
    "market_day_status",
    "gate_time_sanity",
    "gate_trading_window",
    "gate_sole_executor",
    "budget_snapshot",
    "plan_orders",
    "build_runner_broker",
    "execute_plan",
]

ET = ZoneInfo("America/New_York")
# Resolved via dca_config so an installed wheel (config shipped under
# sys.prefix/share/all-stock-dca/config, not a checkout sibling directory) and
# $DCA_CONFIG_DIR overrides both work, instead of hardcoding a checkout-only
# path (Codex review: "Resolve runner configuration through the shared loader").
CONFIG_DIR = get_config_dir()

# Broker states that never consumed budget (at their full requested amount).
# A rejected order is not spend. Sourced from near_close_dca's canonical
# terminal-state set -- which already includes both "cancel(l)ed" spellings
# and "expired" -- rather than a second hand-maintained copy: a hand-copied
# set here had omitted "expired" (like two prior hand-copies elsewhere in
# this repo), so an expired buy read as live and was charged its full
# requested dollar_amount instead of its real (zero or partial) executed
# amount, which can falsely exhaust the shared daily/weekly cap (Codex
# review: "Classify expired broker orders as dead").
_DEAD_ORDER_STATES = _CANONICAL_TERMINAL_ORDER_STATES


def _is_dead_order_state(state: object) -> bool:
    """True if ``state`` names a terminal (cancelled/rejected/failed/voided/expired) order state.

    Checked via ``isinstance()`` before the frozenset membership test below:
    externally normalized broker history is untyped at runtime, and an
    unhashable ``state`` (a list, a dict) would otherwise raise a bare
    ``TypeError`` from the ``in`` operator, escaping ``assess()``'s
    never-raises contract before Gate 9 could write evidence or alert
    (Codex review: "Convert malformed broker states into an abort
    verdict"). Stripped and lowercased before the membership test, the same
    way the placement-response path already normalizes a returned state: a
    case- or whitespace-variant like ``"REJECTED"``/``" cancelled "``
    otherwise reads as a live order and gets charged its full requested
    ``dollar_amount`` instead of its real (zero or partial) executed
    amount, which can falsely exhaust the shared daily/weekly cap (Codex
    review: "Normalize broker-history states before budget accounting").
    """
    if not isinstance(state, str):
        raise ValueError(f"BrokerOrder.state must be a string, got {state!r}")
    return state.strip().lower() in _DEAD_ORDER_STATES


_RECOGNIZED_ORDER_SIDES = frozenset({"buy", "sell"})


def _normalize_side(side: object) -> str:
    """Canonical lowercase, stripped ``BrokerOrder.side``, failing closed on
    anything unrecognized.

    Externally normalized broker history is untyped at runtime despite
    ``BrokerOrder.side``'s ``str`` annotation: an uppercase or whitespace-
    padded ``"BUY"`` would otherwise fail an exact-match comparison against
    the literal ``"buy"`` and be silently excluded from budget accounting
    (``counts_toward_budget``) and the same-day ticker interlock
    (``is_buy_attempt``), treating real committed spend as zero (Codex
    review: "Normalize broker order sides before budget filtering"). A
    non-string or unrecognized side is untrustworthy data, not automatically
    "not a buy" -- treating it as excluded could hide real spend the same
    way, so it is rejected outright instead.
    """
    if not isinstance(side, str):
        raise ValueError(f"BrokerOrder.side must be a string, got {side!r}")
    normalized = side.strip().lower()
    if normalized not in _RECOGNIZED_ORDER_SIDES:
        raise ValueError(f"BrokerOrder.side {side!r} is not a recognized order side")
    return normalized


REFUSED_OVER_PER_ORDER_CAP = "over_per_order_cap"
REFUSED_INVALID_AMOUNT = "invalid_amount"
REFUSED_ALREADY_BOUGHT_TODAY = "already_bought_today"
REFUSED_NOT_ACTIVE_TICKER = "not_active_ticker"
REFUSED_INCONSISTENT_MULTIPLIER = "inconsistent_multiplier_notional"
REFUSED_NON_CENT_AMOUNT = "non_cent_amount"
SKIPPED_DAILY_CAP = "daily_cap_reached"
SKIPPED_WEEKLY_CAP = "weekly_cap_reached"
SKIPPED_INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
SKIPPED_DAILY_ORDER_COUNT_CAP = "daily_order_count_cap_reached"


class CalendarCoverageError(RuntimeError):
    """Asked about a date outside ``market_calendar.json``'s ``years_covered``.

    Fail closed. Guessing a holiday schedule is how an executor trades on a day
    the exchange is shut.
    """


class StateCorruptError(RuntimeError):
    """Durable state exists but could not be read.

    Deliberately fatal. Returning a fresh ``StrategyState`` here would wipe the
    weekly and rollover accounting with no visible symptom.
    """


class BudgetDataError(RuntimeError):
    """Broker order history contains a notional that cannot be trusted.

    Raised by :func:`budget_snapshot` rather than silently excluding or
    summing the row; ``assess()`` catches it and returns ``ABORT``.
    """


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """``object_pairs_hook`` that raises on a duplicate key in one JSON object.

    ``json.loads`` silently keeps the LAST occurrence of a duplicate object
    key by default, so a corrupt document like
    ``{"holidays_closed": [...], "holidays_closed": []}`` loads successfully
    with the real, authoritative list overwritten by an empty one -- the
    same overwrite can erase persisted state's per-day notional or a
    per-ticker buy count, defeating a fail-closed loader's entire purpose
    (Codex review: "Reject duplicate keys in persisted state JSON", "Reject
    duplicate keys in runner configuration"). Applies at every nesting
    level: ``json.loads`` invokes this hook for every JSON object it
    parses, not just the top-level document. Used for both persisted
    ``StrategyState`` and this module's own ``config/*.json`` reads.
    """
    seen: set[str] = set()
    result: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON document")
        seen.add(key)
        result[key] = value
    return result


def _freeze_config_value(value: Any) -> Any:
    """Recursively freeze a parsed-JSON value so no nested dict/list escapes the
    shared cache entry mutable.

    ``MappingProxyType`` on its own only protects the TOP-LEVEL dict: a caller
    that mutates a nested value -- e.g. clearing ``market_calendar.json``'s
    ``holidays_closed`` list or changing a field inside ``circuit_breakers`` --
    would silently corrupt the cached object for every subsequent safety-gate
    read, whereas the pre-caching implementation reparsed a clean object from
    disk every time (Codex review: "Freeze nested configuration values before
    caching"). Every nested dict becomes a ``MappingProxyType`` and every
    nested list becomes a ``tuple``; scalars pass through unchanged.
    """
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_config_value(val) for key, val in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_config_value(item) for item in value)
    return value


@functools.lru_cache(maxsize=128)
def _read_config_cached(name: str, raw_bytes: bytes) -> Mapping[str, Any]:
    """Parse and freeze ``config/<name>``, keyed by ``name`` AND the file's
    exact content.

    Three earlier passes at this cache key (mtime alone; mtime + size) were
    each defeated by a variant of the same problem: any signal DERIVED from
    filesystem metadata can be fooled by a filesystem with coarser timestamp
    resolution than the gap between two writes, or by a deployment tool that
    preserves timestamps -- including an equal-BYTE-COUNT edit landing on a
    preserved mtime, which mtime+size still cannot tell apart (Codex review:
    "Invalidate equal-size configuration replacements", after "Use more than
    mtime to invalidate cached configs", after "Invalidate cached configs
    when their files change"). Keying on the content itself, rather than on
    any proxy for it, closes the whole class: this cache entry is stale if
    and only if ``raw_bytes`` is stale, which is impossible by construction
    since ``_read_config`` (below) always re-reads the file first. A
    long-lived runner process (``gate_trading_window()`` / Gate 0's
    ``market_day_status()``) therefore never serves a superseded trading
    window or market closure -- it still avoids the repeated JSON-parse-and-
    freeze cost this module was written to cut, just not a repeated read()
    syscall, which was never the expensive part (the measured win was
    ~0.2s/1000 calls of parsing, not of reading a few-KB file).
    """
    data = json.loads(raw_bytes, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object, got {type(data).__name__}")
    return _freeze_config_value(data)


def _read_config(name: str) -> Mapping[str, Any]:
    """Load ``config/<name>``, cached by its exact byte content.

    Always re-reads the file's raw bytes -- the read is cheap for these
    small config files, and it is what makes the content-addressed cache in
    ``_read_config_cached`` correct by construction rather than reliant on
    filesystem metadata (see that function's docstring for why metadata
    alone kept failing). Reading bytes rather than decoding via
    ``read_text`` also lets ``json.loads`` auto-detect a UTF-8 BOM or
    UTF-16/32 encoding, as it always could before this module cached config
    reads at all (Codex review: "Preserve BOM-aware configuration
    decoding").
    """
    return _read_config_cached(name, (CONFIG_DIR / name).read_bytes())


# Tests clear this between runs (via ``dca_runner._read_config.cache_clear()``)
# so a monkeypatched ``CONFIG_DIR`` takes effect immediately; delegate to the
# actual lru_cache-decorated function underneath.
_read_config.cache_clear = _read_config_cached.cache_clear


def _positive_finite_config_number(value: object, field: str, config_name: str) -> float:
    """Validate one config-sourced number, failing closed on anything untrustworthy.

    A non-finite or boolean value (``NaN``, the string ``"NaN"``, ``true``)
    would otherwise silently propagate into a live safety limit derived from
    it -- e.g. ``MAX_PER_ORDER_USD`` (every later ``amount_usd >
    MAX_PER_ORDER_USD`` comparison against a NaN ceiling is False) or
    ``HIGH_MULTIPLIER_THRESHOLD`` (``float(True)`` is ``1.0``, so every
    valid ladder candidate would count as high-multiplier) (Codex review:
    "Reject non-finite per-order cap configuration", "Reject boolean
    high-multiplier thresholds"). Checked BEFORE ``float()`` is attempted:
    ``isinstance(True, (int, float))`` is True (bool is an int subclass),
    so a boolean would otherwise coerce silently instead of being rejected.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{config_name} {field!r} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{config_name} {field!r} must be a finite, positive number, got {value!r}"
        )
    return value


def _validated_sizing_ladder() -> dict:
    """Load ``config/sizing_ladder.json``, validating every number this module
    derives a live safety limit from.

    Read and validated exactly once at import time: every module-level
    constant below (``MAX_PER_ORDER_USD``, ``MIN_LADDER_MULTIPLIER``, etc.)
    is derived from this single result, so a malformed config fails module
    import outright rather than deferring to a runtime check some call path
    could skip.
    """
    ladder = _read_config("sizing_ladder.json")
    # Computed up front (rather than in the returned dict at the bottom, as
    # before) so the per-tier loop below can validate each multiplier's
    # allocation against both bases while it still has them.
    base_amount_usd = _positive_finite_config_number(
        ladder["base_amount_usd"], "base_amount_usd", "sizing_ladder.json"
    )
    base_amount_below_close_usd = _positive_finite_config_number(
        ladder["base_amount_below_close_usd"], "base_amount_below_close_usd", "sizing_ladder.json"
    )
    multipliers = []
    seen_multipliers: set[float] = set()
    # multiplier -> (min_drop_pct, max_drop_pct_or_None) for the tier that
    # produces it. A candidate's claimed multiplier is trusted, elsewhere, as
    # long as its notional matches; this range is what additionally lets that
    # trust be cross-checked against a supplied drop_pct (Codex review: "Bind
    # the claimed multiplier to the reported drop").
    drop_ranges: dict[float, tuple[float, float | None]] = {}
    tier_ranges = []  # (min_drop_pct, max_drop_pct_or_None, multiplier), collected for the adjacency check below
    for t in ladder["tiers"]:
        multiplier = _positive_finite_config_number(
            t["multiplier"], "tiers[].multiplier", "sizing_ladder.json"
        )
        if multiplier in seen_multipliers:
            # drop_ranges is keyed by multiplier: a second tier reusing the
            # same multiplier would silently overwrite the first tier's
            # range here, so _candidate_drop_mismatches_claimed_multiplier()
            # would only ever see the LAST tier's range for that multiplier
            # -- refusing a genuine candidate the generator produced from
            # the now-invisible earlier tier (e.g. two adjacent tiers both
            # using 1.0x, [0.1, 1.0) and [1.0, 2.0), leave only the second
            # range visible and a real 0.5%/1.0x candidate gets refused)
            # (Codex review: "Reject duplicate ladder multipliers before
            # indexing ranges").
            raise ValueError(
                f"sizing_ladder.json has more than one tier configured with "
                f"multiplier={multiplier!r}; each configured multiplier must map to exactly "
                "one drop range"
            )
        seen_multipliers.add(multiplier)
        for base, base_field in (
            (base_amount_usd, "base_amount_usd"),
            (base_amount_below_close_usd, "base_amount_below_close_usd"),
        ):
            allocation = multiplier * base
            if abs(allocation - round(allocation, 2)) > 1e-9:
                # The core signal generator applies this exact multiplier to
                # either base and emits whatever notional results; plan_orders()
                # refuses a non-cent notional outright (REFUSED_NON_CENT_AMOUNT).
                # A schema-valid but sub-cent-producing interior multiplier (e.g.
                # 2.505, whose $1-base allocation is $2.505) would therefore let
                # the generator emit a genuine candidate this runner always
                # refuses, even though nothing here rejected the multiplier at
                # import time (Codex review: "Reject multipliers that produce
                # sub-cent allocations").
                raise ValueError(
                    f"sizing_ladder.json tier multiplier={multiplier!r} x {base_field}="
                    f"{base!r} = {allocation!r}, which is not cent-precise; the signal "
                    "generator would emit an amount plan_orders() always refuses as "
                    "REFUSED_NON_CENT_AMOUNT"
                )
        multipliers.append(multiplier)
        min_drop_pct = _positive_finite_config_number(
            t["min_drop_pct"], "tiers[].min_drop_pct", "sizing_ladder.json"
        )
        raw_max_drop_pct = t["max_drop_pct"]
        max_drop_pct = (
            None
            if raw_max_drop_pct is None
            else _positive_finite_config_number(
                raw_max_drop_pct, "tiers[].max_drop_pct", "sizing_ladder.json"
            )
        )
        if max_drop_pct is not None and max_drop_pct <= min_drop_pct:
            raise ValueError(
                f"sizing_ladder.json tier multiplier={multiplier!r} has max_drop_pct "
                f"({max_drop_pct!r}) at or below its own min_drop_pct ({min_drop_pct!r})"
            )
        drop_ranges[multiplier] = (min_drop_pct, max_drop_pct)
        tier_ranges.append((min_drop_pct, max_drop_pct, multiplier))
    # Sorted by min_drop_pct ascending and checked for gaps/overlaps: the
    # core signal generator selects a multiplier purely from these ordered
    # minimum thresholds and never consults the maxima at all, while THIS
    # runner cross-validates a supplied drop_pct against the stored
    # [min, max) range for the candidate's claimed multiplier (see
    # _candidate_drop_mismatches_claimed_multiplier). A schema-valid but
    # inconsistent ladder -- e.g. a 1x range of [0.1, 0.5) followed by a
    # 1.5x tier starting at 1.0, leaving a 0.5-1.0 gap -- lets the generator
    # produce a genuine 1x signal for a 0.75% drop that this runner would
    # then refuse as inconsistent, since 0.75 falls in neither tier's
    # stored range (Codex review: "Reject inconsistent sizing-ladder ranges
    # at import"). Each tier's max_drop_pct must exactly match the NEXT
    # tier's min_drop_pct -- an open-ended (None) max is therefore only
    # valid for the LAST (highest-range) tier.
    tier_ranges.sort(key=lambda r: r[0])
    for i in range(len(tier_ranges) - 1):
        this_min, this_max, this_multiplier = tier_ranges[i]
        next_min, _next_max, next_multiplier = tier_ranges[i + 1]
        if this_max != next_min:
            raise ValueError(
                f"sizing_ladder.json tiers are inconsistent: multiplier={this_multiplier!r}'s "
                f"max_drop_pct ({this_max!r}) does not match multiplier={next_multiplier!r}'s "
                f"min_drop_pct ({next_min!r})"
            )
    # The adjacency loop above only validates tiers that HAVE a successor,
    # so a finite max_drop_pct on the sorted FINAL (highest-range) tier
    # passes it silently. near_close_dca.graded_buy_multiplier() -- the core
    # signal generator -- ignores maxima entirely and keeps selecting that
    # last tier's multiplier for any drop above its min_drop_pct, with no
    # upper bound; this runner instead refuses a candidate whose drop_pct
    # falls outside the stored finite range, so a final 5x tier ending at
    # 9% would let a genuine 10% signal be generated but then refused here
    # as inconsistent (Codex review: "Require an open-ended final sizing
    # tier").
    if tier_ranges and tier_ranges[-1][1] is not None:
        raise ValueError(
            f"sizing_ladder.json's highest-range tier (multiplier={tier_ranges[-1][2]!r}) must "
            f"have a null max_drop_pct, got {tier_ranges[-1][1]!r}; the signal generator ignores "
            "maxima and has no upper cutoff for the highest tier"
        )
    return {
        "base_amount_usd": base_amount_usd,
        "base_amount_below_close_usd": base_amount_below_close_usd,
        "multipliers": multipliers,
        "drop_ranges": drop_ranges,
    }


_SIZING_LADDER = _validated_sizing_ladder()

# Per-order ceiling = base allocation x the largest ladder multiplier. Derived
# from config rather than hardcoded so the two cannot drift apart silently;
# ``test_per_order_cap_is_base_times_max_ladder_multiplier`` pins it.
MAX_PER_ORDER_USD = _SIZING_LADDER["base_amount_usd"] * max(_SIZING_LADDER["multipliers"])

def _validated_max_per_order_hard_limit(derived_max_per_order_usd: float) -> float:
    """Cross-check the sizing-ladder-derived per-order cap against
    ``config/budgets.json``'s own documented absolute circuit breaker.

    ``circuit_breakers.max_per_order_usd`` is an INDEPENDENT source of truth
    for the same $10 ceiling, never itself derived from sizing_ladder.json.
    If ``$DCA_CONFIG_DIR`` supplies a sizing_ladder.json whose base or max
    multiplier drifted from budgets.json (e.g. ``base_amount_usd`` raised to
    $20 with budgets.json's circuit breaker left at $10),
    ``MAX_PER_ORDER_USD`` would otherwise silently rise past the
    authoritative absolute limit budgets.json still declares, letting
    ``plan_orders()`` clear every over-cap check against the wrong, inflated
    ceiling (Codex review: "Cross-check the derived per-order cap against
    the hard limit"). Compared in whole cents, not float equality, to
    absorb ordinary float representation noise without accepting a real
    mismatch.

    The product check alone is not sufficient: a sizing ladder whose base and
    max multiplier both drifted (e.g. a $10 base with a 1x maximum) still
    reproduces the same $10 product even though budgets.json's own
    ``base_amount_usd``/``base_amount_below_close_usd`` -- the mirrored,
    authoritative bases per ``base_amount_policy.source_of_truth`` -- declare
    $2/$1. That would satisfy this function while every candidate is priced
    off the wrong, inflated base (Codex review: "Cross-check both configured
    base amounts"), so both bases are compared directly against
    budgets.json's top-level fields as well, independent of the product.
    """
    budgets_config = _read_config("budgets.json")
    hard_limit = _positive_finite_config_number(
        budgets_config["circuit_breakers"]["max_per_order_usd"],
        "circuit_breakers.max_per_order_usd",
        "budgets.json",
    )
    if round(derived_max_per_order_usd * 100) != round(hard_limit * 100):
        raise ValueError(
            f"MAX_PER_ORDER_USD derived from sizing_ladder.json "
            f"({derived_max_per_order_usd!r}) does not match budgets.json's "
            f"circuit_breakers.max_per_order_usd ({hard_limit!r}); the two configs have "
            "drifted apart"
        )
    for base_field, derived in (
        ("base_amount_usd", _SIZING_LADDER["base_amount_usd"]),
        ("base_amount_below_close_usd", _SIZING_LADDER["base_amount_below_close_usd"]),
    ):
        authoritative = _positive_finite_config_number(
            budgets_config[base_field], base_field, "budgets.json"
        )
        # Comparing only round(derived * 100) == round(authoritative * 100)
        # would treat a sub-cent-drifted base (e.g. $2.004 vs. the
        # authoritative $2.00) as identical, since both round to 200 cents --
        # letting import succeed while every order this base multiplies
        # (order_dollar_amount = base * multiplier) is priced a fraction of a
        # cent off from what budgets.json actually authorizes, e.g. a real
        # $5.00 order becoming a cent-precise $5.01 (Codex review: "Reject
        # sub-cent drift in authoritative base amounts"). Requiring
        # ``derived`` to already be cent-precise, rather than merely rounding
        # to the same cents as ``authoritative``, closes that gap. Inlined
        # (rather than calling the later-defined ``_cent_precise()``) since
        # this function runs at import time, before that helper is defined.
        if abs(derived - round(derived, 2)) > 1e-9:
            raise ValueError(
                f"sizing_ladder.json's {base_field} ({derived!r}) is not cent-precise; "
                "refusing to accept it as equivalent to budgets.json's authoritative "
                "value via a rounded comparison that could mask real configuration drift"
            )
        # The check above alone still misses the OTHER side of the same gap:
        # if an override keeps sizing_ladder.json's base at a cent-precise
        # $2.00 while budgets.json's own authoritative value drifts to a
        # sub-cent $2.004, both still round to 200 cents and the check below
        # would pass -- but the runner actually sizes every order from
        # `derived` ($2.00), silently ignoring the 0.4-cent drift
        # base_amount_policy.source_of_truth declares authoritative, and a
        # multiplier can turn that hidden drift into a real cent difference
        # elsewhere (Codex review: "Validate cent precision on the
        # authoritative base").
        if abs(authoritative - round(authoritative, 2)) > 1e-9:
            raise ValueError(
                f"budgets.json's {base_field} ({authoritative!r}) is not cent-precise; "
                "refusing to accept sizing_ladder.json's value as equivalent to it via a "
                "rounded comparison that could mask real configuration drift"
            )
        if round(derived * 100) != round(authoritative * 100):
            raise ValueError(
                f"sizing_ladder.json's {base_field} ({derived!r}) does not match budgets.json's "
                f"{base_field} ({authoritative!r}); the two configs have drifted apart"
            )
    return hard_limit


_MAX_PER_ORDER_USD_HARD_LIMIT = _validated_max_per_order_hard_limit(MAX_PER_ORDER_USD)

# First-allocation base ($2.00). Used only as a fallback estimate of a
# candidate's ladder multiplier when the caller does not supply one directly
# (see Candidate.multiplier below).
_BASE_AMOUNT_USD = _SIZING_LADDER["base_amount_usd"]

# The reduced ($1.00) first-allocation base, used below the prior close only.
# A legitimate candidate's amount_usd is always multiplier times ONE of these
# two bases -- needed to cross-check a caller-supplied multiplier against its
# own amount_usd (Codex review: "Validate candidate multipliers against their
# notionals").
_REDUCED_BASE_AMOUNT_USD = _SIZING_LADDER["base_amount_below_close_usd"]

# Threshold, in ladder multiplier terms, for a "3x-or-greater" (high-multiplier)
# signal that counts toward budget-tier escalation. Read from config rather than
# hardcoded so it cannot drift from config/budgets.json's own documented value.
# Validated the same way as the sizing-ladder inputs: an unvalidated boolean
# would coerce to 1.0 via float(True), making every valid ladder candidate
# count as high-multiplier and select a richer tier than warranted (Codex
# review: "Reject boolean high-multiplier thresholds").
HIGH_MULTIPLIER_THRESHOLD = _positive_finite_config_number(
    _read_config("budgets.json")["high_multiplier_threshold"],
    "high_multiplier_threshold",
    "budgets.json",
)

# The cap on weekly rollover reserve, mirroring near_close_dca.StrategyConfig's
# own default. Used to independently recompute the reserve
# roll_weekly_reserve() would have derived from persisted prior-week state,
# so a persisted weekly_rollover_reserve_usd cannot claim more than that
# derivation could ever justify (Codex review: "Verify persisted rollover
# reserve against prior-week state").
WEEKLY_ROLLOVER_RESERVE_CAP_USD = _positive_finite_config_number(
    _read_config("budgets.json")["weekly_rollover_reserve_cap_usd"],
    "weekly_rollover_reserve_cap_usd",
    "budgets.json",
)

def _validated_active_tickers() -> frozenset:
    """Load and validate ``tickers.json``'s ``active`` list before freezing it.

    A wrong-shaped but valid-JSON ``active`` value -- e.g. the bare string
    ``"AAPL"`` instead of a list -- would otherwise pass straight into
    ``frozenset()``, which iterates a string character-by-character and
    silently produces ``{"A", "P", "L"}``: legitimate AAPL candidates are
    then refused as "not active", while a single-character ticker like "A"
    passes the supposedly absolute whitelist circuit breaker (Codex review:
    "Validate the active-ticker collection before freezing it"). Entries are
    required to already be nonempty, upper-case-normalized strings (as
    ``config/tickers.json`` always ships them) rather than re-normalized
    here: ``_normalize_ticker`` is defined later in this module, and this
    function runs at import time, before it would exist yet.

    That "already normalized" requirement is enforced, not just documented:
    a supported ``$DCA_CONFIG_DIR`` override supplying ``"aapl"`` or
    ``" AAPL "`` previously only failed the (too-loose) nonempty-after-strip
    check, so it froze into ``ACTIVE_TICKERS`` verbatim, un-normalized. A
    real candidate's ticker is always normalized (stripped and upper-cased)
    before this whitelist check runs, so it would then find no match for
    its own, differently-cased or -padded entry and be refused as "not
    active", silently disabling that ticker (Codex review: "Normalize
    active tickers before freezing them"). Rejecting a not-yet-canonical
    entry outright -- rather than normalizing it here -- keeps this
    fail-closed on a config bug instead of silently repairing it.
    """
    active = _read_config("tickers.json")["active"]
    if (
        # _read_config freezes JSON arrays into tuples (see _freeze_config_value),
        # so the well-formed shape here is a tuple, not a list.
        not isinstance(active, (list, tuple))
        or not active
        or not all(isinstance(t, str) and t.strip() and t == t.strip().upper() for t in active)
    ):
        raise ValueError(
            "tickers.json 'active' must be a nonempty list of nonempty ticker strings, each "
            f"already stripped and upper-case (matching how _normalize_ticker would normalize "
            f"a real candidate's ticker), got {active!r}"
        )
    return frozenset(active)


# Whitelisted (active-universe) tickers. A candidate outside this set is
# refused rather than planned: docs/safeguards.md's circuit breakers require
# "whitelisted (active-universe) tickers only", and neither an excluded symbol
# nor a typo should be able to reach placement just because a persisted signal
# happened to name it (Codex review: "Enforce the active ticker whitelist
# before planning").
ACTIVE_TICKERS = _validated_active_tickers()

# The ladder's configured multiplier range. A candidate's self-reported
# multiplier outside this range (or non-finite) is untrustworthy and must not
# be allowed to inflate high-multiplier tier counting (Codex review: "Validate
# multipliers before tier selection").
MIN_LADDER_MULTIPLIER = min(_SIZING_LADDER["multipliers"])
MAX_LADDER_MULTIPLIER = max(_SIZING_LADDER["multipliers"])

# The exact set of configured ladder multipliers (1.0, 1.5, 2.0, ..., 5.0). A
# caller-supplied multiplier merely falling inside [MIN, MAX] still admits
# values the ladder never produces (e.g. 2.9), so tier counting requires an
# exact match against this set, not just range membership (Codex review:
# "Require exact configured candidate sizes").
_LADDER_MULTIPLIERS = frozenset(_SIZING_LADDER["multipliers"])

# multiplier -> (min_drop_pct, max_drop_pct_or_None), the tier that produces
# it. A candidate's claimed multiplier being a real, notional-consistent
# ladder value does not by itself prove the multiplier is genuine: the
# reported drop_pct must also fall inside the range that ladder value's own
# tier requires, or the pairing could never have been produced by
# config/sizing_ladder.json's real drop-to-multiplier mapping (Codex review:
# "Bind the claimed multiplier to the reported drop").
_LADDER_DROP_RANGE_BY_MULTIPLIER = _SIZING_LADDER["drop_ranges"]


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotSpec:
    """One scheduled execution slot.

    ``nominal_et`` is the cron target. ``window_*`` is the acceptance window the
    time-sanity gate enforces, which is wider than the target because the
    scheduler applies jitter (594s observed in the wild). The window is what
    makes a jittered fire legal and an off-schedule fire illegal.
    """

    slot_id: str
    nominal_et: tuple[int, int]
    window_start_et: tuple[int, int]
    window_end_et: tuple[int, int]
    label: str

    @property
    def window_minutes(self) -> int:
        start = self.window_start_et[0] * 60 + self.window_start_et[1]
        end = self.window_end_et[0] * 60 + self.window_end_et[1]
        return end - start

    def window_bounds_utc(self, day: date) -> tuple[datetime, datetime]:
        """Resolve this slot's ET window to absolute UTC instants on ``day``.

        Computed via ET rather than a fixed UTC offset so the 2026-11-01
        EDT->EST transition cannot shift the window by an hour.
        """
        start = datetime.combine(day, time(*self.window_start_et), tzinfo=ET)
        end = datetime.combine(day, time(*self.window_end_et), tzinfo=ET)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# Authoritative roster: docs/safeguards.md "Live execution roster".
SLOTS: dict[str, SlotSpec] = {
    "0935": SlotSpec("0935", (9, 35), (9, 30), (10, 0), "market-open"),
    "1030": SlotSpec("1030", (10, 30), (10, 30), (11, 0), "morning"),
    "1130": SlotSpec("1130", (11, 30), (11, 30), (12, 0), "intraday"),
    "1230": SlotSpec("1230", (12, 30), (12, 30), (13, 0), "midday"),
    "1330": SlotSpec("1330", (13, 30), (13, 30), (14, 0), "intraday"),
    "1430": SlotSpec("1430", (14, 30), (14, 30), (15, 0), "intraday"),
    "1530": SlotSpec("1530", (15, 30), (15, 30), (16, 0), "final"),
}


# ---------------------------------------------------------------------------
# Gate 0 -- market day
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketDayStatus:
    """Gate 0's verdict on one calendar day, from ``market_day_status()``.

    ``is_closed`` covers both weekends and configured holidays. ``is_early_close``
    is true only for a 1:00 PM ET close; see ``slot_should_end_silently`` for how
    the two combine with a given slot.
    """

    day: date
    is_closed: bool
    is_early_close: bool

    def slot_should_end_silently(self, slot: SlotSpec) -> bool:
        """True when this slot must not run today, with no alert.

        A closed exchange or an early-close afternoon is a normal calendar fact,
        not a fault worth waking anyone for.

        Known deferred gap (Codex review: "End the midday window before an
        early market close"): this checks only ``slot.nominal_et``, not
        whether ``slot.window_end_et`` could reach the 1:00 PM early close --
        the 12:30 slot's window ends exactly at 13:00. Not exploitable today
        (``config/trading_window.json`` ends 2026-10-30, before every
        currently-listed early-close date), so gate_time_sanity's contract
        was not changed to thread market status through it; revisit before
        extending the trading window into a year with an early close on/after
        a pre-13:00 slot's nominal time.
        """
        if self.is_closed:
            return True
        return self.is_early_close and slot.nominal_et >= (13, 0)


def _calendar_dates(cal: dict, key: str) -> frozenset:
    """Validate ``cal[key]`` and return it as a set of parsed ``date`` objects.

    ``stamp in cal[key]`` fails OPEN, not closed, for a wrong-shaped but
    valid-JSON value: ``in`` on a dict checks keys (``"2026-07-03" in {}`` is
    ``False``), and ``in`` on a string is a substring test (``"2026-07-03" in
    ""`` is also ``False``). Either would silently treat a configured closure
    as an open trading day instead of raising, escaping Gate 0's documented
    calendar-unavailable ``ABORT`` entirely (Codex review: "Validate calendar
    collection types before membership checks").

    Comparing raw strings has a second failure mode even once the type is
    right: a closure written as the basic, dash-free ISO spelling is a
    perfectly good date but never textually equals ``day.isoformat()``'s
    extended ``YYYY-MM-DD`` form, so Gate 0 would still
    treat a real configured closure as an open trading day (Codex review:
    "Normalize calendar entries before membership checks"). Parsing every
    entry into an actual ``date`` and comparing ``date`` objects (below)
    closes this regardless of which valid ISO spelling the config uses, and
    ``date.fromisoformat()`` itself raises on anything that isn't a real
    calendar date, routing an unparseable entry through the same conversion
    ``assess()`` already applies to a missing/malformed calendar file.
    """
    value = cal.get(key)
    # _read_config freezes JSON arrays into tuples (see _freeze_config_value),
    # so the well-formed shape here is a tuple, not a list.
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"market_calendar.json {key!r} must be a list of date strings, got {value!r}")
    try:
        return frozenset(date.fromisoformat(v) for v in value)
    except ValueError as exc:
        raise ValueError(f"market_calendar.json {key!r} contains an unparseable date: {exc}") from exc


def market_day_status(day: date) -> MarketDayStatus:
    """Run Gate 0 for ``day`` against ``config/market_calendar.json``.

    Raises :class:`CalendarCoverageError` if ``day``'s year is not covered by
    the calendar, rather than guessing whether the exchange is open.

    ``day`` must be an exact ``date``, not a ``datetime``. Python accepts a
    ``datetime`` here since it is a ``date`` subclass, but ``datetime ==
    date`` is unconditionally ``False`` (the reverse comparison isn't even
    attempted), so a ``datetime in holidays_closed`` membership check below
    would silently miss every configured closure and early close. A caller
    of this exported Gate 0 API who passes a ``datetime`` directly would
    therefore see a configured closure reported as an open session (Codex
    review: "Normalize datetime inputs before holiday membership"). Rejecting
    it outright -- rather than silently calling ``.date()`` on the caller's
    behalf -- keeps this fail-closed: every other caller in this file already
    passes a plain ``date`` derived from ``now.astimezone(ET).date()``.
    """
    if isinstance(day, datetime):
        raise ValueError(
            f"market_day_status requires a plain date, got a datetime ({day!r}); "
            "convert it to its ET calendar date (e.g. now.astimezone(ET).date()) first"
        )
    cal = _read_config("market_calendar.json")
    if day.year not in cal["years_covered"]:
        raise CalendarCoverageError(
            f"{day} is outside market_calendar.json years_covered={cal['years_covered']}; "
            "refresh the calendar rather than guessing the holiday schedule."
        )
    holidays_closed = _calendar_dates(cal, "holidays_closed")
    early_close_1300 = _calendar_dates(cal, "early_close_1300")
    return MarketDayStatus(
        day=day,
        is_closed=day.weekday() >= 5 or day in holidays_closed,
        is_early_close=day in early_close_1300,
    )


# ---------------------------------------------------------------------------
# Gate 1 -- time sanity
# ---------------------------------------------------------------------------


def gate_time_sanity(now: datetime, slot: SlotSpec) -> bool:
    """True when ``now`` falls inside ``slot``'s acceptance window.

    A naive datetime raises rather than being assumed local: around the DST
    switch an assumed offset is a silent one-hour error.

    The closing bound is EXCLUSIVE: an inclusive ``local <= end`` let the
    final (1530) slot's window accept ``now`` at exactly 16:00:00 ET, the
    regular-session close itself, so a placement-time recheck could still
    call the broker to queue a `gfd` market order right at (or a moment
    after) the close instead of enforcing the regular-hours-only contract
    (Codex review: "Exclude the market-close endpoint from slot windows").
    Exclusive on the high side also resolves the boundary between two
    adjacent slots unambiguously (e.g. 10:00:00 ET belongs to the 1030 slot,
    never also to 0935's).
    """
    if now.tzinfo is None:
        raise ValueError("gate_time_sanity requires a timezone-aware datetime")
    local = now.astimezone(ET)
    start = datetime.combine(local.date(), time(*slot.window_start_et), tzinfo=ET)
    end = datetime.combine(local.date(), time(*slot.window_end_et), tzinfo=ET)
    return start <= local < end


# ---------------------------------------------------------------------------
# Gate 3 -- approved trading window
# ---------------------------------------------------------------------------


def gate_trading_window(day: date) -> bool:
    """True when ``day`` falls inside the approved live-trading window.

    Reads both bounds from ``config/trading_window.json`` at call time, so
    the window's start/end dates are never hardcoded here.
    """
    cfg = _read_config("trading_window.json")
    start = date.fromisoformat(cfg["start_date"])
    end = date.fromisoformat(cfg["end_date"])
    if cfg.get("weekdays_only", True) and day.weekday() >= 5:
        return False
    return start <= day <= end


# ---------------------------------------------------------------------------
# Gate 4 -- sole-executor interlock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerOrder:
    """Normalised view of one ``get_equity_orders`` row.

    ``dollar_amount`` is the order's own requested/filled notional, trusted
    as-is for a live or ``filled`` order. ``executed_dollar_amount`` is
    separate: for a dead (cancelled/rejected/failed/voided) order it is the
    settled notional of any PARTIAL fill that happened before it ended, if
    the broker reports one -- ``dollar_amount`` on a dead order may still
    reflect the original, larger requested size rather than what was
    actually bought. See ``counts_toward_budget`` (Codex review: "Account
    for partially filled cancelled buys").
    """

    symbol: str
    side: str
    dollar_amount: float
    created_at: datetime
    placed_agent: str = "agentic"
    state: str = "filled"
    executed_dollar_amount: float | None = None

    @property
    def counts_toward_budget(self) -> bool:
        """True if this order represents real committed spend.

        A live/filled buy always counts, at its own ``dollar_amount``. A
        dead buy counts only when it reports a positive
        ``executed_dollar_amount``: the settled partial (or full) fill
        before it was cancelled/rejected/failed/voided is real money spent
        and real shares bought, and forgetting it would let the aggregate
        budget authorize buying past it as if nothing had happened (Codex
        review: "Account for partially filled cancelled buys"). A dead
        order with no fill at all still counts toward nothing.

        A non-finite or negative ``executed_dollar_amount`` is trusted as
        "counts" rather than filtered out here: comparing it ``> 0``
        directly would make a malformed value (NaN, a negative) read as "no
        fill" and skip ``budget_snapshot()``'s own fail-closed validation
        entirely, treating an unknown fill as zero spend (Codex review:
        "Reject malformed terminal-fill amounts before filtering").
        ``budget_snapshot()`` (via ``_committed_dollar_amount``) is what
        actually raises ``BudgetDataError`` for it.
        """
        if _normalize_side(self.side) != "buy":
            return False
        if _is_dead_order_state(self.state):
            if self.executed_dollar_amount is None:
                return False
            # A string or other non-number executed_dollar_amount must be
            # treated as "counts" (same as the non-finite/negative case
            # below) BEFORE math.isfinite() is called on it: isfinite()
            # raises a bare TypeError on a non-number, which would escape
            # assess()'s never-raises contract instead of reaching
            # budget_snapshot()'s BudgetDataError validation (Codex review:
            # "Validate terminal-fill types before calling isfinite").
            if isinstance(self.executed_dollar_amount, bool) or not isinstance(
                self.executed_dollar_amount, (int, float)
            ):
                return True
            try:
                unusable = (
                    not math.isfinite(self.executed_dollar_amount)
                    or self.executed_dollar_amount < 0
                )
            except OverflowError:
                # A syntactically valid but astronomically large JSON integer
                # (e.g. 10**1000, which json.loads parses as a plain Python
                # int of unbounded size) survives the numeric-type check
                # above but makes math.isfinite() raise OverflowError rather
                # than return False. Treated the same as the non-finite/
                # negative case just below: "counts", so
                # budget_snapshot()'s own OverflowError-aware validation is
                # what actually raises BudgetDataError for it, rather than a
                # bare crash escaping here (Codex review: "Convert oversized
                # broker integers into a budget-data abort").
                unusable = True
            if unusable:
                return True
            return self.executed_dollar_amount > 0
        return True

    @property
    def is_buy_attempt(self) -> bool:
        """True if this row represents an attempted buy for its ticker today,
        regardless of whether it filled, was cancelled, or was rejected.

        Deliberately broader than ``counts_toward_budget``: a cancelled/
        rejected buy with no fill still consumed the day's
        one-order-per-ticker attempt (``config/budgets.json``'s
        ``one_order_per_ticker_per_day``), even though it consumed no
        budget. Using ``counts_toward_budget`` for the same-day dedup let a
        rejected order make its ticker eligible again the same day (Codex
        review: "Keep canceled orders in the daily ticker interlock").
        """
        return _normalize_side(self.side) == "buy"


def _require_aware(created_at: datetime) -> datetime:
    """Reject a naive ``created_at``, mirroring ``gate_time_sanity``'s own check.

    ``datetime.astimezone()`` silently interprets a naive value in the host's
    local timezone. A persisted UTC broker timestamp that lost its offset would
    then shift by several hours once normalised to ET, which can hide an order
    that is really inside this slot's window from the sole-executor interlock
    (Codex review: "Reject naive timestamps in the sole-executor interlock").

    A non-``datetime`` ``created_at`` (a string, ``None``, ...) is checked
    BEFORE the ``.tzinfo`` access below: accessing that attribute on anything
    else raises a bare ``AttributeError``, which ``assess()``'s Gate 4 only
    catches as ``ValueError`` -- an externally normalized broker row with a
    malformed timestamp would otherwise escape assess()'s never-raises
    contract before Gate 9 could write evidence or alert (Codex review:
    "Reject non-datetime broker timestamps as malformed").
    """
    if not isinstance(created_at, datetime):
        # A non-datetime created_at can be arbitrary connector data -- a
        # dict carrying an access token, account identifier, or request
        # headers, for instance -- rather than a plain malformed value.
        # Both assess() and execute_plan() copy this raised message into
        # their persisted/alerted failure evidence, so only a safe
        # classification (the value's type) is reported here, matching the
        # redaction already applied to every other malformed broker field
        # (Codex review: "Redact malformed broker timestamps from failure
        # evidence"). A confirmed datetime that is merely naive (below) is
        # not secret-shaped and stays reported as-is.
        raise ValueError(
            f"BrokerOrder.created_at requires a timezone-aware datetime, got type="
            f"{type(created_at).__name__}"
        )
    if created_at.tzinfo is None:
        raise ValueError(
            f"BrokerOrder.created_at requires a timezone-aware datetime, got a naive one: {created_at!r}"
        )
    return created_at


def _reject_non_broker_order_rows(orders) -> None:
    """Raise ``ValueError`` if any row in materialized ``orders`` is not a ``BrokerOrder``.

    ``gate_sole_executor()``'s own row-shape check (rerun after the fix for
    "Convert malformed history rows into ABORT") only proves a row has a
    usable ``.created_at`` -- a duck-typed row exposing just that one
    attribute clears Gate 4 but still crashes downstream the first time
    ``budget_snapshot()`` or ``.counts_toward_budget``/``.is_buy_attempt``
    reads ``.side``, ``.state``, or ``.executed_dollar_amount``, none of
    which Gate 4 ever touches. A single up-front type check closes every one
    of those later access sites at once instead of chasing each downstream
    ``AttributeError`` individually (Codex review: "Validate complete
    history rows before running later gates").
    """
    for row in orders:
        if not isinstance(row, BrokerOrder):
            # type(row).__name__, not repr(row): a malformed raw row from a
            # broker wrapper can be a dict (or similar) carrying an account
            # number, a token, or request headers, and assess() copies this
            # message verbatim into the ABORT reason it writes as run
            # evidence and may send through alert channels (Codex review:
            # "Redact malformed broker rows from abort evidence").
            raise ValueError(
                "broker order history contains a row that is not a BrokerOrder instance "
                f"(got {type(row).__name__})"
            )


def gate_sole_executor(orders, slot: SlotSpec, day: date) -> list[BrokerOrder]:
    """Return orders already submitted inside this slot's window.

    Non-empty means a second writer is live and the run must abort. Matching is
    on ``created_at`` (submission), never fill time: a fill can lag arbitrarily,
    but submission is what identifies who wrote.

    ``placed_agent`` is intentionally ignored. A manual order inside the window
    is still a second writer against the same account.
    """
    start, end = slot.window_bounds_utc(day)
    return [o for o in orders if start <= _require_aware(o.created_at).astimezone(timezone.utc) <= end]


# ---------------------------------------------------------------------------
# Gate 6 -- budget, computed from broker history
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetSnapshot:
    """Spend already committed, read from the broker rather than local counters.

    Sourcing this from ``get_equity_orders`` instead of ``StrategyState`` means
    the caps hold even when durable state is lost, and it is what lets the
    shared cap span all seven slots plus manual buying.
    """

    day: date
    day_notional_usd: float
    week_notional_usd: float
    # Buy ATTEMPTS already submitted for ``day`` across all seven slots and
    # manual buying. Counts cancelled/rejected attempts too (is_buy_attempt,
    # not counts_toward_budget): an attempt consumed one of the day's orders
    # whether or not it filled, and over-counting is the safe direction for a
    # cap. Defaults to 0 so existing direct constructions keep working.
    day_order_count: int = 0


def _committed_dollar_amount(order: BrokerOrder) -> float:
    """The dollar amount ``order`` actually committed, for budget accounting.

    A dead (cancelled/rejected/failed/voided) order's ``dollar_amount`` may
    still reflect the original, larger requested size rather than what was
    actually filled before it ended -- only ``executed_dollar_amount`` is
    trustworthy spend for a dead order. A live/filled order's own
    ``dollar_amount`` is used as-is (Codex review: "Account for partially
    filled cancelled buys").
    """
    if _is_dead_order_state(order.state):
        return order.executed_dollar_amount if order.executed_dollar_amount is not None else 0.0
    return order.dollar_amount


def budget_snapshot(orders, day: date) -> BudgetSnapshot:
    """Sum committed spend for ``day`` and its ISO week (Monday start).

    Policy (chosen 2026-07-28): count every order regardless of
    ``placed_agent``. Manual and automated buying draw on one shared pool.

    A non-finite or non-positive committed amount raises :class:`BudgetDataError`
    rather than being silently excluded or summed: summing NaN makes every
    downstream cap comparison False (silently bypassing every cap), but
    excluding the row instead understates real spend -- the true amount is
    unknown, not zero, and treating it as zero can authorize a batch beyond
    the actual daily/weekly cap (Codex review: "Abort on malformed broker
    notionals"). ``assess()`` catches this and returns ``ABORT`` rather than
    letting it escape.
    """
    week = iso_week_key(day)
    day_total = 0.0
    week_total = 0.0
    for order in orders:
        if not order.counts_toward_budget:
            continue
        amount = _committed_dollar_amount(order)
        # Externally normalized broker history is untyped at runtime despite
        # BrokerOrder's float annotation: a JSON boolean survives as `True`,
        # and `math.isfinite(True)` succeeds (bool is an int subclass),
        # silently summing it as $1 real spend understated far below the
        # unknown true amount; a string or None instead raises an uncaught
        # TypeError from math.isfinite() below, escaping the documented
        # BudgetDataError/ABORT path entirely (Codex review: "Reject
        # non-number broker notionals before summing").
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise BudgetDataError(
                f"broker order {order.symbol!r} has a non-numeric committed amount "
                f"{amount!r}; true spend is unknown, refusing to treat it as zero"
            )
        try:
            amount_is_unusable = not math.isfinite(amount) or amount <= 0
        except OverflowError:
            # A syntactically valid but astronomically large JSON integer
            # (e.g. 10**1000, which json.loads parses as a plain Python int
            # of unbounded size) passes the numeric-type check above but
            # makes math.isfinite() raise OverflowError rather than return
            # False, escaping this function's documented BudgetDataError/
            # ABORT path entirely instead of failing closed the same way
            # every other unusable notional here does (Codex review:
            # "Convert oversized broker integers into a budget-data abort").
            amount_is_unusable = True
        if amount_is_unusable:
            raise BudgetDataError(
                f"broker order {order.symbol!r} has a non-finite or non-positive "
                f"committed amount {amount!r}; true spend is unknown, refusing "
                "to treat it as zero"
            )
        when = _require_aware(order.created_at).astimezone(ET).date()
        if when == day:
            day_total += amount
        if iso_week_key(when) == week:
            week_total += amount
    # Each individual amount above already passed its own finiteness check,
    # but float addition does not raise on overflow: two otherwise-valid rows
    # in the same day/week (e.g. two $1e308 notionals) can still accumulate
    # past float range into `inf`, which every downstream cap comparison
    # treats as "any prospective spend is over the cap" only by accident --
    # PROCEED with an infinite budget value is the wrong verdict for
    # malformed history, and `Infinity` is not valid standard JSON for the
    # required run evidence either way (Codex review: "Reject broker-history
    # totals that overflow to infinity").
    if not math.isfinite(day_total):
        raise BudgetDataError(
            f"broker order history's total committed spend for {day.isoformat()} overflowed "
            f"to a non-finite value ({day_total!r}); true spend is unknown, refusing to treat "
            "it as usable"
        )
    if not math.isfinite(week_total):
        raise BudgetDataError(
            f"broker order history's total committed spend for the week of "
            f"{day.isoformat()} overflowed to a non-finite value ({week_total!r}); true "
            "spend is unknown, refusing to treat it as usable"
        )
    day_order_count = sum(
        1
        for order in orders
        if order.is_buy_attempt and _require_aware(order.created_at).astimezone(ET).date() == day
    )
    return BudgetSnapshot(
        day=day,
        day_notional_usd=day_total,
        week_notional_usd=week_total,
        day_order_count=day_order_count,
    )


@dataclass(frozen=True)
class Candidate:
    """One caller-supplied signal row before Gate 6's checks are applied.

    ``ticker`` and ``amount_usd`` are taken as given; neither is normalized or
    validated until ``plan_orders``/``assess`` process this candidate.
    """

    ticker: str
    amount_usd: float
    # The ladder multiplier that produced amount_usd, when the caller has it.
    # Preferred over inferring the multiplier from amount_usd / base, because a
    # reduced ($1) first-allocation base makes that inference undercount: a
    # valid 3x-5x candidate on the reduced base has amount_usd of $3-$5, which
    # is below 3x the *global* $2 base and would be missed (Codex review:
    # "Count high-multiplier signals by multiplier, not notional").
    multiplier: float | None = None
    # The drop vs. the prior close that produced this candidate, when the
    # caller has it. Used only as the second-tier allocation priority (Codex
    # review: "Apply the configured allocation priority before cap checks").
    drop_pct: float | None = None


def _normalize_ticker(ticker: object) -> str:
    """Canonical ticker form for dedup, whitelist checks, and placement.

    Deduplication and whitelist membership must agree on one identity, or a
    batch containing both "AAPL" and "aapl" plans two live orders for the same
    security, violating the one-order-per-ticker circuit breaker (Codex
    review: "Normalize tickers before deduplicating them").

    A non-string ``ticker`` (``None``, a bool, ...) is checked BEFORE
    ``.strip()``/``.upper()`` are called on it: those raise a bare
    ``AttributeError`` on anything else, which would escape ``assess()``'s
    never-raises contract before Gate 9 could write evidence or alert
    (Codex review: "Convert malformed broker symbols into an abort
    verdict").

    An empty or whitespace-only ``ticker`` normalizes to ``""`` and is
    rejected rather than returned as-is: for a broker-history symbol, that
    would let the same-day ticker interlock (``bought_today``) record an
    anonymous ``""`` entry instead of the order's real ticker, so a
    genuine same-day buy whose wrapper lost the symbol would not block a
    second live order for that same, unverifiable ticker (Codex review:
    "Reject normalized-empty broker symbols").
    """
    if not isinstance(ticker, str):
        raise ValueError(f"ticker/symbol must be a string, got {ticker!r}")
    normalized = ticker.strip().upper()
    if not normalized:
        raise ValueError(f"ticker/symbol {ticker!r} normalizes to an empty string")
    return normalized


def _cents(amount_usd: float) -> int | float:
    """``amount_usd`` rounded to whole cents, for exact currency comparison.

    A tolerance-based comparison (``abs(a - b) <= 0.01``) treats a full cent
    of drift as noise: ``$5.99`` reads as consistent with a configured
    ``$2 x 3 = $6.00`` size, when the two are a full cent apart and only one
    can be the real, config-derived notional (Codex review: "Require exact
    configured candidate sizes").

    A representable finite ``amount_usd`` (e.g. ``1e308``, well past any
    real per-order cap but still a legitimate Python float) makes
    ``amount_usd * 100`` overflow to +/-inf, and ``round()`` on an infinite
    float raises ``OverflowError`` rather than returning an int. Every real
    caller compares this against a small, config-derived cent value (well
    under $10), so returning +/-inf here instead of raising lets
    candidate/consistency checks correctly read an astronomically large
    amount as "does not match any real ladder allocation" -- comparing False
    against every finite value -- instead of crashing ``plan_orders()``/
    ``assess()`` before either can refuse the single malformed candidate or
    return the documented `ABORT` (Codex review: "Reject oversized finite
    amounts before cent conversion").
    """
    try:
        return round(amount_usd * 100)
    except OverflowError:
        return math.inf if amount_usd > 0 else -math.inf


def _candidate_multiplier_is_consistent(cand: Candidate) -> bool:
    """True if ``cand.multiplier``, when supplied, actually produced ``cand.amount_usd``.

    ``cand.multiplier is None`` (no multiplier claim at all) is trivially
    consistent -- there is nothing to validate, and the caller falls back to
    a notional-based estimate. When a multiplier IS supplied, it must EXACTLY
    match one of the ladder's configured values AND its cent-normalized
    ``amount_usd`` must exactly equal ``multiplier * base`` for one of the
    two configured bases; anything else is an internally inconsistent pair,
    e.g. ``("AAPL", 5.99, 3.0)`` -- 3.0 is a real ladder multiplier, but
    neither $2 nor $1 base x 3.0 produces $5.99. Shares its exact-match logic
    with :func:`_estimated_multiplier`'s own trust condition.
    """
    if cand.multiplier is None:
        return True
    if not math.isfinite(cand.multiplier) or cand.multiplier not in _LADDER_MULTIPLIERS:
        return False
    if not math.isfinite(cand.amount_usd):
        return False
    return _cents(cand.amount_usd) == _cents(cand.multiplier * _BASE_AMOUNT_USD) or _cents(
        cand.amount_usd
    ) == _cents(cand.multiplier * _REDUCED_BASE_AMOUNT_USD)


def _amount_matches_any_ladder_allocation(amount_usd: float, drop_pct: float | None = None) -> bool:
    """True if ``amount_usd`` exactly equals ``base * multiplier`` for SOME
    configured base ($2.00 or the reduced $1.00) and configured ladder
    multiplier (1.0, 1.5, ..., 5.0) -- and, when ``drop_pct`` is usable, that
    SAME multiplier's configured drop range also contains it.

    A legitimate candidate's notional is always exactly one of those
    products, regardless of whether the caller happened to also supply a
    multiplier -- ``("AAPL", 5.99)`` (no multiplier at all) is exactly as
    unrepresentable as ``("AAPL", 5.99, 3.0)``, since the configured $1/$2
    bases and 1x-5x half-step ladder cannot produce $5.99 from any
    combination (Codex review: "Reject amounts that match no ladder
    allocation").

    Checking the notional in isolation is not enough once a ``drop_pct`` is
    also present: ``("AAPL", 2.0, None, -1.0)`` -- no multiplier claim, but a
    negative drop -- has a notional that matches the 1.0x tier, yet no
    configured tier's drop range can ever be negative, so this candidate is
    just as untrustworthy as one with an explicit inconsistent multiplier
    (Codex review: "Validate reported drops without a usable multiplier").
    ``drop_pct`` being ``None`` or non-finite is a metadata gap, not
    corruption, and is not checked here -- consistent with
    :func:`_candidate_drop_mismatches_claimed_multiplier`'s own treatment.
    """
    if not math.isfinite(amount_usd):
        return False
    cents = _cents(amount_usd)
    drop_is_usable = drop_pct is not None and math.isfinite(drop_pct)
    for m in _LADDER_MULTIPLIERS:
        if cents != _cents(m * _BASE_AMOUNT_USD) and cents != _cents(m * _REDUCED_BASE_AMOUNT_USD):
            continue
        if not drop_is_usable:
            return True
        min_drop_pct, max_drop_pct = _LADDER_DROP_RANGE_BY_MULTIPLIER[m]
        if drop_pct < min_drop_pct:
            continue
        if max_drop_pct is not None and drop_pct >= max_drop_pct:
            continue
        return True
    return False


def _candidate_drop_mismatches_claimed_multiplier(cand: Candidate) -> bool:
    """True when ``cand.drop_pct`` could never have produced ``cand.multiplier``.

    Only meaningful once ``cand.multiplier`` is already known to be a real,
    exact ladder value (callers check that first): ``config/sizing_ladder.json``
    maps a specific ``[min_drop_pct, max_drop_pct)`` range to each configured
    multiplier, so e.g. ``Candidate("AAPL", 10.0, multiplier=5.0,
    drop_pct=-0.01)`` -- a real 5x notional match, but a NEGATIVE drop, which
    no tier can ever produce -- is exactly as untrustworthy as a notional
    mismatch, even though the amount_usd/multiplier pair looks internally
    consistent on its own (Codex review: "Bind the claimed multiplier to the
    reported drop").

    ``drop_pct`` is ``None`` or non-finite is treated as a metadata GAP, not
    corruption -- consistent with :func:`_candidate_multiplier_notional_mismatch`'s
    own treatment of missing/malformed metadata alongside an otherwise-valid
    notional -- and returns False rather than flagging the candidate.

    ``cand.multiplier`` equal to the ladder's configured MINIMUM
    (``MIN_LADDER_MULTIPLIER``) is exempt from this check's UPPER bound: the
    documented 7-day risk-guard kill-switch (``claude/GATE5-PROPOSAL.md``
    #3.6) caps a deep-drop signal's multiplier down to that floor while the
    candidate still carries its REAL, uncapped ``drop_pct`` -- callers are
    explicitly required to perform this capping before candidates ever
    reach ``assess()``/``plan_orders()``. Without this exemption, a
    legitimate capped candidate like ``Candidate("AAPL", 2.0,
    multiplier=1.0, drop_pct=8.5)`` would be refused as inconsistent purely
    because its real drop falls outside the floor tier's own narrow range,
    even though the notional itself is already the safest (minimum)
    allocation the ladder can produce regardless of the true drop (Codex
    review: "Accept kill-switch-capped multipliers").

    The floor's own LOWER bound is still enforced even for a capped
    candidate: the kill-switch only ever caps a genuine buy signal (a real
    drop of at least the ladder's own ``no_buy_below_pct`` minimum) down to
    the floor multiplier, it never fabricates one, so a capped candidate
    reporting a sub-threshold or negative drop_pct (e.g. ``drop_pct=-1.0``)
    is still corrupted evidence, not a legitimate kill-switch capping (Codex
    review: "Reject sub-threshold drops for capped minimum multipliers").
    """
    if cand.drop_pct is None or not math.isfinite(cand.drop_pct):
        return False
    if cand.multiplier == MIN_LADDER_MULTIPLIER:
        min_drop_pct, _max_drop_pct = _LADDER_DROP_RANGE_BY_MULTIPLIER[MIN_LADDER_MULTIPLIER]
        return cand.drop_pct < min_drop_pct
    drop_range = _LADDER_DROP_RANGE_BY_MULTIPLIER.get(cand.multiplier)
    if drop_range is None:
        return False
    min_drop_pct, max_drop_pct = drop_range
    if cand.drop_pct < min_drop_pct:
        return True
    return max_drop_pct is not None and cand.drop_pct >= max_drop_pct


def _candidate_multiplier_notional_mismatch(cand: Candidate) -> bool:
    """True when ``cand.amount_usd`` cannot be trusted as a real ladder allocation.

    Three cases, all "sizing corruption, not a shallow candidate that merely
    sorts lower":

    1. A multiplier that IS a real ladder value paired with a notional that
       matches NEITHER configured base at that SPECIFIC multiplier -- e.g.
       ``("AAPL", 5.99, 3.0)``, where 3.0 is real but neither $2 nor $1 base
       x 3.0 is $5.99 (Codex review: "Refuse inconsistent multiplier and
       notional pairs").
    2. A multiplier that IS a real ladder value, with a notional that DOES
       match, but a ``drop_pct`` outside that multiplier's own configured
       drop range -- e.g. ``("AAPL", 10.0, 5.0, -0.01)``, where $10 really is
       5.0x the $2 base, but a negative drop could never have produced a 5x
       signal per ``config/sizing_ladder.json``'s real tiers (Codex review:
       "Bind the claimed multiplier to the reported drop"). See
       :func:`_candidate_drop_mismatches_claimed_multiplier`.
    3. No usable multiplier claim at all (``None``, non-finite, or outside
       the configured set) -- rather than skip validation entirely, the
       notional AND (when usable) the drop are checked against EVERY
       configured base x multiplier product and that same multiplier's drop
       range; a candidate omitting (or mangling) the multiplier is exactly as
       untrustworthy as one supplying a wrong one if no single configured
       multiplier's tier is consistent with BOTH its amount_usd and its
       drop_pct -- e.g. ``("AAPL", 2.0, None, -1.0)``: $2.00 matches the 1.0x
       tier's notional, but no configured tier's drop range is ever negative
       (Codex review: "Reject amounts that match no ladder allocation";
       "Validate reported drops without a usable multiplier"). See
       :func:`_amount_matches_any_ladder_allocation`.

    An out-of-range or non-finite multiplier alongside an amount_usd/drop_pct
    pair that DOES match some real ladder allocation is not flagged here:
    that combination is malformed METADATA only, and merely falls back to a
    notional-based estimate for sorting/tier-counting elsewhere (Codex
    review: "Validate multipliers before tier selection") -- refusing the
    candidate outright over bad metadata alone would discard a real,
    correctly-sized buy.
    """
    if not math.isfinite(cand.amount_usd):
        return False
    if (
        cand.multiplier is not None
        and math.isfinite(cand.multiplier)
        and cand.multiplier in _LADDER_MULTIPLIERS
    ):
        notional_mismatch = not (
            _cents(cand.amount_usd) == _cents(cand.multiplier * _BASE_AMOUNT_USD)
            or _cents(cand.amount_usd) == _cents(cand.multiplier * _REDUCED_BASE_AMOUNT_USD)
        )
        return notional_mismatch or _candidate_drop_mismatches_claimed_multiplier(cand)
    return not _amount_matches_any_ladder_allocation(cand.amount_usd, cand.drop_pct)


def _parse_candidate_rows(candidates) -> list[Candidate]:
    """Parse raw ``(ticker, amount_usd[, multiplier[, drop_pct]])`` rows into
    ``Candidate`` objects, exactly as ``assess()`` does.

    A row that already IS a ``Candidate`` (as the "direct Python caller"
    path -- ``plan_orders()`` then ``execute_plan()`` -- would naturally
    pass, per ``docs/live_wiring.md``'s "reuse the same Plan, BudgetSnapshot,
    and BudgetTier objects for both calls") is re-normalized and used
    directly rather than round-tripped through tuple unpacking: this is what
    lets a caller reuse the EXACT SAME candidates list for both
    ``plan_orders()`` and ``execute_plan()``'s tier-verification, guaranteeing
    ``plan`` was actually built from the candidates being verified instead of
    two independently-maintained (and driftable) lists.

    Signal-generation output ("candidates") is external repo data with no
    upstream validation: a malformed row shape (wrong arity, a non-iterable
    row), a nonnumeric amount/multiplier/drop_pct, or a lazy iterable that
    raises an unrelated exception (RuntimeError, ConnectionError, ...)
    partway through consumption -- e.g. a failing signal-generation service
    -- all raise here rather than being caught; each caller converts that
    into its own fail-closed handling (``assess()``'s ABORT, ``execute_plan``'s
    refusal) (Codex review: "Convert malformed candidate values into an abort
    verdict"; "Convert lazy candidate failures into ABORT").
    """
    cands = []
    for row in candidates:
        if isinstance(row, Candidate):
            # Still routed through the exact same _json_number validation
            # below as a tuple row: a prebuilt Candidate is just as public
            # and caller-constructible as a raw tuple, so Candidate("AAPL",
            # True) or Candidate("AAPL", "bad") must fail the same way a
            # tuple ("AAPL", True)/("AAPL", "bad") already does, instead of
            # bypassing _json_number entirely and either laundering a
            # boolean into a valid-looking $1 order or raising an uncaught
            # TypeError instead of the documented ABORT/refusal (Codex
            # review: "Validate fields on prebuilt Candidate rows").
            ticker, amount, multiplier, drop_pct = (
                row.ticker, row.amount_usd, row.multiplier, row.drop_pct
            )
        elif not isinstance(row, (list, tuple)):
            # Generic iterable unpacking below (`ticker, amount = row`) does not
            # care WHAT it unpacks: a mapping row like {"AAPL": "ignored", 2.0:
            # "ignored"} iterates its KEYS, not its values, so it silently
            # unpacks to the valid-looking pair ("AAPL", 2.0) -- laundering a
            # row whose actual (ignored) values contain no usable candidate
            # data at all into a real order (Codex review: "Reject mapping
            # candidate rows before unpacking keys"). Requiring an explicit
            # list/tuple (besides the Candidate branch above) rejects a dict,
            # a set, a bare string, or any other iterable outright instead of
            # unpacking whatever it happens to iterate as.
            raise ValueError(
                "candidate row must be a (ticker, amount_usd[, multiplier[, drop_pct]]) "
                f"list/tuple or a Candidate instance, got {row!r}"
            )
        elif len(row) == 4:
            ticker, amount, multiplier, drop_pct = row
        elif len(row) == 3:
            ticker, amount, multiplier = row
            drop_pct = None
        else:
            ticker, amount = row
            multiplier = None
            drop_pct = None
        cands.append(
            Candidate(
                # Normalized once here so every downstream comparison (already-
                # bought-today, tier-selection dedup, plan_orders' own dedup)
                # agrees on the same identity for "AAPL" and "aapl" (Codex
                # review: "Normalize tickers before deduplicating them").
                ticker=_normalize_ticker(ticker),
                # _json_number (not a bare float()) rejects a JSON boolean:
                # float(True) silently succeeds as 1.0, so a malformed
                # amount_usd=true would otherwise launder into a
                # valid-looking $1 order that no later boundary can tell
                # apart from a real one (Codex review: "Reject boolean
                # candidate amounts before coercion").
                amount_usd=_json_number(amount, "candidate amount_usd"),
                multiplier=(
                    None if multiplier is None
                    else _json_number(multiplier, "candidate multiplier")
                ),
                # Carried through so plan_orders' allocation-priority tie-break
                # (higher multiplier, then larger drop, then ticker) sees the
                # real drop instead of treating every candidate as a 0% drop,
                # which degrades the tie-break to alphabetical ordering among
                # equal-multiplier candidates (Codex review: "Carry drop
                # percentage through assess").
                drop_pct=(
                    None if drop_pct is None
                    else _json_number(drop_pct, "candidate drop_pct")
                ),
            )
        )
    return cands


def _tier_selection_inputs(fresh_cands: list[Candidate]) -> tuple[list[Candidate], float, int]:
    """Derive ``(valid_cands, planned_spend_usd, high_multiplier_signal_count)``
    from candidates that already exclude same-day-bought tickers, the same
    way ``assess()`` selects a tier.

    Tier selection must see the same one-per-ticker, valid, in-cap,
    whitelisted view ``plan_orders`` will actually place, not the raw
    candidate list: duplicate, invalid, over-cap, non-active-ticker, or
    multiplier/notional-mismatched rows inflate ``planned_spend``/``high_mult``
    and can select a richer tier than the real (deduped) plan needs or uses
    (Codex review: "Deduplicate candidates before selecting the tier",
    "Exclude over-cap candidates from tier selection", "Refuse inconsistent
    multiplier and notional pairs"). Deduped in allocation-priority order, so
    a duplicate ticker's WINNING row here is the same one ``plan_orders()``
    itself keeps. ``valid_cands`` -- these same eligible winners, not the raw
    input -- is also what ``execute_plan()`` must bind ``plan.to_place``
    against: matching against the raw, pre-dedup/pre-validation candidates
    would let a plan order pair with a row ``plan_orders()`` would itself
    have refused (e.g. a multiplier/notional mismatch) or a lower-priority
    duplicate that never actually wins allocation (Codex review: "Match
    plans only against eligible candidate winners").
    """
    seen_tickers: set[str] = set()
    valid_cands = []
    for c in sorted(fresh_cands, key=_allocation_priority_key):
        if c.ticker in seen_tickers:
            continue
        if not math.isfinite(c.amount_usd) or c.amount_usd <= 0:
            continue
        if c.amount_usd > MAX_PER_ORDER_USD:
            continue
        if abs(c.amount_usd - round(c.amount_usd, 2)) > 1e-9:
            # plan_orders() itself refuses a non-cent amount (e.g. 9.996) with
            # REFUSED_NON_CENT_AMOUNT and never places it, but without this
            # same check here, that unplaceable row still counted toward
            # planned_spend/high_mult: near the $129 escalation boundary, a
            # handful of sub-cent rows could select Elevated or higher caps
            # on their own, and the REST of a real, placeable plan would then
            # spend against that inflated cap instead of the Normal tier's
            # real $180 ceiling (Codex review: "Exclude sub-cent candidates
            # from tier selection").
            continue
        if c.ticker not in ACTIVE_TICKERS:
            continue
        if _candidate_multiplier_notional_mismatch(c):
            continue
        # Marked "seen" only now, once this candidate has cleared every
        # validity check -- the same fix already applied to plan_orders()'s
        # own dedup (Codex review: "Deduplicate tier candidates only after
        # validation").
        seen_tickers.add(c.ticker)
        valid_cands.append(c)
    planned_spend = sum(c.amount_usd for c in valid_cands)
    high_mult = sum(1 for c in valid_cands if _estimated_multiplier(c) >= HIGH_MULTIPLIER_THRESHOLD)
    return valid_cands, planned_spend, high_mult


def _multiplier_for_drop_pct(drop_pct: float) -> float | None:
    """The ladder multiplier whose configured drop range contains ``drop_pct``, or ``None``.

    Lets :func:`_estimated_multiplier` recover a candidate's real multiplier
    from a trusted, usable ``drop_pct`` when its own multiplier claim is
    unusable, instead of guessing purely from the notional (Codex review:
    "Infer reduced-base multipliers from usable drops").
    """
    for m, (min_drop_pct, max_drop_pct) in _LADDER_DROP_RANGE_BY_MULTIPLIER.items():
        if drop_pct < min_drop_pct:
            continue
        if max_drop_pct is not None and drop_pct >= max_drop_pct:
            continue
        return m
    return None


def _estimated_multiplier(cand: Candidate) -> float:
    """The multiplier to use for tier counting and allocation priority.

    A caller-supplied multiplier is trusted only when it EXACTLY matches one
    of the ladder's configured values (1.0, 1.5, ..., 5.0 -- not merely inside
    the 1x-5x range, which would still admit a value the ladder never
    produces) AND its cent-normalized ``amount_usd`` exactly equals
    ``multiplier * base`` for one of the two configured bases ($2.00 or the
    reduced $1.00): a legitimate candidate's amount is always exactly one of
    those products. Without this exact check, a malformed candidate could
    report a $2 amount (a low-multiplier notional on either base) alongside
    ``multiplier=5``, or a notional a cent off a real configured size,
    inflating high-multiplier tier counting and selecting a richer budget
    tier than the candidates' real notionals justify (Codex review:
    "Validate candidate multipliers against their notionals", "Require exact
    configured candidate sizes"). Anything else (NaN, inf, a value the
    ladder never configures, or a cent-mismatched pairing) falls back below.

    A non-finite ``amount_usd`` (e.g. NaN) cannot feed any notional-based
    estimate: it would return NaN and poison ``_allocation_priority_key``
    the same way an unguarded NaN ``drop_pct`` did, making sort order among
    the OTHER, valid candidates depend on caller order instead of allocation
    priority (Codex review: "Reject malformed amounts before allocation
    sorting"). Falls back to the ladder's minimum multiplier instead --
    lowest priority, never highest -- since the candidate is refused outright
    once per-candidate amount validation runs regardless of where it sorted.

    Absent a trusted multiplier, dividing ``amount_usd`` by the $2 base
    ALONE (the prior behavior) silently assumes every candidate used the
    full base: ``Candidate("AAPL", 4.0, None, 6.5)`` -- a genuine reduced-
    base ($1) 4x signal, since 6.5% falls in the 4x tier's [6.0, 7.0) drop
    range and $4.00 is exactly 4.0 x $1.00 -- would otherwise estimate as
    only 2x ($4.00 / $2.00), understating both its high-multiplier tier
    count and its allocation priority against real 3x candidates (Codex
    review: "Infer reduced-base multipliers from usable drops"). When
    ``drop_pct`` is usable, the multiplier its own tier implies is tried
    first (against BOTH bases); it is used only if that specific multiplier
    also reproduces ``amount_usd`` exactly, so a drop that does not agree
    with the notional at all still falls through to the plain division.
    """
    if cand.multiplier is not None and _candidate_multiplier_is_consistent(cand):
        return cand.multiplier
    if not math.isfinite(cand.amount_usd):
        return MIN_LADDER_MULTIPLIER
    if cand.drop_pct is not None and math.isfinite(cand.drop_pct):
        drop_multiplier = _multiplier_for_drop_pct(cand.drop_pct)
        if drop_multiplier is not None and (
            _cents(cand.amount_usd) == _cents(drop_multiplier * _BASE_AMOUNT_USD)
            or _cents(cand.amount_usd) == _cents(drop_multiplier * _REDUCED_BASE_AMOUNT_USD)
        ):
            return drop_multiplier
    return cand.amount_usd / _BASE_AMOUNT_USD


def _allocation_priority_key(cand: Candidate) -> tuple[float, float, str]:
    """Sort key for config/budgets.json's documented allocation priority.

    "Allocations are prioritized by higher multiplier first, then larger
    percent drop, then ticker (alphabetical) for a deterministic tie-break."
    Consuming candidates in caller order instead let an unsorted shallow
    candidate claim headroom a later high-multiplier candidate needed,
    reversing the strategy's prescribed allocation.

    A non-finite ``drop_pct`` (e.g. NaN) falls back to 0.0 rather than being
    used as-is: NaN comparisons are always False, so sorting on a raw NaN key
    makes Python's sort order depend on the surrounding candidates' order
    instead of on drop, letting caller order alone decide which of two
    otherwise-identical candidates wins scarce cap headroom (Codex review:
    "Reject non-finite drop percentages before sorting").
    """
    drop = cand.drop_pct if cand.drop_pct is not None and math.isfinite(cand.drop_pct) else 0.0
    return (-_estimated_multiplier(cand), -drop, _normalize_ticker(cand.ticker))


def _candidate_sortable_for_planning(cand: Candidate) -> bool:
    """True if :func:`_allocation_priority_key` can compute ``cand``'s sort
    key without raising.

    ``plan_orders()`` is a public, independently-callable entry point
    (docs/live_wiring.md's "direct Python caller" path) that a caller-built
    ``Candidate`` can reach WITHOUT ever passing through
    ``_parse_candidate_rows()``'s own type validation. A non-numeric
    ``amount_usd``/``multiplier``/``drop_pct`` (e.g. ``Candidate("AAPL",
    "bad")``) makes ``math.isfinite()`` raise a bare ``TypeError`` inside the
    sort key itself, and a non-string or unnormalizable ``ticker`` makes
    ``_normalize_ticker()`` raise a bare ``ValueError`` there too -- either
    crashes ``sorted(candidates, key=_allocation_priority_key)`` before this
    function's own per-candidate loop (and its controlled
    ``REFUSED_INVALID_AMOUNT`` refusal) ever runs for ANY candidate in the
    batch, not just the malformed one (Codex review: "Validate candidates
    before sorting them"). A syntactically valid but astronomically large int
    (e.g. ``10**1000``) passes the ``isinstance`` checks inside the key but
    makes ``math.isfinite()`` raise ``OverflowError`` -- not return ``False``
    -- so that must be caught here too, or the same whole-batch crash
    reappears through a different exception type (Codex review: "Catch
    oversized candidate integers before sorting").
    """
    try:
        _allocation_priority_key(cand)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


@dataclass(frozen=True)
class PlannedOrder:
    """One order ``plan_orders`` decided to place: a canonical, validated ticker,
    an in-cap positive amount, and a buy-only side.

    Both fields are public and constructible independently of ``plan_orders``,
    so ``execute_plan`` revalidates them rather than trusting a caller-built
    ``Plan`` (Codex review: "Revalidate buy plans at the placement boundary").
    """

    ticker: str
    amount_usd: float
    side: str = "buy"


@dataclass
class Plan:
    """The output of ``plan_orders``: what to place, skip, and refuse, and why.

    See ``plan_orders``' docstring for what distinguishes ``skipped`` (a cap
    boundary, stops the batch) from ``refused`` (a per-candidate problem that
    does not).
    """

    to_place: list[PlannedOrder] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_notional_usd(self) -> float:
        return sum(o.amount_usd for o in self.to_place)


def plan_orders(
    candidates,
    snapshot: BudgetSnapshot,
    tier: BudgetTier,
    *,
    weekly_cap_usd: float | None = None,
    buying_power_usd: float | None = None,
) -> Plan:
    """Turn candidates into a placement plan under the per-order and cap rules.

    Four distinct outcomes, kept separate on purpose:

    * ``refused``  -- a non-finite/non-positive amount, or above the per-order
      ceiling. Surfaces a sizing bug. Does NOT stop the batch; one mis-sized
      ticker should not cost the whole day.
    * ``skipped``  -- a cap boundary (daily, weekly, or buying power) was
      reached. This DOES stop the batch, and every remaining candidate is
      skipped with it. Nothing is back-filled, because letting a smaller later
      order slip through the gap would make the cap depend on candidate
      ordering.
    * ``to_place`` -- fits, in order.

    ``weekly_cap_usd`` overrides ``tier.weekly_cap_usd`` when given -- pass
    ``StrategyState.effective_weekly_cap()`` here for the stress tier, whose
    real ceiling depends on the weekly rollover reserve and is not the tier's
    nominal cap alone (Codex review: "Enforce the effective stress-tier
    rollover cap"). Defaults to the tier's nominal cap when omitted.

    ``buying_power_usd``, when given, is an additional ceiling on top of the
    daily/weekly caps: a run must never plan more than the account can
    actually pay for, even when cap headroom remains (Codex review: "Enforce
    available buying power before planning orders").

    Amounts are never shrunk to fit remaining headroom.

    ``weekly_cap_usd`` and ``buying_power_usd``, when given, must themselves be
    finite and non-negative: a NaN ceiling compares False against every
    prospective-spend check, silently disabling that cap for the entire batch
    (Codex review: "Reject non-finite budget ceilings"). Malformed input fails
    closed with a ``ValueError`` rather than planning against a disabled cap.
    """
    for value, field_name in (
        (weekly_cap_usd, "weekly_cap_usd"),
        (buying_power_usd, "buying_power_usd"),
    ):
        if value is None:
            continue
        # Checked BEFORE math.isfinite(): a bool survives as an int subclass
        # and isfinite(True) succeeds silently (treating a $True ceiling as
        # $1 of real headroom), while a string instead raises an uncaught
        # TypeError from isfinite() itself (Codex review: "Validate planner
        # ceilings before calling isfinite").
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"plan_orders: {field_name} must be a number, got {value!r}")
        try:
            value_is_unusable = not math.isfinite(value) or value < 0
        except OverflowError:
            # A syntactically valid but astronomically large JSON integer
            # (e.g. 10**1000, from a broker-supplied buying_power_usd or a
            # caller-supplied weekly_cap_usd) passes the isinstance check
            # above but makes math.isfinite() raise OverflowError rather
            # than return False, escaping plan_orders()'s documented
            # ValueError entirely instead of being treated as unusable
            # (Codex review: "Validate planner ceilings before calling
            # isfinite").
            value_is_unusable = True
        if value_is_unusable:
            raise ValueError(
                f"plan_orders: {field_name} must be a finite, non-negative number, got {value!r}"
            )

    plan = Plan()
    running = 0.0
    seen: set[str] = set()
    stop_reason: str | None = None
    effective_weekly_cap = tier.weekly_cap_usd if weekly_cap_usd is None else weekly_cap_usd

    # Partitioned BEFORE sorting: a candidate whose fields can't even compute
    # a sort key (see _candidate_sortable_for_planning) would otherwise crash
    # sorted() itself, before this loop's own per-candidate validation (right
    # below) ever gets to refuse it with the documented, controlled
    # REFUSED_INVALID_AMOUNT (Codex review: "Validate candidates before
    # sorting them").
    sortable_candidates = []
    for cand in candidates:
        if not isinstance(cand, Candidate):
            # docs/live_wiring.md's documented "direct Python caller" path
            # ("call plan_orders directly with the same snapshot/tier/
            # candidates") reuses the SAME raw (ticker, amount_usd[,
            # multiplier[, drop_pct]]) rows assess() accepts, but
            # plan_orders() never normalized a raw row into a Candidate
            # itself: _allocation_priority_key() below dereferences
            # cand.drop_pct, so a tuple raised a bare AttributeError instead
            # of producing a plan (Codex review: "Normalize documented
            # candidate rows before planning"). Routed through
            # _parse_candidate_rows() for EVERY non-Candidate row, not just
            # a list/tuple: a mapping, bare string, or other malformed
            # object reaches the exact same AttributeError crash, aborting
            # the whole batch instead of being classified the documented
            # REFUSED_INVALID_AMOUNT way a malformed Candidate already is
            # below (Codex review: "Refuse malformed direct candidate
            # objects without crashing"). _parse_candidate_rows() itself
            # raises ValueError for anything that isn't a list/tuple or
            # Candidate instance, so this single call covers every shape.
            try:
                cand = _parse_candidate_rows([cand])[0]
            except (TypeError, ValueError):
                # A malformed direct candidate can be arbitrary connector
                # data -- a dict carrying an access token or account
                # identifier, for instance -- rather than a plain
                # unrecognizable row. This refusal is recorded in
                # Plan.refused, which callers can persist as run evidence or
                # include in alerts, so repr(cand) is embedded only when
                # cand (or a list/tuple row's own first element, the
                # presumed ticker) is itself a plain string; anything else
                # is reported only by type (Codex review: "Redact malformed
                # candidates before returning them").
                if isinstance(cand, str):
                    ticker_repr = repr(cand)
                elif isinstance(cand, (list, tuple)) and cand and isinstance(cand[0], str):
                    ticker_repr = repr(cand[0])
                else:
                    ticker_repr = f"<{type(cand).__name__}>"
                plan.refused.append((ticker_repr, REFUSED_INVALID_AMOUNT))
                continue
        if _candidate_sortable_for_planning(cand):
            sortable_candidates.append(cand)
            continue
        try:
            ticker_repr = _normalize_ticker(cand.ticker)
        except (TypeError, ValueError):
            ticker_repr = repr(cand.ticker)
        plan.refused.append((ticker_repr, REFUSED_INVALID_AMOUNT))

    # Sorted by config/budgets.json's documented allocation priority (higher
    # multiplier first, then larger drop, then ticker) rather than caller
    # order, so a shallow candidate cannot claim cap headroom a later
    # high-multiplier candidate needed (Codex review: "Apply the configured
    # allocation priority before cap checks").
    for cand in sorted(sortable_candidates, key=_allocation_priority_key):
        # Normalized once and reused for dedup, whitelist membership, and the
        # placed order's own symbol, so "AAPL" and "aapl" in the same batch
        # cannot both plan a live order for the same security (Codex review:
        # "Normalize tickers before deduplicating them").
        ticker = _normalize_ticker(cand.ticker)

        if ticker in seen:
            continue  # one order per ticker per batch
        if stop_reason is not None:
            plan.skipped.append((ticker, stop_reason))
            continue

        if ticker not in ACTIVE_TICKERS:
            # Whitelisted (active-universe) tickers only -- a persisted signal
            # naming an excluded symbol or a typo must never reach placement
            # (Codex review: "Enforce the active ticker whitelist before
            # planning").
            plan.refused.append((ticker, REFUSED_NOT_ACTIVE_TICKER))
            continue

        if isinstance(cand.amount_usd, bool) or not isinstance(cand.amount_usd, (int, float)):
            # plan_orders() is a public, independently-callable entry point
            # (docs/live_wiring.md's "direct Python caller" path) -- a
            # Candidate reaching it never passed through
            # _parse_candidate_rows()'s own boolean/type validation, only
            # through assess()'s does. bool is an int subclass, so
            # Candidate("AAPL", True) would otherwise pass math.isfinite()
            # and every later ladder check as amount_usd=1 (True == 1),
            # producing a real $1 PlannedOrder for a direct caller instead of
            # the documented REFUSED_INVALID_AMOUNT (Codex review: "Reject
            # boolean amounts in direct plans").
            plan.refused.append((ticker, REFUSED_INVALID_AMOUNT))
            continue

        if not math.isfinite(cand.amount_usd) or cand.amount_usd <= 0:
            # NaN/inf/zero/negative amounts must never reach arithmetic: NaN
            # compares False against every cap check (silently bypassing every
            # cap), and a negative amount would reduce `running` (Codex
            # review: "Reject non-finite and non-positive candidate
            # notionals").
            plan.refused.append((ticker, REFUSED_INVALID_AMOUNT))
            continue

        if cand.amount_usd > MAX_PER_ORDER_USD:
            plan.refused.append((ticker, REFUSED_OVER_PER_ORDER_CAP))
            continue

        if abs(cand.amount_usd - round(cand.amount_usd, 2)) > 1e-9:
            # Every cap/buying-power comparison below (and, downstream,
            # assess()'s own tier-selection sums) uses this raw float, but
            # placement sends f"{amount_usd:.2f}" -- the rounded cent value
            # the broker actually receives. A non-cent amount like
            # ("AAPL", 9.996) previously authenticated against the ladder at
            # its unrounded value (9.996 rounds to the same cents as a real
            # $10 allocation) and reached to_place, while a caller formatting
            # the returned amount as ordinary currency would submit $10.00 --
            # exceeding a $9.996 buying-power or aggregate-cap boundary by
            # the rounding delta (Codex review: "Refuse sub-cent candidate
            # notionals during planning"). Refused outright rather than
            # quantized, since a real ladder-derived amount (base ×
            # multiplier) is always cent-precision already -- mirroring
            # execute_plan()'s own placement-time cent-precision check.
            plan.refused.append((ticker, REFUSED_NON_CENT_AMOUNT))
            continue

        if _candidate_multiplier_notional_mismatch(cand):
            # A supplied multiplier that is a real ladder value but does not
            # produce amount_usd from either configured base -- e.g.
            # ("AAPL", 5.99, 3.0), where 3.0 is a genuine ladder multiplier
            # but neither $2 nor $1 base x 3.0 is $5.99 -- previously only
            # affected _estimated_multiplier()'s fallback (silently
            # downgrading its tier-selection weight), while the malformed,
            # non-ladder $5.99 notional itself still reached to_place and,
            # from there, a real placement (Codex review: "Refuse
            # inconsistent multiplier and notional pairs"). Also flags a
            # candidate with NO usable multiplier at all whose amount_usd
            # does not match any configured base x ladder-multiplier product
            # (Codex review: "Reject amounts that match no ladder
            # allocation"). Checked AFTER the finite/positive, per-order-cap,
            # and cent-precision checks -- an already-invalid, already-over-
            # cap, or already-non-cent amount is refused for that more
            # fundamental reason first.
            plan.refused.append((ticker, REFUSED_INCONSISTENT_MULTIPLIER))
            continue

        # Marked "seen" only now, once this candidate has cleared every
        # per-candidate check (whitelist, finite/positive amount, per-order
        # cap) and is about to compete for aggregate cap headroom -- not the
        # instant its ticker is first observed. A higher-priority malformed
        # duplicate (e.g. Candidate("AAPL", NaN)) would otherwise claim the
        # ticker in `seen` before its own amount was validated, silently
        # discarding a later, valid same-ticker candidate as "already seen"
        # instead of refusing the malformed one and placing the valid one
        # (Codex review: "Validate duplicate candidates before marking them
        # seen").
        seen.add(ticker)

        prospective = running + cand.amount_usd
        # Order-count cap, config/budgets.json circuit_breakers.max_orders_per_day.
        # Checked FIRST: it is an absolute count ceiling independent of notional,
        # so a day already at the cap must stop the batch even when dollar
        # headroom remains. Like the dollar caps it stops rather than back-fills,
        # so the cap cannot depend on candidate ordering.
        if snapshot.day_order_count + len(plan.to_place) + 1 > MAX_ORDERS_PER_DAY:
            stop_reason = SKIPPED_DAILY_ORDER_COUNT_CAP
        elif snapshot.day_notional_usd + prospective > tier.daily_cap_usd:
            stop_reason = SKIPPED_DAILY_CAP
        elif snapshot.week_notional_usd + prospective > effective_weekly_cap:
            stop_reason = SKIPPED_WEEKLY_CAP
        elif buying_power_usd is not None and prospective > buying_power_usd:
            stop_reason = SKIPPED_INSUFFICIENT_BUYING_POWER

        if stop_reason is not None:
            plan.skipped.append((ticker, stop_reason))
            continue

        plan.to_place.append(PlannedOrder(ticker=ticker, amount_usd=cand.amount_usd))
        running = prospective

    return plan


# ---------------------------------------------------------------------------
# Gate 7 -- placement, behind the second gate
# ---------------------------------------------------------------------------


@dataclass
class _RunnerBroker:
    """Thin holder for the injected placement callable and the two live gates.

    Mirrors the two-gate design in ``dca_live_adapters``/``near_close_dca``: a
    wired ``place_fn`` alone is not enough. ``dry_run`` is the FIRST gate
    (mirrors ``StrategyConfig.dry_run``, defaults True) and ``enable_live_orders``
    is the SECOND (defaults False); both must be flipped before ``execute_plan``
    submits anything. Carrying only ``enable_live_orders`` here let a caller
    flip that one switch and place real orders even while the strategy
    configuration stayed at its default ``dry_run=True`` (Codex review:
    "Preserve the StrategyConfig dry-run placement gate").

    ``account_number`` is the pinned Robinhood account every order is placed
    against, and is threaded into every ``place_fn`` call rather than trusted
    to a caller default (``docs/reliability_spec.md`` requires the account to
    be pinned).
    """

    place_fn: object
    enable_live_orders: bool = False
    dry_run: bool = True
    account_number: str | None = None


def build_runner_broker(
    place_fn,
    enable_live_orders: bool = False,
    *,
    dry_run: bool = True,
    account_number: str | None = None,
) -> _RunnerBroker:
    """Wrap an injected placement callable. Both live gates default to OFF."""
    return _RunnerBroker(
        place_fn=place_fn,
        enable_live_orders=enable_live_orders,
        dry_run=dry_run,
        account_number=account_number,
    )


@dataclass
class ExecutionResult:
    """What ``execute_plan`` actually did, from ``get_equity_orders`` responses,
    never from intent: ``submitted`` lists tickers placed successfully, in
    order; ``halted_on`` is the ticker whose placement failed and stopped the
    batch, or ``None`` if nothing halted; ``failure_reason`` is why it halted
    (a connector exception, a missing order id, an explicit rejection, or a
    terminal state -- see ``_placement_failure_reason``), or ``None`` if
    nothing halted, so Gate 9's alert and the reconciled artifact don't have
    to repeat placement or retain out-of-band data to explain a halt (Codex
    review: "Preserve the placement failure reason"); ``placement_enabled``
    reports whether both live gates were flipped, so a dry run and a real
    submission are never confused.
    """

    submitted: list[str] = field(default_factory=list)
    halted_on: str | None = None
    failure_reason: str | None = None
    placement_enabled: bool = False


# Broker response states that mean "no live order actually exists". Sourced
# from near_close_dca's canonical terminal-state set rather than re-declared
# here: a hand-copied set had previously omitted "expired" in two other
# places in this repo before being consolidated to one source of truth, and
# a third hand-copy here would silently repeat that (Codex review: "Treat
# expired placement responses as terminal"). Without "expired", a response
# carrying a valid order id and state="expired" reads as a live order and
# the batch continues even though that order can never fill.
_DEAD_PLACEMENT_STATES = _CANONICAL_TERMINAL_ORDER_STATES

# The maximum age a placement-time `orders` fetch may be, in seconds, before
# execute_plan() refuses to trust it for the recomputed aggregate cap checks.
# The iterable itself carries no fetch timestamp or completeness marker, and
# `snapshot.day`/`trading_day` prove only the calendar date, not how recently
# the history was actually pulled -- a same-account list fetched earlier in
# the day and reused after another slot or a manual buy consumes the
# remaining cap would let `verified_snapshot` silently report a stale, too-low
# total (Codex review: "Bind cap checks to a fresh history snapshot"). Set
# well above ordinary single-call tool latency but tight enough to catch a
# genuinely stale, reused fetch.
MAX_ORDERS_FETCH_AGE_SECONDS = 120


# Matches a run of 20+ token-shaped characters (alphanumeric plus the
# separators common to base64/hex/JWT encodings) with no whitespace --
# a shape ordinary human-readable failure text ("insufficient funds") never
# takes, but an access token, API key, or other credential a broker wrapper
# mistakenly embedded in an error string typically does.
_SECRET_SHAPED_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_.=\-]{20,}")


def _redacted_envelope_error(envelope: dict) -> str | None:
    """Return ``envelope["error"]``, redacted, or None if absent/falsy.

    The account-number-specific replace applied downstream (where
    ``broker.account_number`` is known) only catches the ONE secret this
    module can identify for certain; a broker response's free-text error
    field is otherwise arbitrary external data that can embed an access
    token, request headers, or other connector credentials, which would
    otherwise be persisted/alerted verbatim (Codex review: "Redact
    non-account secrets from broker response errors"). A non-string error
    value is reported only by its type, never serialized, since a nested
    object could carry far more than a short human-readable reason.
    """
    error = envelope.get("error")
    if not error:
        return None
    if not isinstance(error, str):
        return f"<non-string error field, type={type(error).__name__}>"
    return _SECRET_SHAPED_TOKEN_RE.sub("[REDACTED-TOKEN]", error)


def _envelope_failure(envelope: dict) -> str | None:
    """Return an explicit failure reason from one response envelope, or None.

    A present status key must carry the literal boolean ``True`` to count as
    success; anything else -- ``False``, or a same-ish-looking but
    non-boolean value like ``0``, ``""``, or ``"false"`` -- is treated as a
    reported failure. An identity check against ``False`` alone would let a
    wrapper that encodes "not successful" as ``0``/``""``/``"false"`` instead
    of the JSON boolean pass straight through and have the batch continue as
    if the broker had confirmed success (Codex review: "Reject non-boolean
    failure flags").

    The MCP bridge's explicit ``isError`` envelope for a network/bridge
    failure is checked first, independent of ``success``/``ok``/``approved``:
    ``dca_live_adapters._is_error_response()`` already treats ``isError is
    True`` as a definitive failure signal for every other MCP response this
    repo parses, even when its own ``error`` message is absent. This
    placement-response path never checked it, so a response shaped like
    ``{"isError": true, "id": "request-id"}`` -- carrying none of
    ``success``/``ok``/``approved``/``error`` -- passed straight through
    every check here and could be recorded as a successful submission even
    though no broker order was ever created (Codex review: "Reject MCP
    isError placement envelopes"). Strict identity, not truthiness: a
    non-boolean ``isError`` (e.g. the string ``"true"``) is not this
    canonical bridge-failure signal and falls through to the checks below,
    mirroring ``_is_error_response()``'s own strict ``is True`` check.
    """
    if envelope.get("isError") is True:
        error_text = _redacted_envelope_error(envelope)
        if error_text is not None:
            return f"broker response reported isError=True: {error_text}"
        return "broker response reported isError=True (unknown network/bridge error)"
    for key in ("success", "ok", "approved"):
        if key in envelope:
            value = envelope[key]
            if not isinstance(value, bool) or value is False:
                # A malformed success/ok/approved flag can be arbitrary
                # connector data -- a dict carrying an access token or
                # account identifier, for instance -- rather than the
                # boolean-ish scalar (bool/int/float/str/None) every
                # established response shape in this repo uses. Only a
                # scalar value is embedded as-is; anything else is reported
                # by type, matching the redaction already applied to every
                # other malformed broker field this failure path converts
                # into evidence (Codex review: "Redact malformed placement
                # status flags").
                # A string flag is not itself a bool/int/float/None, so it
                # is not automatically safe: a connector could accidentally
                # place an access token or account identifier in this
                # field. Scrubbed the same way a string error/side/dollar_amount
                # already is, rather than treating every scalar type as
                # safe (Codex review: "Redact string-valued placement
                # status flags").
                if isinstance(value, str):
                    flag_repr = repr(_SECRET_SHAPED_TOKEN_RE.sub("[REDACTED-TOKEN]", value))
                elif isinstance(value, (bool, int, float, type(None))):
                    flag_repr = repr(value)
                else:
                    flag_repr = f"<{type(value).__name__}>"
                error_text = _redacted_envelope_error(envelope)
                if error_text is not None:
                    return f"broker response reported {key}={flag_repr}: {error_text}"
                return f"broker response reported {key}={flag_repr}"
    error_text = _redacted_envelope_error(envelope)
    if error_text is not None:
        return f"broker response reported error: {error_text}"
    return None


def _placement_failure_reason(
    response: object,
    *,
    expected_symbol: str,
    expected_side: str,
    expected_amount_usd: float,
    expected_ref_id: str | None = None,
    account_number: str | None = None,
) -> str | None:
    """Return why ``response`` means "no order was actually placed", or None.

    A broker MCP wrapper can report failure as ordinary return data instead of
    raising -- e.g. ``{"success": false, ...}`` or an order with
    ``state="rejected"``. Trusting any non-raising return as success would
    record the ticker as submitted and keep going, when nothing was actually
    placed (Codex review: "Reject failure-shaped placement responses").

    The OUTER envelope is checked before unwrapping: a response shaped like
    ``{"success": false, "data": {"order": {"id": "ORD", "state": "queued"}}}``
    has a perfectly healthy-looking nested order, so checking only the
    unwrapped ``data`` would discard the outer failure and read this as a
    success (Codex review: "Check outer placement failures before
    unwrapping").

    A non-dict response (``None``, a string, or anything else the wrapper
    might return without raising) is treated as a failure rather than a
    success: reading it as "no failure reason found" would record the ticker
    as submitted with no order id or confirmation that it exists (Codex
    review: "Fail closed on non-dictionary placement responses").

    ``expected_symbol``/``expected_side`` are compared against any ``symbol``/
    ``instrument_symbol``/``side`` the response actually carries -- only when
    present, since no established response shape in this repo guarantees
    them. A stale or mismatched envelope (e.g. a wrapper returning an MSFT
    sell confirmation while AAPL was requested) would otherwise pass every
    check above on order id and state alone and be recorded as a successful
    AAPL buy (Codex review: "Halt when the broker returns a mismatched
    order").

    ``expected_amount_usd`` is compared against a returned ``dollar_amount``
    the same way, when present: this module enforces every per-order,
    daily, weekly, and buying-power ceiling against the REQUESTED amount,
    but the response is what proves what the broker actually processed --
    e.g. $100 processed for a requested $2 buy can exceed every one of
    those ceilings, and recording it as a clean submission would hide the
    overrun entirely (Codex review: "Verify the returned order notional").

    ``expected_ref_id`` -- this call's own
    ``dca-runner-<slot_id>-<trading_day>-<ticker>`` sent to ``place_fn`` --
    is compared against any of ``ref_id``,
    ``client_order_id``, ``client_order_uuid``, or ``client_id`` the
    response carries (this repo's broker adapter already recognizes all
    four as the client reference, see ``dca_live_adapters.py``'s
    ``same_day_strategy_order_exists_with_ref_id_prefix``). A cached or
    stale response can carry a valid order id and matching symbol/side/
    amount for a PRIOR request while actually confirming that other
    request, not this one -- comparing only symbol/side/amount would let
    that prior order be mistaken for confirmation of this submission and
    the batch would continue as if this order were placed (Codex review:
    "Verify a returned client reference before recording success"). Checked
    only when the response actually carries one of these fields, matching
    every other optional cross-check above: no established response shape
    in this repo guarantees any of them.
    """
    if not isinstance(response, dict):
        return f"broker response was not a dict (got {type(response).__name__})"
    outer_failure = _envelope_failure(response)
    if outer_failure is not None:
        return outer_failure
    if "data" in response:
        # Key presence, not a truthy/dict fallback: `response.get("data") if
        # isinstance(...) else response` would otherwise treat an explicitly
        # non-dict "data" (e.g. {"data": null, "id": "REQUEST", ...}) as
        # though the key were simply absent, and silently search the
        # enclosing envelope for an id instead -- reading a malformed nested
        # container as a healthy unnested response and recording an id that
        # may only identify the wrapper's own REQUEST, not a confirmed order
        # (Codex review: "Reject malformed nested placement containers").
        if not isinstance(response["data"], dict):
            return f"broker response 'data' field is not a dict (got {type(response['data']).__name__})"
        data = response["data"]
    else:
        data = response
    inner_failure = _envelope_failure(data)
    if inner_failure is not None:
        return inner_failure
    if "order" in data:
        if not isinstance(data["order"], dict):
            return f"broker response 'order' field is not a dict (got {type(data['order']).__name__})"
        order = data["order"]
    else:
        order = data
    # Checked before the order id itself: a wrapper reporting an explicit
    # failure INSIDE the unwrapped order (e.g. {"success": false, "error":
    # "denied", "id": "ORD", "state": "queued"}) still carries a truthy id
    # and a nonterminal state, so without this the checks below would read
    # it as a healthy submission and record the ticker as placed despite the
    # broker reporting failure (Codex review: "Inspect order-level failure
    # envelopes").
    order_failure = _envelope_failure(order)
    if order_failure is not None:
        return order_failure
    order_id = order.get("id") or order.get("order_id")
    if (
        not order_id
        or isinstance(order_id, bool)
        or not isinstance(order_id, (str, int, float))
    ):
        # A bare truthiness check would accept {"id": true} -- or a
        # container like {"id": ["ORD"]}/{"id": {"value": "ORD"}} -- as
        # proof an order exists, when none of those are a usable identifier
        # at all: a malformed response could then be recorded as a
        # successful submission with no real broker order id backing it
        # (Codex review: "Reject boolean placement order IDs", "Reject
        # non-scalar placement order IDs").
        return "broker response carried no order id"
    if isinstance(order_id, str) and not order_id.strip():
        # A whitespace-only string is truthy, so the check above alone would
        # accept {"id": "   "} as a usable identifier when it plainly is not
        # (Codex review: "Reject blank and non-finite placement IDs").
        return "broker response carried a blank order id"
    if isinstance(order_id, (int, float)) and not math.isfinite(order_id):
        # NaN and infinity are also truthy, so the same gap applies to a
        # numeric id: neither is a real broker-assigned identifier (Codex
        # review: "Reject blank and non-finite placement IDs").
        return "broker response carried a non-finite order id"
    if isinstance(order_id, (int, float)) and order_id <= 0:
        # A finite negative (or zero) numeric id is also truthy and passes
        # every check above -- e.g. a wrapper using -1 as a sentinel for "no
        # order id was actually assigned" -- which is exactly as unusable as
        # NaN/infinity despite being finite (Codex review: "Reject negative
        # numeric placement IDs").
        return "broker response carried a non-positive order id"
    if expected_ref_id is not None:
        # Key presence, not truthiness: an `or` chain of order.get(...) calls
        # would treat a present-but-falsy reference ("", 0, False) as though
        # the field were entirely absent and cascade to the next one,
        # letting a wrapper that explicitly returned no usable idempotency
        # key for THIS request still pass this check via an unrelated
        # fallback field (Codex review: "Reject falsy returned client
        # references"). Mirrors the symbol check's own key-presence idiom
        # below -- except a present ``None`` value is NOT treated the same
        # as an absent key here: a wrapper returning ``{"ref_id": null}``
        # (or that key entirely) still carries no usable idempotency
        # confirmation, and skipping the check for it (as the symbol check's
        # own idiom does for a present-but-null symbol) would let a
        # cached/stale response pass this check via that explicit null
        # (Codex review: "Reject null returned client references").
        if "ref_id" in order:
            returned_ref_id, ref_id_present = order["ref_id"], True
        elif "client_order_id" in order:
            returned_ref_id, ref_id_present = order["client_order_id"], True
        elif "client_order_uuid" in order:
            returned_ref_id, ref_id_present = order["client_order_uuid"], True
        elif "client_id" in order:
            returned_ref_id, ref_id_present = order["client_id"], True
        else:
            returned_ref_id, ref_id_present = None, False
        # Only checked when the response actually carries a client
        # reference KEY (present, even if its value is null): a cached/stale
        # response can otherwise pass every symbol/side/amount check above
        # (a prior order for the same ticker, side, and notional) while
        # confirming a DIFFERENT request entirely (Codex review: "Verify a
        # returned client reference before recording success").
        if ref_id_present and str(returned_ref_id) != expected_ref_id:
            # A mismatched client reference can be arbitrary connector data
            # -- a dict or object carrying a token or other request
            # metadata, not just a stale string -- rather than a real
            # idempotency key: str(returned_ref_id) is only used for the
            # comparison above, so a non-string value reaches here
            # unexamined. execute_plan() copies this failure string into
            # ExecutionResult.failure_reason, which is persisted/alerted, so
            # only a plain string mismatch (useful diagnostic text, e.g. a
            # stale ref_id) is embedded; anything else is reported only by
            # type (Codex review: "Redact mismatched client references
            # before recording failures").
            if isinstance(returned_ref_id, str):
                redacted_ref_id = _SECRET_SHAPED_TOKEN_RE.sub("[REDACTED-TOKEN]", returned_ref_id)
                if account_number:
                    redacted_ref_id = redacted_ref_id.replace(account_number, "[REDACTED-ACCOUNT]")
                return (
                    f"broker response client reference {redacted_ref_id!r} does not match "
                    f"the requested order's {expected_ref_id!r}"
                )
            return (
                f"broker response client reference does not match the requested order's "
                f"{expected_ref_id!r} (returned type={type(returned_ref_id).__name__})"
            )
    # Key presence, not truthiness: `order.get("symbol") or order.get(...)`
    # would otherwise read a present-but-falsy malformed value (`""`, `0`,
    # `False`) as "no symbol reported" and skip this check entirely, letting
    # a response that did not actually identify the created order pass as if
    # the broker had confirmed nothing about symbol identity at all (Codex
    # review: "Reject falsy returned symbols").
    if "symbol" in order:
        returned_symbol, symbol_present = order["symbol"], True
    elif "instrument_symbol" in order:
        returned_symbol, symbol_present = order["instrument_symbol"], True
    else:
        returned_symbol, symbol_present = None, False
    if symbol_present:
        # A present key with an explicit null value is NOT the same as the
        # key being absent: `if returned_symbol is not None` alone would
        # skip this check for {"symbol": null}, letting a valid-looking
        # order id/state/amount/client-reference alone be recorded as a
        # successful submission without ever confirming the returned order
        # is for the requested ticker -- the same gap already closed for a
        # present null client reference (Codex review: "Reject a present
        # null placement symbol").
        if returned_symbol is None:
            return "broker response carried a null symbol"
        try:
            normalized_returned_symbol = _normalize_ticker(str(returned_symbol))
        except ValueError:
            return f"broker response symbol is not usable (type={type(returned_symbol).__name__})"
        if normalized_returned_symbol != expected_symbol:
            # A malformed or mismatched broker response's "symbol" can be
            # arbitrary connector data -- a dict carrying an access token
            # or request headers, for instance -- rather than a real
            # ticker: str(returned_symbol) here only needs to be nonempty
            # to reach this branch, not actually ticker-shaped, so its full
            # repr was previously embedded in this failure string, which
            # execute_plan() copies into ExecutionResult.failure_reason and
            # persists/alerts. A genuine string mismatch (e.g. "MSFT"
            # returned for a requested "AAPL") is not secret-shaped and
            # stays useful diagnostic text; anything else is reported only
            # by type (Codex review: "Redact returned symbols before
            # storing failure evidence").
            if isinstance(returned_symbol, str):
                redacted_symbol = _SECRET_SHAPED_TOKEN_RE.sub("[REDACTED-TOKEN]", returned_symbol)
                if account_number:
                    redacted_symbol = redacted_symbol.replace(account_number, "[REDACTED-ACCOUNT]")
                return (
                    f"broker response symbol {redacted_symbol!r} does not match the requested "
                    f"order's {expected_symbol!r}"
                )
            return (
                f"broker response symbol does not match the requested order's "
                f"{expected_symbol!r} (returned type={type(returned_symbol).__name__})"
            )
    # Key presence, not truthiness: `order.get("side")` would otherwise read
    # a present-but-null side ({"side": null}) as though the field were
    # entirely absent and skip this check, letting a valid-looking
    # id/reference/symbol/amount response alone be recorded as a successful
    # buy with no usable confirmation the returned order is a buy at all,
    # rather than a stale or mismatched order (Codex review: "Reject a
    # present null placement side").
    if "side" in order:
        returned_side = order["side"]
        if returned_side is None:
            return "broker response carried a null side"
        # Stripped as well as lowercased, mirroring the terminal-state check
        # below: a whitespace-padded " buy " is a real accepted order, but
        # the unstripped comparison would treat it as a mismatch, halt the
        # batch, and drop the real order out of `submitted` (Codex review:
        # "Normalize the returned placement side before comparison").
        if str(returned_side).strip().lower() != expected_side:
            # A malformed or mismatched broker response's "side" can be
            # arbitrary connector data -- a dict carrying an access token,
            # account identifier, or request headers, for instance --
            # rather than a real side string: str(returned_side) only
            # needs to differ from expected_side to reach this branch, not
            # actually be a plausible side. A genuine string mismatch is
            # not itself secret-shaped, but is still scrubbed of any
            # token-shaped substring for defense in depth; anything else
            # is reported only by type (Codex review: "Redact returned
            # sides before storing failures").
            if isinstance(returned_side, str):
                return (
                    "broker response side "
                    f"{_SECRET_SHAPED_TOKEN_RE.sub('[REDACTED-TOKEN]', returned_side)!r} "
                    f"does not match the requested order's {expected_side!r}"
                )
            return (
                f"broker response side does not match the requested order's "
                f"{expected_side!r} (returned type={type(returned_side).__name__})"
            )
    # Key presence, not truthiness: a present-but-null dollar_amount
    # ({"dollar_amount": null}) is not the same as the field being omitted
    # -- if the wrapper returned null because it could not confirm the
    # processed amount, a larger broker notional (and therefore a per-order
    # or aggregate cap overrun) could be hidden behind the remaining fields
    # alone (Codex review: "Reject a present null returned notional").
    if "dollar_amount" in order:
        returned_amount = order["dollar_amount"]
        if returned_amount is None:
            return "broker response carried a null dollar_amount"
        if isinstance(returned_amount, bool):
            # Checked BEFORE float(): float(True) succeeds as 1.0, so a
            # response carrying dollar_amount=true would otherwise be
            # accepted for a requested $1 order despite the broker
            # supplying no numeric confirmation of the processed amount
            # (Codex review: "Reject boolean returned notionals before
            # conversion").
            return f"broker response dollar_amount {returned_amount!r} is not a number"
        try:
            returned_amount = float(returned_amount)
        except (TypeError, ValueError):
            # A nonnumeric dollar_amount can be arbitrary connector data --
            # a dict or token-bearing string, for instance -- rather than a
            # malformed but plain numeric-looking value. A genuine string
            # is scrubbed of any token-shaped substring for defense in
            # depth; anything else is reported only by type (Codex review:
            # "Redact malformed returned notionals").
            if isinstance(returned_amount, str):
                return (
                    "broker response dollar_amount "
                    f"{_SECRET_SHAPED_TOKEN_RE.sub('[REDACTED-TOKEN]', returned_amount)!r} "
                    "is not a number"
                )
            return (
                "broker response dollar_amount is not a number "
                f"(type={type(returned_amount).__name__})"
            )
        # A tolerance of a full cent (0.01) accepted an exact one-cent
        # mismatch (e.g. $10.01 returned for a requested $10.00) as a match,
        # even though that one cent can be exactly what pushed the order over
        # the per-order/daily/weekly/buying-power ceilings this module
        # enforced against the REQUESTED amount (Codex review: "Reject a
        # one-cent returned-notional mismatch"). Amounts are cent-rounded
        # strings on the wire, so a tolerance far below half a cent still
        # absorbs float noise (e.g. 1.9999999999998 vs 2.0) without accepting
        # any real cent-level difference.
        if not math.isfinite(returned_amount) or abs(returned_amount - expected_amount_usd) > 0.001:
            return (
                f"broker response dollar_amount {returned_amount!r} does not match the "
                f"requested order's ${expected_amount_usd:.2f}"
            )
    if "state" in order:
        raw_state = order["state"]
        # Key presence, not a blanket str() coercion: str(False)/str([])/
        # str({"value": "rejected"}) all produce a value that is not in
        # _DEAD_PLACEMENT_STATES, so a present-but-malformed state would
        # otherwise read as a healthy, non-terminal order and be recorded as
        # submitted even though the wrapper never provided a usable state at
        # all (Codex review: "Reject non-string placement states"). An
        # OMITTED state key is unaffected -- there is nothing to distinguish
        # from a healthy default.
        if not isinstance(raw_state, str):
            # raw_state's full repr was previously embedded here, but this
            # failure reason is exactly what execute_plan() records as
            # ExecutionResult.failure_reason and persists/alerts -- and a
            # malformed broker response's "state" field can be arbitrary
            # connector data (an object carrying an access token, account
            # identifier, or request headers) rather than a real order
            # state string. _redacted_envelope_error() never processes
            # order metadata like this, and the downstream
            # account-number-specific replace only catches that one known
            # secret, so only a safe classification (the value's type) is
            # reported here, matching the redaction already applied to
            # every other malformed broker field (Codex review: "Redact
            # malformed placement states before recording them").
            return f"broker response carried a non-string order state (type={type(raw_state).__name__})"
        # Stripped as well as lowercased: a whitespace-padded " rejected " or
        # "cancelled\t" would otherwise fail this membership test and be
        # recorded as a successful submission despite the broker rejecting
        # or cancelling the order (Codex review: "Strip returned order
        # states before terminal-state checks").
        state = raw_state.strip().lower()
        if not state:
            # A present "state" key whose value is blank or whitespace-only
            # (e.g. {"state": "   "}) normalizes to "", which is not in
            # _DEAD_PLACEMENT_STATES and would otherwise read exactly like
            # the OMITTED-key case just above -- a healthy default -- and be
            # recorded as submitted even though the wrapper supplied the key
            # but no usable value at all. Failing closed here mirrors the
            # non-string check just above: a KEY THAT IS PRESENT must supply
            # something usable (Codex review: "Reject blank placement
            # states").
            return f"broker response carried a blank order state: {raw_state!r}"
        if state in _DEAD_PLACEMENT_STATES:
            return f"order came back in terminal state {state!r}"
        if state not in _CANONICAL_LIVE_ORDER_STATES:
            # A present, nonblank state that is neither a recognized dead
            # state NOR one of dca_live_adapters._LIVE_ORDER_STATES (e.g.
            # "error", "denied", or an unrecognized "mystery") fell through
            # this far as a healthy default: every nonterminal string was
            # treated as proof a live order exists, when an unrecognized
            # state is untrustworthy evidence, not confirmation of a real
            # submission (Codex review: "Require a recognized live
            # placement state"). Confirmed a string by this point, but a
            # malformed/hostile broker response could still set it to
            # something token-shaped; _redacted_envelope_error() only ever
            # processes envelope["error"], never order metadata like this
            # field, so the same token-shaped redaction is applied directly
            # here too (Codex review: "Redact malformed placement states
            # before recording them").
            return (
                "broker response carried an unrecognized order state: "
                f"{_SECRET_SHAPED_TOKEN_RE.sub('[REDACTED-TOKEN]', state)!r}"
            )
    return None


# Matches an ISO-shaped YYYY-MM-DD substring anywhere in a caller-supplied
# run_id. A plain `trading_day.isoformat() in run_id` substring test accepts
# a run_id carrying MORE THAN ONE date (e.g. "batch-2026-07-28-2026-07-29"),
# which passes on every one of those days even though the derived per-ticker
# ref_id is identical across them -- see the check below (Codex review:
# "Require a single-day idempotency key").
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _now_fn_clock_failure(
    now_fn: Callable[[], datetime],
    trading_day: date,
    slot: SlotSpec,
    slot_id: str,
    orders_fetched_at: datetime,
) -> str | None:
    """Sample ``now_fn()`` and return why placement must halt now, or None.

    ``now_fn`` is an injected callable (e.g. wrapping a system clock or an
    MCP-backed time source) that can raise like any other injected callable.
    Uncaught, that would propagate past ``execute_plan()`` entirely instead
    of the documented ``halted_on``/``failure_reason`` result, denying the
    caller a reconcilable ``ExecutionResult`` for a batch where an earlier
    order in the same loop may already have been submitted and is real,
    committed spend (Codex review: "Preserve partial results when the fresh
    clock fails").

    The order-history freshness check (``orders_fetched_at`` within
    ``MAX_ORDERS_FETCH_AGE_SECONDS`` of "now") is rerun against THIS same
    fresh reading too, not just the stale pre-loop ``now``: an empty history
    fetched at, say, 09:31 and checked once against a pre-loop ``now`` of
    09:31 stays "fresh" for the rest of the batch even once the real clock
    (as ``now_fn()`` reports before a later order) has moved to 09:59,
    letting nearly half an hour of intervening manual or scheduled buys go
    unrepresented in the duplicate/aggregate-cap checks that same stale
    history feeds (Codex review: "Measure history freshness against the
    fresh placement clock").
    """
    try:
        fresh_now = now_fn()
    except Exception as exc:
        # exc's own message is wrapper-defined and can embed an account
        # identifier, a token, or other connector context, and this string
        # is explicitly sent to alerts and durable run evidence via
        # ExecutionResult.failure_reason -- the same class of leak already
        # closed for place_fn's own raised exceptions. Only a safe
        # classification, the exception's type name, is recorded here;
        # never its message (Codex review: "Redact exceptions raised by the
        # placement clock").
        return f"now_fn raised {type(exc).__name__}"
    if not isinstance(fresh_now, datetime) or fresh_now.tzinfo is None:
        # fresh_now is whatever the injected now_fn wrapper returned, which
        # can be a connector response (e.g. a dict) carrying an account
        # identifier or token rather than a real datetime; interpolating it
        # verbatim would leak that into ExecutionResult.failure_reason, the
        # same class of leak already closed for now_fn's raised exceptions
        # above. Only the type name is reported, matching assess()'s own
        # non-datetime clock path (Codex review: "Redact malformed values
        # returned by the placement clock").
        return f"now_fn returned a value that is not a timezone-aware datetime (got {type(fresh_now).__name__})"
    if fresh_now.astimezone(ET).date() != trading_day:
        return (
            f"now_fn returned {fresh_now.isoformat()!r}, which falls on ET calendar day "
            f"{fresh_now.astimezone(ET).date().isoformat()!r}, not "
            f"trading_day={trading_day.isoformat()!r}"
        )
    if not gate_time_sanity(fresh_now, slot):
        return (
            f"now_fn reports the {slot_id!r} slot's "
            f"{slot.window_start_et[0]:02d}:{slot.window_start_et[1]:02d}-"
            f"{slot.window_end_et[0]:02d}:{slot.window_end_et[1]:02d} ET acceptance "
            "window has closed before this order could be placed"
        )
    orders_age_seconds = (fresh_now - orders_fetched_at).total_seconds()
    if orders_age_seconds < 0 or orders_age_seconds > MAX_ORDERS_FETCH_AGE_SECONDS:
        return (
            f"now_fn reports orders_fetched_at is {orders_age_seconds:.1f}s away from the "
            f"fresh clock, outside the {MAX_ORDERS_FETCH_AGE_SECONDS}s freshness window; "
            "refetch order history before continuing this batch"
        )
    return None


def execute_plan(
    plan: Plan,
    broker: _RunnerBroker,
    *,
    run_id: str | None = None,
    snapshot: BudgetSnapshot | None = None,
    tier: BudgetTier | None = None,
    trading_day: date | None = None,
    orders=None,
    weekly_cap_usd: float | None = None,
    weekly_rollover_reserve_usd: float | None = None,
    weekly_rollover_reserve_week_key: tuple[int, int] | None = None,
    finalized_budget_days=None,
    submitted_notional_by_week: dict | None = None,
    buying_power_usd: float | None = None,
    orders_account_number: str | None = None,
    now: datetime | None = None,
    slot_id: str | None = None,
    orders_fetched_at: datetime | None = None,
    now_fn: Callable[[], datetime] | None = None,
    candidates=None,
) -> ExecutionResult:
    """Submit the plan one order at a time, buys only, stopping on first error.

    Sequential by design: MCP client sessions are commonly not thread-safe, and
    a parallel submit makes the sole-executor interlock meaningless.

    ``now_fn`` is REQUIRED whenever placement is enabled, and is called to
    sample a FRESH clock reading immediately before every single
    ``place_fn`` call, rerunning the same market-day/time-window/trading-
    window gates already checked once against ``now`` above. A batch with
    more than one ticker, or a slow ``place_fn`` call, can otherwise
    straddle the slot's acceptance window: the single pre-loop ``now`` check
    only proves the window was open when the BATCH started, not when each
    individual order is actually submitted, so an order accepted at 15:59:59
    could still submit a later order in the same batch after 16:00, past the
    regular-hours-only gate (Codex review: "Recheck the clock before each
    placement"). Making it required rather than merely accepted closes this
    for every live placement instead of leaving it an opt-in a caller could
    forget (Codex review: "Require a fresh clock for every live placement").
    The window closing mid-batch halts the remainder exactly like a
    placement failure does -- ``result.submitted`` still reports every order
    already placed before the window closed -- rather than raising, since
    orders already submitted in this batch are real, already-committed spend
    that must not be hidden by an exception. ``now_fn`` raising itself (an
    injected callable can fail like any other) is treated the same way,
    converting into ``halted_on``/``failure_reason`` instead of propagating
    (Codex review: "Preserve partial results when the fresh clock fails").

    Both gates on ``broker`` must be flipped (``dry_run=False`` AND
    ``enable_live_orders=True``) before anything is submitted.

    A rejection -- whether raised or a failure-shaped response, see
    ``_placement_failure_reason`` -- halts the entire batch. No retry, and no
    attempt at the remainder: a broker refusing one order is evidence the run's
    assumptions are stale, not an invitation to keep going.

    ``run_id`` is REQUIRED whenever placement is enabled and must literally
    contain ``trading_day.isoformat()``, establishing that the caller is
    scoped to the correct run before anything is submitted (Codex review:
    "Require a run-scoped idempotency key for live placement"; "Bind the
    idempotency key to the trading day"). The per-order ``ref_id`` actually
    sent to ``place_fn``, however, is derived from ``slot_id`` and
    ``trading_day`` directly -- ``f"dca-runner-{slot_id}-{trading_day.isoformat()}-{ticker}"``
    -- NOT from ``run_id``'s own string: two overlapping invocations of the
    SAME slot can each pass a different caller-generated ``run_id`` prefix
    that both merely contain today's date, and since the sole-executor
    check is not atomic (both can fetch the same still-empty history before
    either placement becomes visible), keying ``ref_id`` off ``run_id``
    itself would let each derive a different idempotency key for the same
    ticker/slot/day, defeating broker-side deduplication and letting both
    submit the same ticker. Deriving directly from ``slot_id``/
    ``trading_day`` instead guarantees any two concurrent, legitimate
    invocations produce the IDENTICAL ``ref_id`` regardless of ``run_id``
    (Codex review: "Bind the idempotency key to the slot").

    ``place_fn`` is called with exactly the documented injected-callable
    signature (``account_number, symbol, side, type, dollar_amount, ref_id,
    time_in_force`` -- see ``dca_live_adapters.RobinhoodBroker``): no extra
    keyword is added, since a strictly-typed wrapper implementing that exact
    signature would raise ``TypeError`` on an unexpected one, and this
    function's blanket ``except Exception`` would silently read that as a
    per-ticker rejection instead of a wiring bug (Codex review: "Match the
    documented placement callable signature").

    ``broker.account_number`` must be exactly the environment-configured
    ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` before the first placement. Omitting
    it, or wiring any other accessible account, is refused rather than
    forwarded as-is: an unset value can make every live submission fail
    against the wrong (or no) account, while a different valid account number
    would place real buys somewhere other than the authorized account (Codex
    review: "Verify the authorized account before placing orders").

    Every order's ticker and amount are revalidated here too, not just its
    side: ``Plan``/``PlannedOrder`` are public and constructible independently
    of ``plan_orders``, so e.g. ``Plan(to_place=[PlannedOrder("BYDDY",
    1000.0)])`` could otherwise reach ``place_fn`` despite bypassing the
    active-ticker whitelist and per-order cap entirely (Codex review:
    "Revalidate buy plans at the placement boundary"). A non-cent amount
    (e.g. ``$9.996``) is refused outright too, not quantized: every cap and
    buying-power comparison below uses the raw float, but placement sends
    ``f"{order.amount_usd:.2f}"`` -- the rounded cent value the broker
    actually receives -- so an unrounded amount could pass every check here
    while the broker is asked to place a cent-rounded amount that exceeds a
    confirmed ceiling by the rounding delta (Codex review: "Apply caps to
    the rounded notional sent to the broker").

    ``snapshot`` and ``tier`` are REQUIRED whenever placement is enabled --
    pass the same values used to build ``plan`` via ``plan_orders``/``assess``.
    ``tier``'s caps are validated (finite, non-negative) before being
    trusted. Per-order revalidation alone still lets a caller-built ``Plan``
    bypass every AGGREGATE cap: e.g. 45 distinct whitelisted tickers at $10
    each each pass the per-order checks individually but total $450, over
    the $360 Stress-tier daily maximum, with prior broker spend never
    considered (Codex review: "Reapply aggregate caps to caller-built
    plans"). The AGGREGATE comparison itself uses a snapshot recomputed HERE
    from ``orders`` (see below), never ``snapshot``'s own
    ``day_notional_usd``/``week_notional_usd`` directly -- ``snapshot`` is
    still required so its ``.day`` can be checked against ``trading_day``,
    but trusting its notionals blindly would defeat the point of also
    requiring fresh ``orders`` (Codex review: "Recompute spend from
    placement-time order history"). ``weekly_cap_usd`` mirrors
    ``plan_orders``' same-named override (validated finite and
    non-negative). When given, it must not exceed ``tier.weekly_cap_usd``: the
    override exists only to LOWER the effective ceiling below the tier's own
    nominal cap, never to raise it, and a caller-supplied value above
    ``tier.weekly_cap_usd`` -- for any tier, not only ones that use a
    rollover reserve -- was previously trusted as-is so long as it was
    finite and non-negative (Codex review: "Reject caller-inflated weekly
    cap overrides").

    Whenever ``tier.uses_rollover_reserve`` is True, ``weekly_rollover_reserve_usd``
    (paired with ``weekly_rollover_reserve_week_key``) is REQUIRED, and the
    effective weekly ceiling is DERIVED here as
    ``min(tier.weekly_cap_usd, NORMAL_WEEKLY_CAP_USD + weekly_rollover_reserve_usd)``
    -- the same computation ``assess()`` performs -- rather than trusted from
    a caller-supplied ``weekly_cap_usd`` override. A ``weekly_cap_usd`` alone
    only proves a number was given, never that it reflects a real reserve: a
    caller could otherwise pass ``weekly_cap_usd=1_800`` (the tier's own
    nominal cap, which already passes the finite/upper-bound checks) with
    zero reserve actually earned, letting a batch through at $1,800 when the
    real ceiling (no reserve earned) is $900 (Codex review: "Bind Stress
    overrides to verified reserve state", following up on "Require the
    effective rollover cap for Stress execution"). A reserve stamped for an
    earlier ISO week is zeroed the same way ``assess()`` zeroes it (Codex
    review: "Require a week key for every nonzero rollover reserve"). If
    ``weekly_cap_usd`` is ALSO given for such a tier, it may only further
    restrict the derived cap (``min(weekly_cap_usd, derived_cap)``), never
    relax it. ``buying_power_usd`` is REQUIRED (not optional) whenever
    placement is enabled: the absolute buying-power circuit breaker must
    never be silently skippable by a caller that simply omits the argument
    (Codex review: "Require buying power for live execution").

    ``trading_day`` is REQUIRED whenever placement is enabled and must equal
    ``snapshot.day`` exactly. Without this check a stale or mismatched
    ``BudgetSnapshot`` -- e.g. one fetched at the start of a long-running
    session and reused for a later day's submission, or one built for a
    different day entirely -- would pass every aggregate check above with the
    wrong day's spend, letting a batch through after today's actual spend has
    already exhausted the tier cap, or after another slot's same-day order
    has landed since the snapshot was fetched (Codex review: "Reject stale
    budget snapshots at live placement"). Pass the trading day this batch is
    actually being submitted for (typically ``now.astimezone(ET).date()`` at
    the moment of the placement call, not when the snapshot was fetched).

    Every ticker in ``plan.to_place`` must also be unique: a caller-built
    ``Plan`` naming the same ticker twice would otherwise call ``place_fn``
    twice with the same ``ref_id``, either placing two buys or reporting one
    broker order as two submitted entries depending on connector idempotency
    (Codex review: "Reject duplicate tickers in caller-built plans").

    ``orders`` -- the same broker order history used to build ``snapshot`` --
    is REQUIRED whenever placement is enabled. It is used for two independent
    revalidations, both directly against broker history rather than trusting
    ``snapshot``/``plan`` alone: one-order-per-ticker-per-day (``BudgetSnapshot``
    carries only totals, not per-ticker identities, and neither the
    ``plan_orders`` path nor ``assess``'s own pre-plan dedup is re-verified
    here -- a fresh snapshot built after an earlier same-day AAPL buy, paired
    with a caller-built (or stale) ``Plan`` naming AAPL again, would
    otherwise pass every other check here and submit a second same-day buy,
    violating the one-order-per-ticker-per-day circuit breaker (Codex
    review: "Recheck same-day ticker history before placement")); and the
    AGGREGATE daily/weekly spend itself, recomputed here as
    ``budget_snapshot(orders, trading_day)`` rather than read from
    ``snapshot``'s own fields (see above).

    ``orders_account_number`` is REQUIRED too, and must equal
    ``broker.account_number`` exactly: ``orders`` carries no account identity
    of its own, so pinning ``broker.account_number`` proves only where
    placement WOULD go, never where the history behind every placement-time
    cap check actually came from. Pass the account ``orders`` was fetched
    for, so a wiring bug that fetches history from the wrong (or an empty)
    account is refused instead of letting ``verified_snapshot`` silently
    understate real prior spend (Codex review: "Bind placement-time history
    to the authorized account").

    ``now`` and ``slot_id`` are REQUIRED too, and are used to rerun Gates 0, 1,
    3, and 4 (market day, time sanity, approved trading window, sole-executor
    interlock) immediately before the first submission: ``docs/live_wiring.md``
    documents a supported direct-call path where a real Python caller builds
    ``plan``/``snapshot``/``tier`` itself and calls ``plan_orders()`` then
    ``execute_plan()`` WITHOUT ever calling ``assess()``, so this is the only
    place those gates are guaranteed to run at all -- without them, a caller
    invoking ``execute_plan`` after market close, on a holiday, or long after
    a stale prior assessment could submit a real order outside the
    documented regular-hours-only contract, since a matching ``trading_day``
    alone satisfies every other check here (Codex review: "Recheck market
    gates before live placement"). ``now`` must additionally fall on the same
    ET calendar day as ``trading_day``: ``gate_time_sanity`` alone only checks
    the time-of-day portion of ``now`` against the slot window on ``now``'s
    OWN date, never against ``trading_day`` itself, so a caller could
    otherwise pass yesterday's matching snapshot/run_id alongside a
    time-of-day-valid ``now``, silently validating the calendar/budget gates
    against the stale supplied day while today's real spend is excluded from
    every recomputed total (Codex review: "Bind the placement day to the
    current clock"). ``slot_id`` must be one of ``SLOTS``' keys. Gate 4
    (``gate_sole_executor``) is rerun on the freshly materialized ``orders``
    below: an existing order for a DIFFERENT ticker inside this slot's window
    signals a second live executor even though it would not itself collide
    with the per-ticker dedup checks (Codex review: "Recheck the
    sole-executor interlock before placement").

    ``orders_fetched_at`` is REQUIRED too: ``orders`` itself carries no fetch
    timestamp or completeness marker, and ``trading_day``/``snapshot.day``
    prove only the calendar date, not how recently the history was actually
    pulled. A same-account fetch from earlier in the day, reused here after
    another slot or a manual buy consumed the remaining cap, would pass every
    check above while the recomputed ``verified_snapshot`` still reports the
    stale, too-low total, letting a batch submit beyond the real daily or
    weekly ceiling (Codex review: "Bind cap checks to a fresh history
    snapshot"). Must be timezone-aware and within ``MAX_ORDERS_FETCH_AGE_SECONDS``
    of ``now``; refetch ``orders`` immediately before calling this function
    rather than reusing an earlier fetch.

    One placement-time gap this function still does NOT close, documented in
    ``claude/GATE5-PROPOSAL.md`` §3.7 as a requirement for whatever eventually
    calls it live: ``gate_sole_executor``'s read-only broker-history check,
    even rerun here, cannot by itself make two concurrent executor instances
    mutually exclusive (that needs an atomic run/slot lease this module has
    no mechanism to provide) -- it closes the "history already shows a
    collision" case, not a true race between two simultaneous submissions.
    """
    # Literal booleans, not truthiness: a common wiring mistake like
    # enable_live_orders="false" (a nonempty, truthy string) combined with a
    # correctly-False dry_run would otherwise make `and not broker.dry_run`
    # true and reach live placement despite the second gate's value plainly
    # saying "false" (Codex review: "Require literal booleans for both
    # live-order gates").
    placement_enabled = broker.enable_live_orders is True and broker.dry_run is False
    result = ExecutionResult(placement_enabled=placement_enabled)
    if not placement_enabled:
        return result
    if not isinstance(run_id, str) or not run_id:
        # A truthy non-string (e.g. the int 123) passed the old bare
        # `not run_id` truthiness check, then reached _ISO_DATE_RE.findall()
        # below, which raises a bare TypeError on a non-string/bytes-like
        # argument -- escaping this documented direct live boundary's
        # controlled invalid-input ValueError before any placement (Codex
        # review: "Validate run IDs before regex matching").
        raise ValueError(
            "execute_plan requires run_id to be a nonempty string when placement is enabled; a "
            f"ticker-only ref_id would collide across every day and slot that ever buys that "
            f"ticker (got {run_id!r})"
        )
    if any(order.side != "buy" for order in plan.to_place):
        # Plan/PlannedOrder are public and constructible independently of
        # plan_orders(), so this module's accumulate-only contract cannot rely
        # on plan_orders() alone having produced buy-only rows (Codex review:
        # "Reject non-buy orders again at the placement boundary").
        raise ValueError("execute_plan refuses a plan containing a non-buy order")
    if snapshot is None or tier is None:
        raise ValueError(
            "execute_plan requires snapshot and tier when placement is enabled, to "
            "reapply the aggregate daily/weekly caps a caller-built Plan could otherwise "
            "bypass entirely"
        )
    canonical_tier = _BUDGET_TIERS_BY_NAME.get(tier.name)
    if (
        canonical_tier is None
        or tier.daily_cap_usd != canonical_tier.daily_cap_usd
        or tier.weekly_cap_usd != canonical_tier.weekly_cap_usd
        or tier.uses_rollover_reserve != canonical_tier.uses_rollover_reserve
    ):
        # BudgetTier is public and constructible independently of
        # select_budget_tier(): validating its caps as merely finite and
        # non-negative (below) would still accept e.g.
        # BudgetTier("normal", 1_000_000.0, 1_000_000.0), and every
        # aggregate comparison in this function would then use those
        # inflated limits, placing far beyond the configured $180/$900
        # Normal caps (Codex review: "Reject noncanonical budget tier
        # ceilings"). Matched by name AND every cap/flag.
        raise ValueError(
            f"execute_plan refuses a tier that does not match a configured BUDGET_TIERS "
            f"entry: {tier!r}"
        )
    # Matching tier's own fields against a canonical BUDGET_TIERS entry
    # (above) proves the tier's DEFINITION is legitimate, never that it was
    # the correct SELECTION for today's actual candidates: a caller could
    # pass a stale or simply wrong (but otherwise canonical) tier -- e.g.
    # Elevated, with $170 already spent and only $20 of new, non-high-
    # multiplier candidates, which select_budget_tier() would have chosen
    # Normal for. That $20 plan then clears Elevated's $240 cap and pushes
    # the day to $190, past Normal's real $180 ceiling. A caller-supplied
    # planned_spend_usd/high_multiplier_signal_count SCALAR pair does not fix
    # this either -- neither is tied to any actual candidate set, so a caller
    # could just as easily claim planned_spend_usd=130 for a real $20 plan
    # (Codex review: "Bind tier inputs to the plan's verified candidates").
    # `candidates` -- the same raw rows the caller used to select `tier` via
    # select_budget_tier() in the first place -- is required instead, and
    # verified below (once bought_today is known) by independently
    # recomputing planned_spend/high_mult from them the same way assess()
    # does (Codex review: "Derive the tier from verified candidates").
    if candidates is None:
        raise ValueError(
            "execute_plan requires candidates when placement is enabled, to verify tier is "
            "the correct select_budget_tier() selection for today's actual candidates, not "
            "just a canonically-shaped tier"
        )
    if buying_power_usd is None:
        raise ValueError(
            "execute_plan requires buying_power_usd when placement is enabled; the absolute "
            "buying-power circuit breaker must never be silently skippable"
        )
    if trading_day is None:
        raise ValueError(
            "execute_plan requires trading_day when placement is enabled, to reject a stale "
            "or mismatched snapshot before it silently authorizes a batch against the wrong "
            "day's spend"
        )
    if not isinstance(trading_day, date):
        # The mismatch branch below calls trading_day.isoformat(); a
        # documented direct caller supplying a serialized (e.g. ISO string)
        # or otherwise non-date trading_day would otherwise raise a bare
        # AttributeError there (or, if it happens to equal snapshot.day as a
        # string, at the run_id date check just after) instead of this
        # boundary's controlled ValueError (Codex review: "Validate the
        # placement trading day before formatting it").
        raise ValueError(
            f"execute_plan requires trading_day to be a date, got {type(trading_day).__name__}"
        )
    if not isinstance(snapshot.day, date):
        # The mismatch branch below calls snapshot.day.isoformat(); a
        # documented direct caller supplying a caller-built BudgetSnapshot
        # (public and constructible independently of budget_snapshot())
        # whose day is serialized (e.g. an ISO string) or otherwise not a
        # date would otherwise raise a bare AttributeError there instead of
        # this boundary's controlled ValueError (Codex review: "Validate
        # snapshot days before formatting them").
        raise ValueError(
            f"execute_plan requires snapshot.day to be a date, got {type(snapshot.day).__name__}"
        )
    if trading_day != snapshot.day:
        raise ValueError(
            f"execute_plan refuses to place: trading_day={trading_day.isoformat()!r} does not "
            f"match snapshot.day={snapshot.day.isoformat()!r}; the snapshot is stale or was "
            "built for a different day"
        )
    for total_value, total_field in (
        (snapshot.day_notional_usd, "snapshot.day_notional_usd"),
        (snapshot.week_notional_usd, "snapshot.week_notional_usd"),
    ):
        # A caller-built BudgetSnapshot (public and constructible
        # independently of budget_snapshot()) with a NaN, -inf, or negative
        # total defeats the monotonicity check below: any comparison
        # against NaN is False, and nothing compares less than -inf, so
        # verified_snapshot (recomputed from a fresh history fetch) no
        # longer registers as "suspiciously lower than claimed" -- real
        # earlier spend can disappear entirely and a live plan gets
        # authorized as if built from an empty history (Codex review:
        # "Validate supplied snapshot totals before comparison").
        if isinstance(total_value, bool) or not isinstance(total_value, (int, float)):
            raise ValueError(f"execute_plan requires {total_field} to be a number, got {total_value!r}")
        try:
            total_is_unusable = not math.isfinite(total_value) or total_value < 0
        except OverflowError:
            total_is_unusable = True
        if total_is_unusable:
            raise ValueError(
                f"execute_plan requires {total_field} to be a finite, non-negative number, got "
                f"{total_value!r}"
            )
    run_id_dates = set(_ISO_DATE_RE.findall(run_id))
    if run_id_dates != {trading_day.isoformat()}:
        # A nonempty run_id alone does not establish run-scoped uniqueness: a
        # caller reusing a constant run_id (e.g. "scheduled") across days would
        # derive the identical ref_id "dca-runner-scheduled-AAPL" every day, and
        # an idempotent broker can then reject the later buy or return the
        # prior day's order -- which the loop below would record as a
        # successful CURRENT submission (Codex review: "Bind the idempotency
        # key to the trading day"). A plain substring test alone is not
        # enough: a run_id carrying more than one ISO-shaped date (e.g.
        # "batch-2026-07-28-2026-07-29") would pass that test on EVERY one of
        # those days despite deriving the identical ref_id on each, letting an
        # idempotent broker return an earlier day's order as if it were a
        # fresh submission (Codex review: "Require a single-day idempotency
        # key"). Requiring the exact singleton set closes both gaps at once.
        raise ValueError(
            f"execute_plan requires run_id to contain exactly one date, the trading day it is "
            f"actually being submitted for ({trading_day.isoformat()!r}); got run_id={run_id!r} "
            f"(dates found: {sorted(run_id_dates)!r})"
        )
    # docs/live_wiring.md documents a supported direct-call path -- a real Python
    # caller building `plan`/`snapshot`/`tier` itself and calling `plan_orders()`
    # then `execute_plan()` WITHOUT ever calling `assess()` -- so this is the only
    # place Gates 0/1/3 (market day, time sanity, approved trading window) are
    # guaranteed to run at all before a real order is submitted. Without this
    # recheck, a caller invoking `execute_plan()` after market close, on a
    # holiday, or long after a stale prior assessment could submit a real `gfd`
    # market order outside the documented regular-hours-only contract, since a
    # matching `trading_day` alone satisfies every other check above (Codex
    # review: "Recheck market gates before live placement").
    if now is None:
        raise ValueError(
            "execute_plan requires now (a timezone-aware placement clock) when placement is "
            "enabled, to recheck the market-day, trading-window, and time-sanity gates "
            "immediately before the first submission"
        )
    if not isinstance(now, datetime):
        # now.tzinfo below assumes `now` IS a datetime -- the documented
        # direct plan_orders() -> execute_plan() path never routes through
        # assess()'s own now-validation, so a caller supplying an ISO
        # timestamp string, a bare date, or anything else non-datetime
        # would otherwise raise a bare AttributeError here instead of this
        # boundary's controlled ValueError (Codex review: "Validate the
        # direct-placement clock type before access").
        raise ValueError(f"execute_plan requires now to be a datetime, got {type(now).__name__}")
    if now.tzinfo is None:
        raise ValueError("execute_plan requires now to be a timezone-aware datetime, got a naive one")
    if now.astimezone(ET).date() != trading_day:
        # gate_time_sanity() below only checks the TIME portion of `now` against
        # the slot's acceptance window on `now`'s OWN date -- it never compares
        # that date against `trading_day` itself. Without this check, a caller
        # could pass yesterday's matching snapshot/run_id alongside a fresh
        # order fetch and a `now` that is only time-of-day-valid, silently
        # validating the calendar/budget gates against the stale supplied day
        # while today's real spend and today's ticker attempts are excluded
        # from every recomputed total, authorizing an order after today's
        # actual daily cap is already exhausted (Codex review: "Bind the
        # placement day to the current clock").
        raise ValueError(
            f"execute_plan refuses to place: now ({now.isoformat()}) falls on ET calendar day "
            f"{now.astimezone(ET).date().isoformat()!r}, which does not match "
            f"trading_day={trading_day.isoformat()!r}"
        )
    if slot_id is None or slot_id not in SLOTS:
        raise ValueError(
            f"execute_plan requires a recognized slot_id when placement is enabled, got {slot_id!r}"
        )
    slot = SLOTS[slot_id]
    if now_fn is None:
        # The single pre-loop `now` check only proves the window was open
        # when the BATCH started, not when each individual order is
        # actually submitted: a batch accepted at 15:59:59 ET can still
        # submit a later order in the same batch after 16:00 when an
        # earlier place_fn call is slow, past the regular-hours-only gate.
        # Making now_fn REQUIRED (not merely accepted) closes this for
        # every live placement rather than leaving it an opt-in the caller
        # could forget (Codex review: "Require a fresh clock for every live
        # placement").
        raise ValueError(
            "execute_plan requires now_fn (a callable returning a fresh, timezone-aware "
            "clock reading) when placement is enabled, to recheck the acceptance window "
            "immediately before every single placement, not just once before the batch"
        )
    try:
        status = market_day_status(trading_day)
    except (CalendarCoverageError, OSError, ValueError, KeyError, TypeError) as exc:
        # Mirrors assess()'s own Gate 0 exception handling: market_day_status()
        # reads market_calendar.json off disk and indexes into it directly, so a
        # missing/malformed file or an out-of-coverage year must refuse to place
        # rather than assume the market is open.
        raise ValueError(
            f"execute_plan refuses to place: market calendar unavailable, cannot confirm "
            f"trading day: {exc}"
        ) from exc
    if status.slot_should_end_silently(slot):
        raise ValueError(
            "execute_plan refuses to place: the market is closed (weekend/holiday) or in an "
            "early-close afternoon for this slot"
        )
    if not gate_time_sanity(now, slot):
        raise ValueError(
            f"execute_plan refuses to place: now is outside the {slot_id!r} slot's "
            f"{slot.window_start_et[0]:02d}:{slot.window_start_et[1]:02d}-"
            f"{slot.window_end_et[0]:02d}:{slot.window_end_et[1]:02d} ET acceptance window"
        )
    try:
        in_trading_window = gate_trading_window(trading_day)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"execute_plan refuses to place: trading window configuration unavailable: {exc}"
        ) from exc
    if not in_trading_window:
        raise ValueError(
            "execute_plan refuses to place: trading_day is outside the approved trading window"
        )
    if orders is None:
        raise ValueError(
            "execute_plan requires orders (the same broker history used to build snapshot) "
            "when placement is enabled, to reverify no ticker in the plan has already been "
            "bought today"
        )
    # `orders` itself carries no fetch timestamp, and trading_day/snapshot.day
    # prove only the calendar date -- a same-account fetch from earlier in the
    # day, reused here after another slot or a manual buy consumed the
    # remaining cap, would pass every check above while `verified_snapshot`
    # still reports the stale, too-low total (Codex review: "Bind cap checks
    # to a fresh history snapshot").
    if orders_fetched_at is None:
        raise ValueError(
            "execute_plan requires orders_fetched_at (a timezone-aware timestamp of when "
            "orders was actually fetched) when placement is enabled, to refuse a stale "
            "placement-time history snapshot"
        )
    if not isinstance(orders_fetched_at, datetime):
        # orders_fetched_at.tzinfo below assumes it IS a datetime --
        # orders_fetched_at is externally supplied alongside broker history,
        # so a caller passing a serialized (e.g. ISO string) or otherwise
        # non-datetime value would otherwise raise a bare AttributeError
        # here, before the freshness check a few lines down can produce its
        # own controlled refusal (Codex review: "Validate the history-fetch
        # timestamp type before access").
        raise ValueError(
            "execute_plan requires orders_fetched_at to be a datetime, got "
            f"{type(orders_fetched_at).__name__}"
        )
    if orders_fetched_at.tzinfo is None:
        raise ValueError(
            "execute_plan requires orders_fetched_at to be a timezone-aware datetime, got a "
            "naive one"
        )
    orders_age_seconds = (now - orders_fetched_at).total_seconds()
    if orders_age_seconds < 0 or orders_age_seconds > MAX_ORDERS_FETCH_AGE_SECONDS:
        raise ValueError(
            f"execute_plan refuses to place: orders_fetched_at is {orders_age_seconds:.1f}s "
            f"away from now, outside the {MAX_ORDERS_FETCH_AGE_SECONDS}s freshness window; "
            "refetch order history immediately before placement"
        )
    # Materialized once, before either traversal below: a single-pass
    # iterable (e.g. a generator) would otherwise be exhausted by the
    # bought_today comprehension, leaving budget_snapshot() with no history
    # at all and silently dropping every earlier order from the recomputed
    # daily/weekly totals -- the same exhaustion bug assess() was fixed for
    # (Codex review: "Materialize placement history before reusing it").
    try:
        orders = list(orders)
    except Exception as exc:
        # A lazy broker-history iterable (e.g. an API client wrapping a real
        # fetch) that raises while being consumed can embed an account
        # number, an access token, or other connector context in its own
        # exception message; this ValueError becomes the caller's required
        # failure evidence and alerts. Only a safe classification, the
        # exception's type name, is recorded here -- never its message --
        # matching the same fix already applied to assess()'s and this
        # function's own candidate-parsing exception paths (Codex review:
        # "Redact direct history materialization failures").
        raise ValueError(
            f"execute_plan refuses to place: order history could not be materialized: "
            f"{type(exc).__name__}"
        ) from exc
    _reject_non_broker_order_rows(orders)
    # Gate 4 -- sole-executor interlock, rerun on this fresh materialized
    # history for the same reason Gates 0/1/3 are rerun above: the documented
    # direct plan_orders() -> execute_plan() path never calls assess() at all,
    # so this is the only place a second writer's order inside this slot's
    # window is ever detected before a real submission -- without it, an
    # existing order for a DIFFERENT ticker in this same window would not by
    # itself be caught by the bought_today/dedup checks below, letting a
    # second concurrent executor place anyway (Codex review: "Recheck the
    # sole-executor interlock before placement").
    try:
        collisions = gate_sole_executor(orders, slot, trading_day)
    except ValueError as exc:
        raise ValueError(
            f"execute_plan refuses to place: broker order history contains a malformed "
            f"timestamp: {exc}"
        ) from exc
    if collisions:
        raise ValueError(
            f"execute_plan refuses to place: orders already present in the {slot_id!r} ET "
            "window, a second executor may be live"
        )
    try:
        bought_today = {
            _normalize_ticker(o.symbol)
            for o in orders
            if _is_same_day_buy_attempt(o, trading_day)
        }
    except ValueError as exc:
        # _normalize_ticker()'s raised message embeds the malformed
        # symbol's full repr, and a broker wrapper's BrokerOrder.symbol can
        # be arbitrary connector data -- a dict carrying an account
        # identifier, token, or other request context, for instance. This
        # documented direct execute_plan() boundary raises ValueError as
        # its normal failure contract, and that message IS the caller's
        # required failure evidence -- so only a safe classification (the
        # exception's type name) is recorded here, matching the redaction
        # already applied to assess()'s own equivalent bought_today check
        # (Codex review: "Redact malformed symbols at the direct execution
        # boundary").
        raise ValueError(
            f"execute_plan refuses to place: broker order history contains a malformed "
            f"symbol: {type(exc).__name__}"
        ) from exc
    # Independently recompute planned_spend/high_mult from `candidates` --
    # required above -- the same way assess() derives its own tier selection:
    # parse the raw rows, exclude tickers already bought today (bought_today,
    # just computed), then dedup/validate in allocation-priority order. This
    # ties the tier-selection inputs to VERIFIABLE candidate data instead of
    # caller-asserted scalars, which could otherwise claim any tier
    # regardless of what plan_orders() actually built (Codex review: "Bind
    # tier inputs to the plan's verified candidates").
    try:
        parsed_candidates = _parse_candidate_rows(candidates)
    except Exception as exc:
        # exc's own message can originate from a lazy candidates iterable
        # (e.g. a generator wrapping a live market-data/signal-generation
        # call) that raises while being consumed, and can embed a
        # market-data token, account context, or other connector detail;
        # this ValueError becomes the caller's required failure evidence and
        # alerts. Only a safe classification, the exception's type name, is
        # recorded here -- never its message -- matching the same fix
        # already applied to assess()'s own candidate-parsing exception
        # path (Codex review: "Redact candidate exceptions at the live
        # boundary").
        raise ValueError(
            "execute_plan refuses to place: candidates are malformed, cannot verify tier: "
            f"{type(exc).__name__}"
        ) from exc
    fresh_candidates = [c for c in parsed_candidates if c.ticker not in bought_today]
    valid_candidates, planned_spend_usd, high_multiplier_signal_count = _tier_selection_inputs(
        fresh_candidates
    )
    # `valid_candidates` (the deduped, validated winners _tier_selection_inputs
    # itself uses) is used below, once the effective weekly_cap_usd and
    # buying_power_usd are finalized, to rebuild the plan plan_orders() would
    # itself produce from these same verified inputs and require plan.to_place
    # to match it exactly (Codex review: "Rebuild the submitted plan from
    # verified candidates").
    expected_tier = select_budget_tier(planned_spend_usd, high_multiplier_signal_count)
    if expected_tier.name != tier.name:
        raise ValueError(
            f"execute_plan refuses to place: tier={tier.name!r} does not match "
            f"{expected_tier.name!r}, the tier select_budget_tier() selects for this batch's "
            f"actual candidates (planned_spend_usd={planned_spend_usd!r}, "
            f"high_multiplier_signal_count={high_multiplier_signal_count!r})"
        )
    # Recomputed from the SAME broker history already required above, rather
    # than trusting the caller-supplied snapshot's own day_notional_usd/
    # week_notional_usd: a caller could pass fresh orders (for the
    # bought-today check above) alongside a stale snapshot whose totals
    # predate a same-day order for a DIFFERENT ticker, silently authorizing
    # a batch beyond the real current spend even though trading_day matches
    # snapshot.day (Codex review: "Recompute spend from placement-time order
    # history"). budget_snapshot() fails closed the same way it does
    # everywhere else this repo calls it.
    verified_snapshot = budget_snapshot(orders, trading_day)
    if (
        verified_snapshot.day_notional_usd < snapshot.day_notional_usd
        or verified_snapshot.week_notional_usd < snapshot.week_notional_usd
    ):
        # verified_snapshot is expected to show spend >= the caller-supplied
        # snapshot's -- it is recomputed from a FRESHER fetch, so it can only
        # legitimately see the same or MORE committed spend (Codex review:
        # "Recompute spend from placement-time order history", above). A
        # documented direct caller can pass a single-use iterable (e.g. a
        # generator) to budget_snapshot() to build `snapshot`, then reuse
        # that SAME now-exhausted iterable here: `orders` materializes to an
        # empty list, verified_snapshot silently reports zero, and an
        # earlier same-day buy disappears from both the daily/weekly cap
        # check and (via bought_today, above) the per-ticker interlock, even
        # though the caller-supplied snapshot still shows the real prior
        # spend. A recomputed total LOWER than the already-established
        # snapshot is never legitimate for the SAME trading_day and is
        # refused outright rather than trusted (Codex review: "Detect
        # history consumed while constructing the snapshot").
        raise ValueError(
            "execute_plan refuses to place: recomputed day/week spend "
            f"(${verified_snapshot.day_notional_usd:.2f}/${verified_snapshot.week_notional_usd:.2f}) "
            "is less than the supplied snapshot's "
            f"(${snapshot.day_notional_usd:.2f}/${snapshot.week_notional_usd:.2f}); orders may be "
            "an exhausted single-pass iterable already consumed building snapshot, silently "
            "dropping earlier spend from the recomputed totals"
        )
    if weekly_cap_usd is not None:
        # Validated HERE, before the Stress-tier-specific min() calculation
        # below: a malformed weekly_cap_usd (e.g. the string "900") used to
        # reach that min() call unvalidated for any tier.uses_rollover_reserve
        # tier, raising a raw TypeError from comparing a string against a
        # float instead of this documented ValueError -- while the exact
        # same malformed value under a non-reserve tier already reached the
        # controlled failure path below (Codex review: "Validate Stress cap
        # overrides before applying min"). A bool survives isinstance(x,
        # (int, float)) and would otherwise be accepted as a $0/$1 ceiling; a
        # string instead raises an uncaught TypeError from math.isfinite();
        # and a syntactically valid but astronomically large int (e.g.
        # weekly_cap_usd=10**1000) passes the isinstance check but makes
        # math.isfinite() raise OverflowError rather than return False
        # (Codex review: "Validate the weekly cap override before
        # isfinite").
        if isinstance(weekly_cap_usd, bool) or not isinstance(weekly_cap_usd, (int, float)):
            raise ValueError(f"execute_plan: weekly_cap_usd must be a number, got {weekly_cap_usd!r}")
        try:
            weekly_cap_is_unusable = not math.isfinite(weekly_cap_usd) or weekly_cap_usd < 0
        except OverflowError:
            weekly_cap_is_unusable = True
        if weekly_cap_is_unusable:
            raise ValueError(
                f"execute_plan: weekly_cap_usd must be a finite, non-negative number, got "
                f"{weekly_cap_usd!r}"
            )
    if tier.uses_rollover_reserve:
        # A caller-supplied weekly_cap_usd alone only proves a number was
        # given, never that it reflects a real rollover reserve -- a caller
        # could pass weekly_cap_usd=1_800 (the tier's own nominal cap, which
        # already passes every prior check) with zero reserve actually
        # earned. The effective cap is derived HERE from
        # weekly_rollover_reserve_usd, the same way assess() derives it,
        # rather than trusted from an unverifiable override (Codex review:
        # "Bind Stress overrides to verified reserve state").
        if weekly_rollover_reserve_usd is None:
            raise ValueError(
                "execute_plan requires weekly_rollover_reserve_usd when "
                "tier.uses_rollover_reserve is True; a weekly_cap_usd override alone cannot "
                "prove a rollover reserve exists"
            )
        if isinstance(weekly_rollover_reserve_usd, bool) or not isinstance(
            weekly_rollover_reserve_usd, (int, float)
        ):
            # math.isfinite(True) succeeds since bool is an int subclass, so
            # a boolean reserve would otherwise be retained as $1, inflating
            # the derived Stress-tier weekly ceiling by that amount (Codex
            # review: "Reject boolean rollover-reserve values").
            raise ValueError(
                "execute_plan: weekly_rollover_reserve_usd must be a finite, non-negative "
                f"number, got {weekly_rollover_reserve_usd!r}"
            )
        try:
            reserve_is_unusable = (
                not math.isfinite(weekly_rollover_reserve_usd) or weekly_rollover_reserve_usd < 0
            )
        except OverflowError:
            # A syntactically valid but astronomically large JSON integer
            # (e.g. 10**1000, injected via persisted StrategyState) passes
            # the isinstance check above but makes math.isfinite() raise
            # OverflowError rather than return False, escaping this live
            # placement boundary's controlled ValueError entirely instead of
            # being treated as unusable -- the same fix already applied to
            # assess()'s equivalent check (Codex review: "Handle
            # rollover-reserve overflow at the live boundary").
            reserve_is_unusable = True
        if reserve_is_unusable:
            raise ValueError(
                "execute_plan: weekly_rollover_reserve_usd must be a finite, non-negative "
                f"number, got {weekly_rollover_reserve_usd!r}"
            )
        # A reserve stamped for an earlier week is stale, mirroring assess()'s
        # own freshness check (Codex review: "Require a week key for every
        # nonzero rollover reserve").
        if weekly_rollover_reserve_usd != 0.0 and weekly_rollover_reserve_week_key != iso_week_key(
            trading_day
        ):
            weekly_rollover_reserve_usd = 0.0
        if weekly_rollover_reserve_usd != 0.0:
            # Neither weekly_rollover_reserve_usd nor its week key, by
            # themselves, prove this AMOUNT was actually earned: a caller
            # could pass weekly_rollover_reserve_usd=900 with the current
            # week key even when finalized_budget_days/submitted_notional_by_week
            # justify no reserve at all (e.g. an all-zero/empty history).
            # Recomputed and compared the same way _state_from_json's own
            # load-time cross-check does, closing the gap a bare scalar can
            # never close on its own (Codex review: "Verify rollover reserve
            # before raising the weekly cap").
            if finalized_budget_days is None or submitted_notional_by_week is None:
                raise ValueError(
                    "execute_plan requires finalized_budget_days and "
                    "submitted_notional_by_week when tier.uses_rollover_reserve is True and "
                    "weekly_rollover_reserve_usd is nonzero; a week key alone does not prove "
                    "this amount was actually earned"
                )
            expected_reserve = _expected_weekly_rollover_reserve(
                weekly_rollover_reserve_week_key, finalized_budget_days, submitted_notional_by_week
            )
            if _cents(weekly_rollover_reserve_usd) != _cents(expected_reserve):
                raise ValueError(
                    f"execute_plan refuses to place: weekly_rollover_reserve_usd="
                    f"{weekly_rollover_reserve_usd!r} does not match {expected_reserve!r}, the "
                    "amount roll_weekly_reserve() would derive from finalized_budget_days and "
                    "submitted_notional_by_week"
                )
        derived_weekly_cap = min(
            tier.weekly_cap_usd, NORMAL_WEEKLY_CAP_USD + weekly_rollover_reserve_usd
        )
        # A caller-supplied weekly_cap_usd may still further RESTRICT the
        # derived cap (e.g. a caller with its own extra reason to be
        # stricter), but can never relax it above what the verified reserve
        # actually supports.
        weekly_cap_usd = derived_weekly_cap if weekly_cap_usd is None else min(
            weekly_cap_usd, derived_weekly_cap
        )
    for value, field_name in (
        (tier.daily_cap_usd, "tier.daily_cap_usd"),
        (tier.weekly_cap_usd, "tier.weekly_cap_usd"),
        (buying_power_usd, "buying_power_usd"),
    ):
        # Checked BEFORE math.isfinite(): a bool survives as an int subclass
        # and isfinite(True) succeeds silently (treating $True buying power as
        # $1 of real confirmed balance and reaching live placement with no
        # numeric confirmation at all), while a string instead raises an
        # uncaught TypeError from isfinite() itself (Codex review: "Reject
        # nonnumeric buying power at the live boundary"). Reported only by
        # type here, not repr: at this point `value` has not yet been
        # confirmed numeric and could still be arbitrary data an injected
        # account-data layer returned instead of a scalar -- a dict or
        # object carrying an account identifier, token, or other request
        # metadata, for instance -- and this raised message IS the caller's
        # required failure evidence, persisted/alerted the same way
        # assess()'s equivalent ABORT reason is (Codex review: "Redact
        # malformed buying-power values from evidence").
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"execute_plan: {field_name} must be a number, got type={type(value).__name__}"
            )
        try:
            value_is_unusable = not math.isfinite(value) or value < 0
        except OverflowError:
            # A syntactically valid but astronomically large JSON integer
            # (e.g. buying_power_usd=10**1000 from the broker) passes the
            # isinstance check above but makes math.isfinite() raise
            # OverflowError rather than return False, escaping this live
            # placement boundary's controlled ValueError entirely instead of
            # being treated as unusable (Codex review: "Catch oversized
            # buying power at the placement boundary").
            value_is_unusable = True
        if value_is_unusable:
            raise ValueError(
                f"execute_plan: {field_name} must be a finite, non-negative number, got {value!r}"
            )
    if weekly_cap_usd is not None and weekly_cap_usd > tier.weekly_cap_usd:
        # weekly_cap_usd exists only to LOWER the effective ceiling below the
        # tier's own nominal cap (e.g. a Stress tier whose rollover reserve
        # only justifies part of its nominal $1,800 -- see assess()'s own
        # min(tier.weekly_cap_usd, ...) derivation). A finite, non-negative
        # override greater than tier.weekly_cap_usd was previously trusted
        # as-is, for ANY tier including ones that never use a rollover
        # reserve at all, letting a caller-supplied override raise the
        # ceiling arbitrarily above what the tier permits (Codex review:
        # "Reject caller-inflated weekly cap overrides").
        raise ValueError(
            f"execute_plan refuses a weekly_cap_usd override ({weekly_cap_usd!r}) greater than "
            f"tier.weekly_cap_usd ({tier.weekly_cap_usd!r}); the override may only lower the "
            "effective ceiling, never raise it above the tier's own nominal cap"
        )
    # `valid_candidates` alone proves TIER selection, not that plan.to_place is
    # what plan_orders() would actually build from it: a caller could supply a
    # broad candidate set that escalates tier selection (many small candidates
    # summing past a tier's escalation threshold) while submitting only a
    # hand-picked subset, riding on the richer tier's daily/weekly caps for
    # spend a real, full plan_orders() run on these same candidates/snapshot/
    # caps would never authorize on its own -- e.g. with $176 already spent,
    # 26 valid $5 candidates select Elevated from $130 planned spend, but a
    # plan containing only one of those candidates would, alone, belong under
    # Normal's $180 cap (Codex review: "Rebuild the submitted plan from
    # verified candidates"). Rebuilt here, using the SAME weekly_cap_usd and
    # buying_power_usd this call has already finalized above, and required
    # (below) to match plan.to_place exactly, IN ORDER -- not merely each
    # order individually matching some candidate, and not merely the same
    # orders in any order -- since "reuse the same Plan ... for both calls"
    # (docs/live_wiring.md) is exactly what this reproduces for a caller that
    # actually follows that documented pattern.
    expected_plan = plan_orders(
        valid_candidates,
        verified_snapshot,
        tier,
        weekly_cap_usd=weekly_cap_usd,
        buying_power_usd=buying_power_usd,
    )
    expected_pairs = {
        (_normalize_ticker(o.ticker), _cents(o.amount_usd)) for o in expected_plan.to_place
    }
    tickers_seen: set[str] = set()
    for order in plan.to_place:
        ticker = _normalize_ticker(order.ticker)
        if ticker in tickers_seen:
            raise ValueError(
                f"execute_plan refuses a plan containing the same ticker twice: {order.ticker!r}"
            )
        tickers_seen.add(ticker)
        if ticker in bought_today:
            raise ValueError(
                f"execute_plan refuses a plan containing {order.ticker!r}: broker history "
                "shows it was already bought today"
            )
        if ticker not in ACTIVE_TICKERS:
            raise ValueError(
                f"execute_plan refuses a plan containing a non-whitelisted ticker: {order.ticker!r}"
            )
        if isinstance(order.amount_usd, bool) or not isinstance(order.amount_usd, (int, float)):
            # PlannedOrder is public and constructible independently of
            # plan_orders(): math.isfinite(True) succeeds since bool is an
            # int subclass, so amount_usd=True would otherwise pass every
            # check below (positivity, cap, cent-precision) and submit a
            # real $1.00 order at this independently callable live boundary
            # (Codex review: "Reject boolean planned-order amounts").
            raise ValueError(
                f"execute_plan refuses a plan containing a non-numeric amount for "
                f"{order.ticker!r}: {order.amount_usd!r}"
            )
        try:
            amount_is_unusable = not math.isfinite(order.amount_usd) or order.amount_usd <= 0
        except OverflowError:
            # A syntactically valid but astronomically large int (e.g.
            # 10**1000, from a caller-built PlannedOrder) passes the
            # isinstance check above but makes math.isfinite() raise
            # OverflowError rather than return False, escaping this live
            # placement boundary's controlled ValueError entirely instead of
            # being treated as unusable, the same fix already applied to
            # buying_power_usd and weekly_rollover_reserve_usd here (Codex
            # review: "Reject oversized planned amounts without
            # overflowing").
            amount_is_unusable = True
        if amount_is_unusable:
            raise ValueError(
                f"execute_plan refuses a plan containing a non-finite or non-positive amount "
                f"for {order.ticker!r}: {order.amount_usd!r}"
            )
        if order.amount_usd > MAX_PER_ORDER_USD:
            raise ValueError(
                f"execute_plan refuses a plan containing an over-cap amount for {order.ticker!r}: "
                f"{order.amount_usd!r} > {MAX_PER_ORDER_USD!r}"
            )
        if abs(order.amount_usd - round(order.amount_usd, 2)) > 1e-9:
            # Every cap/buying-power comparison below uses this raw float, but
            # placement sends f"{order.amount_usd:.2f}" -- the rounded cent
            # value the broker actually receives. A non-cent amount (e.g.
            # $9.996) can pass every check here at its unrounded value while
            # the broker is asked to place $10.00, exceeding a confirmed
            # ceiling by the rounding delta (Codex review: "Apply caps to the
            # rounded notional sent to the broker"). Refused outright rather
            # than quantized, since a real ladder-derived amount (base ×
            # multiplier) is always cent-precision already.
            raise ValueError(
                f"execute_plan refuses a plan containing a non-cent amount for {order.ticker!r}: "
                f"{order.amount_usd!r}"
            )
        # Every planned order must match an order the freshly rebuilt
        # expected_plan (above) itself contains, cent-exact. Checked here,
        # AFTER every other per-order validation above (ticker shape, numeric
        # type, finiteness, cap, cent-precision), so a malformed order is
        # refused for that more fundamental reason first.
        if (ticker, _cents(order.amount_usd)) not in expected_pairs:
            raise ValueError(
                f"execute_plan refuses to place: {order.ticker!r} at ${order.amount_usd:.2f} "
                "does not match any order plan_orders() would produce from the verified "
                "candidates, snapshot, tier, and caps"
            )
    submitted_sequence = [
        (_normalize_ticker(o.ticker), _cents(o.amount_usd)) for o in plan.to_place
    ]
    expected_sequence = [
        (_normalize_ticker(o.ticker), _cents(o.amount_usd)) for o in expected_plan.to_place
    ]
    if submitted_sequence != expected_sequence:
        # Membership alone (just above) only proves every SUBMITTED order is
        # legitimate; it does not prove plan.to_place is expected_plan in
        # full, IN ORDER. A set-plus-cardinality comparison here would accept
        # any permutation of the same orders, but placement is sequential and
        # stops at the first failure (Gate 7): reversing a high- and a
        # low-priority order lets a failed high-priority placement still
        # leave the lower-priority ticker bought, silently inverting
        # plan_orders()'s configured allocation priority (higher multiplier,
        # then larger drop, then ticker) precisely in the partial-failure
        # case that priority exists to protect (Codex review: "Preserve the
        # rebuilt plan's allocation order"). Comparing the two ordered
        # sequences catches both a reordering and a hand-picked subset (a
        # shorter list can never equal the full one) in a single check.
        raise ValueError(
            "execute_plan refuses to place: plan.to_place does not match, in order, the "
            f"{len(expected_plan.to_place)} order(s) plan_orders() would produce from these "
            "same verified candidates, snapshot, tier, and caps; placement must follow "
            "plan_orders()'s configured allocation priority, not a hand-picked or reordered "
            "subset of it"
        )
    effective_weekly_cap = tier.weekly_cap_usd if weekly_cap_usd is None else weekly_cap_usd
    total_notional = plan.total_notional_usd
    if verified_snapshot.day_notional_usd + total_notional > tier.daily_cap_usd:
        raise ValueError(
            f"execute_plan refuses a plan whose total ${total_notional:.2f} plus already-spent "
            f"${verified_snapshot.day_notional_usd:.2f} exceeds the daily cap ${tier.daily_cap_usd:.2f}"
        )
    if verified_snapshot.week_notional_usd + total_notional > effective_weekly_cap:
        raise ValueError(
            f"execute_plan refuses a plan whose total ${total_notional:.2f} plus already-spent "
            f"${verified_snapshot.week_notional_usd:.2f} exceeds the weekly cap ${effective_weekly_cap:.2f}"
        )
    if total_notional > buying_power_usd:
        raise ValueError(
            f"execute_plan refuses a plan whose total ${total_notional:.2f} exceeds buying "
            f"power ${buying_power_usd:.2f}"
        )
    pinning_failure = account_pinning_failure(broker.account_number, "robinhood")
    if pinning_failure is not None:
        raise ValueError(f"execute_plan refuses to place: {pinning_failure}")
    if broker.account_number != ROBINHOOD_AGENTIC_ACCOUNT_NUMBER:
        # The account number is deliberately NOT interpolated into this
        # message: failure_reason and any exception raised here are meant
        # for alerts and durable run evidence (message-center, email drafts,
        # repo alert files), and a pinning mismatch is exactly the scenario
        # where copying a live brokerage account identifier into those
        # channels is least acceptable (Codex review: "Redact the mismatched
        # brokerage account").
        raise ValueError(
            "execute_plan refuses to place: broker.account_number does not match the "
            "configured ROBINHOOD_AGENTIC_ACCOUNT_NUMBER environment variable"
        )
    # `orders` carries no account identity of its own -- pinning broker.account_number
    # (above) proves only where placement WOULD go, not where the history used for
    # every placement-time cap check (verified_snapshot, the bought-today check)
    # actually came from. A wiring bug that fetches empty or incomplete history from
    # another accessible account would otherwise let verified_snapshot report zero
    # prior spend and place into the authorized account anyway (Codex review: "Bind
    # placement-time history to the authorized account"). Requiring the caller to
    # separately assert which account `orders` was fetched for, and verifying it here,
    # turns a silent single-mistake wiring bug into one that needs two independent
    # mistakes (fetch the wrong history AND mislabel it) to slip through unnoticed.
    if orders_account_number is None:
        raise ValueError(
            "execute_plan requires orders_account_number when placement is enabled, to "
            "verify the placement-time order history actually belongs to the authorized "
            "account"
        )
    if orders_account_number != broker.account_number:
        raise ValueError(
            "execute_plan refuses to place: orders_account_number does not match "
            "broker.account_number; the placement-time order history may belong to a "
            "different account"
        )

    for order in plan.to_place:
        ticker = _normalize_ticker(order.ticker)
        clock_failure = _now_fn_clock_failure(
            now_fn, trading_day, slot, slot_id, orders_fetched_at
        )
        if clock_failure is not None:
            result.halted_on = ticker
            result.failure_reason = clock_failure
            break
        # Derived from slot_id/trading_day, NOT the caller-supplied run_id:
        # two overlapping invocations of the SAME slot can each pass a
        # different run_id prefix that both merely CONTAIN today's date (the
        # only thing validated above), so keying ref_id off run_id itself
        # would let each derive a different idempotency key for the same
        # ticker/slot/day -- each fetches the same still-empty history
        # before either placement becomes visible (the sole-executor check
        # is not atomic), and a different ref_id per call means broker-side
        # idempotency cannot deduplicate the two requests either, letting
        # both submit the same ticker. Deriving directly from slot_id and
        # trading_day instead guarantees identical concurrent invocations
        # always produce the IDENTICAL ref_id, so the broker's own
        # idempotency handling collapses them regardless of what run_id
        # string each caller happened to pass (Codex review: "Bind the
        # idempotency key to the slot").
        ref_id = f"dca-runner-{slot_id}-{trading_day.isoformat()}-{ticker}"
        try:
            response = broker.place_fn(
                account_number=broker.account_number,
                symbol=ticker,
                side=order.side,
                type="market",
                dollar_amount=f"{order.amount_usd:.2f}",
                ref_id=ref_id,
                time_in_force="gfd",
            )
            failure = _placement_failure_reason(
                response,
                expected_symbol=ticker,
                expected_side=order.side,
                expected_amount_usd=order.amount_usd,
                expected_ref_id=ref_id,
                account_number=broker.account_number,
            )
        except Exception as exc:
            # exc's own message is connector-defined and can embed whatever
            # context the broker wrapper puts in an error string -- the
            # requested account number, an access token, request headers --
            # and failure_reason is explicitly sent to alerts and durable
            # run evidence (message-center posts, email drafts, repo alert
            # files). AGENTS.md's secrets policy forbids credentials/account
            # identifiers in anything this repo writes, and this repo's own
            # established pattern for that (e.g. the orders_account_number
            # mismatch error above) is to OMIT the sensitive value from the
            # message rather than attempt to scrub it back out of arbitrary
            # text after the fact -- copying repr(exc) verbatim did the
            # opposite. Only a safe exception CLASSIFICATION, its type name,
            # is recorded here; never its message (Codex review: "Redact
            # broker exceptions before storing failure evidence").
            failure = f"place_fn raised {type(exc).__name__}"
        if failure is not None:
            result.halted_on = ticker
            # _placement_failure_reason() can still copy an ordinary failure
            # RESPONSE's envelope["error"] text verbatim (e.g. "insufficient
            # funds") -- deliberately preserved, since Gate 9 needs that
            # reason (Codex review: "Preserve the placement failure
            # reason") -- but a broker-provided error string can also embed
            # the account number this request was placed against, and
            # failure_reason is explicitly sent to alerts and durable run
            # evidence. The one secret this module itself knows for certain
            # could appear -- broker.account_number -- is redacted here
            # before persisting, the same way this repo already avoids
            # embedding it in the orders_account_number mismatch error above
            # rather than leaving arbitrary broker text unexamined (Codex
            # review: "Redact returned broker errors before persisting
            # them").
            result.failure_reason = failure.replace(broker.account_number, "[REDACTED-ACCOUNT]")
            break
        result.submitted.append(ticker)
    return result


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _is_malformed_usd_type(value) -> bool:
    """Check if a value's type is unsuitable for a USD amount (non-numeric or bool)."""
    return isinstance(value, bool) or not isinstance(value, (int, float))


def _is_unusable_usd_value(value: float) -> bool:
    """Check if a numeric value is unsuitable for a USD amount (NaN, infinity, or negative)."""
    try:
        return not math.isfinite(value) or value < 0
    except OverflowError:
        return True


def _is_same_day_buy_attempt(order, trading_day) -> bool:
    """Check if an order is a buy attempt on the given trading day."""
    return order.is_buy_attempt and _require_aware(order.created_at).astimezone(ET).date() == trading_day


def _is_stale_rollover_reserve(reserve_usd: float, week_key, trading_day) -> bool:
    """Check if a rollover reserve is nonzero but stale (missing key or from a prior week)."""
    return reserve_usd != 0.0 and week_key != iso_week_key(trading_day)


def _effective_weekly_cap(tier, weekly_rollover_reserve_usd: float) -> float:
    """Derive the effective weekly ceiling from the selected tier and rollover reserve."""
    if tier.uses_rollover_reserve:
        return min(tier.weekly_cap_usd, NORMAL_WEEKLY_CAP_USD + weekly_rollover_reserve_usd)
    return tier.weekly_cap_usd


def _redact_malformed_symbol(symbol) -> str:
    """Return a symbol as-is if string, or redact its value if it is an arbitrary connector object."""
    if isinstance(symbol, str):
        return symbol
    return f"<non-string symbol, type={type(symbol).__name__}>"


def _validate_weekly_rollover_reserve(weekly_rollover_reserve_usd: float) -> None:
    if _is_malformed_usd_type(weekly_rollover_reserve_usd):
        # math.isfinite(True) succeeds since bool is an int subclass, so a
        # boolean reserve would otherwise be retained as $1 and inflate the
        # derived Stress-tier weekly ceiling by that amount (Codex review:
        # "Reject boolean rollover-reserve values").
        raise ValueError(
            "assess: weekly_rollover_reserve_usd must be a finite, non-negative number, "
            f"got {weekly_rollover_reserve_usd!r}"
        )
    if _is_unusable_usd_value(weekly_rollover_reserve_usd):
        # A syntactically valid but astronomically large JSON integer (e.g.
        # 10**1000, which json.loads parses as a plain Python int of
        # unbounded size, injected via persisted StrategyState) passes the
        # isinstance check above but makes math.isfinite() raise
        # OverflowError rather than return False. This validation runs
        # before `out()` is even defined (let alone Gate 0), so there is no
        # ABORT payload to build yet -- treated as unusable here, the same
        # way it is for every other malformed reserve, rather than letting
        # OverflowError escape past this point entirely (Codex review:
        # "Catch oversized rollover reserves before finiteness checks").
        raise ValueError(
            "assess: weekly_rollover_reserve_usd must be a finite, non-negative number, "
            f"got {weekly_rollover_reserve_usd!r}"
        )


def _evaluate_gate_0_market_day(trading_day: date, slot) -> tuple[str, str] | None:
    try:
        status = market_day_status(trading_day)
    except CalendarCoverageError as exc:
        # A stale scheduler firing in a year the calendar doesn't cover must
        # still produce an ABORT payload so Gate 9 can alert and write run
        # evidence, not raise past the caller (Codex review: "Convert
        # calendar coverage failures into an abort verdict").
        return "ABORT", f"market calendar unavailable, cannot confirm trading day: {exc}"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # market_day_status() reads market_calendar.json off disk and indexes
        # into it directly; a missing file (OSError/FileNotFoundError),
        # malformed JSON (ValueError -- json.JSONDecodeError is a subclass),
        # bad encoding (ValueError -- UnicodeDecodeError is a subclass), or a
        # missing/wrong-shaped key (KeyError/TypeError) all mean the same
        # thing Gate 0 already handles for CalendarCoverageError: the
        # calendar cannot be trusted. Without this, a deployment/config
        # failure would raise past assess() before Gate 9 could write
        # evidence or alert (Codex review: "Convert unreadable calendar
        # files into an abort verdict").
        return "ABORT", f"market calendar unavailable, cannot confirm trading day: {exc}"
    if status.slot_should_end_silently(slot):
        why = "exchange closed" if status.is_closed else "early close, slot is after 13:00 ET"
        return "END_SILENTLY", why
    return None


def _evaluate_gate_3_trading_window(trading_day: date) -> str | None:
    try:
        in_window = gate_trading_window(trading_day)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # gate_trading_window() reads config/trading_window.json and parses
        # its dates directly; a missing file, malformed JSON, or an invalid
        # date string all mean the same thing Gate 0 already handles for
        # market_calendar.json: the config cannot be trusted, and this
        # deployment/configuration failure must not raise past assess()
        # before Gate 9 can write evidence or alert (Codex review: "Convert
        # trading-window config failures into ABORT").
        return f"trading window configuration unavailable, cannot confirm date: {exc}"
    if not in_window:
        return f"{trading_day} is outside the approved trading window"
    return None


def _materialize_orders(orders) -> tuple[list | None, str | None]:
    try:
        orders_list = list(orders)
    except Exception as exc:
        # exc's own message is broker/wrapper-defined and can embed an
        # account number, an access token, or other connector context; this
        # ABORT reason is exactly what the executor prompts persist as run
        # evidence and may send through alert channels. Only a safe
        # classification, the exception's type name, is recorded here --
        # never its message -- matching the same fix already applied to the
        # placement path's own raised exceptions (Codex review: "Redact
        # broker-history exceptions before returning evidence").
        return None, f"order history could not be materialized: {type(exc).__name__}"
    try:
        _reject_non_broker_order_rows(orders_list)
    except ValueError as exc:
        return None, f"broker order history contains a malformed row: {exc}"
    return orders_list, None


def _evaluate_gate_4_sole_executor(orders: list, slot, trading_day: date, slot_id: str) -> tuple[str | None, dict | None]:
    try:
        collisions = gate_sole_executor(orders, slot, trading_day)
    except (AttributeError, ValueError) as exc:
        # ValueError: a naive created_at raised by _require_aware(). AttributeError:
        # a malformed row itself -- None, a raw dict, or anything else without a
        # .created_at attribute -- raises AttributeError from gate_sole_executor's
        # own `o.created_at` access, before _require_aware() ever runs (Codex
        # review: "Convert malformed history rows into ABORT"). Either way this
        # must produce an ABORT payload, not escape assess()'s documented
        # never-raises contract before Gate 9 can write evidence or alert (Codex
        # review: "Convert malformed broker timestamps into an abort verdict").
        return f"broker order history contains a malformed row or timestamp: {exc}", None
    if collisions:
        return (
            f"orders already present in the {slot_id} ET window, a second executor may be live",
            {"colliding_orders": [_redact_malformed_symbol(o.symbol) for o in collisions]},
        )
    return None, None


# ---------------------------------------------------------------------------
# assess() helpers
# ---------------------------------------------------------------------------


def _check_buying_power(buying_power_usd: float | None) -> str | None:
    if buying_power_usd is None:
        # Every executor prompt's Gate 6 requires "fail closed -- abort and
        # alert rather than assuming it is sufficient" when buying power
        # cannot be confirmed. Treating an omitted buying_power_usd as
        # unlimited headroom (plan_orders()'s own, lower-level default)
        # would let assess() return PROCEED with a placeable plan despite no
        # confirmed funds, which the prompt-driven callers then submit
        # directly at Gate 7 (Codex review: "Abort assessment when buying
        # power is unavailable"). Checked after Gates 0/1/3/4 so a weekend/
        # holiday still ends silently rather than aborting over unconfirmed
        # buying power on a day nothing was going to be bought anyway.
        return "buying power could not be confirmed, refusing to plan a batch"
    if _is_malformed_usd_type(buying_power_usd):
        # A supplied-but-unusable buying_power_usd is not caught by the None
        # check above and would otherwise reach plan_orders(), whose own
        # finiteness check either raises a bare ValueError (NaN/infinity/
        # negative) or, for a bool, does not raise at all: math.isfinite(True)
        # succeeds since bool is an int subclass, so a $1 live order could be
        # placed despite zero confirmed balance; a string raises TypeError.
        # Every one of these must produce the same fail-closed ABORT the
        # omitted case above already gets, not escape assess()'s never-raises
        # contract (Codex review: "Return ABORT for malformed buying power",
        # "Reject nonnumeric buying-power values"). At this point
        # buying_power_usd has NOT yet been confirmed numeric, so it can
        # still be arbitrary data the injected account-data layer returned
        # instead of a scalar -- a dict or object carrying an account
        # identifier, token, or other request metadata, for instance -- and
        # this ABORT reason is exactly what gets persisted as run evidence
        # and sent through alert channels. Only a safe classification (the
        # value's type) is recorded here, never its repr, matching the
        # redaction already applied to every other malformed broker input
        # this function converts into an ABORT reason (Codex review: "Redact
        # malformed buying-power values from evidence").
        return (
            f"buying power is unusable (type={type(buying_power_usd).__name__}), refusing to "
            "plan a batch"
        )
    if _is_unusable_usd_value(buying_power_usd):
        # A syntactically valid but astronomically large JSON integer (e.g.
        # 10**1000, which json.loads parses as a plain Python int of
        # unbounded size) passes the isinstance check above but makes
        # math.isfinite() raise OverflowError rather than return False,
        # escaping assess()'s never-raises contract entirely instead of
        # producing the documented ABORT (Codex review: "Convert oversized
        # buying power into an abort").
        return f"buying power is unusable ({buying_power_usd!r}), refusing to plan a batch"
    return None


def _build_budget_snapshot(orders: list, trading_day: date):
    try:
        snapshot = budget_snapshot(orders, trading_day)
    except BudgetDataError as exc:
        # budget_snapshot()'s raised message embeds the offending order's
        # symbol and amount via repr(), and either can be arbitrary
        # connector data a malfunctioning broker wrapper supplied instead
        # of a real ticker/notional -- a dict carrying an access token or
        # account identifier, for instance. This ABORT reason is exactly
        # what gets persisted as run evidence and sent through alert
        # channels, so only a safe classification (the exception's type
        # name) is recorded here, matching the redaction already applied
        # to every other exception this function converts into an ABORT
        # reason (Codex review: "Redact malformed broker notionals from
        # abort evidence").
        return None, f"broker history contains an unusable notional: {type(exc).__name__}"
    except ValueError as exc:
        # This ValueError is not only a malformed timestamp from
        # _require_aware(): budget_snapshot() also touches
        # order.counts_toward_budget, which calls _normalize_side()/
        # _is_dead_order_state() -- both raise ValueError embedding the
        # malformed side/state value's full repr, and either can be
        # arbitrary connector data (a dict carrying an access token or
        # account identifier, for instance). Only a safe classification
        # (the exception's type name) is recorded here, matching the
        # redaction already applied to the neighboring BudgetDataError path
        # above (Codex review: "Redact malformed history metadata in abort
        # evidence").
        return None, f"broker order history contains a malformed row: {type(exc).__name__}"
    return snapshot, None


def _parse_and_validate_candidates(candidates):
    try:
        cands = _parse_candidate_rows(candidates)
    except Exception as exc:
        # exc's own message can originate from a lazy candidates iterable
        # (e.g. a generator wrapping a live market-data/signal-generation
        # call) that raises while being consumed, and can embed a
        # market-data token, account context, or other connector detail;
        # this ABORT reason is exactly what the executor prompts persist as
        # run evidence and may send through alert channels. Only a safe
        # classification, the exception's type name, is recorded here --
        # never its message -- matching the same fix already applied to the
        # broker-history and placement exception paths (Codex review:
        # "Redact candidate-source exceptions from assessment evidence").
        return None, f"candidate row is malformed, cannot build a plan: {type(exc).__name__}"
    return cands, None


def _get_bought_today_tickers(orders: list, trading_day: date):
    try:
        bought_today = {
            _normalize_ticker(o.symbol)
            for o in orders
            if _is_same_day_buy_attempt(o, trading_day)
        }
    except ValueError as exc:
        # A malformed symbol (None, a bool, ...) on a same-day buy raises
        # from _normalize_ticker() before this comprehension can decide
        # whether the order is even relevant, escaping assess()'s
        # never-raises contract before Gate 9 could write evidence or alert
        # (Codex review: "Convert malformed broker symbols into an abort
        # verdict"). exc's own message embeds the malformed value's full
        # repr, and a broker wrapper's BrokerOrder.symbol can be arbitrary
        # connector data -- a dict carrying an account identifier or token,
        # for instance -- so only a safe classification, the exception's
        # type name, is recorded here, matching the redaction already
        # applied to every other exception this function converts into an
        # ABORT reason (Codex review: "Redact malformed broker symbols from
        # abort evidence").
        return None, f"broker order history contains a malformed symbol: {type(exc).__name__}"
    return bought_today, None


def assess(
    slot_id: str,
    orders,
    candidates,
    now: datetime,
    *,
    weekly_rollover_reserve_usd: float = 0.0,
    weekly_rollover_reserve_week_key: tuple[int, int] | None = None,
    buying_power_usd: float | None = None,
) -> dict:
    """Run Gates 0, 1, 3, 4 and 6 and return a JSON-serialisable verdict.

    This is the single surface the seven scheduled prompts call. Keeping it here
    rather than in prompt text is the entire point of this module: task text is
    silently truncatable, this is not.

    Verdicts:
        END_SILENTLY  normal calendar fact -- weekend, holiday, early close.
                      No alert. Nothing is wrong.
        ABORT         something is wrong. Alert, place nothing, never retry.
        PROCEED       gates pass. ``plan`` holds what would be placed.

    ``placement_enabled`` mirrors the verdict: ``True`` only for ``PROCEED``,
    ``False`` for ``END_SILENTLY``/``ABORT``. It reports this ASSESSMENT's own
    gate outcome only -- it is not authorization to call
    ``place_equity_order`` directly. Real placement must go through
    ``execute_plan()``, which reruns the clock/order-history/budget/account
    checks fresh immediately before submission, since a delayed caller or an
    order placed elsewhere after this assessment ran can make a stale
    ``placement_enabled=True`` no longer true; ``execute_plan()`` also
    requires a live ``Plan`` object, not this dict's ``plan``, so no caller
    reaches it by round-tripping through ``assess()``'s JSON output (see the
    "Returns a JSON-serialisable dict" note below). See
    docs/live_wiring.md's "Not yet wired to the live prompts" section: none
    of the seven scheduled prompts currently acts on this field at all.

    ``candidates`` are ``(ticker, amount_usd)`` pairs, ``(ticker, amount_usd,
    multiplier)`` triples, or ``(ticker, amount_usd, multiplier, drop_pct)``
    quadruples from the repo's own signal generation. This function never
    invents them. Passing the actual ladder multiplier as the third element is
    preferred: without it, a high-multiplier signal is estimated from
    ``amount_usd`` against the global $2 base, which undercounts a valid 3x-5x
    candidate priced off the reduced $1 base (Codex review: "Count
    high-multiplier signals by multiplier, not notional"). Passing the actual
    drop vs. the prior close as the fourth element is preferred too: without
    it, ``plan_orders``' allocation-priority tie-break among equal-multiplier
    candidates degrades from "larger drop wins" to alphabetical (Codex review:
    "Carry drop percentage through assess").

    ``weekly_rollover_reserve_usd`` is the raw reserve from durable state
    (``StrategyState.weekly_rollover_reserve_usd``) -- pass the persisted
    number itself, never a precomputed effective cap. The effective weekly
    ceiling is derived HERE, after Gate 6 selects today's tier from today's
    candidates, as ``min(tier.weekly_cap_usd, NORMAL_WEEKLY_CAP_USD +
    weekly_rollover_reserve_usd)`` when that tier uses the reserve, mirroring
    ``StrategyState.effective_weekly_cap()``. Accepting a precomputed cap
    instead let a caller's own (possibly stale, previous-day) tier selection
    silently override the ceiling that today's actual tier implies -- e.g. a
    persisted Stress-tier cap of $1,800 would keep applying to a run whose
    fresh candidates only justify Normal's $900 (Codex review: "Derive the
    weekly ceiling from the tier selected here"). ``buying_power_usd`` is
    effectively REQUIRED for a ``PROCEED`` verdict, despite staying ``None``-able
    in the signature: every executor prompt's Gate 6 requires an abort when
    buying power cannot be confirmed, so omitting it here returns ``ABORT``
    (after Gates 0/1/3/4, so a weekend/holiday still ends silently) rather
    than treating ``None`` as unlimited headroom the way ``plan_orders``'
    own, lower-level default does (Codex review: "Abort assessment when
    buying power is unavailable"). When given, it is enforced as an
    additional ceiling so a run never plans more than the account can
    actually pay for (Codex review: "Enforce available buying power before
    planning orders").

    ``weekly_rollover_reserve_week_key`` should be the raw
    ``StrategyState.reserve_week_key`` paired with the reserve above. A
    rollover reserve is only valid for the ISO week
    ``StrategyState.roll_weekly_reserve`` computed it for; a reserve carried
    over from an earlier week (e.g. state loaded without having rolled across
    a weekend) is stale and must not keep inflating this week's Stress-tier
    ceiling. Whenever ``weekly_rollover_reserve_usd`` is nonzero, the supplied
    key must match this run's own ``iso_week_key(trading_day)`` exactly or the
    reserve is treated as unrolled and zeroed before use -- a missing key
    (``None``, the default) does NOT bypass this check, since a real reserve
    is never legitimately unkeyed: a persisted or caller-supplied nonzero
    reserve with no key would otherwise keep applying to every future week
    forever (Codex review: "Refresh the rollover reserve for the current
    week", "Require a week key for every nonzero rollover reserve"). A zero
    reserve is unaffected regardless of the key, since there is nothing to
    zero.

    Does NOT apply the 7-day deployment kill-switch (docs/risk_management.md):
    ``candidates`` are planned at the notional they arrive with. The caller
    (the not-yet-built signal-generation entry point, see
    claude/GATE5-PROPOSAL.md#36-the-7-day-deployment-kill-switch-must-be-applied-before-candidates-reach-assess)
    must already have capped every multiplier to 1x before calling this
    function whenever that guard is tripped (Codex review: "Enforce the
    rolling risk guard before planning").

    Returns a JSON-serialisable dict on purpose: the seven scheduled prompts are
    LLM tool-calling sessions with no way to pass a live Python object between tool
    calls. A real Python caller should not round-trip through this dict to reach
    ``execute_plan`` -- call ``plan_orders`` directly and reuse the same ``Plan``,
    ``BudgetSnapshot``, and ``BudgetTier`` objects for both calls. See
    docs/live_wiring.md's "Gate sequence" section (Codex review: "Return execution
    context from assess").
    """
    if isinstance(weekly_rollover_reserve_usd, bool) or not isinstance(
        weekly_rollover_reserve_usd, (int, float)
    ):
        # math.isfinite(True) succeeds since bool is an int subclass, so a
        # boolean reserve would otherwise be retained as $1 and inflate the
        # derived Stress-tier weekly ceiling by that amount (Codex review:
        # "Reject boolean rollover-reserve values").
        raise ValueError(
            "assess: weekly_rollover_reserve_usd must be a finite, non-negative number, "
            f"got {weekly_rollover_reserve_usd!r}"
        )
    try:
        reserve_is_unusable = (
            not math.isfinite(weekly_rollover_reserve_usd) or weekly_rollover_reserve_usd < 0
        )
    except OverflowError:
        # A syntactically valid but astronomically large JSON integer (e.g.
        # 10**1000, which json.loads parses as a plain Python int of
        # unbounded size, injected via persisted StrategyState) passes the
        # isinstance check above but makes math.isfinite() raise
        # OverflowError rather than return False. This validation runs
        # before `out()` is even defined (let alone Gate 0), so there is no
        # ABORT payload to build yet -- treated as unusable here, the same
        # way it is for every other malformed reserve, rather than letting
        # OverflowError escape past this point entirely (Codex review:
        # "Catch oversized rollover reserves before finiteness checks").
        reserve_is_unusable = True
    if reserve_is_unusable:
        raise ValueError(
            "assess: weekly_rollover_reserve_usd must be a finite, non-negative number, "
            f"got {weekly_rollover_reserve_usd!r}"
        )
    slot = SLOTS[slot_id]  # KeyError on an unknown slot is correct: fail loud.
    if not isinstance(now, datetime):
        # now.astimezone(ET) below assumes `now` IS a datetime -- an injected
        # scheduler/time layer supplying None, an ISO timestamp string, or
        # anything else makes that call raise a bare AttributeError before
        # `out()` (right below) is even defined, escaping assess()'s
        # documented never-raises contract with no ABORT payload at all
        # instead of the fail-closed evidence/alert path Gate 9 expects
        # (Codex review: "Validate the assessment clock before converting
        # it"). trading_day cannot be derived from a non-datetime `now`, so
        # this ABORT payload (matching out()'s own shape) reports it as
        # unknown rather than guessing.
        return {
            "slot": slot_id,
            "slot_label": slot.label,
            "trading_day": None,
            "verdict": "ABORT",
            "reason": f"assessment clock is not a datetime (got {type(now).__name__})",
            "placement_enabled": False,
        }
    trading_day = now.astimezone(ET).date()

    def out(verdict: str, reason: str, **extra) -> dict:
        payload = {
            "slot": slot_id,
            "slot_label": slot.label,
            "trading_day": trading_day.isoformat(),
            "verdict": verdict,
            "reason": reason,
            "placement_enabled": verdict == "PROCEED",
        }
        payload.update(extra)
        return payload

    if now.tzinfo is None:
        # now.astimezone(ET) above silently interprets a naive value in the
        # host's local timezone rather than raising, so trading_day may
        # already be wrong by the time this is checked -- but Gate 1's
        # gate_time_sanity() explicitly rejects a naive datetime, and
        # nothing before it caught that ValueError, letting a naive
        # assessment clock (e.g. the common datetime.now() with no tzinfo)
        # escape assess()'s never-raises contract before Gate 9 could write
        # evidence or alert (Codex review: "Convert a naive assessment
        # clock into an abort verdict").
        return out("ABORT", "assessment clock is naive, cannot confirm the actual firing time")

    # Gate 0 -- market day
    try:
        status = market_day_status(trading_day)
    except CalendarCoverageError as exc:
        # A stale scheduler firing in a year the calendar doesn't cover must
        # still produce an ABORT payload so Gate 9 can alert and write run
        # evidence, not raise past the caller (Codex review: "Convert
        # calendar coverage failures into an abort verdict").
        return out("ABORT", f"market calendar unavailable, cannot confirm trading day: {exc}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # market_day_status() reads market_calendar.json off disk and indexes
        # into it directly; a missing file (OSError/FileNotFoundError),
        # malformed JSON (ValueError -- json.JSONDecodeError is a subclass),
        # bad encoding (ValueError -- UnicodeDecodeError is a subclass), or a
        # missing/wrong-shaped key (KeyError/TypeError) all mean the same
        # thing Gate 0 already handles for CalendarCoverageError: the
        # calendar cannot be trusted. Without this, a deployment/config
        # failure would raise past assess() before Gate 9 could write
        # evidence or alert (Codex review: "Convert unreadable calendar
        # files into an abort verdict").
        return out("ABORT", f"market calendar unavailable, cannot confirm trading day: {exc}")
    if status.slot_should_end_silently(slot):
        why = "exchange closed" if status.is_closed else "early close, slot is after 13:00 ET"
        return out("END_SILENTLY", why)

    # Gate 1 -- time sanity. An off-schedule firing is a scheduler fault.
    if not gate_time_sanity(now, slot):
        return out(
            "ABORT",
            f"fired outside the {slot.window_start_et[0]:02d}:{slot.window_start_et[1]:02d}-"
            f"{slot.window_end_et[0]:02d}:{slot.window_end_et[1]:02d} ET window",
        )

    # Gate 3 -- approved trading window
    try:
        in_window = gate_trading_window(trading_day)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # gate_trading_window() reads config/trading_window.json and parses
        # its dates directly; a missing file, malformed JSON, or an invalid
        # date string all mean the same thing Gate 0 already handles for
        # market_calendar.json: the config cannot be trusted, and this
        # deployment/configuration failure must not raise past assess()
        # before Gate 9 can write evidence or alert (Codex review: "Convert
        # trading-window config failures into ABORT").
        return out("ABORT", f"trading window configuration unavailable, cannot confirm date: {exc}")
    if not in_window:
        return out("ABORT", f"{trading_day} is outside the approved trading window")

    # Materialized once, immediately before Gate 4 (gate_sole_executor) makes
    # the first pass -- deliberately AFTER Gates 0/1/3 above, none of which
    # need broker history at all: a weekend/holiday/early-close afternoon,
    # an off-schedule firing, or a date outside the approved trading window
    # must still produce the documented END_SILENTLY/ABORT verdict without
    # ever touching `orders`, rather than paying for (and failing on) a
    # broker-history fetch/materialization that was never needed to answer
    # those questions (Codex review: "Run the calendar gate before consuming
    # broker history"). A single-pass iterable (e.g. a generator) would
    # otherwise be exhausted here, leaving Gate 6's budget_snapshot() and the
    # bought-today dedup with no broker history at all (Codex review:
    # "Materialize order history before running multiple gates"). ``orders``
    # is externally injected broker-history data (see docs/live_wiring.md); a
    # broker-history layer returning ``None``, or a lazy iterable that
    # raises while being consumed, must fail closed into the documented
    # ABORT verdict here rather than raising past assess()'s never-raises
    # contract before Gate 9 can write evidence or alert (Codex review:
    # "Return ABORT when order history cannot be materialized").
    try:
        orders = list(orders)
    except Exception as exc:
        # exc's own message is broker/wrapper-defined and can embed an
        # account number, an access token, or other connector context; this
        # ABORT reason is exactly what the executor prompts persist as run
        # evidence and may send through alert channels. Only a safe
        # classification, the exception's type name, is recorded here --
        # never its message -- matching the same fix already applied to the
        # placement path's own raised exceptions (Codex review: "Redact
        # broker-history exceptions before returning evidence").
        return out(
            "ABORT", f"order history could not be materialized: {type(exc).__name__}"
        )
    try:
        _reject_non_broker_order_rows(orders)
    except ValueError as exc:
        return out("ABORT", f"broker order history contains a malformed row: {exc}")

    # Gate 4 -- sole-executor interlock
    try:
        collisions = gate_sole_executor(orders, slot, trading_day)
    except (AttributeError, ValueError) as exc:
        # ValueError: a naive created_at raised by _require_aware(). AttributeError:
        # a malformed row itself -- None, a raw dict, or anything else without a
        # .created_at attribute -- raises AttributeError from gate_sole_executor's
        # own `o.created_at` access, before _require_aware() ever runs (Codex
        # review: "Convert malformed history rows into ABORT"). Either way this
        # must produce an ABORT payload, not escape assess()'s documented
        # never-raises contract before Gate 9 can write evidence or alert (Codex
        # review: "Convert malformed broker timestamps into an abort verdict").
        return out("ABORT", f"broker order history contains a malformed row or timestamp: {exc}")
    if collisions:
        return out(
            "ABORT",
            f"orders already present in the {slot_id} ET window, a second executor may be live",
            # `_reject_non_broker_order_rows()` only confirms each row IS a
            # BrokerOrder instance, never that its externally-supplied
            # `.symbol` is itself JSON-native (e.g. a caller-constructed
            # BrokerOrder(symbol=date(...), ...)) or even a real ticker at
            # all: it can be arbitrary connector data, a dict carrying an
            # access token, account identifier, or request metadata.
            # `out()`'s payload is documented as JSON-serialisable, and this
            # ABORT reason is exactly what gets persisted as run evidence
            # and sent through alert channels -- str()-ing a non-string
            # symbol made it JSON-safe but not secret-safe, since str() on a
            # dict still serializes its full contents. A plain string
            # symbol (even a malformed one, e.g. an unnormalized ticker) is
            # not secret-shaped and is included as-is; anything else is
            # reported only by type (Codex review: "Redact malformed
            # symbols from collision evidence").
            colliding_orders=[
                o.symbol if isinstance(o.symbol, str)
                else f"<non-string symbol, type={type(o.symbol).__name__}>"
                for o in collisions
            ],
        )

    # Gate 6 -- budget from broker history
    buying_power_error = _check_buying_power(buying_power_usd)
    if buying_power_error is not None:
        return out("ABORT", buying_power_error)

    snapshot, snapshot_error = _build_budget_snapshot(orders, trading_day)
    if snapshot_error is not None:
        return out("ABORT", snapshot_error)
    if snapshot is None:
        return out("ABORT", "budget snapshot could not be built")

    cands, cands_error = _parse_and_validate_candidates(candidates)
    if cands_error is not None:
        return out("ABORT", cands_error)
    if cands is None:
        return out("ABORT", "candidate rows could not be parsed")

    bought_today, bought_today_error = _get_bought_today_tickers(orders, trading_day)
    if bought_today_error is not None:
        return out("ABORT", bought_today_error)
    if bought_today is None:
        return out("ABORT", "broker order history could not be evaluated")
    # Whether a same-day repeat is refused outright is a CONFIG decision, not a
    # hardcoded one: config/budgets.json order_rules.one_order_per_ticker_per_day.
    # True  -> first slot to buy a ticker wins it for the day (original behaviour).
    # False -> same_day_top_up_policy governs instead, and the resize decision
    #          belongs to scripts/same_day_top_up_gate.py, which the prompts run
    #          separately. assess() must NOT also refuse the ticker here, or the
    #          top-up path could never execute.
    if ONE_ORDER_PER_TICKER_PER_DAY:
        already_bought_refusals = [
            (c.ticker, REFUSED_ALREADY_BOUGHT_TODAY) for c in cands if c.ticker in bought_today
        ]
        fresh_cands = [c for c in cands if c.ticker not in bought_today]
    else:
        already_bought_refusals = []
        fresh_cands = list(cands)

    _valid_cands, planned_spend, high_mult = _tier_selection_inputs(fresh_cands)
    tier = select_budget_tier(planned_spend, high_mult)
    # A reserve computed for an earlier week is stale and must not keep
    # inflating this week's ceiling -- see the docstring above (Codex review:
    # "Refresh the rollover reserve for the current week"). A missing key is
    # treated as stale too whenever the reserve is nonzero: a real reserve is
    # never legitimately unkeyed, so `None` must not bypass the freshness
    # check outright, or a persisted/caller-supplied reserve with no key
    # would keep applying to every future week forever (Codex review:
    # "Require a week key for every nonzero rollover reserve").
    if _is_stale_rollover_reserve(weekly_rollover_reserve_usd, weekly_rollover_reserve_week_key, trading_day):
        weekly_rollover_reserve_usd = 0.0
    # Derived from THIS run's tier, never a caller-precomputed value -- see
    # the docstring above (Codex review: "Derive the weekly ceiling from the
    # tier selected here").
    effective_weekly_cap = _effective_weekly_cap(tier, weekly_rollover_reserve_usd)
    plan = plan_orders(
        fresh_cands, snapshot, tier, weekly_cap_usd=effective_weekly_cap, buying_power_usd=buying_power_usd
    )
    plan.refused = already_bought_refusals + plan.refused

    return out(
        "PROCEED",
        "gates passed",
        budget={
            "day_notional_usd": snapshot.day_notional_usd,
            "week_notional_usd": snapshot.week_notional_usd,
            "tier": tier.name,
            "daily_cap_usd": tier.daily_cap_usd,
            "weekly_cap_usd": effective_weekly_cap,
            "day_order_count": snapshot.day_order_count,
            "max_orders_per_day": MAX_ORDERS_PER_DAY,
            "one_order_per_ticker_per_day": ONE_ORDER_PER_TICKER_PER_DAY,
        },
        plan={
            "to_place": [
                {"ticker": o.ticker, "amount_usd": o.amount_usd, "side": o.side}
                for o in plan.to_place
            ],
            "skipped": [list(x) for x in plan.skipped],
            "refused": [list(x) for x in plan.refused],
            "total_notional_usd": plan.total_notional_usd,
        },
    )


def _week_key_to_str(key: tuple[int, int]) -> str:
    return f"{key[0]}-{key[1]:02d}"


def _week_key_from_str(raw: str) -> tuple[int, int]:
    """Parse a persisted ``"YYYY-WW"`` ISO week key, rejecting an impossible one.

    ``int()`` alone accepts a week number outside 1-53 (or 53 in a year with
    no 53rd ISO week), e.g. ``"2026-00"`` parses to ``(2026, 0)``. Every
    later lookup keys off ``iso_week_key()``'s own real output, which never
    produces that tuple, so a corrupted week key would silently miss on
    every comparison and ``week_notional()`` would read back ``0.0`` --
    forgetting the persisted weekly spend rather than raising
    ``StateCorruptError`` (Codex review: "Reject impossible ISO week keys").
    """
    year, week = raw.split("-")
    year, week = int(year), int(week)
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise ValueError(f"{raw!r} is not a valid ISO year-week") from exc
    return year, week


def _expected_weekly_rollover_reserve(
    reserve_week_key: tuple[int, int],
    finalized_budget_days,
    submitted_notional_by_week: dict,
) -> float:
    """Recompute the reserve ``StrategyState.roll_weekly_reserve()`` would derive
    for ``reserve_week_key`` from ``finalized_budget_days``/``submitted_notional_by_week``.

    Shared by ``_state_from_json``'s load-time cross-check and
    ``execute_plan()``'s placement-time cross-check: a week key alone only
    proves SOME reserve was rolled for that week, never that a given AMOUNT
    is what ``roll_weekly_reserve()`` would actually have derived -- e.g. a
    caller could supply ``weekly_rollover_reserve_usd=900`` paired with a
    real, current week key even though no prior week's activity justifies
    any reserve at all (Codex review: "Verify persisted rollover reserve
    against prior-week state"; "Verify rollover reserve before raising the
    weekly cap").
    """
    prior_weeks = [
        iso_week_key(d) for d in finalized_budget_days if iso_week_key(d) < reserve_week_key
    ]
    if not prior_weeks:
        return 0.0
    last_week_key = max(prior_weeks)
    unused_prev = max(
        0.0, NORMAL_WEEKLY_CAP_USD - submitted_notional_by_week.get(last_week_key, 0.0)
    )
    return min(WEEKLY_ROLLOVER_RESERVE_CAP_USD, unused_prev)


# Bumped only if the on-disk shape changes. Guards against a truncated-but-
# valid document (e.g. "{}") loading as a brand-new state: _state_from_json
# defaults every field, so without this check a partial document would
# silently erase the exact budget accounting load_strategy_state exists to
# protect (Codex review: "Reject incomplete valid-JSON state documents").
STATE_SCHEMA_VERSION = 1

# Every key save_strategy_state always writes. A document missing schema_version
# or any of these -- even if it is syntactically valid JSON -- is rejected
# rather than filled in with defaults.
_STATE_REQUIRED_KEYS = frozenset(
    {
        "buys_by_ticker_by_day",
        "submitted_notional_by_day",
        "submitted_notional_by_week",
        "weekly_rollover_reserve_usd",
        "finalized_budget_days",
        "reserve_week_key",
        "selected_tier",
    }
)

# Canonical tiers a persisted selected_tier must match exactly (name, caps,
# and uses_rollover_reserve), not merely name -- see _state_from_json.
_BUDGET_TIERS_BY_NAME = {t.name: t for t in BUDGET_TIERS}


def _validate_budget_tiers_against_config() -> None:
    """Cross-check the hard-coded ``near_close_dca.BUDGET_TIERS`` against the
    RESOLVED ``config/budgets.json``'s own ``budget_tiers`` list.

    ``BUDGET_TIERS`` is documented in ``near_close_dca.py`` as "NOT loaded
    from config at runtime; must be kept in sync with config/budgets.json".
    A repo-level test (``test_budget_tiers_match_config_budgets_json``)
    already pins the two together for the CHECKED-IN config, but
    ``$DCA_CONFIG_DIR`` can point this module's own ``CONFIG_DIR`` at an
    entirely different, deployment-time config directory. A
    ``budgets.json`` there with, say, Normal's daily cap lowered to $1 would
    silently disagree with the still-hard-coded $180 ``BUDGET_TIERS`` entry
    ``assess()``/``plan_orders()`` actually enforce, letting a real order
    clear the deployment's own documented cap (Codex review: "Reject
    overridden budget-tier drift").

    Caps and the rollover flag are not the only hard-coded values
    ``select_budget_tier()`` (in ``near_close_dca.py``) depends on:
    ``TIER_SPEND_THRESHOLDS_USD``/``TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS``
    decide WHICH tier applies at all, and neither is derived from config
    either. An override that changes only ``budgets.json``'s
    ``select_when`` boundaries (e.g. keeping Normal's cap at $180 but
    raising its spend threshold to $200) would not be caught by the
    cap/rollover-flag check above at all, yet would still make a $130 plan
    silently select Elevated's caps under the still-hard-coded $129
    threshold, or vice versa (Codex review: "Validate overridden
    tier-selection thresholds"). ``config/budgets.json``'s
    ``escalate_above_planned_spend_usd``/``escalate_above_high_multiplier_signals``
    fields on every tier but the last (the top tier has nothing above it to
    escalate to) are the structured, resolved-config source of truth these
    are cross-checked against.
    """
    configured = {t["name"]: t for t in _read_config("budgets.json")["budget_tiers"]}
    for i, tier in enumerate(BUDGET_TIERS):
        entry = configured.get(tier.name)
        if entry is None:
            raise ValueError(
                f"config/budgets.json's budget_tiers is missing an entry for {tier.name!r}, "
                "which near_close_dca.BUDGET_TIERS still hard-codes"
            )
        daily_cap = _positive_finite_config_number(
            entry["daily_cap_usd"], f"budget_tiers[{tier.name!r}].daily_cap_usd", "budgets.json"
        )
        weekly_cap = _positive_finite_config_number(
            entry["weekly_cap_usd"], f"budget_tiers[{tier.name!r}].weekly_cap_usd", "budgets.json"
        )
        uses_reserve = bool(entry.get("requires_rollover_reserve", False))
        if (
            round(daily_cap * 100) != round(tier.daily_cap_usd * 100)
            or round(weekly_cap * 100) != round(tier.weekly_cap_usd * 100)
            or uses_reserve != tier.uses_rollover_reserve
        ):
            raise ValueError(
                f"config/budgets.json's budget_tiers[{tier.name!r}] (daily={daily_cap!r}, "
                f"weekly={weekly_cap!r}, requires_rollover_reserve={uses_reserve!r}) does not "
                f"match near_close_dca.BUDGET_TIERS' hard-coded entry (daily="
                f"{tier.daily_cap_usd!r}, weekly={tier.weekly_cap_usd!r}, "
                f"uses_rollover_reserve={tier.uses_rollover_reserve!r})"
            )
        if i < len(BUDGET_TIERS) - 1:
            spend_threshold = _positive_finite_config_number(
                entry.get("escalate_above_planned_spend_usd"),
                f"budget_tiers[{tier.name!r}].escalate_above_planned_spend_usd",
                "budgets.json",
            )
            signal_threshold = entry.get("escalate_above_high_multiplier_signals")
            if (
                isinstance(signal_threshold, bool)
                or not isinstance(signal_threshold, int)
                or signal_threshold < 0
            ):
                raise ValueError(
                    f"config/budgets.json's budget_tiers[{tier.name!r}]."
                    "escalate_above_high_multiplier_signals must be a non-negative int, got "
                    f"{signal_threshold!r}"
                )
            if (
                round(spend_threshold * 100) != round(TIER_SPEND_THRESHOLDS_USD[i] * 100)
                or signal_threshold != TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS[i]
            ):
                raise ValueError(
                    f"config/budgets.json's budget_tiers[{tier.name!r}] escalation thresholds "
                    f"(spend={spend_threshold!r}, signals={signal_threshold!r}) do not match "
                    "near_close_dca's hard-coded selection boundaries (spend="
                    f"{TIER_SPEND_THRESHOLDS_USD[i]!r}, "
                    f"signals={TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS[i]!r})"
                )


def _load_max_orders_per_day() -> int:
    """Read circuit_breakers.max_orders_per_day from config/budgets.json.

    THE single knob for how many orders a day the roster may place, shared
    across all seven slots and manual buying. Change that one number to raise
    or lower the limit; no code edit is needed.

    Fails closed: a missing, non-integer, boolean, or non-positive value raises
    rather than defaulting, because a silently-defaulted count ceiling is
    exactly the kind of limit that stops binding without anyone noticing.
    """
    breakers = _read_config("budgets.json")["circuit_breakers"]
    try:
        value = breakers["max_orders_per_day"]
    except KeyError:
        raise ValueError(
            "config/budgets.json circuit_breakers.max_orders_per_day is missing; "
            "it is a required safety limit and has no default"
        ) from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "config/budgets.json circuit_breakers.max_orders_per_day must be an "
            f"integer, got {value!r}"
        )
    if value < 1:
        raise ValueError(
            "config/budgets.json circuit_breakers.max_orders_per_day must be >= 1, "
            f"got {value!r}"
        )
    return value


def _load_one_order_per_ticker_per_day() -> bool:
    """Read order_rules.one_order_per_ticker_per_day from config/budgets.json."""
    rules = _read_config("budgets.json")["order_rules"]
    try:
        value = rules["one_order_per_ticker_per_day"]
    except KeyError:
        raise ValueError(
            "config/budgets.json order_rules.one_order_per_ticker_per_day is missing"
        ) from None
    if not isinstance(value, bool):
        raise ValueError(
            "config/budgets.json order_rules.one_order_per_ticker_per_day must be a "
            f"boolean, got {value!r}"
        )
    return value


MAX_ORDERS_PER_DAY = _load_max_orders_per_day()
ONE_ORDER_PER_TICKER_PER_DAY = _load_one_order_per_ticker_per_day()


_validate_budget_tiers_against_config()


def _dedup_date_keyed(items, parse_value):
    """Build a ``date``-keyed dict from raw ``(str, value)`` pairs, rejecting
    two textual keys that normalize to the same date.

    ``date.fromisoformat()`` accepts both the extended ``YYYY-MM-DD`` and the
    basic, dash-free ISO spelling, so distinct JSON keys can alias to the
    same date and the later value would otherwise silently overwrite the
    earlier one -- the same risk already closed for week keys (Codex review:
    "Reject aliased persisted date keys").
    """
    seen: dict[date, str] = {}
    result: dict = {}
    for raw_key, raw_value in items:
        day = date.fromisoformat(raw_key)
        if day in seen:
            raise ValueError(
                f"keys {seen[day]!r} and {raw_key!r} both normalize to date "
                f"{day.isoformat()!r}; a duplicate entry is corrupt, not additive"
            )
        seen[day] = raw_key
        result[day] = parse_value(raw_value)
    return result


def _json_number(value: object, field: str) -> float:
    """Reject a persisted ledger value (or candidate field) that is not a
    usable JSON number.

    ``_state_to_json`` never writes a ``bool`` or a numeric string for a
    notional field, so a document carrying either is hand-edited or corrupt.
    ``float()`` would otherwise coerce both silently -- ``float(False) ==
    0.0`` and ``float("123.45") == 123.45`` -- so a persisted positive day or
    week spend replaced with ``false`` would load as ``0.0`` and let the
    rehydrated state authorize additional buys after forgetting prior spend
    (Codex review: "Reject non-number ledger values before float
    coercion"). ``bool`` is checked before ``(int, float)`` since ``bool`` is
    a subclass of ``int`` in Python.

    A syntactically valid but astronomically large JSON integer (e.g.
    ``10**1000``, which ``json.loads`` parses as a plain Python ``int`` of
    unbounded size) makes ``float()`` raise ``OverflowError`` rather than
    ``ValueError``. Neither ``assess()``'s malformed-candidate handler nor
    ``load_strategy_state()``'s corruption wrapper -- the two callers of
    this shared conversion -- catch that exception type, so it would crash
    the scheduled run outright instead of producing the documented
    ``ABORT``/``StateCorruptError`` (Codex review: "Convert oversized JSON
    numbers into a controlled rejection"). Converting it into the same
    ``ValueError`` this function already raises for every other unusable
    value lets both callers' existing handling cover it too.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a JSON number, got {value!r}")
    try:
        return float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} is too large to represent as a float, got {value!r}") from exc


def _finite_nonneg(value: float, field: str) -> float:
    """Reject a non-finite or negative persisted numeric field.

    ``json.loads`` accepts ``NaN``/``Infinity`` even though neither is valid
    JSON. A NaN ``weekly_rollover_reserve_usd`` makes
    ``StrategyState.effective_weekly_cap()`` evaluate to the tier's nominal
    cap regardless of the real reserve, inventing rollover capacity that was
    never earned (Codex review: "Reject semantically corrupt persisted budget
    state").
    """
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be a finite, non-negative number, got {value!r}")
    return value


def _cent_precise(value: float, field: str) -> float:
    """Reject a persisted ledger value with sub-cent precision.

    Every real notional this module ever writes is exactly cent-precision --
    ``record_buy()``'s ``notional_usd`` always traces back to an order's
    cent-rounded ``dollar_amount`` -- so a value like $899.004 cannot be
    legitimate regardless of what it happens to sum or round to. Cent-
    rounding both sides of the daily/weekly consistency check (below) before
    comparing would otherwise treat this kind of corruption as consistent
    whenever the two totals happen to round to the same cent (Codex review:
    "Reject sub-cent persisted ledger values").
    """
    if abs(value - round(value, 2)) > 1e-9:
        raise ValueError(f"{field} must be cent-precision, got {value!r}")
    return value


def _positive_int(value: object, field: str) -> int:
    """Reject a non-integer or non-positive persisted buy count.

    Checked BEFORE ``int(value)`` truncates it: a corrupt ``-0.5`` or ``0.5``
    would otherwise silently become ``0`` and pass a post-conversion
    non-negative check, and a reload would then report no prior buy for a
    ticker that already has one, permitting another same-day order (Codex
    review: "Reject fractional persisted buy counts"). Zero is rejected too,
    not merely negative: ``StrategyState.record_buy()`` only ever creates an
    entry by incrementing from ``buys_today()``'s own zero default, so a
    stored ``0`` never comes from legitimate use -- a real day/ticker with no
    buy simply has no entry at all. Trusting a persisted ``0`` at face value
    would let ``buys_today()`` report no prior buy for a ticker that has one,
    permitting a second same-day order once aggregate headroom allows it
    (Codex review: "Reject zero persisted buy counts").
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer, got {value!r}")
    return value


def _load_week_keyed_nonneg(raw: dict, field: str) -> dict[tuple[int, int], float]:
    """Load a week-keyed (str -> number) mapping into a validated tuple-keyed dict.

    Shared by the two ``reconcile_earlier_week_cross_stream_notional``
    bookkeeping fields (``earlier_week_cross_stream_seeded_usd``/
    ``..._poison_usd``): both are plain ISO-week-keyed dollar amounts with
    the same validity requirements as every other persisted dollar field
    (finite, non-negative), and the same aliased-key risk as
    ``submitted_notional_by_week`` (e.g. ``"2026-1"``/``"2026-01"`` both
    normalize to ``(2026, 1)``).
    """
    result: dict[tuple[int, int], float] = {}
    spellings: dict[tuple[int, int], str] = {}
    for k, v in raw.items():
        week_key = _week_key_from_str(k)
        if week_key in spellings:
            raise ValueError(
                f"{field} keys {spellings[week_key]!r} and {k!r} both normalize to ISO "
                f"week {week_key!r}; a duplicate entry is corrupt, not additive"
            )
        spellings[week_key] = k
        result[week_key] = _finite_nonneg(_json_number(v, field), field)
    return result


def _load_ticker_day_keyed_nonneg(raw: dict, field: str, *, parse_value) -> dict[str, dict[date, Any]]:
    """Load a ``{ticker: {date_str: value}}`` mapping, validating each value.

    Shared by ``reconcile_prior_allocation``'s two bookkeeping fields
    (``seeded_prior_context_amount_by_ticker_day``/``..._attempts_by_ticker_day``),
    which mirror ``buys_by_ticker_by_day``'s own nested shape. Ticker keys are
    used verbatim (not normalized/alias-checked the way
    ``buys_by_ticker_by_day`` is): these are pure idempotency bookkeeping, not
    a ledger real duplicate-buy decisions are keyed against directly, so the
    stricter handling that field needs would be more validation than this
    one's failure mode warrants.
    """
    result: dict[str, dict[date, Any]] = {}
    for ticker, per_day in raw.items():
        result[ticker] = _dedup_date_keyed(
            per_day.items(), lambda v: parse_value(v, field)
        )
    return result


def _state_to_json(state: StrategyState) -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "buys_by_ticker_by_day": {
            ticker: {d.isoformat(): n for d, n in per_day.items()}
            for ticker, per_day in state.buys_by_ticker_by_day.items()
        },
        "submitted_notional_by_day": {
            d.isoformat(): v for d, v in state.submitted_notional_by_day.items()
        },
        "submitted_notional_by_week": {
            _week_key_to_str(k): v for k, v in state.submitted_notional_by_week.items()
        },
        # Shared daily buy-ATTEMPT counter (config/budgets.json
        # circuit_breakers.max_orders_per_day), bumped alongside
        # buys_by_ticker_by_day/submitted_notional_by_day/_by_week in the same
        # record_buy()/seed_prior_allocation() call. Omitting it here (as this
        # field previously did) means every scheduled process reloading state
        # via state_transaction() sees an EMPTY submitted_order_count_by_day,
        # and _seed_prior_allocations() never repairs it for a ticker whose
        # buys_today() is already nonzero (it deliberately skips reseeding
        # that ticker) -- so prior accepted orders silently vanish from the
        # shared max_orders_per_day count and a later slot can exceed the
        # breaker (Codex review: "Persist the shared daily attempt counter").
        "submitted_order_count_by_day": {
            d.isoformat(): n for d, n in state.submitted_order_count_by_day.items()
        },
        # Idempotent-seeding bookkeeping for
        # reconcile_earlier_week_cross_stream_notional (Codex review:
        # "Reconcile earlier-week spend instead of adding it") -- must
        # survive across invocations (state_transaction() reloads this
        # persistent state fresh each time) the same way every other ledger
        # here does, or a fresh reload would see "nothing seeded yet this
        # week" and re-add the full earlier-week cross-stream total again.
        "earlier_week_cross_stream_seeded_usd": {
            _week_key_to_str(k): v
            for k, v in state.earlier_week_cross_stream_seeded_usd.items()
        },
        "earlier_week_cross_stream_poison_usd": {
            _week_key_to_str(k): v
            for k, v in state.earlier_week_cross_stream_poison_usd.items()
        },
        # Idempotent-seeding bookkeeping for reconcile_prior_allocation
        # (Codex review: "Reconcile increases to already-seeded ticker
        # allocations"), mirroring buys_by_ticker_by_day's own nested
        # ticker -> {day: value} shape. Must survive a process restart the
        # same way every other ledger here does, or a fresh reload would
        # forget how much of a ticker's aggregate has already been folded in
        # and either re-add it (if buys_today() happened to be 0) or fall
        # back to reconcile_prior_allocation's own conservative
        # ambiguous-baseline handling.
        "seeded_prior_context_amount_by_ticker_day": {
            ticker: {d.isoformat(): v for d, v in per_day.items()}
            for ticker, per_day in state.seeded_prior_context_amount_by_ticker_day.items()
        },
        "seeded_prior_context_attempts_by_ticker_day": {
            ticker: {d.isoformat(): v for d, v in per_day.items()}
            for ticker, per_day in state.seeded_prior_context_attempts_by_ticker_day.items()
        },
        "weekly_rollover_reserve_usd": state.weekly_rollover_reserve_usd,
        "finalized_budget_days": sorted(d.isoformat() for d in state.finalized_budget_days),
        "reserve_week_key": (
            _week_key_to_str(state.reserve_week_key) if state.reserve_week_key else None
        ),
        # Deliberately always null, never state.selected_tier: this field is
        # run-scoped (near_close_dca.StrategyState.select_daily_budget() only
        # (re)computes it when None), but the documented wiring reloads state
        # before EVERY scheduled invocation across every day. Round-tripping
        # the real value would carry a Stress day's $360/$1,800 tier into a
        # later day whose candidates justify only Normal's $180/$900,
        # authorizing real spend above what that day's own signals justify
        # (Codex review: "Do not persist a run-scoped budget tier across
        # runs"). No day key exists on this field to validate freshness
        # against, so omitting it entirely -- rather than persisting and
        # checking a trading day -- is the safe fix.
        "selected_tier": None,
    }


def _state_from_json(raw: dict) -> StrategyState:
    state = StrategyState()
    ticker_spellings: dict[str, str] = {}
    state.buys_by_ticker_by_day = {}
    for ticker, per_day in raw.get("buys_by_ticker_by_day", {}).items():
        # Normalized the same way runtime lookups (buys_today()) key off a
        # ticker, so a persisted "aapl" or " AAPL " loads under a key no
        # runtime comparison would ever produce -- buys_today("AAPL", day)
        # would then report zero even though this document proves a buy
        # happened, permitting another same-day buy (Codex review:
        # "Normalize persisted ticker keys before restoring counts"). Two
        # distinct raw spellings normalizing to the same ticker are rejected
        # outright, the same as an aliased date/week key: which of the two
        # per-day maps is authoritative is not something this loader can
        # decide.
        normalized_ticker = _normalize_ticker(ticker)
        if not normalized_ticker:
            # A whitespace-only key normalizes to "" and would otherwise
            # load successfully whenever its daily/weekly ledger entries
            # happen to agree -- this state can never have been produced by
            # record_buy(), the only runtime writer, which always uses a
            # real ticker string (Codex review: "Reject empty or
            # non-whitelisted persisted ticker keys"). Membership in the
            # CURRENT ACTIVE_TICKERS is deliberately NOT required here,
            # unlike an empty key: buys_by_ticker_by_day retains historical
            # entries indefinitely, while ACTIVE_TICKERS reflects only
            # today's config -- a ticker legitimately bought in the past and
            # later removed from the active universe must not make every
            # subsequent load_strategy_state() call abort (Codex review:
            # "Preserve historical ticker counts after whitelist changes").
            raise ValueError(
                f"buys_by_ticker_by_day key {ticker!r} normalizes to an empty ticker"
            )
        if normalized_ticker in ticker_spellings:
            raise ValueError(
                f"buys_by_ticker_by_day keys {ticker_spellings[normalized_ticker]!r} and "
                f"{ticker!r} both normalize to {normalized_ticker!r}; a duplicate entry is "
                "corrupt, not additive"
            )
        ticker_spellings[normalized_ticker] = ticker
        state.buys_by_ticker_by_day[normalized_ticker] = _dedup_date_keyed(
            per_day.items(), lambda n: _positive_int(n, "buys_by_ticker_by_day count")
        )
    state.submitted_notional_by_day = _dedup_date_keyed(
        raw.get("submitted_notional_by_day", {}).items(),
        lambda v: _cent_precise(
            _finite_nonneg(
                _json_number(v, "submitted_notional_by_day"), "submitted_notional_by_day"
            ),
            "submitted_notional_by_day",
        ),
    )
    week_key_spellings: dict[tuple[int, int], str] = {}
    state.submitted_notional_by_week = {}
    for k, v in raw.get("submitted_notional_by_week", {}).items():
        week_key = _week_key_from_str(k)
        if week_key in week_key_spellings:
            # Distinct textual keys can normalize to the same ISO week (e.g.
            # "2026-1" and "2026-01" both parse to (2026, 1)): the duplicate-
            # key JSON hook only catches two literally identical keys, not
            # two different spellings that alias to the same tuple, so the
            # later one here would otherwise silently overwrite the earlier
            # rather than raising (Codex review: "Reject aliases of
            # persisted week keys").
            raise ValueError(
                f"submitted_notional_by_week keys {week_key_spellings[week_key]!r} and "
                f"{k!r} both normalize to ISO week {week_key!r}; a duplicate entry is "
                "corrupt, not additive"
            )
        week_key_spellings[week_key] = k
        state.submitted_notional_by_week[week_key] = _cent_precise(
            _finite_nonneg(
                _json_number(v, "submitted_notional_by_week"), "submitted_notional_by_week"
            ),
            "submitted_notional_by_week",
        )
    # record_buy() always updates the day and week ledgers together in the
    # same call, and neither is ever pruned independently of the other, so
    # under any legitimately-written state each week's persisted total must
    # be AT LEAST the sum of that week's daily entries -- it can legitimately
    # exceed that sum too, since bump_week_notional_only()/
    # force_week_notional_to_worst_case_ceiling()/
    # reconcile_earlier_week_cross_stream_notional() add EARLIER-weekday
    # cross-stream spend (or a fail-closed ceiling) to the week bucket ONLY,
    # deliberately without a corresponding submitted_notional_by_day entry in
    # THIS state (that spend belongs to a sibling stream or an earlier day's
    # own history, not a ticker/day this state tracks -- Codex review:
    # "Include prior-week sibling spend in the afternoon ledger"). A week
    # total UNDER its daily sum -- e.g. $900 recorded for a Monday but $0 for
    # that ISO week -- is still corrupt: StrategyState.week_notional() would
    # silently forget real spend and authorize another week's worth of buys
    # even though the same document proves it (Codex review: "Reject
    # inconsistent persisted daily and weekly ledgers"). Compared in whole
    # cents, not with a flat 0.01 tolerance: a tolerance of exactly one cent
    # also accepts a real, corrupt one-cent shortfall (e.g. daily entries
    # summing to $899.01 against a persisted $899.00) as if it were float
    # non-associativity noise, when floating-point subtraction of the two can
    # land just under that threshold even for a real discrepancy (Codex
    # review: "Reject cent-level persisted ledger mismatches").
    expected_week_totals: dict[tuple[int, int], float] = {}
    for d, amount in state.submitted_notional_by_day.items():
        wk = iso_week_key(d)
        expected_week_totals[wk] = expected_week_totals.get(wk, 0.0) + amount
    for wk in set(expected_week_totals) | set(state.submitted_notional_by_week):
        expected = expected_week_totals.get(wk, 0.0)
        persisted = state.submitted_notional_by_week.get(wk, 0.0)
        if _cents(persisted) < _cents(expected):
            raise ValueError(
                f"submitted_notional_by_week for {wk!r} is {persisted!r} but its daily "
                f"entries in submitted_notional_by_day sum to {expected!r}; the week "
                "total can never fall short of its own daily entries"
            )
    # A buys_by_ticker_by_day entry no longer implies positive notional in
    # THIS direction: record_buy() always pairs a buy count with positive
    # notional, but seed_prior_allocation() also bumps buys_by_ticker_by_day
    # by attempt_count for a RECONSTRUCTED zero-capital attempt (a candidate
    # that obtained an order_id but was confirmed rejected/canceled, or a
    # placement that raised) -- an all-rejected day for a ticker is a
    # legitimate buy-count-with-zero-notional state, not corruption (Codex
    # review: "Keep zero-notional attempts out of the buy ledger" -- the
    # stricter check previously here made save_strategy_state() itself
    # reject this state via its own pre-save validation, turning an
    # otherwise-valid reconstruction into StateCorruptError on every day
    # with an all-zero-notional prior attempt). The submitted_order_count_by_
    # day cross-check above already verifies buys_by_ticker_by_day's counts
    # are internally consistent, so this direction is deliberately NOT
    # re-checked against notional.
    #
    # The inverse direction is still corrupt, and IS still checked: record_buy()
    # is the only runtime writer of all three structures, always together, so a positive
    # submitted_notional_by_day entry can never legitimately exist with no
    # ticker's buy count on that date. Without this, a document with real
    # notional but an empty buys_by_ticker_by_day loads successfully, and
    # buys_today() reports zero for every ticker, permitting another
    # same-day buy of one despite the same document proving spend already
    # happened (Codex review: "Reject notional days with no persisted buy
    # count").
    days_with_a_buy_count = {d for per_day in state.buys_by_ticker_by_day.values() for d in per_day}
    for d, amount in state.submitted_notional_by_day.items():
        if amount > 0 and d not in days_with_a_buy_count:
            raise ValueError(
                f"submitted_notional_by_day has a positive entry for {d.isoformat()!r} but "
                "no ticker in buys_by_ticker_by_day has a buy count for that date"
            )
    # record_buy()/seed_prior_allocation() always bump submitted_order_count_by_day
    # and buys_by_ticker_by_day together, by the exact same amount, in the same
    # call -- so a genuinely current document's per-day attempt count must equal
    # the sum of that day's per-ticker buy counts. A document written BEFORE this
    # field existed never persisted it at all (raw.get(...) returning None, not
    # {}, distinguishes "never had this key" from "had it and it was empty");
    # reconstruct it losslessly from buys_by_ticker_by_day's per-day sum rather
    # than loading an empty counter and letting _seed_prior_allocations() skip
    # reseeding it for every ticker whose buys_today() is already nonzero,
    # silently forgetting real prior attempts against max_orders_per_day (Codex
    # review: "Persist the shared daily attempt counter").
    day_buy_count_sums: dict[date, int] = {}
    for per_day in state.buys_by_ticker_by_day.values():
        for d, n in per_day.items():
            day_buy_count_sums[d] = day_buy_count_sums.get(d, 0) + n
    raw_order_counts = raw.get("submitted_order_count_by_day")
    if raw_order_counts is None:
        state.submitted_order_count_by_day = dict(day_buy_count_sums)
    else:
        state.submitted_order_count_by_day = _dedup_date_keyed(
            raw_order_counts.items(),
            lambda n: _positive_int(n, "submitted_order_count_by_day"),
        )
        for d in set(state.submitted_order_count_by_day) | set(day_buy_count_sums):
            expected = day_buy_count_sums.get(d, 0)
            persisted = state.submitted_order_count_by_day.get(d, 0)
            if persisted != expected:
                raise ValueError(
                    f"submitted_order_count_by_day for {d.isoformat()!r} is {persisted!r} "
                    "but buys_by_ticker_by_day's per-ticker counts for that date sum to "
                    f"{expected!r}; the two must agree"
                )
    # Bookkeeping only (no equivalent field existed before, so an absent key
    # loads as "nothing seeded/poisoned yet" -- correct for both a legacy
    # document and a fresh state).
    state.earlier_week_cross_stream_seeded_usd = _load_week_keyed_nonneg(
        raw.get("earlier_week_cross_stream_seeded_usd", {}),
        "earlier_week_cross_stream_seeded_usd",
    )
    state.earlier_week_cross_stream_poison_usd = _load_week_keyed_nonneg(
        raw.get("earlier_week_cross_stream_poison_usd", {}),
        "earlier_week_cross_stream_poison_usd",
    )
    # reconcile_earlier_week_cross_stream_notional() only ever writes a
    # seeded-so-far value that is <= the weekly bucket it just folded it
    # into (the incremental delta it adds can never be negative, so the
    # bucket only grows to cover at least what's recorded as seeded). A
    # persisted seeded value EXCEEDING the weekly bucket is impossible from
    # any legitimate write path: reconcile computes
    # incremental = total_usd - already_seeded and would silently compute a
    # NEGATIVE increment against that inflated baseline on the next call,
    # skip adding real earlier-week spend just discovered, and permanently
    # forget it -- letting the shared weekly cap be exceeded (Codex review:
    # "Reject seeded bookkeeping that exceeds the weekly ledger").
    for wk, seeded in state.earlier_week_cross_stream_seeded_usd.items():
        bucket = state.submitted_notional_by_week.get(wk, 0.0)
        if _cents(seeded) > _cents(bucket):
            raise ValueError(
                f"earlier_week_cross_stream_seeded_usd for {wk!r} is {seeded!r} but "
                f"submitted_notional_by_week has only {bucket!r} for that ISO week; the "
                "seeded total can never exceed the weekly bucket it was folded into"
            )
    # Bookkeeping only, same absent-key handling as the week-keyed fields
    # above (Codex review: "Reconcile increases to already-seeded ticker
    # allocations").
    state.seeded_prior_context_amount_by_ticker_day = _load_ticker_day_keyed_nonneg(
        raw.get("seeded_prior_context_amount_by_ticker_day", {}),
        "seeded_prior_context_amount_by_ticker_day",
        parse_value=lambda v, field: _finite_nonneg(_json_number(v, field), field),
    )
    state.seeded_prior_context_attempts_by_ticker_day = _load_ticker_day_keyed_nonneg(
        raw.get("seeded_prior_context_attempts_by_ticker_day", {}),
        "seeded_prior_context_attempts_by_ticker_day",
        parse_value=lambda v, field: _positive_int(v, field),
    )
    # reconcile_prior_allocation() only ever writes a seeded attempt count
    # that is <= buys_by_ticker_by_day's own count for that exact
    # (ticker, day) -- and a seeded dollar amount that is <= the DAY-level
    # submitted_notional_by_day total (a looser bound, since that field
    # aggregates every ticker, not just this one, but still a real ceiling
    # no single ticker's seeded portion can exceed). A persisted seeded
    # value beyond either bound is impossible from any legitimate write
    # path and would let a later reconciliation compute a negative
    # increment, permanently forgetting real evidence the same way an
    # inflated earlier_week_cross_stream_seeded_usd would (Codex review:
    # "Reject seeded bookkeeping that exceeds the weekly ledger" -- the same
    # class of corruption, applied to this per-ticker bookkeeping).
    for ticker, per_day in state.seeded_prior_context_attempts_by_ticker_day.items():
        for d, seeded_attempts in per_day.items():
            actual_attempts = state.buys_by_ticker_by_day.get(ticker, {}).get(d, 0)
            if seeded_attempts > actual_attempts:
                raise ValueError(
                    f"seeded_prior_context_attempts_by_ticker_day for {ticker!r} on "
                    f"{d.isoformat()!r} is {seeded_attempts!r} but buys_by_ticker_by_day "
                    f"has only {actual_attempts!r} for that ticker/date; the seeded count "
                    "can never exceed the buy ledger it was folded into"
                )
    for ticker, per_day in state.seeded_prior_context_amount_by_ticker_day.items():
        for d, seeded_amount in per_day.items():
            day_total = state.submitted_notional_by_day.get(d, 0.0)
            if _cents(seeded_amount) > _cents(day_total):
                raise ValueError(
                    f"seeded_prior_context_amount_by_ticker_day for {ticker!r} on "
                    f"{d.isoformat()!r} is {seeded_amount!r} but submitted_notional_by_day "
                    f"has only {day_total!r} for that date; a single ticker's seeded amount "
                    "can never exceed the day's total notional"
                )
    state.weekly_rollover_reserve_usd = _finite_nonneg(
        _json_number(raw.get("weekly_rollover_reserve_usd", 0.0), "weekly_rollover_reserve_usd"),
        "weekly_rollover_reserve_usd",
    )
    raw_finalized_days = raw.get("finalized_budget_days", [])
    if not isinstance(raw_finalized_days, list) or not all(
        isinstance(d, str) for d in raw_finalized_days
    ):
        # _state_to_json only ever emits a list of ISO date strings. A dict
        # instead (e.g. {"2026-07-24": "invented"}) would otherwise iterate
        # silently over its KEYS here, which look like valid date strings and
        # parse fine, letting a structurally malformed document read as a
        # legitimate list of finalized days -- and, downstream, as prior-week
        # activity with zero spend, justifying rollover capacity that was
        # never earned (Codex review: "Reject non-list finalized-day state").
        raise ValueError(
            "finalized_budget_days must be a list of ISO date strings, got "
            f"{raw_finalized_days!r}"
        )
    state.finalized_budget_days = {date.fromisoformat(d) for d in raw_finalized_days}
    for finalized_day in state.finalized_budget_days:
        # A finalized_budget_days entry is accepted so far purely because it
        # parses as a date -- but a corrupt or hand-edited weekend/holiday
        # entry would still count as prior-week activity below
        # (_expected_weekly_rollover_reserve), even though Gate 0 could
        # never have finalized a live budget on a day the exchange was
        # closed. E.g. finalized_budget_days={2026-07-25} (a Saturday) with
        # an empty prior-week ledger makes a full, unearned reserve look
        # legitimate (Codex review: "Reject non-trading finalized days
        # before granting reserve").
        try:
            finalized_day_status = market_day_status(finalized_day)
        except CalendarCoverageError as exc:
            raise ValueError(
                f"finalized_budget_days contains {finalized_day.isoformat()!r}, which "
                f"market_day_status() cannot verify: {exc}"
            ) from exc
        if finalized_day_status.is_closed:
            raise ValueError(
                f"finalized_budget_days contains {finalized_day.isoformat()!r}, which "
                "market_day_status() reports as a closed trading day (weekend or holiday); "
                "Gate 0 could never have finalized a live budget on a day the exchange was shut"
            )
    key = raw.get("reserve_week_key")
    state.reserve_week_key = _week_key_from_str(key) if key else None
    if state.weekly_rollover_reserve_usd != 0.0 and state.reserve_week_key is None:
        # A real reserve is never legitimately unkeyed -- roll_weekly_reserve
        # always stamps reserve_week_key in the same write that sets a nonzero
        # weekly_rollover_reserve_usd. A document combining the two is either
        # hand-edited or corrupt, and trusting it would let assess() treat the
        # reserve as fresh forever, since a caller has no other way to tell it
        # apart from one just rolled for the current week (Codex review:
        # "Require a week key for every nonzero rollover reserve").
        raise ValueError(
            "weekly_rollover_reserve_usd is nonzero but reserve_week_key is null; a real "
            "reserve is always stamped with the week it was rolled for"
        )
    if state.reserve_week_key is not None:
        # A week key alone only proves SOME reserve was rolled for that week,
        # never that this AMOUNT -- including zero -- is what
        # roll_weekly_reserve() would actually have derived from the
        # persisted finalized_budget_days and prior-week
        # submitted_notional_by_week -- e.g. a document with no finalized days
        # and no spend anywhere, but weekly_rollover_reserve_usd=900 paired
        # with a real week key, satisfies every check above even though the
        # legitimate reserve for that state is $0. This check previously ran
        # only when weekly_rollover_reserve_usd was nonzero, so the mirror-image
        # corruption -- weekly_rollover_reserve_usd=0 paired with a real week
        # key, even though a finalized prior week with no spend actually
        # justifies the full $900 -- loaded successfully and silently capped
        # Stress-tier weekly capacity at Normal's $900 for the whole week
        # instead of the full $1,800 (Codex review: "Validate zero rollover
        # reserves against persisted history"). Recomputing and comparing
        # whenever a week key is present at all -- not only when the stored
        # amount is nonzero -- closes both directions the same way the
        # daily/weekly ledger sums are cross-checked above (Codex review:
        # "Verify persisted rollover reserve against prior-week state").
        expected_reserve = _expected_weekly_rollover_reserve(
            state.reserve_week_key, state.finalized_budget_days, state.submitted_notional_by_week
        )
        if _cents(state.weekly_rollover_reserve_usd) != _cents(expected_reserve):
            raise ValueError(
                f"weekly_rollover_reserve_usd={state.weekly_rollover_reserve_usd!r} does not "
                f"match {expected_reserve!r}, the amount roll_weekly_reserve() would derive "
                "from this document's own finalized_budget_days and submitted_notional_by_week"
            )
    # Deliberately discarded regardless of what the document contains: this
    # field is run-scoped (near_close_dca.StrategyState.select_daily_budget()
    # only (re)computes it when None), but state is reloaded before EVERY
    # scheduled invocation across every day, with no day key on the field to
    # validate freshness against. Round-tripping a real value would carry a
    # Stress day's $360/$1,800 tier into a later day whose candidates justify
    # only Normal's $180/$900, authorizing real spend above what that day's
    # own signals justify (Codex review: "Do not persist a run-scoped budget
    # tier across runs"). _state_to_json now always writes null for this key;
    # this branch also tolerates an old pre-fix file that still carries a
    # real tier object, by never trusting it either way.
    state.selected_tier = None
    return state


def _default_state_path() -> Path:
    """Default durable-state location when the caller omits ``path``.

    ``docs/live_wiring.md``'s worked example calls ``load_strategy_state()`` /
    ``save_strategy_state(state)`` with no path, but this module's
    implementations required ``path`` positionally, raising ``TypeError``
    before the documented example ever ran (Codex review: "Supply the
    documented default state path"). Resolved through the shared
    ``dca_paths`` helper (honors ``$DCA_CRON_TRACKING_ROOT``) rather than a
    bare relative path, which resolves against whatever directory the
    process happens to be in.
    """
    return Path(stream_root("dca_runner")) / "strategy_state.json"


def load_strategy_state(path=None) -> StrategyState:
    """Load durable state. Missing file is fine; unreadable file is fatal."""
    path = Path(path) if path is not None else _default_state_path()
    try:
        state_file_exists = path.exists()
    except OSError as exc:
        # A prior round wrapped read_bytes()'s own OSError, but this stat
        # probe ran BEFORE that guarded block: an inaccessible ancestor
        # directory or another filesystem error makes Path.exists() itself
        # raise, not just return False, escaping this function's documented
        # StateCorruptError fail-closed contract with a raw exception before
        # Gate 9 could ever classify it as corruption (Codex review: "Wrap
        # state stat failures as StateCorruptError"). A genuinely missing
        # file is unaffected: exists() returns False for that, it does not
        # raise.
        raise StateCorruptError(
            f"{path} could not be checked for existence ({exc}). Refusing to continue with "
            "a fresh state: that would silently reset the weekly and rollover accounting."
        ) from exc
    if not state_file_exists:
        return StrategyState()
    try:
        raw = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
    except OSError as exc:
        # path.exists() above only proves the file was there a moment ago --
        # a permissions change, a filesystem I/O error, or the file being
        # removed between that check and this read all raise OSError from
        # read_bytes() itself, which neither UnicodeDecodeError nor ValueError
        # (below) catches. Left uncaught, that would escape this function's
        # documented StateCorruptError fail-closed contract with a raw
        # exception instead, and state_transaction() would exit before ever
        # yielding a state to save (Codex review: "Convert durable-state read
        # failures to StateCorruptError").
        raise StateCorruptError(
            f"{path} exists but could not be read ({exc}). Refusing to continue with a "
            "fresh state: that would silently reset the weekly and rollover accounting."
        ) from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise StateCorruptError(
            f"{path} exists but is not valid JSON. Refusing to continue with a fresh "
            "state: that would silently reset the weekly and rollover accounting."
        ) from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != STATE_SCHEMA_VERSION
        or not _STATE_REQUIRED_KEYS.issubset(raw)
    ):
        # A syntactically valid but incomplete document (e.g. "{}") is exactly
        # as dangerous as a truncated one: _state_from_json would default every
        # missing field and load it as a fresh state, silently wiping the
        # weekly and rollover accounting this loader exists to protect.
        raise StateCorruptError(
            f"{path} is valid JSON but is missing schema_version={STATE_SCHEMA_VERSION} "
            f"or one of the required keys {sorted(_STATE_REQUIRED_KEYS)}. Refusing to "
            "continue with a fresh state: that would silently reset the weekly and "
            "rollover accounting."
        )
    try:
        return _state_from_json(raw)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StateCorruptError(f"{path} is not a readable StrategyState document.") from exc


def save_strategy_state(state: StrategyState, path=None, *, _fail_after_temp_write: bool = False) -> None:
    """Write state atomically and durably via :func:`dca_fileio.atomic_open_w`.

    A crash mid-write must leave the previous good state intact rather than a
    truncated file, which ``load_strategy_state`` would then treat as fatal.
    ``path`` defaults to :func:`_default_state_path` when omitted, matching
    ``load_strategy_state``.

    Writing directly with ``tmp.write_text()`` then ``os.replace()`` (the
    prior implementation) makes the rename atomic and gives every unique
    temp name its own path (Codex review: "Serialize concurrent
    durable-state updates"), but neither the temp file's contents nor the
    parent directory are ever fsynced -- a host power loss shortly after
    this function returns can still lose the write or the rename itself,
    silently reviving the previous state despite the caller having been told
    the accepted-order and rollover accounting was saved.
    ``dca_fileio.atomic_open_w`` already flushes and fsyncs the file before
    the swap and fsyncs the directory after it, on top of the same unique
    per-call temp name and atomic-replace behavior this function relied on
    before (Codex review: "Fsync durable state before reporting a
    successful save").

    ``_fail_after_temp_write`` is a test seam for the crash path.
    """
    payload = _state_to_json(state)
    try:
        # Validated against load_strategy_state's own invariants BEFORE
        # anything is written: a semantically invalid in-memory state (e.g.
        # record_buy() called with a NaN or negative notional) would
        # otherwise be written successfully and os.replace() would destroy
        # the previous good file, and the very next load_strategy_state()
        # call would then reject the document it just wrote, breaking every
        # subsequent scheduled invocation until someone repairs the file by
        # hand (Codex review: "Validate state before replacing the durable
        # file").
        _state_from_json(payload)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StateCorruptError(
            f"refusing to persist state: it would not itself pass load_strategy_state's own "
            f"invariants ({exc}); the previous durable file is left untouched"
        ) from exc
    path = Path(path) if path is not None else _default_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False is a second, independent guard specifically against
    # NaN/Infinity: json.dumps otherwise silently emits the nonstandard bare
    # NaN/Infinity/-Infinity literals, which round-trip back through THIS
    # module's own json.loads (Python accepts them by default) but are
    # invalid per the JSON spec and may not survive other tooling.
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    with atomic_open_w(path, encoding="utf-8") as handle:
        handle.write(text)
        if _fail_after_temp_write:
            # Raised INSIDE atomic_open_w's guarded block, before its own
            # fsync/replace/directory-fsync ever run: the temp file is
            # discarded (never replaces `path`) and this exception
            # propagates, exactly like a real crash between the write and
            # the swap.
            raise RuntimeError("simulated crash after temp write")


def _state_lock_path(path: Path) -> Path:
    """Sibling lock-file path for ``path``, used by :func:`state_transaction`."""
    return path.with_name(path.name + ".lock")


def _acquire_exclusive_lock(lock_file) -> None:
    """Block until an exclusive, cross-process OS lock on ``lock_file`` is held.

    POSIX uses ``fcntl.flock``, which blocks indefinitely until the lock is
    free. The standard library has no equivalent on Windows: ``msvcrt.locking``
    only offers a lock call that retries internally for roughly ten seconds
    and then raises ``PermissionError`` instead of blocking forever, so this
    loops that call until it succeeds -- matching ``fcntl.flock(LOCK_EX)``'s
    indefinite-block behavior using only the standard library on either
    platform (Codex review: "Provide a portable state-lock backend").
    """
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                return
            except PermissionError:
                # PermissionError is the documented transient-contention
                # condition described above -- msvcrt.locking's internal
                # ~10s retry window expired without acquiring the lock, not
                # a permanent failure. A bare `except OSError` also caught
                # every OTHER OSError (a bad descriptor, a permanent
                # filesystem/I/O failure), looping forever -- potentially
                # without delay, since a non-contention OSError can raise
                # immediately rather than after msvcrt's own ~10s internal
                # wait -- instead of propagating so state_transaction() can
                # fail closed and the scheduled run can persist evidence and
                # alert (Codex review: "Stop retrying permanent Windows lock
                # errors").
                continue
    else:
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_EX)


def _release_exclusive_lock(lock_file) -> None:
    """Release a lock acquired by :func:`_acquire_exclusive_lock`."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextmanager
def state_transaction(path=None):
    """Hold an exclusive, cross-process lock across a load-mutate-save cycle.

    ``load_strategy_state()``/``save_strategy_state()`` are each individually
    safe (atomic write, corrupt-file detection), but nothing serializes the
    WHOLE load-mutate-save transaction between them: two overlapping scheduled
    invocations can each load the same prior state, mutate their own in-memory
    copy independently (e.g. via ``record_buy()``), and then save -- the last
    writer wins and silently overwrites the other invocation's new buy counts
    and notional totals with a stale payload that never saw them (Codex
    review: "Serialize concurrent durable-state updates").

    Usage mirrors ``docs/live_wiring.md``'s documented
    ``load_strategy_state()``/``try``/``finally: save_strategy_state()``
    pattern, but as a context manager so the lock is always released and the
    state is always saved, on both a clean exit AND an exception::

        with state_transaction() as state:
            run_pipeline(..., state, ...)

    The state is saved even when the ``with`` block raises, mirroring
    ``docs/live_wiring.md``'s own documented reason for saving from a
    ``finally`` rather than only on normal return: ``state.record_buy()``
    mutates `state` in memory as soon as the broker accepts each order, long
    before ``run_pipeline()`` (or any later artifact/reporting step) returns
    or raises. An exception here is typically that LATER failure, after real
    orders already placed were recorded -- discarding those mutations because
    something downstream raised would let the next invocation reload
    pre-order totals and re-submit, or exceed a later cap, for buys that
    already happened (Codex review: "Persist partial state before releasing
    the transaction"). The save itself still runs before the lock is
    released, and an exception it raises (e.g. the mutated state fails its
    own pre-write validation) replaces/chains the original exception rather
    than silently discarding whichever one is more important -- either way,
    the caller learns something went wrong.

    The lock backend is chosen per platform by :func:`_acquire_exclusive_lock`/
    :func:`_release_exclusive_lock` (``fcntl`` on POSIX, ``msvcrt`` on
    Windows), both of which import their platform module lazily, inside the
    helper function, rather than at module level: an unconditional top-level
    import would make importing ``dca_runner`` at all raise
    ``ModuleNotFoundError`` on the platform lacking that module, even for a
    caller that never calls this function (Codex review: "Avoid importing
    the POSIX-only locking module unconditionally"; "Provide a portable
    state-lock backend").
    """
    resolved_path = Path(path) if path is not None else _default_state_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _state_lock_path(resolved_path)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        _acquire_exclusive_lock(lock_file)
        try:
            state = load_strategy_state(resolved_path)
            try:
                yield state
            finally:
                save_strategy_state(state, resolved_path)
        finally:
            _release_exclusive_lock(lock_file)
