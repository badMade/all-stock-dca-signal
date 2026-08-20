"""Morning graded-DCA executor (watchlist scan + live execution streams).

This module adds a *morning* stream on top of the existing near-close strategy
library (:mod:`near_close_dca`). It is deliberately isolated from the afternoon
scheduled live task (cron ``10ce362c``, which supersedes the deleted
``ce16ba6b``):

    - It uses the SAME canonical $2-base 0x -> 5x graded ladder as the afternoon
      and midday live streams (bound to ``StrategyConfig.sizing_tiers``); the
      former coarser 0x -> 3x morning ladder is historical/superseded.
    - It keeps its OWN budget ledger namespace (:class:`MorningLedger`) so
      morning activity can never consume the afternoon 3:20 budget ledger.
    - It writes artifacts under its OWN run-state roots and NEVER touches
      ``.../ce16ba6b/latest_scheduled_run.json`` or the afternoon
      ``scheduled_runs/`` folders.
    - Its run_classification always carries a distinct ``stream='morning'``
      marker and ``valid_afternoon_evidence=false`` so downstream afternoon
      reporting (which only globs ``.../ce16ba6b/scheduled_runs/**``) never
      mistakes it for afternoon scheduled evidence.

The morning stream has two modes (see :data:`MORNING_PROFILES`):

    - ``watchlist`` (9:45 AM ET, ``morning-dca-0945``): a READ-ONLY early-warning
      watchlist scan. Analytics only, ``trades=false``. It may surface early
      candidates but every dollar figure is HYPOTHETICAL / watchlist-only. It
      writes under ``.../cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/``
      with ``run_type='morning_dca_watchlist_scan'`` and
      ``state_schema_version='morning-watchlist-v1'``.
    - ``live`` (10:30 AM ET, ``42173154``): the approved live Robinhood morning
      execution, moved from 9:45 to 10:30 so it evaluates a true 60-minute
      trend window (9:30-10:30). ``trades=true``. It writes under
      ``.../cron_tracking/morning_dca/YYYYMMDD_1030ET/`` with
      ``run_type='morning_dca_live'``, ``scheduled_for_et='10:30'``,
      ``run_id='morning_live_YYYYMMDD_1030ET'`` and
      ``state_schema_version='morning-live-v2-preauthorized'`` (the
      pre-authorized direct-placement contract with raw-data persistence and
      automatic failure recovery).

Confidence rule (live mode): when a ticker's price/drop qualifies for a buy but
the intraday trend over the 60-minute window cannot be validated, the candidate
is NOT auto-bought. It is classified as a ``manual_review_candidate``. The
loosened "buy anyway when trend is unavailable" rule is deliberately NOT
implemented.

Robinhood account 562057042 remains the only execution broker; Alpaca stays
read-only. Morning fills are never afternoon scheduled evidence; they count only
as prior-allocation context for the afternoon same-day top-up guard.
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import math
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dca_config import load_approved_dates, load_tickers
from near_close_dca import (
    ACTIVE_TICKERS,
    EASTERN,
    EXCLUDED_TICKERS,
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
    TOP_UP_INCREMENTAL,
    TOP_UP_MISSING_PRIOR_PRICE,
    TOP_UP_NO_INCREMENTAL,
    TOP_UP_NO_PRIOR_RUN,
    TOP_UP_PRICE_NOT_BELOW,
    Action,
    Bar,
    BrokerAdapter,
    OrderConfirmationRequiredError,
    PriorSameDayAllocation,
    StrategyConfig,
    completed_bar_anchor,
    graded_buy_multiplier,
    iso_week_key,
    latest_bar_at_or_before,
    load_prior_same_day_allocations_multi,
    quote_spread_pct,
)
from scripts.run_classification import save_raw_input

_log = logging.getLogger(__name__)

# --- Morning stream identity / classification constants ---------------------
#
# The morning stream is INDEPENDENT of the afternoon live task; it never
# supersedes it and is never valid afternoon evidence. These constants
# intentionally differ from the afternoon 15:20 classification so the streams can
# never be confused (see docs/morning_dca.md).
MORNING_STREAM = "morning"
# The afternoon live task id, recorded on morning artifacts for cross-reference.
AFTERNOON_LIVE_CRON_ID = "10ce362c"

# Watchlist (9:45 AM ET) scan identity — analytics-only, no trading.
MORNING_WATCHLIST_MODE = "watchlist"
MORNING_WATCHLIST_CRON_ID = "morning-dca-0945"
MORNING_WATCHLIST_RUN_TYPE = "morning_dca_watchlist_scan"
MORNING_WATCHLIST_SCHEDULED_FOR_ET = "09:45"
MORNING_WATCHLIST_WINDOW_START_ET = "09:40"
MORNING_WATCHLIST_WINDOW_END_ET = "09:50"
MORNING_WATCHLIST_SCHEMA_VERSION = "morning-watchlist-v1"

# Pre-flight validation (9:45 AM ET) identity — read-only pre-screen for the
# 10:30 live executor. Writes eligible_to_execute=true/false per ticker. This is
# a COARSE pre-screen (is a ticker worth evaluating at 10:30?), NOT a final
# approval: the 10:30 live run always recomputes signals, budget, spread, trend,
# and top-up from scratch and may still decline to place an order.
#
# As of Mon Jul 13 2026 the artifact is machine-complete
# (schema morning-preflight-v2-machine-complete, schemas/morning_preflight.schema.json):
# it always writes the top-level overall_ok flag plus eligible_count,
# ineligible_count, unresolved_tickers, blocking_errors, and a
# per_ticker_eligibility list with every active-universe symbol exactly once, so
# the 10:30 live task can gate directly off machine fields. Unresolved symbols
# (e.g. AAAU, SPCX) are emitted as rows with eligible_to_execute=false and
# ineligible_reason='unresolved_ticker'; they never force overall_ok=false, which
# flips false only for global/account/buying-power/auth/schema-write failures.
MORNING_PREFLIGHT_MODE = "preflight"
MORNING_PREFLIGHT_CRON_ID = "morning-dca-0945"
MORNING_PREFLIGHT_RUN_TYPE = "morning_dca_preflight"
MORNING_PREFLIGHT_SCHEDULED_FOR_ET = "09:45"
MORNING_PREFLIGHT_WINDOW_START_ET = "09:40"
MORNING_PREFLIGHT_WINDOW_END_ET = "09:50"
MORNING_PREFLIGHT_SCHEMA_VERSION = "morning-preflight-v2-machine-complete"
MORNING_PREFLIGHT_TREND_STATUS = "not_evaluated_preflight"

# Live (10:30 AM ET) execution identity — Robinhood-only live buys.
MORNING_LIVE_MODE = "live"
MORNING_LIVE_CRON_ID = "42173154"
MORNING_LIVE_RUN_TYPE = "morning_dca_live"
MORNING_LIVE_SCHEDULED_FOR_ET = "10:30"
MORNING_LIVE_WINDOW_START_ET = "10:25"
MORNING_LIVE_WINDOW_END_ET = "10:35"
MORNING_LIVE_SCHEMA_VERSION = "morning-live-v2-preauthorized"

# Backwards-compatible aliases (the watchlist scan is the successor of the old
# 9:45 dry-run stream, so unqualified references resolve to it).
MORNING_CRON_ID = MORNING_WATCHLIST_CRON_ID
MORNING_RUN_TYPE = MORNING_WATCHLIST_RUN_TYPE
MORNING_SCHEDULED_FOR_ET = MORNING_WATCHLIST_SCHEDULED_FOR_ET
MORNING_WINDOW_START_ET = MORNING_WATCHLIST_WINDOW_START_ET
MORNING_WINDOW_END_ET = MORNING_WATCHLIST_WINDOW_END_ET

# Artifact roots. Both are siblings of, and never nested under, the afternoon
# ce16ba6b root. The watchlist scan and live execution use distinct roots so a
# read-only scan can never be confused with a live-execution artifact.
DEFAULT_MORNING_WATCHLIST_STATE_ROOT = "/home/user/workspace/cron_tracking/morning_dca_watchlist"
DEFAULT_MORNING_PREFLIGHT_STATE_ROOT = "/home/user/workspace/cron_tracking/morning_dca_preflight"
DEFAULT_MORNING_LIVE_STATE_ROOT = "/home/user/workspace/cron_tracking/morning_dca"
# Legacy alias: the default root historically pointed at the morning stream.
DEFAULT_MORNING_STATE_ROOT = DEFAULT_MORNING_WATCHLIST_STATE_ROOT
# The afternoon live task artifact root. The afternoon scheduled live task
# uses cron id 10ce362c (supersedes ce16ba6b), but the artifact root remains
# under the historical ce16ba6b directory for continuity. Scanned by the
# morning top-up guard so manual/recovery fills recorded before 10:30 AM are
# detected.
DEFAULT_AFTERNOON_TASK_ROOT = "/home/user/workspace/cron_tracking/ce16ba6b"

# Jul 20 2026 pre-flight path fix (see live_task.preflight_artifact_resolution
# in config/morning_dca.json). The 10:30 live run must never read the same-day
# 9:45 pre-flight artifact from either of these roots -- both were implicated
# in the Jul 20 2026 escalation (a stray cron-id path and the live task's own
# output directory). _apply_preflight_gate() fails closed rather than reading
# from a preflight_run_state_root that resolves under one of these.
DEFAULT_PREFLIGHT_FORBIDDEN_ROOTS: Tuple[str, ...] = (
    "/home/user/workspace/cron_tracking/42173154",
    DEFAULT_MORNING_LIVE_STATE_ROOT,
)

# Jul 20 2026 budget ledger location fix (see budget_ledger.bootstrap in
# config/morning_dca.json). Default filename for the persisted morning budget
# ledger snapshot written under the live run's own output directory.
DEFAULT_BUDGET_LEDGER_FILENAME = "budget_ledger.json"


# Per-mode identity/profile. Selected by MorningDCAExecutor(mode=...).
MORNING_PROFILES: Dict[str, Dict[str, Any]] = {
    MORNING_WATCHLIST_MODE: {
        "mode": MORNING_WATCHLIST_MODE,
        "artifact_mode": "watchlist",
        "trades": False,
        "cron_id": MORNING_WATCHLIST_CRON_ID,
        "run_type": MORNING_WATCHLIST_RUN_TYPE,
        "scheduled_for_et": MORNING_WATCHLIST_SCHEDULED_FOR_ET,
        "window_start_et": MORNING_WATCHLIST_WINDOW_START_ET,
        "window_end_et": MORNING_WATCHLIST_WINDOW_END_ET,
        "state_schema_version": MORNING_WATCHLIST_SCHEMA_VERSION,
        "state_root": DEFAULT_MORNING_WATCHLIST_STATE_ROOT,
        "folder_suffix": "0945ET",
        "scheduled_run_id_prefix": "morning",
        "manual_run_id_prefix": "manual_morning",
        "decision_time_et": time(9, 45),
        "validate_trend": False,
    },
    MORNING_PREFLIGHT_MODE: {
        "mode": MORNING_PREFLIGHT_MODE,
        "artifact_mode": "preflight",
        "trades": False,
        "cron_id": MORNING_PREFLIGHT_CRON_ID,
        "run_type": MORNING_PREFLIGHT_RUN_TYPE,
        "scheduled_for_et": MORNING_PREFLIGHT_SCHEDULED_FOR_ET,
        "window_start_et": MORNING_PREFLIGHT_WINDOW_START_ET,
        "window_end_et": MORNING_PREFLIGHT_WINDOW_END_ET,
        "state_schema_version": MORNING_PREFLIGHT_SCHEMA_VERSION,
        "state_root": DEFAULT_MORNING_PREFLIGHT_STATE_ROOT,
        "folder_suffix": "0945ET",
        "scheduled_run_id_prefix": "morning_preflight",
        "manual_run_id_prefix": "manual_morning_preflight",
        "decision_time_et": time(9, 45),
        "validate_trend": False,
    },
    MORNING_LIVE_MODE: {
        "mode": MORNING_LIVE_MODE,
        "artifact_mode": "live",
        "trades": True,
        "cron_id": MORNING_LIVE_CRON_ID,
        "run_type": MORNING_LIVE_RUN_TYPE,
        "scheduled_for_et": MORNING_LIVE_SCHEDULED_FOR_ET,
        "window_start_et": MORNING_LIVE_WINDOW_START_ET,
        "window_end_et": MORNING_LIVE_WINDOW_END_ET,
        "state_schema_version": MORNING_LIVE_SCHEMA_VERSION,
        "state_root": DEFAULT_MORNING_LIVE_STATE_ROOT,
        "folder_suffix": "1030ET",
        "scheduled_run_id_prefix": "morning_live",
        "manual_run_id_prefix": "manual_morning_live",
        "decision_time_et": time(10, 30),
        "validate_trend": True,
    },
}

STATE_SCHEMA_VERSION = MORNING_WATCHLIST_SCHEMA_VERSION

# Morning sizing ladder. As of the Fri Jul 10 2026 live-automation update every
# live stream (morning 10:30, midday 12:30, afternoon 3:20) shares the SAME
# canonical $2-base 0x -> 5x graded ladder defined on
# ``near_close_dca.StrategyConfig.sizing_tiers``. The former coarser 0x -> 3x
# morning ladder (0.1-2/2-5/5-8/8+ -> 1x/1.5x/2x/3x) is HISTORICAL/SUPERSEDED and
# is no longer used by the live morning stream. Binding to the afternoon tuple
# keeps all live streams in lockstep, so a single ladder edit updates them all.
#     drop < 0.1%             -> 0x  (no buy)
#     0.1% <= drop < 1.0%     -> 1x
#     1.0% <= drop < 2.0%     -> 1.5x
#     2.0% <= drop < 3.0%     -> 2x
#     3.0% <= drop < 4.0%     -> 2.5x
#     4.0% <= drop < 5.0%     -> 3x
#     5.0% <= drop < 6.0%     -> 3.5x
#     6.0% <= drop < 7.0%     -> 4x
#     7.0% <= drop < 8.0%     -> 4.5x
#     drop >= 8.0%            -> 5x  (no upper cutoff)
MORNING_SIZING_TIERS: Tuple[Tuple[float, float], ...] = StrategyConfig().sizing_tiers

MORNING_MINIMUM_DROP = 0.001

logger = logging.getLogger(__name__)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the ``n``-th ``weekday`` (0=Mon..6=Sun) in ``year``/``month``."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Return the date of the last ``weekday`` (0=Mon..6=Sun) in ``year``/``month``."""
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Return Easter Sunday for ``year`` via the Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(holiday: date) -> date:
    """Shift a fixed-date holiday off a weekend per standard US observance rules."""
    if holiday.weekday() == 5:  # Saturday -> observed Friday
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday -> observed Monday
        return holiday + timedelta(days=1)
    return holiday


def nyse_holidays(year: int) -> frozenset[date]:
    """Return the NYSE's full-market-closure dates for ``year``.

    Computed from the exchange's long-standing, publicly documented holiday
    rules (fixed dates with weekend observance shifts, or Nth-weekday-of-month
    rules) rather than a hardcoded per-year list, so the morning stream's
    holiday gate (unlike :data:`near_close_dca.MARKET_HOLIDAYS`, which is
    scoped to the afternoon live-test window) stays correct indefinitely into
    the future without manual updates. Good Friday is included; NYSE has
    observed it as a full closure every year on record.
    """
    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),  # MLK Day: 3rd Monday of January
        _nth_weekday_of_month(year, 2, 0, 3),  # Washington's Birthday: 3rd Monday of February
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday_of_month(year, 5, 0),  # Memorial Day: last Monday of May
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day: 1st Monday of September
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving: 4th Thursday of November
        _observed(date(year, 12, 25)),  # Christmas
    }
    # When Jan 1 of the *next* year falls on a Saturday, NYSE observes it on the
    # preceding Friday (Dec 31 of *this* year).  That observed date belongs to
    # this year's holiday set but would otherwise only appear in nyse_holidays(year+1).
    next_new_year = date(year + 1, 1, 1)
    if next_new_year.weekday() == 5:  # Saturday -> observed Friday Dec 31
        holidays.add(date(year, 12, 31))
    return frozenset(holidays)

# Morning budget caps. The morning stream owns these; they are intentionally not
# the afternoon BUDGET_TIERS caps so morning activity is budgeted independently.
DEFAULT_MORNING_DAILY_CAP_USD = 90.0
DEFAULT_MORNING_WEEKLY_CAP_USD = 450.0


@dataclass(frozen=True)
class TopUpOutcome:
    """Per-ticker result of the same-day prior-run offset/top-up guard.

    Shared by the morning and midday live streams; ``MiddayTopUpOutcome`` in
    ``midday_dca`` is an alias for backwards compatibility.
    """

    ticker: str
    desired_amount: float
    decision_price: Optional[float]
    prior_same_day_amount: float
    prior_same_day_reference_price: Optional[float]
    prior_same_day_run_id: Optional[str]
    prior_same_day_source: Optional[str]
    incremental_order_amount: float
    top_up_guard_result: str
    top_up_skipped: bool
    top_up_manual_review: bool = False


@dataclass(frozen=True)
class MorningConfig:
    """Configuration hooks for the morning live stream.

    Kept separate from :class:`~near_close_dca.StrategyConfig` so the morning
    ladder, caps, decision anchor, and run-state root can never leak into the
    afternoon scheduled path. ``base_buy_amount_usd`` matches the afternoon $2
    base by default so the hypothetical dollar figures are comparable.
    """

    tickers: Sequence[str] = field(default_factory=lambda: tuple(ACTIVE_TICKERS))
    excluded_tickers: frozenset[str] = field(default_factory=lambda: frozenset(EXCLUDED_TICKERS))
    base_buy_amount_usd: float = 2.0
    sizing_tiers: Tuple[Tuple[float, float], ...] = MORNING_SIZING_TIERS
    minimum_drop_vs_previous_close: float = MORNING_MINIMUM_DROP
    # None means "use the active mode's default decision anchor" (09:45 for the
    # watchlist scan, 10:30 for the live execution).
    decision_time_et: Optional[time] = None
    interval_minutes: int = 5
    morning_daily_cap_usd: float = DEFAULT_MORNING_DAILY_CAP_USD
    morning_weekly_cap_usd: float = DEFAULT_MORNING_WEEKLY_CAP_USD
    # Optional explicit artifact-root override. When None the executor uses the
    # active mode's default root (watchlist vs. live).
    morning_run_state_root: Optional[str] = None
    # Root the 10:30 live gate reads the same-day 9:45 pre-flight artifact from.
    # When None the live executor uses DEFAULT_MORNING_PREFLIGHT_STATE_ROOT.
    preflight_run_state_root: Optional[str] = None
    # Roots the pre-flight artifact must NEVER be read from (see
    # live_task.preflight_artifact_resolution.must_not_look_under in
    # morning_dca.json). When empty, DEFAULT_PREFLIGHT_FORBIDDEN_ROOTS is used.
    preflight_forbidden_roots: Tuple[str, ...] = ()
    # Whether _apply_preflight_gate performs the single bounded recovery scan
    # (see live_task.preflight_artifact_resolution.bounded_recovery_lookup)
    # when the primary path and latest_preflight.json pointer both miss.
    preflight_bounded_recovery: bool = True
    # Filename of the persisted morning budget ledger snapshot the live task
    # writes under its own output directory (see budget_ledger.bootstrap /
    # budget_ledger.ledger_files in morning_dca.json).
    budget_ledger_filename: str = DEFAULT_BUDGET_LEDGER_FILENAME
    # Roots scanned for same-day prior allocations by the morning live top-up
    # guard. None (default) means use [DEFAULT_AFTERNOON_TASK_ROOT] at runtime
    # so manual/recovery fills before 10:30 AM are detected automatically.
    # Set to () (empty tuple) to fully disable the guard (equivalent to setting
    # artifact_paths.prior_allocation_roots to [] in morning_dca.json).
    # Override via artifact_paths.prior_allocation_roots in morning_dca.json;
    # an explicit [] is preserved as () to honour the opt-out intent.
    prior_allocation_roots: Optional[Tuple[str, ...]] = None
    # Roots used ONLY for seeding the morning daily/weekly cap ledger with prior
    # same-day spend. Must contain ONLY morning-stream roots (e.g.
    # morning_dca/manual_runs); never afternoon roots like ce16ba6b, which would
    # incorrectly exhaust the morning cap. When None, no cap seeding is performed
    # (the full daily cap remains available). Set via artifact_paths.cap_seed_roots.
    cap_seed_roots: Optional[Tuple[str, ...]] = None
    # Minimum number of completed intraday bars over the trend window required to
    # validate the intraday trend in live mode. When fewer are available a
    # qualifying candidate is flagged manual_review_candidate rather than bought.
    trend_min_bars: int = 6
    # Broker account number that must match the supplied BrokerAdapter before
    # any live orders are placed.
    target_account_number: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
    # Weekday dates on which live (non-manual) execution is permitted. Empty
    # frozenset means the date gate is disabled (no restriction beyond weekends
    # and NYSE holidays). Mirrors StrategyConfig.approved_test_dates semantics
    # so morning/midday honour the same trading-window config as the afternoon.
    approved_test_dates: frozenset[date] = field(default_factory=frozenset)
    # Absolute per-order ceiling loaded from config/budgets.json
    # circuit_breakers.max_per_order_usd. 5x * $2 base = $10 default. Any
    # single order amount above this is blocked before it reaches the broker.
    max_per_order_usd: float = 10.0
    # Bid/ask spread circuit breaker for live placement, mirroring the
    # afternoon stream's StrategyConfig.max_bid_ask_spread_pct. A candidate
    # whose live quote spread exceeds this percent of the quote midpoint is
    # blocked before placement rather than bought at a stale/no-data price.
    max_bid_ask_spread_pct: float = 1.0

    def as_strategy_config(self) -> StrategyConfig:
        """Return a StrategyConfig view carrying the morning ladder.

        Only the fields the shared sizing math reads (``sizing_tiers``,
        ``minimum_drop_vs_previous_close``, ``base_buy_amount_usd``) are set; the
        result is used solely to drive :func:`graded_buy_multiplier` and never to
        run the afternoon live path. ``dry_run`` stays True.
        """
        return StrategyConfig(
            base_buy_amount_usd=self.base_buy_amount_usd,
            sizing_tiers=tuple(self.sizing_tiers),
            minimum_drop_vs_previous_close=self.minimum_drop_vs_previous_close,
            dry_run=True,
        )


@dataclass
class MorningLedger:
    """A budget ledger namespaced to the morning stream.

    This is a self-contained session that tracks the morning live stream's own
    notional. It shares no state with the afternoon
    :class:`~near_close_dca.StrategyState`, so morning activity can never consume
    or validate the afternoon 3:20/3:45 daily/weekly budget ledger.
    """

    namespace: str = MORNING_STREAM
    daily_cap_usd: float = DEFAULT_MORNING_DAILY_CAP_USD
    weekly_cap_usd: float = DEFAULT_MORNING_WEEKLY_CAP_USD
    planned_by_day: Dict[date, float] = field(default_factory=dict)
    planned_by_week: Dict[Tuple[int, int], float] = field(default_factory=dict)

    def record_hypothetical(self, trading_day: date, amount_usd: float) -> None:
        if amount_usd <= 0:
            return
        self.planned_by_day[trading_day] = self.day_planned(trading_day) + amount_usd
        week = iso_week_key(trading_day)
        self.planned_by_week[week] = self.planned_by_week.get(week, 0.0) + amount_usd

    def day_planned(self, trading_day: date) -> float:
        return self.planned_by_day.get(trading_day, 0.0)

    def week_planned(self, trading_day: date) -> float:
        return self.planned_by_week.get(iso_week_key(trading_day), 0.0)

    def remaining_daily(self, trading_day: date) -> float:
        return max(0.0, self.daily_cap_usd - self.day_planned(trading_day))

    def remaining_weekly(self, trading_day: date) -> float:
        return max(0.0, self.weekly_cap_usd - self.week_planned(trading_day))

    def over_daily_cap(self, trading_day: date) -> bool:
        return self.day_planned(trading_day) > self.daily_cap_usd

    def over_weekly_cap(self, trading_day: date) -> bool:
        return self.week_planned(trading_day) > self.weekly_cap_usd


# Live-mode execution decision for a qualifying candidate.
#   auto_buy_eligible     -> price/drop passed AND intraday trend validated.
#   manual_review_candidate -> price/drop passed but intraday trend could not be
#                              validated; the run does NOT auto-buy.
EXEC_AUTO_BUY_ELIGIBLE = "auto_buy_eligible"
EXEC_MANUAL_REVIEW_CANDIDATE = "manual_review_candidate"


def preflight_eligibility(signal: "MorningSignal") -> bool:
    """Return whether ``signal`` is eligible for the 10:30 live evaluator.

    This is the 9:45 pre-flight verdict: a COARSE pre-screen answering "is this
    ticker worth re-evaluating at 10:30?", NOT a final buy approval. A ticker is
    eligible when it graded to a positive-multiplier BUY at 9:45 with both a
    usable current price and previous close. The 10:30 live run always recomputes
    signals/budget/spread/trend/top-up from scratch and may still decline, so an
    eligible ticker is not guaranteed to be bought and an ineligible ticker is
    simply skipped by the live gate.
    """
    return (
        signal.action == Action.BUY
        and signal.multiplier > 0
        and signal.current_price is not None
        and signal.previous_close is not None
    )


def preflight_ticker_eligibility_row(signal: "MorningSignal") -> Dict[str, Any]:
    """Build one machine-complete ``per_ticker_eligibility`` row for ``signal``.

    Emits every field the 10:30 live gate consumes, exactly once per active
    symbol. Fields the coarse read-only pre-flight does not evaluate (account,
    buying power, spread) are ``None`` rather than fabricated; the 10:30 live run
    computes them fresh. Trend is ``not_evaluated_preflight`` (no 60-minute
    window at 9:45). Unresolved symbols (no usable quote or prior close, e.g.
    AAAU/SPCX) are marked ``eligible_to_execute=false`` with
    ``ineligible_reason='unresolved_ticker'`` and never invalidate the artifact.
    """
    quote_available = signal.current_price is not None
    prior_close_available = signal.previous_close is not None
    resolved = quote_available and prior_close_available
    eligible = preflight_eligibility(signal)

    if not quote_available and not prior_close_available:
        ineligible_reason: Optional[str] = "unresolved_ticker"
    elif not quote_available:
        ineligible_reason = "quote_unavailable"
    elif not prior_close_available:
        ineligible_reason = "prior_close_unavailable"
    elif not eligible:
        ineligible_reason = "no_qualifying_drop"
    else:
        ineligible_reason = None

    return {
        "symbol": signal.ticker,
        "active_universe": True,
        "quote_available": quote_available,
        "prior_close_available": prior_close_available,
        "tradable": resolved,
        # Account/buying-power/spread are not bound by the read-only pre-flight;
        # the 10:30 live run performs the authoritative checks.
        "account_ok": None,
        "buying_power_ok": None,
        "spread_pct": None,
        "spread_ok": None,
        "trend_status": MORNING_PREFLIGHT_TREND_STATUS,
        "data_confidence_status": "resolved" if resolved else "unresolved",
        "early_drop_pct": signal.drop_pct,
        "early_multiplier": signal.multiplier,
        "hypothetical_amount": signal.hypothetical_amount_usd,
        "eligible_to_execute": eligible,
        "ineligible_reason": ineligible_reason,
    }


@dataclass(frozen=True)
class MorningSignal:
    """One morning signal row for a single ticker."""

    ticker: str
    source: str
    action: Action
    reason: str
    current_price: Optional[float]
    previous_close: Optional[float]
    drop_pct: Optional[float]
    multiplier: float
    hypothetical_amount_usd: float
    warnings: Tuple[str, ...] = ()
    # Live mode only: the execution decision for a qualifying candidate. None in
    # watchlist mode (no execution decision is taken there).
    execution_status: Optional[str] = None
    trend_validated: Optional[bool] = None

    def to_row(self) -> Dict[str, Any]:
        row = {
            "ticker": self.ticker,
            "source": self.source,
            "action": self.action.value,
            "reason": self.reason,
            "current_price": self.current_price,
            "previous_close": self.previous_close,
            "drop_pct": self.drop_pct,
            "multiplier": self.multiplier,
            "hypothetical_amount_usd": self.hypothetical_amount_usd,
            "warnings": "; ".join(self.warnings),
        }
        # Keep the signal CSV schema stable across mixed live rows. Some rows
        # (for example NO_DATA or sub-threshold skips) do not make an execution
        # decision, but later BUY rows may carry these fields; including them on
        # every row prevents DictWriter field mismatches and gives downstream
        # readers predictable columns.
        row["execution_status"] = self.execution_status
        row["trend_validated"] = self.trend_validated
        row["auto_buy_signal"] = self.execution_status == EXEC_AUTO_BUY_ELIGIBLE
        return row


def load_max_per_order_usd(config_dir_path: Path) -> Optional[float]:
    """Read ``circuit_breakers.max_per_order_usd`` from ``config/budgets.json``.

    Returns the value when present and valid, ``None`` when the file is absent or
    the key is not set. Raises ``ValueError`` when the key is present but invalid,
    and raises ``RuntimeError`` when the file exists but cannot be read or parsed
    (both are fail-closed: a cap file that exists but is unreadable must not
    silently fall back to the dataclass default and bypass a deliberate cap edit).
    """
    budgets_path = config_dir_path / "budgets.json"
    if not budgets_path.exists():
        return None
    try:
        budgets_raw = json.loads(budgets_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"budgets.json exists but could not be read or parsed; "
            f"refusing to silently bypass the per-order cap ({exc})"
        ) from exc
    if not isinstance(budgets_raw, dict):
        raise RuntimeError(
            f"budgets.json exists but is not a JSON object (got {type(budgets_raw).__name__}); "
            "refusing to silently bypass the per-order cap"
        )
    cb = budgets_raw.get("circuit_breakers", {})
    if not isinstance(cb, dict):
        raise RuntimeError(
            f"budgets.json circuit_breakers must be an object, got {type(cb).__name__}; "
            "refusing to silently bypass the per-order cap"
        )
    if "max_per_order_usd" not in cb:
        return None
    raw_val = cb["max_per_order_usd"]
    try:
        max_order = float(raw_val)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"budgets.json circuit_breakers.max_per_order_usd must be a finite "
            f"positive number, got {raw_val!r}"
        ) from exc
    if not (math.isfinite(max_order) and max_order > 0):
        raise ValueError(
            f"budgets.json circuit_breakers.max_per_order_usd must be a finite "
            f"positive number, got {raw_val!r}"
        )
    return max_order


# A non-empty, unreachable date so the existing `bool(approved) and trading_day
# not in approved` gate check in _maybe_block_for_holiday() still blocks every
# real trading day (fail closed) rather than silently disabling the date gate
# the way an empty frozenset would (see _load_approved_dates_fail_closed()).
_APPROVED_DATES_LOAD_FAILED = frozenset({date.min})


def _load_approved_dates_fail_closed(config_dir_path: Path) -> "frozenset[date]":
    """Return approved_test_dates from trading_window.json, failing closed on error.

    An absent trading_window.json is a legitimate, expected configuration (the
    stream falls back to its hardcoded/empty default, matching prior behavior)
    and load_approved_dates() returns ``{}`` for that case without raising. It
    only raises when the file exists but is malformed (missing start_date/
    end_date, an invalid date range, or a bad decision_time_et) -- an anomalous
    state that must not be treated the same as "no restriction configured".
    Falling back to an empty set there would silently disable the live date
    gate precisely when its configuration is broken, so this returns
    _APPROVED_DATES_LOAD_FAILED (an unreachable date) instead, which blocks
    every real trading day through the normal "outside approved trading
    window" path.
    """
    try:
        return load_approved_dates(config_dir_path).get("approved_test_dates", frozenset())
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "could not load approved dates from trading_window.json (%s); "
            "failing closed (blocking all dates) rather than disabling the gate",
            exc,
        )
        return _APPROVED_DATES_LOAD_FAILED


def load_morning_config(config_dir=None, mode: str = MORNING_WATCHLIST_MODE):
    """Read ``config/morning_dca.json`` and return a :class:`MorningConfig`.

    ``mode`` selects which artifact root to bind (the watchlist scan and the live
    execution share the morning ladder and budget caps but write to distinct
    roots). Looks for ``morning_dca.json`` in ``config_dir`` (default: a
    ``config/`` directory next to this module, falling back to ``./config``). If
    the file is absent or unreadable the function logs a warning and returns
    ``MorningConfig()`` with hardcoded defaults -- the same graceful-fallback
    pattern used by :func:`dca_config.load_strategy_config`.
    """
    if config_dir is None:
        env_dir = os.environ.get("DCA_CONFIG_DIR")
        if env_dir:
            config_dir_path = Path(env_dir)
        else:
            _here = Path(__file__).resolve().parent
            _installed = Path(sys.prefix) / "share" / "all-stock-dca" / "config"
            for candidate in (_here / "config", _installed, Path.cwd() / "config"):
                if candidate.is_dir():
                    config_dir_path = candidate
                    break
            else:
                config_dir_path = _here / "config"
    else:
        config_dir_path = Path(config_dir)

    cfg_path = config_dir_path / "morning_dca.json"
    if not cfg_path.exists():
        logger.warning(
            "morning_dca.json not found at %s; using MorningConfig defaults", cfg_path
        )
        approved_dates = _load_approved_dates_fail_closed(config_dir_path)
        return MorningConfig(approved_test_dates=approved_dates)

    approved_date_overrides = {
        "approved_test_dates": _load_approved_dates_fail_closed(config_dir_path)
    }

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("could not parse %s (%s); using MorningConfig defaults", cfg_path, exc)
        return MorningConfig(**approved_date_overrides)

    kwargs = dict(approved_date_overrides)

    sizing = raw.get("sizing_ladder", {})
    if "base_amount_usd" in sizing:
        kwargs["base_buy_amount_usd"] = float(sizing["base_amount_usd"])
    if "no_buy_below_pct" in sizing:
        kwargs["minimum_drop_vs_previous_close"] = float(sizing["no_buy_below_pct"]) / 100.0
    if "tiers" in sizing:
        tiers = tuple(
            (float(t["min_drop_pct"]) / 100.0, float(t["multiplier"]))
            for t in sizing["tiers"]
        )
        kwargs["sizing_tiers"] = tiers

    budget = raw.get("budget_ledger", {})
    if "morning_daily_cap_usd" in budget:
        kwargs["morning_daily_cap_usd"] = float(budget["morning_daily_cap_usd"])
    if "morning_weekly_cap_usd" in budget:
        kwargs["morning_weekly_cap_usd"] = float(budget["morning_weekly_cap_usd"])
    # budget_ledger.bootstrap (added Jul 20 2026): the live task persists a
    # budget_ledger.json snapshot under its own output directory using this
    # configured filename, bootstrapping it from the caps above when absent
    # rather than requiring a pre-existing ledger file.
    ledger_files = budget.get("ledger_files")
    if isinstance(ledger_files, list) and ledger_files:
        kwargs["budget_ledger_filename"] = str(ledger_files[0])

    if "trend_min_bars" in raw:
        kwargs["trend_min_bars"] = int(raw["trend_min_bars"])

    live_task = raw.get("live_task", {})
    if "broker_account" in live_task:
        kwargs["target_account_number"] = str(live_task["broker_account"])
    # preflight_artifact_resolution (added Jul 20 2026): pins where the 10:30
    # live gate is and is not allowed to read the same-day 9:45 pre-flight
    # artifact from, and whether it performs the single bounded recovery scan.
    preflight_resolution = live_task.get("preflight_artifact_resolution", {})
    forbidden_roots = preflight_resolution.get("must_not_look_under")
    if isinstance(forbidden_roots, list) and forbidden_roots:
        kwargs["preflight_forbidden_roots"] = tuple(str(p) for p in forbidden_roots)
    if "bounded_recovery_lookup" in preflight_resolution:
        kwargs["preflight_bounded_recovery"] = bool(preflight_resolution["bounded_recovery_lookup"])

    artifact_paths = raw.get("artifact_paths", {})
    # Per-mode root keys take precedence; fall back to the legacy shared key.
    mode_root_key = {
        MORNING_WATCHLIST_MODE: "watchlist_run_state_root",
        MORNING_PREFLIGHT_MODE: "preflight_run_state_root",
        MORNING_LIVE_MODE: "live_run_state_root",
    }.get(mode)
    if mode_root_key and mode_root_key in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths[mode_root_key]
    elif "morning_run_state_root" in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths["morning_run_state_root"]
    # The 10:30 live gate reads the same-day pre-flight artifact from this root.
    if "preflight_run_state_root" in artifact_paths:
        kwargs["preflight_run_state_root"] = artifact_paths["preflight_run_state_root"]
    prior_roots = artifact_paths.get("prior_allocation_roots")
    if isinstance(prior_roots, list):
        # Preserve [] as () so an explicit empty array in JSON fully disables
        # the guard (None = not set = use default afternoon root at runtime).
        kwargs["prior_allocation_roots"] = tuple(str(r) for r in prior_roots)
    cap_roots = artifact_paths.get("cap_seed_roots")
    if isinstance(cap_roots, list) and cap_roots:
        kwargs["cap_seed_roots"] = tuple(str(r) for r in cap_roots)

    # Only apply the fail-closed per-order cap in live mode: watchlist/preflight runs
    # never reach broker placement, so a malformed budgets.json must not block the
    # 9:45 preflight from writing its validation artifact (which the 10:30 live gate
    # requires to proceed).
    if mode == MORNING_LIVE_MODE:
        max_order = load_max_per_order_usd(config_dir_path)
        if max_order is not None:
            kwargs["max_per_order_usd"] = max_order

    # Overlay tickers.json so config-only emergency exclusions take effect without
    # a code deploy (mirrors the afternoon load_strategy_config ticker overlay).
    ticker_cfg = load_tickers(config_dir_path)
    if "tickers" in ticker_cfg:
        kwargs["tickers"] = ticker_cfg["tickers"]
    if "excluded_tickers" in ticker_cfg:
        kwargs["excluded_tickers"] = ticker_cfg["excluded_tickers"]

    return MorningConfig(**kwargs)


def load_same_day_preflight(
    preflight_root: str, trading_day: date, *, bounded_recovery: bool = True
) -> Optional[Dict[str, Any]]:
    """Load the same-day 9:45 pre-flight artifact for the 10:30 live gate.

    Returns a dict ``{"run_id", "eligible_tickers": set[str], "run_state_path",
    "recovered_via_bounded_lookup"}`` when a same-day pre-flight
    ``run_state.json`` is found under ``preflight_root/YYYYMMDD_0945ET/`` (or
    via ``latest_preflight.json`` when it points at the same trading day, or
    via the bounded recovery scan below), else ``None``. The live executor
    uses the returned ``eligible_tickers`` to restrict which tickers it
    re-evaluates; a ``None`` result means "no pre-flight found" and the caller
    degrades to evaluating the full universe with a warning rather than
    blocking all buys.

    An artifact that explicitly declares ``overall_ok=False`` (a global/
    account/buying-power/auth/schema-write failure per the machine-complete
    contract) is treated the same as "no pre-flight found" -- None is
    returned -- rather than trusting whatever eligibility data happens to be
    present on a preflight run that flagged itself invalid.

    Eligibility is derived primarily from ``per_ticker_eligibility`` (the
    schema-required v2 field: every active-universe symbol with a ``symbol``
    key and an ``eligible_to_execute`` flag), falling back to the legacy
    ``eligible_to_execute_tickers`` convenience field only when
    ``per_ticker_eligibility`` is absent (an older artifact shape).

    ``bounded_recovery`` implements
    ``live_task.preflight_artifact_resolution.bounded_recovery_lookup``: when
    the primary path and the ``latest_preflight.json`` pointer both miss,
    perform exactly ONE additional lookup -- a directory scan of
    ``preflight_root`` for today's ``YYYYMMDD_0945ET`` directory -- before
    reporting "not found". This is a second, independent lookup path (scan vs.
    direct path join) so a regression in the primary path construction does
    not silently fail closed without ever finding an artifact that is actually
    on disk.
    """
    root = Path(preflight_root)
    expected_dir_name = f"{trading_day.strftime('%Y%m%d')}_0945ET"
    run_dir = root / expected_dir_name
    state_path = run_dir / "run_state.json"
    recovered_via_bounded_lookup = False
    if not state_path.exists():
        pointer_state_path: Optional[Path] = None
        pointer = root / "latest_preflight.json"
        if pointer.exists():
            try:
                pdata = json.loads(pointer.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pdata = None
            if pdata is not None and pdata.get("trading_day") == trading_day.isoformat():
                candidate = Path(pdata.get("run_state_path", ""))
                if candidate.exists():
                    pointer_state_path = candidate
        if pointer_state_path is not None:
            state_path = pointer_state_path
        elif bounded_recovery and root.is_dir():
            recovered_path = None
            for entry in sorted(root.iterdir()):
                if entry.name == expected_dir_name and entry.is_dir():
                    candidate = entry / "run_state.json"
                    if candidate.exists():
                        recovered_path = candidate
                    break
            if recovered_path is None:
                return None
            state_path = recovered_path
            recovered_via_bounded_lookup = True
        else:
            return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if state.get("overall_ok") is False:
        return None
    per_ticker = state.get("per_ticker_eligibility")
    if isinstance(per_ticker, list) and per_ticker:
        eligible = [
            row.get("symbol")
            for row in per_ticker
            if isinstance(row, dict) and row.get("eligible_to_execute") is True
        ]
    else:
        eligible = state.get("eligible_to_execute_tickers")
        if eligible is None:
            eligible = [
                row["ticker"]
                for row in state.get("signal_rows", [])
                if row.get("eligible_to_execute") is True
            ]
    return {
        "run_id": state.get("run_id"),
        "eligible_tickers": set(eligible),
        "run_state_path": str(state_path),
        "recovered_via_bounded_lookup": recovered_via_bounded_lookup,
    }


def _raw_json_safe(value: Any) -> Any:
    """Recursively convert a connector response to a JSON-serializable value.

    Handles the shapes returned by :class:`near_close_dca.MarketDataProvider`/
    :class:`near_close_dca.BrokerAdapter` implementations: ``Bar`` dataclasses,
    raw datetimes (e.g. inside a ``get_quote``/``get_bid_ask`` tuple), and
    nested lists/dicts of those. Anything already JSON-safe passes through.
    """
    if isinstance(value, Bar):
        return {
            "timestamp": value.timestamp.isoformat(),
            "open": value.open,
            "high": value.high,
            "low": value.low,
            "close": value.close,
            "volume": value.volume,
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _raw_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_raw_json_safe(v) for v in value]
    return value


def _restore_raw_datetimes(value: Any) -> Any:
    """Restore datetime values after a persisted JSON payload is read back."""
    if isinstance(value, list):
        return [_restore_raw_datetimes(item) for item in value]
    if isinstance(value, dict):
        return {key: _restore_raw_datetimes(item) for key, item in value.items()}
    if isinstance(value, str) and "T" in value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return value


def _bar_from_raw(value: Any) -> Bar:
    """Build a strategy bar exclusively from its persisted representation."""
    if not isinstance(value, dict):
        raise ValueError("persisted bar is not an object")
    timestamp = _restore_raw_datetimes(value.get("timestamp"))
    if not isinstance(timestamp, datetime):
        raise ValueError("persisted bar has no valid timestamp")
    return Bar(
        timestamp=timestamp,
        open=float(value["open"]),
        high=float(value["high"]),
        low=float(value["low"]),
        close=float(value["close"]),
        volume=float(value["volume"]),
    )


def _raw_envelope_reports_failure(envelope: Dict[str, Any]) -> bool:
    """Mirror ``dca_live_adapters._is_error_response`` for the raw-persist round trip.

    A connector wrapper can surface an outage as data (``{"error": ...}`` /
    ``{"success": false}`` / ``{"ok": false}``) rather than raising. That failure flag
    must not be discarded by ``_RawInputRecorder.persist()``'s envelope unwrapping --
    downstream broker safeguards check for it on the returned payload.
    """
    if envelope.get("error"):
        return True
    return any(envelope.get(flag) is False for flag in ("success", "ok"))


class _RawInputRecorder:
    """Persists connector responses used by a scheduled live run under ``raw/``.

    Implements ``live_schedule_hardening_2026_07_20``'s raw-input persistence
    contract (config/morning_dca.json) for the 10:30 morning live task (and,
    via inheritance, the 12:30 midday live task): before compute uses a
    connector response, that response is written to ``raw/`` so a downstream
    consumer never has to trust an in-memory value that was never durably
    recorded. Morning fetches data lazily per ticker (unlike the afternoon
    stream's upfront batch fetch), so persistence happens per-call rather than
    as one preceding batch step -- functionally equivalent (nothing is used
    for signal computation or order placement before its own response is on
    disk), just interleaved instead of front-loaded.

    A write failure (disk full, unwritable path, non-serializable payload) is
    never raised into the caller: it is recorded on :attr:`error` (first
    failure wins) so ``run()`` can fail the scheduled run closed at a safe
    checkpoint, rather than surfacing as an unrelated-looking crash deep
    inside signal evaluation or order placement.
    """

    def __init__(self, raw_dir: Path, trading_day_iso: str):
        self.raw_dir = raw_dir
        self.trading_day_iso = trading_day_iso
        self.saved_filenames: List[str] = []
        self.error: Optional[str] = None
        self._seq = 0

    def record_error(self, kind: str, key: str, exc: Exception) -> None:
        """Record a connector/normalization failure as a raw-input failure."""
        if self.error is None:
            self.error = f"failed to fetch or normalize {kind} for {key}: {exc}"

    def persist(self, kind: str, key: str, payload: Any) -> Any:
        """Persist *payload* and return the representation read back from disk."""
        self._seq += 1
        safe_key = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(key))
        filename = f"{kind}_{safe_key}_{self._seq}.json"
        try:
            path = save_raw_input(
                self.trading_day_iso,
                filename,
                _raw_json_safe(payload),
                raw_dir=self.raw_dir,
            )
            persisted = json.loads(Path(path).read_text(encoding="utf-8"))
            # save_raw_input() may wrap payloads in one or more {"data": ...} envelopes.
            # Unwrap the normalize_connector_payload envelope (normalized true/false) and
            # any nested carrier {"data": {...}} layers so computation sees the archived shape.
            # But an envelope that reports failure (e.g. {"success": false, "data": {"order":
            # {"id": "..."}}}) must keep that outer flag: RobinhoodBroker._response_failure()
            # inspects the round-tripped payload for it, and discarding it in favor of the
            # inner "data" would let a failed placement/lookup with an inner id or empty
            # orders list read back as success. Stop unwrapping at the first envelope that
            # reports failure instead.
            if (
                isinstance(persisted, dict)
                and "results" not in persisted
                and "data" in persisted
                and "normalized" in persisted
                and not _raw_envelope_reports_failure(persisted)
            ):
                persisted = persisted["data"]
            seen_ids: set[int] = set()
            while (
                isinstance(persisted, dict)
                and "results" not in persisted
                and isinstance(persisted.get("data"), dict)
                and not _raw_envelope_reports_failure(persisted)
            ):
                if id(persisted) in seen_ids:
                    break
                seen_ids.add(id(persisted))
                persisted = persisted["data"]
        except (OSError, ValueError, TypeError) as exc:
            if self.error is None:
                self.error = f"failed to persist {filename}: {exc}"
            return None
        self.saved_filenames.append(filename)
        return persisted


class _PersistingMarketDataProvider:
    """``MarketDataProvider`` decorator that persists every response it returns.

    Wraps ``get_previous_close_bar``/``get_intraday_bars``/``get_quote``/
    ``get_bid_ask`` -- the price-data calls ``evaluate_signal`` makes -- and
    forwards everything else (including the ``source`` attribute) to the
    wrapped provider unchanged.
    """

    def __init__(self, inner: Any, recorder: _RawInputRecorder):
        self._inner = inner
        self._recorder = recorder
        self._raw_connectors_wrapped = self._wrap_raw_connectors()

    def _wrap_raw_connectors(self) -> bool:
        """Intercept Robinhood and Finance connector callables before adapter parsing.

        Wraps a private *copy* of ``self._inner`` rather than mutating the caller-
        supplied adapter in place: the caller may reuse that same
        data-provider instance for a later manual or
        watchlist run, and a wrapper left bound to this run's (short-lived)
        ``_RawInputRecorder`` would then archive that later run's responses into this
        run's ``raw/`` directory -- or return ``None`` for a perfectly valid response
        if that directory has since become unwritable.
        """
        wrapped = False
        copied = False
        for attribute, kind in (
            ("_historicals_fn", "historicals"),
            ("_quotes_fn", "quotes"),
            ("_bars_fn", "intraday_bars"),
            ("_previous_close_fn", "prior_close"),
        ):
            connector = getattr(self._inner, attribute, None)
            if not callable(connector) or getattr(connector, "_raw_recorder", None) is self._recorder:
                continue
            connector = getattr(connector, "_raw_connector_original", connector)
            if not copied:
                self._inner = copy.copy(self._inner)
                copied = True

            def persisted_connector(*args: Any, _connector=connector, _kind=kind, **kwargs: Any) -> Any:
                symbols = kwargs.get("symbols") or (args[0] if args else ["unknown"])
                key = "-".join(str(symbol) for symbol in symbols) if isinstance(symbols, (list, tuple)) else str(symbols)
                try:
                    payload = _connector(*args, **kwargs)
                    return self._recorder.persist(_kind, key, payload)
                except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
                    self._recorder.record_error(_kind, key, exc)
                    raise

            persisted_connector._raw_recorder = self._recorder  # type: ignore[attr-defined]
            persisted_connector._raw_connector_original = connector  # type: ignore[attr-defined]
            setattr(self._inner, attribute, persisted_connector)
            wrapped = True
        return wrapped

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        try:
            bar = self._inner.get_previous_close_bar(ticker, as_of)
            if self._raw_connectors_wrapped:
                return bar
            persisted = self._recorder.persist("prior_close", ticker, bar)
            return _bar_from_raw(persisted) if persisted is not None else None
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("prior_close", ticker, exc)
            raise

    def get_intraday_bars(
        self, *, ticker: str, start: datetime, end: datetime, interval_minutes: int
    ) -> Sequence[Bar]:
        try:
            bars = self._inner.get_intraday_bars(
                ticker=ticker, start=start, end=end, interval_minutes=interval_minutes
            )
            if self._raw_connectors_wrapped:
                return bars
            persisted = self._recorder.persist("intraday_bars", ticker, list(bars))
            return [_bar_from_raw(item) for item in persisted] if persisted is not None else []
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("intraday_bars", ticker, exc)
            raise

    def get_quote(self, ticker: str, as_of: datetime) -> Any:
        get_quote = getattr(self._inner, "get_quote", None)
        if not callable(get_quote):
            return None
        try:
            quote = get_quote(ticker, as_of)
            if self._raw_connectors_wrapped:
                return quote
            persisted = self._recorder.persist("quote", ticker, quote)
            return _restore_raw_datetimes(persisted)
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("quote", ticker, exc)
            raise

    def get_bid_ask(self, ticker: str, as_of: datetime) -> Any:
        get_bid_ask = getattr(self._inner, "get_bid_ask", None)
        if not callable(get_bid_ask):
            return None
        try:
            result = get_bid_ask(ticker, as_of)
            if self._raw_connectors_wrapped:
                return result
            persisted = self._recorder.persist("bid_ask", ticker, result)
            return _restore_raw_datetimes(persisted)
        except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
            self._recorder.record_error("bid_ask", ticker, exc)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _BuyingPowerLookupError(RuntimeError):
    """Signal a broker buying-power lookup failure to the run orchestrator."""


class _PersistingBrokerAdapter:
    """``BrokerAdapter`` decorator that persists the read-only queries it serves.

    Wraps ``account_number``/``available_buying_power``/
    ``is_tradable_for_fractional_buy``/``same_day_strategy_order_exists`` --
    the broker-side inputs a live run consults before placing an order -- so
    portfolio/buying-power/tradability/same-day-order-existence responses are
    captured under ``raw/``. For adapters exposing injected connector callables,
    all connector results (including order actions and fill evidence) are
    intercepted before the adapter selects or converts values from them.
    """

    def __init__(self, inner: BrokerAdapter, recorder: _RawInputRecorder):
        self._inner = inner
        self._recorder = recorder
        self._account_persisted = False
        self._buying_power_persisted = False
        self._wrapped_connector_attributes = self._wrap_raw_connectors()

    def _wrap_raw_connectors(self) -> set[str]:
        """Persist injected broker connector responses before broker parsing.

        Wraps a private *copy* of ``self._inner`` rather than mutating the caller-
        supplied adapter in place: see ``_PersistingMarketDataProvider._wrap_raw_connectors``
        for why a wrapper bound to this run's short-lived ``_RawInputRecorder`` must
        never leak onto an adapter instance the caller may reuse for a later run.
        """
        kinds = {
            "_buying_power_fn": "buying_power",
            "_orders_fn": "orders_since_midnight",
            "_tradability_fn": "tradability",
            "_review_fn": "review",
            "_place_fn": "place",
            "_order_fill_fn": "order_fill",
        }
        wrapped: set[str] = set()
        copied = False
        for attribute, kind in kinds.items():
            connector = getattr(self._inner, attribute, None)
            if not callable(connector) or getattr(connector, "_raw_recorder", None) is self._recorder:
                continue
            connector = getattr(connector, "_raw_connector_original", connector)
            if not copied:
                self._inner = copy.copy(self._inner)
                copied = True

            def persisted_connector(*args: Any, _connector=connector, _kind=kind, **kwargs: Any) -> Any:
                key = kwargs.get("symbol") or kwargs.get("order_id") or "account"
                try:
                    payload = _connector(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - connectors raise arbitrary errors
                    self._recorder.record_error(_kind, str(key), exc)
                    raise
                persisted = self._recorder.persist(_kind, str(key), payload)
                if persisted is None and payload is not None and _kind == "place":
                    # The connector already submitted a real order -- self._recorder.error
                    # is now set (the run still fails closed on it below), but returning
                    # None here would also erase the only record of that already-completed
                    # broker-side action, letting place_market_buy report "no order id" for
                    # an order that in fact went through. Fall back to the in-memory
                    # response so the id still reaches _place_broker_orders and gets
                    # recorded as submitted before the run blocks.
                    return payload
                return persisted

            persisted_connector._raw_recorder = self._recorder  # type: ignore[attr-defined]
            persisted_connector._raw_connector_original = connector  # type: ignore[attr-defined]
            setattr(self._inner, attribute, persisted_connector)
            wrapped.add(attribute)
        return wrapped

    def account_number(self) -> str:
        value = self._inner.account_number()
        if not self._account_persisted:
            self._recorder.persist("account_number", "account", {"account_number": value})
            self._account_persisted = True
        return value

    def available_buying_power(self) -> float:
        value = self._inner.available_buying_power()
        if not self._buying_power_persisted and "_buying_power_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist("buying_power", "account", {"available_buying_power": value})
            self._buying_power_persisted = True
        return value

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        value = self._inner.same_day_strategy_order_exists(ticker, trading_day)
        if "_orders_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist(
                "orders_since_midnight",
                ticker,
                {"ticker": ticker, "trading_day": trading_day.isoformat(), "same_day_order_exists": value},
            )
        return value

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: Sequence[str]
    ) -> Any:
        """Return and persist the guarded top-up idempotency query response."""
        checker = getattr(self._inner, "same_day_strategy_order_exists_with_ref_id_prefix", None)
        if not callable(checker):
            raise RuntimeError("broker does not support same-stream ref-id duplicate checks for guarded top-ups")
        value = checker(ticker, trading_day, ref_id_prefixes)
        if "_orders_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist(
                "orders_since_midnight",
                ticker,
                {
                    "ticker": ticker,
                    "trading_day": trading_day.isoformat(),
                    "ref_id_prefixes": list(ref_id_prefixes),
                    "same_stream_order": value,
                },
            )
        return value

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        value = self._inner.is_tradable_for_fractional_buy(ticker)
        if "_tradability_fn" not in self._wrapped_connector_attributes:
            self._recorder.persist("tradability", ticker, {"ticker": ticker, "tradable": value})
        return value

    def review_market_buy(self, ticker: str, notional_usd: float):
        return self._inner.review_market_buy(ticker, notional_usd)

    def place_market_buy(self, ticker: str, notional_usd: float, ref_id: str) -> str:
        return self._inner.place_market_buy(ticker, notional_usd, ref_id)

    def get_order_fill(self, order_id: str):
        return self._inner.get_order_fill(order_id)

    def get_raw_placement_responses(self) -> List[Dict[str, Any]]:
        getter = getattr(self._inner, "get_raw_placement_responses", None)
        return getter() if callable(getter) else []

    def __getattr__(self, name: str) -> Any:
        # Forward optional, non-protocol broker methods (e.g.
        # same_day_strategy_order_exists_with_ref_id_prefix) that some
        # BrokerAdapter implementations support beyond the base protocol.
        return getattr(self._inner, name)


def _strip_template_segments(path: str) -> str:
    """Drop ``YYYYMMDD*`` template path segments (e.g. ``YYYYMMDD_1030ET``).

    ``must_not_look_under`` entries in config/morning_dca.json include the
    dated live-output directory as a template
    (``.../morning_dca/YYYYMMDD_1030ET/``); this normalizes that down to the
    actual root (``.../morning_dca``) so it can be compared against a
    configured ``preflight_run_state_root``.
    """
    segments = [seg for seg in path.rstrip("/").split("/") if "YYYYMMDD" not in seg]
    return "/".join(segments)


def _forbidden_preflight_root_reason(
    root: str, forbidden_roots: Sequence[str]
) -> Optional[str]:
    """Return a block reason if ``root`` resolves under a forbidden path, else None.

    Implements ``live_task.preflight_artifact_resolution.must_not_look_under``:
    the same-day 9:45 pre-flight artifact must never be read from the 10:30
    live output directory or a bare cron-id path (the Jul 20 2026 incident).
    """
    resolved = str(Path(root)).rstrip("/")
    for forbidden in forbidden_roots:
        normalized = _strip_template_segments(str(forbidden))
        if resolved == normalized or resolved.startswith(normalized + "/"):
            return (
                f"preflight_run_state_root {root!r} resolves under a forbidden "
                f"must_not_look_under path ({forbidden!r}); refusing to read the "
                "pre-flight artifact from there (see "
                "live_task.preflight_artifact_resolution in config/morning_dca.json)"
            )
    return None


class DCAExecutor:
    """Base class for class-based DCA executors over the strategy library.

    The historical afternoon path is functional (``near_close_dca.run_once`` /
    ``run_daily_compact``) and remains the authoritative live path. This base
    introduces the shared extension points a class-based executor needs — the
    sizing ladder, a run-state root, and a run-classification block — so new
    streams (starting with the morning live stream) can plug in without touching
    the afternoon code.

    Subclasses provide the concrete ladder via :attr:`sizing_config`, the artifact
    root via :meth:`run_state_dir`, and the classification via
    :meth:`run_classification`.
    """

    stream: str = "base"
    run_type: str = "dca"

    def multiplier_for_drop(self, drop_fraction: float) -> float:
        """Map a drop (fraction of previous close) to a buy multiplier."""
        return graded_buy_multiplier(drop_fraction, self.sizing_config)

    @property
    def sizing_config(self) -> StrategyConfig:  # pragma: no cover - abstract
        raise NotImplementedError

    def run_state_dir(self, now: datetime) -> Path:  # pragma: no cover - abstract
        raise NotImplementedError

    def run_classification(self, now: datetime) -> Dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError


class MorningDCAExecutor(DCAExecutor):
    """Morning graded-DCA executor: drop-vs-yesterday's-close evaluation.

    Evaluates each ticker's morning price weakness relative to yesterday's
    regular-session close and grades it on the shared $2-base 0x -> 5x live
    ladder. Two modes (see :data:`MORNING_PROFILES`):

    - ``watchlist`` (default, 9:45 AM ET): a read-only early-warning scan. It
      places NO orders and every dollar figure is hypothetical/watchlist-only.
    - ``live`` (10:30 AM ET): the approved Robinhood morning execution. A
      candidate whose price/drop qualifies but whose 60-minute intraday trend
      cannot be validated is flagged ``manual_review_candidate`` and NOT
      auto-bought (the loosened trend-unavailable rule is not implemented).

    Either mode writes ONLY under its morning run-state root and NEVER into the
    afternoon ce16ba6b artifact tree or ``latest_scheduled_run.json``.
    """

    stream = MORNING_STREAM
    # Stream-identity hooks. Subclasses (e.g. the midday executor) override these
    # to reuse the whole evaluate/build/write pipeline under a different stream
    # identity without duplicating it. The defaults reproduce the morning stream
    # exactly, so morning behavior is unchanged.
    PROFILES: Dict[str, Dict[str, Any]] = MORNING_PROFILES
    ledger_namespace: str = MORNING_STREAM
    ladder_name: str = "morning_0x_to_5x"
    signals_csv_name: str = "morning_signals.csv"
    latest_pointer_name: str = "latest_morning_run.json"
    manual_run_subdir: str = "manual_runs"
    budget_daily_key: str = "morning_daily_cap_usd"
    budget_weekly_key: str = "morning_weekly_cap_usd"
    budget_label: str = "morning"
    # Whether _seed_ledger_from_prior_runs should also fold manual_run_subdir/
    # reruns into this stream's OWN cap ledger. True for morning: a manual
    # rerun there is a rerun of this same scheduled task and should count
    # against this stream's own cap. Midday overrides this to False because
    # its manual_reruns/ folder holds post-12:30 backfill artifacts that are
    # documented (near_close_dca.py) as feeding ONLY the afternoon top-up
    # guard, never the midday stream's own scheduled cap.
    scan_manual_reruns_for_ledger: bool = True

    def __init__(
        self,
        config: Optional[MorningConfig] = None,
        ledger: Optional[MorningLedger] = None,
        *,
        mode: str = MORNING_WATCHLIST_MODE,
        config_dir: Optional[str] = None,
    ) -> None:
        if mode not in self.PROFILES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(self.PROFILES)}")
        self.mode = mode
        self.profile = self.PROFILES[mode]
        self.run_type = self.profile["run_type"]
        self.trades = self.profile["trades"]
        self.config = config if config is not None else self._load_default_config(config_dir, mode)
        self.ledger = ledger or MorningLedger(
            namespace=self.ledger_namespace,
            daily_cap_usd=self.config.morning_daily_cap_usd,
            weekly_cap_usd=self.config.morning_weekly_cap_usd,
        )
        self._sizing_config = self.config.as_strategy_config()
        self.state_root = self.config.morning_run_state_root or self.profile["state_root"]
        self.decision_time = self.config.decision_time_et or self.profile["decision_time_et"]
        # run_ids already folded into self.ledger by _seed_ledger_from_prior_runs
        # -- including this instance's OWN completed runs (added in run() right
        # after write_artifacts) -- so a retried/rerun run() on this SAME
        # executor instance never seeds the same on-disk artifact into the
        # ledger twice.
        self._seeded_prior_run_ids: set = set()
        # Submitted/filled candidate rows recovered from today's own fixed
        # scheduled run_state.json by the most recent _seed_ledger_from_prior_runs
        # call, so a scheduled retry can carry them forward into the artifact
        # it's about to overwrite. Reset at the top of every seeding call.
        self._prior_same_day_candidate_rows: List[Dict[str, Any]] = []
        # Tracks total amount already seeded per (frozenset(cap_roots), trading_day)
        # so repeated run() calls correctly pick up new fills without double-counting.
        self._cap_seeded_amounts: Dict[Tuple, float] = {}
        # Tracks amounts recorded directly into the ledger by record_buy_candidates
        # so cap-seeding doesn't double-count this executor's own submitted orders
        # once the filesystem scan later picks up the same on-disk artifact (e.g.
        # a manual run followed by a scheduled run the same day, or a scheduled
        # RETRY re-scanning its own earlier attempt's dated-slot artifact). Split
        # by manual vs. scheduled origin since cap_seed_roots may cover one
        # without covering the other (Codex review, PR #88: "Avoid double-
        # counting this executor's scheduled fills" -- widening cap_seed_roots
        # to include the scheduled dated slot, done for a separate PR #88 fix,
        # meant a scheduled run's own submission could now also be re-scanned,
        # but only the manual bucket was ever credited back out).
        self._own_recorded_manual: Dict[date, float] = {}
        self._own_recorded_scheduled: Dict[date, float] = {}
        # Always 0.0: cap seeding writes straight into self.ledger (see
        # _compute_top_up's cap_seed_roots block) rather than a separate
        # seeded-amount field, so remaining_daily/remaining_weekly already
        # reflect it. Kept only so the reporting/budget fields below never
        # need a None-check.
        self._prior_daily_spend_seeded = 0.0

    def _load_default_config(self, config_dir: Optional[str], mode: str) -> MorningConfig:
        """Load this executor's config when none is passed. Overridable per stream."""
        return load_morning_config(config_dir, mode=mode)

    # -- Top-up guard (same-day prior-allocation offset) ---------------------
    def _compute_top_up(
        self, now: datetime, signals: Sequence["MorningSignal"], *, manual: bool = False
    ) -> Dict[str, TopUpOutcome]:
        """Return per-ticker prior-allocation/top-up outcomes.

        Only active in ``MORNING_LIVE_MODE``.  Watchlist and preflight modes are
        analytics-only and return ``{}`` immediately.  For all live runs (scheduled
        and manual alike) the morning cap ledger is seeded from ``cap_seed_roots``
        so prior morning fills always reduce ``remaining_daily`` before orders are
        placed.  Per-ticker incremental top-up outcomes on the MORNING stream are
        computed only for scheduled runs — a manual morning recovery run relies on
        ``same_day_strategy_order_exists`` as its sole duplicate-order gate. Other
        streams (e.g. midday) keep computing per-ticker outcomes on manual retries
        too, since a manual midday retry always writes to its own fresh,
        uuid-suffixed artifact directory (see ``run_id()``) rather than overwriting
        the original run's ``run_state.json`` — so the guarded same-stream
        duplicate check must still run to avoid double-counting a same-stream
        order as a fresh submission (see the ``has_prior_allocation and manual``
        branch in ``_place_broker_orders``).  When ``config.prior_allocation_roots``
        is not set (None) scans the default afternoon root and the morning
        ``manual_runs`` subtree; returns an empty dict only when roots is
        explicitly set to ``[]`` (fully disabled).  Also seeds the cap ledger
        from ``cap_seed_roots`` regardless of per-ticker guard status.

        Cap seeding is intentionally separate from ``prior_allocation_roots`` so
        cross-stream fills (e.g. afternoon fills scanned for per-ticker top-up
        context) never erroneously reduce this stream's cap.
        """
        self._prior_daily_spend_seeded = 0.0
        if self.mode != MORNING_LIVE_MODE:
            return {}
        raw_roots = getattr(self.config, "prior_allocation_roots", None)
        # Track whether the operator explicitly disabled the per-ticker guard ([]
        # opt-out). Cap seeding must still run even when the guard is disabled, so
        # we defer the early return until after the cap-seeding block below.
        explicit_disable = raw_roots is not None and len(raw_roots) == 0
        configured = list(raw_roots) if raw_roots is not None else [DEFAULT_AFTERNOON_TASK_ROOT]
        # Append the manual_runs subtree of self.state_root only for the morning
        # stream. Midday manual reruns are prior context for the afternoon guard,
        # not for the midday scheduled run; skip to avoid scanning
        # midday_dca/manual_reruns as live prior allocation.
        # Also skip when a configured root already covers manual_root (ancestor)
        # to avoid load_prior_same_day_allocations_multi double-counting fills.
        manual_root = str(Path(self.state_root) / self.manual_run_subdir)
        morning_only = self.stream == MORNING_STREAM
        roots = []
        if not explicit_disable:
            already_covered = any(
                Path(manual_root).resolve().is_relative_to(Path(r).resolve()) for r in configured
            )
            if morning_only and not already_covered:
                roots = list(dict.fromkeys([*configured, manual_root]))
            else:
                roots = list(dict.fromkeys(configured))
        trading_day = now.astimezone(EASTERN).date()
        boundary_et = now.astimezone(EASTERN).time()
        # Seed the morning cap ledger for ALL live runs (scheduled and manual) so
        # prior morning fills always reduce remaining_daily before _place_broker_orders.
        # Use cap_seed_roots (morning-only roots) to avoid charging afternoon fills.
        # Incremental seeding: repeated calls pick up new fills without double-counting.
        # This runs even when prior_allocation_roots=[] disables the per-ticker guard.
        if self.stream == MORNING_STREAM:
            cap_roots = getattr(self.config, "cap_seed_roots", None)
            if cap_roots:
                seed_key = (frozenset(cap_roots), trading_day)
                cap_allocations = load_prior_same_day_allocations_multi(
                    cap_roots, trading_day, boundary_et=boundary_et
                )
                new_total = sum(
                    a.amount_usd
                    for a in cap_allocations.values()
                    if a.amount_usd > 0 and math.isfinite(a.amount_usd)
                )
                # Only credit each _own_recorded_* bucket when the corresponding
                # on-disk location is actually covered by cap_seed_roots -- manual
                # artifacts live under manual_root, scheduled artifacts live
                # directly under state_root (run_state_dir()). The two are gated
                # independently (not via a single state_root-covers-everything
                # check) because cap_seed_roots may cover one without the other,
                # e.g. a legacy/narrower config scanning only manual_runs/. When a
                # location isn't covered, its own fills are absent from new_total
                # so subtracting them would suppress external fill pickup.
                own_manual_in_scan = any(
                    Path(manual_root).is_relative_to(Path(r)) for r in cap_roots
                )
                own_scheduled_in_scan = any(
                    Path(self.state_root).is_relative_to(Path(r)) for r in cap_roots
                )
                own_amount = (
                    self._own_recorded_manual.get(trading_day, 0.0) if own_manual_in_scan else 0.0
                ) + (
                    self._own_recorded_scheduled.get(trading_day, 0.0) if own_scheduled_in_scan else 0.0
                )
                prev_total = self._cap_seeded_amounts.get(seed_key, 0.0) + own_amount
                incremental = new_total - prev_total
                if incremental > 0:
                    # Store only the external (non-own) portion so _own_recorded
                    # additions by later scheduled-run record_buy_candidates calls
                    # do not inflate prev_total beyond new_total and suppress
                    # pickup of subsequent external fills.
                    self._cap_seeded_amounts[seed_key] = new_total - own_amount
                    self.ledger.record_hypothetical(trading_day, incremental)
        # Per-ticker incremental outcomes on the MORNING stream: scheduled runs
        # only. A manual morning recovery run relies on same_day_strategy_order_exists
        # as its sole duplicate-order gate. Other streams (midday) keep the
        # guarded per-ticker check active on manual retries too.
        if manual and self.stream == MORNING_STREAM:
            return {}
        if explicit_disable:
            return {}
        prior_allocations = load_prior_same_day_allocations_multi(
            roots, trading_day, boundary_et=boundary_et
        )
        outcomes: Dict[str, TopUpOutcome] = {}
        for signal in signals:
            if signal.action != Action.BUY or signal.multiplier <= 0:
                continue
            outcomes[signal.ticker] = self._evaluate_top_up(
                signal, prior_allocations.get(signal.ticker)
            )
        return outcomes

    def _evaluate_top_up(
        self, signal: "MorningSignal", prior: Optional[PriorSameDayAllocation]
    ) -> TopUpOutcome:
        """Apply the same-day top-up rule to one live buy candidate.

        No earlier allocation → full desired amount.  Earlier allocation and
        current price strictly below the prior reference → incremental
        difference only (desired − prior; $0 if ≤ 0).  Price not below
        reference → $0.  Amount known but reference price missing → $0 with
        manual review flagged.
        """
        desired = round(signal.hypothetical_amount_usd, 2)
        price = signal.current_price

        def _outcome(
            incremental: float, result: str, *, skipped: bool, manual_review: bool = False
        ) -> TopUpOutcome:
            return TopUpOutcome(
                ticker=signal.ticker,
                desired_amount=desired,
                decision_price=price,
                prior_same_day_amount=round(prior.amount_usd, 2) if prior else 0.0,
                prior_same_day_reference_price=prior.reference_price if prior else None,
                prior_same_day_run_id=prior.run_id if prior else None,
                prior_same_day_source=prior.source if prior else None,
                incremental_order_amount=round(max(0.0, incremental), 2),
                top_up_guard_result=result,
                top_up_skipped=skipped,
                top_up_manual_review=manual_review,
            )

        if prior is None or prior.amount_usd <= 0:
            return _outcome(desired, TOP_UP_NO_PRIOR_RUN, skipped=desired <= 0)
        if prior.reference_price is None or price is None:
            return _outcome(0.0, TOP_UP_MISSING_PRIOR_PRICE, skipped=True, manual_review=True)
        if price >= prior.reference_price:
            return _outcome(0.0, TOP_UP_PRICE_NOT_BELOW, skipped=True)
        incremental = desired - round(prior.amount_usd, 2)
        if incremental <= 0:
            return _outcome(0.0, TOP_UP_NO_INCREMENTAL, skipped=True)
        return _outcome(incremental, TOP_UP_INCREMENTAL, skipped=False)

    # -- Reuse hooks ---------------------------------------------------------
    def _effective_buy_amount(
        self,
        signal: "MorningSignal",
        top_up_outcomes: Optional[Dict[str, Any]],
        broker_outcomes: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Dollar amount actually planned for a buy candidate.

        Prefers the real submitted_amount_usd from broker_outcomes (covers
        duplicate-found retry orders where dup_amount may differ from the
        pre-placement incremental_order_amount). Falls back to the incremental
        order amount when a top-up outcome exists, then the full graded amount.
        """
        if broker_outcomes:
            boc = broker_outcomes.get(signal.ticker)
            if boc is not None:
                submitted = boc.get("submitted_amount_usd")
                if submitted is not None:
                    return float(submitted)
        if top_up_outcomes:
            outcome = top_up_outcomes.get(signal.ticker)
            if outcome is not None:
                return outcome.incremental_order_amount
        return signal.hypothetical_amount_usd

    def _augment_buy_candidate_row(
        self, row: Dict[str, Any], signal: "MorningSignal", top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        """Attach top-up guard fields to a buy-candidate row when the guard ran."""
        outcome = top_up_outcomes.get(signal.ticker) if top_up_outcomes else None
        if outcome is None:
            return
        row["prior_same_day_amount"] = outcome.prior_same_day_amount
        row["prior_same_day_reference_price"] = outcome.prior_same_day_reference_price
        row["prior_same_day_run_id"] = outcome.prior_same_day_run_id
        row["prior_same_day_source"] = outcome.prior_same_day_source
        row["morning_desired_amount"] = outcome.desired_amount
        row["morning_decision_price"] = outcome.decision_price
        row["incremental_order_amount"] = outcome.incremental_order_amount
        row["top_up_guard_result"] = outcome.top_up_guard_result
        row["top_up_skipped"] = outcome.top_up_skipped
        row["top_up_manual_review"] = outcome.top_up_manual_review
        if outcome.top_up_manual_review:
            row["execution_status"] = EXEC_MANUAL_REVIEW_CANDIDATE

    def _augment_artifacts(
        self, artifacts: Dict[str, Any], top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        """Attach top-up summary fields to the run artifact when the guard ran."""
        if not top_up_outcomes:
            return
        outcomes = list(top_up_outcomes.values())
        top_up_manual_reviews = sorted(o.ticker for o in outcomes if o.top_up_manual_review)
        artifacts["top_up_manual_review_tickers"] = top_up_manual_reviews
        artifacts["top_up_offset_tickers"] = sorted(
            o.ticker for o in outcomes if o.top_up_skipped and not o.top_up_manual_review
        )
        existing_manual_review = set(artifacts.get("manual_review_tickers", []))
        new_top_up_manual = [t for t in top_up_manual_reviews if t not in existing_manual_review]
        if new_top_up_manual:
            artifacts["counts"]["manual_review_candidates"] = (
                artifacts["counts"].get("manual_review_candidates", 0) + len(new_top_up_manual)
            )
            artifacts["manual_review_tickers"] = (
                artifacts.get("manual_review_tickers", []) + new_top_up_manual
            )
            # Also add the full row dicts (not a re-derived minimal dict) so
            # downstream readers of manual_review_candidates see top-up
            # missing-price cases with the same shape as trend-unvalidated
            # cases (MorningSignal.to_row(): source, action, drop_pct,
            # multiplier, warnings, execution_status, trend_validated) plus
            # the top-up fields _attach_top_up_fields already set on the
            # buy_candidates row (prior_same_day_*, morning_desired_amount,
            # morning_decision_price, incremental_order_amount,
            # top_up_guard_result, top_up_skipped, top_up_manual_review).
            new_set = set(new_top_up_manual)
            extra_rows = [
                row for row in artifacts.get("buy_candidates", [])
                if row.get("ticker") in new_set
            ]
            if extra_rows:
                manual_rows = list(artifacts.get("manual_review_candidates", []))
                by_ticker = {r.get("ticker"): r for r in manual_rows if r.get("ticker")}
                for row in extra_rows:
                    ticker = row.get("ticker")
                    if ticker in by_ticker:
                        by_ticker[ticker].update(row)
                    else:
                        # row is one of the SAME dict objects living in
                        # artifacts["buy_candidates"] (extra_rows is filtered
                        # from it, not copied). Append a shallow copy so a
                        # later in-memory mutation of either array's row
                        # (e.g. a broker outcome status update) can't silently
                        # leak into the other before write_artifacts() serializes
                        # them (Gemini review, PR #88).
                        new_row = dict(row)
                        manual_rows.append(new_row)
                        by_ticker[ticker] = new_row
                artifacts["manual_review_candidates"] = manual_rows

    # -- DCAExecutor hooks ---------------------------------------------------
    @property
    def sizing_config(self) -> StrategyConfig:
        return self._sizing_config

    def run_state_dir(self, now: datetime) -> Path:
        now_et = now.astimezone(EASTERN)
        folder = f"{now_et.strftime('%Y%m%d')}_{self.profile['folder_suffix']}"
        return Path(self.state_root) / folder

    def run_id(self, now: datetime, *, manual: bool) -> str:
        now_et = now.astimezone(EASTERN)
        if manual:
            now_utc = now.astimezone(timezone.utc)
            # A short random suffix guarantees uniqueness even when two manual
            # replays share the same decision timestamp (down to the second),
            # so write_artifacts never silently overwrites a prior manual run.
            unique = uuid.uuid4().hex[:6]
            return f"{self.profile['manual_run_id_prefix']}_{now_utc.strftime('%Y%m%d_%H%M%S')}Z_{unique}"
        return f"{self.profile['scheduled_run_id_prefix']}_{now_et.strftime('%Y%m%d')}_{self.profile['folder_suffix']}"

    def run_classification(self, now: datetime, *, manual: bool = False) -> Dict[str, Any]:
        """Build the morning run-classification block for the active mode.

        A scheduled watchlist scan is ``run_type='morning_dca_watchlist_scan'``
        at 09:45; a scheduled live run is ``run_type='morning_dca_live'`` at
        10:30. Either way ``stream='morning'`` and ``valid_afternoon_evidence``
        is false, so the artifact is never afternoon scheduled evidence.
        """
        triggered_iso = now.astimezone(timezone.utc).isoformat()
        profile = self.profile
        classification: Dict[str, Any] = {
            "run_origin": "manual" if manual else "scheduled",
            "run_type": profile["run_type"],
            "stream": self.stream,
            "trades": profile["trades"],
            "cron_id": profile["cron_id"],
            "supersedes_cron_id": None,
            "scheduled_for_et": None if manual else profile["scheduled_for_et"],
            "scheduled_window_start_et": None if manual else profile["window_start_et"],
            "scheduled_window_end_et": None if manual else profile["window_end_et"],
            "manual_run": manual,
            "run_id": self.run_id(now, manual=manual),
            "triggered_at_utc": triggered_iso,
            "state_schema_version": profile["state_schema_version"],
            # The execution broker and account for all morning stream modes; always Robinhood.
            "execution_broker": "robinhood",
            "account_number": ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
            # Pre-authorized direct placement is only for scheduled LIVE runs; read-only
            # (watchlist/preflight) and manual runs are never pre-authorized.
            "preauthorized_direct_place": bool(profile["trades"]) and not manual,
            # Morning artifacts are valid morning evidence (and prior-allocation context
            # for the midday/afternoon top-up guard), but never afternoon evidence.
            # MiddayDCAExecutor inherits this method unchanged, so this must be
            # scoped to self.stream rather than hardcoded True -- a midday run
            # (self.stream == "midday") is midday prior-allocation context, not
            # morning evidence (Codex review: "Keep morning evidence flags off
            # midday runs").
            "valid_morning_evidence": self.stream == MORNING_STREAM,
            # Explicit, machine-checkable separation from the afternoon stream.
            "valid_afternoon_evidence": False,
            "afternoon_live_cron_id": AFTERNOON_LIVE_CRON_ID,
        }
        return classification

    # -- Evaluation ----------------------------------------------------------
    def evaluate_signal(self, ticker: str, now: datetime, data: Any) -> MorningSignal:
        """Grade one ticker's morning weakness vs. yesterday's close.

        Uses the provider's previous-close bar and the latest completed intraday
        bar (or a live quote via ``get_quote`` when available) at/or before the
        active mode's decision anchor (09:45 for the watchlist scan, 10:30 for
        the live execution). In live mode a qualifying candidate whose intraday
        trend cannot be validated is flagged ``manual_review_candidate`` and is
        not auto-bought. The watchlist scan places no orders.
        """
        source = getattr(getattr(data, "source", None), "value", "unknown")
        now_et = now.astimezone(EASTERN)
        trading_day = now_et.date()
        decision_dt = datetime.combine(trading_day, self.decision_time, tzinfo=EASTERN)
        anchor_label = self.profile["scheduled_for_et"]

        warnings: List[str] = []
        previous_close: Optional[float] = None
        current_price: Optional[float] = None

        try:
            prev_bar: Optional[Bar] = data.get_previous_close_bar(ticker, decision_dt)
        except Exception as exc:  # noqa: BLE001 - degrade to NO_DATA with a warning
            prev_bar = None
            warnings.append(f"previous_close provider error: {exc}")
        if prev_bar is not None and prev_bar.timestamp >= decision_dt:
            warnings.append(
                f"previous_close timestamp {prev_bar.timestamp.isoformat()} is not before "
                f"the {anchor_label} ET decision anchor; rejecting"
            )
            prev_bar = None
        if prev_bar is not None:
            if not math.isfinite(prev_bar.close) or prev_bar.close <= 0:
                warnings.append(f"previous close {prev_bar.close} is non-positive; rejecting")
            else:
                previous_close = prev_bar.close
        else:
            warnings.append("missing previous close")

        try:
            bars = data.get_intraday_bars(
                ticker=ticker,
                start=datetime.combine(trading_day, time(9, 30), tzinfo=EASTERN),
                end=decision_dt,
                interval_minutes=self.config.interval_minutes,
            )
        except Exception as exc:  # noqa: BLE001
            bars = []
            warnings.append(f"intraday provider error: {exc}")

        current_price = self._current_price(data, ticker, decision_dt, bars, warnings)

        if previous_close is None or not previous_close or current_price is None:
            return MorningSignal(
                ticker=ticker,
                source=source,
                action=Action.NO_DATA,
                reason="missing previous close or current price",
                current_price=current_price,
                previous_close=previous_close,
                drop_pct=None,
                multiplier=0.0,
                hypothetical_amount_usd=0.0,
                warnings=tuple(warnings),
            )

        drop_fraction = (previous_close - current_price) / previous_close
        drop_pct = drop_fraction * 100.0
        multiplier = self.multiplier_for_drop(drop_fraction)
        hypothetical = round(self.config.base_buy_amount_usd * multiplier, 2)
        execution_status: Optional[str] = None
        trend_validated: Optional[bool] = None
        mode_label = "watchlist" if self.mode == MORNING_WATCHLIST_MODE else "live"

        if drop_fraction < self.config.minimum_drop_vs_previous_close:
            action = Action.SKIP if current_price >= previous_close else Action.NO_ACTION
            reason = (
                "above/at prior close" if current_price >= previous_close
                else "below prior close but under 0.1% minimum drop"
            )
            multiplier = 0.0
            hypothetical = 0.0
        else:
            action = Action.BUY
            reason = f"morning weakness {drop_pct:.2f}% vs prior close -> {multiplier:g}x ({mode_label})"
            if self.profile["validate_trend"]:
                # Confidence rule: price/drop passed, but only auto-buy when the
                # intraday trend over the 60-minute window can be validated.
                # Otherwise flag for manual review instead of buying.
                trend_validated = self._trend_validated(bars, decision_dt, warnings)
                if trend_validated:
                    execution_status = EXEC_AUTO_BUY_ELIGIBLE
                else:
                    execution_status = EXEC_MANUAL_REVIEW_CANDIDATE
                    reason += "; intraday trend unvalidated -> manual_review_candidate (no auto-buy)"

        return MorningSignal(
            ticker=ticker,
            source=source,
            action=action,
            reason=reason,
            current_price=current_price,
            previous_close=previous_close,
            drop_pct=drop_pct,
            multiplier=multiplier,
            hypothetical_amount_usd=hypothetical,
            warnings=tuple(warnings),
            execution_status=execution_status,
            trend_validated=trend_validated,
        )

    def _trend_validated(
        self, bars: Sequence[Bar], decision_dt: datetime, warnings: List[str]
    ) -> bool:
        """Return True when the intraday trend over the window can be validated.

        The 10:30 live run exists to observe a true ~60-minute trend window
        (9:30-10:30 ET). Validation requires at least ``trend_min_bars``
        completed, positive-priced intraday bars in that window; when fewer are
        available the trend is treated as UNVALIDATED so the caller flags the
        candidate ``manual_review_candidate`` rather than auto-buying. The
        loosened "buy anyway when trend is unavailable" rule is not implemented.
        """
        bar_anchor = completed_bar_anchor(decision_dt, self.config.interval_minutes, self._sizing_config)
        interval = timedelta(minutes=self.config.interval_minutes)
        window_start = decision_dt - timedelta(minutes=60)
        freshest_allowed = bar_anchor - interval
        usable: Dict[Any, Bar] = {}
        for b in bars:
            if window_start <= b.timestamp <= bar_anchor and math.isfinite(b.close) and b.close > 0:
                usable[b.timestamp] = b
        sorted_bars = sorted(usable.values(), key=lambda b: b.timestamp)
        if not sorted_bars or sorted_bars[-1].timestamp < freshest_allowed:
            latest = sorted_bars[-1].timestamp.isoformat() if sorted_bars else "none"
            warnings.append(
                "intraday trend unvalidated: latest completed bar "
                f"{latest} is older than one interval before the decision anchor {bar_anchor.isoformat()}"
            )
            return False
        if len(usable) < self.config.trend_min_bars:
            warnings.append(
                f"intraday trend unvalidated: {len(usable)} usable bars < required "
                f"{self.config.trend_min_bars} over the latest 60-minute window"
            )
            return False
        if sorted_bars[-1].close >= sorted_bars[0].close:
            warnings.append(
                "intraday trend flat or rising: price is at or above open; skipping auto-buy"
            )
            return False
        return True

    def _current_price(
        self,
        data: Any,
        ticker: str,
        decision_dt: datetime,
        bars: Sequence[Bar],
        warnings: List[str],
    ) -> Optional[float]:
        bar_anchor = completed_bar_anchor(decision_dt, self.config.interval_minutes, self._sizing_config)
        bar = latest_bar_at_or_before(bars, bar_anchor)

        get_quote = getattr(data, "get_quote", None)
        if callable(get_quote):
            try:
                quote = get_quote(ticker, decision_dt)
                if quote is not None and isinstance(quote, (tuple, list)) and len(quote) >= 1:
                    quote_time = quote[1] if len(quote) >= 2 else None
                    if isinstance(quote_time, datetime) and bar_anchor <= quote_time <= decision_dt:
                        quote_price = float(quote[0])
                        if quote_price > 0:
                            return quote_price
                        warnings.append(
                            f"quote price {quote_price!r} is not positive; falling back to completed bar"
                        )
                    elif quote_time is None:
                        warnings.append(
                            "quote has no usable timestamp; falling back to completed bar"
                        )
                    elif quote_time > decision_dt:
                        warnings.append(
                            f"quote timestamp {quote_time!r} is after the {self.profile['scheduled_for_et']} ET "
                            "decision anchor; falling back to completed bar"
                        )
                    else:
                        warnings.append(
                            f"quote timestamp {quote_time!r} is older than the completed bar "
                            "anchor; falling back to completed bar"
                        )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"quote provider error: {exc}")
        if bar is None:
            warnings.append(f"missing completed intraday bar at/or before {self.profile['scheduled_for_et']} ET")
            return None
        freshest_allowed = bar_anchor - timedelta(minutes=self.config.interval_minutes)
        if bar.timestamp < freshest_allowed:
            warnings.append(
                f"latest completed intraday bar {bar.timestamp.isoformat()} is older than one interval before "
                f"the {self.profile['scheduled_for_et']} ET decision anchor; rejecting stale data"
            )
            return None
        if not math.isfinite(bar.close) or bar.close <= 0:
            warnings.append(f"completed bar close {bar.close!r} is not positive; rejecting")
            return None
        return bar.close

    def evaluate(self, now: datetime, data: Any, *, manual: bool = False) -> List[MorningSignal]:
        """Evaluate every non-excluded ticker for the morning stream.

        In morning live (10:30) mode the universe is first narrowed by the
        same-day 9:45 pre-flight gate: only tickers marked
        ``eligible_to_execute=true`` are re-evaluated. For scheduled
        (non-manual) runs a missing pre-flight artifact is fatal; for manual
        runs the gate degrades to the full universe with a warning.
        """
        tickers = [t for t in self.config.tickers if t not in self.config.excluded_tickers]
        tickers = self._apply_preflight_gate(now, tickers, manual=manual)
        return [self.evaluate_signal(t, now, data) for t in tickers]

    # Populated by :meth:`_apply_preflight_gate` for morning live runs so
    # build_artifacts can surface which tickers the pre-flight gate admitted.
    preflight_gate: Optional[Dict[str, Any]] = None

    def _apply_preflight_gate(self, now: datetime, tickers: List[str], *, manual: bool = False) -> List[str]:
        """Restrict the morning live universe to the same-day pre-flight verdict."""
        self.preflight_gate = None
        if not (self.mode == MORNING_LIVE_MODE and self.stream == MORNING_STREAM):
            return tickers
        trading_day = now.astimezone(EASTERN).date()
        root = self.config.preflight_run_state_root or DEFAULT_MORNING_PREFLIGHT_STATE_ROOT
        forbidden_roots = self.config.preflight_forbidden_roots or DEFAULT_PREFLIGHT_FORBIDDEN_ROOTS
        forbidden_reason = _forbidden_preflight_root_reason(root, forbidden_roots)
        if forbidden_reason is not None:
            # must_not_look_under: treat a misconfigured root the same as
            # "artifact not found" -- fail closed for scheduled runs -- rather
            # than silently reading from (or failing to find data in) the
            # 10:30 live output directory or a bare cron-id path.
            if not manual:
                self.preflight_gate = {
                    "preflight_found": False,
                    "preflight_missing_block": True,
                    "block_reason": forbidden_reason,
                }
                return []
            self.preflight_gate = {"preflight_found": False, "warning": forbidden_reason}
            return tickers
        loaded = load_same_day_preflight(
            root, trading_day, bounded_recovery=self.config.preflight_bounded_recovery
        )
        if loaded is None:
            if not manual:
                # Fail closed but write a durable blocked artifact so watchdog and
                # reporting can distinguish a missing-preflight block from a crash.
                self.preflight_gate = {
                    "preflight_found": False,
                    "preflight_missing_block": True,
                    "block_reason": (
                        "no same-day 9:45 pre-flight artifact found; "
                        "scheduled live run blocked (fail-closed)"
                    ),
                }
                return []
            self.preflight_gate = {
                "preflight_found": False,
                "warning": "no same-day 9:45 pre-flight artifact found; evaluating full universe",
            }
            return tickers
        eligible = loaded["eligible_tickers"]
        admitted = [t for t in tickers if t in eligible]
        gated_out = [t for t in tickers if t not in eligible]
        self.preflight_gate = {
            "preflight_found": True,
            "preflight_run_id": loaded["run_id"],
            "eligible_to_execute_tickers": sorted(eligible),
            "admitted_tickers": admitted,
            "gated_out_tickers": gated_out,
            "recovered_via_bounded_lookup": loaded.get("recovered_via_bounded_lookup", False),
        }
        return admitted

    @staticmethod
    def _buy_candidates(signals: Sequence[MorningSignal]) -> List[MorningSignal]:
        return [s for s in signals if s.action == Action.BUY and s.multiplier > 0]

    def record_buy_candidates(
        self,
        trading_day: date,
        buy_candidates: Sequence[MorningSignal],
        top_up_outcomes: Optional[Dict[str, Any]] = None,
        broker_outcomes: Optional[Dict[str, Any]] = None,
        *,
        _track_own: Optional[str] = None,
    ) -> None:
        """Record this run's buy candidates against the morning ledger.

        Call exactly once per run (``run()`` does this before building
        artifacts) -- ``build_artifacts`` itself is side-effect-free, so
        inspecting/rebuilding artifacts never double-counts hypothetical spend.
        The effective amount (after any stream-specific top-up offset) is what is
        recorded, so the ledger reflects what the stream actually plans to spend.

        ``broker_outcomes``, when provided, takes precedence for duplicate-found
        orders where dup_amount may differ from incremental_order_amount (see
        ``_effective_buy_amount``).

        ``_track_own="manual"`` or ``_track_own="scheduled"`` (set by ``run()``
        for a genuinely submitted batch) additionally records the amount in the
        matching ``_own_recorded_manual``/``_own_recorded_scheduled`` bucket so
        the cap-seeding watermark in ``_compute_top_up`` can exclude this
        executor's own submissions from the filesystem scan once it re-scans the
        same on-disk artifact. The two buckets use DIFFERENT accumulation
        semantics because manual and scheduled artifacts persist differently:

        - ``"manual"`` ACCUMULATES (``+=``) across calls: each manual run writes
          its own fresh, uuid-suffixed artifact directory (see ``run_id()``) that
          is never overwritten, so every past manual submission still sits on
          disk and the filesystem scan will find ALL of them.
        - ``"scheduled"`` REPLACES (not accumulates): the scheduled dated slot
          (``run_state_dir()``) is the SAME path every call and gets fully
          overwritten by each ``write_artifacts()``. A ticker "submitted" on an
          earlier attempt can become "skipped" on a later retry via the
          ordinary same-day duplicate-check path (the top-up-guard duplicate
          path is the only one that re-records it as "submitted" to preserve
          evidence) -- so the on-disk total can SHRINK between calls, not just
          grow, and only the CURRENT write's own batch should ever be credited.
        """
        if _track_own == "scheduled":
            # The scheduled dated-slot artifact is fully OVERWRITTEN by every
            # write_artifacts() call (unlike manual artifacts, which each get
            # their own unique uuid-suffixed file via run_id() and are never
            # overwritten) -- a ticker "submitted" on an earlier attempt this
            # trading day can become "skipped" on a later retry via the
            # ordinary same-day duplicate-check path in _place_broker_orders
            # (only the top-up-guard duplicate path re-records it as
            # "submitted" to preserve evidence). So the on-disk total
            # attributable to this executor's own writes can SHRINK between
            # calls, not just grow, and accumulating with += would keep
            # crediting an amount the filesystem scan can no longer see.
            # Replace with just this call's own batch total so a later
            # cap-seeding scan credits exactly what THIS write currently
            # puts on disk (Codex review, PR #88: "Do not subtract
            # overwritten scheduled submissions from cap seeding").
            self._own_recorded_scheduled[trading_day] = 0.0
        for candidate in buy_candidates:
            amt = self._effective_buy_amount(candidate, top_up_outcomes, broker_outcomes)
            self.ledger.record_hypothetical(trading_day, amt)
            if _track_own == "manual":
                self._own_recorded_manual[trading_day] = (
                    self._own_recorded_manual.get(trading_day, 0.0) + amt
                )
            elif _track_own == "scheduled":
                self._own_recorded_scheduled[trading_day] = (
                    self._own_recorded_scheduled.get(trading_day, 0.0) + amt
                )

    # -- Artifacts -----------------------------------------------------------
    def build_artifacts(
        self,
        now: datetime,
        signals: Sequence[MorningSignal],
        data_source: str,
        *,
        manual: bool = False,
        top_up_outcomes: Optional[Dict[str, Any]] = None,
        broker_outcomes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Assemble the morning run_state dict for the active mode.

        Pure/side-effect-free: reads the ledger but never mutates it. Callers
        must record buy candidates via :meth:`record_buy_candidates` beforehand
        for the ledger-derived fields (caps, remaining budget) to reflect this
        run. In live mode candidates whose intraday trend could not be validated
        are surfaced as ``manual_review_candidates`` rather than counted as buys.
        """
        now_et = now.astimezone(EASTERN)
        trading_day = now_et.date()
        is_live = self.mode == MORNING_LIVE_MODE
        is_preflight = self.mode == MORNING_PREFLIGHT_MODE

        signal_rows = [s.to_row() for s in signals]
        if is_preflight:
            for row, sig in zip(signal_rows, signals, strict=True):
                row["eligible_to_execute"] = preflight_eligibility(sig)
        buy_candidates = self._buy_candidates(signals)
        manual_review = [
            c for c in buy_candidates if c.execution_status == EXEC_MANUAL_REVIEW_CANDIDATE
        ]
        spend_candidates = [c for c in buy_candidates if c.execution_status != EXEC_MANUAL_REVIEW_CANDIDATE]

        hypothetical_spend = round(
            sum(self._effective_buy_amount(c, top_up_outcomes, broker_outcomes) for c in spend_candidates), 2
        )

        warnings: List[str] = []
        for sig in signals:
            warnings.extend(f"{sig.ticker}: {w}" for w in sig.warnings)
        if self.ledger.over_daily_cap(trading_day):
            warnings.append(
                f"hypothetical {self.budget_label} day spend {self.ledger.day_planned(trading_day):.2f} exceeds "
                f"{self.budget_label} daily cap {self.ledger.daily_cap_usd:.2f}"
            )
        if self.ledger.over_weekly_cap(trading_day):
            warnings.append(
                f"hypothetical {self.budget_label} week spend {self.ledger.week_planned(trading_day):.2f} exceeds "
                f"{self.budget_label} weekly cap {self.ledger.weekly_cap_usd:.2f}"
            )

        classification = self.run_classification(now, manual=manual)

        buy_candidate_rows = []
        for c in spend_candidates:
            row = c.to_row()
            self._augment_buy_candidate_row(row, c, top_up_outcomes)
            buy_candidate_rows.append(row)

        artifacts = {
            "run_id": classification["run_id"],
            "stream": self.stream,
            "mode": self.profile["artifact_mode"],
            "trades": self.trades,
            "generated_at": now_et.isoformat(),
            "trading_day": trading_day.isoformat(),
            "iso_week": list(iso_week_key(trading_day)),
            "data_source": data_source,
            "ladder": {
                "name": self.ladder_name,
                "base_amount_usd": self.config.base_buy_amount_usd,
                "tiers": [list(t) for t in self.config.sizing_tiers],
                "no_buy_below_pct": self.config.minimum_drop_vs_previous_close * 100.0,
            },
            "budget": {
                "ledger_namespace": self.ledger.namespace,
                self.budget_daily_key: self.ledger.daily_cap_usd,
                self.budget_weekly_key: self.ledger.weekly_cap_usd,
                "hypothetical_planned_spend_usd": hypothetical_spend,
                "hypothetical_day_spend_usd": round(self.ledger.day_planned(trading_day), 2),
                "hypothetical_week_spend_usd": round(self.ledger.week_planned(trading_day), 2),
                "prior_same_day_spend_usd": round(self._prior_daily_spend_seeded, 2),
                "remaining_daily_usd": round(
                    max(0.0, self.ledger.remaining_daily(trading_day) - self._prior_daily_spend_seeded), 2
                ),
                "remaining_weekly_usd": round(
                    max(0.0, self.ledger.remaining_weekly(trading_day) - self._prior_daily_spend_seeded), 2
                ),
            },
            "counts": {
                "evaluated": len(signals),
                # Aligned with len(buy_candidate_rows)/the "buy_candidates" artifact
                # list below (both built from spend_candidates), not the raw
                # BUY-actioned signal count -- dca_weekly_dashboard.py sums this
                # count directly, so a withheld manual-review row must not inflate
                # it (Codex review on the PR #43 merge).
                "buy_candidates": len(spend_candidates),
                "no_data": sum(1 for s in signals if s.action == Action.NO_DATA),
                "manual_review_candidates": len(manual_review),
            },
            "run_classification": classification,
            "signal_rows": signal_rows,
            "buy_candidates": buy_candidate_rows,
            "warnings": warnings,
        }
        if is_live:
            # Live-mode confidence rule: expose the trend-unvalidated candidates
            # that were withheld from auto-buy for downstream manual review.
            # Attach top-up guard fields the same way spend_candidates rows do:
            # _compute_top_up only checks action/multiplier (not trend_validated),
            # so a trend-unvalidated BUY can still have a real prior-allocation/
            # top-up outcome a reviewer needs to see (Codex review on the PR #43
            # merge).
            manual_review_rows = []
            for c in manual_review:
                row = c.to_row()
                self._augment_buy_candidate_row(row, c, top_up_outcomes)
                manual_review_rows.append(row)
            artifacts["manual_review_candidates"] = manual_review_rows
            artifacts["manual_review_tickers"] = [c.ticker for c in manual_review]
            if self.preflight_gate is not None:
                artifacts["preflight_gate"] = self.preflight_gate
                if not self.preflight_gate.get("preflight_found"):
                    # The manual degrade-to-full-universe path sets "warning"; the
                    # scheduled fail-closed block sets "block_reason" instead.
                    warnings.append(
                        self.preflight_gate.get("warning") or self.preflight_gate.get("block_reason")
                    )
        if is_preflight:
            # Pre-flight verdict: which tickers the 10:30 live evaluator should
            # re-evaluate. Coarse pre-screen only; 10:30 recomputes fresh.
            eligible = [s for s in signals if preflight_eligibility(s)]
            artifacts["eligible_to_execute_tickers"] = [s.ticker for s in eligible]
            artifacts["counts"]["eligible_to_execute"] = len(eligible)
            # Machine-complete envelope (schema morning-preflight-v2-machine-complete):
            # one row per active symbol plus top-level gate fields the 10:30 live
            # task consumes without re-deriving. Unresolved symbols become rows
            # with eligible_to_execute=false; they never flip overall_ok.
            ticker_rows = [preflight_ticker_eligibility_row(s) for s in signals]
            unresolved = [
                r["symbol"] for r in ticker_rows
                if r["data_confidence_status"] != "resolved"
            ]
            blocking_errors: List[str] = []
            artifacts["per_ticker_eligibility"] = ticker_rows
            artifacts["eligible_count"] = sum(1 for r in ticker_rows if r["eligible_to_execute"])
            artifacts["ineligible_count"] = sum(1 for r in ticker_rows if not r["eligible_to_execute"])
            artifacts["unresolved_tickers"] = unresolved
            artifacts["blocking_errors"] = blocking_errors
            # overall_ok is false ONLY for global/account/auth/schema-write
            # failures; per-symbol unresolved/ineligible tickers do not count.
            artifacts["overall_ok"] = not blocking_errors
        self._augment_artifacts(artifacts, top_up_outcomes)
        return artifacts

    def write_artifacts(self, artifacts: Dict[str, Any], now: datetime) -> Dict[str, str]:
        """Write morning run_state.json (+ CSV) under the morning run-state root.

        Writes ONLY under the morning root and, for scheduled runs, updates the
        morning-only ``latest_morning_run.json`` pointer. It never writes into the
        afternoon ce16ba6b tree or touches ``latest_scheduled_run.json``.
        """
        manual_run = artifacts["run_classification"]["manual_run"]
        out = (
            Path(self.state_root) / self.manual_run_subdir / artifacts["run_id"]
            if manual_run
            else self.run_state_dir(now)
        )
        out.mkdir(parents=True, exist_ok=True)

        run_state_path = out / "run_state.json"
        run_state_path.write_text(json.dumps(artifacts, indent=2, sort_keys=True), encoding="utf-8")

        signals_path = out / self.signals_csv_name
        _write_csv(signals_path, artifacts["signal_rows"])

        paths = {"run_state": str(run_state_path), "signals": str(signals_path)}

        # budget_ledger.bootstrap (added Jul 20 2026, see config/morning_dca.json):
        # persist a budget_ledger.json snapshot of the morning ledger under the
        # live task's own output directory. No pre-existing file is required --
        # this always (re)writes the snapshot from the caps/spend already
        # computed into artifacts["budget"], so a missing ledger on disk is
        # bootstrapped rather than treated as an escalation condition.
        if self.mode == MORNING_LIVE_MODE and self.stream == MORNING_STREAM:
            ledger_path = out / self.config.budget_ledger_filename
            ledger_path.write_text(
                json.dumps(artifacts["budget"], indent=2, sort_keys=True), encoding="utf-8"
            )
            paths["budget_ledger"] = str(ledger_path)

        # Write submitted_orders.csv so detect_drift() can build submitted_symbols.
        # Without this file the detector's submitted set is empty and every auto-buy
        # signal is falsely reported as an unsubmitted order.
        broker_outcomes: Dict[str, Any] = artifacts.get("broker_outcomes") or {}
        submitted_rows = [
            {
                "ticker": ticker,
                "order_id": outcome.get("order_id"),
                "submitted_amount_usd": outcome.get("submitted_amount_usd"),
                "status": outcome.get("status"),
            }
            for ticker, outcome in broker_outcomes.items()
            if outcome.get("status") == "submitted"
        ]
        if submitted_rows:
            orders_path = out / "submitted_orders.csv"
            _write_csv(orders_path, submitted_rows)
            paths["submitted_orders"] = str(orders_path)

        is_preflight = self.mode == MORNING_PREFLIGHT_MODE
        if is_preflight:
            # validation.csv: the per-ticker eligible_to_execute verdict.
            validation_path = out / "validation.csv"
            _write_csv(validation_path, artifacts["signal_rows"])
            paths["validation"] = str(validation_path)
            # run_summary.json: a compact roll-up of the pre-flight verdict.
            summary_path = out / "run_summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "run_id": artifacts["run_id"],
                        "trading_day": artifacts["trading_day"],
                        "stream": self.stream,
                        "mode": artifacts["mode"],
                        "counts": artifacts["counts"],
                        "eligible_to_execute_tickers": artifacts.get("eligible_to_execute_tickers", []),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            paths["run_summary"] = str(summary_path)

        orders_rows = [
            c for c in artifacts.get('buy_candidates', []) if c.get('order_id')
        ]
        if orders_rows:
            orders_csv_path = out / "orders.csv"
            _write_csv(
                orders_csv_path,
                [
                    {
                        "ticker": c.get("ticker", ""),
                        "order_id": c.get("order_id", ""),
                        "notional_usd": c.get("planned_amount_usd", c.get("hypothetical_amount_usd", "")),
                        "status": c.get("status", ""),
                        "avg_fill_price": "",
                        "quantity_filled": "",
                    }
                    for c in orders_rows
                ],
            )
            paths["orders_csv"] = str(orders_csv_path)

        pointer_name = "latest_preflight.json" if is_preflight else self.latest_pointer_name
        if not artifacts["run_classification"]["manual_run"]:
            pointer = Path(self.state_root) / pointer_name
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(
                json.dumps(
                    {
                        "run_id": artifacts["run_id"],
                        "run_state_path": str(run_state_path),
                        "trading_day": artifacts["trading_day"],
                        "stream": self.stream,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            paths[pointer_name.replace(".json", "")] = str(pointer)

        return paths

    def _blocked_live_result(
        self,
        now: datetime,
        block_reason: str,
        signals: Sequence[MorningSignal],
        source: str,
        *,
        error_class: Optional[str] = None,
        raw_recorder: Optional["_RawInputRecorder"] = None,
    ) -> Dict[str, Any]:
        """Write and return a fail-closed live-run result before broker placement.

        This is used for scheduler-wiring safety failures (for example no broker
        adapter or the wrong live account) where no ordering should proceed, but
        operators still need a durable run_state explaining why the scheduled
        live stream did not submit orders. Routed through the normal
        build_artifacts()/write_artifacts() pipeline -- mirroring the preflight-
        missing-block path above -- rather than a hand-rolled stub, so the
        persisted state still carries run_id, generated_at, counts, and the
        signal/candidate arrays already evaluated for this run, and the latest
        scheduled pointer is still updated: watchdog/latest-pointer consumers
        see this as an intentional block rather than a malformed or missing run.

        ``raw_recorder``, when passed by a raw-persistence-failure caller, stamps
        the same ``raw_input_dir``/``raw_inputs_saved``/``raw_persistence_error``
        fields onto the blocked artifact that a normal completed run carries
        (see the end of ``run()``) -- this early-return path bypasses that
        block entirely, so without this the documented machine-readable
        raw-persistence fields would silently disappear from blocked scheduled
        states, even though ``block_reason``/``error_class`` still describe the
        failure in prose.

        Before writing a fresh blocked stub, checks whether this scheduled
        slot's run_state.json already exists and carries genuine submitted-order
        evidence (a buy_candidates row or broker_outcomes entry with
        status == "submitted") from an earlier attempt this run. If so, that
        submitted evidence is kept, but THIS attempt's block_reason/error_class
        (and raw-persistence fields) are stamped onto it and it is rewritten to
        disk -- returning it completely unchanged (as before) hid a genuine
        current-attempt failure behind whatever status the earlier attempt
        happened to write, so a retry that failed for a NEW reason (e.g. a
        broker-side raw write) looked indistinguishable from a clean run to
        operators and watchdogs. An existing state that is unreadable, not a
        dict, or has no submitted evidence is NOT preserved -- it is
        overwritten with the fresh block_reason, so a second, different wiring
        failure is never hidden behind a stale first one.

        ``error_class`` optionally stamps a concrete, machine-checkable code
        (e.g. ``"missing_or_invalid_raw_inputs"``) alongside the human-readable
        ``block_reason``; omitted for the pre-existing wiring-failure callers.
        """
        existing = self._existing_submitted_run_state(now)
        if existing is not None:
            existing_artifacts = existing["artifacts"]
            # Merge the preserved submitted evidence into a FRESH artifact
            # built the normal way, rather than rewriting the raw on-disk
            # dict directly: _existing_submitted_run_state() validates only
            # the buy_candidates/broker_outcomes containers, so a
            # structurally partial existing file (missing e.g.
            # run_classification/budget/signal_rows) would raise KeyError if
            # handed straight to write_artifacts() (Codex review, PR #420:
            # "Validate preserved artifacts before rewriting them"). Building
            # fresh also means this attempt's own error_class/
            # raw_persistence_error are never contaminated by a DIFFERENT
            # stale failure the preserved artifact may have recorded (Codex
            # review, PR #420: "Clear stale failure metadata when rewriting a
            # retry") -- only what is set below, for THIS attempt, appears.
            artifacts = self.build_artifacts(now, signals, source, manual=False)
            preserved_candidates = existing_artifacts.get("buy_candidates")
            if isinstance(preserved_candidates, list) and preserved_candidates:
                artifacts["buy_candidates"] = preserved_candidates
                artifacts["counts"]["buy_candidates"] = len(preserved_candidates)
            preserved_outcomes = existing_artifacts.get("broker_outcomes")
            if isinstance(preserved_outcomes, dict) and preserved_outcomes:
                artifacts["broker_outcomes"] = preserved_outcomes
            artifacts["status"] = "blocked"
            artifacts["block_reason"] = block_reason
            if error_class is not None:
                artifacts["error_class"] = error_class
            if raw_recorder is not None:
                artifacts["raw_input_dir"] = str(raw_recorder.raw_dir)
                artifacts["raw_inputs_saved"] = raw_recorder.saved_filenames
                if raw_recorder.error:
                    artifacts["raw_persistence_error"] = raw_recorder.error
            # write_artifacts() derives the same dated-slot output directory
            # from artifacts["run_classification"]/now, so this overwrites
            # the same run_state.json (+ CSVs) and refreshes the scheduled
            # latest pointer to it -- downstream consumers still see a valid,
            # intentional run for this slot, now describing the CURRENT
            # attempt's failure (with the earlier submission still intact)
            # rather than a stale, unchanged one.
            paths = self.write_artifacts(artifacts, now)
            if self.mode == MORNING_LIVE_MODE and self.trades:
                _run_drift_check(Path(paths["run_state"]).parent)
            return {"artifacts": artifacts, "paths": paths}
        artifacts = self.build_artifacts(now, signals, source, manual=False)
        artifacts["status"] = "blocked"
        artifacts["block_reason"] = block_reason
        if error_class is not None:
            artifacts["error_class"] = error_class
        artifacts["broker_outcomes"] = {}
        if raw_recorder is not None:
            artifacts["raw_input_dir"] = str(raw_recorder.raw_dir)
            artifacts["raw_inputs_saved"] = raw_recorder.saved_filenames
            if raw_recorder.error:
                artifacts["raw_persistence_error"] = raw_recorder.error
        paths = self.write_artifacts(artifacts, now)
        # A blocked scheduled live run may have committed signals but placed no
        # orders — exactly the scenario the drift detector guards against. Run the
        # check so operators receive an immediate alert rather than a silent gap.
        if self.mode == MORNING_LIVE_MODE and self.trades:
            _run_drift_check(Path(paths["run_state"]).parent)
        return {"artifacts": artifacts, "paths": paths}

    def _existing_submitted_run_state(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Return the existing scheduled-slot run_state if it has submitted evidence.

        Returns ``None`` (nothing to preserve) when the file is missing, unreadable,
        not a JSON object, or has no buy_candidates/broker_outcomes entry with
        status == "submitted".
        """
        state_path = self.run_state_dir(now) / "run_state.json"
        if not state_path.exists():
            return None
        try:
            existing_artifacts = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            _log.warning(
                "existing run_state.json at %s is corrupted or unreadable (%s); "
                "overwriting with a fresh blocked stub",
                state_path,
                exc,
            )
            return None
        if not isinstance(existing_artifacts, dict):
            return None
        # .get(key, default) only substitutes the default when the key is
        # ABSENT; a structurally corrupted but parseable existing file with
        # "buy_candidates": null or "broker_outcomes": [] still returns that
        # non-list/non-dict value, and iterating/.values()-ing it would raise
        # instead of falling through to the fresh blocked stub below.
        buy_candidates = existing_artifacts.get("buy_candidates")
        if not isinstance(buy_candidates, list):
            buy_candidates = []
        broker_outcomes = existing_artifacts.get("broker_outcomes")
        if not isinstance(broker_outcomes, dict):
            broker_outcomes = {}
        has_submitted = any(
            isinstance(row, dict) and row.get("status") == "submitted" for row in buy_candidates
        ) or any(
            isinstance(outcome, dict) and outcome.get("status") == "submitted"
            for outcome in broker_outcomes.values()
        )
        if not has_submitted:
            return None
        return {"artifacts": existing_artifacts, "paths": {"run_state": str(state_path)}}

    def _maybe_block_for_holiday(self, now: datetime, *, manual: bool) -> Optional[Dict[str, Any]]:
        """Return a blocked-run result when ``now`` falls on a weekend or NYSE holiday.

        A cron fire on a weekend or full market closure has no real session to
        evaluate. Unlike near_close_dca.MARKET_HOLIDAYS (scoped to the afternoon
        live-test window and hand-maintained), this stream runs indefinitely with
        no end date, so it uses the computed nyse_holidays() calendar to stay
        correct into future years without manual updates. Builds the blocked
        result through the normal artifact pipeline (``build_artifacts``) so the
        persisted run_state carries the standard shape (run_id, generated_at,
        counts, signal/candidate arrays) and watchdog/reporting consumers see a
        well-formed blocked run rather than a minimal stub. The latest-run pointer
        is NOT advanced so it continues to point at the last real trading session.
        Returns None on a normal trading day.
        """
        trading_day = now.astimezone(EASTERN).date()
        is_weekend = trading_day.weekday() >= 5
        is_holiday = trading_day in nyse_holidays(trading_day.year)
        approved = self.config.approved_test_dates
        is_outside_window = (
            self.mode == MORNING_LIVE_MODE
            and bool(approved)
            and trading_day not in approved
        )
        if manual or (not is_weekend and not is_holiday and not is_outside_window):
            return None
        if is_weekend:
            block_reason = "weekend"
        elif is_holiday:
            block_reason = "market holiday"
        else:
            block_reason = "outside approved trading window"
        # Guard: if this date slot already has submitted-order evidence (e.g. the
        # 10:30 live run placed orders and then trading_window.json was briefly
        # malformed, making a retry fire with is_outside_window=True), never
        # overwrite that evidence — midday's prior-allocation scan reads this root
        # and losing submitted orders would allow a duplicate allocation.
        existing = self._existing_submitted_run_state(now)
        if existing is not None:
            return existing
        artifacts = self.build_artifacts(now, [], "none", manual=manual)
        artifacts["status"] = "blocked"
        artifacts["block_reason"] = block_reason
        artifacts["trades"] = False
        artifacts["broker_outcomes"] = {}
        out = self.run_state_dir(now)
        out.mkdir(parents=True, exist_ok=True)
        state_path = out / "run_state.json"
        # Overwrite any stale minimal stub: the blocked content is deterministic
        # for a given date and an older stub without run_id/counts/run_classification
        # would otherwise persist indefinitely. Submitted-evidence cases are
        # already returned above.
        state_path.write_text(
            json.dumps(artifacts, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {"artifacts": artifacts, "paths": {"run_state": str(state_path)}}

    def _seed_ledger_from_prior_runs(self, now: datetime) -> None:
        """Fold already-submitted same-day/same-week spend into the ledger.

        ``MorningLedger`` is in-memory and this executor is normally instantiated
        fresh per invocation (a new cron process each time), so a retried or
        manually rerun live task would otherwise see an empty ledger and could
        re-spend a cap that an earlier run already spent against. This scans
        this stream's own live run-state artifacts under this stream's
        scheduled-folder naming for every day in the current ISO week up to and
        including today -- the fixed scheduled path for today specifically (a
        retry overwrites that same path, so the pre-retry file must be read
        before that happens) -- plus, when :attr:`scan_manual_reruns_for_ledger`
        is true, any ``manual_run_subdir`` reruns.

        Each already ``submitted``/``filled`` buy candidate's amount from an
        earlier day in the same ISO week folds into ``planned_by_week`` only
        (the daily bucket stays scoped to today, and that day already ended).
        This guards the weekly cap across a fresh process starting later in
        the week. A file dated today folds into both ``planned_by_day`` and
        ``planned_by_week`` (via ``record_hypothetical``) UNLESS this stream
        has ``cap_seed_roots`` configured, in which case ``_compute_top_up``'s
        cap_seed_roots scan already seeds today's own-stream spend from the
        same on-disk artifacts and this method must not seed it a second time.

        Guards against double-counting a run's own on-disk artifact if the
        SAME executor instance calls ``run()`` again later: ``run()`` adds its
        own ``run_id`` to ``self._seeded_prior_run_ids`` right after writing,
        so a later same-instance seeding pass recognizes and skips it (its
        spend is already reflected in ``self.ledger`` from that first call).
        A different (fresh) instance scanning the same file for the first time
        still folds it in correctly.

        Also records today's own submitted/filled rows from the fixed
        scheduled path (not manual reruns, which write to unique paths and are
        never overwritten) into ``self._prior_same_day_candidate_rows`` so
        ``run()`` can carry them forward into the artifact it's about to
        overwrite on a same-day scheduled retry.

        Watchlist/preflight modes never place real orders, so caps are never
        enforced there and this is a no-op outside live mode.
        """
        self._prior_same_day_candidate_rows = []
        if self.mode != MORNING_LIVE_MODE:
            return
        trading_day = now.astimezone(EASTERN).date()
        this_week = iso_week_key(trading_day)
        state_root = Path(self.state_root)
        folder_suffix = self.profile["folder_suffix"]
        # _compute_top_up's cap_seed_roots scan already seeds TODAY's own-stream
        # spend (both planned_by_day and planned_by_week) when cap_seed_roots is
        # configured AND actually covers the scheduled dated-slot path --
        # seeding it again here would double-count the same on-disk artifact.
        # Only skip today's amount in that case; a narrower cap_seed_roots
        # config that covers only manual_runs/ (explicitly supported by
        # _compute_top_up's own_manual_in_scan/own_scheduled_in_scan split)
        # never sees the scheduled slot, so this method must remain the one
        # seeding it or a scheduled retry could re-spend the full cap.
        _cap_seed_roots = (
            getattr(self.config, "cap_seed_roots", None) if self.stream == MORNING_STREAM else None
        )
        _today_seeded_by_cap_roots = bool(_cap_seed_roots) and any(
            Path(self.state_root).is_relative_to(Path(r)) for r in _cap_seed_roots
        )

        scheduled_path = self.run_state_dir(now) / "run_state.json"
        candidate_paths: List[Tuple[Path, bool]] = []
        week_start = trading_day - timedelta(days=trading_day.weekday())
        day = week_start
        while day <= trading_day:
            path = state_root / f"{day.strftime('%Y%m%d')}_{folder_suffix}" / "run_state.json"
            if path.exists():
                candidate_paths.append((path, path == scheduled_path))
            day += timedelta(days=1)

        # Cache parsed manual-run states to avoid re-reading files in the main loop.
        _manual_state_cache: Dict[Path, Dict[str, Any]] = {}
        if self.scan_manual_reruns_for_ledger:
            manual_root = state_root / self.manual_run_subdir
            if manual_root.exists():
                filtered_manual: List[Tuple[Path, bool]] = []
                for p in sorted(manual_root.glob("*/run_state.json")):
                    try:
                        raw_state = json.loads(p.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    _manual_state_cache[p] = raw_state
                    filtered_manual.append((p, False))
                candidate_paths.extend(filtered_manual)

        for path, is_scheduled_today in candidate_paths:
            if path in _manual_state_cache:
                state = _manual_state_cache[path]
            else:
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
            # A valid JSON document whose root is a list, string, or other
            # non-object value (e.g. a partially recovered artifact) has no
            # .get() -- skip it rather than raising AttributeError and
            # crashing the retry before it can reach the fail-closed path.
            if not isinstance(state, dict):
                continue
            file_trading_day_str = state.get("trading_day")
            try:
                file_trading_day = (
                    date.fromisoformat(file_trading_day_str) if file_trading_day_str else None
                )
            except (TypeError, ValueError):
                file_trading_day = None
            if file_trading_day is None or file_trading_day > trading_day:
                continue
            if iso_week_key(file_trading_day) != this_week:
                continue
            # Applies to manual reruns AND the day-by-day scheduled/fixed-slot
            # paths alike: a replayed/backfilled run (this call's own `now`)
            # must not seed cap spend out of an artifact that was actually
            # written later in the day than the decision being reproduced --
            # e.g. a delayed scheduled invocation whose fixed YYYYMMDD_1030ET/
            # run_state.json ends up with a generated_at well after 10:30.
            if _run_state_after_boundary(state, now):
                continue
            run_id = state.get("run_id")
            if run_id is None:
                continue
            already_seeded = run_id in self._seeded_prior_run_ids
            if already_seeded:
                # Ledger already has this run's spend; skip re-seeding, but
                # still load submitted rows from the scheduled path so a second
                # call to run() on the SAME executor instance (same run_id)
                # carries them forward into _prior_same_day_candidate_rows.
                if is_scheduled_today:
                    _bcs = state.get("buy_candidates") or []
                    if isinstance(_bcs, list):
                        submitted_rows_only = [
                            c for c in _bcs
                            if isinstance(c, dict) and c.get("status") in ("submitted", "filled")
                        ]
                        if submitted_rows_only:
                            self._prior_same_day_candidate_rows.extend(submitted_rows_only)
                continue
            total = 0.0
            submitted_rows = []
            _buy_candidates = state.get("buy_candidates") or []
            if not isinstance(_buy_candidates, list):
                _buy_candidates = []
            for candidate in _buy_candidates:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("status") not in ("submitted", "filled"):
                    continue
                amount = candidate.get("planned_amount_usd")
                if amount is None:
                    amount = candidate.get("hypothetical_amount_usd")
                try:
                    amount = float(amount)
                except (TypeError, ValueError):
                    continue
                # Reject inf/-inf/nan for the same reason as the CSV cross-check
                # below: an infinite amount would seed infinite cap spend.
                if not math.isfinite(amount):
                    continue
                if amount > 0:
                    total += amount
                    submitted_rows.append(candidate)

            # Cross-check against the broker-order log written alongside
            # run_state.json: a Copilot Autofix pass flagged that the JSON
            # buy_candidates total could under-report actual submitted/filled
            # spend, so take whichever total is larger as the true
            # already-spent amount for cap-seeding purposes.
            # filled_orders.csv (the confirmed-fill source written by manual
            # midday overrides) is generally the more trustworthy signal, since
            # orders.csv is always written with blank avg_fill_price /
            # quantity_filled fields and would shadow real fill data -- but a
            # PARTIAL filled_orders.csv (some rows confirmed, others still only
            # in orders.csv as submitted-not-yet-confirmed) can under-report
            # relative to orders.csv's total. Take the larger of the two
            # qualifying totals, consistent with the same "take whichever is
            # larger" policy already applied against the JSON total above,
            # rather than treating the two files as mutually exclusive.
            filled_orders_csv = path.parent / "filled_orders.csv"
            orders_csv = path.parent / "orders.csv"
            csv_total = _csv_qualifying_notional_total(filled_orders_csv, blank_status_means_filled=True)
            if orders_csv.exists() and orders_csv != filled_orders_csv:
                csv_total = max(
                    csv_total,
                    _csv_qualifying_notional_total(orders_csv, blank_status_means_filled=False),
                )
            total = max(total, csv_total)
            self._seeded_prior_run_ids.add(run_id)
            if total > 0:
                if file_trading_day == trading_day:
                    # Only seed today's own bucket here when cap_seed_roots isn't
                    # already doing it (see _today_seeded_by_cap_roots above) --
                    # otherwise the same on-disk artifact would be double-counted.
                    if not _today_seeded_by_cap_roots:
                        self.ledger.record_hypothetical(trading_day, total)
                else:
                    # Cross-day-in-week seeding only bumps the shared weekly
                    # bucket; the daily bucket stays scoped to trading_day.
                    self.ledger.planned_by_week[this_week] = (
                        self.ledger.planned_by_week.get(this_week, 0.0) + total
                    )
            if is_scheduled_today and submitted_rows:
                self._prior_same_day_candidate_rows.extend(submitted_rows)

    def _place_broker_orders(
        self,
        candidates: List[MorningSignal],
        trading_day: Any,
        broker: BrokerAdapter,
        top_up_outcomes: Optional[Dict[str, Any]],
        remaining_daily: float,
        remaining_weekly: float,
        manual: bool = False,
        data: Any = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Attempt broker order placement for each EXEC_AUTO_BUY_ELIGIBLE candidate.

        Returns a dict mapping ticker → outcome dict. Each outcome has a ``status``
        key: ``submitted``, ``skipped``, ``blocked``, ``cap_exceeded``,
        ``not_submitted_confirmation_required``, or ``failed``.

        Cap enforcement: tracks running submitted spend against the pre-run
        remaining daily/weekly budget so orders that would exceed the configured
        caps are skipped rather than submitted.

        Top-up awareness: for midday candidates the notional is taken from the
        top-up outcome's ``incremental_order_amount`` rather than the signal's
        ``hypothetical_amount_usd``. Cross-stream prior orders are expected for
        guarded top-ups, but same-stream ref-id duplicate checks still run so
        retries cannot submit the same stream order twice.

        Spread breaker: when ``data`` exposes a live bid/ask quote (the same
        mechanism the afternoon stream uses), a candidate whose spread exceeds
        ``config.max_bid_ask_spread_pct`` is blocked before placement. Providers
        without a ``get_bid_ask`` hook (e.g. most test doubles) are silently
        skipped -- this is a best-effort circuit breaker, not a hard requirement.

        Review policy: scheduled runs (manual=False) skip review_market_buy and
        call place_market_buy directly after all internal checks pass, per the
        pre-authorization policy in config/budgets.json. Manual runs still call
        review_market_buy first.
        """
        if not candidates:
            return {}
        outcomes: Dict[str, Any] = {}
        submitted_today = 0.0
        submitted_this_week = 0.0
        # Prior-submitted amounts by ticker from the CURRENT on-disk dated-slot
        # artifact (this stream's own earlier attempt this scheduled slot, if
        # any), read once before this call's outcomes overwrite that file.
        # Used below by the same-stream top-up-duplicate path to PRESERVE the
        # originally submitted notional rather than recording a freshly
        # recomputed (and generally smaller, once a same-day external fill has
        # landed) top-up incremental amount over it -- a retry's recomputed
        # incremental reflects an updated prior-allocation total, not what was
        # actually placed with the broker on the earlier attempt (Codex
        # review, PR #88: "Preserve original scheduled notional across top-up
        # retries").
        prior_submitted_amounts: Dict[str, float] = {}
        if not manual and now is not None:
            existing = self._existing_submitted_run_state(now)
            if existing is not None:
                for row in existing["artifacts"].get("buy_candidates", []):
                    if not isinstance(row, dict) or row.get("status") != "submitted":
                        continue
                    row_ticker = row.get("ticker")
                    if not row_ticker:
                        continue
                    amt = row.get("submitted_amount_usd")
                    if amt is None:
                        amt = row.get("planned_amount_usd")
                    try:
                        amt = float(amt)
                    except (TypeError, ValueError):
                        continue
                    if amt > 0:
                        prior_submitted_amounts[row_ticker] = amt
        # Snapshotted lazily (see below) so adapters that immediately reflect
        # placed orders don't cause double-subtraction (reserved_buying_power
        # tracks THIS batch only). None means "not fetched yet" -- fetched on
        # first actual need so a batch where every candidate is blocked by an
        # earlier, cheaper check (per-order cap, duplicate, tradability) never
        # makes an unnecessary broker/account round-trip.
        batch_buying_power: Optional[float] = None
        reserved_buying_power = 0.0

        def raw_persistence_failed() -> bool:
            """Stop before submission when the decorated broker could not save an input."""
            recorder = getattr(broker, "_recorder", None)
            return bool(recorder is not None and recorder.error)

        for signal in candidates:
            ticker = signal.ticker
            top_up = (top_up_outcomes or {}).get(ticker)

            # Determine the real order notional from the top-up outcome if active.
            if top_up is not None:
                if top_up.top_up_skipped or top_up.incremental_order_amount <= 0:
                    outcomes[ticker] = {
                        "status": "skipped",
                        "skip_reason": f"top-up guard: {top_up.top_up_guard_result}",
                    }
                    continue
                notional = round(top_up.incremental_order_amount, 2)
            else:
                notional = signal.hypothetical_amount_usd

            # Hard per-order ceiling from config/budgets.json circuit_breakers.max_per_order_usd.
            # A pure local computation (no broker round-trip), so it stays ahead
            # of the duplicate check below -- an over-ceiling candidate must
            # never even reach the broker (Codex review #25 on PR #22).
            _max_order_usd = self.config.max_per_order_usd
            if notional > _max_order_usd:
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"exceeds ${_max_order_usd:.0f} per-order ceiling",
                    "deliberate_skip": True,
                }
                continue

            # Bypass the broad same-day duplicate check only when there is an actual
            # cross-stream prior allocation (top-up guard result is not 'no_prior_run').
            # Even then, keep a same-stream ref-id check so retrying this stream after
            # it already submitted the ticker remains idempotent.
            #
            # This duplicate check runs BEFORE the daily/weekly cap checks
            # below: a duplicate places no new order, so it must never be
            # gated by checks that exist to decide whether a NEW placement
            # should proceed. Once cap-seeding has recovered an earlier
            # scheduled order that exactly consumes (or slightly exceeds, via
            # a freshly recomputed but stale notional) the remaining cap,
            # running the cap check first would record cap_exceeded and
            # overwrite the dated-slot artifact's real "submitted" row for
            # this ticker, erasing evidence a fresh executor's cap-seeding
            # scan depends on (Codex review, PR #88: "Preserve submitted
            # evidence when a cap-seeded retry is full").
            has_prior_allocation = (
                top_up is not None and top_up.top_up_guard_result != TOP_UP_NO_PRIOR_RUN
            )
            dup_order: Optional[Dict[str, Any]] = None
            try:
                if has_prior_allocation:
                    dup_order = _same_stream_order_exists(broker, ticker, trading_day, self.stream)
                    dup = dup_order is not None
                    duplicate_reason = "same-stream broker order exists"
                else:
                    dup = broker.same_day_strategy_order_exists(ticker, trading_day)
                    duplicate_reason = "same-day broker order exists"
            except Exception as exc:  # noqa: BLE001
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"duplicate-check failed: {exc}",
                }
                _log.warning("Duplicate-check error for %s: %s", ticker, exc)
                continue
            if raw_persistence_failed():
                # The duplicate response is an order-placement input. Do not
                # let a failed audit write turn into a later submission.
                break
            if dup:
                if has_prior_allocation and not manual:
                    # A same-stream top-up duplicate on a SCHEDULED retry means
                    # this stream already submitted/reserved the incremental
                    # order on an earlier attempt today, and a scheduled rerun
                    # overwrites the same date-keyed run_state.json path (see
                    # run_state_dir()), so there is no separate artifact for
                    # load_prior_same_day_allocations() to double-sum against.
                    # Persist the real broker order's id and amount so rewriting
                    # run_state on retry does not fabricate phantom values that
                    # downstream fill-lookup or reconciliation would reject or
                    # misread. Prefer the broker's own dollar_amount for this
                    # matched order (most authoritative -- straight from the
                    # broker); adapters that only support the legacy boolean
                    # duplicate-check contract normalize to an empty dup_order
                    # dict with no amount fields, so fall back to this
                    # process's own previously-recorded submission for the
                    # ticker (read into prior_submitted_amounts above) before
                    # finally falling back to the freshly recomputed
                    # `notional`, which can have shrunk since the original
                    # attempt if new prior-allocation context (e.g. an
                    # external fill) appeared in between.
                    try:
                        dup_amount = float(
                            (dup_order or {}).get("dollar_amount")
                            or (dup_order or {}).get("notional")
                            or 0
                        ) or prior_submitted_amounts.get(ticker, notional)
                    except (TypeError, ValueError):
                        dup_amount = prior_submitted_amounts.get(ticker, notional)
                    if not math.isfinite(dup_amount) or dup_amount <= 0:
                        dup_amount = prior_submitted_amounts.get(ticker, notional)
                    outcomes[ticker] = {
                        "status": "submitted",
                        "order_id": (dup_order or {}).get("id") or (dup_order or {}).get("order_id"),
                        "skip_reason": duplicate_reason,
                        "submitted_amount_usd": dup_amount,
                        # This amount is a DUPLICATE of an already-submitted
                        # order, not a fresh placement -- it's already
                        # reflected in the cap-seeding scan of this same
                        # dated-slot artifact (_compute_top_up), so run()'s
                        # submitted_candidates filter must exclude it from
                        # record_buy_candidates()'s ledger booking, or the
                        # amount gets double-counted every retry (Codex
                        # review, PR #88: "Avoid booking same-stream top-up
                        # retries a second time").
                        "duplicate_preserved": True,
                    }
                    submitted_today += dup_amount
                    submitted_this_week += dup_amount
                    # Do NOT add to reserved_buying_power: available_buying_power()
                    # is a fresh broker snapshot fetched (lazily, below) AFTER the
                    # earlier order already consumed it, so crediting dup_amount
                    # again here would double-subtract it and could incorrectly
                    # block a later candidate in this retry batch even when real
                    # buying power is sufficient (Codex review, PR #51: "Do not
                    # reserve buying power for existing duplicates").
                elif has_prior_allocation and manual:
                    # A MANUAL/recovery retry always writes to its own fresh,
                    # uuid-suffixed artifact directory (see run_id()) and never
                    # overwrites the original submission's run_state.json. If
                    # this were also recorded as "submitted" with a fresh
                    # notional, load_prior_same_day_allocations() would find
                    # both files and double-count the same broker order as two
                    # separate same-day allocations, understating what an
                    # afternoon/final top-up is still owed. The original
                    # submission's own artifact already carries the real
                    # notional, so this retry contributes nothing further.
                    outcomes[ticker] = {
                        "status": "skipped",
                        "skip_reason": f"{duplicate_reason} (duplicate top-up retry; already recorded by the original run)",
                    }
                else:
                    outcomes[ticker] = {"status": "skipped", "skip_reason": duplicate_reason}
                continue

            # Enforce daily/weekly caps against pre-run remaining budget.
            if submitted_today + notional > remaining_daily:
                outcomes[ticker] = {"status": "cap_exceeded", "skip_reason": "daily cap would be exceeded"}
                continue
            if submitted_this_week + notional > remaining_weekly:
                outcomes[ticker] = {"status": "cap_exceeded", "skip_reason": "weekly cap would be exceeded"}
                continue

            if data is not None and now is not None:
                try:
                    spread_pct = quote_spread_pct(data, ticker, now)
                except Exception as exc:  # noqa: BLE001
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": f"spread quote error: {exc}",
                    }
                    _log.warning("Spread quote error for %s: %s", ticker, exc)
                    continue
                if raw_persistence_failed():
                    break
                max_spread = self.config.max_bid_ask_spread_pct
                if spread_pct is not None and spread_pct > max_spread:
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": (
                            f"bid/ask spread {spread_pct:.2f}% exceeds {max_spread:.2f}% limit"
                        ),
                        "deliberate_skip": True,
                    }
                    continue
            try:
                tradable = broker.is_tradable_for_fractional_buy(ticker)
            except Exception as exc:  # noqa: BLE001
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"tradability lookup failed: {exc}",
                }
                _log.warning("Tradability lookup error for %s: %s", ticker, exc)
                continue
            if raw_persistence_failed():
                break
            if not tradable:
                outcomes[ticker] = {"status": "blocked", "skip_reason": "not tradable for fractional buy", "deliberate_skip": True}
                continue
            if batch_buying_power is None:
                try:
                    batch_buying_power = broker.available_buying_power()
                except Exception as exc:  # noqa: BLE001 - adapters raise arbitrary connector errors
                    if manual:
                        raise
                    raise _BuyingPowerLookupError(str(exc)) from exc
            if raw_persistence_failed():
                break
            if batch_buying_power - reserved_buying_power < notional:
                outcomes[ticker] = {"status": "blocked", "skip_reason": "insufficient buying power", "deliberate_skip": True}
                continue
            # Scheduled runs are pre-authorized to call place_equity_order directly
            # (config/budgets.json execution_authorization_policy: calls_review_equity_order=false).
            # Manual runs still call review_market_buy before placement.
            if manual:
                try:
                    review_ok, review_reason = broker.review_market_buy(ticker, notional)
                except Exception as exc:  # noqa: BLE001
                    outcomes[ticker] = {
                        "status": "blocked",
                        "skip_reason": f"review failed: {exc}",
                    }
                    _log.warning("Review error for %s: %s", ticker, exc)
                    continue
                if not review_ok:
                    outcomes[ticker] = {"status": "blocked", "skip_reason": f"review blocked: {review_reason}", "deliberate_skip": True}
                    continue
            # Stable across retries so the broker's idempotency key can collapse
            # duplicate submissions; guarded top-ups use a distinct suffix to
            # avoid colliding with the earlier same-day run's ref_id for the
            # ticker. A manual run without a prior allocation (e.g. a MORNING
            # manual recovery run, which disables per-ticker top-up computation
            # in _compute_top_up) gets its own "-manual-" ref_id segment too:
            # without it, a manual fill's ref_id is indistinguishable from a
            # plain scheduled order's ref_id, so a LATER scheduled run that
            # finds this manual fill as a prior allocation and computes a
            # guarded top-up would have its _same_stream_order_exists ordinary-
            # prefix check match the manual fill and wrongly treat the fresh
            # incremental top-up as an already-submitted retry -- recording it
            # as "submitted" without ever calling place_market_buy (Codex
            # review, PR #42).
            if has_prior_allocation:
                ref_id = f"{self.stream}-dca-topup-{trading_day.isoformat()}-{ticker}"
            elif manual:
                ref_id = f"{self.stream}-dca-manual-{trading_day.isoformat()}-{ticker}"
            else:
                ref_id = f"{self.stream}-dca-{trading_day.isoformat()}-{ticker}"
            try:
                order_id = broker.place_market_buy(ticker, notional, ref_id=ref_id)
            except OrderConfirmationRequiredError:
                outcomes[ticker] = {
                    "status": "not_submitted_confirmation_required",
                    "order_id": None,
                    "skip_reason": "broker requires post-review confirmation before submission",
                    "submitted_amount_usd": notional,
                }
                continue
            except Exception as exc:  # noqa: BLE001
                outcomes[ticker] = {
                    "status": "failed",
                    "order_id": None,
                    "skip_reason": f"placement failed: {exc}",
                    "submitted_amount_usd": notional,
                }
                _log.error("Placement failed for %s: %s", ticker, exc)
                continue
            # DryRunBroker returns a DRY-RUN-... order id; record as dry_run so
            # load_prior_same_day_allocations never treats it as real capital.
            if order_id.startswith("DRY-RUN"):
                outcomes[ticker] = {
                    "status": "dry_run",
                    "order_id": order_id,
                    "skip_reason": "dry_run: order not placed",
                    "submitted_amount_usd": notional,
                }
                continue
            outcomes[ticker] = {
                "status": "submitted",
                "order_id": order_id,
                "submitted_amount_usd": notional,
            }
            submitted_today += notional
            submitted_this_week += notional
            reserved_buying_power += notional
            _log.info("Placed morning buy for %s: $%.2f, order_id=%s", ticker, notional, order_id)
        return outcomes

    def run(
        self,
        now: datetime,
        data: Any,
        *,
        broker: Optional[BrokerAdapter] = None,
        data_source: Optional[str] = None,
        manual: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate the stream and write its artifacts for the active mode.

        Returns ``{"artifacts": <run_state dict>, "paths": <written paths>}``.
        The watchlist scan places no orders; the live mode surfaces
        auto-buy-eligible vs. manual_review candidates per the confidence rule.
        When ``broker`` is supplied in live mode, auto-buy-eligible candidates
        are submitted via the broker to the account configured in
        ``target_account_number`` and outcomes recorded in the artifacts.
        Alpaca stays read-only.
        """
        blocked = self._maybe_block_for_holiday(now, manual=manual)
        if blocked is not None:
            return blocked
        # live_schedule_hardening_2026_07_20 (config/morning_dca.json): a
        # SCHEDULED live run must persist every raw connector response it uses
        # under raw/ before that response is used to compute a signal or place
        # an order, and must never escalate without writing a valid run_state.
        # Manual/watchlist/preflight runs are unaffected -- the policy's
        # "applies_to" is scoped to the scheduled live task (42173154/a6239dae).
        is_scheduled_live = self.mode == MORNING_LIVE_MODE and self.trades and not manual
        raw_recorder: Optional[_RawInputRecorder] = None
        raw_dir: Optional[Path] = None
        if is_scheduled_live:
            raw_dir = self.run_state_dir(now) / "raw"
            raw_recorder = _RawInputRecorder(raw_dir, now.astimezone(EASTERN).date().isoformat())
            data = _PersistingMarketDataProvider(data, raw_recorder)
            if broker is not None:
                broker = _PersistingBrokerAdapter(broker, raw_recorder)
        signals = self.evaluate(now, data, manual=manual)
        source = data_source or getattr(getattr(data, "source", None), "value", "unknown")
        # Scheduled-run preflight block: _apply_preflight_gate found no same-day
        # pre-flight artifact (or one that explicitly flagged itself invalid) and
        # set preflight_missing_block instead of raising. Build the blocked result
        # through the normal artifact pipeline (build_artifacts/write_artifacts)
        # rather than a hand-written stub, so the persisted run_state carries the
        # standard shape (run_id, generated_at, counts, run_classification) and
        # the latest scheduled pointer is still updated -- watchdog/reporting can
        # then validate this as a real (blocked) scheduled-run attempt instead of
        # something that looks malformed or missing.
        pgate = getattr(self, "preflight_gate", None)
        if pgate and pgate.get("preflight_missing_block"):
            # Before overwriting the dated-slot artifact with a blocked stub,
            # check whether an EARLIER attempt at this same slot already
            # submitted real orders -- e.g. the preflight artifact was
            # available on the first attempt but becomes unavailable/stale by
            # the time a scheduled retry runs. Unlike _blocked_live_result(),
            # this path built its own stub directly rather than going through
            # that helper, so it skipped the same preservation check and could
            # erase submitted-order evidence a same-day top-up guard depends
            # on (Codex review, PR #88: "Preserve submitted state on a
            # preflight-blocked retry").
            existing = self._existing_submitted_run_state(now)
            if existing is not None:
                return existing
            block_reason = pgate["block_reason"]
            artifacts = self.build_artifacts(now, signals, source, manual=manual)
            artifacts["status"] = "blocked"
            artifacts["block_reason"] = block_reason
            artifacts["trades"] = False
            artifacts["broker_outcomes"] = {}
            paths = self.write_artifacts(artifacts, now)
            if self.mode == MORNING_LIVE_MODE and self.trades:
                _run_drift_check(Path(paths["run_state"]).parent)
            return {"artifacts": artifacts, "paths": paths}
        if raw_recorder is not None and raw_recorder.error:
            # Raw-input persistence failed before any order could be placed --
            # evaluate() above only reads price data and never places an
            # order, so nothing has been submitted yet. Fail closed rather
            # than compute/place from a connector response that was never
            # durably recorded (config/morning_dca.json:
            # live_schedule_hardening_2026_07_20.raw_input_generation).
            return self._blocked_live_result(
                now,
                f"raw input persistence failed: {raw_recorder.error}",
                signals,
                source,
                error_class="missing_or_invalid_raw_inputs",
                raw_recorder=raw_recorder,
            )
        trading_day = now.astimezone(EASTERN).date()
        top_up_outcomes = self._compute_top_up(now, signals, manual=manual)
        # Sort by multiplier DESC, then drop_pct DESC, then ticker ASC so tied
        # multipliers are resolved by the larger-drop ticker first, matching the
        # allocation-priority policy in config/budgets.json.
        auto_buy_candidates = sorted(
            (s for s in self._buy_candidates(signals) if s.execution_status != EXEC_MANUAL_REVIEW_CANDIDATE),
            key=lambda s: (-s.multiplier, -(s.drop_pct or 0.0), s.ticker),
        )
        # Fold already-spent same-day capital from an earlier run of this same
        # stream (retry or manual rerun) into the ledger BEFORE snapshotting
        # remaining budget, so a fresh-process retry never re-spends the cap.
        # This only seeds WEEKLY spend from earlier days in the ISO week (plus
        # today's carry-forward candidate rows) -- today's own-stream spend is
        # already seeded above by _compute_top_up's cap_seed_roots scan, so
        # _seed_ledger_from_prior_runs must not also seed today's amount here
        # or the same on-disk artifact would be double-counted.
        self._seed_ledger_from_prior_runs(now)
        # Subtract prior same-day spend (recorded by _compute_top_up) from the
        # pre-run budget so caps account for earlier manual/recovery fills that
        # already consumed part of the daily/weekly cap on other tickers.
        # Restrict to the morning stream: midday has its own independent ledger
        # namespace and daily/weekly budget, so morning fills must not reduce
        # the midday cap even when prior_cap_seed_roots is configured.
        prior_daily_seeded = self._prior_daily_spend_seeded if self.stream == MORNING_STREAM else 0.0
        pre_run_remaining_daily = max(
            0.0, self.ledger.remaining_daily(trading_day) - prior_daily_seeded
        )
        pre_run_remaining_weekly = max(
            0.0, self.ledger.remaining_weekly(trading_day) - prior_daily_seeded
        )
        broker_outcomes: Optional[Dict[str, Any]] = None
        # Set below when raw persistence fails AFTER at least one candidate in
        # this call already reached "submitted" -- the artifact still needs to
        # be written (with that real submission intact) but stamped as blocked
        # rather than treated as a clean run. None on every other path.
        raw_persistence_partial_block: Optional[str] = None
        if self.mode == MORNING_LIVE_MODE and self.trades and not manual and broker is None:
            return self._blocked_live_result(
                now, "missing broker adapter for scheduled live execution", signals, source
            )
        if broker is not None and self.mode == MORNING_LIVE_MODE:
            expected_account = getattr(self.config, "target_account_number", None)
            try:
                actual_account = broker.account_number()
            except Exception as exc:  # noqa: BLE001 - broker adapters raise arbitrary connector errors
                if not manual:
                    return self._blocked_live_result(
                        now, f"failed to retrieve broker account number: {exc}", signals, source
                    )
                raise RuntimeError(f"Failed to retrieve broker account number: {exc}") from exc
            if raw_recorder is not None and raw_recorder.error:
                return self._blocked_live_result(
                    now,
                    f"raw input persistence failed: {raw_recorder.error}",
                    signals,
                    source,
                    error_class="missing_or_invalid_raw_inputs",
                    raw_recorder=raw_recorder,
                )
            if expected_account and actual_account != expected_account:
                mismatch = f"broker account mismatch: expected {expected_account}, got {actual_account}"
                if not manual:
                    return self._blocked_live_result(now, mismatch, signals, source)
                raise RuntimeError(mismatch)
            try:
                broker_outcomes = self._place_broker_orders(
                    auto_buy_candidates,
                    trading_day,
                    broker,
                    top_up_outcomes,
                    remaining_daily=pre_run_remaining_daily,
                    remaining_weekly=pre_run_remaining_weekly,
                    manual=manual,
                    data=data,
                    now=now,
                )
            except _BuyingPowerLookupError as exc:
                return self._blocked_live_result(
                    now,
                    f"failed to retrieve broker buying power: {exc}",
                    signals,
                    source,
                    error_class="buying_power_lookup_failed",
                )
            if raw_recorder is not None and raw_recorder.error:
                # _place_broker_orders checks after every persisted broker or
                # market-data input and stops before the next submission.
                # Convert that stopped execution into the promised valid
                # failure-state artifact rather than merely annotating a run
                # that may otherwise look eligible for execution -- UNLESS an
                # earlier candidate in this same call already reached
                # "submitted" before persistence failed on a later one.
                # _blocked_live_result() writes broker_outcomes={} and no
                # buy_candidates rows, so calling it here would silently drop
                # that already-placed order from the ledger and same-day
                # duplicate-check artifact. Fail closed with an empty
                # broker_outcomes only when nothing was actually submitted;
                # otherwise fall through to the normal record/build/write path
                # below so the real submission is recorded, and stamp the
                # block reason onto that artifact afterward so it is never
                # mistaken for a fully clean run (Codex/Copilot review,
                # PR #420: "Preserve orders submitted before a later
                # persistence failure").
                any_submitted = any(
                    isinstance(oc, dict) and oc.get("status") == "submitted"
                    for oc in broker_outcomes.values()
                )
                if not any_submitted:
                    return self._blocked_live_result(
                        now,
                        f"raw input persistence failed: {raw_recorder.error}",
                        signals,
                        source,
                        error_class="missing_or_invalid_raw_inputs",
                        raw_recorder=raw_recorder,
                    )
                raw_persistence_partial_block = f"raw input persistence failed: {raw_recorder.error}"
        # In live mode record only candidates whose orders were actually submitted
        # so cap-exceeded orders do not inflate the ledger and corrupt budget reporting.
        # A "duplicate_preserved" outcome (a same-stream top-up duplicate retry)
        # is excluded too: its amount is already reflected in the cap-seeding
        # filesystem scan of this same dated-slot artifact, so booking it again
        # here would double-count it on every retry (Codex review, PR #88:
        # "Avoid booking same-stream top-up retries a second time").
        if broker_outcomes is not None:
            submitted_candidates = [
                c for c in auto_buy_candidates
                if broker_outcomes.get(c.ticker, {}).get("status") == "submitted"
                and not broker_outcomes.get(c.ticker, {}).get("duplicate_preserved")
            ]
            self.record_buy_candidates(
                trading_day,
                submitted_candidates,
                top_up_outcomes,
                broker_outcomes,
                _track_own="manual" if manual else "scheduled",
            )
            if not manual:
                # record_buy_candidates's REPLACE-semantics reset for
                # "scheduled" only accumulates over submitted_candidates
                # (excluding duplicate_preserved), since those must not be
                # booked into self.ledger a second time. But the dated-slot
                # artifact this call is about to write to disk (write_artifacts,
                # below) carries BOTH freshly placed and duplicate-preserved
                # "submitted" rows -- the cap-seeding watermark needs the FULL
                # on-disk submitted total to correctly credit back everything
                # a future scan of this same file would see, not just the
                # subset that was freshly booked into the ledger this call.
                self._own_recorded_scheduled[trading_day] = sum(
                    oc.get("submitted_amount_usd", 0.0) or 0.0
                    for oc in broker_outcomes.values()
                    if isinstance(oc, dict) and oc.get("status") == "submitted"
                )
        else:
            # No broker in live mode: nothing was submitted, so record nothing in
            # the ledger. A phantom hypothetical amount would inflate the cap and
            # block cap-seeding's later pickup of a real external fill. Scoped to
            # the morning stream specifically: MIDDAY_LIVE_MODE == MORNING_LIVE_MODE
            # == "live", so mode alone can't tell the midday executor apart, and it
            # still needs hypothetical spend recorded for its own cap/budget
            # reporting. Watchlist/preflight modes always reach this branch and
            # still need hypothetical spend recorded for cap/budget reporting.
            morning_live_no_broker = self.mode == MORNING_LIVE_MODE and self.stream == MORNING_STREAM
            candidates_to_record = [] if morning_live_no_broker else auto_buy_candidates
            self.record_buy_candidates(trading_day, candidates_to_record, top_up_outcomes, _track_own=None)
        artifacts = self.build_artifacts(now, signals, source, manual=manual, top_up_outcomes=top_up_outcomes, broker_outcomes=broker_outcomes)
        if raw_persistence_partial_block:
            # A real order above was submitted and IS recorded in this
            # artifact -- unlike _blocked_live_result()'s empty stub -- but a
            # later candidate never got evaluated because its raw audit write
            # failed. Mark the run blocked so operators/watchdogs see the
            # failure instead of a run that looks fully clean.
            artifacts["status"] = "blocked"
            artifacts["block_reason"] = raw_persistence_partial_block
            artifacts["error_class"] = "missing_or_invalid_raw_inputs"
        if broker_outcomes is not None:
            # Update buy_candidate rows with broker outcome so downstream
            # load_prior_same_day_allocations can read "submitted" status,
            # planned_amount_usd, and decision_price for same-day top-up guards.
            signal_by_ticker = {s.ticker: s for s in auto_buy_candidates}
            for row in artifacts.get("buy_candidates", []):
                ticker = row.get("ticker")
                if ticker and ticker in broker_outcomes:
                    oc = broker_outcomes[ticker]
                    row["status"] = oc.get("status", "not_submitted")
                    row["order_id"] = oc.get("order_id")
                    if "submitted_amount_usd" in oc:
                        row["planned_amount_usd"] = oc["submitted_amount_usd"]
                    # Persist the decision price so _candidate_logged_reference_price
                    # can resolve the top-up guard's reference price for later runs.
                    sig = signal_by_ticker.get(ticker)
                    if sig and sig.current_price and "decision_price" not in row:
                        row["decision_price"] = sig.current_price
            artifacts["broker_outcomes"] = broker_outcomes
            # Reconcile auto_buy_signal in signal rows against final placement
            # outcomes. Pre-placement eligibility sets it True, but top-up guards
            # (price_not_below, no_incremental) and duplicate-skip on retry leave
            # no submitted order for that ticker. The drift detector treats every
            # True row without a submitted order as missing.
            #
            # Only clear for deliberate non-submissions:
            #   "skipped"/"cap_exceeded" — guard fired or budget cap blocked
            #   "dry_run" — DryRunBroker intentionally placed no real order;
            #     write_artifacts() only emits submitted_orders.csv rows for
            #     status=="submitted", so a dry-run candidate never gets an
            #     order row and would otherwise false-alert every run
            #   blocked with deliberate_skip=True — safety gate (ceiling, spread,
            #     tradability, buying power, review blocked); these are explicit
            #     strategy decisions, not unexpected failures
            # Keep auto_buy_signal=True for genuine failures ("failed",
            # "not_submitted_confirmation_required", and "blocked" without
            # deliberate_skip, e.g. spread-quote errors) so drift alerts fire.
            _deliberate_skip_tickers = {
                t for t, oc in broker_outcomes.items()
                if oc.get("status") in ("skipped", "cap_exceeded", "dry_run")
                or (oc.get("status") == "blocked" and oc.get("deliberate_skip"))
            }
            for _row in artifacts.get("signal_rows", []):
                if _row.get("auto_buy_signal") and _row.get("ticker") in _deliberate_skip_tickers:
                    _row["auto_buy_signal"] = False
        if not manual and self._prior_same_day_candidate_rows:
            # A scheduled retry is about to overwrite the fixed scheduled
            # path; carry forward any earlier same-day submitted/filled rows
            # (found by _seed_ledger_from_prior_runs) for tickers this run
            # didn't itself re-submit, so the persisted artifact keeps the
            # afternoon top-up guard's visibility into that earlier fill.
            # Only suppress carry-forward when the new artifact already has a
            # submitted/filled row for that ticker. A skipped/cap_exceeded retry
            # row must NOT shadow the real submitted row from the first run.
            submitted_tickers = {
                row.get("ticker")
                for row in artifacts["buy_candidates"]
                if row.get("status") in ("submitted", "filled")
            }
            carried_forward = [
                dict(row)
                for row in self._prior_same_day_candidate_rows
                if row.get("ticker") not in submitted_tickers
            ]
            if carried_forward:
                artifacts["buy_candidates"] = carried_forward + artifacts["buy_candidates"]
                artifacts["counts"]["buy_candidates"] = len(artifacts["buy_candidates"])
                # These rows are this stream's OWN earlier scheduled submissions
                # being re-persisted into the artifact this call is about to
                # write, not a fresh submission this call placed. Fold their
                # total into _own_recorded_scheduled too -- otherwise a later
                # cap_seed_roots scan of this same on-disk file (_compute_top_up)
                # would see them as if they were a brand-new external fill and
                # double-count them into the ledger.
                carried_forward_total = 0.0
                for _cf_row in carried_forward:
                    _cf_amt = _cf_row.get("planned_amount_usd")
                    if _cf_amt is None:
                        _cf_amt = _cf_row.get("hypothetical_amount_usd")
                    try:
                        _cf_amt = float(_cf_amt)
                    except (TypeError, ValueError):
                        continue
                    if _cf_amt > 0:
                        carried_forward_total += _cf_amt
                if carried_forward_total > 0:
                    self._own_recorded_scheduled[trading_day] = (
                        self._own_recorded_scheduled.get(trading_day, 0.0) + carried_forward_total
                    )
        if raw_recorder is not None:
            artifacts["raw_input_dir"] = str(raw_dir)
            artifacts["raw_inputs_saved"] = raw_recorder.saved_filenames
            if raw_recorder.error:
                # Defensive visibility for a future post-placement raw input.
                # Current pre-placement reads are checked above and fail
                # closed before an order can be submitted.
                artifacts["raw_persistence_error"] = raw_recorder.error
        paths = self.write_artifacts(artifacts, now)
        # Fold this run's own run_id into the seen-set so a LATER run() call on
        # this SAME executor instance never re-reads this just-written artifact
        # from disk and double-counts spend already reflected in self.ledger
        # (via record_buy_candidates above).
        self._seeded_prior_run_ids.add(artifacts["run_id"])
        if self.mode == MORNING_LIVE_MODE and self.trades and not manual:
            _run_drift_check(Path(paths["run_state"]).parent)
        return {"artifacts": artifacts, "paths": paths}


def _run_drift_check(run_dir: Path) -> None:
    """Invoke drift detection after a live run writes its artifacts.

    Writes ``drift_report.json`` and ``drift_report.md`` alongside the run's
    artifacts, logs a warning when drift is detected, and sends a Slack alert
    when ``DCA_DRIFT_SLACK_WEBHOOK`` is set and drift is detected.
    """
    _log = logging.getLogger("morning_dca.drift")
    try:
        from scripts.drift_detection import (  # lazy — stdlib only dep
            DEFAULT_SLACK_WEBHOOK_ENV,
            detect_drift,
            send_slack_alert,
        )
    except Exception as exc:  # pragma: no cover
        _log.warning("drift detection unavailable: %s", exc)
        return
    try:
        report = detect_drift(run_dir)
        (run_dir / "drift_report.json").write_text(
            report.to_json(compact=False), encoding="utf-8"
        )
        (run_dir / "drift_report.md").write_text(report.to_markdown(), encoding="utf-8")
        if report.drift_detected:
            _log.warning(
                "DRIFT DETECTED in %s: %d symbol(s) expected but no order record — %s",
                run_dir,
                len(report.affected_symbols),
                ", ".join(s.symbol for s in report.affected_symbols),
            )
            try:
                send_slack_alert(report, env_var=DEFAULT_SLACK_WEBHOOK_ENV)
            except Exception as slack_exc:  # pragma: no cover
                _log.warning("drift Slack alert failed: %s", slack_exc)
        else:
            _log.info("drift check OK for %s", run_dir)
    except Exception as exc:  # pragma: no cover
        _log.warning("drift check failed for %s: %s", run_dir, exc)


def _same_stream_order_exists(
    broker: BrokerAdapter, ticker: str, trading_day: date, stream: str
) -> Optional[Dict[str, Any]]:
    """Return the broker order dict when this stream's idempotency ref id already exists, else None.

    Guarded midday top-ups intentionally allow earlier cross-stream orders for the
    same ticker, so the broad same-day duplicate check would be too strict. This
    narrower hook lets live brokers fail closed on retries of the current stream
    without treating the morning/manual prior allocation as a duplicate.

    Returning the matched order dict (rather than a plain bool) lets callers
    preserve the real broker order_id and amount so retried run_state writes do
    not fabricate phantom values.

    Both the plain and topup scheduled prefixes are checked here to cover:
    - A retry where the first attempt placed {stream}-dca-{date}-{ticker} (no prior
      allocation at the time) and a manual fill appeared before the retry elevating
      has_prior_allocation to True; we must detect the already-live plain order.
    - A retry where the first attempt already placed the topup variant.
    Manual fills use {stream}-dca-manual-{date}-{ticker} (see ref_id construction
    in _place_broker_orders) and therefore never collide with either prefix here.
    """
    checker = getattr(broker, "same_day_strategy_order_exists_with_ref_id_prefix", None)
    if checker is None:
        raise RuntimeError(
            "broker does not support same-stream ref-id duplicate checks for guarded top-ups"
        )
    prefixes = (
        f"{stream}-dca-topup-{trading_day.isoformat()}-{ticker}",
        f"{stream}-dca-{trading_day.isoformat()}-{ticker}",
    )
    result = checker(ticker, trading_day, prefixes)
    if not isinstance(result, dict):
        # Normalize legacy boolean adapters: False/None → None (no duplicate),
        # True → treat as found but without order details.
        return None if not result else {}
    return result


def _run_state_after_boundary(state: Dict[str, Any], boundary: datetime) -> bool:
    """True when ``state["generated_at"]`` is strictly after ``boundary``.

    Used by ``_seed_ledger_from_prior_runs`` to keep a replayed/backfilled run
    (``boundary`` = that replay's own ``now``) from seeding cap spend out of
    an artifact -- manual rerun OR the scheduled slot's own fixed-path file --
    that was actually written later in the day than the decision being
    reproduced. Strict ``>`` (not ``>=``) so a same-instant retry, where a
    fresh executor instance calls run() with the identical ``now`` an earlier
    attempt used for its own generated_at, still sees that attempt's spend --
    see test_morning_live_ledger_seeds_cap_spend_from_earlier_same_process_day_run.
    A missing/unparseable generated_at is conservatively treated as NOT after
    the boundary (more prior context only ever reduces a later buy).
    """
    gen_at_str = state.get("generated_at")
    if not gen_at_str:
        return False
    try:
        gen_at = datetime.fromisoformat(gen_at_str)
    except (ValueError, TypeError):
        return False
    if gen_at.tzinfo is None:
        # Match _prior_run_before_boundary in near_close_dca.py: a naive
        # generated_at is a local Eastern timestamp, not UTC.
        gen_at = gen_at.replace(tzinfo=EASTERN)
    return gen_at.astimezone(EASTERN) > boundary.astimezone(EASTERN)


def _csv_qualifying_notional_total(csv_path: Path, blank_status_means_filled: bool) -> float:
    """Sum notional_usd/amount_usd across submitted/filled, non-DRY-RUN rows.

    Used by ``_seed_ledger_from_prior_runs`` as a cross-check against the
    JSON-derived spend total. Returns ``0.0`` -- never raises -- for a missing,
    corrupt, unreadable, or empty file, or one with no qualifying rows, so the
    caller can safely try a fallback source (or fall back to the JSON total
    alone) without any special-casing at the call site.
    """
    total = 0.0
    if not csv_path.exists():
        return total
    try:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                status = (row.get("status") or "").lower()
                # filled_orders.csv rows are always confirmed fills; a
                # blank/missing status field still counts as "filled".
                if blank_status_means_filled and not status:
                    status = "filled"
                order_id = row.get("order_id") or ""
                if status not in ("submitted", "filled") or order_id.startswith("DRY-RUN"):
                    continue
                # filled_orders.csv uses amount_usd; orders.csv uses notional_usd.
                raw = row.get("notional_usd") or row.get("amount_usd")
                try:
                    notional = float(raw) if raw not in (None, "", "-") else None
                except (TypeError, ValueError):
                    notional = None
                # Reject inf/-inf/nan: an infinite notional would seed the
                # ledger with infinite spend and cap-exceed every later order.
                if notional is not None and not math.isfinite(notional):
                    notional = None
                if notional and notional > 0:
                    total += notional
    except (OSError, UnicodeDecodeError, csv.Error):
        # A corrupt/unreadable CSV must not abort the scheduled retry; the
        # caller falls back to another source (or the JSON total alone).
        return 0.0
    return total


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    seen: Dict[str, None] = {}
    for row in rows:
        seen.update(dict.fromkeys(row))
    fieldnames = list(seen)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)
