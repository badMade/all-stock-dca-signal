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

See ``tests/test_dca_runner.py`` for the behavioural contract; it was written first.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time as time_module
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dca_config import get_config_dir
from dca_fileio import atomic_open_w
from dca_live_adapters import (
    _FILL_EVIDENCE_EXPLICIT_ZERO,
    _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE,
    _PLACEMENT_EXECUTION_FIELDS,
    _combined_fill_evidence,
)
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
    _normalized_order_id,
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
    "PlacementFailure",
    "REFUSED_OVER_PER_ORDER_CAP",
    "REFUSED_INVALID_AMOUNT",
    "REFUSED_ALREADY_BOUGHT_TODAY",
    "REFUSED_NOT_ACTIVE_TICKER",
    "REFUSED_INCONSISTENT_MULTIPLIER",
    "REFUSED_NON_CENT_AMOUNT",
    "REFUSED_BELOW_BROKER_MINIMUM_ORDER_SIZE",
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
    "count_compatible_executor_attempts",
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
REFUSED_BELOW_BROKER_MINIMUM_ORDER_SIZE = "below_broker_minimum_order_size"
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


def _read_config(name: str) -> dict:
    return json.loads(
        (CONFIG_DIR / name).read_bytes(), object_pairs_hook=_reject_duplicate_keys
    )


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


def _validated_max_consecutive_placement_failures(value: object) -> int:
    """Validate ``circuit_breakers.max_consecutive_placement_failures``.

    A per-ticker rejection no longer stops the batch (see ``execute_plan``),
    so this is the remaining backstop against continuing to hammer a broker
    that is systemically refusing everything -- an outage, an expired
    session, an account-level block, or exhausted buying power. Validated the
    same way as every other config-sourced live limit rather than trusted:
    ``bool`` is checked before ``int`` (``bool`` is an ``int`` subclass, so
    ``True`` would otherwise become a threshold of 1 and restore the exact
    halt-on-first-rejection behavior this knob exists to replace), and a
    float is refused rather than truncated since a count of attempts is
    inherently whole (Codex-style review of the 2026-08-24 policy change).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "budgets.json 'circuit_breakers.max_consecutive_placement_failures' must be "
            f"an integer, got {value!r}"
        )
    if value < 1:
        raise ValueError(
            "budgets.json 'circuit_breakers.max_consecutive_placement_failures' must be "
            f">= 1, got {value!r}"
        )
    return value


# How many placements may fail BACK TO BACK before execute_plan() stops
# attempting the remainder. A single ticker's rejection is that ticker's
# problem (an untradable symbol, a stale quote) and must not cost every
# ticker behind it its buy -- the 2026-08-24 incident this replaced was
# exactly that: "GS, EVR: never submitted -- batch halted after RVII
# rejection". A RUN of failures is a different signal: it means the broker
# leg itself is unhealthy, and continuing would just submit more doomed
# orders. Sourced from config so the threshold is a knob, not a literal
# buried in a placement loop.
MAX_CONSECUTIVE_PLACEMENT_FAILURES = _validated_max_consecutive_placement_failures(
    _read_config("budgets.json")["circuit_breakers"]["max_consecutive_placement_failures"]
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
        not isinstance(active, list)
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
    # The window opens AT nominal (9:35), never before: the 9:35 decision reads the
    # completed 9:30 bar, so an earlier fire has no decision bar to trade on.
    "0935": SlotSpec("0935", (9, 35), (9, 35), (10, 0), "market-open"),
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

        This start-of-run decision checks the nominal slot time. The fresh
        placement-clock check also validates the regular exchange session,
        independently of the slot's acceptance window, so a delayed order
        cannot cross the 1:00 PM early close.
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
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
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
    # None, not "agentic": "agentic" is exactly the provenance the Gate 4
    # grace path treats as the compatible executor, so a default of "agentic"
    # made every row from a history wrapper that never populates provenance
    # (a manual app order included) look compatible during a grace window,
    # contradicting gate_sole_executor's own "a manual order is still a second
    # writer" contract. Adapters that KNOW the provenance set it explicitly;
    # unknown fails closed as rogue (2026-09-07 diagnostic, C:F3).
    placed_agent: str | None = None
    state: str = "filled"
    executed_dollar_amount: float | None = None
    # Deterministic client reference used to reconcile an id-less placement
    # after the connector lost its response. Broker history adapters may map
    # this to ref_id/client_order_uuid as appropriate.
    client_order_id: str | None = None
    # Client-supplied idempotency key.  Ambiguous placement records retain it
    # so reconciliation can query and bind the exact broker order before a
    # reservation is released or the ticker is retried.
    ref_id: str | None = None

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


def count_compatible_executor_attempts(
    orders,
    trading_day: date,
    *,
    self_client_ref_prefix: str,
    compatible_agent_value: str = "agentic",
) -> int:
    """Count ``trading_day``'s buy ATTEMPTS attributable to the recognized
    compatible executor (Perplexity's standing backup task -- see
    ``claude/PERPLEXITY-CLAUDE-COORDINATION.md``), for a caller to fold into
    a SEPARATE engine's own local ``max_orders_per_day`` seeding.

    Built for the one live path whose own order-count seeding is NOT already
    account-wide: near_close_dca.py's 3:30 PM canonical stream seeds
    ``day_order_count`` purely from this repo's own local run artifacts (see
    ``_seed_prior_allocations``), with no visibility into orders a second,
    cooperating executor places directly against the same broker account.
    The other six live-mirror prompts already route through ``plan_orders()``
    fed by freshly fetched broker history, which -- per ``budget_snapshot()``'s
    own "count every order regardless of ``placed_agent``" policy -- is
    already account-wide provided that fetch is complete and fresh; this
    function is additive to a caller's own local seeding, never a
    replacement for it, and must never be used to seed a count for THIS
    engine's own attempts (those are already counted locally).

    An order counts when ALL of:

    * ``order.is_buy_attempt`` -- mirrors ``budget_snapshot()``'s own
      "REJECTED and CANCELLED attempts still consume one" contract.
      Over-counting is the safe direction for a count ceiling.
    * its ``created_at`` falls on ``trading_day`` in America/New_York, the
      same ``_require_aware(...).astimezone(ET).date() == trading_day``
      convention ``budget_snapshot()`` already uses.
    * ``order.placed_agent`` equals ``compatible_agent_value`` EXACTLY
      (fail-safe: ``None``, ``"user"``, ``"recurring"``, ``"drip"``, or any
      other value is never compatible -- mirrors Gate 4's own
      classification).
    * its ``client_order_id`` is present AND does NOT start with
      ``self_client_ref_prefix``. ``self_client_ref_prefix`` is required, not
      optional, and given no default, and must be a nonempty string (an
      empty prefix would make ``str.startswith("")`` True for every order,
      silently zeroing this function's entire count): a bare
      ``placed_agent == "agentic"`` is not sufficient to attribute an order
      to the compatible executor specifically, because THIS engine's own
      live orders carry the identical generic tag
      (``claude/PERPLEXITY-CLAUDE-COORDINATION.md`` Sec 4: "A bare
      placed_agent of 'agentic' alone is NOT sufficient"). Without excluding
      this engine's own client-reference prefix
      (``dca-runner-<slot>-<trading_day>-<TICKER>``, per that same doc),
      this function would double-count THIS engine's own attempts --
      already reflected in its caller's local seeding -- as the compatible
      executor's. A MISSING ``client_order_id`` fails closed (does not
      count) rather than being treated as "not self": the 1530ET
      live-mirror prompt's own Gate 7 documents that a refreshed broker
      fetch can return ``client_order_id`` as null even for an order THIS
      engine itself placed, so a missing reference cannot prove the order
      is NOT self and must never be counted as the compatible executor's on
      that gap alone (PR review: "fail closed when the identifier is
      absent instead").
    """
    if not isinstance(self_client_ref_prefix, str) or not self_client_ref_prefix:
        raise ValueError(
            "count_compatible_executor_attempts requires a nonempty "
            f"self_client_ref_prefix to exclude this engine's own orders; "
            f"got {self_client_ref_prefix!r}"
        )
    count = 0
    for order in orders:
        if not order.is_buy_attempt:
            continue
        if order.placed_agent != compatible_agent_value:
            continue
        client_ref = order.client_order_id
        if not client_ref:
            continue
        if client_ref.startswith(self_client_ref_prefix):
            continue
        if _require_aware(order.created_at).astimezone(ET).date() != trading_day:
            continue
        count += 1
    return count


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
    # Optional producer-supplied calibration evidence.  When present as a
    # complete set, planner consistency is checked against this ticker's
    # selected base and tier bounds rather than the global fallback ladder.
    sizing_base_usd: float | None = None
    sizing_min_drop_pct: float | None = None
    sizing_max_drop_pct: float | None = None


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
    documented 7-day risk-guard kill-switch (``docs/risk_management.md``
    #2 -- applied inside ``near_close_dca.graded_buy_multiplier`` via
    ``dca_risk.apply_risk_guard``, not in this module) caps a deep-drop
    signal's multiplier down to that floor while the candidate still
    carries its REAL, uncapped ``drop_pct`` -- callers are explicitly
    required to perform this capping before candidates ever reach
    ``assess()``/``plan_orders()`` (a stale prior version of this reference
    pointed at ``PRODUCTION_READINESS.md`` #3.6, a section that never
    existed there -- the actual worked mechanics lived in the now-removed
    ``GATE5-PROPOSAL.md`` and are restored here from ``docs/risk_management.md``'s
    own, currently-accurate section instead (Codex review: "Restore the
    detailed kill-switch reference")). Without this exemption, a
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


def _calibration_fields_well_formed(cand: Candidate) -> bool:
    """True when ``cand``'s three calibration fields are individually
    well-formed: present as a complete set, numeric (not bool), finite, and
    internally ordered (``0 <= sizing_min_drop_pct < sizing_max_drop_pct``
    when a max is given). Does NOT check ``cand.multiplier`` or bind the
    evidence to any ticker's configured curve -- see
    :func:`_configured_calibration_bounds` for that. Shared by
    :func:`_candidate_multiplier_notional_mismatch` (refusal) and
    :func:`_candidate_calibrated_multiplier_is_consistent` (tier-counting
    trust) so the two never drift on what counts as well-formed evidence.
    """
    if cand.sizing_base_usd is None or cand.sizing_min_drop_pct is None:
        return False
    calibration = (cand.sizing_base_usd, cand.sizing_min_drop_pct, cand.sizing_max_drop_pct)
    numeric = [value for value in calibration if value is not None]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
        return False
    try:
        # A persisted JSON integer can be arbitrarily large, and
        # math.isfinite(10**1000) raises OverflowError rather than
        # returning False (Codex review: "Convert oversized calibration
        # integers into a refusal").
        if any(not math.isfinite(value) for value in numeric):
            return False
    except OverflowError:
        return False
    if cand.sizing_base_usd <= 0 or cand.sizing_min_drop_pct < 0:
        return False
    return not (cand.sizing_max_drop_pct is not None and cand.sizing_max_drop_pct <= cand.sizing_min_drop_pct)


def _configured_calibration_bounds(
    ticker: object, multiplier: float
) -> tuple[set[float], float, float | None, float] | None:
    """``(configured_bases, configured_min_drop_pct, configured_max_drop_pct,
    minimum_order_usd)`` for ``ticker``'s active per-ticker curve at
    ``multiplier``, or ``None`` if the ticker has no active calibrated curve
    (unconfigured, listed in ``fallback_tickers``, or the config can't be
    read/parsed) or ``multiplier`` doesn't match one of its configured
    tiers. Candidate rows are external evidence, not configuration -- both
    callers below bind a candidate's claimed sizing to THIS ticker's real,
    active curve rather than trusting any internally-consistent-looking
    value.
    """
    try:
        sizing = _read_config("per_ticker_sizing.json")
        normalized = _normalize_ticker(ticker)
        if normalized in sizing.get("fallback_tickers", ()):
            return None
        ticker_sizing = sizing["tickers"][normalized]
        configured_bases = {
            float(ticker_sizing["base_amount_usd"]),
            float(ticker_sizing["base_amount_below_close_usd"]),
        }
        tiers = ticker_sizing["tiers"]
        configured_tier = next(tier for tier in tiers if float(tier["multiplier"]) == multiplier)
        tier_index = tiers.index(configured_tier)
        configured_min = float(configured_tier["min_drop_pct"])
        configured_max = (
            float(tiers[tier_index + 1]["min_drop_pct"])
            if tier_index + 1 < len(tiers)
            else None
        )
        minimum_order_usd = float(sizing["minimum_order_usd"])
    except (KeyError, TypeError, ValueError, StopIteration, OSError, json.JSONDecodeError):
        return None
    return configured_bases, configured_min, configured_max, minimum_order_usd


def _candidate_calibrated_multiplier_is_consistent(cand: Candidate) -> bool:
    """True if ``cand``'s calibration evidence, when complete, makes
    ``cand.multiplier`` trustworthy for tier counting and allocation
    priority.

    Mirrors :func:`_candidate_multiplier_notional_mismatch`'s calibrated
    structural checks (complete well-formed fields, bound to the ticker's
    active configured tier, notional at or below the full ceiling) but does
    NOT require ``drop_pct`` consistency, matching
    :func:`_candidate_multiplier_is_consistent`'s own notional-only trust
    condition for the global ladder -- callers here (``_estimated_multiplier``)
    need only a trustworthy MULTIPLIER, not full candidate validity (Codex
    review: "Preserve calibrated multipliers for reduced top-ups").
    """
    if (
        cand.multiplier is None
        or isinstance(cand.multiplier, bool)
        or not math.isfinite(cand.multiplier)
        or cand.multiplier <= 0
    ):
        return False
    if not _calibration_fields_well_formed(cand):
        return False
    if not math.isfinite(cand.amount_usd) or cand.amount_usd <= 0:
        return False
    bounds = _configured_calibration_bounds(cand.ticker, cand.multiplier)
    if bounds is None:
        return False
    configured_bases, configured_min, configured_max, _minimum_order_usd = bounds
    if cand.sizing_base_usd is None or not any(math.isclose(cand.sizing_base_usd, b, abs_tol=1e-4) for b in configured_bases):
        return False
    try:
        if not math.isclose(cand.sizing_min_drop_pct, configured_min, abs_tol=1e-4):
            return False
        if cand.sizing_max_drop_pct is None:
            if configured_max is not None:
                return False
        else:
            if configured_max is None or not math.isclose(cand.sizing_max_drop_pct, configured_max, abs_tol=1e-4):
                return False
    except OverflowError:
        return False
    return _cents(cand.amount_usd) <= _cents(cand.sizing_base_usd * cand.multiplier)


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
    try:
        # A persisted JSON integer can be arbitrarily large, and
        # math.isfinite(10**1000) raises OverflowError rather than
        # returning False -- an astronomically large amount_usd must fail
        # closed the same way this function already treats an oversized
        # calibration value, not escape uncaught and abort the whole
        # plan_orders() call.
        if not math.isfinite(cand.amount_usd):
            return False
    except OverflowError:
        return True
    calibration = (
        cand.sizing_base_usd,
        cand.sizing_min_drop_pct,
        cand.sizing_max_drop_pct,
    )
    if any(value is not None for value in calibration):
        # Partial or malformed calibration must not disable the global
        # consistency guard.  A producer using a calibrated curve supplies
        # the complete selected tier as evidence.
        if not _calibration_fields_well_formed(cand):
            return True
        if (
            cand.multiplier is None
            or isinstance(cand.multiplier, bool)
            or not math.isfinite(cand.multiplier)
            or cand.multiplier <= 0
        ):
            return True
        # Candidate rows are external evidence, not configuration. Bind the
        # claimed base and interval to this ticker's ACTIVE configured tier
        # -- a ticker listed in fallback_tickers has a stale/inactive
        # tickers[] entry and is sized off the global ladder instead
        # (config/per_ticker_sizing.json's own documented contract; see the
        # midday live-mirror prompt's identical fallback_tickers exclusion),
        # so calibration evidence for one is never authoritative here (Codex
        # review: "Verify calibration evidence against the configured
        # curve").
        bounds = _configured_calibration_bounds(cand.ticker, cand.multiplier)
        if bounds is None:
            return True
        configured_bases, configured_min, configured_max, minimum_order_usd = bounds
        if cand.sizing_base_usd is None or not any(math.isclose(cand.sizing_base_usd, b, abs_tol=1e-4) for b in configured_bases):
            return True
        try:
            if not math.isclose(cand.sizing_min_drop_pct, configured_min, abs_tol=1e-4):
                return True
            if cand.sizing_max_drop_pct is None:
                if configured_max is not None:
                    return True
            else:
                if configured_max is None or not math.isclose(cand.sizing_max_drop_pct, configured_max, abs_tol=1e-4):
                    return True
        except OverflowError:
            return True
        # amount_usd may be strictly less than sizing_base_usd * multiplier:
        # a same-day top-up (scripts/same_day_top_up_gate.py, Gate 6.5 in the
        # midday live-mirror prompt) can shrink buy_usd below the full
        # calibrated allocation while the candidate still carries its
        # original sizing evidence. Treat that product as a CEILING, not an
        # exact target, so a genuinely reduced, planner-approved top-up is
        # not refused as inconsistent_multiplier_notional; amount_usd above
        # the ceiling (an inflated/corrupted notional) is still refused
        # (Codex review: "Allow planner-approved reduced top-ups to reach
        # placement").
        if cand.amount_usd <= 0 or _cents(cand.amount_usd) > _cents(cand.sizing_base_usd * cand.multiplier):
            return True
        # A same-day top-up can shrink to a residual below the broker's own
        # configured minimum order size (config/per_ticker_sizing.json's
        # minimum_order_usd) -- docs/dca_operational_todos.md records
        # Robinhood actually rejecting such an order. The live-mirror flow
        # forwards every buy_usd > 0.00 to placement, so this must be
        # enforced here rather than trusted to a downstream broker error
        # (Codex review: "Reject calibrated top-ups below the broker
        # minimum").
        if _cents(cand.amount_usd) < _cents(minimum_order_usd):
            return True
        if cand.drop_pct is None or not math.isfinite(cand.drop_pct):
            return False
        if cand.sizing_min_drop_pct is not None and cand.drop_pct < cand.sizing_min_drop_pct:
            return True
        if cand.multiplier == MIN_LADDER_MULTIPLIER:
            # Mirrors _candidate_drop_mismatches_claimed_multiplier's own
            # exemption for the global ladder: the risk-guard kill-switch
            # (dca_risk.apply_risk_guard) can cap a deep-drop calibrated
            # signal down to the floor multiplier while the candidate keeps
            # its real, deep drop_pct. Enforcing the floor tier's own finite
            # upper bound here would refuse a valid capped candidate, e.g.
            # 1x at a 5% drop on a curve whose 1x tier only spans up to 2%
            # (Codex review: "Keep kill-switch-capped calibrated candidates
            # eligible").
            return False
        return cand.sizing_max_drop_pct is not None and cand.drop_pct >= cand.sizing_max_drop_pct
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
    """Parse raw ``(ticker, amount_usd[, multiplier[, drop_pct[, sizing_base_usd, sizing_min_drop_pct, sizing_max_drop_pct]]])`` rows into
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
            # Calibration evidence (sizing_base_usd/sizing_min_drop_pct/
            # sizing_max_drop_pct) is carried through here too: a prebuilt
            # Candidate is the only row shape that can carry it (the tuple
            # shapes below have no slots for it), and dropping it during
            # reparsing would silently fall back to the global ladder for
            # every caller that routes through assess()/execute_plan()
            # instead of calling plan_orders() directly -- rejecting a
            # calibrated candidate's real evidence as
            # inconsistent_multiplier_notional even though the same
            # candidate is accepted when plan_orders() is called with it
            # unparsed (Codex review: "Preserve calibration fields while
            # parsing Candidates").
            ticker, amount, multiplier, drop_pct = (
                row.ticker, row.amount_usd, row.multiplier, row.drop_pct
            )
            sizing_base_usd = row.sizing_base_usd
            sizing_min_drop_pct = row.sizing_min_drop_pct
            sizing_max_drop_pct = row.sizing_max_drop_pct
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
        elif len(row) == 7:
            # (ticker, amount, multiplier, drop_pct, sizing_base_usd, sizing_min_drop_pct,
            # sizing_max_drop_pct): the JSON-serializable form of a calibrated Candidate, as
            # published in planner_candidates. Without these slots a JSON row could never
            # carry calibration evidence, so every calibrated row was validated against the
            # global ladder alone (Copilot review, PR #1597). The values are still bound to
            # the ticker's ACTIVE configured tier in _candidate_multiplier_notional_mismatch.
            ticker, amount, multiplier, drop_pct, sizing_base_usd, sizing_min_drop_pct, sizing_max_drop_pct = row
        elif len(row) == 4:
            ticker, amount, multiplier, drop_pct = row
            sizing_base_usd = sizing_min_drop_pct = sizing_max_drop_pct = None
        elif len(row) == 3:
            ticker, amount, multiplier = row
            drop_pct = None
            sizing_base_usd = sizing_min_drop_pct = sizing_max_drop_pct = None
        else:
            ticker, amount = row
            multiplier = None
            drop_pct = None
            sizing_base_usd = sizing_min_drop_pct = sizing_max_drop_pct = None
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
                sizing_base_usd=(
                    None if sizing_base_usd is None
                    else _json_number(sizing_base_usd, "candidate sizing_base_usd")
                ),
                sizing_min_drop_pct=(
                    None if sizing_min_drop_pct is None
                    else _json_number(sizing_min_drop_pct, "candidate sizing_min_drop_pct")
                ),
                sizing_max_drop_pct=(
                    None if sizing_max_drop_pct is None
                    else _json_number(sizing_max_drop_pct, "candidate sizing_max_drop_pct")
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

    A calibrated candidate (complete, config-bound ``sizing_*`` evidence) is
    trusted the same way, via :func:`_candidate_calibrated_multiplier_is_consistent`:
    without this, a same-day top-up that Gate 6.5 legitimately shrinks below
    the full calibrated allocation (e.g. a TRIN 5x buy reduced from $5 to
    $3) fails the global-ladder-only check above -- $3 matches neither a
    $1 nor $2 base product at 5x -- and falls through to the plain-division
    estimate (1.5x), undercounting high-multiplier signals in tier selection
    and allocation priority (Codex review: "Preserve calibrated multipliers
    for reduced top-ups").
    """
    if cand.multiplier is not None and (
        _candidate_multiplier_is_consistent(cand)
        or _candidate_calibrated_multiplier_is_consistent(cand)
    ):
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

        # isinstance(..., bool) excluded deliberately: bool is a numeric
        # subtype in Python, so a JSON `true` multiplier equals 1.0 and would
        # otherwise match a real 1.0x tier in _configured_calibration_bounds,
        # letting malformed external evidence read as broker-minimum-skip
        # evidence instead of the REFUSED_INCONSISTENT_MULTIPLIER classification
        # _candidate_multiplier_notional_mismatch() gives it below -- mirrors
        # the same isinstance(cand.multiplier, bool) guard already used by
        # _candidate_calibrated_multiplier_is_consistent() and
        # _candidate_multiplier_notional_mismatch() itself (Codex review:
        # "Reject boolean calibrated multipliers").
        bounds = (
            _configured_calibration_bounds(ticker, cand.multiplier)
            if cand.multiplier is not None and not isinstance(cand.multiplier, bool)
            else None
        )
        if bounds is not None:
            _, _, _, minimum_order_usd = bounds
            if _cents(cand.amount_usd) < _cents(minimum_order_usd):
                # Robinhood rejects a same-day top-up below its own minimum
                # order size (docs/dca_operational_todos.md); refusing it
                # here at planning time keeps that failure classified under
                # its own canonical reason instead of falling through to
                # _candidate_multiplier_notional_mismatch()'s
                # REFUSED_INCONSISTENT_MULTIPLIER below, which audit/alert
                # consumers read as corrupted sizing evidence rather than an
                # expected broker-minimum skip (Codex review: "Preserve the
                # broker-minimum refusal reason").
                plan.refused.append((ticker, REFUSED_BELOW_BROKER_MINIMUM_ORDER_SIZE))
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

    place_fn: Any
    enable_live_orders: bool = False
    dry_run: bool = True
    account_number: str | None = None


def build_runner_broker(
    place_fn: Any,
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


# Sentinel for "place_fn raised", distinct from any value place_fn could
# legitimately return (including None).
_PLACE_FN_RAISED = object()


def _placement_response_may_hold_a_live_order(response: object) -> bool:
    """Whether a FAILED placement response might still leave a live order at
    the broker.

    Decides halt-vs-isolate in ``execute_plan``. ``_placement_failure_reason``
    reports two very different kinds of failure with one string:

    * **Proven no live order** -- no usable order id AND no positive or
      unreadable fill figure, or an order that came back in a terminal state
      *and explicitly reported a zero fill*. Nothing was accepted, so the
      remaining orders' caps and buying-power arithmetic is untouched and the
      batch is safe to continue. This is the reported incident's shape (a
      broker refusing one ticker outright, with no order id at all and nothing
      said about execution). Note the two branches read SILENCE about the fill
      differently on purpose: with an id, silence leaves an accepted order's
      execution unresolved and halts; without one, silence is just an ordinary
      refusal and isolates.
    * **Spend we cannot account for** -- a terminal order carrying a usable
      order id that reports, or simply fails to rule out, a settled partial
      fill. It can never fill again, but any shares it already bought are
      real, and they are in neither ``submitted`` nor the batch's
      arithmetic. Only an EXPLICIT zero fill rules this out; an omitted
      figure does not.
    * **A live order we could not verify** -- the response carried a usable
      order id in a non-terminal state, but its symbol, side, client
      reference, or notional does not match what was requested. The canonical
      case is ``$100`` confirmed for a requested ``$2``: an order exists, for
      an amount none of the pre-flight cap or buying-power checks accounted
      for, and it is not in ``submitted`` either. Continuing would place the
      rest of the batch against arithmetic that is already wrong (Codex
      review, PR #786: "Halt after responses that may represent accepted
      orders").
    * **An order id we cannot track** -- a positive numeric identifier can be
      too large for the normal finite-number validation while still proving
      that the broker assigned an identity. That response is ambiguous, not a
      no-order refusal, and therefore halts without discarding the preceding
      submissions from the result.

    DELIBERATELY CONSERVATIVE AND DELIBERATELY INDEPENDENT of
    ``_placement_failure_reason``'s own traversal: any shape this cannot
    confidently read returns ``True``. Divergence between the two can
    therefore only ever cause an extra halt, never an unsafe continue -- the
    safe direction for a duplicated safety check.

    A raised ``place_fn`` has no response to inspect, so it never reaches this
    classifier at all -- ``execute_plan`` halts on it directly. Nothing here
    could prove anything about a call that returned no envelope.
    """
    if not isinstance(response, dict):
        return True
    data = response.get("data", response)
    if not isinstance(data, dict):
        return True
    order = data.get("order", data)
    if not isinstance(order, dict):
        return True
    # Fill figures are read across EVERY envelope level, not just the deepest
    # mapping this descent selected. Wrappers differ on where they put the
    # executed quantity/notional: it can sit beside `data.order` rather than
    # inside it, or on the outermost envelope next to `success` -- e.g.
    # {"success": false, "executed_dollar_amount": 5, "data": {"state":
    # "cancelled"}}. Scanning only `order` discards that figure and reports
    # real spend as proof that no order exists (Codex review, PR #786:
    # "Inspect fill evidence across every response envelope"). Same
    # combination rule the per-key scan already uses: any positive or
    # unreadable figure anywhere settles it, and a zero only counts as proof
    # when nothing else contradicts it.
    envelopes, envelopes_truncated, _list_item_ids = _response_envelopes(response, data, order)
    # A BRIDGE failure is the response-shaped twin of a raised `place_fn`, and
    # gets the same verdict for the same reason. `{"isError": true, "error":
    # "timeout"}` says the MCP transport failed; it does not say whether it
    # failed before the request reached the broker or while carrying the
    # broker's acceptance back. `execute_plan` already halts unconditionally
    # on a raise on exactly that reasoning (an accepted order the caller never
    # learns about is missing from `submitted` and from every remaining
    # order's cap arithmetic), and an ID-less `isError` envelope left the
    # batch placing on against the same unknown (Codex review, PR #786: "Halt
    # on ID-less MCP bridge errors").
    #
    # This does NOT touch the reported incident's shape. A broker refusing a
    # ticker reports `success: false` with an error string -- verified: the
    # RVII "not tradable for fractional buys" response carries no `isError` at
    # all -- while `isError` is the connector/bridge's own marker for "this
    # call did not complete". Strict `is True`, matching
    # `_placement_failure_reason` and `_is_error_response`: a truthy non-bool
    # is not this canonical signal.
    if any(envelope.get("isError") is True for envelope in envelopes):
        return True
    if envelopes_truncated:
        # The walk could not see the whole response, so "no order anywhere" is
        # not something this call established -- it is something it failed to
        # look for. Unreadable shapes halt.
        return True
    # One envelope naming TWO DIFFERENT orders is a shape this cannot read.
    # Its state and its fill figures are single-valued, so there is no way to
    # say which of the two they describe -- and round 20's attribution rule
    # ("a figure on an envelope that NAMES an order describes that order")
    # gives no answer when the envelope names two. Previously the first id won
    # and the second was never recorded at all, so `{"id": "REQ", "order_id":
    # "ORD", "state": "rejected", "cumulative_quantity": 0}` read as one
    # proven-unfilled order while ORD -- possibly a distinct accepted broker
    # order -- went unaccounted for (Codex review, PR #786: "Inspect both
    # identifier keys in each envelope"). Halting is the honest answer for an
    # unattributable shape, and it is the direction this classifier fails in
    # everywhere else.
    if any(len(_usable_order_ids(envelope)) > 1 for envelope in envelopes):
        return True
    identities = _order_identity_groups(envelopes)
    if not identities:
        # No usable order id. Ordinarily that proves nothing was accepted --
        # it is the reported incident's own shape, a broker refusing a symbol
        # outright -- but only when the response says nothing about execution
        # either. A response that omits the id while REPORTING a fill (e.g.
        # {"success": false, "data": {"state": "cancelled",
        # "executed_dollar_amount": 5}}) is describing real spend, and
        # isolating it drops that spend from the remaining orders' cap and
        # buying-power arithmetic (Codex review, PR #786: "Check fill evidence
        # before isolating missing-ID responses").
        #
        # Deliberately NARROWER than the terminal-state branch below: there,
        # an absent fill figure halts, because an id proves the broker
        # accepted something and silence about the fill leaves that unresolved.
        # Here there is no id, so silence about the fill is the ordinary shape
        # of a refusal and must stay isolatable -- otherwise every plain
        # rejection would halt and the whole change would be undone.
        #
        # An id is not the ONLY way a response can say the broker accepted
        # something, though. A missing-id response that nonetheless reports a
        # working or completed state -- queued, confirmed, partially_filled,
        # filled -- is describing an order that exists, and isolating it left
        # that order and any spend out of `submitted` and out of every
        # remaining order's cap and buying-power arithmetic (Codex review,
        # PR #786: "Honor working states even when an order ID is missing").
        if _accepted_state_reported(envelopes):
            return True
        # ... and an id we cannot USE is not the same thing as no id at all.
        # A positive numeric id too large to represent is the broker naming an
        # order this batch simply cannot track, which is ambiguity rather than
        # proof -- see _untrackable_positive_order_id_reported.
        if _untrackable_positive_order_id_reported(envelopes):
            return True
        # A wrapper's own explicit success flag is acceptance evidence too,
        # and it is the one signal that reaches here having ALREADY been read
        # by `_placement_failure_reason` -- which rejected this response only
        # for want of an order id ("broker response carried no order id"),
        # not because anything said the placement failed. So `{"success":
        # true}` with no id, no state and no fill isolated: the wrapper said
        # the order went through, and the batch continued as though nothing
        # had (Codex review, PR #786: "Halt on success-flagged responses that
        # omit an ID").
        #
        # Confined to the NO-IDENTITY branch on purpose. Once a response names
        # an order, `success: true` is ordinarily describing the API call
        # rather than that order's fate -- a named `rejected` order with an
        # explicit zero can legitimately sit under a successful call -- and
        # halting on it there would halt ordinary refusals.
        if _success_flag_reported(envelopes):
            return True
        # A state that means an order EXISTED AND THEN DIED needs the same
        # explicit-zero proof the with-an-id branch below demands, for exactly
        # the same reason: `cancelled` and `expired` are not refusals. An order
        # has to exist before it can be cancelled, and it has to be working
        # before it can expire, so either one may have partially filled first
        # -- and without an id that spend is even less accountable, not more
        # (Codex review, PR #786: "Require zero fills for missing-ID
        # cancellations").
        #
        # `rejected`/`failed`/`voided` are deliberately NOT in that set even
        # though they share the dead-state vocabulary elsewhere: those say the
        # broker declined and no order came into being, which is the reported
        # incident's own shape. Including them would halt the first ordinary
        # refusal and undo this whole change.
        if _post_acceptance_terminal_state_reported(envelopes):
            return _combined_fill_evidence(envelopes) != _FILL_EVIDENCE_EXPLICIT_ZERO
        return _combined_fill_evidence(envelopes) == _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE
    # The acceptance-state read above is NOT confined to the no-identity case.
    # Naming one order does not license ignoring what the response says about
    # levels that name none: `{"state": "queued", "data": {"id": "ORD",
    # "state": "rejected", "cumulative_quantity": 0}}` isolated, because the
    # per-identity loop below only ever inspects envelopes that carry an id,
    # and the ID-less `queued` was never looked at once `identities` was
    # non-empty (Codex review, PR #786: "Inspect ID-less acceptance states
    # beside named orders"). Both readings of that shape demand a halt, so no
    # disambiguation is needed: either the outer level describes a SECOND,
    # unnamed order that is live and in nobody's arithmetic, or it describes
    # ORD itself in a state contradicting `rejected` -- and the loop below
    # already halts on exactly that disagreement when both states happen to
    # sit on id-bearing levels. Only the accepted states are read here; the
    # post-acceptance terminal ones (`cancelled`/`expired`) are deliberately
    # left to `_terminal_order_may_have_filled`, which already reads
    # unattributed envelopes and knows when a single named order's own
    # explicit zero vouches for them -- re-checking them here would halt every
    # ordinary cancellation whose outer level merely restates the inner state.
    unattributed_envelopes = [
        envelope for envelope in envelopes if not _usable_order_ids(envelope)
    ]
    if _accepted_state_reported(unattributed_envelopes):
        return True
    # ... and the post-acceptance terminal states are read here too, on the
    # narrower CONTRADICTION test. Round 28 excluded them wholesale, on the
    # grounds that an unnamed level restating a named order's own `cancelled`
    # is not new information -- true, and too coarse a rule for the
    # justification it was given: `{"state": "cancelled", "data": {"id":
    # "ORD", "state": "rejected", "cumulative_quantity": 0}}` is not a
    # restatement of anything. No named order reports `cancelled`, so that
    # outer level is describing something else: a second, unnamed order that
    # existed and was killed, and may have partially filled on the way (Codex
    # review, PR #786: "Inspect ID-less terminal states beside named orders").
    # A state some named order DOES report stays a restatement and is left to
    # `_terminal_order_may_have_filled`, which reads the unattributed
    # envelopes itself and knows when one named order's explicit zero vouches
    # for them -- so an ordinary cancellation echoed at two levels still
    # isolates. `rejected`/`failed`/`voided` are not in this set for the same
    # reason as everywhere else: they say no order was created.
    named_states = {
        envelope["state"].strip().lower()
        for _, group in identities
        for envelope in group
        if isinstance(envelope.get("state"), str)
    }
    # Canonicalized on BOTH sides: this is a same-state comparison, and
    # `cancelled`/`canceled` are one state spelled two ways. Comparing the raw
    # strings made an ordinary echo -- a wrapper reporting one spelling over a
    # named, explicitly zero-filled order reporting the other -- look like a
    # level describing an order the response never names, which halts the whole
    # batch (Codex review, PR #786: "Normalize cancellation aliases before
    # comparing echoes").
    #
    # Only the ECHO test folds spellings. Membership in
    # `_POST_ACCEPTANCE_TERMINAL_STATES` already covers both, and the identity
    # grouping still refuses to fold anything about an order's IDENTITY --
    # merging two orders is the unsafe direction there, while failing to
    # recognize an echo here is the unsafe direction.
    _named_canonical = {_canonical_order_state(_state) for _state in named_states}
    for envelope in unattributed_envelopes:
        state = envelope.get("state")
        if not isinstance(state, str):
            continue
        normalized = state.strip().lower()
        if (
            normalized in _POST_ACCEPTANCE_TERMINAL_STATES
            and _canonical_order_state(normalized) not in _named_canonical
        ):
            return True
    # Each state is read from the SAME envelope its id came from, not from the
    # descent's own pick: id and state together identify one order, so reading
    # the state off a different level than the id would describe two.
    #
    # And EVERY named order has to be accounted for, not just the first. A
    # response can name more than one -- a wrapper's own request identity
    # outside, the real broker order inside -- and isolating requires all of
    # them to be provably dead. One live order among several terminal ones is
    # still a live order (Codex review, PR #786: "Inspect all response
    # identities before isolating").
    for _, group in identities:
        # Every state this order reported, from the envelopes that name IT.
        # A level that omits the state contributes nothing rather than
        # condemning the order: when the same id appears twice and only one
        # level carries the state, both levels are describing the one order.
        states = [
            envelope.get("state") for envelope in group if envelope.get("state") is not None
        ]
        if not states:
            # Nothing anywhere says what this order is doing.
            return True
        for state in states:
            if not (isinstance(state, str) and state.strip().lower() in _DEAD_PLACEMENT_STATES):
                # Non-terminal or unreadable -- including two levels of the
                # same order disagreeing, where one says rejected and the
                # other queued. Not provably dead, so the response as a whole
                # is not provably order-free.
                return True
    # Every order the response names is dead (rejected/cancelled/expired) -- none
    # of them can fill FROM HERE, but "terminal" is not the same as "never
    # filled": an order that partially filled and was THEN cancelled has
    # already moved real money and bought real shares. BrokerOrder.
    # counts_toward_budget makes exactly that distinction for history
    # (Codex review: "Account for partially filled cancelled buys"), and this
    # classifier has to make it too, or a cancelled-after-partial-fill
    # response isolates as "nothing was accepted" and the remaining orders are
    # placed against cap and buying-power arithmetic that never saw that spend
    # (Codex review, PR #786: "Reconcile partial fills before isolating
    # terminal responses").
    if _terminal_order_may_have_filled(envelopes, identities):
        return True
    return False


def _isolated_failure_carries_a_trackable_order_id(response: object) -> bool:
    """Whether an otherwise isolatable failure already names a broker order id.

    If a terminal zero-fill response carries a usable order id, that exact row
    can later appear in broker history on its own and should not also gain an
    anonymous synthetic attempt record that nothing can deduplicate away.
    """
    if not isinstance(response, dict):
        return False
    data = response.get("data", response)
    if not isinstance(data, dict):
        return False
    order = data.get("order", data)
    if not isinstance(order, dict):
        return False
    envelopes, envelopes_truncated, _list_item_ids = _response_envelopes(response, data, order)
    if envelopes_truncated:
        return True
    return bool(_order_identity_groups(envelopes))


# Bounds on the envelope walk below. A placement response is a handful of
# small nested objects; these exist so a pathological or hostile payload cannot
# turn the classifier into a long traversal, not as a real structural limit.
_MAX_ENVELOPE_DEPTH = 6
_MAX_ENVELOPES = 64


def _response_envelopes(response: dict, data: dict, order: dict) -> tuple:
    """Every distinct mapping in a placement response, outermost first.

    The descent that produced ``data``/``order`` follows exactly one path --
    ``response`` -> ``data`` -> ``data.order`` -- and a wrapper is not obliged
    to put the order there. ``{"success": false, "order": {"id": "ORD",
    "state": "queued"}, "data": {"error": "timeout"}}`` hangs the accepted
    order off the OUTER envelope as a sibling of ``data``, so the descent
    selected ``data``, never visited ``response["order"]``, found no id, state
    or fill on the levels it did visit, and isolated an order that may be live
    (Codex review, PR #786: "Inspect order mappings outside the selected data
    branch").

    So this walks the whole response breadth-first instead of following one
    branch: the three descent picks first, so the outermost-first ordering the
    callers rely on is unchanged, then every other nested mapping. Lists are
    walked too -- an ``orders: [...]`` array is an obvious way to report more
    than one order.

    Deduplicated by IDENTITY, not equality: the descent falls back to the
    enclosing mapping when ``data``/``order`` are absent, so the same dict
    routinely appears twice, and two genuinely distinct envelopes could still
    compare equal.

    Returns ``(envelopes, truncated, list_item_ids)``. Bounded by
    ``_MAX_ENVELOPE_DEPTH`` and ``_MAX_ENVELOPES``, which a real response never
    approaches -- but reaching either bound is NOT harmless, and an earlier
    version of this docstring claimed it was ("fewer envelopes ... can only
    make the classifier halt more readily"). That is backwards. Fewer
    envelopes means an order-bearing branch may go unvisited, and an unvisited
    order reads as no order at all, which ISOLATES. Seven nested wrappers
    around ``{"id": "ORD", "state": "queued"}`` did exactly that (Codex
    review, PR #786: "Fail closed when response traversal is truncated"). So
    the caller is told when the walk was cut short, and treats a truncated
    walk as unreadable -- the same verdict every other shape this classifier
    cannot fully inspect gets.

    ``list_item_ids`` is the ``id()`` of every envelope reached by descending
    into a LIST-valued field (an ``orders: [...]`` array being the obvious
    case, but any list counts). An item's very presence there is already
    positional evidence that it describes an order -- lists of anything else
    are not a shape a placement response has a reason to carry. An envelope
    reached instead through a plain MAPPING field (``{"metadata": {"state":
    "healthy"}}``) carries no such positional evidence: nothing marks
    ``metadata`` as an order-bearing key the way an array slot does, so a
    caller distinguishing "this is plainly an order entry" from "this is some
    other nested mapping that happens to share a field name" needs to know
    which path found it (Codex review, PR #786: "Ignore non-order metadata
    states when scanning siblings").
    """
    envelopes = []
    truncated = False
    # `id()` of every envelope reached by descending into a list-valued
    # field, as opposed to a plain mapping field -- see this function's own
    # docstring for why that distinction matters to a caller.
    list_item_ids: set = set()
    # Shared across EVERY array-valued field AND every plain mapping-field
    # visit this walk makes, not reset per field. `_MAX_ENVELOPES` is
    # documented above as the walk's global bound, but re-initializing this
    # counter inside the array branch gave each separately-keyed list its
    # OWN fresh budget -- a response holding many array-valued fields (a
    # malformed or hostile connector payload) scanned every one of them in
    # full regardless of how many scalar items that totaled, since nothing
    # this walk tracks ever tripped truncation across fields. Measured:
    # 100,000 keys of 63 scalars each (6.3M items) ran to completion,
    # untruncated, in this synchronous call sitting between one placement
    # and the next (Codex review, PR #786: "Share the traversal budget
    # across response arrays").
    #
    # That fix bounded ARRAYS and left plain MAPPING fields uncharged: the
    # `for value in parent.values()` loop just below visits every field of
    # every mapping with no budget check of its own, so a single mapping
    # carrying millions of DICT-valued fields keeps calling `_add()` (and,
    # once the cap is hit, the O(len(envelopes)) identity scan in its
    # rejection branch) for every one of them before the outer per-DEPTH
    # check ever gets a chance to run. Measured: 2,000,000 dict-valued
    # fields in one mapping took over 3 seconds to walk, synchronously,
    # between one placement call and the next (Codex review, PR #786:
    # "Bound mapping-field traversal as well as arrays"). Renamed from
    # `traversal_items_processed` now that it also counts mapping-field
    # visits, not only array items.
    traversal_items_processed = 0

    def _add(candidate: object) -> bool:
        if not isinstance(candidate, dict) or len(envelopes) >= _MAX_ENVELOPES:
            return False
        if any(candidate is seen for seen in envelopes):
            return False
        envelopes.append(candidate)
        return True

    # The descent's own picks first, so `envelopes[0]` stays the outermost
    # mapping and the ordering every caller reads is untouched.
    frontier = [candidate for candidate in (response, data, order) if _add(candidate)]
    depth = 0
    while frontier:
        if depth >= _MAX_ENVELOPE_DEPTH or len(envelopes) >= _MAX_ENVELOPES:
            # Mappings were left unvisited. Whatever they hold is unknown, so
            # the caller must not read this envelope set as exhaustive.
            truncated = True
            break
        depth += 1
        children = []
        budget_exhausted = False
        for parent in frontier:
            if budget_exhausted:
                break
            for value in parent.values():
                # Charged for EVERY field visited, dict-valued or not --
                # a mapping's scalar fields cost nothing to classify, but
                # the loop still has to look at each one, and a hostile
                # payload can put an unbounded number of them (or an
                # unbounded number of dict-valued ones) in a single
                # mapping. See this function's own budget comment above.
                traversal_items_processed += 1
                if traversal_items_processed >= _MAX_ENVELOPES:
                    truncated = True
                    budget_exhausted = True
                    break
                if isinstance(value, dict):
                    if _add(value):
                        children.append(value)
                    elif not any(value is seen for seen in envelopes):
                        # Rejected for the cap, not because it was already
                        # seen: a real mapping we will never look at.
                        truncated = True
                elif isinstance(value, (list, tuple)):
                    # Flattened, not just iterated one level. A `continue` on
                    # any non-dict item silently skipped a NESTED sequence, so
                    # `{"success": false, "results": [[{"id": "ORD", "state":
                    # "queued"}]]}` was walked past without visiting ORD and
                    # without marking the walk truncated -- the classifier then
                    # saw no identity and no acceptance evidence anywhere and
                    # isolated an order that may be live (Codex review,
                    # PR #786: "Traverse nested response arrays before
                    # isolating"). Every other shape this walk cannot fully
                    # read reports truncation; a nested array reported nothing
                    # at all, which is the worse of the two failures.
                    #
                    # Bounded by the same node cap as everything else: the
                    # queue cannot exceed _MAX_ENVELOPES pending items, and a
                    # sequence dropped for that cap sets `truncated` exactly
                    # as a dropped mapping does.
                    # Bounded on THREE axes, because the queue length alone
                    # bounds none of the ways this can run away: a chain of
                    # singleton arrays keeps `len(pending) == 1` forever while
                    # descending without limit, and a self-referential list
                    # (`loop = []; loop.append(loop)`) re-appends itself and
                    # never terminates at all. `place_fn` is injected Python
                    # and may return either, so an unbounded walk here hangs
                    # the whole batch -- before the placement breaker, before
                    # the window check, before anything (Codex review,
                    # PR #786: "Bound nested response-array traversal").
                    #
                    # Verified: the self-referential case looped until killed.
                    # This is the one defect in this file that costs more than
                    # a misclassification.
                    pending = [(value, 0)]
                    seen_sequences = {id(value)}
                    while pending:
                        if traversal_items_processed >= _MAX_ENVELOPES:
                            truncated = True
                            break
                        sequence, sequence_depth = pending.pop(0)
                        if sequence_depth >= _MAX_ENVELOPE_DEPTH:
                            # As deep as the mapping walk is allowed to go.
                            truncated = True
                            continue
                        for item in sequence:
                            traversal_items_processed += 1
                            if traversal_items_processed >= _MAX_ENVELOPES:
                                truncated = True
                                break
                            if isinstance(item, (list, tuple)):
                                # Identity, not equality: a cycle is the case
                                # this exists for, and two distinct sequences
                                # can compare equal.
                                if id(item) in seen_sequences:
                                    continue
                                seen_sequences.add(id(item))
                                pending.append((item, sequence_depth + 1))
                                continue
                            if not isinstance(item, dict):
                                continue
                            if _add(item):
                                children.append(item)
                                list_item_ids.add(id(item))
                            elif not any(item is seen for seen in envelopes):
                                truncated = True
        frontier = children
    return envelopes, truncated, list_item_ids


def _numeric_value_is_unusable(value: Any) -> bool:
    """Whether a numeric id/figure is NaN, infinite, or unrepresentable as a float.

    ``math.isfinite()`` converts its argument to a float first, so a
    syntactically valid but astronomically large JSON integer (``10**1000``)
    makes it raise ``OverflowError`` rather than return ``False`` -- the exact
    trap ``_candidate_is_sortable`` and ``budget_snapshot()`` already document
    and catch for candidate amounts and fill figures. The order-id validators
    below did not, and unlike those two the raise escapes somewhere fatal:
    ``_placement_failure_reason`` runs INSIDE ``execute_plan``'s placement
    ``try`` and its OverflowError is converted into an ordinary failure, but
    ``_placement_response_may_hold_a_live_order`` is consulted OUTSIDE that
    ``try``, so the same value there aborts ``execute_plan`` and destroys its
    ``ExecutionResult`` -- taking the record of every order already submitted
    earlier in the batch with it (Codex review, PR #786: "Treat oversized
    numeric order IDs as unusable"). Unrepresentable is unusable: no broker
    assigns an identifier a float cannot hold.

    Shared by both validators deliberately, unlike the traversals around them
    (see ``_placement_response_may_hold_a_live_order``'s docstring): what round
    32 showed is that restating a primitive numeric rule in two places is how
    the two verdicts drift apart, and this predicate answers the same question
    for both.
    """
    try:
        return not math.isfinite(value)
    except OverflowError:
        return True


def _is_usable_order_id(order_id: object) -> bool:
    """Whether one candidate value is a usable broker order identifier.

    The SAME validity rules ``_placement_failure_reason`` applies when it
    decides a response carried no usable id -- deliberately restated here
    rather than shared, because the two traversals are independent by design
    (see ``_placement_response_may_hold_a_live_order``'s docstring); what must
    not differ is the VERDICT, and
    ``test_both_order_id_validators_agree_on_every_shape`` enforces that.

    They did differ. This side rejected only falsy, boolean and non-scalar
    values, so a whitespace-only, negative, or non-finite id read as a NAMED
    order here while the failure reader called it no id at all. That is not a
    harmless divergence: naming an order routes the response past the
    no-identity branch, which is where the acceptance-state and success-flag
    checks live, so ``{"success": true, "id": "   ", "state": "rejected",
    "cumulative_quantity": 0}`` isolated -- an explicit success flag on a
    response with no real identifier, continuing the batch against an order
    nothing accounts for (Codex review, PR #786: "Treat invalid IDs as missing
    before reading success flags"). The standing claim that divergence between
    the two "can only ever cause an extra halt" assumed they disagreed about
    whether a response FAILED; here they disagreed about whether it named an
    order, and that runs the other way.
    """
    if not order_id or isinstance(order_id, bool):
        # Falsy (None, "", 0) or a bool -- True is an int in Python and would
        # otherwise read as a perfectly good id.
        return False
    if not isinstance(order_id, (str, int, float)):
        # A container ({"value": "ORD"}, ["ORD"]) is not an identifier.
        return False
    if isinstance(order_id, str) and not order_id.strip():
        # Whitespace-only is truthy and plainly not an id.
        return False
    if isinstance(order_id, (int, float)) and _numeric_value_is_unusable(order_id):
        # NaN, infinity, and an integer too large to convert to a float -- the
        # last of which made math.isfinite() RAISE here rather than answer.
        return False
    if isinstance(order_id, (int, float)) and order_id <= 0:
        # A wrapper's -1 sentinel for "no id assigned" is as unusable as NaN.
        return False
    return True


def _usable_order_id(candidate: object) -> object:
    """A usable order id from one mapping, or ``None``.

    Booleans are rejected before the numeric check: ``True`` is an ``int`` in
    Python and would otherwise read as a perfectly good order id.
    """
    if not isinstance(candidate, dict):
        return None
    # Each key is validated on its own. `a or b` picks `a` whenever it is
    # TRUTHY, so a truthy-but-invalid id -- {"id": true, "order_id": "ORD"} --
    # short-circuited the fallback, then failed validation and reported no id
    # at all, isolating an order that may be live (Codex review, PR #786:
    # "Check order_id after rejecting an invalid id"). Note the falsy invalid
    # values (0, {}, []) never had this problem: `or` falls through them. It
    # is specifically `True` -- the one value the bool check below exists to
    # reject -- that is both truthy and invalid.
    for key in ("id", "order_id"):
        order_id = candidate.get(key)
        if not _is_usable_order_id(order_id):
            continue
        return order_id
    return None


def _usable_order_ids(candidate: object) -> list:
    """EVERY usable order id one mapping names, outermost key first.

    ``_usable_order_id`` returns the first valid key and stops, which is right
    for callers that only need to know whether an envelope names *an* order.
    The classifier needs all of them: one envelope can carry two DIFFERENT
    valid identifiers -- ``{"id": "REQ", "order_id": "ORD", "state":
    "rejected", "cumulative_quantity": 0}`` -- and recording only ``REQ`` let
    that response read as one proven-unfilled order while ``ORD``, potentially
    a distinct accepted broker order, was never accounted for at all (Codex
    review, PR #786: "Inspect both identifier keys in each envelope").

    Round 18 made each key VALIDATE on its own; this makes each key COUNT on
    its own. Distinct values only -- a wrapper echoing one id under both keys
    names one order, and identity here is the same (type, stripped value) pair
    :func:`_order_identity_groups` groups on.
    """
    if not isinstance(candidate, dict):
        return []
    found = []
    seen = set()
    for key in ("id", "order_id"):
        order_id = candidate.get(key)
        if not _is_usable_order_id(order_id):
            continue
        identity = _order_identity_key(order_id)
        if identity in seen:
            continue
        seen.add(identity)
        found.append(order_id)
    return found


def _order_identity_key(order_id: object) -> tuple:
    """Return the exact, type-preserving key used to group broker order ids."""
    return (type(order_id), str(order_id).strip())


# Terminal states that imply the order EXISTED before it died, as against
# being refused outright. An order must exist to be cancelled, and must be
# working to expire; `rejected`/`failed`/`voided` mean the broker never took
# it. Only the first kind can have partially filled, so only the first kind
# needs an explicit zero to rule a fill out when there is no order id.
_POST_ACCEPTANCE_TERMINAL_STATES = frozenset({"cancelled", "canceled", "expired"})

#: The two spellings of ONE state. A broker (or a wrapper between us and it)
#: can report the same cancellation as either, so any comparison that asks
#: whether two levels describe the SAME state has to canonicalize first --
#: otherwise a wrapper saying `cancelled` over an order saying `canceled`
#: reads as a second, unnamed order and halts the batch on what is an
#: ordinary zero-filled cancellation echo (Codex review, PR #786:
#: "Normalize cancellation aliases before comparing echoes").
_STATE_SPELLING_ALIASES = {"canceled": "cancelled"}


def _canonical_order_state(state: str) -> str:
    """``state`` stripped, lowercased, and folded onto one spelling."""
    normalized = state.strip().lower()
    return _STATE_SPELLING_ALIASES.get(normalized, normalized)


# The wire spellings a single logical field arrives under. Robinhood's MCP
# wrappers are not consistent about these, and `_placement_failure_reason`
# has accepted every one of them since round 18 -- it takes the FIRST key
# present, so `{"client_order_uuid": ...}` is that response's client
# reference exactly as `{"ref_id": ...}` would be.
#
# Held as constants because the two readers drifted: round 44 taught
# `_accepted_response_names_unaccounted_orders` to check an off-chain
# same-id envelope against the primary field by field, and spelled the
# client reference `ref_id`/`client_order_id` only. A sibling naming a
# DIFFERENT reference under `client_order_uuid` or `client_id` was compared
# against nothing and accepted -- and so was one spelling it
# `client_order_id` while the primary spelled it `ref_id`, since a
# field-by-field comparison never puts two spellings of one field side by
# side (Codex review, PR #786: "Compare client-reference aliases across
# echoed envelopes"). The same gap existed for `instrument_symbol`, which
# the finding did not name.
#
# This is deliberately NOT the traversal-sharing that
# `_placement_response_may_hold_a_live_order`'s docstring rules out. That
# independence is about how each function WALKS a response -- a judgment
# each makes for itself. Which key a broker writes a symbol under is a fact
# about the wire format, and two copies of a fact is just drift waiting to
# happen, as it was here.
_CLIENT_REFERENCE_KEYS = ("ref_id", "client_order_id", "client_order_uuid", "client_id")

#: The subset of `_CLIENT_REFERENCE_KEYS` that names the ORDER's idempotency
#: request and nothing else, so it can be compared against the request along
#: the validated order's own chain. `client_id` is excluded on purpose: it is
#: the key a transport wrapper puts its OWN request id under, and comparing
#: that against the order's reference would halt every placement from such a
#: wrapper. The sibling check still uses the full family -- there the chain's
#: own values are the reference set, so a wrapper's `client_id` never trips it.
_CHAIN_ORDER_REFERENCE_KEYS = ("ref_id", "client_order_id", "client_order_uuid")

#: The spellings a broker reports EXECUTED dollar spend under. Distinct from
#: `dollar_amount`, which is what was REQUESTED -- so these are compared as a
#: ceiling against the validated notional rather than for equality: a fill
#: below the request is ordinary, one above it is spend no cap approved.
_EXECUTED_NOTIONAL_KEYS = (
    "executed_dollar_amount",
    "executed_notional",
    "filled_notional",
)
_SYMBOL_KEYS = ("symbol", "instrument_symbol")


def _cap_approved_amounts(envelopes: list, expected_amount_usd: object) -> list:
    """Every notional this run's caps actually approved, as read from
    ``envelopes``, falling back to the REQUEST when they name none.

    The request is a legitimate reference rather than a last resort:
    ``_placement_failure_reason`` has already proven the validated order
    matches it on every field the response supplies, so it is what those
    envelopes would have said had they said anything (round 49).
    """
    amounts = [
        amount
        for amount in (_coerced_float(e.get("dollar_amount")) for e in envelopes)
        if amount is not None
    ]
    if not amounts and expected_amount_usd is not None:
        requested = _coerced_float(expected_amount_usd)
        if requested is not None:
            amounts = [requested]
    return amounts


def _reports_executed_notional_above(envelopes: list, approved_amounts: list) -> bool:
    """Whether any of ``envelopes`` reports EXECUTED spend exceeding the
    largest amount the caps approved.

    A CEILING, never an equality test, and that is the whole difference
    between this and the ``dollar_amount`` comparison: an executed figure
    BELOW the request is an ordinary partial or complete fill and must not
    halt; one ABOVE it is spend no cap approved, whatever id the envelope
    carries. Tolerance matches the requested-notional comparison.

    One function for both scopes on purpose. The first version of this rule
    lived inline and asked the question only of OFF-CHAIN siblings, which
    left the validated order's own envelopes unexamined -- and a chain
    reporting ``executed_notional: 100`` for a requested $2 is the same
    unaccounted spend whether the figure sits beside the order or on it
    (Codex review, PR #786: "Validate execution figures on the primary
    envelope").
    """
    if not approved_amounts:
        return False
    largest = max(approved_amounts)
    for envelope in envelopes:
        # `fill_field`, not `field`: `dataclasses.field` is imported at module
        # scope and a loop variable named `field` shadows it (ruff F402).
        for fill_field in _EXECUTED_NOTIONAL_KEYS:
            if fill_field not in envelope:
                # Nothing supplied. Silence about the fill is governed by the
                # id branches elsewhere, not here.
                continue
            executed = _coerced_float(envelope.get(fill_field))
            # A SUPPLIED but unreadable figure is not an omitted one. `True`,
            # `"unknown"`, `NaN`, `inf`, a container -- `_coerced_float` returns
            # `None` for all of them, and skipping on `None` meant the broker
            # explicitly reported execution evidence this batch cannot read and
            # the response still recorded a clean submission (Codex review,
            # PR #786: "Reject unreadable execution notionals").
            #
            # A NEGATIVE figure lands here for the same reason it does in
            # `near_close_dca._reconciled_order_may_have_executed`: it is
            # malformed evidence rather than a measurement of "nothing was
            # spent", and reading it as under-the-cap lets a wrapper suppress
            # this whole check by reporting `-1`.
            #
            # Exactly the rule the chain's own `dollar_amount` and `state` checks
            # already apply -- a supplied but unreadable value cannot be
            # treated like an absent one -- reaching the executed spellings,
            # which predate it.
            if executed is None or executed < 0:
                return True
            if executed - largest > 0.001:
                return True
    return False


def _first_present_key(mapping: dict, keys: tuple) -> tuple:
    """The value under the first of ``keys`` PRESENT in ``mapping``.

    Returns ``(value, present)``. Key presence, not truthiness, and a present
    ``None`` stops the search exactly as a real value would -- a wrapper that
    explicitly returned null for a field did report that field, and cascading
    past it to an unrelated fallback is the bypass round 18 closed.
    """
    for key in keys:
        if key in mapping:
            return mapping[key], True
    return None, False


def _aliased_values(envelope: dict, keys: tuple, *, case_folded: bool = True) -> set:
    """Every non-null value ``envelope`` carries for one logical field.

    Unlike :func:`_first_present_key` this pools ALL spellings rather than
    resolving one, because the comparison it feeds asks "does this sibling
    name something the primary did not" -- and a sibling is naming a
    different order whichever spelling carries the differing value.

    ``case_folded`` is per-field rather than global. A ticker or a side is the
    same ticker or side whichever case a wrapper echoes it in, so folding
    those absorbs noise. A CLIENT REFERENCE is not: it is an idempotency key,
    ``_placement_failure_reason`` compares it with an exact ``str(...) !=``,
    and this module's own generated reference embeds the UPPERCASE ticker
    (``dca-runner-<slot>-<date>-AAPL``). Folding it made
    ``...-AAPL`` and ``...-aapl`` compare equal, so a sibling naming a
    different request's key passed as an echo -- the comparison quietly
    weaker than the one that validated the order in the first place (Codex
    review, PR #786: "Preserve client-reference case when comparing
    aliases").
    """
    found = set()
    for key in keys:
        value = envelope.get(key)
        if value is not None:
            text = str(value).strip()
            found.add(text.lower() if case_folded else text)
    return found


def _post_acceptance_terminal_state_reported(envelopes: list) -> bool:
    """True if any envelope reports a state meaning an order existed and died.

    Read only where the response carries NO usable order id -- with one, the
    terminal branch already requires an explicit zero for every named order.
    """
    for envelope in envelopes:
        state = envelope.get("state")
        if isinstance(state, str) and state.strip().lower() in _POST_ACCEPTANCE_TERMINAL_STATES:
            return True
    return False


def _coerced_float(value: Any) -> "float | None":
    """``float(value)`` or ``None`` -- never raises, including on an unbounded
    integer (see ``_numeric_value_is_unusable``) or a bool, which is not a
    numeric field value here however truthy Python considers it.

    Annotated with the union rather than ``Optional`` deliberately: importing
    that name puts ``Optional`` on this module's surface, and
    ``test_runner_exposes_no_sell_or_option_path`` asserts structurally that no
    exported name contains "option" -- the standing no-options-trading
    prohibition. The guard is right; the annotation is what gives way.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _envelope_names_an_order(envelope: dict) -> bool:
    """Whether ``envelope`` carries ANY evidence -- an id, a symbol, a side,
    an order-reference alias, a requested notional, or a fill figure -- that
    it is describing an order at all, as opposed to some unrelated nested
    mapping a response happens to carry (a transport wrapper's own metadata,
    a service-health block, ...).

    Used only to decide whether a mapping-field-sourced envelope's `state`
    key is meaningful: an item reached by descending into a LIST field is
    already positional evidence of an order by
    ``_response_envelopes``'s own reasoning (an `orders: [...]` array is an
    obvious way to report more than one), but a plain mapping field carries
    no such convention -- ``{"metadata": {"state": "healthy"}}`` names no
    order at all (Codex review, PR #786: "Ignore non-order metadata states
    when scanning siblings"). Every key checked here is one this module's
    OWN sibling-scanning loop otherwise separately reads (symbol, side,
    reference, `dollar_amount`, id, or a fill figure) -- this is not a new
    vocabulary, just an existence check over the same one.
    """
    if any(key in envelope for key in _SYMBOL_KEYS):
        return True
    if "side" in envelope or "dollar_amount" in envelope:
        return True
    if any(key in envelope for key in _CHAIN_ORDER_REFERENCE_KEYS):
        return True
    if "id" in envelope or "order_id" in envelope:
        return True
    return any(key in envelope for key in _PARTIAL_FILL_EVIDENCE_KEYS)


def _accepted_response_names_unaccounted_orders(
    response: object,
    *,
    expected_symbol: str | None = None,
    expected_side: str | None = None,
    expected_amount_usd: float | None = None,
    expected_ref_id: str | None = None,
) -> bool:
    """Whether a response the caps ACCEPTED still names orders nobody counted.

    ``_placement_failure_reason`` validates exactly one order -- the one the
    ``response`` -> ``data`` -> ``data.order`` descent selects -- and returning
    ``None`` from it means that one order matches the request. It says nothing
    about the rest of the payload. So
    ``{"data": {"id": "A", "state": "queued", "symbol": "AAPL", "side": "buy",
    "dollar_amount": "2.00", "ref_id": "REF"}, "orders": [{"id": "B", "state":
    "queued"}]}`` was recorded as ONE successful submission and the batch
    continued, while ``B`` sat live at the broker, absent from this run's cap
    and buying-power arithmetic and from ``ExecutionResult.submitted`` (Codex
    review, PR #786: "Inspect sibling orders before accepting a successful
    response").

    Every other identity rule in this module was built for the FAILURE path --
    ``_placement_response_may_hold_a_live_order`` is consulted only once a
    failure reason exists. This is the same question asked on the success path,
    and it has to be asked there too: "one order validated" and "only one order
    exists" are different claims, and the batch was treating the first as the
    second.

    Deliberately narrower than the failure-path classifier. A success means the
    caps really do account for one order, so this halts only on evidence of
    SOMETHING ELSE:

    * a second distinct order named anywhere in the payload,
    * one envelope naming two different orders (unattributable, exactly as on
      the failure path),
    * a bridge ``isError``, whose transport failure is not resolved by the
      primary order having validated,
    * a walk that could not finish, since an unread branch may name the extra
      order this function exists to find.

    Repeated identities are accepted as echoes only after their off-chain
    envelopes agree with the validated order.  A same-ID sibling that changes
    the symbol, side, client reference, requested notional, terminal state, or
    reports execution above the approved amount is contradictory evidence,
    not a harmless duplicate.  The ordinary successful shape -- one order,
    including consistent echoes of that order -- stays cheap and does not
    halt.
    """
    if response is _PLACE_FN_RAISED or not isinstance(response, dict):
        # A raise is handled by the caller's own branch; a non-dict cannot be
        # the shape _placement_failure_reason just validated.
        return False
    data = response.get("data", response)
    if not isinstance(data, dict):
        data = response
    order = data.get("order", data)
    if not isinstance(order, dict):
        order = data
    envelopes, envelopes_truncated, list_item_ids = _response_envelopes(response, data, order)
    if envelopes_truncated:
        return True
    if any(envelope.get("isError") is True for envelope in envelopes):
        return True
    if any(len(_usable_order_ids(envelope)) > 1 for envelope in envelopes):
        return True
    identities = _order_identity_groups(envelopes)
    if len(identities) > 1:
        return True
    # An id this batch cannot TRACK is still an id the broker assigned, and
    # discarding it left the sibling carrying it invisible to every check
    # below: `orders: [{"id": 10**1000}]` beside a validated order is dropped
    # by `_usable_order_ids`, so `identities` stays at one, and with no state,
    # symbol or fill of its own the unattributed checks pass over it too
    # (Codex review, PR #786: "Halt on untrackable positive IDs in successful
    # siblings").
    #
    # This is round 39's rule reaching the success path, which is where round
    # 41 built this function and did not carry it across -- the intersection
    # of two findings each already fixed on its own side. The rule is
    # unchanged, including its confinement to POSITIVE values: `0`/`-1` are
    # the documented wrapper sentinels for "no id assigned", and halting on
    # those would undo round 21's over-halt fix here exactly as it would
    # there.
    if _untrackable_positive_order_id_reported(envelopes):
        return True
    # ... and a sibling does not need an ID to be an order. An envelope naming
    # NO order but reporting an accepting state and real spend -- e.g.
    # ``orders: [{"symbol": "MSFT", "state": "filled",
    # "executed_dollar_amount": 100}]`` beside a validated AAPL order -- is a
    # second order this batch never counted, and counting only usable
    # identities walked straight past it (Codex review, PR #786: "Inspect
    # ID-less sibling orders on successful responses").
    #
    # This corrects the scope of THIS function's own first version. Its
    # docstring justified skipping states and fill figures on the grounds that
    # "the validated order is allowed to be queued" -- true of that order's
    # OWN envelopes, and not a reason to ignore an envelope describing
    # something else. Round 29's rule, applied to a carve-out of mine: no
    # wider than the sentence justifying it.
    # ANCESTORS vs SIBLINGS, which the flat envelope list alone cannot tell
    # apart -- and getting that wrong is the dangerous direction here. The
    # `response` -> `data` -> `data.order` chain is exactly the validated
    # order's own nesting, so a figure or state on one of those levels
    # plausibly describes THAT order: `{"executed_dollar_amount": 2.00,
    # "data": {...validated...}}` is an ordinary success reporting its own
    # fill on the outer envelope, and halting on it would stop every clean
    # placement in the batch. Anything else the breadth-first walk found is a
    # branch beside that chain, and a branch describing an order is describing
    # a DIFFERENT one.
    chain = {id(response), id(data), id(order)}
    # The validated order's OWN envelopes are checked against the request
    # first, before any sibling question is asked.
    #
    # The ancestors-vs-siblings rule below says a figure on the chain
    # "plausibly describes THAT order" -- true, and not a reason to leave it
    # unread. `_placement_failure_reason` compares `dollar_amount`, the amount
    # REQUESTED, and nothing anywhere compared the EXECUTED spellings against
    # it, so a filled $2 order reporting `executed_notional: 100` passed both
    # validators and `execute_plan` booked $2 and carried on with $98 of real
    # spend outside every cap and buying-power figure this run holds (Codex
    # review, PR #786: "Validate execution figures on the primary envelope").
    # Round 51 asked exactly this question of off-chain siblings and stopped
    # at the chain boundary; the boundary decides whose order a figure
    # describes, and this figure is unaccounted spend either way.
    #
    # Halting rather than failing is the honest outcome: the order really was
    # accepted, so the caller records it and then stops, which is what the
    # sibling branch below already does.
    #
    # The REQUEST is the reference here, not the chain's own `dollar_amount`
    # -- the opposite priority to the sibling check below, and deliberately
    # so. There the primary's values are the reference because the question
    # is whether a sibling describes a DIFFERENT order; here the question is
    # whether this order spent more than the caps approved, and the only
    # figure that answers it is the amount `_validate_execute_plan` approved.
    # Reading the chain's own `dollar_amount` would let a wrapper level
    # naming a larger amount vouch for its own overspend.
    _chain_objects = list({id(e): e for e in (response, data, order)}.values())
    _requested_amount = _coerced_float(expected_amount_usd)
    _chain_reference = (
        [_requested_amount]
        if _requested_amount is not None
        # No request to compare against -- fall back to what the chain itself
        # named, so a caller that passes no expected amount still gets the
        # check rather than none.
        else _cap_approved_amounts(_chain_objects, None)
    )
    if _reports_executed_notional_above(_chain_objects, _chain_reference):
        return True
    # ...and the executed figure was only ONE of the things nobody compared
    # along that chain. `_placement_failure_reason` validates exactly the
    # envelope its descent SELECTS -- symbol, side, client reference, the
    # requested `dollar_amount`, the state -- and reads none of the levels
    # above it. So `{"dollar_amount": 100, "data": {…a clean $2 order…}}`
    # passed both validators for a $2 request, and so did an outer
    # `state: "rejected"` over a `queued` primary: the batch recorded the
    # planned $2, reset the failure streak, and carried on while the same
    # order's own chain reported an amount the caps never approved, or said
    # the order was dead (Codex review, PR #786: "Validate every
    # primary-chain envelope against the request").
    #
    # Round 52 fixed the executed-notional half of exactly this and left the
    # rest, which is the shape this PR keeps repeating: a rule aimed at one
    # field when the boundary it corrects covers several.
    #
    # Symbol, side, and the ORDER-REFERENCE aliases. A single-order placement
    # response has no business naming another ticker, another side, or another
    # idempotency request at any level of the validated order's own chain.
    #
    # `client_id` alone stays exempt, and round 53 got the WIDTH of that
    # exemption wrong: it excluded the whole `_CLIENT_REFERENCE_KEYS` family
    # because ONE member of it carries a second meaning. `client_id` really is
    # what a transport wrapper puts its own request id under, and comparing
    # THAT against the order's reference would halt every placement from such
    # a wrapper. `ref_id`, `client_order_id` and `client_order_uuid` carry no
    # such second meaning -- they name the order's idempotency request and
    # nothing else -- so an ancestor carrying `ref_id: "OTHER"` over an inner
    # order carrying this request's own reference is the response saying it
    # describes two different requests, and both validators accepted it
    # (Codex review, PR #786: "Validate request-reference aliases on ancestor
    # envelopes").
    #
    # My own rule, broken in the round that cited it: a carve-out is exactly
    # as wide as the sentence justifying it, and that sentence was about one
    # key.
    #
    # The reference is compared EXACTLY, never case-folded -- it is an
    # idempotency key and this module's generated reference ends in the
    # uppercase ticker (round 46). Ticker and side stay folded, since those
    # are the same ticker and side however a wrapper spells them.
    for _alias_keys, _requested, _case_folded in (
        (_SYMBOL_KEYS, expected_symbol, True),
        (("side",), expected_side, True),
        (_CHAIN_ORDER_REFERENCE_KEYS, expected_ref_id, False),
    ):
        if _requested is None:
            continue
        _expected_text = _requested.strip()
        if not _expected_text:
            continue
        if _case_folded:
            _expected_text = _expected_text.lower()
        for _envelope in _chain_objects:
            _values = _aliased_values(_envelope, _alias_keys, case_folded=_case_folded)
            if _values - {_expected_text}:
                return True
    # The requested notional, compared as an EQUALITY against the request
    # rather than as the ceiling above -- these are two different questions
    # about two different fields. `dollar_amount` is what was ASKED FOR, so a
    # chain level naming a different one contradicts the request in either
    # direction; the executed spellings are what was SPENT, where only an
    # excess is unaccounted. Same 0.001 tolerance the selected order's own
    # check uses.
    if _requested_amount is not None:
        for _envelope in _chain_objects:
            if "dollar_amount" not in _envelope:
                continue
            _named = _coerced_float(_envelope.get("dollar_amount"))
            # A supplied but unreadable amount cannot be treated like an
            # omitted amount.  The selected order may still be clean, but the
            # ancestor is claiming to describe that same request with a value
            # we cannot reconcile to the amount approved by the caps.
            if _named is None or abs(_named - _requested_amount) > 0.001:
                return True
    # A DEAD state anywhere on the chain, where no chain level reports a
    # working one, denies the acceptance the caps just booked -- round 50's
    # contradiction rule, which until now only ever ran against OFF-CHAIN
    # echoes because its loop skips any identity group with no sibling. An
    # ancestor is not a sibling, so an outer `rejected` over an inner `queued`
    # reached no check at all.
    #
    # No "unless another level says working" carve-out, and the first pass at
    # this had one -- copied from the off-chain rule's shape and measured
    # wrong immediately, because there the WORKING level is what makes a dead
    # echo a contradiction, not what excuses it. The measurement is what
    # caught it: `{"state": "rejected", "data": {…"state": "queued"…}}` --
    # the reported case -- came back False under the carve-out.
    #
    # A plain membership test is right here for a reason that does not hold
    # off-chain: reaching this function AT ALL means the selected order was
    # accepted, so `_placement_failure_reason` has already refused every dead
    # state on the envelope it validated. A dead state left on the chain is
    # therefore an ANCESTOR's, and no ordinary progression -- `queued` then
    # `filled`, or a wrapper echoing either -- passes through one.
    _chain_state_values = {
        _envelope["state"].strip().lower()
        for _envelope in _chain_objects
        if isinstance(_envelope.get("state"), str)
    }
    if any("state" in _envelope and not isinstance(_envelope.get("state"), str) for _envelope in _chain_objects):
        # State is acceptance evidence.  If a chain level supplies it in an
        # unreadable shape, accepting the inner order would guess past a
        # potentially terminal report and reset the placement-failure streak.
        return True
    if _chain_state_values & _DEAD_PLACEMENT_STATES:
        return True
    # A STRING chain state that is neither a recognized dead state (just
    # above) NOR a recognized LIVE one is exactly as unreadable as the
    # non-string case just above it -- blank, or a word this vocabulary does
    # not define. `{"state": "mystery", "data": {"order": <clean queued
    # inner>}}` and `{"state": "", ...}` are both STRINGS, so the isinstance
    # check above passed them, and neither is in `_DEAD_PLACEMENT_STATES`, so
    # the membership test just above also passed them: both response
    # validators then accepted a response whose own chain supplied state
    # evidence this classifier could not interpret (Codex review, PR #786:
    # "Reject unrecognized states on primary-chain ancestors"). This is round
    # 62's rule for a same-ID sibling's unrecognized state -- absent from the
    # live vocabulary is not a cleared claim -- reaching the ancestor chain
    # it did not touch.
    if _chain_state_values - _CANONICAL_LIVE_ORDER_STATES:
        return True
    # An off-chain envelope REPEATING the primary's id is only an echo while it
    # agrees with the primary. Grouping by id alone classified the whole group
    # as attributed and dropped it before any sibling check ran, so supplying
    # the primary's id bypassed every one of them:
    # `orders: [{"id": "A", "symbol": "MSFT", "state": "queued",
    # "executed_dollar_amount": 100}]` beside a validated AAPL order said the
    # same broker order may have spent $100 on MSFT, and the batch recorded the
    # planned AAPL amount and carried on (Codex review, PR #786: "Inspect
    # contradictory same-ID sibling envelopes").
    #
    # Compared against the PRIMARY's own values rather than the request's, so
    # this stays self-contained: `_placement_failure_reason` has already proven
    # those match the request, making them the right reference.
    #
    # STATE is compared only in ONE direction: a DEAD state off-chain against a
    # working one on the chain.
    #
    # Round 44 excluded state entirely, reasoning that the order is known
    # accepted so `queued` beside `filled` changes nothing about
    # accountability. That is right for PROGRESS -- a wrapper reporting an
    # order at two points in its life -- and wrong for CONTRADICTION: an echo
    # of the validated order saying `rejected`/`cancelled`/`expired` is the
    # broker stating that the order the caps just accepted is dead, which is
    # not a later stage of `queued`, it is a denial of it. Recording a
    # submission and resetting the failure streak on that response books
    # spend for an order the response itself says never lived (Codex review,
    # PR #786: "Halt on contradictory terminal echoes").
    #
    # Deliberately asymmetric, and measured rather than assumed: only
    # off-chain DEAD beside on-chain WORKING halts. Progress in either
    # direction -- `queued` then `filled`, or a wrapper echoing `queued`
    # after `filled` -- still accepts, which is the over-halt round 44's
    # exclusion was protecting against.
    for _oid, group in identities:
        _chain_envelopes = [e for e in group if id(e) in chain]
        _off_chain = [e for e in group if id(e) not in chain]
        if not _chain_envelopes or not _off_chain:
            continue
        _chain_states = {
            str(e["state"]).strip().lower()
            for e in _chain_envelopes
            if isinstance(e.get("state"), str)
        }
        # An EMPTY chain-state set does not excuse the sibling. A primary
        # mapping may omit `state` entirely -- `_placement_failure_reason`
        # permits that, and returning None from it means the order was
        # accepted -- so "the primary said nothing about state" reads as
        # ACCEPTED-and-not-known-dead, which a `rejected` echo contradicts
        # exactly as it contradicts an explicit `queued` (Codex review,
        # PR #786: "Reject terminal echoes when the primary omits state").
        #
        # Round 49's lesson, not carried into the check I added one round
        # later: a comparison that finds nothing to compare against asks what
        # ELSE serves as the reference before skipping. Here the reference is
        # the acceptance the failure path already established.
        if not (_chain_states & _DEAD_PLACEMENT_STATES):
            for _envelope in _off_chain:
                if "state" not in _envelope:
                    # ABSENT is ordinary: an echo that simply does not
                    # restate the state says nothing, exactly like an echo
                    # that omits `dollar_amount` (round 57/59's rule, applied
                    # to this field).
                    continue
                _state = _envelope.get("state")
                _normalized = (
                    _state.strip().lower() if isinstance(_state, str) else None
                )
                if _normalized in _DEAD_PLACEMENT_STATES:
                    return True
                if _normalized not in _CANONICAL_LIVE_ORDER_STATES:
                    # PRESENT and unreadable as either a recognized dead OR
                    # recognized live state -- blank, non-string, or a word
                    # this vocabulary does not define. Reading this as an
                    # absent state let it pass silently: `orders: [{"id":
                    # "A", "state": "mystery"}]` beside a clean `queued`
                    # primary satisfied both response validators, and
                    # `execute_plan` recorded the submission and reset the
                    # failure streak even though the broker supplied state
                    # evidence this classifier could not interpret (Codex
                    # review, PR #786: "Reject unreadable states in same-ID
                    # echoes"). A present value this classifier cannot place
                    # in either vocabulary cannot be ruled OUT as a dead-state
                    # contradiction either, so it is unaccounted rather than
                    # ordinary -- the same treatment the chain's own
                    # unreadable state gets a few lines above.
                    return True
        # Pooled across every spelling of each logical field, not compared
        # field by field. A field-by-field comparison silently skips whenever
        # the two envelopes spell one field differently -- the primary's
        # `ref_id` never meets the sibling's `client_order_uuid`, and neither
        # set is ever tested against the other. Pooling keeps the rule
        # identical (any value the sibling names that the primary did not is
        # a different order) while making it blind to spelling.
        # The REQUEST is the fallback reference when the primary carries no
        # value for a field. Skipping the comparison instead meant a primary
        # of `{"id": "A", "state": "queued"}` -- which
        # `_placement_failure_reason` accepts, since it only checks the fields
        # a response actually supplies -- left a sibling naming MSFT, the
        # wrong side, a different reference or a different notional compared
        # against nothing at all (Codex review, PR #786: "Validate sibling
        # fields when the primary omits them").
        #
        # Sound because `_placement_failure_reason` has already proven the
        # primary matches the request on every field it DOES carry, so the
        # request is what the primary would have said had it said anything.
        for _alias_keys, _case_folded, _requested in (
            (_SYMBOL_KEYS, True, expected_symbol),
            (("side",), True, expected_side),
            (_CLIENT_REFERENCE_KEYS, False, expected_ref_id),
        ):
            _primary = set()
            for _envelope in _chain_envelopes:
                _primary |= _aliased_values(_envelope, _alias_keys, case_folded=_case_folded)
            if not _primary and _requested is not None:
                _requested_text = _requested.strip()
                if _requested_text:
                    _primary = {
                        _requested_text.lower() if _case_folded else _requested_text
                    }
            if not _primary:
                continue
            for _envelope in _off_chain:
                if _aliased_values(_envelope, _alias_keys, case_folded=_case_folded) - _primary:
                    return True
        # Same request fallback as the fields above, for the same reason.
        _primary_amounts = _cap_approved_amounts(_chain_envelopes, expected_amount_usd)
        if _primary_amounts:
            for _envelope in _off_chain:
                if "dollar_amount" not in _envelope:
                    # ABSENT says nothing, and never has: an echo that simply
                    # does not restate the amount is ordinary.
                    continue
                _value = _coerced_float(_envelope.get("dollar_amount"))
                if _value is None:
                    # SUPPLIED AND UNREADABLE is a different answer, and this
                    # line read it as the absent one. `null`, `true`,
                    # `"unknown"` or a container all came back `None` and were
                    # skipped -- and `attributed` below then removed the
                    # envelope from `unattributed`, so `_combined_fill_evidence`
                    # never saw it either. Measured, a clean $2 primary beside
                    # `orders: [{"id": "ORD", "dollar_amount": "unknown"}]`
                    # recorded a successful $2 submission while the response
                    # supplied contradictory amount evidence nobody could read
                    # (Codex review, PR #786: "Reject unreadable notionals in
                    # same-ID echoes").
                    #
                    # This is round 57's rule -- absent is not the same answer
                    # as unreadable -- on the line directly ABOVE the one round
                    # 57 fixed, and it matches what the validated chain already
                    # does with an unreadable `dollar_amount` of its own.
                    return True
                if all(
                    abs(_value - _primary) > 0.001 for _primary in _primary_amounts
                ):
                    return True
            # EXECUTED figures are a different question from the requested
            # notional, and were not asked at all: a same-id echo reporting
            # its fill through `executed_dollar_amount`, `executed_notional`
            # or `filled_notional` slipped past the `dollar_amount` test, and
            # `attributed` then removed that envelope before
            # `_combined_fill_evidence` could see it -- so a validated $2
            # order beside `orders: [{"id": "A", "state": "filled",
            # "executed_dollar_amount": 100}]` recorded the planned $2 and
            # carried on (Codex review, PR #786: "Validate executed notionals
            # in same-ID echoes").
            #
            # See `_reports_executed_notional_above` for why this is a ceiling
            # rather than an equality test.
            if _reports_executed_notional_above(_off_chain, _primary_amounts):
                return True
    attributed = {id(envelope) for _oid, group in identities for envelope in group}
    unattributed = [
        e for e in envelopes if id(e) not in attributed and id(e) not in chain
    ]
    if not unattributed:
        return False
    # An accepting state on an OFF-CHAIN envelope halts, even when it is the
    # same state the validated order reports.
    #
    # The first version of this excused a matching state as an echo, borrowing
    # the failure path's contradiction test. That test is right THERE, where
    # the candidates for "echo" include the enclosing wrapper. Here they do
    # not: `unattributed` has already removed every mapping in the
    # `response` -> `data` -> `data.order` chain, so an entry inside `orders`
    # cannot be the outer-wrapper echo the carve-out was written to tolerate
    # -- it is a distinct branch describing an order (Codex review, PR #786:
    # "Treat off-chain accepted states as sibling orders"). The carve-out
    # borrowed a rule together with a justification that the chain exclusion
    # had already made false.
    #
    # `{"data": {"id": "A", "state": "queued", ...}, "orders": [{"state":
    # "queued"}]}` is the shape: identical state, no id, off the chain, and
    # previously accepted. An id-less array entry beside a validated order is
    # ambiguous at best, and ambiguity halts everywhere else in this module --
    # this branch was the one place inverting that, on the weaker reasoning
    # that brokers echo created orders in arrays. They do, and those echoes
    # carry the id, which the identity grouping already absorbs.
    for envelope in unattributed:
        # PRESENCE decides, not recognition -- a state this vocabulary does
        # not define is not a cleared claim. The original check only halted
        # on a RECOGNIZED live or dead state, so an id-less sibling reporting
        # a present-but-unrecognized value -- `orders: [{"symbol": "AAPL",
        # "state": "pending"}]` beside a validated AAPL buy -- read as though
        # it had said nothing: `pending` is in neither
        # `_CANONICAL_LIVE_ORDER_STATES` nor `_POST_ACCEPTANCE_TERMINAL_
        # STATES`, its matching symbol passed the next check (this is the
        # SAME symbol, not a differing one), and it carries no fill-evidence
        # key either, so both response validators accepted a response that
        # may describe a second, uncounted live order (Codex review, PR
        # #786: "Reject unreadable states on ID-less sibling orders"). This
        # is round 62's rule -- a same-ID sibling's unrecognized state
        # halts rather than reading as absent -- reaching the id-less
        # sibling loop it did not touch. ABSENT is the only value that says
        # nothing: an envelope that simply does not carry a `state` key at
        # all (the bare-metadata carve-out's own shape) is unchanged.
        #
        # Gated on `list_item_ids`/order-bearing evidence, which the
        # unqualified version above was not. `orders: [{"state": "queued"}]`
        # is an item of a LIST field -- its very presence there is positional
        # evidence that it describes an order, the same reasoning this
        # module already applies to a bare id-less array entry -- so it
        # keeps halting unconditionally. But `_response_envelopes` also
        # walks plain MAPPING fields now (round 69), and a wrapper's
        # completely unrelated nested mapping -- `{"data": <validated
        # order>, "metadata": {"state": "healthy"}}`, a service-health
        # field that happens to share the word "state" -- is not an item of
        # any list and names no order id, side, reference, or notional
        # either. Halting the whole batch on it read an ordinary successful
        # response as a second, uncounted order (Codex review, PR #786:
        # "Ignore non-order metadata states when scanning siblings"). Only a
        # mapping-field-sourced envelope that ALSO carries some other
        # order-bearing evidence stays covered by "presence decides" --
        # exactly the id-less-but-symbol-bearing shape round 65 was written
        # for.
        if "state" in envelope and (id(envelope) in list_item_ids or _envelope_names_an_order(envelope)):
            return True
        # A symbol reported off-chain that DIFFERS from the request is
        # describing an order that is not the validated one, whatever state it
        # claims. Read through every spelling: `orders: [{"instrument_symbol":
        # "MSFT"}]` names a second order exactly as `{"symbol": "MSFT"}` does,
        # and reading the one key walked past it.
        #
        # Narrowed to a DIFFERING symbol, which is all the sentence above ever
        # justified. As first written it halted on ANY symbol, so a wrapper
        # carrying non-order metadata beside the order -- `{"data": <validated
        # order>, "metadata": {"symbol": "AAPL"}}` -- named the request's own
        # ticker, carried no id, no state and no fill, and stopped every
        # remaining ticker in the batch on a completely ordinary successful
        # response (Codex review, PR #786: "Ignore non-order symbol metadata
        # when scanning siblings").
        #
        # What this rule uniquely adds is a ticker the response should not be
        # mentioning at all, and that is exactly what it now tests for. With
        # no request symbol to compare against, any symbol still halts -- an
        # unverifiable claim is not a cleared one.
        # Pooled across every spelling, not resolved to the first one present
        # -- `_first_present_key` reads only `symbol` when both aliases are
        # supplied, so a self-contradictory `{"symbol": "AAPL",
        # "instrument_symbol": "MSFT", ...}` beside a validated AAPL buy
        # matched on the first alias and this check never saw the second,
        # even though the sibling itself disagrees about which ticker it
        # names (Codex review, PR #786: "Validate every symbol alias on
        # sibling envelopes"). Blank values are filtered out exactly as the
        # single-value read discarded them via its own truthiness check.
        symbol_values = {v for v in _aliased_values(envelope, _SYMBOL_KEYS, case_folded=True) if v}
        if symbol_values:
            if expected_symbol is None:
                return True
            if symbol_values - {expected_symbol.strip().lower()}:
                return True
        # The claim above -- that repeating the request's own symbol is still
        # caught by the state or fill-evidence checks if it carries real order
        # evidence -- was wrong, and measured wrong: `{"symbol": "AAPL",
        # "side": "sell", "dollar_amount": 100, "ref_id": "OTHER"}` beside a
        # validated $2 AAPL buy repeats the request's own ticker, so the
        # symbol check above passes it over, and it has neither a `state` key
        # for the check above nor any of the EXECUTED spellings
        # `_fill_evidence` reads -- `dollar_amount` is what was ASKED FOR, a
        # different field from what was spent -- so the fill-evidence check
        # below passes it over too. Both response validators then accept a
        # response that plainly describes a second, opposite-side order this
        # run never counted (Codex review, PR #786: "Recognize order-bearing
        # siblings that repeat the request symbol").
        #
        # `side` and the order-reference aliases are fields no non-order
        # metadata wrapper has a reason to carry -- unlike a bare `symbol`,
        # which a wrapper might echo as ordinary context (round 56's fix).
        # Compared against the REQUEST, exactly as the chain-ancestor check
        # above compares them, and for the same reason: `_placement_failure_
        # reason` has already proven the validated order matches the request
        # on every field it carries, so a sibling naming something else is
        # naming a different order. `client_id` stays excluded from the
        # reference aliases for the same round-53 reason it is excluded on
        # the chain: an unattributed envelope is, structurally, exactly the
        # kind of wrapper-owned entry that field can legitimately belong to.
        for _alias_keys, _case_folded, _requested in (
            (("side",), True, expected_side),
            (_CHAIN_ORDER_REFERENCE_KEYS, False, expected_ref_id),
        ):
            if _requested is None:
                continue
            _expected_text = _requested.strip()
            if not _expected_text:
                continue
            if _case_folded:
                _expected_text = _expected_text.lower()
            if _aliased_values(envelope, _alias_keys, case_folded=_case_folded) - {_expected_text}:
                return True
        # The REQUESTED notional, compared as an equality exactly like the
        # chain-ancestor check above -- this is what was ASKED FOR, not what
        # executed, so it is a different question from the fill-evidence scan
        # below. A supplied-but-unreadable amount is a different answer from
        # an absent one (round 57/59's rule), so it halts here too.
        if _requested_amount is not None and "dollar_amount" in envelope:
            _named = _coerced_float(envelope.get("dollar_amount"))
            if _named is None or abs(_named - _requested_amount) > 0.001:
                return True
        # Every check above only halts on a DIFFERENCE from the request --
        # matching it was read as "an ordinary echo" (round 63). That is
        # right for a THIN echo (a bare `symbol`, or `symbol` plus a
        # transport wrapper's own `client_id`), but wrong for an entry
        # that is, by field count, a genuinely complete order description:
        # `orders: [{"symbol": "AAPL", "side": "buy", "dollar_amount": 2,
        # "ref_id": "REF"}]` beside a validated $2 AAPL buy repeats every
        # field the request specifies, so nothing above finds a difference
        # to halt on -- but this envelope has NO id tying it to the
        # primary, so agreement is not attribution. It reads exactly like
        # a second, genuinely accepted order that happens to share the
        # request's own parameters (a retried submission the connector
        # treated as new despite reusing this run's own idempotency
        # reference, say), left outside submission, cap, and buying-power
        # accounting (Codex review, PR #786: "Halt on matching ID-less
        # order-list entries"). Requiring identity attribution rather than
        # mere field agreement is the SAME correction the earlier checks in
        # this loop already apply to a DIFFERING sibling; this closes the
        # matching side of that same gap.
        #
        # Confined to LIST items (`id(envelope) in list_item_ids`), for the
        # same reason the state/success-flag checks above are: an entry's
        # position in an `orders`-shaped array is explicit order-bearing
        # evidence a plain mapping field does not carry.
        #
        # Confined to at least TWO of the five request/fill-comparable
        # fields (symbol, side, an order-reference alias, `dollar_amount`,
        # or any fill-evidence key), not one: a bare `{"symbol": "AAPL"}`
        # is exactly the thin echo round 56 carved out, a bare
        # `{"cumulative_quantity": 0}` is an ordinary same-order echo
        # ruling out its OWN execution (round 41 -- an id-less sibling that
        # reports NOTHING else must stay an accepted read of the one order
        # this response names), and `{"symbol": "AAPL", "client_id":
        # "OTHER"}` stays excluded the same way -- `client_id` is
        # deliberately not one of the counted reference aliases (round 53).
        #
        # The fill-evidence category is evidence only a real order report
        # would carry -- nothing else in a placement response has a reason
        # to name `cumulative_quantity`/`executed_notional`/etc. So
        # `orders: [{"symbol": "AAPL", "filled_quantity": 0}]` (two
        # categories: symbol AND a fill key) still names a second,
        # distinct order-shaped entry, even though the figure is an
        # EXPLICIT ZERO. The aggregate fill-evidence read at the end of
        # this function only treats POSITIVE-OR-UNREADABLE as unaccounted
        # SPEND, which is the right answer for money -- but this question
        # is whether an ORDER was attempted at all: a rejected or
        # cancelled second buy attempt spends nothing and must still count
        # toward max_orders_per_day and the consecutive-failure breaker
        # (Codex review, PR #786: "Halt on zero-fill sibling order
        # entries").
        _comparable_evidence_count = (
            bool(symbol_values)
            + ("side" in envelope)
            + bool(_aliased_values(envelope, _CHAIN_ORDER_REFERENCE_KEYS, case_folded=False))
            + ("dollar_amount" in envelope)
            + any(k in envelope for k in _PARTIAL_FILL_EVIDENCE_KEYS)
        )
        if id(envelope) in list_item_ids and _comparable_evidence_count >= 2:
            return True
    # A sibling naming no order, no state, and no fill figure can still say
    # the placement succeeded via an explicit boolean flag: `orders:
    # [{"success": true}]` beside a validated order carries none of the
    # evidence any check above reads, so it passed every one of them, and the
    # fill-evidence scan below inspects quantity/notional keys, not boolean
    # flags, so it passed that too. The failure-path classifier
    # (`_placement_response_may_hold_a_live_order`) already reads exactly
    # this `success`/`ok`/`approved` vocabulary as acceptance evidence for an
    # id-less order; an off-chain sibling on the SUCCESS path deserves the
    # same reading, not a pass (Codex review, PR #786: "Inspect success
    # flags on ID-less sibling orders").
    #
    # Gated exactly like the state check above -- a list-item envelope's
    # position is positional evidence on its own, but a mapping-field
    # envelope needs real order-bearing evidence, or a completely
    # unrelated `{"metadata": {"success": true}}` halts the batch on
    # nothing an order would ever carry (Codex review, PR #786: "Restrict
    # sibling success flags to order-bearing entries").
    _success_flag_envelopes = [
        e for e in unattributed if id(e) in list_item_ids or _envelope_names_an_order(e)
    ]
    if _success_flag_reported(_success_flag_envelopes):
        return True
    # Spend reported by an envelope that names no order and echoes no state
    # cannot be attributed to the validated order OR ruled out, which is the
    # same reading the failure path gives an unattributed positive figure.
    return _combined_fill_evidence(unattributed) == _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE


def _untrackable_positive_order_id_reported(envelopes: list) -> bool:
    """Whether any envelope names an id we cannot use but that is plainly a
    broker-ASSIGNED one.

    ``_is_usable_order_id`` answers "can this batch track this order?", and the
    no-identity branch reads a False from it as "the broker named no order".
    That reading is right for every value it rejects for being a NON-id -- a
    falsy ``None``/``""``/``0``, a bool, a container, whitespace, and the
    documented ``-1`` sentinel a wrapper uses for "no id was assigned". It is
    wrong for a positive number we merely cannot REPRESENT: an id of
    ``10**1000`` is the broker handing us something, and calling that "no
    order" isolated a ticker whose order may be live, while the identical
    response shape carrying an ordinary id (``{"id": "ORD"}``) is a clean
    SUBMISSION (Codex review, PR #786: "Preserve acceptance evidence from
    oversized positive IDs"). One response shape cannot mean both "order
    placed" and "no order exists" depending only on how large the number is.

    So this is untrackability treated as AMBIGUITY rather than as proof, which
    is the doctrine everywhere else here: isolate only on proof that nothing
    was accepted.

    Deliberately confined to values that are **positive**, which is what keeps
    it from undoing round 21's over-halt fix. ``-1``/``0`` stay isolatable
    because they are the wrapper sentinels for "no id assigned", and halting on
    them would halt ordinary refusals from any wrapper using that convention.
    ``NaN`` is not positive either (every comparison against it is False), and
    it is the shape of a FAILED parse rather than an overflowed identifier, so
    it stays out too -- it still halts whenever anything else in the response
    reports acceptance, through the checks around this one.
    """
    for envelope in envelopes:
        for key in ("id", "order_id"):
            if key not in envelope:
                continue
            candidate = envelope[key]
            if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
                # A bool is not an id, and a string id is either usable (so it
                # never reaches here) or blank (a non-id). Only NUMERIC ids can
                # be simultaneously well-formed and unrepresentable.
                continue
            if not _numeric_value_is_unusable(candidate):
                # Finite: either already usable, or non-positive and therefore
                # a sentinel handled above.
                continue
            if candidate > 0:
                # Comparison-safe for an unbounded int, and False for NaN.
                return True
    return False


def _success_flag_reported(envelopes: list) -> bool:
    """True if any envelope explicitly flags the placement as having succeeded.

    Reads exactly the vocabulary and the strictness ``_placement_failure_
    reason`` already applies to these keys: ``success``/``ok``/``approved``,
    and only a real boolean ``True``. A truthy non-bool (the string
    ``"true"``, ``1``) is NOT this signal -- the same reasoning as that
    function's own ``isinstance(value, bool)`` check and ``_is_error_
    response``'s strict ``is True``. Loosening it would let arbitrary
    connector data read as acceptance and halt ordinary refusals.
    """
    for envelope in envelopes:
        for key in ("success", "ok", "approved"):
            value = envelope.get(key)
            if isinstance(value, bool) and value is True:
                return True
    return False


def _order_identity_groups(envelopes: list) -> list:
    """The distinct orders this response names, each with its own envelopes.

    Returns ``[(order_id, [envelope, ...]), ...]`` in outermost-first order of
    first appearance, empty when no level names an order at all.

    **Every distinct order, not just the first.** A wrapper can report its own
    request identity on the outer envelope and the real broker order
    underneath -- ``{"success": false, "id": "REQ", "state": "failed",
    "cumulative_quantity": 0, "data": {"order": {"id": "ORD", "state":
    "queued"}}}``. Reading only the outermost identity saw a terminal request
    with an explicit zero fill and isolated, while ``ORD`` sat live at the
    broker and absent from every remaining order's cap and buying-power
    arithmetic (Codex review, PR #786: "Inspect all response identities
    before isolating").

    **Grouped by id, because a repeated id is one order, not two.** A wrapper
    that echoes the same id at two levels and reports the fill on only one of
    them -- ``{"id": "ORD", "state": "rejected", "cumulative_quantity": 0,
    "data": {"id": "ORD", "state": "rejected"}}`` -- was read as two separate
    orders, and the echo, having no fill figure of its own, was demanded
    evidence it could never supply. An ordinary rejected order with an
    explicit zero fill therefore halted the batch (Codex review, PR #786:
    "Deduplicate envelopes that repeat the same order ID").

    Ids are matched on TYPE AND stripped value, and never case-folded. Merging
    is the unsafe direction -- it lets one order's zero vouch for another -- so
    two ids merge only when nothing distinguishes them. Case-folding would
    merge ``ORD`` with ``ord``; stringifying alone would merge numeric ``1``
    with string ``"1"``, letting a terminal request's explicit zero answer for
    a distinct inner order that reported no fill at all (Codex review, PR #786:
    "Keep differently typed order IDs in separate groups"). An echo repeats a
    value exactly, type included, so exact matching still catches the case this
    grouping exists for; anything less exact halts, which is the safe way to be
    wrong.

    Each order's states and fill figures still come only from the envelopes
    that name IT: an id and a state identify one order, so reading a state off
    a level that names a different order would describe an order that does not
    exist.
    """
    groups = []
    by_id = {}
    for envelope in envelopes:
        # EVERY id this envelope names, not just the first: one level can carry
        # two different valid identifiers, and the second one may be a distinct
        # accepted order.
        for order_id in _usable_order_ids(envelope):
            # Type, not the value's own hash: `1` and `1.0` hash equal in
            # Python, so a raw-value key would silently merge them. Keep this
            # identical to the per-envelope de-duplication above.
            key = _order_identity_key(order_id)
            group = by_id.get(key)
            if group is not None:
                group.append(envelope)
                continue
            by_id[key] = [envelope]
            groups.append((order_id, by_id[key]))
    return groups


def _accepted_state_reported(envelopes: list) -> bool:
    """True if any envelope reports a state meaning the broker took the order.

    Read only where the response carries NO usable order id. An id is this
    codebase's usual proof that something was accepted, but it is not the only
    one: a response that omits the id while reporting ``queued``,
    ``confirmed``, ``partially_filled`` or ``filled`` is describing an order
    that exists, and isolating it left that order out of ``submitted`` and out
    of every remaining order's cap and buying-power arithmetic (Codex review,
    PR #786: "Honor working states even when an order ID is missing").

    Deliberately limited to states that READ as acceptance -- the shared
    ``_LIVE_ORDER_STATES`` vocabulary. An absent, null, or unrecognized state
    does NOT count here, unlike the with-an-id branch where anything
    unreadable halts. The asymmetry is the same one that governs silence about
    the fill: with an id there is positive evidence of acceptance, so failing
    safe on everything else costs nothing; without one, an unfamiliar state
    string is the ordinary shape of a broker's own refusal vocabulary, and
    halting on it would restore halt-on-first-rejection -- the exact failure
    this change exists to remove.
    """
    for envelope in envelopes:
        state = envelope.get("state")
        if isinstance(state, str) and state.strip().lower() in _CANONICAL_LIVE_ORDER_STATES:
            return True
    return False


# The fill-evidence classifiers are dca_live_adapters' -- one implementation
# for every entry point, so the gate-sequence runner and the executors/afternoon
# path read a placement response's fill fields identically. This module used to
# carry AST-identical copies of them (2026-09-07 diagnostic, P:DUP-01), the
# duplicated-logic drift CLAUDE.md warns about; the PR #786 rationale for the
# oversized-integer handling lives on the adapter's _is_finite_zero.
_PARTIAL_FILL_EVIDENCE_KEYS = tuple(sorted(_PLACEMENT_EXECUTION_FIELDS))


def _terminal_order_may_have_filled(envelopes: list, identities: list) -> bool:
    """Whether a terminal placement response fails to RULE OUT a partial fill.

    Answers the narrow question ``_placement_response_may_hold_a_live_order``
    needs before it isolates dead orders that carry usable order ids: did any
    of them settle shares before it died? Takes every envelope level rather
    than the single selected ``order`` mapping, since a wrapper can report the
    fill outside it (Codex review, PR #786: "Inspect fill evidence across
    every response envelope").

    **Asked once PER NAMED ORDER, not once for the response.** Combining every
    level into a single verdict let one order's explicit zero answer for
    another order that said nothing at all: ``{"id": "REQ", "state":
    "rejected", "cumulative_quantity": 0, "data": {"order": {"id": "ORD",
    "state": "cancelled"}}}`` isolated on REQ's zero while ORD -- a distinct
    accepted order, terminal and silent about its fill -- was never required
    to rule a partial fill out (Codex review, PR #786: "Require zero fill
    evidence for each terminal identity").

    Per DISTINCT order, though: ``identities`` arrives already grouped by id
    (see ``_order_identity_groups``), so a wrapper echoing one id at two
    levels contributes one question answered from both levels, not two
    questions of which the echo can only ever fail the second.

    Which figures count for which order follows from where they sit: a figure
    on an envelope that NAMES an order describes that order. A figure on an
    envelope that names none is unattributed: positive or unreadable evidence
    makes every named order unsafe, while an explicit zero can vouch for the
    order only when exactly one identity was named. That single-identity rule
    keeps the round-15 shape working -- a lone order reported deep with its
    zero attached to an outer wrapper envelope still isolates -- without
    letting one wrapper zero vouch for multiple sibling orders. A wrapper that
    reports the INNER order's fill on its own identity-bearing envelope
    therefore halts, which is the fail-safe direction for a shape we cannot
    confidently attribute.

    **An EXPLICIT readable zero is required to say no.** Absence is not proof:
    a response that simply omits every fill field establishes nothing about
    what executed, and ``dca_live_adapters`` documents the very shape that
    makes this concrete -- ``{"success": false, "data": {"id": ..., "state":
    "cancelled", "cumulative_quantity": ...}}``, a partially filled order
    whose cancellation surfaced as the outer failure -- while
    ``near_close_dca``'s terminal-row collector already applies exactly this
    rule to artifacts ("An EXPLICIT numeric zero is required, not merely the
    absence of a positive quantity"), noting that a real fill's
    ``cumulative_quantity`` may simply not have been flushed yet (Codex
    review, PR #786: "Treat omitted fill evidence as ambiguous").

    This does NOT re-introduce halt-on-first-rejection. The caller reaches
    this function only for a response carrying a USABLE ORDER ID, which in
    this codebase is itself evidence the broker accepted something -- see the
    adapters' "authoritative evidence regardless of the outer envelope's own
    verdict". The reported incident's shape (a broker refusing a ticker
    outright) has no order id at all and is judged by the narrower
    ``_FILL_EVIDENCE_POSITIVE_OR_UNREADABLE`` test instead, so silence about
    the fill still isolates there.
    """
    # Figures nobody claims. Counted toward every identity, since an envelope
    # that names no order cannot be describing one order in preference to
    # another.
    unattributed = [
        envelope for envelope in envelopes if not _usable_order_ids(envelope)
    ]
    unattributed_evidence = _combined_fill_evidence(unattributed)
    # A positive or unreadable figure nobody claims is spend this response
    # cannot account for, however many orders it names.
    if unattributed_evidence == _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE:
        return True
    # An unattributed ZERO is different: it can only vouch for ONE order. With
    # a single named order it plainly describes that order -- the round-20
    # shape, a lone order reported deep with its zero on an outer wrapper.
    # With two, `{"success": false, "cumulative_quantity": 0, "orders":
    # [{"id": "A", "state": "cancelled"}, {"id": "B", "state": "cancelled"}]}`
    # was letting one zero prove BOTH orders unfilled, when it cannot say
    # which of them it describes -- or whether it is their sum (Codex review,
    # PR #786: "Require distinct zero-fill evidence for each order"). Past
    # one identity, each order has to carry its own zero.
    share_unattributed = len(identities) == 1
    for _, group in identities:
        scope = group + unattributed if share_unattributed else group
        if _combined_fill_evidence(scope) != _FILL_EVIDENCE_EXPLICIT_ZERO:
            return True
    return False


def _redacted_placement_failure(failure: str, account_number: str | None) -> str:
    """Redact the one secret this module knows for certain out of a failure
    reason before it is recorded.

    Shared by the per-ticker :class:`PlacementFailure` and the batch-level
    ``ExecutionResult.failure_reason`` so the two can never drift: both are
    persisted to durable run evidence and sent to alerts, so an isolated
    failure must be scrubbed exactly as thoroughly as a halt. Broker-provided
    error text is arbitrary external data (see ``_redacted_envelope_error``,
    which has already stripped token-shaped runs from it by this point); this
    is the account-number pass that needs ``broker.account_number`` to run.
    """
    if account_number:
        return failure.replace(account_number, "[REDACTED-ACCOUNT]")
    return failure


@dataclass(frozen=True)
class PlacementFailure:
    """One ticker's placement failure, isolated from the rest of the batch.

    ``reason`` is already redacted for persistence/alerting exactly like
    ``ExecutionResult.failure_reason``: the account number this module knows,
    and any secret-shaped token a broker error string embedded, are stripped
    before the reason is ever recorded (see ``_redacted_placement_failure``).
    A connector exception contributes only its type NAME, never its message,
    for the same reason.
    """

    ticker: str
    reason: str


@dataclass
class ExecutionResult:
    """What ``execute_plan`` actually did, from ``get_equity_orders`` responses,
    never from intent.

    ``submitted`` lists tickers placed successfully, in order.

    ``failures`` lists EVERY ticker whose placement failed, in order, each with
    its own (redacted) reason -- see :class:`PlacementFailure`. One ticker's
    rejection no longer stops the batch (2026-08-24 policy change; see
    ``execute_plan``'s docstring), so a run can legitimately finish with both
    ``submitted`` and ``failures`` non-empty and ``halted_on`` still ``None``.
    A non-empty ``failures`` is always a trading failure that needs reporting,
    whether or not the batch halted: **every** entry must reach Gate 9's alert
    and the reconciled artifact.

    ``halted_on`` is the ticker the batch actually STOPPED at -- the window
    closing mid-batch, or the consecutive-failure circuit breaker tripping --
    or ``None`` if the batch ran to completion. It is deliberately NOT set for
    an isolated per-ticker rejection: a caller reading it as "did anything go
    wrong?" would miss the failures the batch survived, so read ``failures``
    for that.

    ``failure_reason`` is why it halted (a closed window, or the
    consecutive-failure breaker naming the streak and the most recent reason),
    or ``None`` if nothing halted, so Gate 9's alert and the reconciled
    artifact don't have to repeat placement or retain out-of-band data to
    explain a halt (Codex review: "Preserve the placement failure reason").

    ``placement_enabled`` reports whether both live gates were flipped, so a
    dry run and a real submission are never confused.

    ``attempt_records`` contains synthetic broker rows for outcomes that do not
    otherwise produce a submitted row. Proven-no-order failures reserve only
    an attempt. Ambiguous placements carry the full requested notional, state
    ``potentially_submitted``, and the exact client reference used at the
    broker boundary under both ``ref_id`` and ``client_order_id`` so later
    reconciliation can bind an id-less broker row while cap checks remain
    conservative until that exact reference is reconciled.
    """

    submitted: list[str] = field(default_factory=list)
    halted_on: str | None = None
    failure_reason: str | None = None
    placement_enabled: bool = False
    failures: list["PlacementFailure"] = field(default_factory=list)
    attempt_records: list[BrokerOrder] = field(default_factory=list)
    # Dry-run only: the refusal _validate_execute_plan would have raised on
    # the live path, so a rehearsal of the exact live call surfaces a plan
    # the live run will refuse instead of returning an empty, clean-looking
    # result (2026-09-07 diagnostic, C:F13). None when validation passed or
    # when the live-path arguments were not supplied at all.
    validation_failure: str | None = None


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
    # Same per-key validation as _usable_order_id, and for the same reason: a
    # truthy-but-invalid `id` beside a valid `order_id` must not read as "no
    # order id". Written out here rather than calling that helper on purpose
    # -- this function's traversal of the response is deliberately independent
    # of the classifier's (see _placement_response_may_hold_a_live_order's
    # docstring), and sharing a traversal is what that independence rules out.
    # Sharing this predicate's SHAPE, not its code, keeps them from diverging
    # without collapsing them into one.
    order_id = None
    for _id_key in ("id", "order_id"):
        _candidate_id = order.get(_id_key)
        if (
            not _candidate_id
            or isinstance(_candidate_id, bool)
            or not isinstance(_candidate_id, (str, int, float))
        ):
            continue
        order_id = _candidate_id
        break
    if order_id is None:
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
    if isinstance(order_id, (int, float)) and _numeric_value_is_unusable(order_id):
        # NaN and infinity are also truthy, so the same gap applies to a
        # numeric id: neither is a real broker-assigned identifier (Codex
        # review: "Reject blank and non-finite placement IDs"). An oversized
        # integer belongs here too, and reached this line as an OverflowError
        # rather than a verdict -- see _numeric_value_is_unusable (Codex
        # review, PR #786: "Treat oversized numeric order IDs as unusable").
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
        # Spellings from _CLIENT_REFERENCE_KEYS rather than an elif chain
        # written out here: this list and the success-path checker's had
        # drifted apart, which is the whole of round 45's first finding.
        # The resolution is unchanged -- first key PRESENT wins, a present
        # null included.
        returned_ref_id, ref_id_present = _first_present_key(order, _CLIENT_REFERENCE_KEYS)
        # ... but resolving to ONE spelling means the others are never read,
        # and an order carrying `ref_id` matching this request beside a
        # `client_order_id` naming a different one confirmed this request via
        # the first key while the response itself said the two disagree. The
        # aliases are spellings of ONE field, so an order that gives them
        # different values is not confirming anything -- it is contradicting
        # itself about which request it belongs to (Codex review, PR #786:
        # "Validate every client-reference alias in the primary order").
        #
        # Checked before the value comparison below, because a self-
        # contradictory order fails whichever spelling happens to match.
        # Exact values, never case-folded: this is an idempotency key, and
        # the comparison below is `str(...) !=` for the same reason.
        _reference_values = {
            str(order[_key]).strip()
            for _key in _CLIENT_REFERENCE_KEYS
            if _key in order and order[_key] is not None
        }
        if len(_reference_values) > 1:
            # The values are request-identifying data rather than secrets,
            # but this string is persisted into ExecutionResult.failure_reason
            # verbatim, so only the COUNT is reported -- the same posture
            # every other malformed-field branch here takes.
            return (
                "broker response carried "
                f"{len(_reference_values)} disagreeing client references for one order"
            )
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
                account_number = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
                if account_number and account_number.strip():
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
    returned_symbol, symbol_present = _first_present_key(order, _SYMBOL_KEYS)
    # Same self-contradiction test as the client reference above, for the same
    # reason: `symbol` and `instrument_symbol` are one field, so an order
    # giving them different tickers is not identifying itself. The finding
    # named only the reference; this pair has the identical shape. Normalized
    # through _normalize_ticker where it can be, so `aapl`/`AAPL` is one
    # ticker and only a real disagreement reaches the refusal.
    _symbol_values = set()
    for _key in _SYMBOL_KEYS:
        if _key in order and order[_key] is not None:
            try:
                _symbol_values.add(_normalize_ticker(str(order[_key])))
            except ValueError:
                # Unusable values are left to the per-value checks below,
                # which report them properly; counting them here would turn a
                # malformed symbol into a "disagreement" and lose the reason.
                continue
    if len(_symbol_values) > 1:
        return (
            f"broker response carried {len(_symbol_values)} disagreeing symbols for one order"
        )
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
                account_number = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
                if account_number and account_number.strip():
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
        except (TypeError, ValueError, OverflowError):
            # OverflowError, not just TypeError/ValueError: float(10**1000)
            # raises it, and this parse sits on the same fatal path as the
            # order-id checks above -- see _numeric_value_is_unusable.
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
    orders_fetched_at: datetime | None,
) -> tuple[datetime | None, str | None]:
    """Sample ``now_fn()`` and return its valid reading and any halt reason.

    ``now_fn`` is an injected callable (e.g. wrapping a system clock or an
    MCP-backed time source) that can raise like any other injected callable.
    Uncaught, that would propagate past ``execute_plan()`` entirely instead
    of the documented ``halted_on``/``failure_reason`` result, denying the
    caller a reconcilable ``ExecutionResult`` for a batch where an earlier
    order in the same loop may already have been submitted and is real,
    committed spend (Codex review: "Preserve partial results when the fresh
    clock fails").

    The order-history freshness check (``orders_fetched_at`` within
    ``MAX_ORDERS_FETCH_AGE_SECONDS`` of "now") runs against THIS fresh
    reading, not only the caller-supplied pre-loop ``now``, when
    ``orders_fetched_at`` is given: a history fetched at 09:31 and checked
    against a stale pre-loop ``now`` of 09:31 would otherwise read as fresh
    even if the real clock had already moved on (Codex review: "Measure
    history freshness against the fresh placement clock"). ``execute_plan``
    runs that check once before the first placement and again before every
    placement in the loop: the 2026-09-07 diagnostic (C:F7) briefly made it
    pre-loop only so a slow batch could finish, but that let every later
    placement run on an unbounded-age snapshot; halting the remainder is the
    fail-closed outcome (Copilot review, PR #1394).
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
        return None, f"now_fn raised {type(exc).__name__}"
    if not isinstance(fresh_now, datetime) or fresh_now.tzinfo is None:
        # fresh_now is whatever the injected now_fn wrapper returned, which
        # can be a connector response (e.g. a dict) carrying an account
        # identifier or token rather than a real datetime; interpolating it
        # verbatim would leak that into ExecutionResult.failure_reason, the
        # same class of leak already closed for now_fn's raised exceptions
        # above. Only the type name is reported, matching assess()'s own
        # non-datetime clock path (Codex review: "Redact malformed values
        # returned by the placement clock").
        return None, f"now_fn returned a value that is not a timezone-aware datetime (got {type(fresh_now).__name__})"
    if fresh_now.astimezone(ET).date() != trading_day:
        return None, (
            f"now_fn returned {fresh_now.isoformat()!r}, which falls on ET calendar day "
            f"{fresh_now.astimezone(ET).date().isoformat()!r}, not "
            f"trading_day={trading_day.isoformat()!r}"
        )
    if not gate_time_sanity(fresh_now, slot):
        return None, (
            f"now_fn reports the {slot_id!r} slot's "
            f"{slot.window_start_et[0]:02d}:{slot.window_start_et[1]:02d}-"
            f"{slot.window_end_et[0]:02d}:{slot.window_end_et[1]:02d} ET acceptance "
            "window has closed before this order could be placed"
        )
    # Slot acceptance and the exchange session are independent: a
    # configured early close can truncate an otherwise valid slot.
    from near_close_dca import _regular_session_failure

    session_failure = _regular_session_failure(fresh_now, trading_day)
    if session_failure:
        return None, session_failure
    if orders_fetched_at is not None:
        orders_age_seconds = (fresh_now - orders_fetched_at).total_seconds()
        if orders_age_seconds < 0 or orders_age_seconds > MAX_ORDERS_FETCH_AGE_SECONDS:
            return None, (
                f"now_fn reports orders_fetched_at is {orders_age_seconds:.1f}s away from the "
                f"fresh clock, outside the {MAX_ORDERS_FETCH_AGE_SECONDS}s freshness window; "
                "refetch order history before starting this batch"
            )
    return fresh_now, None


def _validate_execute_plan(
    plan: Plan,
    broker: _RunnerBroker,
    run_id: str | None,
    snapshot: BudgetSnapshot | None,
    tier: BudgetTier | None,
    trading_day: date | None,
    orders,
    weekly_cap_usd: float | None,
    weekly_rollover_reserve_usd: float | None,
    weekly_rollover_reserve_week_key: tuple[int, int] | None,
    finalized_budget_days,
    submitted_notional_by_week: dict | None,
    buying_power_usd: float | None,
    orders_account_number: str | None,
    now: datetime | None,
    slot_id: str | None,
    orders_fetched_at: datetime | None,
    now_fn: Callable[[], datetime] | None,
    candidates,
    sole_executor_grace_period_active: bool,
) -> tuple[SlotSpec, set[str]]:
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
        # Mirror assess()'s Gate 4 grace-period policy exactly: only a
        # collision whose provenance is the recognized compatible executor
        # ("agentic") may be let through, and only when the flag is the
        # exact literal ``True`` (fail closed against a stringly-typed or
        # otherwise truthy-but-wrong value). Any rogue collision -- missing,
        # manual, or unknown provenance -- continues to refuse placement
        # even during an active grace window, matching assess()'s own
        # "one rogue row cannot be hidden by a compatible row" rule.
        grace_active = sole_executor_grace_period_active is True
        rogue_collisions = [o for o in collisions if o.placed_agent != "agentic"]
        if not grace_active or rogue_collisions:
            raise ValueError(
                f"execute_plan refuses to place: orders already present in the {slot_id!r} ET "
                "window, a second executor may be live"
            )
    try:
        bought_today = {
            _normalize_ticker(o.symbol)
            for o in orders
            if o.is_buy_attempt and _require_aware(o.created_at).astimezone(ET).date() == trading_day
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
    # Mirrors assess(): whether a same-day repeat is refused outright is the
    # config/budgets.json order_rules.one_order_per_ticker_per_day decision,
    # not a hardcoded one. With the switch off, same_day_top_up_policy governs
    # and scripts/same_day_top_up_gate.py resizes the repeat; assess() then
    # PROCEEDs with the bought-today ticker still in plan.to_place, and this
    # function applied the exclusion unconditionally, so the recomputed
    # expected plan never matched and the WHOLE batch was refused -- every
    # other ticker lost its buy for the day too (2026-09-07 diagnostic, C:F2).
    if ONE_ORDER_PER_TICKER_PER_DAY:
        fresh_candidates = [c for c in parsed_candidates if c.ticker not in bought_today]
    else:
        fresh_candidates = list(parsed_candidates)
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
            if (
                finalized_budget_days is None
                or submitted_notional_by_week is None
                or weekly_rollover_reserve_week_key is None
            ):
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
        if ONE_ORDER_PER_TICKER_PER_DAY and ticker in bought_today:
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
        # can still end early (Gate 7): the window can close mid-batch, and
        # the consecutive-failure breaker can trip. Reversing a high- and a
        # low-priority order then lets the lower-priority ticker be bought
        # while the higher-priority one is left unattempted, silently
        # inverting plan_orders()'s configured allocation priority (higher
        # multiplier, then larger drop, then ticker) precisely in the
        # partial-batch case that priority exists to protect (Codex review:
        # "Preserve the rebuilt plan's allocation order"). Isolating a single
        # rejection (2026-08-24) narrowed how often a batch ends early but
        # did not remove it, so this check is unchanged. Comparing the two
        # ordered sequences catches both a reordering and a hand-picked
        # subset (a shorter list can never equal the full one) in a single
        # check.
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

    return slot, bought_today


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
    sole_executor_grace_period_active: bool = False,
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
    ``_placement_failure_reason`` -- is ISOLATED TO ITS OWN TICKER as of
    2026-08-24: it is recorded in ``result.failures`` and the batch continues
    with the next order. It is still never retried within this run.

    That reverses the prior policy ("a broker refusing one order is evidence
    the run's assumptions are stale, not an invitation to keep going"), which
    in practice cost every ticker behind the failing one its buy for the day
    -- "GS, EVR: never submitted -- batch halted after RVII rejection", and
    "34 of 37 planned buys placed and filled, 3 tickers not bought". The
    refusals that actually occur are ticker-scoped (a symbol that cannot be
    bought the way the plan assumed), and each remaining order is
    independently valid: ``_validate_execute_plan`` has already proved the
    WHOLE plan against the caps, the whitelist, buying power, and a freshly
    rebuilt ``plan_orders()`` output before the first placement, and skipping
    a ticker only ever lowers the total actually spent. So one order's failure
    tells the next nothing, and the batch is safe to finish.

    Three things still stop the batch outright:

    * **The window closing mid-batch** (``now_fn``; see above) -- placing
      outside the approved window is unsafe, not merely unsuccessful.
    * **``MAX_CONSECUTIVE_PLACEMENT_FAILURES`` consecutive failures**
      (``config/budgets.json``'s
      ``circuit_breakers.max_consecutive_placement_failures``) -- an unbroken
      run of failures is the case the old blanket halt was reaching for and
      the one where it was right: the broker leg itself is unhealthy (an
      outage, an expired session, an account block, exhausted buying power)
      and every further placement is a doomed live call. Counted
      consecutively, so a batch that keeps succeeding between isolated bad
      symbols runs to completion. A submitted order resets the streak.
    * A validation failure before placement begins, which still raises.

    Callers must therefore read ``result.failures``, not ``result.halted_on``,
    to answer "did anything go wrong?": a completed batch with isolated
    failures leaves ``halted_on`` ``None`` and is still a trading failure that
    Gate 9 must alert on, naming every failed ticker.

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
    ``PRODUCTION_READINESS.md``'s state-durability gap (#2) as a requirement
    for whatever eventually calls it live: ``gate_sole_executor``'s read-only
    broker-history check,
    even rerun here, cannot by itself make two concurrent executor instances
    mutually exclusive (that needs an atomic run/slot lease this module has
    no mechanism to provide) -- it closes the "history already shows a
    collision" case, not a true race between two simultaneous submissions.

    ``sole_executor_grace_period_active`` mirrors ``assess()``'s own
    same-named Gate 4 argument exactly, including its fail-closed exact-
    ``True`` check and its ``placed_agent == "agentic"`` compatible-executor
    scoping. A caller that received a ``PROCEED`` from ``assess()`` because
    the grace period cleared a compatible collision would otherwise have
    that same collision unconditionally re-rejected here -- this function's
    own Gate 4 rerun (above) previously had no way to know a grace override
    had already been evaluated and accepted, making ``assess()``'s PROCEED
    verdict impossible to actually execute during the grace window (Codex
    review: "Thread the grace-period policy through execute_plan"). Leaving
    this at its default ``False`` reproduces the original always-reject
    behavior for every caller that does not pass it explicitly.
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
        # Rehearse the live path's own validation when the caller supplied
        # everything it needs, so a dry run can surface a plan the live call
        # would refuse (a stale snapshot, a wrong tier, a same-day repeat
        # under one_order_per_ticker_per_day). Reported, never raised: a dry
        # run places nothing, and its callers read the result. A bare dry run
        # without the live-path arguments stays silent as before.
        if (
            trading_day is not None
            and slot_id is not None
            and orders_fetched_at is not None
            and now_fn is not None
        ):
            try:
                _validate_execute_plan(
                    plan,
                    broker,
                    run_id,
                    snapshot,
                    tier,
                    trading_day,
                    orders,
                    weekly_cap_usd,
                    weekly_rollover_reserve_usd,
                    weekly_rollover_reserve_week_key,
                    finalized_budget_days,
                    submitted_notional_by_week,
                    buying_power_usd,
                    orders_account_number,
                    now,
                    slot_id,
                    orders_fetched_at,
                    now_fn,
                    candidates,
                    sole_executor_grace_period_active,
                )
            except ValueError as exc:
                result.validation_failure = str(exc)
        return result
    slot, bought_today = _validate_execute_plan(
        plan,
        broker,
        run_id,
        snapshot,
        tier,
        trading_day,
        orders,
        weekly_cap_usd,
        weekly_rollover_reserve_usd,
        weekly_rollover_reserve_week_key,
        finalized_budget_days,
        submitted_notional_by_week,
        buying_power_usd,
        orders_account_number,
        now,
        slot_id,
        orders_fetched_at,
        now_fn,
        candidates,
        sole_executor_grace_period_active,
    )
    if (
        trading_day is None
        or slot_id is None
        or orders_fetched_at is None
        or now_fn is None
    ):
        raise ValueError("trading_day, slot_id, orders_fetched_at, and now_fn are required")
    # History freshness is proven here, before the first placement, against
    # the fresh clock (the caller-supplied `now` cannot mask a fetch that is
    # already stale) and again before EVERY placement inside the loop: Gate 4
    # only checked the initial snapshot, so a manual order or another
    # executor landing during a slow batch would otherwise let later orders
    # run on arbitrarily stale totals and exceed the caps. A batch that
    # outlives MAX_ORDERS_FETCH_AGE_SECONDS halts with the remainder unplaced,
    # the fail-closed outcome (Copilot review, PR #1394: "Preserve order-
    # history freshness validation throughout placement batches", revisiting
    # the 2026-09-07 diagnostic's C:F7).
    if plan.to_place:
        _fresh_now, fetch_failure = _now_fn_clock_failure(
            now_fn, trading_day, slot, slot_id, orders_fetched_at
        )
        if fetch_failure is not None:
            result.halted_on = _normalize_ticker(plan.to_place[0].ticker)
            result.failure_reason = fetch_failure
            return result
    # Consecutive, not cumulative -- see the breaker at the bottom of the loop.
    consecutive_failures = 0
    for order in plan.to_place:
        ticker = _normalize_ticker(order.ticker)
        placement_now, clock_failure = _now_fn_clock_failure(
            now_fn, trading_day, slot, slot_id, orders_fetched_at
        )
        if clock_failure is not None:
            result.halted_on = ticker
            result.failure_reason = clock_failure
            break
        assert placement_now is not None
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
        # A DISTINCT sentinel, not None: place_fn can legitimately RETURN None
        # (an adapter with no return value), which _placement_failure_reason
        # correctly rejects as "not a dict" -- conflating that with "raised"
        # would skip the live-order classification for a response shape this
        # code cannot read at all. See
        # _placement_response_may_hold_a_live_order's closing note for why a
        # genuine raise is the one case safe to isolate without classifying.
        response: object = _PLACE_FN_RAISED
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
            # _placement_failure_reason() can still copy an ordinary failure
            # RESPONSE's envelope["error"] text verbatim (e.g. "insufficient
            # funds") -- deliberately preserved, since Gate 9 needs that
            # reason (Codex review: "Preserve the placement failure
            # reason") -- but a broker-provided error string can also embed
            # the account number this request was placed against, and every
            # reason recorded here is explicitly sent to alerts and durable
            # run evidence. The one secret this module itself knows for
            # certain could appear -- broker.account_number -- is redacted
            # before persisting, the same way this repo already avoids
            # embedding it in the orders_account_number mismatch error above
            # rather than leaving arbitrary broker text unexamined (Codex
            # review: "Redact returned broker errors before persisting
            # them"). Applied to the PER-TICKER reason too, not only to a
            # halt reason: an isolated failure is now persisted/alerted on
            # exactly the same paths a halt is.
            redacted = _redacted_placement_failure(failure, broker.account_number)
            result.failures.append(PlacementFailure(ticker=ticker, reason=redacted))
            # A failure that may still have left a LIVE order at the broker is
            # not isolatable: the rest of the batch's caps and buying power
            # were validated without it, so continuing would place against
            # arithmetic that is already wrong (Codex review, PR #786: "Halt
            # after responses that may represent accepted orders"). Isolation
            # covers the failures that PROVE nothing was accepted -- which is
            # the reported incident's shape.
            #
            # A RAISE is one of those unprovable cases, and used to be exempt
            # here on the grounds that any order it left behind is for the
            # amount this batch already validated, so the caps still hold.
            # True as far as it goes, and it does not go far enough: an
            # accepted order the caller never learns about is also absent
            # from `submitted` and from everything downstream reads off it,
            # so the run reports the ticker as unbought while real shares
            # exist (Codex review, PR #786: "Halt when the placement call
            # raises ambiguously"). Three things settle it. `place_fn` gives
            # no response, so unlike a returned envelope there is nothing to
            # classify -- a connector that timed out while returning the
            # broker's acceptance is indistinguishable from one that failed
            # before the request left. near_close_dca already fails its run
            # for this same event class once an order has been published,
            # isolating only raises it can PROVE came before the broker
            # boundary -- a distinction this loop cannot make. And Gate 7's
            # prompt instruction now says in as many words that a placement
            # error leaving you unable to establish whether an order landed
            # stops the batch; the engine and the prose that actually runs
            # it must not disagree.
            #
            # This still costs the reported incident nothing: a broker
            # REFUSING a ticker returns a response with no order id, which
            # proves nothing was accepted and isolates exactly as before.
            if response is _PLACE_FN_RAISED or _placement_response_may_hold_a_live_order(
                response
            ):
                # The placement call either raised or returned an ambiguous payload. Either way,
                # we cannot prove whether the request reached the broker or was accepted, so book
                # the worst-case exposure immediately before returning the halted result. Without
                # this reservation, durable state says zero and a later invocation can spend the
                # same daily dollars and attempt slot again while reconciliation is unavailable.
                reservation = BrokerOrder(
                    symbol=ticker,
                    side=order.side,
                    dollar_amount=order.amount_usd,
                    created_at=placement_now,
                    placed_agent="agentic",
                    state="potentially_submitted",
                    client_order_id=ref_id,
                    ref_id=ref_id,
                )
                result.attempt_records.append(reservation)
                if isinstance(orders, list):
                    orders.append(reservation)
                result.halted_on = ticker
                unprovable = (
                    "the placement call raised, so whether an order was accepted cannot be "
                    "established from here"
                    if response is _PLACE_FN_RAISED
                    else "the broker response may still hold a live order"
                )
                result.failure_reason = (
                    f"halted: {ticker}'s placement failed and {unprovable}, so the batch "
                    f"cannot account for what was placed -- {redacted}"
                )
                break
            # Preserve the otherwise history-invisible broker call as a
            # zero-notional terminal order.  ``budget_snapshot`` deliberately
            # excludes it from spend while counting it in day_order_count,
            # which is exactly the daily attempt-cap contract.  Returning the
            # row on ExecutionResult gives artifact writers a durable value to
            # carry into a later slot; appending to a mutable history list also
            # makes an immediate retry with that same snapshot conservative.
            if not _isolated_failure_carries_a_trackable_order_id(response):
                attempt_record = BrokerOrder(
                    symbol=ticker,
                    side=order.side,
                    dollar_amount=0.0,
                    # Use the same freshly validated clock sampled immediately
                    # before this broker call. Slot interlocking classifies order
                    # attempts by created_at, so a synthetic noon timestamp would
                    # hide a 09:35 attempt from its own slot and misattribute it
                    # to the later 11:30 window.
                    created_at=placement_now,
                    state="rejected",
                    executed_dollar_amount=0.0,
                )
                result.attempt_records.append(attempt_record)
                if isinstance(orders, list):
                    orders.append(attempt_record)
            # THE 2026-08-24 POLICY CHANGE. This used to `break`: "a broker
            # refusing one order is evidence the run's assumptions are
            # stale, not an invitation to keep going." In practice the
            # refusals that actually happened were ticker-scoped, not
            # run-scoped -- one symbol that cannot be bought the way the
            # plan assumed (RVII, not tradable for fractional/notional
            # buys) -- and halting there cost every ticker BEHIND it in the
            # plan its buy for the day, with no mechanism to make it up:
            # "GS, EVR: never submitted -- batch halted after RVII
            # rejection", and "34 of 37 planned buys placed and filled, 3
            # tickers not bought". Each remaining order is independently
            # valid -- _validate_execute_plan already proved the WHOLE plan
            # against the caps, the whitelist, buying power, and the
            # rebuilt plan_orders() output before the first placement, and
            # a skipped ticker only ever lowers the total actually spent --
            # so the failure of one tells the next nothing. Isolate it and
            # continue.
            #
            # No retry, still: the failed order is never re-attempted
            # within this run. Isolation changes which OTHER tickets get
            # their chance, not how a failure itself is handled.
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_PLACEMENT_FAILURES:
                # A RUN of failures is the signal the old blanket halt was
                # reaching for and the one case where it was right: the
                # broker leg itself is unhealthy (an outage, an expired
                # session, an account-level block, exhausted buying power),
                # and every further placement is a doomed live call. Counted
                # CONSECUTIVELY, not cumulatively, so a batch that keeps
                # succeeding between isolated bad symbols runs to completion
                # -- which is the whole point of the change above.
                result.halted_on = ticker
                result.failure_reason = (
                    f"halted after {consecutive_failures} consecutive placement failures "
                    f"(most recent -- {ticker}: {redacted}); "
                    f"{len(plan.to_place) - len(result.submitted) - len(result.failures)} "
                    "order(s) never attempted"
                )
                break
            continue
        # A validated primary order is not a promise that it is the ONLY
        # order the response names. `_placement_failure_reason` checks the one
        # order its descent selects; a sibling branch naming a second live
        # order left that order out of this run's cap and buying-power
        # arithmetic and out of `submitted` entirely, while the batch recorded
        # a clean success and carried on (Codex review, PR #786: "Inspect
        # sibling orders before accepting a successful response").
        #
        # Same verdict the failure path gives the same evidence, and for the
        # same reason: the batch cannot account for what it did not count.
        if _accepted_response_names_unaccounted_orders(
            response,
            expected_symbol=ticker,
            expected_side=order.side,
            expected_amount_usd=order.amount_usd,
            expected_ref_id=ref_id,
        ):
            # RECORDED FIRST, then halted. `_placement_failure_reason` has
            # already established that the primary order was accepted and
            # matched this request's ticker, side, ref_id and notional -- it is
            # a real live order at the broker. Breaking before the append left
            # it out of `ExecutionResult.submitted` along with the sibling,
            # so submission metrics and any recovery artifact built from the
            # result understated what actually reached the broker (Codex
            # review, PR #786: "Record the validated primary order before
            # halting"). That is the same class of harm this whole change
            # exists to prevent, produced by the fix for it.
            #
            # `halted_on` and a submission for the same ticker is the honest
            # reading: this order went through, and the batch stopped here
            # because the response described something else as well.
            result.submitted.append(ticker)
            result.halted_on = ticker
            result.failure_reason = (
                f"halted: {ticker}'s own order was accepted and is recorded, but its response "
                "names orders beyond the one validated, so the batch cannot account for what "
                "else was placed"
            )
            break
        # Reset only on an actual success: the breaker is about an unbroken
        # RUN of failures, and a submitted order in between is proof the
        # broker leg is still working.
        consecutive_failures = 0
        result.submitted.append(ticker)
    return result


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


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
    sole_executor_grace_period_active: bool = False,
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
    PRODUCTION_READINESS.md's 7-day deployment kill-switch gap)
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
        collision_symbols = [
            o.symbol if isinstance(o.symbol, str)
            else f"<non-string symbol, type={type(o.symbol).__name__}>"
            for o in collisions
        ]
        # The temporary grace period recognizes only the executor provenance
        # it was introduced for.  Missing, manual, and otherwise unknown
        # provenance must continue to fail closed; in particular, one rogue
        # row alongside an ``agentic`` row cannot be hidden by the compatible
        # row.  Classify the complete window before deciding so result order
        # cannot affect the interlock.
        rogue_collisions = [o for o in collisions if o.placed_agent != "agentic"]
        # ``sole_executor_grace_period_active`` is an external scheduler/
        # tool-calling surface: a caller could pass the string "false" or the
        # integer 1 rather than a real boolean, and plain truthiness would
        # treat both as "grace active", incorrectly bypassing the Gate 4
        # safety interlock. Require the exact literal ``True`` so anything
        # else -- including truthy non-bool values -- fails closed (Codex
        # review: "Require an exact boolean for the grace-period flag").
        grace_active = sole_executor_grace_period_active is True
        if not grace_active or rogue_collisions:
            return out(
                "ABORT",
                f"orders already present in the {slot_id} ET window, a second executor may be live",
                colliding_orders=collision_symbols,
            )
        # Grace period is active: let the run proceed to Gate 6 rather than
        # aborting, but always record the collision evidence so run artifacts
        # show a grace-period override was applied (caller-supplied flag,
        # mirroring buying_power_usd -- assess() never reads config to decide
        # whether the window is still active, the caller does that check).
        _gate4_grace_extra: dict = {
            "gate4_grace_period_applied": True,
            "gate4_collisions": collision_symbols,
        }
        # The compatible collision tickers must not also be planned again by
        # Gate 6: config/budgets.json's one_order_per_ticker_per_day is False
        # by default, so plan_orders() would otherwise happily plan a SECOND
        # same-window order for a ticker the grace-compatible executor
        # already bought moments ago (Codex review: "Exclude grace-period
        # collision tickers from the candidate set"). Only tickers that
        # actually collided are excluded; every other candidate is untouched.
        _gate4_grace_collision_tickers = {
            o.symbol for o in collisions if isinstance(o.symbol, str)
        }
    else:
        _gate4_grace_extra = {}
        _gate4_grace_collision_tickers = set()

    def out_with_grace(verdict: str, reason: str, **extra) -> dict:
        # Every verdict returned from here on (Gate 6's various ABORTs as
        # well as the final PROCEED) must carry the grace-period evidence
        # collected above, not just the PROCEED path -- otherwise a Gate 6
        # ABORT (unavailable buying power, a malformed budget snapshot,
        # invalid candidates, bought-today, ...) silently drops the fact
        # that Gate 4 already found and overrode a collision, leaving
        # persisted run artifacts unable to show a grace override applied
        # (Codex review: "Attach grace-period evidence to every verdict
        # after Gate 4, not just PROCEED").
        merged = dict(_gate4_grace_extra)
        merged.update(extra)
        return out(verdict, reason, **merged)

    # Gate 6 -- budget from broker history
    buying_power_error = _check_buying_power(buying_power_usd)
    if buying_power_error is not None:
        return out_with_grace("ABORT", buying_power_error)

    snapshot, snapshot_error = _build_budget_snapshot(orders, trading_day)
    if snapshot_error is not None:
        return out_with_grace("ABORT", snapshot_error)
    if snapshot is None:
        return out_with_grace("ABORT", "budget snapshot could not be built")

    cands, cands_error = _parse_and_validate_candidates(candidates)
    if cands_error is not None:
        return out_with_grace("ABORT", cands_error)
    if cands is None:
        return out_with_grace("ABORT", "candidate rows could not be parsed")

    bought_today, bought_today_error = _get_bought_today_tickers(orders, trading_day)
    if bought_today_error is not None:
        return out_with_grace("ABORT", bought_today_error)
    if bought_today is None:
        return out_with_grace("ABORT", "broker order history could not be evaluated")
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

    # See the grace-period collision-exclusion comment above Gate 6: a
    # compatible collision ticker must not be planned again this call.
    if _gate4_grace_collision_tickers:
        fresh_cands = [c for c in fresh_cands if c.ticker not in _gate4_grace_collision_tickers]

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

    return out_with_grace(
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
#
# Bumped 1 -> 2 when recorded_attempt_order_ids was added: once ANY process
# running this version saves state, every future write always includes that
# key (see _state_to_json), so a syntactically valid schema_version=2
# document that has LOST just that one key is corrupt, not merely an older
# file -- silently defaulting it to {} would make an unrelated known
# same-ticker order anonymous, letting it mask a different reconciliation's
# lost attempt charge (Codex review, PR #1164/#1165: "Require the
# provenance key after migrating durable state"). A genuinely older
# schema_version=1 file, which never had this key AT ALL, is not that case
# -- see _LEGACY_STATE_REQUIRED_KEYS and its own use in load_strategy_state
# for the one-way migration path that still accepts it.
# Bumped 2 -> 3 when notional_applied_order_ids was added (Codex review, PR
# #1251: "Preserve the first notional top-up for recorded attempts") -- the
# identical reasoning as the 1 -> 2 bump above, one field later: a
# syntactically valid schema_version=3 document missing this key is
# corrupt, not merely an older file. See _SCHEMA_2_STATE_REQUIRED_KEYS and
# its own use in load_strategy_state for the one-way migration path that
# still accepts a genuine schema_version=2 document (which never had this
# key at all).
STATE_SCHEMA_VERSION = 3

# Every key save_strategy_state always writes AT THE CURRENT SCHEMA VERSION.
# A document missing schema_version or any of these -- even if it is
# syntactically valid JSON -- is rejected rather than filled in with
# defaults.
_STATE_REQUIRED_KEYS = frozenset(
    {
        "buys_by_ticker_by_day",
        "submitted_notional_by_day",
        "submitted_notional_by_week",
        "weekly_rollover_reserve_usd",
        "finalized_budget_days",
        "reserve_week_key",
        "selected_tier",
        # save_strategy_state has always emitted this key since fractional-
        # reconciliation durability was introduced, but it was never added
        # here, so a document that is valid JSON, carries the current
        # schema_version, and is missing only this one field (a rollback
        # to an older binary, or any other partial loss) loaded as an EMPTY
        # idempotency ledger instead of being rejected. A retry against such
        # a document would then re-book notional/attempts the ledger already
        # proved were durable (Codex review: "Require the new reconciliation
        # ledger in persisted state").
        "reconciled_fractional_placement_refs",
        "recorded_attempt_order_ids",
        # Required alongside recorded_attempt_order_ids for the identical
        # reason: save_strategy_state always serializes it too (see
        # _state_to_json), distinguishing "an attempt was recorded for
        # this id" from "this id's REAL notional was applied" -- a
        # document missing it, even while syntactically valid, would
        # silently reload as "nothing applied yet" for every id and make
        # book_reconciled_attempt_notional's own first-top-up guard fire
        # again for an id whose notional was already durably applied,
        # double-crediting persisted spend (Codex review, PR #1251:
        # "Preserve the first notional top-up for recorded attempts").
        "notional_applied_order_ids",
        # Both required alongside recorded_attempt_order_ids, not merely
        # optional bookkeeping: save_strategy_state always serializes both
        # (see _state_to_json), so a document missing either -- even while
        # syntactically valid -- silently reloaded it as `{}` and let a
        # later reconcile_prior_allocation() call compute the ID-backfill
        # room from an amputated history, potentially masking a distinct
        # order's own attempt the same way the un-required recorded_
        # attempt_order_ids field itself used to (Codex review, PR #1168:
        # "Require the new seeded-provenance counters").
        "seeded_prior_context_proven_attempts_by_ticker_day",
        "seeded_prior_context_registered_id_count_by_ticker_day",
        # Every OTHER serializer-written accounting field `_state_to_json`
        # unconditionally emits at schema_version=2, not just the ID-
        # provenance trio above: a document missing one of THESE -- e.g.
        # `earlier_week_cross_stream_seeded_usd` -- previously still
        # passed this required-key check and loaded as an empty/default
        # ledger for that field. The very next scan then re-folded the
        # full earlier-week total into weekly notional again (or the
        # equivalent double-count for whichever field was lost), since a
        # genuine schema_version=2 writer (this same module) always
        # includes all of them -- their total absence is corruption, not
        # a legacy omission the way it is for `_LEGACY_STATE_REQUIRED_
        # KEYS` below (Codex review, PR #1168/#1169: "Require persisted
        # allocation baselines in schema 2" / "Require every schema-2
        # bookkeeping field" -- two independently-authored fixes for the
        # identical class of gap).
        "submitted_order_count_by_day",
        "earlier_week_cross_stream_seeded_usd",
        "earlier_week_cross_stream_poison_usd",
        "seeded_prior_context_amount_by_ticker_day",
        "seeded_prior_context_attempts_by_ticker_day",
    }
)

# The required-key set for a genuinely OLDER schema_version=2 document,
# written before notional_applied_order_ids existed -- a one-way migration
# path, not a standing dual-schema contract: load_strategy_state accepts
# this OR _STATE_REQUIRED_KEYS (gated on the matching schema_version), but
# save_strategy_state only ever writes STATE_SCHEMA_VERSION/
# _STATE_REQUIRED_KEYS going forward, so every process that saves state
# upgrades it to schema_version=3 for good.
#
# Held as its OWN fixed literal set (everything _STATE_REQUIRED_KEYS has
# EXCEPT notional_applied_order_ids), not derived from it by subtraction,
# for the identical reason _LEGACY_STATE_REQUIRED_KEYS below is its own
# fixed set: a future field added to _STATE_REQUIRED_KEYS should not
# silently also become optional for schema_version=2 documents just
# because it was appended after this migration boundary.
_SCHEMA_2_STATE_REQUIRED_KEYS = frozenset(
    {
        "buys_by_ticker_by_day",
        "submitted_notional_by_day",
        "submitted_notional_by_week",
        "weekly_rollover_reserve_usd",
        "finalized_budget_days",
        "reserve_week_key",
        "selected_tier",
        "recorded_attempt_order_ids",
        "seeded_prior_context_proven_attempts_by_ticker_day",
        "seeded_prior_context_registered_id_count_by_ticker_day",
        "submitted_order_count_by_day",
        "earlier_week_cross_stream_seeded_usd",
        "earlier_week_cross_stream_poison_usd",
        "seeded_prior_context_amount_by_ticker_day",
        "seeded_prior_context_attempts_by_ticker_day",
    }
)

# The required-key set for a genuinely OLDER schema_version=1 document,
# written before recorded_attempt_order_ids (and the two seeded-provenance
# counters added alongside it) existed at all -- one-way migration
# support, not a standing dual-schema contract: load_strategy_state
# accepts either this OR _STATE_REQUIRED_KEYS (gated on the matching
# schema_version), but save_strategy_state only ever writes
# STATE_SCHEMA_VERSION/_STATE_REQUIRED_KEYS going forward, so every process
# that saves state upgrades it to schema_version=2 for good.
#
# Held as its OWN fixed literal set, not derived from _STATE_REQUIRED_KEYS
# by subtraction: a genuine schema_version=1 file predates every field
# added since that original migration boundary, not just the ID-
# provenance trio -- deriving this set from the (now-larger)
# _STATE_REQUIRED_KEYS above would also demand `submitted_order_count_
# by_day`/`earlier_week_cross_stream_*`/`seeded_prior_context_amount_
# and_attempts_by_ticker_day` from a document that never had them either,
# breaking the one-way migration path for the exact legacy files it
# exists to accept.
_LEGACY_STATE_REQUIRED_KEYS = frozenset(
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



def _nest_by_ticker_day(flat: dict) -> dict[str, dict[date, set]]:
    """Reshape a ``{(ticker, day): set}`` mapping into ``{ticker: {day: set}}``.

    ``StrategyState.own_recovered_attempt_run_ids`` is keyed by the bare
    ``(ticker, trading_day)`` tuple in memory (matching every other
    ticker/day ledger this module persists as a nested dict), so this mirrors
    the same reshape ``_state_to_json`` already applies to
    ``buys_by_ticker_by_day``/``seeded_prior_context_amount_by_ticker_day``.
    """
    result: dict[str, dict[date, set]] = {}
    for (ticker, trading_day), value in flat.items():
        result.setdefault(ticker, {})[trading_day] = value
    return result


def _nest_by_ticker_day_run_id(flat: dict) -> dict[str, dict[date, dict[str, Any]]]:
    """Reshape a ``{(ticker, day, run_id): value}`` mapping into nested form.

    Mirrors ``_nest_by_ticker_day`` one dimension deeper for
    ``own_recovered_amount_by_ticker_day_run_id``/
    ``own_recovered_attempts_by_ticker_day_run_id``, which additionally key on
    the recovered source run's id.
    """
    result: dict[str, dict[date, dict[str, Any]]] = {}
    for (ticker, trading_day, run_id), value in flat.items():
        result.setdefault(ticker, {}).setdefault(trading_day, {})[run_id] = value
    return result


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


def _nonneg_int(value: object, field: str) -> int:
    """Reject a non-integer or negative persisted count, but ALLOW zero.

    Unlike :func:`_positive_int`, a persisted ``0`` here is a genuine,
    legitimate value, not corruption: ``StrategyState.reconcile_prior_
    allocation`` persists ``seeded_prior_context_proven_attempts_by_ticker_
    day`` UNCONDITIONALLY on every call, even one whose own ``attempt_
    delta`` is 0 -- e.g. the very first reconciliation call for a ticker/
    day, where a scan exactly matches the existing ``buys_today()``
    baseline (see that method's own "Persisted UNCONDITIONALLY" comment for
    why: recording the observed baseline even when nothing changed keeps
    the NEXT call out of the ambiguous branch). Loading that serializer-
    produced ``0`` back through `_positive_int` rejected it as corrupt,
    failing ``save_strategy_state``'s own pre-write validation -- potentially
    after the run had already placed another broker order (Codex review, PR
    #1169: "Allow zero proven-attempt baselines to round-trip").
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer, got {value!r}")
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

    Shared by ``reconcile_prior_allocation``'s bookkeeping fields
    (``seeded_prior_context_amount_by_ticker_day``/``..._attempts_by_ticker_day``/
    ``..._proven_attempts_by_ticker_day``/``..._registered_id_count_by_ticker_day``),
    which mirror ``buys_by_ticker_by_day``'s own nested shape.

    Ticker keys are normalized via :func:`_normalize_ticker`, the same as
    ``buys_by_ticker_by_day``/``recorded_attempt_order_ids``: several of
    these fields are cross-validated DIRECTLY against those normalized
    ledgers a few lines below (``seeded_prior_context_attempts_by_ticker_day``
    against ``buys_by_ticker_by_day``, in particular). A persisted
    noncanonical spelling (``reconcile_prior_allocation`` called with
    ``"aapl"``) left this field's own key mismatched against that
    normalized ledger -- looking up ``"aapl"`` in a ``buys_by_ticker_by_day``
    that only has ``"AAPL"`` found nothing, so ``save_strategy_state``'s
    own load-back validation rejected a perfectly legitimate in-memory
    state as corrupt (Codex review, PR #1168: "Normalize seeded-provenance
    ticker keys before validation"). A second raw spelling normalizing to
    the same ticker is rejected outright, the same as
    ``recorded_attempt_order_ids``'s own loader -- which of the two
    per-day dicts is authoritative is not this loader's call to make.
    """
    result: dict[str, dict[date, Any]] = {}
    spellings: dict[str, str] = {}
    for ticker, per_day in raw.items():
        normalized_ticker = _normalize_ticker(ticker)
        if normalized_ticker in spellings and spellings[normalized_ticker] != ticker:
            raise ValueError(
                f"{field} has multiple spellings ({spellings[normalized_ticker]!r} and "
                f"{ticker!r}) normalizing to the same ticker {normalized_ticker!r}"
            )
        spellings[normalized_ticker] = ticker
        result[normalized_ticker] = _dedup_date_keyed(
            per_day.items(), lambda v: parse_value(v, field)
        )
    return result


def _group_order_ids_by_ticker(
    by_ticker_day: dict[tuple[str, date], set[str]],
) -> dict[str, dict[str, list[str]]]:
    """Reshape a ``{(ticker, day): {order_id, ...}}`` mapping into the nested
    ``{ticker: {date_str: [order_id, ...]}}`` JSON shape every other
    per-ticker/day field here uses (e.g.
    ``seeded_prior_context_amount_by_ticker_day``), for
    ``StrategyState.recorded_attempt_order_ids``.
    """
    result: dict[str, dict[str, list[str]]] = {}
    for (ticker, trading_day), ids in by_ticker_day.items():
        result.setdefault(ticker, {})[trading_day.isoformat()] = sorted(ids)
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
        "reconciled_fractional_placement_refs": sorted(
            state.reconciled_fractional_placement_refs
        ),
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
        # Per-source-run recovery baseline that book_reconciled_attempt_notional()
        # writes and _seed_prior_allocations() reads to scope a prior-allocation
        # aggregate's baseline to only the recovered runs it actually overlaps.
        # Previously omitted here (treated like the deliberately-not-persisted
        # own_orders_recorded/own_recovered_order_ids, which only guard
        # in-process retries) -- but unlike those two, this bookkeeping drives a
        # CROSS-RUN ledger: if state_transaction() persists a partial recovery
        # (one unresolved reference group resolved, a later one raised) and this
        # baseline is lost on reload, a later invocation that resolves another
        # group from the SAME source run re-adds the already-persisted amount
        # and attempt from that run's full aggregate (Codex review: "Persist
        # recovery baselines across partial reconciliation failures").
        "own_recovered_attempt_run_ids": {
            ticker: {
                d.isoformat(): sorted(run_ids)
                for d, run_ids in per_day.items()
            }
            for ticker, per_day in _nest_by_ticker_day(
                state.own_recovered_attempt_run_ids
            ).items()
        },
        "own_recovered_amount_by_ticker_day_run_id": {
            ticker: {
                d.isoformat(): {run_id: v for run_id, v in per_run.items()}
                for d, per_run in per_day.items()
            }
            for ticker, per_day in _nest_by_ticker_day_run_id(
                state.own_recovered_amount_by_ticker_day_run_id
            ).items()
        },
        "own_recovered_attempts_by_ticker_day_run_id": {
            ticker: {
                d.isoformat(): {run_id: v for run_id, v in per_run.items()}
                for d, per_run in per_day.items()
            }
            for ticker, per_day in _nest_by_ticker_day_run_id(
                state.own_recovered_attempts_by_ticker_day_run_id
            ).items()
        },
        # Both persisted the same nested ticker -> {day: value} way as
        # seeded_prior_context_attempts_by_ticker_day, for the identical
        # process-restart reason -- see StrategyState.seeded_prior_context_
        # registered_id_count_by_ticker_day/..._proven_attempts_by_ticker_
        # day's own docstrings for what each tracks and why
        # reconcile_prior_allocation's ID-backfill room needs them kept
        # separate from the (possibly ambiguity-absorbed)
        # seeded_prior_context_attempts_by_ticker_day baseline above (Codex
        # review, PR #1168: "Restrict ID backfills to seeded anonymous
        # attempts").
        "seeded_prior_context_registered_id_count_by_ticker_day": {
            ticker: {d.isoformat(): v for d, v in per_day.items()}
            for ticker, per_day in state.seeded_prior_context_registered_id_count_by_ticker_day.items()
        },
        "seeded_prior_context_proven_attempts_by_ticker_day": {
            ticker: {d.isoformat(): v for d, v in per_day.items()}
            for ticker, per_day in state.seeded_prior_context_proven_attempts_by_ticker_day.items()
        },
        # Durable per-order idempotency marker for
        # book_reconciled_attempt_notional (Codex review on PR #1164/#1165:
        # "Restore recovered attempts after unrelated same-ticker buys") --
        # see StrategyState.recorded_attempt_order_ids' own docstring. Grouped
        # by ticker/day the same way seeded_prior_context_amount_by_ticker_day
        # is, even though the in-memory field is keyed by the bare
        # (ticker, trading_day) tuple, for the same JSON-shape reasons.
        # Order preserved via sorted() so the on-disk file is deterministic.
        "recorded_attempt_order_ids": _group_order_ids_by_ticker(state.recorded_attempt_order_ids),
        # Durable per-order marker distinguishing "an attempt was recorded
        # for this id" from "this id's REAL notional was applied" -- see
        # StrategyState.notional_applied_order_ids' own docstring (Codex
        # review, PR #1251: "Preserve the first notional top-up for
        # recorded attempts"). Grouped the same way recorded_attempt_
        # order_ids is, via the same generic reshape helper.
        "notional_applied_order_ids": _group_order_ids_by_ticker(state.notional_applied_order_ids),
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
    _reconciliation_refs = raw.get("reconciled_fractional_placement_refs", [])
    if not isinstance(_reconciliation_refs, list) or not all(
        isinstance(ref, str) and ref for ref in _reconciliation_refs
    ):
        raise ValueError("reconciled_fractional_placement_refs must be a list of non-empty strings")
    state.reconciled_fractional_placement_refs = set(_reconciliation_refs)
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
    # force_week_notional_to_worst_case_ceiling only ever injects a poison
    # delta into submitted_notional_by_week and the identical delta into
    # earlier_week_cross_stream_poison_usd IN THE SAME CALL -- so a
    # persisted poison total can never exceed the weekly bucket it was
    # folded into. But the bucket itself can ALSO carry real, proven spend
    # from this state's own submitted_notional_by_day (already summed into
    # expected_week_totals above) -- poison is only ever the delta added ON
    # TOP of whatever was already there, so the correct ceiling is the
    # portion of the bucket NOT already proven by daily entries, not the
    # whole bucket. Comparing against the whole bucket lets a week with
    # $100 proven daily spend and a $100 bucket carry a full $100 of
    # "poison" too: reconcile_earlier_week_cross_stream_notional() then
    # subtracts that poison back out (zero-clamped) on the next successful
    # scan, erasing the $100 of REAL proven spend right along with it and
    # reopening weekly headroom that was never actually available (Codex
    # review, PR #1168: "Keep poison amounts above proven weekly spend").
    for wk, poisoned in state.earlier_week_cross_stream_poison_usd.items():
        bucket = state.submitted_notional_by_week.get(wk, 0.0)
        proven_daily_total = expected_week_totals.get(wk, 0.0)
        headroom_above_proven = max(0.0, bucket - proven_daily_total)
        if _cents(poisoned) > _cents(headroom_above_proven):
            raise ValueError(
                f"earlier_week_cross_stream_poison_usd for {wk!r} is {poisoned!r} but "
                f"submitted_notional_by_week has only {bucket!r} for that ISO week, of "
                f"which {proven_daily_total!r} is already proven by submitted_notional_"
                "by_day; the poisoned total can never exceed the portion of the weekly "
                "bucket that isn't already accounted for by proven daily spend"
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
        # `_nonneg_int`, not `_positive_int`: note_prior_allocation_baseline_
        # is_disjoint() deliberately writes a genuine `0` here for a
        # current-day crash lease, matching the sibling seeded_prior_context_
        # proven_attempts_by_ticker_day field below, which already uses
        # `_nonneg_int` for the identical reason (Codex review, PR #1168:
        # "Allow zero attempt baselines to round-trip"). `_positive_int`
        # rejected that legitimate `0` outright, raising StateCorruptError
        # and preventing the recovered broker order's attempt/notional from
        # ever being durably saved.
        parse_value=lambda v, field: _nonneg_int(v, field),
    )
    # Both bookkeeping-only, same absent-key handling as the seeded_prior_
    # context_* fields above -- see StrategyState.seeded_prior_context_
    # registered_id_count_by_ticker_day/..._proven_attempts_by_ticker_day's
    # own docstrings for what each tracks (Codex review, PR #1168:
    # "Restrict ID backfills to seeded anonymous attempts").
    state.seeded_prior_context_registered_id_count_by_ticker_day = _load_ticker_day_keyed_nonneg(
        raw.get("seeded_prior_context_registered_id_count_by_ticker_day", {}),
        "seeded_prior_context_registered_id_count_by_ticker_day",
        parse_value=lambda v, field: _positive_int(v, field),
    )
    state.seeded_prior_context_proven_attempts_by_ticker_day = _load_ticker_day_keyed_nonneg(
        raw.get("seeded_prior_context_proven_attempts_by_ticker_day", {}),
        "seeded_prior_context_proven_attempts_by_ticker_day",
        # `_nonneg_int`, not `_positive_int`: see `_nonneg_int`'s own
        # docstring -- this field is persisted UNCONDITIONALLY, including a
        # genuine `0` baseline, unlike every other `_positive_int`-guarded
        # ticker/day field here (Codex review, PR #1169: "Allow zero
        # proven-attempt baselines to round-trip").
        parse_value=lambda v, field: _nonneg_int(v, field),
    )
    # Synthesized for a document below the CURRENT schema version -- at
    # minimum a genuinely legacy schema_version=1 one, which predates
    # seeded_prior_context_proven_attempts_by_ticker_day entirely, so its
    # parent serializer already wrote nonzero seeded_prior_context_
    # attempts_by_ticker_day entries with no way to have ever written the
    # matching provenance field. Loading it successfully (the "Exempt
    # legacy states from the new provenance requirement" fix above) is not
    # enough on its own: `_state_to_json` always serializes at the CURRENT
    # schema version, so this exact in-memory state's very NEXT
    # `save_strategy_state()` round-trips through the nonzero-baseline
    # provenance check as a current-schema document -- one that still
    # carries no provenance entry -- and gets rejected as corrupt,
    # permanently blocking the documented load/mutate/save flow from ever
    # migrating this legacy state or persisting any new order accounting
    # (Codex review, PR #1168: "Make legacy allocation baselines writable
    # after migration"). Backfilled to the FULL tracked total, not zero:
    # that is the conservative direction the sibling ID-backfill invariant
    # already requires -- treating every legacy attempt as already proven
    # leaves zero backfill room for a NEW id to be registered against a
    # baseline this loader cannot actually prove is anonymous, the same
    # "restrict backfills to seeded anonymous attempts" reasoning already
    # governs this field's own write path. Purely additive (only fills a
    # missing (ticker, day) entry, never overwrites one already present),
    # Scoped to schema_version=1 SPECIFICALLY, not merely "not current":
    # a schema_version=2 document is NOT exempt from the sibling invariant
    # a few dozen lines below (a nonzero tracked-attempts entry must have
    # a matching proven-attempts entry) -- that field has existed since
    # schema_version=2, so a v2 document missing an entry for it is
    # corruption, the exact case
    # test_state_with_a_nonzero_tracked_baseline_missing_its_proven_
    # attempts_entry_fails_closed exists to catch. Backfilling it HERE
    # first, unconditionally for anything below current, would silently
    # fill the gap before that later invariant ever gets a chance to see
    # it missing -- defeating the check for every schema_version=2
    # document the moment schema_version=3 shipped, an unrelated field
    # bump (Codex review, PR #1251: "Preserve the first notional top-up
    # for recorded attempts").
    _raw_schema_version_for_legacy_migration = raw.get("schema_version")
    _is_legacy_schema_version_for_migration = (
        isinstance(_raw_schema_version_for_legacy_migration, int)
        and not isinstance(_raw_schema_version_for_legacy_migration, bool)
        and _raw_schema_version_for_legacy_migration == 1
    )
    if _is_legacy_schema_version_for_migration:
        for _ticker, _per_day in state.seeded_prior_context_attempts_by_ticker_day.items():
            for _d, _tracked_attempts in _per_day.items():
                if _tracked_attempts <= 0:
                    continue
                _proven_per_day = state.seeded_prior_context_proven_attempts_by_ticker_day.setdefault(
                    _ticker, {}
                )
                if _d not in _proven_per_day:
                    _proven_per_day[_d] = _tracked_attempts
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
    # reconcile_prior_allocation() only ever writes these two ticker/day
    # maps TOGETHER, in the same call, for the same key set -- so a
    # ticker/day present in one but not the other is impossible from any
    # legitimate write path. Loading them independently let a document
    # missing only an attempts entry (whose proven-attempt counter
    # happens to be zero) pass every check above: the surviving amount
    # baseline then has no matching attempts entry, so the next scan's
    # `tracked_attempts` lookup returns `None` for that ticker/day and the
    # day-level fallback treats the amount as unclaimed, letting it absorb
    # a DIFFERENT ticker's dollars into the same day bucket and permit
    # spend beyond the daily cap (Codex review, PR #1169: "Require
    # matching seeded-allocation key sets").
    _seeded_amount_keys = {
        (ticker, d) for ticker, per_day in state.seeded_prior_context_amount_by_ticker_day.items() for d in per_day
    }
    _seeded_attempt_keys = {
        (ticker, d) for ticker, per_day in state.seeded_prior_context_attempts_by_ticker_day.items() for d in per_day
    }
    if _seeded_amount_keys != _seeded_attempt_keys:
        raise ValueError(
            "seeded_prior_context_amount_by_ticker_day and seeded_prior_context_"
            "attempts_by_ticker_day must cover the identical (ticker, day) keys; "
            f"amount-only keys: {sorted(_seeded_amount_keys - _seeded_attempt_keys)!r}, "
            f"attempts-only keys: {sorted(_seeded_attempt_keys - _seeded_amount_keys)!r}"
        )
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
    # Per-source-run recovery baseline (Codex review: "Persist recovery
    # baselines across partial reconciliation failures") -- see this field's
    # write-side comment in _state_to_json for why this, unlike
    # own_orders_recorded/own_recovered_order_ids, must survive a reload.
    state.own_recovered_attempt_run_ids = {}
    for ticker, per_day in raw.get("own_recovered_attempt_run_ids", {}).items():
        for d, run_ids in _dedup_date_keyed(per_day.items(), lambda v: v).items():
            if not isinstance(run_ids, list) or not all(isinstance(r, str) for r in run_ids):
                raise ValueError(
                    f"own_recovered_attempt_run_ids for {ticker!r} on {d.isoformat()!r} "
                    "must be a list of strings"
                )
            state.own_recovered_attempt_run_ids[(ticker, d)] = set(run_ids)
    state.own_recovered_amount_by_ticker_day_run_id = {}
    for ticker, per_day in raw.get("own_recovered_amount_by_ticker_day_run_id", {}).items():
        for d, per_run in _dedup_date_keyed(per_day.items(), lambda v: v).items():
            for run_id, amount in per_run.items():
                state.own_recovered_amount_by_ticker_day_run_id[(ticker, d, run_id)] = _finite_nonneg(
                    _json_number(amount, "own_recovered_amount_by_ticker_day_run_id"),
                    "own_recovered_amount_by_ticker_day_run_id",
                )
    state.own_recovered_attempts_by_ticker_day_run_id = {}
    for ticker, per_day in raw.get("own_recovered_attempts_by_ticker_day_run_id", {}).items():
        for d, per_run in _dedup_date_keyed(per_day.items(), lambda v: v).items():
            for run_id, count in per_run.items():
                state.own_recovered_attempts_by_ticker_day_run_id[(ticker, d, run_id)] = _positive_int(
                    count, "own_recovered_attempts_by_ticker_day_run_id"
                )
    # A recovered amount/attempt entry can never exist for a run_id absent
    # from own_recovered_attempt_run_ids -- that set is the only record of
    # which runs this ticker/day's baseline is scoped to, and
    # _seed_prior_allocations intersects a prior allocation's attempt_run_ids
    # against it. A dangling amount/attempts entry with no matching run-id
    # membership could never have been produced by
    # book_reconciled_attempt_notional (which always adds to all three
    # together) and would silently vanish from every future overlap
    # computation while its dollars still counted toward the day/week ledger.
    for ticker, trading_day, run_id in state.own_recovered_amount_by_ticker_day_run_id:
        if run_id not in state.own_recovered_attempt_run_ids.get((ticker, trading_day), set()):
            raise ValueError(
                f"own_recovered_amount_by_ticker_day_run_id has an entry for "
                f"{ticker!r}/{trading_day.isoformat()!r}/{run_id!r} not present in "
                "own_recovered_attempt_run_ids for that ticker/day; a recovered amount "
                "can never exist for a run_id the baseline does not know about"
            )
    for ticker, trading_day, run_id in state.own_recovered_attempts_by_ticker_day_run_id:
        if run_id not in state.own_recovered_attempt_run_ids.get((ticker, trading_day), set()):
            raise ValueError(
                f"own_recovered_attempts_by_ticker_day_run_id has an entry for "
                f"{ticker!r}/{trading_day.isoformat()!r}/{run_id!r} not present in "
                "own_recovered_attempt_run_ids for that ticker/day; a recovered attempt "
                "can never exist for a run_id the baseline does not know about"
            )
    # seeded_prior_context_proven_attempts_by_ticker_day only ever
    # accumulates by attempt_delta -- a strict SUBSET of the same ticker/
    # day's full (possibly ambiguity-absorbed) seeded_prior_context_
    # attempts_by_ticker_day total -- so it can never exceed that sibling
    # field. And seeded_prior_context_registered_id_count_by_ticker_day
    # only ever grows by ids reconcile_prior_allocation itself proved fit
    # within the proven-attempts room at write time, so it can never exceed
    # proven_attempts either. A persisted value violating either bound is
    # impossible from any legitimate write path (Codex review, PR #1168:
    # "Restrict ID backfills to seeded anonymous attempts" -- the same
    # fail-closed reasoning as the seeded-bookkeeping checks above, applied
    # to this pair).
    for ticker, per_day in state.seeded_prior_context_proven_attempts_by_ticker_day.items():
        for d, proven_attempts in per_day.items():
            tracked_attempts = state.seeded_prior_context_attempts_by_ticker_day.get(ticker, {}).get(d, 0)
            if proven_attempts > tracked_attempts:
                raise ValueError(
                    f"seeded_prior_context_proven_attempts_by_ticker_day for {ticker!r} on "
                    f"{d.isoformat()!r} is {proven_attempts!r} but seeded_prior_context_"
                    f"attempts_by_ticker_day has only {tracked_attempts!r} for that "
                    "ticker/date; proven attempts can never exceed the tracked total they "
                    "were folded into"
                )
    # The REVERSE direction is checked too: reconcile_prior_allocation()
    # writes seeded_prior_context_proven_attempts_by_ticker_day
    # UNCONDITIONALLY on every call that reaches seed_prior_allocation() --
    # the same call that gives seeded_prior_context_attempts_by_ticker_day
    # its first NONZERO entry for a (ticker, day) -- so a nonzero tracked
    # attempts entry with no matching proven-attempts entry at all is
    # impossible from that write path. The one legitimate way attempts_by_
    # ticker_day gets an entry WITHOUT touching proven_attempts_by_ticker_
    # day is note_prior_allocation_baseline_is_disjoint()'s own `setdefault
    # (trading_day, 0)` for a current-day crash-lease recovery -- which
    # only ever writes a ZERO, never a nonzero value -- so this check is
    # scoped to nonzero entries only, leaving that legitimate zero-baseline
    # shape untouched. A missing provenance entry for a genuine nonzero
    # baseline silently defaulted to 0 via a bare `.get(d, 0)` lookup, so a
    # later same-count scan reading `tracked_attempts - 0` room found the
    # seeded order's id had zero backfill room and left the attempt
    # anonymous -- letting a DIFFERENT same-ticker order's own
    # reconciliation mistake that anonymous attempt for its own durable
    # charge and skip restoring its attempt, understating
    # `max_orders_per_day` (Codex review, PR #1168: "Require provenance
    # entries for persisted allocation baselines").
    #
    # Scoped to a document at schema_version 2 OR the current version (3),
    # not a legacy schema_version=1 one: `load_strategy_state()` explicitly
    # supports migrating a genuinely older schema-1 document, written
    # before `seeded_prior_context_proven_attempts_by_ticker_day` existed
    # at all -- its parent serializer already wrote nonzero seeded_prior_
    # context_attempts_by_ticker_day entries with no way to have ever
    # written the newly introduced field. Applying this invariant to that
    # legacy shape rejected every migrating document carrying a prior
    # allocation, halting all scheduled runs until the durable state was
    # manually repaired (Codex review, PR #1168: "Exempt legacy states
    # from the new provenance requirement"). Deliberately NOT narrowed to
    # `== STATE_SCHEMA_VERSION` alone: `seeded_prior_context_proven_
    # attempts_by_ticker_day` has existed since schema_version=2 (only
    # `notional_applied_order_ids`, unrelated to this field, was added at
    # 3), so a genuine schema_version=2 document is exactly as obligated
    # to satisfy this invariant as a current one -- treating it as exempt
    # merely because a LATER, unrelated field bumped the version again
    # would silently stop enforcing this check for every schema_version=2
    # document once schema_version=3 shipped (Codex review, PR #1251:
    # "Preserve the first notional top-up for recorded attempts").
    _raw_schema_version = raw.get("schema_version")
    _is_schema_2_or_current_version = (
        isinstance(_raw_schema_version, int)
        and not isinstance(_raw_schema_version, bool)
        and _raw_schema_version in (2, STATE_SCHEMA_VERSION)
    )
    for ticker, per_day in (
        state.seeded_prior_context_attempts_by_ticker_day.items() if _is_schema_2_or_current_version else ()
    ):
        for d, tracked_attempts in per_day.items():
            if tracked_attempts <= 0:
                continue
            if d not in state.seeded_prior_context_proven_attempts_by_ticker_day.get(ticker, {}):
                raise ValueError(
                    f"seeded_prior_context_attempts_by_ticker_day for {ticker!r} on "
                    f"{d.isoformat()!r} is {tracked_attempts!r} but seeded_prior_context_"
                    "proven_attempts_by_ticker_day has no entry for that ticker/date; "
                    "reconcile_prior_allocation() always writes both together for a "
                    "nonzero baseline"
                )
    for ticker, per_day in state.seeded_prior_context_registered_id_count_by_ticker_day.items():
        for d, registered_count in per_day.items():
            proven_attempts = state.seeded_prior_context_proven_attempts_by_ticker_day.get(ticker, {}).get(d, 0)
            if registered_count > proven_attempts:
                raise ValueError(
                    f"seeded_prior_context_registered_id_count_by_ticker_day for {ticker!r} "
                    f"on {d.isoformat()!r} is {registered_count!r} but seeded_prior_context_"
                    f"proven_attempts_by_ticker_day has only {proven_attempts!r} for that "
                    "ticker/date; the registered-id count can never exceed the proven "
                    "attempts it was backfilled into"
                )
    # Durable per-order idempotency marker for book_reconciled_attempt_
    # notional -- see StrategyState.recorded_attempt_order_ids' own
    # docstring. Absent key loads as "nothing known yet", the same as every
    # other bookkeeping-only field above -- a document written before this
    # field existed carries no per-order history, which is no worse than
    # this field's own explicit reload-time fallback to the older aggregate
    # heuristic when it has nothing to say about a given order_id.
    state.recorded_attempt_order_ids = {}
    recorded_attempt_ticker_spellings: dict[str, str] = {}
    for ticker, per_day in raw.get("recorded_attempt_order_ids", {}).items():
        # Normalized the same way `buys_by_ticker_by_day` above (and every
        # runtime lookup, e.g. `buys_today()`) keys off a ticker -- a
        # persisted "aapl" here left the ledger comparison below reading
        # `buys_by_ticker_by_day["AAPL"]`, found no entry, and raised
        # `StateCorruptError` on a document `save_strategy_state` itself
        # had just written moments earlier for an ordinary noncanonical-
        # spelling `record_buy` call, turning a previously loadable state
        # unloadable the instant this field started being populated (Codex
        # review on this PR: "Normalize ticker keys before restoring
        # recorded IDs"). A second raw spelling normalizing to the same
        # ticker is rejected outright, the same as an aliased date key --
        # which of the two id sets is authoritative is not this loader's
        # call to make.
        normalized_ticker = _normalize_ticker(ticker)
        if not normalized_ticker:
            raise ValueError(
                f"recorded_attempt_order_ids key {ticker!r} normalizes to an empty ticker"
            )
        if normalized_ticker in recorded_attempt_ticker_spellings:
            raise ValueError(
                f"recorded_attempt_order_ids keys "
                f"{recorded_attempt_ticker_spellings[normalized_ticker]!r} and {ticker!r} both "
                f"normalize to {normalized_ticker!r}; a duplicate entry is corrupt, not additive"
            )
        recorded_attempt_ticker_spellings[normalized_ticker] = ticker
        # Routed through _dedup_date_keyed, exactly like every other nested
        # ticker -> {date: value} field above, so two textual date spellings
        # that normalize to the SAME date (e.g. "2026-07-28"/"20260728")
        # raise rather than the later one silently overwriting the earlier
        # id set (Codex review on this PR: "Reject aliased dates before
        # overwriting order IDs") -- an overwritten id set could make a
        # corrupt document pass the count invariant just below and later
        # cause book_reconciled_attempt_notional to restore an attempt
        # whose durable marker was actually discarded.
        def _parse_order_ids(ids, _ticker=ticker):
            if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
                raise ValueError(
                    f"recorded_attempt_order_ids[{_ticker!r}] must be a list of non-empty "
                    f"strings, got {ids!r}"
                )
            # Normalized through the exact SAME `_normalized_order_id` helper
            # every runtime path canonicalizes an id through
            # (record_buy()/book_reconciled_attempt_notional()/etc.), not
            # just a bare `.strip()` -- otherwise a persisted `"ORD"` and
            # `" ORD "` load as two DISTINCT set members even though they
            # name the same order, and a persisted NONCANONICAL numeric
            # spelling like `"123.0"` loads verbatim even though every
            # runtime path canonicalizes it to `"123"` (Codex review, PR
            # #1168: "Canonicalize persisted order IDs with the runtime
            # helper" -- the fresh-evidence follow-up to the plain-strip
            # version below). Either gap lets the count invariant below
            # pass against an inflated id count, and reconciliation then
            # normalizes the CURRENT order_id before comparing, so
            # `_other_known_ids` would subtract both aliases as unrelated
            # attempts -- if the second was actually this exact order's own
            # already-persisted anonymous charge,
            # `book_reconciled_attempt_notional()` restores it a second
            # time (Codex review, PR #1164/#1165: "Reject aliased IDs while
            # loading attempt provenance"). Two raw spellings that
            # normalize to the same id are rejected outright, the same as
            # an aliased ticker or date key -- the serializer never
            # legitimately emits them (recorded_attempt_order_ids is
            # always written from a Python ``set``, which cannot hold two
            # equal strings), so their presence here is corruption, not a
            # duplicate to silently collapse.
            _normalized_ids: dict[str, str] = {}
            for _raw_id in ids:
                _normalized = _normalized_order_id(_raw_id)
                if not _normalized:
                    raise ValueError(
                        f"recorded_attempt_order_ids[{_ticker!r}] contains {_raw_id!r}, "
                        "which normalizes to an empty id"
                    )
                if _normalized in _normalized_ids:
                    # No `!= _raw_id` carve-out: an EXACT duplicate raw
                    # spelling (`["A", "A"]`) is corrupt for the identical
                    # reason two aliased spellings are -- the serializer
                    # always writes this field from a Python `set`, which
                    # cannot hold two equal strings under any legitimate
                    # write path, so an exact repeat here is corruption
                    # too, not a duplicate to silently collapse. The
                    # earlier `!= _raw_id` guard let this exact-duplicate
                    # shape fall through and collapse to one id via the
                    # dict assignment below, understating the persisted id
                    # count the same way an aliased pair would (Codex
                    # review, PR #1169: "Reject exact duplicate IDs in
                    # persisted provenance").
                    raise ValueError(
                        f"recorded_attempt_order_ids[{_ticker!r}] ids {_normalized_ids[_normalized]!r} "
                        f"and {_raw_id!r} both normalize to {_normalized!r}; a duplicate entry is "
                        "corrupt, not additive"
                    )
                _normalized_ids[_normalized] = _raw_id
            return set(_normalized_ids)

        for day, ids in _dedup_date_keyed(per_day.items(), _parse_order_ids).items():
            state.recorded_attempt_order_ids[(normalized_ticker, day)] = ids
    # record_buy()/book_reconciled_attempt_notional() only ever add an
    # order_id here in the SAME call that also bumps buys_by_ticker_by_day
    # for that exact (ticker, day) by at least 1 -- so the number of DISTINCT
    # ids known for a ticker/day can never exceed that day's own attempt
    # count. A persisted set larger than the ledger it was drawn from is
    # impossible from any legitimate write path and would silently subtract
    # too much from `buys_today()` in book_reconciled_attempt_notional's own
    # adjusted-count comparison, permitting extra restorations past
    # max_orders_per_day (the same class of corruption every other
    # persisted-bookkeeping-vs-ledger check above already guards against).
    for (ticker, day), ids in state.recorded_attempt_order_ids.items():
        actual_attempts = state.buys_by_ticker_by_day.get(ticker, {}).get(day, 0)
        if len(ids) > actual_attempts:
            raise ValueError(
                f"recorded_attempt_order_ids for {ticker!r} on {day.isoformat()!r} has "
                f"{len(ids)!r} distinct order id(s) but buys_by_ticker_by_day has only "
                f"{actual_attempts!r} attempt(s) for that ticker/date; recorded ids can never "
                "exceed the buy ledger they were drawn from"
            )
    # Durable per-order marker distinguishing "an attempt was recorded for
    # this id" from "this id's REAL notional was applied" -- see
    # StrategyState.notional_applied_order_ids' own docstring (Codex
    # review, PR #1251: "Preserve the first notional top-up for recorded
    # attempts"). Absent key loads as "nothing known yet", the same
    # forward-compatible fallback recorded_attempt_order_ids' own loader
    # above uses for a document written before this field existed.
    #
    # EXCEPT for a genuine schema_version=2 document: this field did not
    # exist until schema_version=3, so "nothing known yet" is the wrong
    # default there -- schema 2 already applied real notional for a
    # recorded id (either record_buy's own immediate nonzero booking, or
    # the pre-fix reconciliation top-up), just with no separate provenance
    # marker recording WHICH ids that covered. Defaulting to empty here
    # treated every schema-2 recorded id as still needing its first
    # top-up, so the very next reconciliation pass for an id that already
    # had real money booked under schema 2 added it a SECOND time,
    # inflating persisted daily/weekly spend past the configured caps
    # (Codex review, PR #1251: "Preserve schema-2 notional provenance
    # during migration").
    #
    # A first attempt at this fix blanket-copied EVERY schema-2 recorded id
    # into notional_applied_order_ids, on the theory that over-reporting
    # (missing a genuine top-up) is safer than under-reporting (double-
    # booking real spend). That was too blunt: a genuinely zero-dollar
    # schema-2 attempt (`record_buy(..., 0.0, order_id="X")`, never yet
    # topped up) got marked "applied" right alongside a real one, so ITS
    # first top-up became a permanent no-op too -- silently starving the
    # daily/weekly ledgers of dollars that were never actually spent
    # (Codex review, PR #1251: "Keep zero-dollar schema-2 attempts
    # eligible for top-up").
    #
    # The per-day submitted notional (present since schema 1, unlike the
    # new field) is the signal that disambiguates the two cases for a
    # (ticker, day) with exactly one recorded id shared by no other ticker
    # that same day: a zero day-total proves that id's zero-dollar charge
    # was never topped up, and a nonzero total can only have come from
    # that same id (nothing else recorded that day to have earned it). A
    # day with MORE than one id recorded across all tickers and a nonzero
    # total is genuinely ambiguous -- schema 2 never recorded which of
    # those ids the money actually belongs to -- so this fails closed
    # (raises) rather than guessing every id on the day was paid, the same
    # posture this module's other corrupt/ambiguous-state checks already
    # take.
    #
    # A single recorded id is ALSO not enough on its own: the day's total
    # ATTEMPT count (buys_by_ticker_by_day, which counts every attempt,
    # identified or not) can exceed its recorded-id count -- a day with
    # one id-bearing zero-dollar attempt plus one anonymous (id-less)
    # attempt, even for a different ticker, still has `_day_total_ids ==
    # 1` and a nonzero day total, but the dollars may belong to the
    # ANONYMOUS attempt, not the identified one. Marking the identified id
    # applied there would make its own later top-up a silent no-op the
    # same way the multi-id case does (Codex review, PR #1251: "Fail
    # closed on schema-2 days with anonymous attempts"). Only when the
    # identified-id count EQUALS the day's total attempt count -- nothing
    # anonymous hiding among them either -- is a single id provably the
    # one that earned the money.
    _is_schema_2_document_for_notional_migration = (
        isinstance(_raw_schema_version, int)
        and not isinstance(_raw_schema_version, bool)
        and _raw_schema_version == 2
    )
    state.notional_applied_order_ids = {}
    if _is_schema_2_document_for_notional_migration:
        _schema_2_recorded_ids_by_day: dict[date, dict[str, set[str]]] = {}
        for (_ticker, _day), _ids in state.recorded_attempt_order_ids.items():
            _schema_2_recorded_ids_by_day.setdefault(_day, {})[_ticker] = _ids
        _schema_2_attempts_by_day: dict[date, int] = {}
        for _per_day in state.buys_by_ticker_by_day.values():
            for _day, _count in _per_day.items():
                _schema_2_attempts_by_day[_day] = _schema_2_attempts_by_day.get(_day, 0) + _count
        for _day, _ids_by_ticker in _schema_2_recorded_ids_by_day.items():
            _day_total_ids = sum(len(_ids) for _ids in _ids_by_ticker.values())
            _day_notional = state.submitted_notional_by_day.get(_day, 0.0)
            if _day_notional == 0.0:
                # Genuinely never topped up (or a zero-dollar recorded
                # attempt sharing the day with another zero-dollar one) --
                # leave every id for this day out of notional_applied_
                # order_ids so its first real top-up can still land.
                continue
            _day_total_attempts = _schema_2_attempts_by_day.get(_day, 0)
            if _day_total_ids > 1 or _day_total_ids != _day_total_attempts:
                raise ValueError(
                    f"schema_version=2 document has {_day_total_ids!r} distinct recorded "
                    f"order id(s) but {_day_total_attempts!r} total attempt(s) on "
                    f"{_day.isoformat()!r}, with a nonzero submitted notional "
                    f"({_day_notional!r}) but no per-id provenance to say which id(s) or "
                    "anonymous attempt(s) earned it; migrating this document would have to "
                    "guess, which this repo's fail-closed posture forbids -- repair or "
                    "manually annotate this state file before it can be loaded"
                )
            (_only_ticker, _only_ids), = (
                (t, ids) for t, ids in _ids_by_ticker.items() if ids
            )
            state.notional_applied_order_ids[(_only_ticker, _day)] = set(_only_ids)
    notional_applied_ticker_spellings: dict[str, str] = {}
    for ticker, per_day in raw.get("notional_applied_order_ids", {}).items():
        normalized_ticker = _normalize_ticker(ticker)
        if not normalized_ticker:
            raise ValueError(
                f"notional_applied_order_ids key {ticker!r} normalizes to an empty ticker"
            )
        if normalized_ticker in notional_applied_ticker_spellings:
            raise ValueError(
                f"notional_applied_order_ids keys "
                f"{notional_applied_ticker_spellings[normalized_ticker]!r} and {ticker!r} both "
                f"normalize to {normalized_ticker!r}; a duplicate entry is corrupt, not additive"
            )
        notional_applied_ticker_spellings[normalized_ticker] = ticker

        def _parse_notional_applied_ids(ids, _ticker=ticker):
            # Identical validation/canonicalization to recorded_attempt_
            # order_ids' own `_parse_order_ids` closure above -- see its
            # extensive comments for why each check exists (aliased/empty/
            # duplicate ids are all corruption, never additive, for the
            # identical reason: this field is always written from a Python
            # `set`, which cannot hold two equal (post-normalization)
            # strings under any legitimate write path).
            if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
                raise ValueError(
                    f"notional_applied_order_ids[{_ticker!r}] must be a list of non-empty "
                    f"strings, got {ids!r}"
                )
            _normalized_ids: dict[str, str] = {}
            for _raw_id in ids:
                _normalized = _normalized_order_id(_raw_id)
                if not _normalized:
                    raise ValueError(
                        f"notional_applied_order_ids[{_ticker!r}] contains {_raw_id!r}, "
                        "which normalizes to an empty id"
                    )
                if _normalized in _normalized_ids:
                    raise ValueError(
                        f"notional_applied_order_ids[{_ticker!r}] ids "
                        f"{_normalized_ids[_normalized]!r} and {_raw_id!r} both normalize to "
                        f"{_normalized!r}; a duplicate entry is corrupt, not additive"
                    )
                _normalized_ids[_normalized] = _raw_id
            return set(_normalized_ids)

        for day, ids in _dedup_date_keyed(per_day.items(), _parse_notional_applied_ids).items():
            state.notional_applied_order_ids[(normalized_ticker, day)] = ids
    # StrategyState.notional_applied_order_ids is only ever populated for
    # an id already present in recorded_attempt_order_ids (record_buy and
    # book_reconciled_attempt_notional both add to the latter at the same
    # time or earlier than the former -- see the field's own docstring for
    # why) -- so a persisted id claiming its notional was applied for a
    # (ticker, day) that recorded_attempt_order_ids never even attempted
    # is impossible from any legitimate write path, the same class of
    # ledger-vs-provenance corruption the count invariant just above
    # already guards against for recorded_attempt_order_ids itself.
    for (ticker, day), ids in state.notional_applied_order_ids.items():
        _recorded_ids_for_day = state.recorded_attempt_order_ids.get((ticker, day), set())
        _unrecorded_ids = ids - _recorded_ids_for_day
        if _unrecorded_ids:
            raise ValueError(
                f"notional_applied_order_ids for {ticker!r} on {day.isoformat()!r} names "
                f"id(s) {sorted(_unrecorded_ids)!r} not present in recorded_attempt_order_ids "
                "for that ticker/date; a notional-applied id can never be missing from the "
                "attempt ledger it was drawn from"
            )
        # Every legitimate writer of this marker (record_buy,
        # book_reconciled_attempt_notional, seed_prior_allocation,
        # reconcile_prior_allocation) adds an id here ONLY in the SAME
        # call that also adds a NONZERO amount to submitted_notional_by_
        # day -- see notional_applied_order_ids' own docstring. A day with
        # at least one applied id but a zero (or absent) day notional is
        # therefore impossible from any legitimate write path: a damaged
        # or manually repaired state carrying that shape would otherwise
        # make book_reconciled_attempt_notional's own replay-guard treat
        # this id's notional as already booked forever, permanently
        # starving the daily/weekly ledgers of dollars that were never
        # actually spent and potentially permitting later orders past the
        # real caps (Codex review, PR #1251: "Reject applied IDs that have
        # no booked notional").
        if ids and state.submitted_notional_by_day.get(day, 0.0) == 0.0:
            raise ValueError(
                f"notional_applied_order_ids for {ticker!r} on {day.isoformat()!r} names "
                f"id(s) {sorted(ids)!r} as notional-applied, but submitted_notional_by_day "
                f"for {day.isoformat()!r} is zero or absent; an applied id can never lack "
                "the dollar backing that marking it applied claims"
            )
    # seeded_prior_context_registered_id_count_by_ticker_day is validated
    # above against its own proven-attempts bound, but that alone does not
    # prove the ids it claims to have registered actually exist:
    # reconcile_prior_allocation() only ever increments this counter in the
    # SAME call that adds a NEW id to recorded_attempt_order_ids, so its
    # value can never exceed the number of ids actually recorded for that
    # exact (ticker, day) -- a persisted registered_count of 1 with zero
    # entries in recorded_attempt_order_ids is impossible from any
    # legitimate write path (Codex review, PR #1168: "Reject registered
    # counts that exceed the ID ledger"). Without this cross-check, a
    # corrupted document could claim room was already used up (blocking a
    # later scan from backfilling the id it actually discovers) even
    # though no id was ever durably recorded to back that claim.
    for ticker, per_day in state.seeded_prior_context_registered_id_count_by_ticker_day.items():
        for d, registered_count in per_day.items():
            recorded_ids = state.recorded_attempt_order_ids.get((ticker, d), set())
            if registered_count > len(recorded_ids):
                raise ValueError(
                    f"seeded_prior_context_registered_id_count_by_ticker_day for {ticker!r} "
                    f"on {d.isoformat()!r} is {registered_count!r} but recorded_attempt_"
                    f"order_ids has only {len(recorded_ids)!r} id(s) for that ticker/date; "
                    "the registered-id count can never exceed the ids actually recorded"
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
    # A genuinely older schema_version=1 document (written before
    # recorded_attempt_order_ids existed) is validated against the smaller
    # legacy key set instead of being rejected outright -- a one-way
    # migration path, not a standing dual-schema contract: this process's
    # own next save_strategy_state() call always writes
    # STATE_SCHEMA_VERSION/_STATE_REQUIRED_KEYS, upgrading the file for
    # good (Codex review, PR #1164/#1165: "Require the provenance key
    # after migrating durable state").
    _raw_schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
    # Python considers `True == 1` and `1.0 == 1`, so a state document with
    # `"schema_version": true` or `1.0` would otherwise pass the `== 1`
    # comparison below and enter the legacy migration branch -- a malformed
    # or corrupted current document could then omit
    # `recorded_attempt_order_ids` and the two seeded-provenance counters
    # while still loading as an accepted empty legacy default, defeating the
    # version-2 requirement that exists to preserve per-order attempt
    # accounting. A later reconciliation could then treat a known attempt as
    # anonymous and mask a distinct missing charge (Codex review, PR #1169:
    # "Reject non-integer schema versions before migration"). Requiring an
    # actual, non-boolean `int` closes that gap without weakening the
    # ordinary `schema_version: 1` case.
    _is_legacy_schema_version = (
        isinstance(_raw_schema_version, int)
        and not isinstance(_raw_schema_version, bool)
        and _raw_schema_version == 1
    )
    # A genuinely older schema_version=2 document (written before
    # notional_applied_order_ids existed) is likewise validated against
    # its own smaller required-key set rather than being rejected outright
    # -- the identical one-way migration reasoning as the schema_version=1
    # case above, one field later (Codex review, PR #1251: "Preserve the
    # first notional top-up for recorded attempts"). The SAME non-boolean-
    # int requirement as every other version check here, for the identical
    # reason.
    _is_schema_2_version = (
        isinstance(_raw_schema_version, int)
        and not isinstance(_raw_schema_version, bool)
        and _raw_schema_version == 2
    )
    # The SAME non-boolean-int requirement as the legacy check above,
    # applied to the CURRENT version too -- `_raw_schema_version ==
    # STATE_SCHEMA_VERSION` alone lets a document spelling the version as
    # `2.0` (Python considers `2.0 == 2`) pass this comparison and enter
    # the current-version branch without ever going through the strict
    # legacy check. A malformed/corrupted document could carry that
    # float spelling while still satisfying `_STATE_REQUIRED_KEYS.
    # issubset(raw)`, defeating this loader's fail-closed version
    # contract the same way an unguarded `schema_version: 1.0` did before
    # the legacy check was hardened (Codex review, PR #1168: "Require an
    # integer for the current schema version").
    _is_current_schema_version = (
        isinstance(_raw_schema_version, int)
        and not isinstance(_raw_schema_version, bool)
        and _raw_schema_version == STATE_SCHEMA_VERSION
    )
    if _is_current_schema_version:
        _required_keys = _STATE_REQUIRED_KEYS
    elif _is_schema_2_version:
        _required_keys = _SCHEMA_2_STATE_REQUIRED_KEYS
    elif _is_legacy_schema_version:
        _required_keys = _LEGACY_STATE_REQUIRED_KEYS
    else:
        _required_keys = _STATE_REQUIRED_KEYS
    if (
        not isinstance(raw, dict)
        or not (_is_current_schema_version or _is_schema_2_version or _is_legacy_schema_version)
        or not _required_keys.issubset(raw)
    ):
        # A syntactically valid but incomplete document (e.g. "{}") is exactly
        # as dangerous as a truncated one: _state_from_json would default every
        # missing field and load it as a fresh state, silently wiping the
        # weekly and rollover accounting this loader exists to protect.
        raise StateCorruptError(
            f"{path} is valid JSON but is missing a supported schema_version "
            f"(got {_raw_schema_version!r}, supported: {STATE_SCHEMA_VERSION!r}, 2, or 1) "
            f"or one of that version's required keys {sorted(_required_keys)}. Refusing "
            "to continue with a fresh state: that would silently reset the weekly and "
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


_LOCK_IS_WINDOWS: bool = os.name == "nt"
# Bounded like scripts.file_lock.cross_process_lock (the lock every other
# reliability engine here uses): a waiter that cannot take the strategy-state
# lock within this window raises TimeoutError and the slot fails closed with
# evidence, instead of blocking forever behind a HUNG holder -- the documented
# wiring holds this lock around the whole run_pipeline() call, i.e. across
# every broker/MCP call, so an indefinite block wedged every later slot's
# process for the day silently (2026-09-07 diagnostic, C:F4). A crashed holder
# never wedges either backend: the kernel releases a POSIX flock and Windows
# releases a region lock when the process exits.
_STATE_LOCK_TIMEOUT_SECONDS: float = 30.0
_STATE_LOCK_POLL_SECONDS: float = 0.02


def _acquire_exclusive_lock(lock_file, timeout_seconds: float = _STATE_LOCK_TIMEOUT_SECONDS) -> None:
    """Take an exclusive, cross-process OS lock on ``lock_file`` or raise ``TimeoutError``.

    Both platforms poll a NON-blocking lock call against this function's own
    monotonic deadline, so ``timeout_seconds`` is honoured exactly: POSIX
    ``fcntl.flock(LOCK_EX | LOCK_NB)`` and Windows ``msvcrt.locking(LK_NBLCK)``.
    The first cut of this bound kept ``LK_LOCK`` on Windows, which retries
    inside msvcrt for roughly ten seconds per attempt, so a caller-supplied
    deadline shorter than that was never honoured there (Copilot review, PR
    #1394: "Use nonblocking Windows locking"). Only the documented contention
    signal is retried -- ``BlockingIOError`` from ``LOCK_NB``,
    ``PermissionError`` (EACCES) from ``LK_NBLCK``; every other ``OSError`` (a
    bad descriptor, a filesystem or I/O failure) is permanent and propagates
    immediately so ``state_transaction()`` fails closed with the real cause
    instead of sleeping on it and misreporting a timeout (Copilot review, PR
    #1394: "Retry only lock contention"; Codex review: "Stop retrying permanent
    Windows lock errors"). One bounded contract using only the standard
    library on either platform (Codex review: "Provide a portable state-lock
    backend").
    """
    deadline = time_module.monotonic() + timeout_seconds
    if _LOCK_IS_WINDOWS:
        import msvcrt

        win_msvcrt: Any = msvcrt
        lock_file.seek(0)
        while True:
            try:
                win_msvcrt.locking(lock_file.fileno(), win_msvcrt.LK_NBLCK, 1)
                return
            except PermissionError:
                if time_module.monotonic() > deadline:
                    raise TimeoutError(
                        f"could not acquire the strategy-state lock {lock_file.name} "
                        f"within {timeout_seconds:g}s"
                    ) from None
                time_module.sleep(_STATE_LOCK_POLL_SECONDS)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time_module.monotonic() > deadline:
                    raise TimeoutError(
                        f"could not acquire the strategy-state lock {lock_file.name} "
                        f"within {timeout_seconds:g}s"
                    ) from None
                time_module.sleep(_STATE_LOCK_POLL_SECONDS)


def _release_exclusive_lock(lock_file) -> None:
    """Release a lock acquired by :func:`_acquire_exclusive_lock`."""
    if _LOCK_IS_WINDOWS:
        import msvcrt

        win_msvcrt: Any = msvcrt
        lock_file.seek(0)
        win_msvcrt.locking(lock_file.fileno(), win_msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_UN)


def _exception_group(message: str, errors: list[BaseException]) -> BaseException:
    """``ExceptionGroup(message, errors)``, or ``BaseExceptionGroup`` if any
    of ``errors`` is a direct ``BaseException`` (e.g. ``KeyboardInterrupt``/
    ``SystemExit``) rather than an ``Exception``.

    ``ExceptionGroup`` itself raises ``TypeError`` when asked to wrap a
    non-``Exception`` member -- state_transaction()'s own callback-error
    collection deliberately catches ``BaseException`` (a callback's marker
    write failing on a KeyboardInterrupt mid-write is exactly as much a
    "did the marker actually get written" question as an OSError), and the
    in-flight body exception folded in alongside it can be anything the
    transaction body itself raised. Constructing a bare ``ExceptionGroup``
    in that case would raise TypeError right where this code means to
    surface the ORIGINAL failure, masking it behind a completely unrelated
    "cannot nest BaseExceptions" error instead (Codex review: "Use
    BaseExceptionGroup when grouping callback interrupts").
    """
    if all(isinstance(exc, Exception) for exc in errors):
        return ExceptionGroup(message, [exc for exc in errors if isinstance(exc, Exception)])
    return BaseExceptionGroup(message, errors)


@contextmanager
def state_transaction(path=None, *, lock_timeout_seconds: float = _STATE_LOCK_TIMEOUT_SECONDS):
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

    After a successful save, every callback queued on
    ``state.pending_durable_commit_callbacks`` runs -- ALL of them, even
    after an earlier one raises, so one claim's marker-write failure never
    silently skips a later claim's (see that field's own docstring). A
    caller catching errors from this context manager should expect either
    a single raw exception (exactly one callback failed) or a builtin
    ``ExceptionGroup`` (more than one did) on top of whatever
    ``save_strategy_state()``/``run_pipeline()`` themselves can raise
    (Antigravity AI review: "Callback Failure Exception Handling
    Uniformity").
    """
    resolved_path = Path(path) if path is not None else _default_state_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _state_lock_path(resolved_path)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        _acquire_exclusive_lock(lock_file, lock_timeout_seconds)
        try:
            state = load_strategy_state(resolved_path)
            # Reconciliation spans the run artifact and this ledger.  Expose a
            # lock-safe checkpoint so it can make the state half durable while
            # the transaction lock is still held, before allowing an artifact
            # finalizer to replace its replay marker.
            state._durable_commit = lambda: save_strategy_state(state, resolved_path)
            try:
                yield state
            finally:
                save_strategy_state(state, resolved_path)
                # Only reached once the save above returned without raising --
                # a caller queues these via
                # StrategyState.pending_durable_commit_callbacks specifically
                # to defer work (e.g. writing its own "this is durably
                # booked" artifact marker) that must never happen before the
                # record_buy()/etc. mutation it is vouching for has actually
                # reached disk (Codex review: "Mark recovery only after
                # committing StrategyState"). A save that raises skips this
                # entirely, leaving the callbacks queued for nothing -- there
                # is no next attempt to run them on, but that is correct:
                # the state they describe was never durably committed
                # either, so nothing should claim it was.
                callbacks = state.pending_durable_commit_callbacks
                state.pending_durable_commit_callbacks = []
                # Run EVERY queued callback, even after one raises, before
                # propagating anything -- the save above already committed
                # ALL of their corresponding mutations together in one
                # write, so a claim whose OWN marker write would have
                # succeeded must not be skipped just because a DIFFERENT
                # claim's marker write (earlier in the list) failed. An
                # early `for` loop that let the first raise abort the rest
                # left those later claims' StrategyState bookings durably
                # saved but their state_booked markers never written,
                # making every one of them look unbooked to a later retry
                # and double-booking each into StrategyState again even
                # though nothing was actually wrong with them (Codex
                # review: "Run later commit callbacks after one marker
                # fails").
                errors: list[BaseException] = []
                for callback in callbacks:
                    # A callback that fails on a TRANSIENT artifact read/write
                    # error (e.g. a momentary filesystem hiccup) leaves its
                    # underlying record_buy() durably saved above but its
                    # state_booked marker unwritten -- for a still-live order
                    # claim, the ledger claim stays unresolved (settle()/
                    # preserve_pending_if_claimed() only ever resolve
                    # terminal/zero-fill claims), so the next invocation's
                    # _gated_whole_share_recovery_already_recorded() reads
                    # state_booked=False and books the SAME notional and
                    # attempt into StrategyState again. Retrying the marker
                    # write a few times, immediately, closes the transient
                    # case without changing behavior for a durable failure
                    # (which will exhaust the retries and still surface below
                    # exactly as before) (review: "Repair failed marker writes
                    # before rebooking live claims").
                    last_exc: BaseException | None = None
                    for _attempt in range(3):
                        try:
                            callback()
                            last_exc = None
                            break
                        except BaseException as exc:  # noqa: BLE001 - collected, not
                            # swallowed: re-raised below once every callback has
                            # had its own chance to run.
                            last_exc = exc
                    if last_exc is not None:
                        errors.append(last_exc)
                if errors:
                    # A `raise` executed from this `finally` block REPLACES
                    # whatever exception is already propagating out of the
                    # `yield state` body above -- if run_pipeline() itself
                    # raised (the actual reason this transaction is
                    # unwinding) and a callback ALSO then raises, an
                    # unconditional `raise errors[0]`/ExceptionGroup(errors)
                    # here would silently discard that original body
                    # exception, leaving only the marker-write failure
                    # visible to the caller. Retry/failure-classification
                    # logic keyed on the run's own exception type would then
                    # respond to the wrong error entirely (Codex review:
                    # "Preserve the transaction-body exception when
                    # callbacks fail"). `sys.exc_info()` inside a `finally`
                    # returns exactly that in-flight exception, if any --
                    # fold it into the group alongside the callback errors
                    # rather than letting it vanish.
                    body_exc = sys.exc_info()[1]
                    if body_exc is not None:
                        raise _exception_group(
                            "gated recovery marker callbacks failed alongside the run's own exception",
                            [body_exc, *errors],
                        )
                    if len(errors) == 1:
                        raise errors[0]
                    raise _exception_group("gated recovery marker callbacks failed", errors)
        finally:
            _release_exclusive_lock(lock_file)
