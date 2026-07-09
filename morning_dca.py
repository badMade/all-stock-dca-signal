"""Morning graded-DCA executor (watchlist scan + live execution streams).

This module adds a *morning* stream on top of the existing near-close strategy
library (:mod:`near_close_dca`). It is deliberately isolated from the afternoon
scheduled live task (cron ``10ce362c``, which supersedes the deleted
``ce16ba6b``):

    - It uses its OWN sizing ladder (a coarser 0x -> 3x morning ladder) rather
      than the afternoon 0x -> 5x ladder.
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
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from near_close_dca import (
    ACTIVE_TICKERS,
    EASTERN,
    EXCLUDED_TICKERS,
    Action,
    Bar,
    StrategyConfig,
    completed_bar_anchor,
    graded_buy_multiplier,
    iso_week_key,
    latest_bar_at_or_before,
)

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

# Morning sizing ladder: a coarser 0x -> 3x ladder keyed on the drop vs.
# *yesterday's regular-session close*. Thresholds are fractions of the previous
# close (0.02 == 2.0%). The largest threshold <= the actual drop wins; a drop
# below the first threshold yields no buy (0x). This is SEPARATE from the
# afternoon 0x -> 5x ladder in StrategyConfig.sizing_tiers.
#     drop < 0.1%           -> 0x  (no buy)
#     0.1% <= drop < 2.0%   -> 1x
#     2.0% <= drop < 5.0%   -> 1.5x
#     5.0% <= drop < 8.0%   -> 2x
#     drop >= 8.0%          -> 3x  (no upper cutoff)
MORNING_SIZING_TIERS: Tuple[Tuple[float, float], ...] = (
    (0.001, 1.0),
    (0.020, 1.5),
    (0.050, 2.0),
    (0.080, 3.0),
)

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
    # Minimum number of completed intraday bars over the trend window required to
    # validate the intraday trend in live mode. When fewer are available a
    # qualifying candidate is flagged manual_review_candidate rather than bought.
    trend_min_bars: int = 6

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
    import logging
    _log = logging.getLogger(__name__)

    if config_dir is None:
        _here = Path(__file__).resolve().parent
        for candidate in (_here / "config", Path.cwd() / "config"):
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
            (
                float(t["min_drop_pct"]) / 100.0,
                float(t["max_drop_pct"]) / 100.0 if t.get("max_drop_pct") is not None else float("inf"),
            )
            for t in sizing["tiers"]
        )
        kwargs["sizing_tiers"] = tiers

    budget = raw.get("budget_ledger", {})
    if "morning_daily_cap_usd" in budget:
        kwargs["morning_daily_cap_usd"] = float(budget["morning_daily_cap_usd"])
    if "morning_weekly_cap_usd" in budget:
        kwargs["morning_weekly_cap_usd"] = float(budget["morning_weekly_cap_usd"])

    artifact_paths = raw.get("artifact_paths", {})
    # Per-mode root keys take precedence; fall back to the legacy shared key.
    mode_root_key = {
        MORNING_WATCHLIST_MODE: "watchlist_run_state_root",
        MORNING_LIVE_MODE: "live_run_state_root",
    }.get(mode)
    if mode_root_key and mode_root_key in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths[mode_root_key]
    elif "morning_run_state_root" in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths["morning_run_state_root"]

    return MorningConfig(**kwargs)


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
    regular-session close and grades it on the morning 0x -> 3x ladder. Two modes
    (see :data:`MORNING_PROFILES`):

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
    ladder_name: str = "morning_0x_to_3x"
    signals_csv_name: str = "morning_signals.csv"
    latest_pointer_name: str = "latest_morning_run.json"
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
        usable = [
            b for b in bars
            if b.timestamp <= decision_dt and math.isfinite(b.close) and b.close > 0
        ]
        if len(usable) < self.config.trend_min_bars:
            warnings.append(
                f"intraday trend unvalidated: {len(usable)} usable bars < required "
                f"{self.config.trend_min_bars} over the 60-minute window"
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
        """Evaluate every non-excluded ticker for the morning stream."""
        tickers = [t for t in self.config.tickers if t not in self.config.excluded_tickers]
        return [self.evaluate_signal(t, now, data) for t in tickers]

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

        signal_rows = [s.to_row() for s in signals]
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
            Path(self.state_root) / "manual_runs" / artifacts["run_id"]
            if manual_run
            else self.run_state_dir(now)
        )
        out.mkdir(parents=True, exist_ok=True)

        run_state_path = out / "run_state.json"
        run_state_path.write_text(json.dumps(artifacts, indent=2, sort_keys=True), encoding="utf-8")

        signals_path = out / self.signals_csv_name
        _write_csv(signals_path, artifacts["signal_rows"])

        paths = {"run_state": str(run_state_path), "signals": str(signals_path)}

        if not artifacts["run_classification"]["manual_run"]:
            pointer = Path(self.state_root) / self.latest_pointer_name
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
            paths[self.latest_pointer_name.replace(".json", "")] = str(pointer)

        return paths

    def _maybe_block_for_holiday(self, now: datetime, *, manual: bool) -> Optional[Dict[str, Any]]:
        """Return a blocked-run result when ``now`` falls on an NYSE holiday.

        A weekday cron fire on a full market closure has no real session to
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
        if manual or trading_day not in nyse_holidays(trading_day.year):
            return None
        out = self.run_state_dir(now)
        out.mkdir(parents=True, exist_ok=True)
        state_path = out / "run_state.json"
        if not state_path.exists():
            state_path.write_text(
                json.dumps(
                    {"trading_day": trading_day.isoformat(), "status": "blocked",
                     "block_reason": "market holiday", "stream": self.stream},
                    indent=2, sort_keys=True,
                ),
                encoding="utf-8",
            )
        return {"artifacts": {"trading_day": trading_day.isoformat(), "status": "blocked"},
                "paths": {"run_state": str(state_path)}}

    def run(
        self,
        now: datetime,
        data: Any,
        *,
        data_source: Optional[str] = None,
        manual: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate the stream and write its artifacts for the active mode.

        Returns ``{"artifacts": <run_state dict>, "paths": <written paths>}``.
        The watchlist scan places no orders; the live mode surfaces
        auto-buy-eligible vs. manual_review candidates per the confidence rule.
        """
        blocked = self._maybe_block_for_holiday(now, manual=manual)
        if blocked is not None:
            return blocked
        signals = self.evaluate(now, data)
        source = data_source or getattr(getattr(data, "source", None), "value", "unknown")
        trading_day = now.astimezone(EASTERN).date()
        top_up_outcomes = self._compute_top_up(now, signals)
        self.record_buy_candidates(trading_day, self._buy_candidates(signals), top_up_outcomes)
        artifacts = self.build_artifacts(now, signals, source, manual=manual, top_up_outcomes=top_up_outcomes)
        paths = self.write_artifacts(artifacts, now)
        return {"artifacts": artifacts, "paths": paths}


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
