"""Morning graded-DCA executor (watchlist scan + live execution streams).

This module adds a *morning* stream on top of the existing near-close strategy
library (:mod:`near_close_dca`). It is deliberately isolated from the afternoon
scheduled live task (cron ``10ce362c``, which supersedes the deleted
``ce16ba6b``):

    - It uses the SAME canonical $3-base 0x -> 5x graded ladder as the afternoon
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
      ``state_schema_version='morning-live-v1'``.

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

from near_close_dca import (
    ACTIVE_TICKERS,
    EASTERN,
    EXCLUDED_TICKERS,
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
    TOP_UP_NO_PRIOR_RUN,
    Action,
    Bar,
    BrokerAdapter,
    OrderConfirmationRequiredError,
    StrategyConfig,
    completed_bar_anchor,
    graded_buy_multiplier,
    iso_week_key,
    latest_bar_at_or_before,
)

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
MORNING_PREFLIGHT_MODE = "preflight"
MORNING_PREFLIGHT_CRON_ID = "morning-dca-0945"
MORNING_PREFLIGHT_RUN_TYPE = "morning_dca_preflight"
MORNING_PREFLIGHT_SCHEDULED_FOR_ET = "09:45"
MORNING_PREFLIGHT_WINDOW_START_ET = "09:40"
MORNING_PREFLIGHT_WINDOW_END_ET = "09:50"
MORNING_PREFLIGHT_SCHEMA_VERSION = "morning-preflight-v1"

# Live (10:30 AM ET) execution identity — Robinhood-only live buys.
MORNING_LIVE_MODE = "live"
MORNING_LIVE_CRON_ID = "42173154"
MORNING_LIVE_RUN_TYPE = "morning_dca_live"
MORNING_LIVE_SCHEDULED_FOR_ET = "10:30"
MORNING_LIVE_WINDOW_START_ET = "10:25"
MORNING_LIVE_WINDOW_END_ET = "10:35"
MORNING_LIVE_SCHEMA_VERSION = "morning-live-v1"

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
# canonical $3-base 0x -> 5x graded ladder defined on
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
class MorningConfig:
    """Configuration hooks for the morning dry-run stream.

    Kept separate from :class:`~near_close_dca.StrategyConfig` so the morning
    ladder, caps, decision anchor, and run-state root can never leak into the
    afternoon scheduled path. ``base_buy_amount_usd`` matches the afternoon $3
    base by default so the hypothetical dollar figures are comparable.
    """

    tickers: Sequence[str] = field(default_factory=lambda: tuple(ACTIVE_TICKERS))
    excluded_tickers: frozenset[str] = field(default_factory=lambda: frozenset(EXCLUDED_TICKERS))
    base_buy_amount_usd: float = 3.0
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
    # Minimum number of completed intraday bars over the trend window required to
    # validate the intraday trend in live mode. When fewer are available a
    # qualifying candidate is flagged manual_review_candidate rather than bought.
    trend_min_bars: int = 6
    # Broker account number that must match the supplied BrokerAdapter before
    # any live orders are placed.
    target_account_number: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

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

    This is a self-contained session that tracks only *hypothetical* morning
    dry-run notional (and, if the morning stream ever executes in the future, its
    real notional). It shares no state with the afternoon
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
        if self.execution_status is not None:
            row["execution_status"] = self.execution_status
            row["trend_validated"] = self.trend_validated
        return row


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
        _log.warning(
            "morning_dca.json not found at %s; using MorningConfig defaults", cfg_path
        )
        return MorningConfig()

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("could not parse %s (%s); using MorningConfig defaults", cfg_path, exc)
        return MorningConfig()

    kwargs = {}

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

    if "trend_min_bars" in raw:
        kwargs["trend_min_bars"] = int(raw["trend_min_bars"])

    live_task = raw.get("live_task", {})
    if "broker_account" in live_task:
        kwargs["target_account_number"] = str(live_task["broker_account"])

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

    return MorningConfig(**kwargs)


def load_same_day_preflight(
    preflight_root: str, trading_day: date
) -> Optional[Dict[str, Any]]:
    """Load the same-day 9:45 pre-flight artifact for the 10:30 live gate.

    Returns a dict ``{"run_id", "eligible_tickers": set[str], "run_state_path"}``
    when a same-day pre-flight ``run_state.json`` is found under
    ``preflight_root/YYYYMMDD_0945ET/`` (or via ``latest_preflight.json`` when it
    points at the same trading day), else ``None``. The live executor uses the
    returned ``eligible_tickers`` to restrict which tickers it re-evaluates; a
    ``None`` result means "no pre-flight found" and the caller degrades to
    evaluating the full universe with a warning rather than blocking all buys.
    """
    root = Path(preflight_root)
    run_dir = root / f"{trading_day.strftime('%Y%m%d')}_0945ET"
    state_path = run_dir / "run_state.json"
    if not state_path.exists():
        pointer = root / "latest_preflight.json"
        if not pointer.exists():
            return None
        try:
            pdata = json.loads(pointer.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if pdata.get("trading_day") != trading_day.isoformat():
            return None
        state_path = Path(pdata.get("run_state_path", ""))
        if not state_path.exists():
            return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
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
    }


class DCAExecutor:
    """Base class for class-based DCA executors over the strategy library.

    The historical afternoon path is functional (``near_close_dca.run_once`` /
    ``run_daily_compact``) and remains the authoritative live path. This base
    introduces the shared extension points a class-based executor needs — the
    sizing ladder, a run-state root, and a run-classification block — so new
    streams (starting with the morning dry-run) can plug in without touching the
    afternoon code.

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
    regular-session close and grades it on the shared $3-base 0x -> 5x live
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

    def _load_default_config(self, config_dir: Optional[str], mode: str) -> MorningConfig:
        """Load this executor's config when none is passed. Overridable per stream."""
        return load_morning_config(config_dir, mode=mode)

    # -- Reuse hooks (no-ops for the morning stream) -------------------------
    def _compute_top_up(self, now: datetime, signals: Sequence["MorningSignal"]) -> Dict[str, Any]:
        """Return per-ticker prior-allocation/top-up outcomes. Empty for morning."""
        return {}

    def _effective_buy_amount(
        self, signal: "MorningSignal", top_up_outcomes: Optional[Dict[str, Any]]
    ) -> float:
        """Dollar amount actually planned for a buy candidate. Morning: the graded amount."""
        return signal.hypothetical_amount_usd

    def _augment_buy_candidate_row(
        self, row: Dict[str, Any], signal: "MorningSignal", top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        """Attach stream-specific fields to a buy-candidate row. No-op for morning."""
        return None

    def _augment_artifacts(
        self, artifacts: Dict[str, Any], top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        """Attach stream-specific top-level artifact fields. No-op for morning."""
        return None

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
        usable: Dict[Any, Bar] = {}
        for b in bars:
            if b.timestamp <= bar_anchor and math.isfinite(b.close) and b.close > 0:
                usable[b.timestamp] = b
        if len(usable) < self.config.trend_min_bars:
            warnings.append(
                f"intraday trend unvalidated: {len(usable)} usable bars < required "
                f"{self.config.trend_min_bars} over the 60-minute window"
            )
            return False
        sorted_bars = sorted(usable.values(), key=lambda b: b.timestamp)
        if sorted_bars[-1].close > sorted_bars[0].close:
            warnings.append(
                "intraday trend rising: price is above open; skipping auto-buy"
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
        if not math.isfinite(bar.close) or bar.close <= 0:
            warnings.append(f"completed bar close {bar.close!r} is not positive; rejecting")
            return None
        return bar.close

    def evaluate(self, now: datetime, data: Any) -> List[MorningSignal]:
        """Evaluate every non-excluded ticker for the morning stream.

        In morning live (10:30) mode the universe is first narrowed by the
        same-day 9:45 pre-flight gate: only tickers marked
        ``eligible_to_execute=true`` are re-evaluated. If no same-day pre-flight
        artifact is found the gate degrades to the full universe (with a warning
        recorded in :attr:`preflight_gate`) rather than blocking all buys.
        """
        tickers = [t for t in self.config.tickers if t not in self.config.excluded_tickers]
        tickers = self._apply_preflight_gate(now, tickers)
        return [self.evaluate_signal(t, now, data) for t in tickers]

    # Populated by :meth:`_apply_preflight_gate` for morning live runs so
    # build_artifacts can surface which tickers the pre-flight gate admitted.
    preflight_gate: Optional[Dict[str, Any]] = None

    def _apply_preflight_gate(self, now: datetime, tickers: List[str]) -> List[str]:
        """Restrict the morning live universe to the same-day pre-flight verdict."""
        self.preflight_gate = None
        if not (self.mode == MORNING_LIVE_MODE and self.stream == MORNING_STREAM):
            return tickers
        trading_day = now.astimezone(EASTERN).date()
        root = self.config.preflight_run_state_root or DEFAULT_MORNING_PREFLIGHT_STATE_ROOT
        loaded = load_same_day_preflight(root, trading_day)
        if loaded is None:
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
    ) -> None:
        """Record this run's buy candidates against the morning ledger.

        Call exactly once per run (``run()`` does this before building
        artifacts) -- ``build_artifacts`` itself is side-effect-free, so
        inspecting/rebuilding artifacts never double-counts hypothetical spend.
        The effective amount (after any stream-specific top-up offset) is what is
        recorded, so the ledger reflects what the stream actually plans to spend.
        """
        for candidate in buy_candidates:
            self.ledger.record_hypothetical(
                trading_day, self._effective_buy_amount(candidate, top_up_outcomes)
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

        hypothetical_spend = round(
            sum(self._effective_buy_amount(c, top_up_outcomes) for c in buy_candidates), 2
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
        for c in buy_candidates:
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
                "remaining_daily_usd": round(self.ledger.remaining_daily(trading_day), 2),
                "remaining_weekly_usd": round(self.ledger.remaining_weekly(trading_day), 2),
            },
            "counts": {
                "evaluated": len(signals),
                "buy_candidates": len(buy_candidates),
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
            artifacts["manual_review_candidates"] = [c.to_row() for c in manual_review]
            artifacts["manual_review_tickers"] = [c.ticker for c in manual_review]
            if self.preflight_gate is not None:
                artifacts["preflight_gate"] = self.preflight_gate
                if not self.preflight_gate.get("preflight_found"):
                    warnings.append(self.preflight_gate["warning"])
        if is_preflight:
            # Pre-flight verdict: which tickers the 10:30 live evaluator should
            # re-evaluate. Coarse pre-screen only; 10:30 recomputes fresh.
            eligible = [s for s in signals if preflight_eligibility(s)]
            artifacts["eligible_to_execute_tickers"] = [s.ticker for s in eligible]
            artifacts["counts"]["eligible_to_execute"] = len(eligible)
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

    def _maybe_block_for_holiday(self, now: datetime, *, manual: bool) -> Optional[Dict[str, Any]]:
        """Return a blocked-run result when ``now`` falls on a weekend or NYSE holiday.

        A cron fire on a weekend or full market closure has no real session to
        evaluate. Unlike near_close_dca.MARKET_HOLIDAYS (scoped to the afternoon
        live-test window and hand-maintained), this stream runs indefinitely with
        no end date, so it uses the computed nyse_holidays() calendar to stay
        correct into future years without manual updates. Writes a blocked stub --
        mirroring near_close_dca's date-gate pattern -- and never advances the
        scheduled ``latest_*_run.json`` pointer, so it keeps referring to the last
        real session instead of a non-trading placeholder. Returns None on a
        normal trading day.
        """
        trading_day = now.astimezone(EASTERN).date()
        is_weekend = trading_day.weekday() >= 5
        is_holiday = trading_day in nyse_holidays(trading_day.year)
        if manual or (not is_weekend and not is_holiday):
            return None
        block_reason = "weekend" if is_weekend else "market holiday"
        out = self.run_state_dir(now)
        out.mkdir(parents=True, exist_ok=True)
        state_path = out / "run_state.json"
        if not state_path.exists():
            state_path.write_text(
                json.dumps(
                    {"trading_day": trading_day.isoformat(), "status": "blocked",
                     "block_reason": block_reason, "stream": self.stream},
                    indent=2, sort_keys=True,
                ),
                encoding="utf-8",
            )
        return {"artifacts": {"trading_day": trading_day.isoformat(), "status": "blocked"},
                "paths": {"run_state": str(state_path)}}

    def _place_broker_orders(
        self,
        candidates: List[MorningSignal],
        trading_day: Any,
        broker: BrokerAdapter,
        top_up_outcomes: Optional[Dict[str, Any]],
        remaining_daily: float,
        remaining_weekly: float,
        manual: bool = False,
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
        ``hypothetical_amount_usd``; the broker's same-day-order duplicate check
        is bypassed when a top-up outcome is active (the prior order is expected).

        Review policy: scheduled runs (manual=False) skip review_market_buy and
        call place_market_buy directly after all internal checks pass, per the
        pre-authorization policy in config/budgets.json. Manual runs still call
        review_market_buy first.
        """
        outcomes: Dict[str, Any] = {}
        submitted_today = 0.0
        submitted_this_week = 0.0
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

            # Hard per-order ceiling: 5x * $3 base = $15 (config/budgets.json circuit_breakers).
            _MAX_ORDER_USD = 15.0
            if notional > _MAX_ORDER_USD:
                outcomes[ticker] = {
                    "status": "blocked",
                    "skip_reason": f"exceeds ${_MAX_ORDER_USD:.0f} per-order ceiling",
                }
                continue

            # Enforce daily/weekly caps against pre-run remaining budget.
            if submitted_today + notional > remaining_daily:
                outcomes[ticker] = {"status": "cap_exceeded", "skip_reason": "daily cap would be exceeded"}
                continue
            if submitted_this_week + notional > remaining_weekly:
                outcomes[ticker] = {"status": "cap_exceeded", "skip_reason": "weekly cap would be exceeded"}
                continue

            # Bypass same-day duplicate check only when there is an actual prior
            # same-day allocation (top-up guard result is not 'no_prior_run').
            # When no prior run exists, the standard broker duplicate safeguard fires.
            has_prior_allocation = (
                top_up is not None and top_up.top_up_guard_result != TOP_UP_NO_PRIOR_RUN
            )
            if not has_prior_allocation and broker.same_day_strategy_order_exists(ticker, trading_day):
                outcomes[ticker] = {"status": "skipped", "skip_reason": "same-day broker order exists"}
                continue
            if not broker.is_tradable_for_fractional_buy(ticker):
                outcomes[ticker] = {"status": "blocked", "skip_reason": "not tradable for fractional buy"}
                continue
            if broker.available_buying_power() < notional:
                outcomes[ticker] = {"status": "blocked", "skip_reason": "insufficient buying power"}
                continue
            # Scheduled runs are pre-authorized to call place_equity_order directly
            # (config/budgets.json execution_authorization_policy: calls_review_equity_order=false).
            # Manual runs still call review_market_buy before placement.
            if manual:
                review_ok, review_reason = broker.review_market_buy(ticker, notional)
                if not review_ok:
                    outcomes[ticker] = {"status": "blocked", "skip_reason": f"review blocked: {review_reason}"}
                    continue
            # Stable across retries so the broker's idempotency key can collapse
            # duplicate submissions; guarded top-ups use a distinct suffix to
            # avoid colliding with the earlier same-day run's ref_id for the ticker.
            ref_id = (
                f"{self.stream}-dca-topup-{trading_day.isoformat()}-{ticker}"
                if has_prior_allocation
                else f"{self.stream}-dca-{trading_day.isoformat()}-{ticker}"
            )
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
            _log.info("Placed morning buy for %s: $%.2f, order_id=%s", ticker, notional, order_id)
        return outcomes

    def run(
        self,
        now: datetime,
        data: Any,
        *,
        data_source: Optional[str] = None,
        manual: bool = False,
        broker: Optional[BrokerAdapter] = None,
    ) -> Dict[str, Any]:
        """Evaluate the stream and write its artifacts for the active mode.

        Returns ``{"artifacts": <run_state dict>, "paths": <written paths>}``.
        The watchlist scan places no orders; the live mode surfaces
        auto-buy-eligible vs. manual_review candidates per the confidence rule.
        When ``broker`` is supplied in live mode, auto-buy-eligible candidates
        are submitted via the broker and outcomes recorded in the artifacts.
        """
        blocked = self._maybe_block_for_holiday(now, manual=manual)
        if blocked is not None:
            return blocked
        signals = self.evaluate(now, data)
        source = data_source or getattr(getattr(data, "source", None), "value", "unknown")
        trading_day = now.astimezone(EASTERN).date()
        top_up_outcomes = self._compute_top_up(now, signals)
        auto_buy_candidates = [
            s for s in self._buy_candidates(signals)
            if s.execution_status != EXEC_MANUAL_REVIEW_CANDIDATE
        ]
        # Capture remaining budget BEFORE placing orders so _place_broker_orders
        # enforces caps against the pre-run headroom.
        pre_run_remaining_daily = self.ledger.remaining_daily(trading_day)
        pre_run_remaining_weekly = self.ledger.remaining_weekly(trading_day)
        broker_outcomes: Optional[Dict[str, Any]] = None
        if broker is not None and self.mode == MORNING_LIVE_MODE:
            expected_account = getattr(self.config, "target_account_number", None)
            if expected_account and broker.account_number() != expected_account:
                raise RuntimeError(
                    f"Refusing to trade account {broker.account_number()}; "
                    f"expected {expected_account}"
                )
            broker_outcomes = self._place_broker_orders(
                auto_buy_candidates,
                trading_day,
                broker,
                top_up_outcomes,
                remaining_daily=pre_run_remaining_daily,
                remaining_weekly=pre_run_remaining_weekly,
                manual=manual,
            )
        # In live mode record only candidates whose orders were actually submitted
        # so cap-exceeded orders do not inflate the ledger and corrupt budget reporting.
        if broker_outcomes is not None:
            submitted_candidates = [
                c for c in auto_buy_candidates
                if broker_outcomes.get(c.ticker, {}).get("status") == "submitted"
            ]
            self.record_buy_candidates(trading_day, submitted_candidates, top_up_outcomes)
        else:
            self.record_buy_candidates(trading_day, auto_buy_candidates, top_up_outcomes)
        artifacts = self.build_artifacts(now, signals, source, manual=manual, top_up_outcomes=top_up_outcomes)
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
        paths = self.write_artifacts(artifacts, now)
        return {"artifacts": artifacts, "paths": paths}


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
