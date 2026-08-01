"""
Near-close graded DCA strategy skeleton.

This mirrors the scheduled live-test rules:
    - Run at 3:30 PM America/New_York (the canonical live schedule).
    - Use completed regular-session bars only.
    - Compare finance-side and Robinhood-side signals before auto-ordering.
    - Place real orders only through a broker adapter that reviews first.
    - Never trade outside the explicit caps, account, universe, and test dates.

The file is adapter-based and dry-run by default. The sizing, budget, and signal
logic is broker-agnostic: select a backend with ``StrategyConfig.broker`` (either
``BrokerName.ROBINHOOD`` or ``BrokerName.ALPACA``) and the matching adapter is used
without touching any of the strategy math.

Live Robinhood market-data providers and a Robinhood broker adapter live in
``dca_live_adapters`` (``RobinhoodDataProvider``, ``FinanceDataProvider``,
``RobinhoodBroker``); they take injected MCP callables so this module needs no
credentials or network access. ``run_dca`` is the runnable orchestrator/CLI that wires
config -> providers/broker -> ``run_daily_compact`` -> reports. The Alpaca adapter
builds connector-ready payloads for the ``alpaca__pipedream`` source and keeps live
placement gated behind ``AlpacaConfig.enable_live_orders`` (False by default). Paper
trading is the safest mode for connection testing; toggle it with
``AlpacaConfig.is_paper_api``.

Live contract note (2026-07-23): the authoritative scheduled live contract now
spans four Robinhood-only streams (9:45 preflight, 10:30 morning live, 12:30
midday live, 3:30 final live). Runners must validate same-day pointers before
trusting them, treat a ticker with no prior same-day context as a first
allocation (top-up only when prior same-day context exists and the price is
strictly below that ticker's most recent prior same-day reference), dedupe against same-day
submitted orders and Robinhood history before any fallback placement, place
dollar buys as notional market orders (type='market', dollar_amount,
time_in_force='gfd'; regular session only), and — for live runs that reached
signal computation — always write a non-null run_classification plus
signals.csv and submitted_orders.csv (the read-only 9:45 preflight and the
pre-signal raw-persistence failure path are standing exceptions). A 3:05
read-only monitor also runs (no trading).

JUL 23 2026 EFFECTIVE PRIOR ALLOCATION PATCH (10:30 / 12:30 / 3:20; applied to
the scheduled task text, PENDING in this module and run_dca.py, which do not
yet compute effective_prior_usd or read prior_usd_rh): compute
effective_prior_usd from deduped same-day artifacts plus Robinhood same-day order
history; do not ignore prior_usd_rh; corrected_delta_usd =
max(0, desired_usd - effective_prior_usd); skip already-met names. See
analysis/2026-07-23-live-reliability-fixes/live_task_reliability_update_2026-07-23.md.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)
from zoneinfo import ZoneInfo

from dca_fileio import atomic_open_w, atomic_write_text

if TYPE_CHECKING:
    from scripts.run_classification import PriorAllocation

try:  # pragma: no cover - import shim for flat sys.path
    from scripts.run_classification import (
        BLOCKED_BY_SPREAD_GATE,
        COMPLETION_REASON_DATE_GATE_BLOCKED,
        ERROR_CLASS_DEGRADED_MARKET_DATA,
        ERROR_CLASS_PARTIAL_PLACEMENT_FAILURE,
        ERROR_CLASS_POST_SUBMISSION_FAILURE,
        ERROR_CLASS_STARTUP_FAILURE,
        ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
        ORDER_SUBMIT_STATUS_ERROR,
        ORDER_SUBMIT_STATUS_NO_CANDIDATES,
        ORDER_SUBMIT_STATUS_NO_ORDERS,
        ORDER_SUBMIT_STATUS_SUBMITTED,
        RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
        RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION,
        RUN_CLASSIFICATION_STARTUP_FAILED,
        SPREAD_STATUS_NOT_EVALUATED,
        classify_no_order_run,
        spread_gate_fields,
        stamp_failure_classification,
    )
except ImportError:  # pragma: no cover
    from run_classification import (  # type: ignore
        BLOCKED_BY_SPREAD_GATE,
        COMPLETION_REASON_DATE_GATE_BLOCKED,
        ERROR_CLASS_DEGRADED_MARKET_DATA,
        ERROR_CLASS_PARTIAL_PLACEMENT_FAILURE,
        ERROR_CLASS_POST_SUBMISSION_FAILURE,
        ERROR_CLASS_STARTUP_FAILURE,
        ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
        ORDER_SUBMIT_STATUS_ERROR,
        ORDER_SUBMIT_STATUS_NO_CANDIDATES,
        ORDER_SUBMIT_STATUS_NO_ORDERS,
        ORDER_SUBMIT_STATUS_SUBMITTED,
        RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
        RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION,
        RUN_CLASSIFICATION_STARTUP_FAILED,
        SPREAD_STATUS_NOT_EVALUATED,
        classify_no_order_run,
        spread_gate_fields,
        stamp_failure_classification,
    )

EASTERN = ZoneInfo("America/New_York")

# Module logger. Library code never configures handlers (that is the entry point's job
# in run_dca.py / the scheduler); operator-facing run output goes through this logger so
# an unattended live task can route it to files/alerting instead of bare stdout.
logger = logging.getLogger("near_close_dca")

# Security Enhancement: Fetch account number from environment variable
# to prevent exposing sensitive identifiers in version control.
_UNSET_ACCOUNT_SENTINEL = "UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"

# The digit shape the run-classification schemas pin account_number to. Enforced
# at the shared pinning gate, not only in the config loader: the loaders'
# graceful-fallback paths (config file absent or unparseable) and any direct
# MorningConfig/StrategyConfig construction bypass resolve_broker_account
# entirely, so a malformed environment value like "acct-123" reached live
# execution and was stamped into the artifact as an account, which the schema
# then rejects -- turning a connector or mismatch failure into evidence a
# watchdog reads as missing rather than blocked (Codex review, PR #219).
_ACCOUNT_NUMBER_SHAPE = re.compile(r"^[0-9]{6,12}$")
_env_account = os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER")
ROBINHOOD_AGENTIC_ACCOUNT_NUMBER = _env_account or _UNSET_ACCOUNT_SENTINEL

# Lightweight usage-proxy fields logged to each live run's run_state so the monthly
# cost tracker can aggregate automation load without any exact credit accounting.
# All numbers are activity proxies (call/row/byte counts), never real credit spend.
USAGE_PROXY_FIELDS = (
    "tickers_scanned",
    "finance_calls_attempted",
    "robinhood_calls_attempted",
    "signal_rows_written",
    "buy_candidates_written",
    "order_reviews_attempted",
    "orders_placed_count",
    "state_file_bytes",
    "csv_artifact_count",
    "total_artifact_bytes",
    "notifications_sent",
    "errors_count",
    "manual_recovery_required",
    "cost_control_notes",
)

# Default cost-control notes recorded on the compact daily live path. They document
# the work deliberately NOT done daily so the choice is auditable from the artifact.
DAILY_COST_CONTROL_NOTES = (
    "daily compact mode: structured JSON/CSV + compact summary only",
    "narrative slippage report deferred to manual/Friday runs",
    "weekly matrix, historical backtest, and full Notion dashboard not run on daily path",
    "Notion daily sync limited to the compact Latest Run section",
)

ACTIVE_TICKERS = [
    "DFTX",
    "TRIN",
    "AAAU",
    "SPCX",
    "EVR",
    "LUNR",
    "IRDM",
    "DXYZ",
    "LITE",
    "ASML",
    "DRAM",
    "STX",
    "MSFT",
    "AAPL",
    "AMAT",
    "AMD",
    "CBRS",
    "MRVL",
    "BTX",
    "STM",
    "SNDK",
    "WDC",
    "TSM",
    "AVGO",
    "NVDA",
    "QBTS",
    "FLKR",
    "INTC",
    "GOOGL",
    "GOOG",
    "AMZN",
    "GLW",
    "MA",
    "GS",
    "MU",
    "NFLX",
    "XLG",
    "TOPT",
    "QTOP",
    "SATA",
    "RGTI",
    "IBM",
    "GFUZ",
    "TXN",
    "RKLB",
]

# BYDDY (ADR wind-down), VCX, TRLV, and RVI are excluded from the active universe.
EXCLUDED_TICKERS = {"BYDDY", "TRLV", "VCX", "RVI"}


def _weekday_range(start: date, end: date) -> set:
    """All Mon-Fri dates in [start, end] inclusive."""
    days = set()
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:  # 0=Mon .. 4=Fri
            days.add(cursor)
        cursor += timedelta(days=1)
    return days


# US equity-market full closures that fall inside the original (pre-2026-08-28)
# live window (weekdays only, so weekend holidays are already excluded).
# 2026-07-03 is the observed Independence Day (Jul 4 2026 is a Saturday). This
# hand-maintained set only covers that original window; ``nyse_holidays()``
# below is the computed, indefinitely-correct calendar used by
# ``_check_execution_gates`` and the morning/midday streams so extending
# ``config/trading_window.json`` past this set (e.g. to include Labor Day or
# Thanksgiving) can never silently reopen a holiday as a trading day.
MARKET_HOLIDAYS = frozenset({date(2026, 7, 3)})

# Approved live-test window. The original pilot dates (Jun 29 - Jul 1 2026) plus
# the canonical live window of weekdays 2026-07-02 through 2026-08-28 inclusive
# (minus market holidays), matching the ce16ba6b/10ce362c trigger_window in
# config/scheduled_tasks.json. Without this, run_once()'s date gate would mark
# every scheduled run in the live window blocked/no-trade. Callers normally
# override this default via ``config/trading_window.json``
# (``dca_config.load_approved_dates``); ``nyse_holidays()`` still applies on
# top of whichever set is active.
APPROVED_TEST_DATES = (
    {date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)}
    | _weekday_range(date(2026, 7, 2), date(2026, 8, 28))
) - MARKET_HOLIDAYS


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


def _new_years_day_observed(year: int) -> Optional[date]:
    """Return the observed New Year's Day closure for ``year``, or ``None``.

    Unlike the other fixed-date holidays here, NYSE Rule 7.2 carves out an
    explicit year-end-accounting exception for New Year's Day: when January 1
    falls on a Saturday, the Exchange stays open the preceding Friday (it
    traded a full session on 2021-12-31 despite 2022-01-01 being a Saturday)
    rather than observing the holiday early. A Sunday New Year's Day still
    shifts to the following Monday as normal.
    """
    jan_1 = date(year, 1, 1)
    if jan_1.weekday() == 5:  # Saturday -> NOT observed (Rule 7.2 exception)
        return None
    if jan_1.weekday() == 6:  # Sunday -> observed Monday
        return jan_1 + timedelta(days=1)
    return jan_1


def nyse_holidays(year: int) -> frozenset[date]:
    """Return the NYSE's full-market-closure dates for ``year``.

    Computed from the exchange's long-standing, publicly documented holiday
    rules (fixed dates with weekend observance shifts, or Nth-weekday-of-month
    rules) rather than a hardcoded per-year list, so every stream's holiday
    gate stays correct indefinitely into the future without manual updates,
    unlike :data:`MARKET_HOLIDAYS` (hand-maintained, scoped to the original
    live-test window). Good Friday is included; NYSE has observed it as a full
    closure every year on record.
    """
    holidays = {
        _nth_weekday_of_month(year, 1, 0, 3),  # MLK Day: 3rd Monday of January
        _nth_weekday_of_month(
            year, 2, 0, 3
        ),  # Washington's Birthday: 3rd Monday of February
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday_of_month(year, 5, 0),  # Memorial Day: last Monday of May
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day: 1st Monday of September
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving: 4th Thursday of November
        _observed(date(year, 12, 25)),  # Christmas
    }
    new_years = _new_years_day_observed(year)
    if new_years is not None:
        holidays.add(new_years)
    return frozenset(holidays)


class Action(str, Enum):
    BUY = "BUY"
    SKIP = "SKIP"
    NO_ACTION = "NO_ACTION"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NO_DATA = "NO_DATA"


class SourceName(str, Enum):
    FINANCE = "finance"
    ROBINHOOD = "robinhood"


class BrokerName(str, Enum):
    """Selectable broker backends. Strategy math is identical across backends."""

    ROBINHOOD = "robinhood"
    ALPACA = "alpaca"


# Alpaca connector (Pipedream) identifiers and tool names. These describe the
# adapter-compatible payloads the strategy builds; they are never executed here.
ALPACA_SOURCE_ID = "alpaca__pipedream"
ALPACA_ACCOUNT_INFO_TOOL = "alpaca-get-account-info"
ALPACA_LIST_ORDERS_TOOL = "alpaca-list-orders"
ALPACA_LIST_POSITIONS_TOOL = "alpaca-list-positions"
ALPACA_PLACE_ORDER_TOOL = "alpaca-place-order"


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SourceSignal:
    ticker: str
    source: SourceName
    action: Action
    reason: str
    decision_price: float = 0.0
    previous_close: float = 0.0
    lookback_price: float = 0.0
    price_vs_previous_close_pct: float = 0.0
    intraday_trend_pct: float = 0.0
    drop_vs_previous_close_pct: float = 0.0
    buy_multiplier: float = 0.0
    suggested_amount_usd: float = 0.0
    # Prior regular-session low and the first-allocation base scale selected from
    # it (see StrategyConfig.base_buy_amount_below_close_usd and
    # select_first_allocation_base_usd). base_selection_reason records which branch
    # was taken. suggested_amount_usd == selected_base_amount_usd * buy_multiplier.
    previous_low: float = 0.0
    selected_base_amount_usd: float = 0.0
    base_selection_reason: str = ""
    decision_bar_timestamp: Optional[datetime] = None
    previous_close_timestamp: Optional[datetime] = None
    lookback_bar_timestamp: Optional[datetime] = None
    # Signal-time quote is the intended target price for execution. It defaults to
    # the completed decision-bar close but a provider may supply a distinct
    # last-trade/quote captured at signal time via get_quote(). decision_bar_close
    # is always the completed regular-session bar close, retained separately.
    quote_price: float = 0.0
    quote_time: Optional[datetime] = None
    decision_bar_close: float = 0.0
    # True when this signal stands in for a provider exception rather than a
    # genuine "no data" answer. Reconciliation must never auto-buy past it.
    provider_error: bool = False


@dataclass(frozen=True)
class ReconciledDecision:
    ticker: str
    action: Action
    reason: str
    finance_signal: Optional[SourceSignal]
    robinhood_signal: Optional[SourceSignal]
    buy_multiplier: float = 0.0
    suggested_amount_usd: float = 0.0
    # The first-allocation base ($1 or $2) behind suggested_amount_usd (see
    # select_first_allocation_base_usd). Persisted so a later same-day top-up
    # guard call can reuse THIS run's base instead of recomputing one from its
    # own (possibly lower) price -- see evaluate_top_up_guard.
    selected_base_amount_usd: float = 0.0
    # Execution target derived from the source that drives the buy. target_price is
    # the signal-time quote by default; decision_bar_close is retained separately.
    target_price: float = 0.0
    target_price_source: str = ""
    target_quote_time: Optional[datetime] = None
    decision_bar_close: float = 0.0


# 2:30 PM ET boundary. Morning/manual (market-open recovery and future morning
# session) runs happen before this time; the scheduled afternoon run happens after
# it. Orders from before this boundary may be read ONLY as prior-allocation context
# for the same-day top-up guard -- never as scheduled afternoon execution evidence,
# slippage, watchdog confirmation, scheduled cost basis, or scheduled fill counts.
MORNING_MANUAL_BOUNDARY_ET = time(14, 30)

# Sentinel used when a prior run has no parseable generated_at timestamp. It sorts
# before every real timestamp so that, among untimestamped runs, later scan/merge
# order (paths are scanned in ascending sorted order) still wins the reference.
_TS_FLOOR = datetime.min.replace(tzinfo=timezone.utc)

# top_up_guard_result values recorded per candidate on the scheduled afternoon run.
TOP_UP_NO_PRIOR_RUN = "no_prior_run"  # normal allocation; nothing bought earlier today
TOP_UP_INCREMENTAL = "incremental_top_up"  # buy only the incremental dollar difference
# Legacy morning/midday-guard-only codes (scripts.run_classification.incremental_top_up_amount
# and morning_dca.py's own guard still emit these for their own sessions). The scheduled
# afternoon guard (evaluate_top_up_guard below) uses the unified canonical code instead --
# see TOP_UP_NOT_LOWER_OR_NO_DELTA.
TOP_UP_PRICE_NOT_BELOW = (
    "price_not_below_prior_run"  # afternoon price >= prior ref -> $0
)
TOP_UP_NO_INCREMENTAL = (
    "no_incremental_difference"  # afternoon desired <= prior amount -> $0
)
# Canonical rule item 12 (config/scheduled_tasks.json canonical_first_buy_and_topup_rule):
# the scheduled afternoon guard (evaluate_top_up_guard) emits this single unified code
# whenever a same-day top-up is skipped because the price is not strictly lower than the
# ticker's same-day reference, OR the recomputed corrected_delta_usd is 0 -- replacing the
# two legacy codes above for that guard specifically.
TOP_UP_NOT_LOWER_OR_NO_DELTA = (
    "same_day_topup_not_lower_or_no_delta"  # afternoon price not lower or delta 0 -> $0
)
TOP_UP_MISSING_PRIOR_PRICE = (
    "missing_prior_price"  # prior amount known but no ref price -> $0 (manual review)
)
TOP_UP_MISSING_PRIOR_BASE = "missing_prior_base"  # prior amount known but no resolved base -> $0 (manual review)


@dataclass(frozen=True)
class PriorSameDayAllocation:
    """Allocation already bought for a ticker by an earlier same-day (morning/manual) run.

    Read as prior-allocation context ONLY by the scheduled afternoon top-up guard;
    these runs are never counted as scheduled evidence/metrics. ``reference_price`` is
    the earlier run's decision/fill/target price for the ticker (None when the earlier
    artifact recorded an amount but no usable price).
    """

    ticker: str
    amount_usd: float = 0.0
    reference_price: Optional[float] = None
    run_id: Optional[str] = None
    source: Optional[str] = None
    # The contributing run's generated_at, parsed, for the run that established
    # reference_price -- so merging allocations from different roots/streams can
    # pick the ticker's MOST RECENT valid same-day reference (canonical rule item
    # 9) rather than the lowest. None when unresolved/unavailable.
    reference_ts: Optional[datetime] = None
    # The first same-day allocation's selected_base_amount_usd ($1 or $2), when the
    # contributing artifact recorded one. None for older artifacts that predate this
    # field, or when it could not be resolved -- never treat that as $0. A caller with
    # a known prior amount_usd but this still None must fail closed (bounded reload,
    # then manual review) rather than silently recompute the base from the current
    # run's own price (Codex review: "Fail closed when the first allocation base is
    # unknown"); see run_dca._tickers_with_unresolved_same_day_context and
    # near_close_dca.evaluate_top_up_guard / morning_dca._evaluate_top_up.
    selected_base_amount_usd: Optional[float] = None
    # The contributing run's generated_at (ISO string) that established
    # selected_base_amount_usd, so merging allocations from different roots/streams
    # (e.g. run_dca.py's afternoon-root + cross-stream-root merge) can pick the
    # chronologically FIRST allocation's base rather than whichever root happened
    # to be merged first. None when unresolved/unavailable.
    selected_base_amount_usd_at: Optional[str] = None


@dataclass(frozen=True)
class TopUpGuardOutcome:
    """Per-ticker result of the same-day prior-run offset/top-up guard."""

    ticker: str
    afternoon_desired_amount: float
    afternoon_decision_price: Optional[float]
    prior_same_day_amount: float
    prior_same_day_reference_price: Optional[float]
    prior_same_day_run_id: Optional[str]
    prior_same_day_source: Optional[str]
    incremental_order_amount: float
    top_up_guard_result: str
    skipped: bool
    manual_review: bool = False
    # The base ($1 or $2) that actually produced afternoon_desired_amount -- the
    # preserved first-allocation base when reused (see
    # resolve_top_up_desired_amount_usd), otherwise this run's own freshly
    # selected base. A candidate-row artifact should record THIS, not the
    # current run's own selected_base_amount_usd, so it stays consistent with
    # the amount actually calculated (Codex review: "Persist the preserved
    # base on top-up candidates").
    effective_base_amount_usd: float = 0.0


@dataclass(frozen=True)
class BudgetTier:
    """A budget tier with its absolute daily and weekly notional ceilings."""

    name: str
    daily_cap_usd: float
    weekly_cap_usd: float
    uses_rollover_reserve: bool = False


# Ordered low-to-high. The live $2-allocation policy selects the highest tier
# triggered by EITHER the day's planned spend or the count of 3x-or-greater
# signals. These tier values are NOT loaded from config at runtime; they must be
# kept in sync with config/budgets.json, which documents the same policy.
BUDGET_TIERS: tuple[BudgetTier, ...] = (
    BudgetTier("normal", 180.0, 900.0),
    BudgetTier("elevated", 240.0, 1200.0),
    BudgetTier("high_volatility", 300.0, 1500.0),
    BudgetTier("stress", 360.0, 1800.0, uses_rollover_reserve=True),
)

NORMAL_WEEKLY_CAP_USD = BUDGET_TIERS[0].weekly_cap_usd

# Escalation thresholds, named rather than inlined as bare numbers in the
# branches below. Each entry is the value a run must EXCEED to reach the tier at
# the same index + 1, so both tuples are read low-to-high alongside BUDGET_TIERS
# and the "> threshold" boundary is stated once instead of per branch.
#
# Both are "headroom" triggers: escalate before the current tier's ceiling is
# actually hit, so a run that is trending large gets its larger cap before it
# starts refusing buys. $129 is ~72% of the normal tier's $180 daily cap, $213
# ~89% of elevated's $240, $272 ~91% of high_volatility's $300. The signal counts
# are the matching "how broad is the selloff" trigger — 6, 11, and 18+ tickers
# at 3x-or-greater. Mirrored in config/budgets.json -> budget_tiers[].select_when
# and pinned against it by test_config_invariants.py.
TIER_SPEND_THRESHOLDS_USD: tuple[float, ...] = (129.0, 213.0, 272.0)
TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS: tuple[int, ...] = (5, 10, 17)


def _escalation_index(value: float, thresholds: Sequence[float]) -> int:
    """Number of ``thresholds`` strictly exceeded by ``value`` — i.e. the tier index."""
    return sum(1 for threshold in thresholds if value > threshold)


def select_budget_tier(
    planned_spend_usd: float, high_multiplier_signal_count: int
) -> BudgetTier:
    """Select the budget tier for a run from planned spend and 3x+ signal count.

    Each dimension independently maps to a tier index and the HIGHER of the two
    wins, so either a large planned spend or a broad selloff can escalate the
    day's caps on its own:

        spend:   <=129 -> 0, >129 -> 1, >213 -> 2, >272 -> 3
        signals: <=5   -> 0, 6-10 -> 1, 11-17 -> 2, >17 -> 3

    See TIER_SPEND_THRESHOLDS_USD / TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS for
    where the boundaries come from.
    """
    spend_idx = _escalation_index(planned_spend_usd, TIER_SPEND_THRESHOLDS_USD)
    signal_idx = _escalation_index(
        high_multiplier_signal_count, TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS
    )
    return BUDGET_TIERS[max(spend_idx, signal_idx)]


@dataclass(frozen=True)
class StrategyConfig:
    tickers: Sequence[str] = tuple(ACTIVE_TICKERS)
    excluded_tickers: frozenset[str] = frozenset(EXCLUDED_TICKERS)
    approved_test_dates: frozenset[date] = frozenset(APPROVED_TEST_DATES)
    decision_time_et: time = time(
        15, 30
    )  # matches the 3:30 PM ET canonical live schedule
    trend_lookback_minutes: int = 60
    # First-allocation base amount selected before the ladder multiplier. When the
    # decision price is below the prior regular-session close AND the prior
    # regular-session low, the full ``base_buy_amount_usd`` ($2) scale is used;
    # when it is below the prior close only (not below the prior low), the reduced
    # ``base_buy_amount_below_close_usd`` ($1) scale is used. Not below the prior
    # close is no buy. The 0x-to-5x ladder multiplier is applied after this
    # selection, so the per-order ceiling stays 5x * $2 = $10.
    base_buy_amount_usd: float = 2.0
    base_buy_amount_below_close_usd: float = 1.0
    max_per_order_usd: float = 10.0
    weekly_rollover_reserve_cap_usd: float = 900.0
    high_multiplier_threshold: float = 3.0
    minimum_drop_vs_previous_close: float = 0.001
    max_buys_per_ticker_per_day: int = 1
    one_order_per_ticker_per_day: bool = False
    max_bid_ask_spread_pct: float = 1.0
    target_account_number: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
    broker: BrokerName = BrokerName.ROBINHOOD
    dry_run: bool = True

    # Bars from many market-data APIs are start-stamped. At a 3:30 PM decision,
    # the latest completed 15-minute start-stamped bar is 3:05 PM.
    assume_bar_timestamps_are_start_times: bool = True
    finance_interval_minutes: int = 15
    robinhood_interval_minutes: int = 5

    # Graded sizing ladder mapping the drop from prior close to a buy multiplier.
    # Thresholds are fractions of the previous close (0.001 == 0.1%). The largest
    # threshold less than or equal to the actual drop wins; a drop below the first
    # threshold yields no buy (0x). There is no upper cutoff: any drop >= 8.0% is 5x.
    #     drop < 0.1%             -> 0x  (no buy)
    #     0.1% <= drop < 1.0%     -> 1x
    #     1.0% <= drop < 2.0%     -> 1.5x
    #     2.0% <= drop < 3.0%     -> 2x
    #     3.0% <= drop < 4.0%     -> 2.5x
    #     4.0% <= drop < 5.0%     -> 3x
    #     5.0% <= drop < 6.0%     -> 3.5x
    #     6.0% <= drop < 7.0%     -> 4x
    #     7.0% <= drop < 8.0%     -> 4.5x
    #     drop >= 8.0%            -> 5x
    sizing_tiers: tuple[tuple[float, float], ...] = (
        (0.001, 1.0),  # down 0.1% to <1.0%
        (0.010, 1.5),  # down 1.0% to <2.0%
        (0.020, 2.0),  # down 2.0% to <3.0%
        (0.030, 2.5),  # down 3.0% to <4.0%
        (0.040, 3.0),  # down 4.0% to <5.0%
        (0.050, 3.5),  # down 5.0% to <6.0%
        (0.060, 4.0),  # down 6.0% to <7.0%
        (0.070, 4.5),  # down 7.0% to <8.0%
        (0.080, 5.0),  # down 8.0% or more (no upper cutoff)
    )

    # --- Dynamic risk guard (rolling deployment kill-switch) -----------------
    # The monthly cash allocation the strategy is funded with, and the fraction
    # of it that, once deployed within any rolling 7-day window, engages the
    # kill-switch. When engaged, the graded ladder is capped at 1x so the
    # strategy keeps participating in dips at the base amount but stops scaling
    # up. deployed_last_7d_usd is a *runtime* field injected per run from the
    # deployment history (see dca_risk.apply_risk_guard); it is never read from
    # config JSON, so loading config can never by itself change sizing.
    monthly_cash_allocation_usd: float = 3600.0
    kill_switch_7d_deploy_fraction: float = 0.40
    deployed_last_7d_usd: float = 0.0

    # When True the 60-minute intraday trend filter is skipped for the buy
    # decision: a ticker below its prior close with multiplier > 0 is bought
    # regardless of whether the 60-minute trend is up or flat. All other guards
    # (minimum drop, spread, budget, data confidence, hard circuit breakers) still
    # apply. Set to True for the final scheduled live buy of the day (3:30 PM ET)
    # per the final_buy_behavior policy in config/scheduled_tasks.json.
    ignore_trend_filter: bool = False


@dataclass
class StrategyState:
    buys_by_ticker_by_day: Dict[str, Dict[date, int]] = field(default_factory=dict)
    submitted_notional_by_day: Dict[date, float] = field(default_factory=dict)
    submitted_notional_by_week: Dict[tuple[int, int], float] = field(
        default_factory=dict
    )
    weekly_rollover_reserve_usd: float = 0.0
    selected_tier: Optional[BudgetTier] = None
    finalized_budget_days: Set[date] = field(default_factory=set)
    # ISO-week key the current ``weekly_rollover_reserve_usd`` was computed for.
    # Guards the roll-forward so it runs exactly once per week, independent of the
    # order in which days are finalized vs. queried (a quiet week with no BUY
    # candidates must still roll the reserve before that day is finalized).
    reserve_week_key: Optional[tuple[int, int]] = None
    # (ticker, trading_day) pairs THIS state instance has booked its own order
    # for -- via a fresh placement or a same-stream duplicate-preserved retry.
    # Deliberately separate from buys_by_ticker_by_day: _seed_prior_allocations
    # also calls record_buy (to seed CROSS-stream morning/midday context into
    # this ticker's day/week spend), which bumps buys_today to a nonzero value
    # with no order of THIS stream's own ever booked. A same-stream duplicate
    # check keyed off buys_today alone would then wrongly treat a freshly
    # restarted process's never-recorded afternoon order as already booked
    # (Codex review, PR #231: "Record the recovered buy separately from seeded
    # prior context").
    own_orders_recorded: Set[Tuple[str, date]] = field(default_factory=set)

    def buys_today(self, ticker: str, trading_day: date) -> int:
        return self.buys_by_ticker_by_day.get(ticker, {}).get(trading_day, 0)

    def record_buy(self, ticker: str, trading_day: date, notional_usd: float) -> None:
        self.buys_by_ticker_by_day.setdefault(ticker, {})
        self.buys_by_ticker_by_day[ticker][trading_day] = (
            self.buys_today(ticker, trading_day) + 1
        )
        self.submitted_notional_by_day[trading_day] = (
            self.day_notional(trading_day) + notional_usd
        )
        self.submitted_notional_by_week[iso_week_key(trading_day)] = (
            self.week_notional(trading_day) + notional_usd
        )

    def day_notional(self, trading_day: date) -> float:
        return self.submitted_notional_by_day.get(trading_day, 0.0)

    def week_notional(self, trading_day: date) -> float:
        return self.submitted_notional_by_week.get(iso_week_key(trading_day), 0.0)

    def current_tier(self) -> BudgetTier:
        """Return the tier selected for the current run, defaulting to Normal."""
        return self.selected_tier or BUDGET_TIERS[0]

    def select_daily_budget(
        self,
        planned_spend_usd: float,
        high_multiplier_signal_count: int,
        config: StrategyConfig,
    ) -> BudgetTier:
        """Choose and store the budget tier for the run from the day's plan."""
        tier = select_budget_tier(planned_spend_usd, high_multiplier_signal_count)
        self.selected_tier = tier
        return tier

    def roll_weekly_reserve(self, trading_day: date, config: StrategyConfig) -> None:
        """Roll the weekly rollover reserve forward for ``trading_day``'s week.

        The reserve reflects the *prior* week's unused Normal-cap capacity (itself
        capped by ``weekly_rollover_reserve_cap_usd``) and stays constant for the
        whole week. It is computed exactly once per week, keyed by
        ``reserve_week_key``, so the result does not depend on whether the day has
        already been finalized or how many times this is called -- a quiet week
        with no BUY candidates still rolls the reserve before the day is finalized.
        """
        week_key = iso_week_key(trading_day)
        if self.reserve_week_key == week_key:
            return
        # Base the reserve on the most recent *earlier* week that saw activity.
        prior_weeks = [
            iso_week_key(day)
            for day in self.finalized_budget_days
            if iso_week_key(day) < week_key
        ]
        if prior_weeks:
            last_week_key = max(prior_weeks)
            unused_prev = max(
                0.0,
                NORMAL_WEEKLY_CAP_USD
                - self.submitted_notional_by_week.get(last_week_key, 0.0),
            )
            self.weekly_rollover_reserve_usd = min(
                config.weekly_rollover_reserve_cap_usd, unused_prev
            )
        else:
            self.weekly_rollover_reserve_usd = 0.0
        self.reserve_week_key = week_key

    def effective_weekly_cap(self) -> float:
        """Return the weekly notional ceiling the selected tier will actually allow.

        For the stress tier this is the Normal weekly cap plus the current rollover
        reserve (bounded by the tier's nominal weekly cap); with only a partial
        reserve it is below the nominal $1,800 tier cap. All other tiers allow
        their nominal weekly cap.
        """
        tier = self.current_tier()
        if tier.uses_rollover_reserve:
            return min(
                tier.weekly_cap_usd,
                NORMAL_WEEKLY_CAP_USD + self.weekly_rollover_reserve_usd,
            )
        return tier.weekly_cap_usd

    def allowed_spend_today(self, trading_day: date, config: StrategyConfig) -> float:
        """Return the max strategy spend allowed today under the selected tier caps.

        The weekly ceiling is the selected tier's weekly cap. Stress tier capacity
        above the Normal weekly cap is available only to the extent of the prior
        unused weekly rollover reserve (itself capped by
        ``weekly_rollover_reserve_cap_usd``).
        """
        self.roll_weekly_reserve(trading_day, config)
        effective_weekly = self.effective_weekly_cap()
        remaining_weekly = max(0.0, effective_weekly - self.week_notional(trading_day))
        return max(0.0, min(self.current_tier().daily_cap_usd, remaining_weekly))

    def remaining_spend_today(self, trading_day: date, config: StrategyConfig) -> float:
        """Return remaining strategy spend after same-day submitted notional."""
        return max(
            0.0,
            self.allowed_spend_today(trading_day, config)
            - self.day_notional(trading_day),
        )

    def finalize_daily_budget(self, trading_day: date, config: StrategyConfig) -> None:
        """Mark the current trading day as finalized.

        The weekly rollover reserve is rolled forward at the start of a new week in
        ``allowed_spend_today``, so it stays constant during the week and correctly
        reflects the *prior* week's unused Normal-cap capacity. This method only
        records that the day's budget has been finalized.
        """
        if trading_day in self.finalized_budget_days:
            return
        self.finalized_budget_days.add(trading_day)


class MarketDataProvider(Protocol):
    source: SourceName

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        """Return the prior completed regular-session close bar before as_of."""

    def get_intraday_bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> List[Bar]:
        """Return regular-session intraday bars for the given interval."""


@dataclass(frozen=True)
class OrderFill:
    """Broker-reported fill details used to compute execution slippage."""

    avg_fill_price: float
    quantity_filled: float
    order_submitted_at: Optional[datetime] = None
    order_filled_at: Optional[datetime] = None


class OrderConfirmationRequiredError(Exception):
    """Raised by a BrokerAdapter when review passed but submission needs confirmation.

    Some brokers require additional post-review user confirmation (e.g. a
    step-up/2FA gate) before a reviewed order will actually submit. This is a
    normal, expected broker response, not a placement failure: no order was
    attempted and nothing needs retrying by this run. Per
    schemas/run_state.schema.json, a run that hits this is still valid
    scheduled-run evidence that the task executed and passed review; it must be
    recorded distinctly (``not_submitted_confirmation_required``) rather than
    counted as an error.
    """


class BrokerAdapter(Protocol):
    def account_number(self) -> str:
        """Return the account number that will be used for orders."""

    def available_buying_power(self) -> float:
        """Return available buying power for the target account."""

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        """Return True if this strategy already submitted an order for ticker today."""

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        """Return the live same-day order whose ref id matches one of ``ref_id_prefixes``.

        Used to narrow the broad same-day-order check to only this stream's own
        idempotency ids, so a guarded top-up candidate (whose broad check is
        intentionally bypassed to allow topping up a cross-stream allocation)
        can still detect a retry of ITS OWN earlier same-day order.
        """

    def same_day_ticker_notional_usd(self, ticker: str, trading_day: date) -> float:
        """Return the total dollar notional of every live same-day order for ticker.

        Unlike the two methods above (existence checks scoped to this
        strategy's own ref-id conventions), this sums EVERY live-state broker
        order for the ticker/day regardless of ref id or which stream/executor
        placed it -- the real, ground-truth figure a guarded top-up's
        incremental delta must never under-count relative to (see
        ``evaluate_top_up_guard``'s KNOWN GAP note).
        """

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        """Return True only if the instrument can receive this notional buy."""

    def review_market_buy(self, ticker: str, notional_usd: float) -> tuple[bool, str]:
        """Return (approved, reason) from broker-side order review."""

    def place_market_buy(self, ticker: str, notional_usd: float, ref_id: str) -> str:
        """Place a notional regular-hours market buy and return an order id.

        May raise ``OrderConfirmationRequiredError`` when review passed but the
        broker requires additional confirmation before it will submit.
        """

    def get_order_fill(self, order_id: str) -> Optional[OrderFill]:
        """Return fill details for a placed order, or None if not yet filled."""

    @property
    def supports_concurrent_fill_lookup(self) -> bool:
        """Whether get_order_fill may be called from multiple threads at once.

        Defaults to False. get_order_fill is commonly a thin wrapper around a
        scheduler-owned MCP callable that is not guaranteed thread-safe; only
        override to True once a broker's wiring is verified safe for concurrent
        calls (see docs/live_wiring.md).
        """
        return False

    def get_raw_placement_responses(self) -> List[Dict[str, Any]]:
        """Return raw broker response dicts for every order placed this run.

        Used to persist ``raw/orders_after_place.json`` for durable fill
        data so the slippage report is replayable without a live broker
        re-query. Brokers that do not support this may return ``[]``.
        """
        return []


class DryRunBroker:
    def __init__(
        self,
        cash: float = 300.0,
        account: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
        reference_prices: Optional[Dict[str, float]] = None,
        simulated_slippage_pct: float = 0.0,
    ) -> None:
        self._cash = cash
        self._account = account
        # When reference prices are supplied the dry-run broker simulates fills so the
        # slippage/coverage pipeline can be exercised end-to-end without a live broker.
        self._reference_prices = dict(reference_prices or {})
        self._simulated_slippage_pct = simulated_slippage_pct
        self._fills: Dict[str, OrderFill] = {}

    def account_number(self) -> str:
        return self._account

    def available_buying_power(self) -> float:
        return self._cash

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        return None

    def same_day_ticker_notional_usd(self, ticker: str, trading_day: date) -> float:
        return 0.0

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        return True

    def review_market_buy(self, ticker: str, notional_usd: float) -> tuple[bool, str]:
        return True, "dry-run review approved"

    def place_market_buy(self, ticker: str, notional_usd: float, ref_id: str) -> str:
        order_id = f"DRY-RUN-{ticker}-{notional_usd:.2f}-{ref_id}"
        reference = self._reference_prices.get(ticker)
        if reference:
            submitted_at = datetime.now(tz=EASTERN)
            fill_price = reference * (1.0 + self._simulated_slippage_pct / 100.0)
            self._fills[order_id] = OrderFill(
                avg_fill_price=fill_price,
                quantity_filled=notional_usd / fill_price if fill_price else 0.0,
                order_submitted_at=submitted_at,
                order_filled_at=submitted_at + timedelta(seconds=1),
            )
        return order_id

    def get_order_fill(self, order_id: str) -> Optional[OrderFill]:
        return self._fills.get(order_id)

    def get_raw_placement_responses(self) -> List[Dict[str, Any]]:
        return []


def format_notional(amount_usd: float) -> str:
    """Format a dollar amount as the string Alpaca expects for notional orders."""
    return f"{amount_usd:.2f}"


@dataclass(frozen=True)
class AlpacaConfig:
    """Alpaca backend settings.

    ``is_paper_api`` selects paper vs. live at the connector; paper is safest for
    connection testing. ``enable_live_orders`` is the hard gate on real placement:
    while False (default), the adapter builds the order payload but never submits it.
    """

    is_paper_api: bool = True
    enable_live_orders: bool = False
    time_in_force: str = "day"
    extended_hours: bool = False
    # Dry-run buying power used when no live account-info callable is wired in.
    simulated_buying_power_usd: float = 300.0


def build_alpaca_account_info_payload(is_paper_api: bool) -> Dict[str, Any]:
    return {
        "source_id": ALPACA_SOURCE_ID,
        "tool": ALPACA_ACCOUNT_INFO_TOOL,
        "input": {"isPaperAPI": is_paper_api},
    }


def build_alpaca_list_orders_payload(
    is_paper_api: bool,
    symbols: Sequence[str],
    side: str = "buy",
    status: str = "all",
    after: Optional[str] = None,
    until: Optional[str] = None,
    nested: str = "",
) -> Dict[str, Any]:
    order_input: Dict[str, Any] = {
        "isPaperAPI": is_paper_api,
        "symbols": list(symbols),
        "side": side,
        "status": status,
        "nested": nested,
    }
    if after is not None:
        order_input["after"] = after
    if until is not None:
        order_input["until"] = until
    return {
        "source_id": ALPACA_SOURCE_ID,
        "tool": ALPACA_LIST_ORDERS_TOOL,
        "input": order_input,
    }


def build_alpaca_list_positions_payload(is_paper_api: bool) -> Dict[str, Any]:
    return {
        "source_id": ALPACA_SOURCE_ID,
        "tool": ALPACA_LIST_POSITIONS_TOOL,
        "input": {"isPaperAPI": is_paper_api},
    }


def build_alpaca_market_buy_payload(
    symbol: str,
    notional_usd: float,
    is_paper_api: bool = True,
    time_in_force: str = "day",
    extended_hours: bool = False,
) -> Dict[str, Any]:
    """Build a regular-hours market buy payload using notional as a string."""
    return {
        "source_id": ALPACA_SOURCE_ID,
        "tool": ALPACA_PLACE_ORDER_TOOL,
        "input": {
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "timeInForce": time_in_force,
            "notional": format_notional(notional_usd),
            "extendedHours": extended_hours,
            "isPaperAPI": is_paper_api,
        },
    }


class AlpacaBroker:
    """BrokerAdapter for Alpaca via the ``alpaca__pipedream`` connector.

    The adapter is a skeleton: it builds connector-ready payloads and only submits
    a real order when ``AlpacaConfig.enable_live_orders`` is True and a ``place_order_fn``
    is wired in. With the defaults it is entirely dry-run and never calls the network.
    Inject callables to connect it to the live connector without changing the strategy.
    """

    def __init__(
        self,
        config: Optional[AlpacaConfig] = None,
        account: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
        place_order_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
        account_info_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        list_orders_fn: Optional[
            Callable[[Dict[str, Any]], List[Dict[str, Any]]]
        ] = None,
    ) -> None:
        self.config = config or AlpacaConfig()
        self._account = account
        self._place_order_fn = place_order_fn
        self._account_info_fn = account_info_fn
        self._list_orders_fn = list_orders_fn

    def account_number(self) -> str:
        return self._account

    def available_buying_power(self) -> float:
        if self._account_info_fn is not None:
            info = self._account_info_fn(
                build_alpaca_account_info_payload(self.config.is_paper_api)
            )
            return float(info.get("buying_power", info.get("cash", 0.0)))
        return self.config.simulated_buying_power_usd

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        if self._list_orders_fn is None:
            return False
        day_iso = trading_day.isoformat()
        payload = build_alpaca_list_orders_payload(
            is_paper_api=self.config.is_paper_api,
            symbols=[ticker],
            side="buy",
            status="all",
            after=f"{day_iso}T00:00:00Z",
            until=f"{day_iso}T23:59:59Z",
        )
        return bool(self._list_orders_fn(payload))

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        # build_alpaca_market_buy_payload carries no client-order-id field, so
        # place_market_buy below never sends this strategy's ref_id to the
        # live connector -- no listed Alpaca order could ever be matched by
        # ref_id here even with list_orders_fn wired. Returning None ("no
        # known duplicate") would silently disable retry protection for a
        # guarded top-up: an explicitly enabled live AlpacaBroker could then
        # place a second real order on every retry. Fail closed instead --
        # this raises, which _same_stream_order_exists's caller already
        # converts into a blocked candidate (Codex review, PR #231: "Fail
        # closed for Alpaca until client IDs are wired") -- the same posture
        # as an adapter that doesn't implement this method at all. Real
        # conflict protection for this adapter requires plumbing a
        # client-order-id through the connector payload and inspecting
        # listed orders here -- out of scope while ALPACA is not a selected
        # backend in any config.
        raise RuntimeError(
            "AlpacaBroker cannot verify same-stream order duplicates: "
            "place_market_buy does not send a client order id the live "
            "connector could match against"
        )

    def same_day_ticker_notional_usd(self, ticker: str, trading_day: date) -> float:
        # Same posture as same_day_strategy_order_exists_with_ref_id_prefix
        # above: real order-history verification for this adapter is out of
        # scope while ALPACA is not a selected backend in any config. Fail
        # closed rather than silently returning 0.0 and letting a guarded
        # top-up trust an artifact-only figure this adapter cannot verify.
        raise RuntimeError(
            "AlpacaBroker cannot verify same-day order notional: "
            "list_orders_fn wiring for this adapter is not implemented"
        )

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        return True

    def review_market_buy(self, ticker: str, notional_usd: float) -> tuple[bool, str]:
        return True, "alpaca dry-run review approved"

    def build_market_buy_payload(
        self, ticker: str, notional_usd: float
    ) -> Dict[str, Any]:
        return build_alpaca_market_buy_payload(
            symbol=ticker,
            notional_usd=notional_usd,
            is_paper_api=self.config.is_paper_api,
            time_in_force=self.config.time_in_force,
            extended_hours=self.config.extended_hours,
        )

    def place_market_buy(self, ticker: str, notional_usd: float, ref_id: str) -> str:
        payload = self.build_market_buy_payload(ticker, notional_usd)
        if self.config.enable_live_orders and self._place_order_fn is not None:
            return self._place_order_fn(payload)
        mode = "PAPER" if self.config.is_paper_api else "LIVE"
        return f"ALPACA-DRYRUN-{mode}-{ticker}-{payload['input']['notional']}-{ref_id}"


def account_pinning_failure(
    target_account_number: Optional[str],
    broker: "BrokerName | str | None" = None,
) -> Optional[str]:
    """Return why ``target_account_number`` cannot pin a live run, or ``None`` if it can.

    A live run must know, explicitly, which brokerage account it is allowed to trade.
    Three values fail that test:

      * falsy (``None``/``""``) — nothing was configured, so a
        ``configured != actual`` comparison short-circuits and silently permits
        ANY account the adapter happens to be wired to;
      * :data:`_UNSET_ACCOUNT_SENTINEL` — ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` was
        not set in the environment, so the module-level default is a placeholder,
        not an account;
      * for Robinhood only, anything that is not 6-12 digits.

    The digit shape is Robinhood's, not every broker's. Applying it unconditionally
    made ``build_broker`` reject a perfectly normal Alpaca identifier (a ``PA...``
    paper account) before execution ever reached the ALPACA branch, so the
    documented broker-agnostic live adapter could not be constructed at all
    (Codex review, PR #219). ``broker`` defaults to Robinhood because every
    caller that omits it -- the morning and midday streams -- is a Robinhood
    stream, and the artifact schema requires the digit shape for those runs.
    The nonempty/placeholder checks above are broker-agnostic and always apply:
    no backend may trade an account nobody named.

    Shared by both live execution paths on purpose. ``build_broker`` refuses to
    construct a live adapter, and ``morning_dca``'s class-based executor refuses to
    place, on exactly the same condition — previously the executor guarded with
    ``if expected_account and ...``, so an empty configured value disabled its
    account check entirely rather than blocking the run.
    """
    if not target_account_number:
        return (
            "config.target_account_number is unset "
            f"({target_account_number!r}), so the live account cannot be pinned"
        )
    if target_account_number == _UNSET_ACCOUNT_SENTINEL:
        return (
            "config.target_account_number is the unset placeholder "
            f"({target_account_number!r}); the ROBINHOOD_AGENTIC_ACCOUNT_NUMBER "
            "environment variable is not set"
        )
    if broker is None or BrokerName(broker) == BrokerName.ROBINHOOD:
        if not _ACCOUNT_NUMBER_SHAPE.fullmatch(target_account_number):
            return (
                f"config.target_account_number is a {len(target_account_number)}-character "
                "value that is not a valid Robinhood account number (expected 6-12 "
                "digits); the value is withheld because a malformed one is often a "
                "secret pasted into the wrong variable"
            )
    return None


def build_broker(
    config: StrategyConfig,
    alpaca_config: Optional[AlpacaConfig] = None,
    **adapter_kwargs: Any,
) -> BrokerAdapter:
    """Return the BrokerAdapter for ``config.broker`` without changing strategy logic.

    For ``BrokerName.ROBINHOOD`` a live :class:`RobinhoodBroker` is returned when any
    Robinhood MCP callable is supplied via ``adapter_kwargs`` (e.g. ``review_fn=...``,
    ``place_fn=...``); otherwise a :class:`DryRunBroker` is used so unwired callers keep
    the safe simulator. For ``BrokerName.ALPACA`` an :class:`AlpacaBroker` is returned.
    Live placement stays gated behind ``config.dry_run`` and the adapter's own
    ``enable_live_orders`` flag regardless of which backend is selected.

    Security: for a live run (``config.dry_run`` is False) ``config.target_account_number``
    must be a real, explicitly-configured value. ``run_once`` guards against trading the
    wrong account with ``broker.account_number() != config.target_account_number``, but every
    branch below constructs the broker with ``account=config.target_account_number``, so that
    check is trivially satisfied by construction and can never catch a missing account number.
    Refuse to build a live broker (the ALPACA branch, and the ROBINHOOD branch when adapter
    callables are wired) here instead, before any live adapter is constructed.
    """
    if not config.dry_run:
        pinning_failure = account_pinning_failure(
            config.target_account_number, config.broker
        )
        if pinning_failure:
            raise RuntimeError(
                f"build_broker: config.dry_run is False but {pinning_failure}. Set the "
                "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER environment variable (or otherwise "
                "configure a real account number) before running live, or set dry_run=True."
            )
    if config.broker == BrokerName.ALPACA:
        return AlpacaBroker(
            config=alpaca_config or AlpacaConfig(),
            account=config.target_account_number,
            **adapter_kwargs,
        )
    if config.broker == BrokerName.ROBINHOOD and adapter_kwargs:
        # Imported lazily so the core strategy module has no hard dependency on the
        # live-adapter layer (keeps import graph and tests light when unused).
        from dca_live_adapters import RobinhoodBroker

        return RobinhoodBroker(account=config.target_account_number, **adapter_kwargs)
    if not config.dry_run:
        # A live run (dry_run=False) must never fall through to the DryRunBroker: run_once's
        # live branch would call its place_market_buy, record the buy, and mark it submitted
        # with a DRY-RUN id though no order was sent. Refuse the unwired build instead.
        raise RuntimeError(
            "build_broker: config.dry_run is False but no live broker could be constructed "
            f"for {config.broker.value} (no adapter callables supplied). Wire the broker "
            "callables or set dry_run=True."
        )
    return DryRunBroker(account=config.target_account_number)


def iso_week_key(day: date) -> tuple[int, int]:
    iso = day.isocalendar()
    return iso.year, iso.week


def completed_bar_anchor(
    decision_dt: datetime, interval_minutes: int, config: StrategyConfig
) -> datetime:
    if config.assume_bar_timestamps_are_start_times:
        return decision_dt - timedelta(minutes=interval_minutes)
    return decision_dt


def latest_bar_at_or_before(bars: Iterable[Bar], target: datetime) -> Optional[Bar]:
    # ⚡ Bolt Optimization: single-pass loop avoids per-candidate key=lambda calls and generator-expression overhead in this hot path.
    best = None
    for bar in bars:
        if bar.timestamp <= target:
            if best is None or bar.timestamp > best.timestamp:
                best = bar
    return best


def kill_switch_engaged(config: StrategyConfig) -> bool:
    """Return True when the rolling-deployment kill-switch should cap sizing.

    Engaged when capital deployed in the trailing 7 days
    (``config.deployed_last_7d_usd``, injected at runtime from the deployment
    history) exceeds ``kill_switch_7d_deploy_fraction`` of the configured monthly
    cash allocation. A non-positive allocation disables the guard so a
    mis-/unconfigured allocation can never wedge the ladder to 1x.
    """
    allocation = config.monthly_cash_allocation_usd
    if allocation <= 0:
        return False
    fraction = config.kill_switch_7d_deploy_fraction
    if not 0 <= fraction <= 1:
        return False
    threshold = fraction * allocation
    return config.deployed_last_7d_usd > threshold


def graded_buy_multiplier(
    drop_vs_previous_close: float, config: StrategyConfig
) -> float:
    """Map a drop from prior close to a buy multiplier via the graded ladder.

    ``drop_vs_previous_close`` is a fraction (0.02 == 2.0% down). The largest tier
    threshold less than or equal to the drop determines the multiplier; a drop below
    the first threshold returns 0.0 (no buy). See StrategyConfig.sizing_tiers.

    When the dynamic risk kill-switch is engaged (see ``kill_switch_engaged``) any
    positive multiplier is capped to 1x: the strategy still participates in a dip
    at the base amount but stops scaling up. A 0x (no-buy) stays 0x.
    """
    multiplier = 0.0
    for threshold, tier_multiplier in sorted(config.sizing_tiers):
        if drop_vs_previous_close >= threshold:
            multiplier = tier_multiplier
    multiplier = min(multiplier, 5.0)
    if kill_switch_engaged(config):
        multiplier = min(multiplier, 1.0)
    return multiplier


# base_selection_reason values recorded per signal for the first-allocation base.
BASE_BELOW_CLOSE_AND_LOW = "below_prior_close_and_low"  # $2 scale
BASE_BELOW_CLOSE_ONLY = "below_prior_close_only"  # $1 scale
BASE_NOT_BELOW_CLOSE = "not_below_prior_close_no_buy"  # $0, no buy


def select_first_allocation_base_usd(
    decision_price: float,
    previous_close: float,
    previous_low: Optional[float],
    config: StrategyConfig,
) -> tuple[float, str]:
    """Select the first-allocation base dollar amount before the ladder multiplier.

    Policy (applied per ticker before ``graded_buy_multiplier``):

      * decision price not below the prior regular-session close -> $0 (no buy).
      * below the prior close but not below the prior regular-session low ->
        the reduced ``base_buy_amount_below_close_usd`` ($1) scale.
      * below both the prior close and the prior low -> the full
        ``base_buy_amount_usd`` ($2) scale.

    A missing/unknown ``previous_low`` is treated conservatively as "not below the
    low" so an unavailable low can never upgrade the base to the larger $2 scale.
    """
    if not (decision_price < previous_close):
        return 0.0, BASE_NOT_BELOW_CLOSE
    if (
        previous_low is not None
        and math.isfinite(previous_low)
        and 0 < previous_low <= previous_close
        and decision_price < previous_low
    ):
        return config.base_buy_amount_usd, BASE_BELOW_CLOSE_AND_LOW
    return config.base_buy_amount_below_close_usd, BASE_BELOW_CLOSE_ONLY


# Substrings (checked case-insensitively) that indicate a provider raised because the
# ticker itself is unresolved/unlisted on that source (e.g. GFUZ on the finance
# connector) rather than a transient failure. This distinction matters: reconcile_signals
# treats provider_error=True as unsafe-to-auto-buy, but a permanently-unresolved ticker
# on one source should still allow the documented Robinhood-only BUY path, not permanently
# force MANUAL_REVIEW every run. See docs data_source_notes in config/tickers.json.
_TICKER_NOT_FOUND_MARKERS = (
    "not found",
    "no data found",
    "invalid symbol",
    "unknown symbol",
    "unresolved symbol",
    "404",
)


def _is_ticker_not_found_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TICKER_NOT_FOUND_MARKERS)


def _compute_signal_timestamps(
    now: datetime, config: StrategyConfig, interval_minutes: int
) -> tuple[date, datetime, datetime, datetime]:
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()
    decision_dt = datetime.combine(trading_day, config.decision_time_et, tzinfo=EASTERN)
    decision_anchor = completed_bar_anchor(decision_dt, interval_minutes, config)
    lookback_anchor = decision_anchor - timedelta(minutes=config.trend_lookback_minutes)
    return trading_day, decision_dt, decision_anchor, lookback_anchor


def evaluate_source_signal(
    ticker: str,
    now: datetime,
    config: StrategyConfig,
    data: MarketDataProvider,
    interval_minutes: int,
) -> SourceSignal:
    trading_day, decision_dt, decision_anchor, lookback_anchor = (
        _compute_signal_timestamps(now, config, interval_minutes)
    )

    bars = data.get_intraday_bars(
        ticker=ticker,
        start=datetime.combine(trading_day, time(9, 30), tzinfo=EASTERN),
        end=decision_dt,
        interval_minutes=interval_minutes,
    )
    decision_bar = latest_bar_at_or_before(bars, decision_anchor)
    lookback_bar = latest_bar_at_or_before(bars, lookback_anchor)
    previous_close_bar = data.get_previous_close_bar(ticker, decision_dt)

    if decision_bar is None or lookback_bar is None or previous_close_bar is None:
        return SourceSignal(
            ticker=ticker,
            source=data.source,
            action=Action.NO_DATA,
            reason="missing completed bar or previous close",
        )

    decision_price = decision_bar.close
    previous_close = previous_close_bar.close
    previous_low = previous_close_bar.low
    lookback_price = lookback_bar.close

    # Signal-time quote is the intended execution target. Prefer a live quote from
    # the provider (get_quote) captured at signal time; otherwise fall back to the
    # completed decision-bar close. decision_bar_close is always retained separately.
    decision_bar_close = decision_bar.close
    quote_price = decision_bar_close
    quote_time = decision_bar.timestamp
    get_quote = getattr(data, "get_quote", None)
    if callable(get_quote):
        quote = get_quote(ticker, decision_dt)
        if quote is not None and isinstance(quote, (tuple, list)) and len(quote) >= 2:
            quote_price = float(quote[0])
            quote_time = quote[1]
    price_vs_previous_close_pct = (decision_price / previous_close - 1.0) * 100.0
    intraday_trend_pct = (decision_price / lookback_price - 1.0) * 100.0
    drop_vs_previous_close = (previous_close - decision_price) / previous_close
    drop_vs_previous_close_pct = drop_vs_previous_close * 100.0
    buy_multiplier = graded_buy_multiplier(drop_vs_previous_close, config)
    selected_base_amount_usd, base_selection_reason = select_first_allocation_base_usd(
        decision_price, previous_close, previous_low, config
    )
    if buy_multiplier <= 0:
        # Canonical rule item 6: "not lower than close, OR multiplier is 0" ->
        # no-buy. select_first_allocation_base_usd only looks at price vs.
        # close/low, so a price below the close with a drop too shallow to
        # clear the first ladder tier (multiplier 0) would otherwise still
        # carry a below_prior_close_only/and_low reason despite being a no-buy.
        base_selection_reason = BASE_NOT_BELOW_CLOSE
    suggested_amount_usd = selected_base_amount_usd * buy_multiplier

    below_previous_close = decision_price < previous_close
    minimum_drop_met = drop_vs_previous_close >= config.minimum_drop_vs_previous_close
    intraday_trend_down = decision_price < lookback_price
    above_previous_close = decision_price > previous_close
    intraday_trend_up = decision_price > lookback_price

    trend_required = not getattr(config, "ignore_trend_filter", False)
    if (
        below_previous_close
        and minimum_drop_met
        and buy_multiplier > 0
        and (intraday_trend_down or not trend_required)
    ):
        action = Action.BUY
        if trend_required:
            reason = "below prior close, minimum drop met, 60-minute trend down"
        else:
            reason = (
                "below prior close, minimum drop met, trend filter bypassed (final-buy)"
            )
    elif above_previous_close and intraday_trend_up:
        action = Action.SKIP
        reason = "above prior close and 60-minute trend up"
    elif below_previous_close and not minimum_drop_met:
        action = Action.NO_ACTION
        reason = "below prior close but below 0.1% minimum drop"
    elif below_previous_close and not intraday_trend_down and trend_required:
        action = Action.NO_ACTION
        reason = "below prior close but 60-minute trend not down"
    else:
        action = Action.NO_ACTION
        reason = "mixed or flat conditions"

    return SourceSignal(
        ticker=ticker,
        source=data.source,
        action=action,
        reason=reason,
        decision_price=decision_price,
        previous_close=previous_close,
        lookback_price=lookback_price,
        price_vs_previous_close_pct=price_vs_previous_close_pct,
        intraday_trend_pct=intraday_trend_pct,
        drop_vs_previous_close_pct=drop_vs_previous_close_pct,
        buy_multiplier=buy_multiplier if action == Action.BUY else 0.0,
        suggested_amount_usd=suggested_amount_usd if action == Action.BUY else 0.0,
        previous_low=previous_low,
        selected_base_amount_usd=(
            selected_base_amount_usd if action == Action.BUY else 0.0
        ),
        base_selection_reason=base_selection_reason,
        decision_bar_timestamp=decision_bar.timestamp,
        previous_close_timestamp=previous_close_bar.timestamp,
        lookback_bar_timestamp=lookback_bar.timestamp,
        quote_price=quote_price,
        quote_time=quote_time,
        decision_bar_close=decision_bar_close,
    )


def _target_fields(signal: SourceSignal) -> Dict[str, object]:
    """Derive execution-target fields from the source signal that drives the buy.

    target_price defaults to the signal-time quote (SourceSignal.quote_price), while
    decision_bar_close keeps the completed regular-session bar close for comparison.
    """
    return {
        "target_price": signal.quote_price,
        "target_price_source": f"{signal.source.value}:signal_quote",
        "target_quote_time": signal.quote_time,
        "decision_bar_close": signal.decision_bar_close,
    }


def reconcile_signals(
    ticker: str,
    finance_signal: Optional[SourceSignal],
    robinhood_signal: Optional[SourceSignal],
) -> ReconciledDecision:
    finance_action = finance_signal.action if finance_signal else Action.NO_DATA
    robinhood_action = robinhood_signal.action if robinhood_signal else Action.NO_DATA

    if Action.BUY in {finance_action, robinhood_action}:
        errored = [
            name
            for name, signal in (
                ("finance", finance_signal),
                ("robinhood", robinhood_signal),
            )
            if signal is not None and signal.provider_error
        ]
        if errored:
            return ReconciledDecision(
                ticker=ticker,
                action=Action.MANUAL_REVIEW,
                reason=f"provider error on {', '.join(errored)}; do not auto-buy",
                finance_signal=finance_signal,
                robinhood_signal=robinhood_signal,
            )

    if finance_action == Action.BUY and robinhood_action == Action.BUY:
        # Keep amount, base, AND multiplier all from the SAME winning signal --
        # whichever produced the smaller suggested_amount_usd -- so the
        # reconciled decision's own base * multiplier always equals its amount
        # (Codex review: "Keep the winning amount's multiplier paired with its
        # base", following "Preserve the base that produced the reconciled
        # amount"). Independently taking min(base1, base2) and
        # min(mult1, mult2) can each come from a DIFFERENT, non-winning signal
        # -- e.g. finance $2*2.5x=$5 vs. Robinhood $1*3.5x=$3.50: amount is
        # Robinhood's $3.50, so both its base ($1) and multiplier (3.5x) must
        # be used together, not finance's mismatched 2.5x.
        winning_signal = (
            finance_signal
            if finance_signal.suggested_amount_usd
            <= robinhood_signal.suggested_amount_usd
            else robinhood_signal
        )
        return ReconciledDecision(
            ticker=ticker,
            action=Action.BUY,
            reason="finance and Robinhood agree on BUY",
            finance_signal=finance_signal,
            robinhood_signal=robinhood_signal,
            buy_multiplier=winning_signal.buy_multiplier,
            suggested_amount_usd=winning_signal.suggested_amount_usd,
            selected_base_amount_usd=winning_signal.selected_base_amount_usd,
            **_target_fields(winning_signal),
        )

    if finance_action == Action.BUY and robinhood_action != Action.BUY:
        return ReconciledDecision(
            ticker=ticker,
            action=Action.MANUAL_REVIEW,
            reason=f"finance BUY but Robinhood is {robinhood_action.value}; do not auto-buy",
            finance_signal=finance_signal,
            robinhood_signal=robinhood_signal,
        )

    if robinhood_action == Action.BUY and finance_action == Action.NO_DATA:
        return ReconciledDecision(
            ticker=ticker,
            action=Action.BUY,
            reason="Robinhood-only BUY because finance source is unavailable",
            finance_signal=finance_signal,
            robinhood_signal=robinhood_signal,
            buy_multiplier=robinhood_signal.buy_multiplier,
            suggested_amount_usd=robinhood_signal.suggested_amount_usd,
            selected_base_amount_usd=robinhood_signal.selected_base_amount_usd,
            **_target_fields(robinhood_signal),
        )

    if robinhood_action == Action.BUY and finance_action != Action.BUY:
        return ReconciledDecision(
            ticker=ticker,
            action=Action.MANUAL_REVIEW,
            reason=f"Robinhood BUY but finance is {finance_action.value}; do not auto-buy",
            finance_signal=finance_signal,
            robinhood_signal=robinhood_signal,
        )

    return ReconciledDecision(
        ticker=ticker,
        action=(
            Action.NO_ACTION
            if Action.NO_DATA not in {finance_action, robinhood_action}
            else Action.NO_DATA
        ),
        reason=f"no reconciled BUY; finance={finance_action.value}, robinhood={robinhood_action.value}",
        finance_signal=finance_signal,
        robinhood_signal=robinhood_signal,
    )


def within_caps(
    amount: float, state: StrategyState, trading_day: date, config: StrategyConfig
) -> bool:
    """True if ``amount`` (the amount actually about to be ordered) fits the remaining cap.

    Takes the amount explicitly rather than reading it off a decision so a
    caller that reduced a candidate's amount (e.g. the kill-switch's mid-batch
    cap in ``run_once``) checks caps against what will really be submitted,
    not the pre-cap ladder amount.
    """
    if amount <= 0:
        return False
    if amount > state.remaining_spend_today(trading_day, config):
        return False
    return True


def _quote_bid_ask(
    data: MarketDataProvider, ticker: str, as_of: datetime
) -> Optional[tuple[float, float]]:
    """Return a live bid/ask quote when the provider exposes one.

    The scheduled live path uses this as a final pre-placement spread circuit
    breaker. Providers that cannot expose bid/ask return ``None``; a concrete
    bid/ask with an excessive spread blocks the order before the broker path.
    """
    get_bid_ask = getattr(data, "get_bid_ask", None)
    if not callable(get_bid_ask):
        return None
    quote = get_bid_ask(ticker, as_of)
    if quote is None or not isinstance(quote, (tuple, list)) or len(quote) < 2:
        return None
    try:
        bid = float(quote[0])
        ask = float(quote[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(bid) or not math.isfinite(ask):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask


def quote_spread_pct(
    data: MarketDataProvider, ticker: str, as_of: datetime
) -> Optional[float]:
    """Return bid/ask spread as a percent of quote midpoint, if available."""
    bid_ask = _quote_bid_ask(data, ticker, as_of)
    if bid_ask is None:
        return None
    bid, ask = bid_ask
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    return (ask - bid) / midpoint * 100.0


def allocation_sort_key(decision: ReconciledDecision) -> Tuple[float, float, str]:
    """Deterministic allocation priority for buy candidates.

    Orders by higher multiplier first, then larger percent drop, then ticker
    alphabetical (A->Z) as the final tie-break so a capped run always fills the
    same names the strategy state promises. Numeric keys are negated (rather than
    sorting with ``reverse=True``) so ticker still sorts ascending.
    """
    drop = max(
        (
            decision.finance_signal.drop_vs_previous_close_pct
            if decision.finance_signal
            else 0.0
        ),
        (
            decision.robinhood_signal.drop_vs_previous_close_pct
            if decision.robinhood_signal
            else 0.0
        ),
    )
    return (-decision.buy_multiplier, -drop, decision.ticker)


def _signal_target_price(decision: ReconciledDecision) -> Optional[float]:
    """Return the decision-bar close used as the buy target price at signal time.

    Prefer the finance decision price, falling back to Robinhood. Returns None when
    neither source carried a usable decision price so downstream slippage math can
    flag ``target_price_missing`` instead of computing against a fabricated value.
    """
    for signal in (decision.finance_signal, decision.robinhood_signal):
        if signal is not None and signal.decision_price > 0:
            return signal.decision_price
    return None


def _afternoon_decision_price(decision: ReconciledDecision) -> Optional[float]:
    """Return the current afternoon decision price used to compare against a prior run.

    Prefer the reconciled execution target (the signal-time quote), falling back to the
    decision-bar close derived from the source signals. Returns None when neither is
    available so the top-up guard can behave conservatively rather than guess.
    """
    if decision.target_price and decision.target_price > 0:
        return decision.target_price
    return _signal_target_price(decision)


def resolve_top_up_desired_amount_usd(
    prior: Optional[PriorSameDayAllocation],
    current_multiplier: float,
    current_suggested_amount_usd: float,
    current_base_amount_usd: float,
) -> tuple[float, float]:
    """Return the same-day full-day target used by a top-up guard, base-preserved.

    The first same-day allocation for a ticker fixes that day's first-allocation
    base scale ($1 or $2, see select_first_allocation_base_usd); a later same-day
    top-up must keep using THAT base and only re-apply its own run's ladder
    multiplier (which legitimately varies with price), never recompute the base
    from its own price. Otherwise a later, lower price that newly crosses the
    prior regular-session low would silently upgrade the whole day's target from
    the $1 to the $2 scale and over-order the incremental difference.

    Falls back to ``current_suggested_amount_usd``/``current_base_amount_usd``
    (the pre-existing behavior) when there is no prior allocation. When a prior
    allocation exists but predates this field / could not be resolved, this
    still returns that same fallback -- never treat an unknown preserved base
    as $0 here -- but callers with a known prior amount (``prior.amount_usd >
    0``) must not trust this fallback value directly: they fail closed
    (manual review, $0) instead of using it, since silently recomputing the
    base from the current run's own price is exactly the bug this function
    exists to prevent (Codex review: "Fail closed when the first allocation
    base is unknown"; see evaluate_top_up_guard's own check).

    Returns ``(desired_amount_usd, effective_base_amount_usd)`` -- the latter is
    whichever base actually produced ``desired_amount_usd``, so a caller
    building a candidate-row artifact can persist the base that was ACTUALLY
    used (Codex review: "Persist the preserved base on top-up candidates")
    instead of always the current run's own freshly recomputed base.
    """
    if (
        prior is not None
        and prior.selected_base_amount_usd
        and prior.selected_base_amount_usd > 0
    ):
        return (
            round(prior.selected_base_amount_usd * current_multiplier, 2),
            prior.selected_base_amount_usd,
        )
    return round(current_suggested_amount_usd, 2), current_base_amount_usd


def evaluate_top_up_guard(
    decision: ReconciledDecision,
    prior: Optional[PriorSameDayAllocation],
    config: StrategyConfig,
) -> TopUpGuardOutcome:
    """Compute the same-day prior-run offset/top-up for one afternoon buy candidate.

    A morning/manual run must never block the scheduled afternoon run; instead the
    afternoon run only tops up, and only when it is buying into a lower price:

      * No earlier same-day allocation for the ticker -> normal allocation (buy the
        full afternoon desired amount).
      * Earlier allocation exists and the afternoon decision price is *below* the
        earlier run's reference price -> buy only the incremental dollar difference
        (afternoon desired minus the amount already bought), and $0 (reason
        ``TOP_UP_NOT_LOWER_OR_NO_DELTA``, canonical rule item 12) if that is <= 0.
      * Earlier allocation exists but the afternoon price is not below the reference
        price -> buy $0 (reason ``TOP_UP_NOT_LOWER_OR_NO_DELTA``).
      * Earlier amount is known but its reference price (or the current afternoon
        price) is missing -> conservative $0 with ``manual_review`` flagged, rather
        than topping up blind.

    The full-day target (``desired``) reuses the first allocation's selected base
    when known (see :func:`resolve_top_up_desired_amount_usd`) so a later, lower
    price cannot silently upgrade the whole day's target to the $2 scale.

    KNOWN GAP (config/scheduled_tasks.json canonical_first_buy_and_topup_rule ->
    effective_prior_usd_gap_2026_07_24, module docstring's JUL 23 2026 EFFECTIVE
    PRIOR ALLOCATION PATCH note): ``prior.amount_usd`` is the artifact-derived
    prior allocation only. This does not yet compute the canonical rule's
    ``effective_prior_usd`` (deduped same-day artifacts PLUS Robinhood same-day
    broker order history) or read ``prior_usd_rh``, so a same-day buy confirmed
    at the broker but missing/incomplete in the artifact trail is not yet folded
    into ``corrected_delta_usd`` here.
    """
    desired, effective_base = resolve_top_up_desired_amount_usd(
        prior,
        decision.buy_multiplier,
        decision.suggested_amount_usd,
        decision.selected_base_amount_usd,
    )
    afternoon_price = _afternoon_decision_price(decision)

    def _outcome(
        incremental: float,
        result: str,
        *,
        skipped: bool,
        manual_review: bool = False,
    ) -> TopUpGuardOutcome:
        return TopUpGuardOutcome(
            ticker=decision.ticker,
            afternoon_desired_amount=desired,
            afternoon_decision_price=afternoon_price,
            prior_same_day_amount=round(prior.amount_usd, 2) if prior else 0.0,
            prior_same_day_reference_price=prior.reference_price if prior else None,
            prior_same_day_run_id=prior.run_id if prior else None,
            prior_same_day_source=prior.source if prior else None,
            incremental_order_amount=round(max(0.0, incremental), 2),
            top_up_guard_result=result,
            skipped=skipped,
            effective_base_amount_usd=effective_base,
            manual_review=manual_review,
        )

    # No earlier same-day allocation: behave exactly like a normal scheduled run.
    if prior is None or prior.amount_usd <= 0:
        return _outcome(desired, TOP_UP_NO_PRIOR_RUN, skipped=desired <= 0)

    # Prior amount is known but its first-allocation base ($1 or $2) could not
    # be resolved (legacy artifact predating the field, or an unresolved bare
    # manual CSV entry) -- resolve_top_up_desired_amount_usd's fallback above
    # silently recomputed `desired` from THIS run's own freshly selected base,
    # which is exactly the base-upgrade/downgrade bug the preserved-base rule
    # exists to prevent. Fail closed like a missing reference price instead of
    # trusting that silently-recomputed target (Codex review: "Fail closed
    # when the first allocation base is unknown").
    if prior.selected_base_amount_usd is None or prior.selected_base_amount_usd <= 0:
        return _outcome(
            0.0, TOP_UP_MISSING_PRIOR_BASE, skipped=True, manual_review=True
        )

    # Prior amount is known but we cannot establish a comparable price on either side.
    # Prefer the conservative no-top-up path and flag the ticker for manual review.
    if prior.reference_price is None or afternoon_price is None:
        return _outcome(
            0.0, TOP_UP_MISSING_PRIOR_PRICE, skipped=True, manual_review=True
        )

    # Only top up when buying into a lower price than the earlier same-day run.
    # Canonical rule item 12: not-strictly-lower and zero-delta both use the
    # single unified TOP_UP_NOT_LOWER_OR_NO_DELTA code.
    if afternoon_price >= prior.reference_price:
        return _outcome(0.0, TOP_UP_NOT_LOWER_OR_NO_DELTA, skipped=True)

    incremental = desired - round(prior.amount_usd, 2)
    if incremental <= 0:
        return _outcome(0.0, TOP_UP_NOT_LOWER_OR_NO_DELTA, skipped=True)
    return _outcome(incremental, TOP_UP_INCREMENTAL, skipped=False)


def _signal_row(
    ticker: str, source: str, signal: Optional[SourceSignal]
) -> Dict[str, Any]:
    if signal is None:
        return {
            "ticker": ticker,
            "source": source,
            "action": Action.NO_DATA.value,
            "reason": "no signal produced",
            "auto_buy_signal": False,
            "provider_error": None,
            "decision_bar_close": None,
            "previous_close": None,
            "previous_low": None,
            "lookback_price": None,
            "price_vs_previous_close_pct": None,
            "intraday_trend_pct": None,
            "drop_vs_previous_close_pct": None,
            "buy_multiplier": None,
            "selected_base_amount_usd": None,
            "base_selection_reason": None,
            "decision_bar_timestamp": None,
            "previous_close_timestamp": None,
            "lookback_bar_timestamp": None,
        }
    return {
        "ticker": ticker,
        "source": source,
        "action": signal.action.value,
        "reason": signal.reason,
        "auto_buy_signal": signal.action == Action.BUY,
        "provider_error": signal.provider_error,
        "decision_bar_close": signal.decision_price,
        "previous_close": signal.previous_close,
        "previous_low": signal.previous_low,
        "lookback_price": signal.lookback_price,
        "price_vs_previous_close_pct": signal.price_vs_previous_close_pct,
        "intraday_trend_pct": signal.intraday_trend_pct,
        "drop_vs_previous_close_pct": signal.drop_vs_previous_close_pct,
        "buy_multiplier": signal.buy_multiplier,
        "selected_base_amount_usd": signal.selected_base_amount_usd,
        "base_selection_reason": signal.base_selection_reason,
        "suggested_amount_usd": signal.suggested_amount_usd,
        "decision_bar_timestamp": _iso_or_none(signal.decision_bar_timestamp),
        "previous_close_timestamp": _iso_or_none(signal.previous_close_timestamp),
        "lookback_bar_timestamp": _iso_or_none(signal.lookback_bar_timestamp),
    }


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def build_usage_proxy(
    *,
    decisions: Sequence[ReconciledDecision],
    signal_rows: Sequence[Dict[str, Any]],
    buy_candidates: Sequence[Dict[str, Any]],
    candidate_outcomes: Dict[str, Dict[str, Any]],
    order_reviews_attempted: Optional[int] = None,
    finance_calls_attempted: Optional[int] = None,
    robinhood_calls_attempted: Optional[int] = None,
    errors_count: int = 0,
    notifications_sent: Optional[int] = None,
    cost_control_notes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Assemble the lightweight usage-proxy block for a run's run_state.

    Every value is an activity proxy (counts of calls/rows/orders/bytes), never an
    exact credit or dollar figure. ``*_calls_attempted`` default to the number of
    scanned tickers because the daily path evaluates one finance and one Robinhood
    signal per ticker; pass explicit values when the provider count is known. The
    byte/count-of-file fields are left ``None`` here and filled by
    ``write_run_artifacts`` once the files exist on disk.
    """
    tickers_scanned = len(decisions)
    manual_review_count = sum(1 for d in decisions if d.action == Action.MANUAL_REVIEW)
    top_up_manual_review_count = sum(
        1 for c in buy_candidates if c.get("top_up_manual_review")
    )
    orders_placed_count = sum(
        1
        for outcome in candidate_outcomes.values()
        if outcome.get("order_id") and outcome.get("status") in {"submitted", "dry_run"}
    )
    return {
        "tickers_scanned": tickers_scanned,
        "finance_calls_attempted": (
            finance_calls_attempted
            if finance_calls_attempted is not None
            else tickers_scanned
        ),
        "robinhood_calls_attempted": (
            robinhood_calls_attempted
            if robinhood_calls_attempted is not None
            else tickers_scanned
        ),
        "signal_rows_written": len(signal_rows),
        "buy_candidates_written": len(buy_candidates),
        "order_reviews_attempted": (
            order_reviews_attempted
            if order_reviews_attempted is not None
            else len(buy_candidates)
        ),
        "orders_placed_count": orders_placed_count,
        # Filled in write_run_artifacts once files are written (approximate proxy).
        "state_file_bytes": None,
        "csv_artifact_count": None,
        "total_artifact_bytes": None,
        "notifications_sent": notifications_sent,
        "errors_count": errors_count,
        "manual_recovery_required": manual_review_count > 0
        or top_up_manual_review_count > 0
        or errors_count > 0,
        "cost_control_notes": (
            list(cost_control_notes)
            if cost_control_notes is not None
            else list(DAILY_COST_CONTROL_NOTES)
        ),
    }


def _derive_order_submit_status(
    config: "StrategyConfig",
    buy_candidates: Sequence[Dict[str, Any]],
    errors_count: int,
) -> str:
    """Classify the run's overall order-submission outcome for run_classification/reporting.

    Precedence (most concrete evidence first): a dry run is always reported as
    such regardless of individual candidate outcomes; any real submitted order
    makes the whole run "submitted" evidence even if other candidates were
    blocked; an empty candidate list (no BUY signals at all that cycle) is its
    own explicit state rather than being confused with a blocked/error run; a
    broker confirmation-required response is surfaced distinctly so
    reporting/the watchdog know the task ran and passed review but placed
    nothing; unexpected provider/placement errors are reported as such; any
    other block (budget cap, buying power, tradability) falls back to
    budget_exhausted, the closest existing category.

    A placement that RAISED outranks the confirmation hold: in a zero-submission
    batch where one order awaits confirmation and another ticker's placement
    failed, reporting must not read the run as an ordinary confirmation block.
    ``is_confirmation_blocked()`` would otherwise return true for a run
    ``_apply_no_order_classification`` correctly records as a placement failure
    (Codex review).
    """
    try:
        from scripts.run_classification import (
            ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
            ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
            ORDER_SUBMIT_STATUS_DRY_RUN,
            ORDER_SUBMIT_STATUS_ERROR,
            ORDER_SUBMIT_STATUS_NO_CANDIDATES,
            ORDER_SUBMIT_STATUS_SUBMITTED,
        )
    except ImportError:
        from run_classification import (
            ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
            ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
            ORDER_SUBMIT_STATUS_DRY_RUN,
            ORDER_SUBMIT_STATUS_ERROR,
            ORDER_SUBMIT_STATUS_NO_CANDIDATES,
            ORDER_SUBMIT_STATUS_SUBMITTED,
        )

    if config.dry_run:
        return ORDER_SUBMIT_STATUS_DRY_RUN
    if any(c.get("status") == "submitted" for c in buy_candidates):
        return ORDER_SUBMIT_STATUS_SUBMITTED
    if not buy_candidates:
        return ORDER_SUBMIT_STATUS_NO_CANDIDATES
    # Checked BEFORE the confirmation hold so a genuine broker failure is never
    # reported as a routine confirmation block (Codex review). Mirrors
    # morning_dca._concrete_order_submit_status, which returns None (leaving the
    # classifier's failure status in place) as soon as any row failed.
    if any(c.get("status") == "failed" for c in buy_candidates):
        return ORDER_SUBMIT_STATUS_ERROR
    if any(
        c.get("status") == "not_submitted_confirmation_required" for c in buy_candidates
    ):
        return ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED
    if errors_count > 0:
        return ORDER_SUBMIT_STATUS_ERROR
    return ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED


def _build_buy_candidates(
    decisions: Sequence[ReconciledDecision],
    candidate_outcomes: Dict[str, Dict[str, Any]],
    top_up_outcomes: Optional[Dict[str, "TopUpGuardOutcome"]],
) -> List[Dict[str, Any]]:
    buy_candidates: List[Dict[str, Any]] = []
    for decision in decisions:
        if decision.action != Action.BUY:
            continue
        outcome = candidate_outcomes.get(decision.ticker, {})
        target_price = _signal_target_price(decision)
        # planned_amount_usd: when the top-up guard is active for this ticker use
        # the incremental amount; otherwise use submitted_amount_usd (which
        # reflects any mid-batch kill-switch cap) or fall back to suggested_amount.
        guard = top_up_outcomes.get(decision.ticker) if top_up_outcomes else None
        submitted_amount = outcome.get("submitted_amount_usd")
        if guard is not None and outcome.get("duplicate_preserved"):
            # A same-stream duplicate-preserved retry (near_close_dca.py's
            # _process_buy_candidate) carries the REAL broker order's amount
            # from an earlier attempt this run, not a fresh placement -- this
            # retry's own freshly recomputed guard.incremental_order_amount
            # can differ (e.g. new prior-allocation context shrinks it) and
            # must not clamp away the real submitted amount here (Codex
            # review, PR #231: "Keep the broker notional when rebuilding
            # duplicate artifacts").
            planned_amount_usd = (
                submitted_amount
                if submitted_amount is not None
                else guard.incremental_order_amount
            )
        elif guard is not None:
            # When the kill-switch capped order_amount before the guard ran,
            # submitted_amount_usd already reflects the actual capped spend.
            # Use that when available so planned_amount_usd is never higher than
            # what was really submitted (guard.incremental_order_amount was itself
            # already capped in run_once, but submitted_amount_usd is the ground truth).
            planned_amount_usd = (
                min(guard.incremental_order_amount, submitted_amount)
                if submitted_amount is not None
                else guard.incremental_order_amount
            )
        else:
            planned_amount_usd = (
                submitted_amount
                if submitted_amount is not None
                else decision.suggested_amount_usd
            )
        candidate_row = {
            "ticker": decision.ticker,
            "reason": decision.reason,
            "buy_multiplier": decision.buy_multiplier,
            # Recorded so a later same-day top-up guard reading this run back as
            # prior-allocation context can reuse this run's base if it was the
            # day's first allocation (see resolve_top_up_desired_amount_usd).
            # When the guard is active, this must be the base that ACTUALLY
            # produced planned_amount_usd (guard.effective_base_amount_usd --
            # the preserved first-allocation base when reused), not this run's
            # own freshly recomputed decision.selected_base_amount_usd (Codex
            # review: "Persist the preserved base on top-up candidates").
            "selected_base_amount_usd": (
                guard.effective_base_amount_usd
                if guard is not None
                else decision.selected_base_amount_usd
            ),
            "planned_amount_usd": round(planned_amount_usd, 2),
            "target_price": target_price,
            "target_price_missing": target_price is None,
            "decision_bar_close": decision.decision_bar_close,
            "previous_close": (
                decision.finance_signal.previous_close
                if decision.finance_signal is not None
                else (
                    decision.robinhood_signal.previous_close
                    if decision.robinhood_signal
                    else None
                )
            ),
            "order_id": outcome.get("order_id"),
            "status": outcome.get("status", "not_submitted"),
            "skip_reason": outcome.get("skip_reason"),
            "reviewed_order": outcome.get("reviewed_order"),
            # Row-level spread verdict, so a reader can separate "never reached the
            # gate" from "failed closed there" without parsing skip-reason prose.
            "spread_ok": outcome.get("spread_ok"),
            "spread_pct": outcome.get("spread_pct"),
            "spread_status": outcome.get("spread_status", SPREAD_STATUS_NOT_EVALUATED),
        }
        if guard is not None:
            candidate_row.update(
                {
                    "afternoon_desired_amount_usd": round(
                        decision.suggested_amount_usd, 2
                    ),
                    "afternoon_desired_amount": round(
                        guard.afternoon_desired_amount, 2
                    ),
                    "afternoon_decision_price": guard.afternoon_decision_price,
                    "prior_same_day_amount": round(guard.prior_same_day_amount, 2),
                    "prior_same_day_reference_price": guard.prior_same_day_reference_price,
                    "prior_same_day_run_id": guard.prior_same_day_run_id,
                    "prior_same_day_source": guard.prior_same_day_source,
                    # outcome.get(..., default) only falls back on a MISSING key --
                    # an outcome that explicitly carries None (a skip path that
                    # doesn't set incremental_order_amount) would otherwise pass
                    # None straight to float() and raise TypeError (Antigravity
                    # review: "Defensive None check for dictionary lookup").
                    "incremental_order_amount": round(
                        float(
                            outcome["incremental_order_amount"]
                            if outcome.get("incremental_order_amount") is not None
                            else guard.incremental_order_amount
                        ),
                        2,
                    ),
                    "top_up_guard_result": guard.top_up_guard_result,
                    "top_up_skipped": guard.skipped,
                    "top_up_manual_review": guard.manual_review,
                }
            )
        buy_candidates.append(candidate_row)
    return buy_candidates


def _build_budget_block(
    config: StrategyConfig,
    state: StrategyState,
    tier: BudgetTier,
    planned_spend: float,
    high_multiplier_signals: int,
    trading_day: date,
) -> Dict[str, Any]:
    return {
        "base_amount_usd": config.base_buy_amount_usd,
        "base_buy_amount_usd": config.base_buy_amount_usd,
        "budget_tier": tier.name,
        "planned_spend_usd": round(planned_spend, 2),
        "high_multiplier_signal_count": high_multiplier_signals,
        "daily_cap_usd": tier.daily_cap_usd,
        "weekly_cap_usd": round(state.effective_weekly_cap(), 2),
        "weekly_rollover_reserve_usd": round(state.weekly_rollover_reserve_usd, 2),
        "weekly_rollover_reserve_cap_usd": config.weekly_rollover_reserve_cap_usd,
        "allowed_spend_today_usd": round(
            state.allowed_spend_today(trading_day, config), 2
        ),
        "remaining_spend_today_usd": round(
            state.remaining_spend_today(trading_day, config), 2
        ),
        "day_notional_usd": round(state.day_notional(trading_day), 2),
        "week_notional_usd": round(state.week_notional(trading_day), 2),
    }


def _build_risk_guard_block(config: StrategyConfig) -> Dict[str, Any]:
    return {
        "monthly_cash_allocation_usd": config.monthly_cash_allocation_usd,
        "kill_switch_7d_deploy_fraction": config.kill_switch_7d_deploy_fraction,
        "kill_switch_7d_threshold_usd": round(
            config.kill_switch_7d_deploy_fraction * config.monthly_cash_allocation_usd,
            2,
        ),
        "deployed_last_7d_usd": round(config.deployed_last_7d_usd, 2),
        "kill_switch_engaged": kill_switch_engaged(config),
    }


def build_run_artifacts(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    decisions: Sequence[ReconciledDecision],
    candidate_outcomes: Dict[str, Dict[str, Any]],
    *,
    order_reviews_attempted: Optional[int] = None,
    finance_calls_attempted: Optional[int] = None,
    robinhood_calls_attempted: Optional[int] = None,
    errors_count: int = 0,
    notifications_sent: Optional[int] = None,
    cost_control_notes: Optional[Sequence[str]] = None,
    top_up_outcomes: Optional[Dict[str, "TopUpGuardOutcome"]] = None,
    manual: bool = False,
    raw_persistence_state: Optional[Dict[str, object]] = None,
    classification_overrides: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the structured run-state dict written by the daily live-run path.

    The artifact captures everything downstream reporting needs: per-source signal
    rows, buy candidates with the target price at signal time and the per-symbol
    planned dollar amount, order ids, skip/block reasons, and budget/reserve fields.
    When ``top_up_outcomes`` is supplied (the scheduled afternoon run applied the
    same-day prior-run offset/top-up guard), each candidate also records the guard's
    prior-allocation context and the resulting incremental order amount.

    Also stamps a ``run_classification`` block (via ``scripts/run_classification``)
    so downstream consumers -- ``dca_reporting``'s manual-order exclusion and
    ``dca_risk``'s scheduled-stream deployment accounting -- can tell a scheduled
    live run from a manual one from the artifact content alone. ``manual``
    selects which block is stamped: False (default) is the scheduled 3:30 PM ET
    live task, True is a manual/ad hoc invocation. ``classification_overrides``
    lets a non-afternoon scheduled caller (the roster's ``run_slot`` for 1030ET/
    1230ET) stamp its OWN stream/cron_id/scheduled_for_et/run_id instead of the
    afternoon defaults (Codex review, discussion_r3679391261: "Stamp each slot
    with its own scheduled classification") -- ignored when ``manual`` is True.

    ``correlation_id``, when given, is stamped onto the top-level artifact (next
    to ``run_id``) so the deterministic (day, stream, window) id a scheduler used
    to fire this run is recoverable from the durable run_state.json itself, not
    only from an in-process return value that is lost if the process exits or
    crashes before the caller reads it (Codex review, discussion_r3679633768:
    "Persist the slot correlation ID in run artifacts").
    """
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()
    run_id = f"{trading_day.isoformat()}-{now_et.strftime('%H%M%S')}"
    triggered_at_utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        from scripts.run_classification import (
            build_manual_classification,
            build_scheduled_classification,
        )
    except ImportError:
        from run_classification import (
            build_manual_classification,
            build_scheduled_classification,
        )
    run_classification = (
        build_manual_classification(triggered_at_utc=triggered_at_utc)
        if manual
        else build_scheduled_classification(
            trading_day.isoformat(),
            triggered_at_utc=triggered_at_utc,
            **(classification_overrides or {}),
        )
    )
    if not manual:
        # The scheduled afternoon (3:30 PM ET) artifact contract requires the
        # run_classification to additionally carry trading_day, same_day_required,
        # and valid_scheduled_evidence (see
        # scripts.scheduled_artifacts.build_afternoon_live_run_classification and
        # the identical morning-path stamping in morning_dca.py). trading_day and
        # same_day_required are known now; valid_scheduled_evidence is finalized
        # from the completed state just before this function returns.
        run_classification.setdefault("trading_day", trading_day.isoformat())
        run_classification.setdefault("same_day_required", True)

    signal_rows: List[Dict[str, Any]] = []
    for decision in decisions:
        is_reconciled_buy = decision.action == Action.BUY
        finance_row = _signal_row(
            decision.ticker, SourceName.FINANCE.value, decision.finance_signal
        )
        robinhood_row = _signal_row(
            decision.ticker, SourceName.ROBINHOOD.value, decision.robinhood_signal
        )
        # Override auto_buy_signal with the RECONCILED decision. When providers
        # disagree (one BUY, one SKIP/NO_ACTION) reconcile_signals() produces
        # MANUAL_REVIEW and no order is placed; using the per-provider action would
        # mark that ticker as expected and trigger a false drift alert.
        finance_row["auto_buy_signal"] = is_reconciled_buy
        robinhood_row["auto_buy_signal"] = is_reconciled_buy
        signal_rows.append(finance_row)
        signal_rows.append(robinhood_row)

    # Reconcile auto_buy_signal against final placement outcomes. A BUY decision
    # may still be deliberately skipped by the top-up guard, same-day duplicate
    # guard, budget cap, safety breakers (spread, ceiling, tradability, buying
    # power). Clear the flag for those so the drift detector ignores them.
    # Keep it True for genuine errors (spread-quote error, placement failure) so
    # those do trigger alerts.
    _afternoon_deliberate_skips = {
        t
        for t, oc in candidate_outcomes.items()
        if oc.get("status") == "skipped"
        or (oc.get("status") == "blocked" and oc.get("deliberate_skip"))
    }
    if _afternoon_deliberate_skips:
        for _row in signal_rows:
            if (
                _row.get("auto_buy_signal")
                and _row.get("ticker") in _afternoon_deliberate_skips
            ):
                _row["auto_buy_signal"] = False

    buy_candidates = _build_buy_candidates(
        decisions, candidate_outcomes, top_up_outcomes
    )

    manual_review_tickers = [
        d.ticker for d in decisions if d.action == Action.MANUAL_REVIEW
    ]
    for candidate in buy_candidates:
        if (
            candidate.get("top_up_manual_review")
            and candidate["ticker"] not in manual_review_tickers
        ):
            manual_review_tickers.append(candidate["ticker"])

    # Backfill signal rows with the reconciled planned_amount_usd from buy candidates
    # so the drift detector uses the actual order amount rather than a per-provider
    # suggested amount. When both providers return BUY at different tiers, each
    # signal row carries its own suggested_amount_usd but the reconciled candidate
    # row captures what was actually placed. The drift detector reads suggested_amount_usd
    # via DESIRED_COLUMNS; overwriting it here ensures accurate missing-order reporting.
    _reconciled_amount_by_ticker: Dict[str, float] = {
        c["ticker"]: c["planned_amount_usd"]
        for c in buy_candidates
        if c.get("planned_amount_usd") is not None
    }
    for _srow in signal_rows:
        _sticker = _srow.get("ticker")
        if _sticker in _reconciled_amount_by_ticker and _srow.get("auto_buy_signal"):
            _srow["suggested_amount_usd"] = _reconciled_amount_by_ticker[_sticker]

    # ⚡ Bolt Optimization: Consolidate multiple generator passes into a single loop
    # to eliminate redundant O(N) scans and generator overhead in this hot aggregation.
    planned_spend = 0.0
    high_multiplier_signals = 0
    submitted_count = 0
    skipped_count = 0
    for c in buy_candidates:
        planned_spend += c["planned_amount_usd"]
        if (c.get("buy_multiplier") or 0.0) >= config.high_multiplier_threshold:
            high_multiplier_signals += 1
        if c.get("status") == "submitted":
            submitted_count += 1
        else:
            skipped_count += 1

    manual_review_count = 0
    for d in decisions:
        if d.action == Action.MANUAL_REVIEW:
            manual_review_count += 1

    if state.selected_tier is None:
        state.selected_tier = select_budget_tier(planned_spend, high_multiplier_signals)
    tier = state.selected_tier
    # Ensure the rollover reserve reflects this trading day's week before the
    # effective weekly cap is reported.
    state.roll_weekly_reserve(trading_day, config)

    artifacts = {
        "run_id": run_id,
        "run_classification": run_classification,
        **({"correlation_id": correlation_id} if correlation_id is not None else {}),
        "order_submit_status": _derive_order_submit_status(
            config, buy_candidates, errors_count
        ),
        "generated_at": now_et.isoformat(),
        "trading_day": trading_day.isoformat(),
        "iso_week": list(iso_week_key(trading_day)),
        "broker": config.broker.value,
        "dry_run": config.dry_run,
        "account_number": None,
        "budget": _build_budget_block(
            config, state, tier, planned_spend, high_multiplier_signals, trading_day
        ),
        "risk_guard": _build_risk_guard_block(config),
        "counts": {
            "evaluated": len(decisions),
            "buy_candidates": len(buy_candidates),
            "submitted": submitted_count,
            "skipped": skipped_count,
            "manual_review": manual_review_count,
        },
        "signal_rows": signal_rows,
        "buy_candidates": buy_candidates,
        "manual_review_tickers": manual_review_tickers,
        "usage_proxy": build_usage_proxy(
            decisions=decisions,
            signal_rows=signal_rows,
            buy_candidates=buy_candidates,
            candidate_outcomes=candidate_outcomes,
            order_reviews_attempted=order_reviews_attempted,
            finance_calls_attempted=finance_calls_attempted,
            robinhood_calls_attempted=robinhood_calls_attempted,
            errors_count=errors_count,
            notifications_sent=notifications_sent,
            cost_control_notes=cost_control_notes,
        ),
    }
    _apply_no_order_classification(artifacts, candidate_outcomes, scheduled=not manual)
    if raw_persistence_state is not None:
        artifacts.update(raw_persistence_state)
    if not manual:
        # Derive valid_scheduled_evidence from the completed state rather than
        # asserting it: a scheduled (non-manual) run is same-day scheduled
        # evidence, a manual run never is. Mirrors morning_dca.py's finalization.
        try:
            from scripts.same_day_pointer import derive_valid_scheduled_evidence
        except ImportError:
            from same_day_pointer import derive_valid_scheduled_evidence
        run_classification["valid_scheduled_evidence"] = (
            derive_valid_scheduled_evidence(artifacts)
        )
    return artifacts


def _apply_no_order_classification(
    artifacts: Dict[str, Any],
    candidate_outcomes: Dict[str, Dict[str, Any]],
    *,
    scheduled: bool = True,
) -> None:
    """Record how a run that submitted zero orders is classified.

    Separates no-order logic from failures per
    config/scheduled_tasks.json:spread_gate_outcome_contract_2026_07_27. The counts
    the classifier needs -- candidates evaluated, candidates that actually reached
    the spread gate, and those failed closed there for want of a proven spread --
    come from the per-row verdicts stamped by :func:`_process_buy_candidate`, so a
    run where nothing qualified on the ladder records
    ``completion_reason='no_ladder_candidates'`` with a NULL ``error_class``
    instead of blaming the spread, while candidates stopped by a budget/cap check
    BEFORE the spread lookup record ``blocked_before_spread_gate``.

    A run whose placements all RAISED is a genuine failure, not a healthy
    no-order run: any candidate left at ``status='failed'`` marks the run
    ``failed_after_connector_native_recovery_attempt`` with
    ``is_trading_failure=true`` (Codex review).

    A run that submitted an order is not a no-order run; it keeps the
    ``order_submit_status`` the placement path derived.
    """
    outcomes = [
        oc for oc in (candidate_outcomes or {}).values() if isinstance(oc, dict)
    ]

    submitted = 0
    failed = 0
    connector_errors = 0
    for oc in outcomes:
        status = oc.get("status")
        if status == "submitted":
            submitted += 1
        elif status == "failed":
            failed += 1
        elif status == "blocked" and not oc.get("deliberate_skip"):
            connector_errors += 1

    # A pre-placement broker check that RAISED is recorded as a non-deliberate
    # block, not as 'failed'. We count these as connector errors here too so a batch
    # that placed one order but lost another ticker to a connector failure is not
    # recorded as an ordinary success with no error class (Codex review).
    # Same rule as the morning runner.
    artifacts["orders_submitted_count"] = submitted
    if submitted:
        if failed or connector_errors:
            # A partially submitted batch: some orders landed and another ticker
            # was lost -- either its placement raised, or a pre-placement broker
            # check did. Returning as an ordinary success would leave the run
            # with no is_trading_failure and no failure outcome even though an
            # intended order never happened (Codex review).
            artifacts["run_classification_outcome"] = (
                RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION
            )
            artifacts["is_trading_failure"] = True
            artifacts["orders_failed_count"] = failed
            artifacts.setdefault(
                "error_class",
                (
                    ERROR_CLASS_PARTIAL_PLACEMENT_FAILURE
                    if failed
                    else ERROR_CLASS_DEGRADED_MARKET_DATA
                ),
            )
        return
    at_gate = [oc for oc in outcomes if oc.get("spread_status")]
    reaching_gate = sum(
        1 for oc in at_gate if oc.get("spread_status") != SPREAD_STATUS_NOT_EVALUATED
    )
    # Only rows the SPREAD GATE itself stopped, recorded explicitly rather than
    # inferred from spread_status: a manual run does not fail closed on an
    # unproven spread, so a row can carry spread_status='spread_unavailable' and
    # still be stopped later by review or confirmation (Codex review).
    blocked_missing_spread = sum(
        1 for oc in outcomes if oc.get("blocked_by") == BLOCKED_BY_SPREAD_GATE
    )
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=reaching_gate,
        candidates_blocked_missing_spread=blocked_missing_spread,
        candidates_evaluated=len(artifacts.get("buy_candidates") or []),
        placement_attempted_and_failed=any(
            oc.get("status") == "failed" for oc in outcomes
        ),
        # Providers that raised degrade tickers to NO_DATA/manual review rather
        # than genuinely evaluating them, so the zero-order result is not a clean
        # ladder no-op and must not be recorded with a null error_class.
        data_errors=int((artifacts.get("usage_proxy") or {}).get("errors_count") or 0),
        # A manual run never runs the scheduled connector-native recovery policy,
        # so its broker failures must not be recorded under that outcome.
        scheduled=scheduled,
    )
    fields = outcome.as_run_state_fields()
    # A concrete status the placement path derived -- dry_run,
    # not_submitted_confirmation_required (review passed, submission awaiting
    # confirmation), not_submitted_error -- is more specific than "no orders" and
    # stays, or downstream consumers such as is_confirmation_blocked would lose
    # it. Two are replaced:
    #   * not_submitted_budget_exhausted is the fallback
    #     _derive_order_submit_status reaches when nothing else matched, so it is
    #     only trustworthy when a candidate really was blocked on a budget, cap,
    #     or buying-power check; and
    #   * not_submitted_no_candidates describes exactly the no-ladder-candidate
    #     case the contract assigns 'no_orders_submitted'. Keeping it would make
    #     the afternoon aggregate disagree with both the contract and the morning
    #     stream for the same situation (Codex review). Its specificity is not
    #     lost: completion_reason='no_ladder_candidates' carries it, which is the
    #     field designed for exactly that.
    current = artifacts.get("order_submit_status")
    budget_blocked = any(oc.get("budget_blocked") for oc in outcomes)
    replaceable = current in (
        ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED,
        ORDER_SUBMIT_STATUS_NO_CANDIDATES,
    )
    if not replaceable or (
        current == ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED and budget_blocked
    ):
        fields.pop("order_submit_status", None)
    artifacts.update(fields)


# CSV artifacts a run may leave beside run_state.json, and the status recorded in
# the minimal run_state stub written when the date gate blocks execution.
_RUN_CSV_NAMES = ("signals.csv", "buy_candidates.csv", "orders.csv")
RUN_STATUS_BLOCKED = "blocked"


def _existing_csv_artifacts(out: Path) -> List[Path]:
    return [out / name for name in _RUN_CSV_NAMES if (out / name).exists()]


def _serialize_run_state(run_state: Dict[str, Any]) -> str:
    return json.dumps(run_state, indent=2, sort_keys=True)


def _settle_usage_bytes(
    run_state: Dict[str, Any], usage: Dict[str, Any], csv_bytes: int
) -> str:
    """Set state_file_bytes/total_artifact_bytes to match the payload written to disk.

    The serialized size only shifts when a counter changes digit count, so this
    settles in a pass or two; the values are documented usage *proxies*, so a
    pathological digit-boundary case exiting one byte off is tolerable.
    """
    serialized = _serialize_run_state(run_state)
    for _ in range(4):
        state_bytes = len(serialized.encode("utf-8"))
        if (
            usage.get("state_file_bytes") == state_bytes
            and usage.get("total_artifact_bytes") == state_bytes + csv_bytes
        ):
            break
        usage["state_file_bytes"] = state_bytes
        usage["total_artifact_bytes"] = state_bytes + csv_bytes
        serialized = _serialize_run_state(run_state)
    return serialized


def write_run_artifacts(
    artifacts: Dict[str, Any], output_dir: str | Path
) -> Dict[str, str]:
    """Write the run-state JSON plus signal and candidate CSVs; return their paths."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    signals_path = out / "signals.csv"
    _write_csv(signals_path, artifacts["signal_rows"])

    candidates_path = out / "buy_candidates.csv"
    _write_csv(candidates_path, artifacts["buy_candidates"])

    # Count only the CSVs this run just wrote. orders.csv (written afterwards on
    # the compact path) is folded in by _backfill_orders_csv_usage; a stale
    # orders.csv left in a reused directory must not be attributed to this run.
    csv_paths = [signals_path, candidates_path]
    csv_bytes = sum(path.stat().st_size for path in csv_paths)

    usage = artifacts.get("usage_proxy")
    if isinstance(usage, dict):
        usage["csv_artifact_count"] = len(csv_paths)
        serialized = _settle_usage_bytes(artifacts, usage, csv_bytes)
    else:
        serialized = _serialize_run_state(artifacts)

    run_state_path = out / "run_state.json"
    atomic_write_text(run_state_path, serialized)

    return {
        "run_state": str(run_state_path),
        "signals": str(signals_path),
        "buy_candidates": str(candidates_path),
    }


def _backfill_orders_csv_usage(run_state_path: Path) -> None:
    """Re-derive the CSV usage-proxy counters from disk after orders.csv is written.

    ``orders.csv`` lands after ``write_run_artifacts`` has already finalized
    ``run_state.json``, so without this pass the daily compact path would
    undercount ``csv_artifact_count``/``total_artifact_bytes``.
    """
    if not run_state_path.exists():
        return
    run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
    usage = run_state.get("usage_proxy")
    if not isinstance(usage, dict):
        return
    csv_paths = _existing_csv_artifacts(run_state_path.parent)
    usage["csv_artifact_count"] = len(csv_paths)
    csv_bytes = sum(path.stat().st_size for path in csv_paths)
    atomic_write_text(run_state_path, _settle_usage_bytes(run_state, usage, csv_bytes))


# Stable header for a zero-row signals.csv / buy_candidates.csv. An empty
# universe (no active tickers, or every configured ticker excluded) previously
# wrote a zero-byte file, which the artifact contract rejects for lacking a
# header, failing the run and blocking the latest-pointer update (Codex review).
_EMPTY_CSV_FIELDS = ("ticker",)


def _write_csv(
    path: Path,
    rows: Sequence[Dict[str, Any]],
    *,
    empty_fieldnames: Sequence[str] = _EMPTY_CSV_FIELDS,
) -> None:
    if not rows:
        with atomic_open_w(path, newline="") as handle:
            csv.DictWriter(handle, fieldnames=list(empty_fieldnames)).writeheader()
        return
    fieldnames = list(rows[0].keys())
    with atomic_open_w(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# Buy-candidate statuses that mean an order was actually placed (and therefore
# count as prior same-day allocation for the top-up guard).
# "dry_run" is deliberately excluded: its own skip_reason ("dry_run: order not
# placed") means no real capital moved, so a manual dry-run test earlier the
# same day must never reduce the scheduled run's real buy.
_PRIOR_ALLOCATION_STATUSES = frozenset({"submitted", "filled"})


def _candidate_logged_reference_price(candidate: Dict[str, Any]) -> Optional[float]:
    """Resolve a prior candidate's logged reference price by policy preference.

    Same-day top-up guard policy resolves a prior run's reference price in this
    order: the run's logged ``target_price``, then ``decision_price`` (also
    recorded as ``decision_bar_close``). Callers must only use this logged
    reference for candidates whose capital movement is confirmed (for example a
    filled candidate or a candidate with a confirmed fill row). Unconfirmed
    submitted candidates keep an unknown reference even when target/decision
    prices were logged, routing later top-ups to the conservative manual-review
    path. Returns the first strictly-positive value, else ``None``.
    """
    for key in ("target_price", "decision_price", "decision_bar_close"):
        raw = candidate.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


# Streams whose runs are scheduled evidence for THEIR OWN slot but still count
# as prior same-day allocation context for a later run. intraday* is matched by
# prefix in _prior_run_is_scheduled since that stream carries a per-slot suffix.
# The afternoon stream is deliberately absent.
_PRIOR_CONTEXT_ELIGIBLE_STREAMS = (
    "morning",
    "midday",
    "market_open",
)

# Recovery-context counterparts of the eligible streams above. Handled on a
# separate branch because, unlike the streams above, being named is NOT
# sufficient: the artifact must also satisfy the scheduled-recovery contract
# before its fills may reduce a later buy. No afternoon entry -- an afternoon
# recovery is the afternoon run's own repair, not prior context for it.
_RECOVERY_PRIOR_CONTEXT_STREAMS = (
    "market_open_recovery",
    "morning_recovery",
    "midday_recovery",
    # The intraday task fires at 11:30/13:30/14:30, so its recovery stream is
    # slot-qualified (e.g. intraday_1430_recovery, the Jul 27 2:32 PM pass).
    "intraday_recovery",
    "intraday_1130_recovery",
    "intraday_1330_recovery",
    "intraday_1430_recovery",
)


def _recovery_root_for(run_dir: Path) -> Path:
    """Delegate to run_classification's canonical recovery-root resolver.

    Imported lazily and by both module paths, matching
    :func:`_is_scheduled_recovery_context`. On ImportError we fall back to
    ``run_dir`` -- the narrower boundary, and moot in practice: the same module
    must already have imported successfully for the recovery branch that calls
    this to have been entered at all.
    """
    try:
        from scripts.run_classification import recovery_root_for
    except ImportError:  # pragma: no cover - exercised via the scripts/ path
        try:
            from run_classification import recovery_root_for
        except ImportError:
            return run_dir
    return recovery_root_for(run_dir)


def _path_within(path: Path, root: Path) -> bool:
    """True when ``path`` resolves inside ``root`` (or is ``root`` itself).

    Resolved on both sides, so a symlink or ``..`` segment cannot escape.
    """
    try:
        resolved = path.resolve()
        root_key = root.resolve()
    except OSError:
        return False
    return resolved == root_key or root_key in resolved.parents


def _is_scheduled_recovery_context(
    run_state: Dict[str, Any], trading_day: Optional[str] = None
) -> bool:
    """True only for a classification satisfying the scheduled-recovery contract.

    Delegates to ``scripts.run_classification.validate_recovery_classification``,
    the single source of truth, rather than re-deriving the rules here. An
    earlier local copy checked only the origin and boolean flags, so a
    recursively discovered ``midday_recovery`` state could omit or fake
    ``run_type``, ``run_id``, ``state_schema_version``, and the classification
    ``trading_day`` and still have its fills reduce or block the 3:30
    allocation (Codex review: "Enforce the full recovery contract before
    loading fills"). This module already imports from ``scripts/`` on
    neighbouring guard paths (see ``derive_valid_scheduled_evidence`` below),
    so there is no reason to keep a second, drifting implementation.

    Takes the whole RUN_STATE, not just its classification. The state-level
    entry point derives the expected day from the state's own top-level
    ``trading_day`` -- so a state dated for today whose recovery classification
    names a stale or bogus day is rejected rather than loaded (Codex review:
    "Pass the state day into recovery validation") -- and fails closed on a
    top-level ``artifact_contract_failure`` marker, which a classification-only
    check would have accepted as usable prior context (Codex review: "Reject
    artifact-contract failures as recovery context").

    Fails CLOSED on an import error: an artifact that cannot be validated is
    not trusted as prior context.

    See schemas/recovery_run_classification.schema.json for the full contract.
    """
    if not isinstance(run_state, dict):
        return False
    try:
        from scripts.run_classification import is_valid_recovery_context_state
    except ImportError:
        logger.exception(
            "could not import the recovery-contract validator; treating the "
            "recovery artifact as untrusted (it will NOT offset a later buy)"
        )
        return False
    return is_valid_recovery_context_state(run_state, trading_day)


def _prior_run_is_scheduled(run_state: Dict[str, Any]) -> bool:
    """True when a run_state is classified as the scheduled *afternoon* run.

    The scheduled afternoon run is never read as prior allocation context. A
    morning-, midday-, or market-open-stream run (``run_classification.stream``
    in ``{'morning', 'midday', 'market_open'}``), however, IS eligible prior
    context even when its own ``run_origin`` is ``'scheduled'``: the 9:35 AM
    market-open live task, the 10:30 AM morning live task, and the 12:30 PM
    midday live task are scheduled within their own streams but must still
    offset the afternoon top-up (Codex review: "Feed market-open fills into
    later top-up guards"). An ad hoc intraday scheduled-recovery stream
    (``stream`` starting with ``'intraday'`` -- e.g. the Jul 24 2026
    ``intraday_1430`` recovery, see config/scheduled_tasks.json
    canonical_first_buy_and_topup_rule -> intraday_dca_scheduled_recovery_2026_07_24,
    and the recurring 11:30/13:30/14:30 intraday live task 7be82b97) is
    eligible for the same reason: it is scheduled evidence for ITS OWN stream
    but must still offset a later same-day run (Codex review: "Include the
    intraday recovery in later top-up context"). Only afternoon scheduled runs
    are excluded here.

    A recovery stream for one of those earlier slots (``morning_recovery``,
    ``midday_recovery``, ``market_open_recovery`` -- e.g. the Jul 27 2026 12:30
    recovery pass, see config/scheduled_tasks.json ->
    midday_dca_scheduled_recovery_2026_07_27) is eligible for the same reason:
    it moved real capital that day and must offset a later run, while never
    being scheduled evidence for the slot it recovered. It is matched
    explicitly rather than left to the ``run_origin`` fallthrough below -- a
    recovery artifact carries ``run_origin='scheduled_recovery'``, which merely
    happens not to equal ``'scheduled'``, so today it lands on eligible by
    accident. Naming it means a future recovery classification that DOES use
    ``run_origin='scheduled'`` cannot silently flip to ineligible (Codex
    review: "Add the recovery root to watchdog classification"). An
    *afternoon* recovery is deliberately NOT listed: like any afternoon
    scheduled run it must stay invisible to the afternoon guard's own pass.
    """
    classification = run_state.get("run_classification")
    if isinstance(classification, dict):
        stream = classification.get("stream")
        # Dispatch on the ORIGIN first, then the stream. Keying the recovery
        # branch off the stream name alone left every malformed-stream shape
        # unvalidated: a state with run_origin='scheduled_recovery' but a
        # missing or non-string stream fell through to the generic arms, and
        # one naming e.g. 'intraday_bogus' was even accepted as eligible by the
        # startswith('intraday') arm below -- so a corrupted artifact under a
        # recursively scanned root had its fills loaded without ever reaching
        # the canonical recovery validator (Codex review: "Gate every
        # scheduled-recovery origin before loading fills").
        if classification.get("run_origin") == "scheduled_recovery" or (
            isinstance(stream, str) and stream.endswith("_recovery")
        ):
            # A recovery stream is eligible prior context ONLY when it is one of
            # the earlier-slot recovery streams AND the artifact satisfies the
            # scheduled-recovery contract. Naming the stream is not enough: the
            # recovery dirs are dated siblings under the recursively scanned
            # midday/morning roots, and _prior_run_is_valid_manual_recovery_artifact
            # only guards paths containing "manual_recovery", so a corrupted or
            # misclassified file that merely sets stream='midday_recovery' would
            # otherwise be read as prior context and could wrongly reduce or
            # block the 3:30 allocation (Codex review: "Gate recovery streams
            # before loading their fills").
            #
            # Returning True means "treated as a scheduled run", i.e. NOT prior
            # context -- the safe direction, since the artifact is then ignored
            # rather than trusted. An afternoon recovery lands here too: it is
            # absent from the eligible set, so it stays invisible to the
            # afternoon guard's own pass.
            if stream not in _RECOVERY_PRIOR_CONTEXT_STREAMS:
                return True
            # The top-level state day is what load_prior_same_day_allocations
            # already accepted this artifact on, so require the classification
            # to name the SAME day rather than merely a non-blank one.
            return not _is_scheduled_recovery_context(run_state)
        if stream in _PRIOR_CONTEXT_ELIGIBLE_STREAMS:
            # Explicitly named streams are unchanged. A MANUAL artifact under
            # morning/midday/market_open is legitimate prior context and is
            # validated by _prior_run_is_valid_manual_recovery_artifact, so the
            # origin check below must not reach it.
            return False
        if isinstance(stream, str) and stream.startswith("intraday"):
            # Naming the stream is NOT enough, for the same reason it is not
            # enough on the recovery branch above. 'intraday_1430_rerun' starts
            # with 'intraday' but carries run_origin='manual_live_recovery_rerun'
            # -- the shape every manifest in this PR records as rejected by
            # is_top_up_context_state() -- and it ends in '_rerun', so it misses
            # the '_recovery' branch and was admitted here unvalidated. A rerun
            # carrying a submitted-but-never-filled candidate would then load as
            # unresolved prior context and suppress the legitimate final buy
            # (Codex review: "Validate ordinary intraday origins before loading
            # context").
            #
            # Only the ORDINARY scheduled identity is admitted here. Returning
            # True for anything else means "treated as a scheduled run", i.e.
            # ignored -- the safe direction, and it leaves manual and
            # scheduled-recovery artifacts to the validators that own them.
            origin = classification.get("run_origin")
            if classification.get("manual_run") is True:
                return True
            if origin not in (None, "scheduled"):
                return True
            return False
        if classification.get("run_origin") == "scheduled":
            return True
        if (
            classification.get("manual_run") is False
            and classification.get("run_origin") is None
        ):
            return True
    return False


def _prior_run_is_valid_manual_recovery_artifact(
    run_state: Dict[str, Any], run_state_path: Path
) -> bool:
    """True unless a manual_recovery/ artifact fails its required classification.

    ``manual_missed_run_recovery`` (config/morning_dca.json) makes a manual
    missed-run recovery artifact valid prior-allocation / same-day top-up
    context ONLY when it carries ``run_origin='manual'``, ``manual_run=true``,
    ``same_day_top_up_context_only=true``, and ``valid_scheduled_evidence=false``.
    A malformed or misclassified file placed under a ``manual_recovery/``
    directory must not be able to reduce or block a later buy, so such a file
    is excluded here rather than trusted at face value. Artifacts outside a
    ``manual_recovery/`` directory (e.g. manual override reruns, which use a
    distinct classification contract) are unaffected by this check.
    """
    if "manual_recovery" not in run_state_path.parts:
        return True
    classification = run_state.get("run_classification")
    if not isinstance(classification, dict):
        return False
    return (
        classification.get("run_origin") == "manual"
        and classification.get("manual_run") is True
        and classification.get("same_day_top_up_context_only") is True
        and classification.get("valid_scheduled_evidence") is False
    )


def _prior_run_before_boundary(run_state: Dict[str, Any], boundary_et: time) -> bool:
    """True when the run's generated_at (ET) is before the morning/manual boundary.

    Runs with no parseable timestamp are treated as eligible prior context: the guard
    is conservative (more prior context only ever reduces the afternoon buy), so an
    unknown time should not silently disable the offset.
    """
    raw = run_state.get("generated_at")
    if not isinstance(raw, str):
        return True
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN).timetz().replace(tzinfo=None) < boundary_et


def _prior_run_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """Parse a run_state ``generated_at`` string into an ET-aware datetime for recency ordering.

    Mirrors :func:`_prior_run_before_boundary`'s parsing (a naive timestamp is
    assumed ET). Returns ``None`` when ``raw`` is missing/unparseable; callers fall
    back to :data:`_TS_FLOOR` (reference-price recency) or to scan/merge order
    (first-allocation base recency, see :func:`_earlier_selected_base`) so an
    untimestamped run loses ties to any timestamped run but still wins later
    scan/merge order against other untimestamped runs.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN)


def _earlier_selected_base(
    existing_base: Optional[float],
    existing_at: Optional[str],
    alloc_base: Optional[float],
    alloc_at: Optional[str],
) -> tuple[Optional[float], Optional[str]]:
    """Pick the base belonging to the chronologically FIRST of two allocations.

    Used wherever two :class:`PriorSameDayAllocation` values for the same ticker
    (typically from different roots/streams) are merged, so the day's base
    always reflects the true first same-day allocation rather than whichever
    side happened to be merged first (Codex review: "Select the base from the
    chronologically first allocation" -- a later manual override loaded from the
    afternoon root must not out-rank an earlier morning/midday allocation just
    because the afternoon root is scanned first). Falls back to preferring
    ``existing`` when either timestamp is missing/unparseable, matching the
    default used before chronological comparison was possible.
    """
    existing_dt = _prior_run_timestamp(existing_at)
    alloc_dt = _prior_run_timestamp(alloc_at)
    if existing_dt is not None and alloc_dt is not None:
        return (
            (alloc_base, alloc_at)
            if alloc_dt < existing_dt
            else (existing_base, existing_at)
        )
    return (
        (existing_base, existing_at)
        if existing_base is not None
        else (alloc_base, alloc_at)
    )


# Confirmed-fill column aliases. The codebase-standard writers (_CSV_FIELDS,
# the scheduled artifact writers) emit avg_fill_price/quantity_filled, but a
# recovery-context artifact persists the broker's own field names,
# average_fill_price/quantity -- the shape recorded for the Jul 27 2026 12:30
# recovery pass (config/scheduled_tasks.json ->
# midday_dca_scheduled_recovery_2026_07_27.fills). Without these aliases such a
# filled_orders.csv parses to no usable price or quantity, so the ticker either
# drops out of the same-day context entirely or lands in the guard's
# conservative missing_prior_price / $0 manual_review path -- silently
# re-allocating capital already spent that day (Codex review: "Teach the
# final-run loader the recovery fill shape"). scripts/run_classification.py's
# _PRICE_COLS/_QUANTITY_COLS already accept both spellings; these mirror it for
# the core loader. Canonical name first: an artifact carrying both wins on the
# codebase-standard column.
# Broker outcomes in the recovery shape's `state` column that mean the order
# will NEVER fill. Used by _orders_csv_submitted_notionals to drop a row whose
# status still says 'submitted'; a pending/queued/unrecognized state is
# deliberately NOT here, because an order that may yet fill must keep counting
# as prior allocation.
def _load_terminal_order_states() -> frozenset:
    """The canonical broker terminal-state set, from broker_reconciliation.

    Sourced rather than re-declared. This set had been hand-copied here and
    into scripts/run_classification, and both copies silently omitted `voided`
    and `expired` -- so a zero-execution order that could no longer fill was
    still carried as live unknown context, forcing its ticker onto the $0
    manual-review path and suppressing a legitimate top-up (Codex review:
    "Treat voided and expired broker orders as terminal"). Any state the broker
    normalizer learns about now reaches both readers automatically.

    The literal is a fallback for the import failing, not a second source of
    truth; it is kept identical to the canonical set.
    """
    try:
        from scripts.broker_reconciliation import _TERMINAL_ORDER_STATES
    except ImportError:  # pragma: no cover - exercised via the scripts/ path
        try:
            from broker_reconciliation import _TERMINAL_ORDER_STATES
        except ImportError:
            return frozenset(
                {"cancelled", "canceled", "rejected", "failed", "voided", "expired"}
            )
    return frozenset(_TERMINAL_ORDER_STATES)


_TERMINAL_NON_FILL_STATES = _load_terminal_order_states()

# Submitted-amount aliases, canonical first. `amount_usd` is the broker-native
# recovery spelling; _orders_csv_fills already reads it.
_SUBMITTED_NOTIONAL_COLS = ("notional_usd", "amount_usd")
_FILL_PRICE_COLS = ("avg_fill_price", "average_fill_price")
# EXECUTED-quantity columns, in precedence order. `quantity` is deliberately
# absent: in the broker-native shape it is the REQUESTED size and
# `cumulative_quantity` is what actually filled -- exactly the distinction
# scripts/broker_reconciliation.py._normalize_order() already draws, including
# its warning that an `x or y` fallthrough misreads a 0-filled order as fully
# executed. Treating `quantity` as executed meant a canceled zero-fill row
# (`quantity=1`, `cumulative_quantity=0`) reported executed capital and its full
# request was retained as prior allocation, suppressing a legitimate later
# top-up; partially filled rows were overstated the same way (Codex review:
# "Prefer cumulative quantity over requested quantity").
_EXECUTED_QUANTITY_COLS = ("quantity_filled", "cumulative_quantity")
# `quantity` remains a LAST-RESORT alias, used only when neither executed column
# is present at all. That is the recovery artifact shape recorded in
# config/scheduled_tasks.json -> recovery_context_run_outcomes, whose `quantity`
# IS the filled amount because it carries no cumulative_quantity column.
_FILL_QUANTITY_COLS = (*_EXECUTED_QUANTITY_COLS, "quantity")


def _first_present(row: Mapping[str, Any], cols: Sequence[str]) -> Any:
    """First non-blank value among ``cols``, else None.

    Blank ("", "-") and missing columns are skipped rather than returned, so a
    CSV that carries BOTH spellings with the canonical one left empty still
    resolves via the alias instead of yielding a blank that the caller would
    read as "no value".
    """
    for col in cols:
        value = row.get(col)
        if value not in (None, "", "-"):
            return value
    return None


def _has_executed_quantity(row: Mapping[str, object]) -> bool:
    """True when an orders-CSV row records a positive executed quantity.

    The test that decides whether a non-``filled`` row still represents deployed
    capital. A ``partially_filled`` row, or a ``canceled`` one that executed some
    quantity before it ended, carries real shares; only a row with no executed
    quantity can be discarded. math.isfinite rejects a corrupted 'inf'/'nan' cell
    so it cannot pass as executed size.

    An EXPLICIT ZERO in an executed column is authoritative and stops the search
    -- it says the order filled nothing, which is the whole answer. Falling
    through to the requested ``quantity`` on a 0 would report a canceled
    zero-fill row as executed capital, the exact trap
    ``broker_reconciliation._normalize_order()`` documents.
    """
    for col in _EXECUTED_QUANTITY_COLS:
        value = row.get(col)
        if value in (None, "", "-"):
            continue
        try:
            executed = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(executed) and executed > 0
    raw = _first_present(row, _FILL_QUANTITY_COLS)
    if raw in (None, "", "-"):
        return False
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(qty) and qty > 0


def _executed_quantity_is_explicit_zero(row: Mapping[str, object]) -> bool:
    """True only when an executed column states a definitive numeric ZERO.

    Stricter than ``not _has_executed_quantity(row)``, which also returns true
    for an absent, blank, non-finite, or unparsable value. Those say "unknown",
    not "nothing filled" -- and a caller that REMOVES prior allocation must not
    read the first as the second: `cumulative_quantity='oops'` on a canceled row
    does not establish that the order failed to execute, so treating it as a
    zero fill drops a candidate that may really have partially filled and lets
    the later run buy the full amount again (Codex review: "Require an explicit
    zero before excluding terminal candidates").
    """
    for col in _EXECUTED_QUANTITY_COLS:
        value = row.get(col)
        if value in (None, "", "-"):
            continue
        try:
            executed = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(executed) and executed == 0
    raw = _first_present(row, _FILL_QUANTITY_COLS)
    if raw in (None, "", "-"):
        # No quantity column at all. The row says nothing about execution, so it
        # is unresolved rather than a zero fill.
        return False
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(qty) and qty == 0


def _orders_csv_fills(
    run_dir: Path,
    *,
    orders_csv_file: Optional[Path] = None,
    skip_sink: Optional[List[Path]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Map ticker -> {order_id, avg_fill_price, quantity_filled} for filled rows only.

    Used to reconcile a prior run's ``buy_candidates`` (whose status is fixed at
    order-submission time) against what the broker actually confirmed: a candidate
    recorded ``status='submitted'`` in run_state.json only means an order_id was
    obtained, not that it filled. A submission that never filled (execution gap,
    reject, cancel) must not count as prior-allocation capital.

    Pass ``orders_csv_file`` to read a specific CSV (e.g. the path recorded in
    ``run_state.artifacts.orders_csv_path``) instead of looking for a sibling
    ``orders.csv``; ``run_and_report`` writes files with custom names like
    ``dca_<label>_<day>_<stamp>_orders.csv`` that ``run_dir / "orders.csv"`` won't find.

    Pass ``skip_sink`` when ``orders_csv_file`` names a CSV a caller already
    knows exists (e.g. just confirmed via ``.exists()``) so a read/parse
    failure caught mid-rewrite still gets recorded -- otherwise it is
    indistinguishable from a genuinely empty (zero-fills) day (Codex review:
    "Include referenced CSV failures in scan completeness").

    When no explicit file is given the sibling ``orders.csv`` is used, falling back
    to ``filled_orders.csv`` -- the confirmed-fill record manual override reruns
    write under ``.../manual_reruns/`` -- so a manual override fill still counts as
    prior-allocation context for the same-day top-up guard. Manual fill files may
    use the documented ``amount_usd``/``avg_fill_price`` shape without orders.csv
    fields like ``status`` or ``quantity_filled``.
    """
    fills: Dict[str, Dict[str, Any]] = {}
    if orders_csv_file is not None:
        orders_csv = orders_csv_file
    else:
        # Prefer filled_orders.csv (confirmed-fill record with actual fill
        # price/quantity) over orders.csv (submitted stub with blank fill
        # fields), so a partial fill or cancellation is seen correctly by the
        # afternoon top-up guard. Fall back to orders.csv when no confirmed
        # fill record exists.
        filled_csv = run_dir / "filled_orders.csv"
        orders_csv = filled_csv if filled_csv.exists() else run_dir / "orders.csv"
    if not orders_csv.exists():
        return fills
    rows = []
    fieldnames = None
    had_csv_error = False
    try:
        with orders_csv.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames  # reads the header row
            if fieldnames is not None:
                try:
                    for row in reader:
                        rows.append(row)
                except csv.Error:
                    had_csv_error = True
                    logger.exception(
                        "failed to parse %s for same-day fill context; prior allocations from "
                        "this file may be undercounted -- reconcile against broker orders",
                        orders_csv,
                    )
                    if skip_sink is not None:
                        skip_sink.append(orders_csv)
    except (OSError, UnicodeDecodeError):
        logger.exception(
            "failed to parse %s for same-day fill context; prior allocations from "
            "this file may be undercounted -- reconcile against broker orders",
            orders_csv,
        )
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        return fills
    except Exception:
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        return fills
    if fieldnames is None:
        # write_execution_csv() unconditionally calls writer.writeheader(),
        # so a COMPLETE write always has a header row even on a genuine
        # zero-orders day (rows == [] with a real header is a legitimate
        # terminal state, not an error). fieldnames is None only when the
        # file has no header line at all -- observed in the instant right
        # after its truncating open(path, "w") but before even the header
        # was flushed, which `if rows` alone can't distinguish from that
        # legitimate empty day (Codex review: "Treat empty CSV snapshots as
        # incomplete").
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        return fills
    if not had_csv_error and rows and any(value is None for value in rows[-1].values()):
        # The final row has FEWER fields than the header -- csv.DictReader
        # fills any missing trailing columns with None (its `restval`)
        # rather than raising, so a file observed mid-write with its last
        # record only partially flushed (e.g. "AAPL,filled" with the
        # price/quantity/order_id columns not yet written) never reaches the
        # except-clause skip_sink signal above (Codex review: "Detect
        # silently truncated CSV reads"). Drop the untrustworthy tail row
        # but keep whatever was already fully flushed before it.
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        rows = rows[:-1]
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        status = (row.get("status") or "").lower()
        # The broker-native recovery shape records the outcome in `state`, not
        # `status`. Without this, a recovery filled_orders.csv row with
        # state='rejected'/'canceled' hit the statusless is_manual_filled_orders
        # heuristic below and was counted as deployed capital -- amount and
        # reference price included -- which reduces or entirely suppresses that
        # ticker's 3:30 allocation for an order that never filled (Codex review:
        # "Honor the recovery row state before counting fills"). When the column
        # is present it is authoritative: anything other than 'filled',
        # including a blank cell, is not a confirmed fill.
        # EXECUTED QUANTITY decides, not the state name. Requiring state=='filled'
        # discarded a 'partially_filled' row, and a 'canceled' row that executed
        # some quantity before it ended -- both of which are real deployed
        # capital. Dropping them made the loader report no prior buy and let the
        # later run purchase the full amount again, the expensive direction and
        # exactly what this guard exists to prevent.
        # scripts/broker_reconciliation.py already treats partially_filled as
        # live exposure and a terminal order with executed dollars as prior
        # exposure, for the same reason (Codex review: "Preserve executed partial
        # fills in recovery CSVs").
        row_state = (row.get("state") or "").strip().lower() if "state" in row else None
        # `state` is the BROKER's answer; `status` is what we asked for. When the
        # broker says filled, that settles it -- a row carrying
        # status='submitted' with state='filled' was being dropped here by the
        # status gate below, then kept by _orders_csv_submitted_notionals as an
        # unknown-price notional, forcing the ticker onto the conservative $0
        # manual-review path despite having a real fill price and quantity
        # (Codex review: "Let the broker state confirm filled recovery rows").
        state_confirms_fill = row_state == "filled"
        partial_execution = False
        if row_state is not None and not state_confirms_fill:
            if not _has_executed_quantity(row):
                continue
            partial_execution = True
        is_manual_filled_orders = (
            orders_csv.name == "filled_orders.csv" or "amount_usd" in row
        ) and not status
        # partial_execution passes too. Last round taught this gate about
        # state='filled' but not about a row still carrying status='submitted'
        # while the broker reports partially_filled (or canceled after executing
        # some quantity). Such a row was dropped here while
        # _orders_csv_submitted_notionals kept its FULL requested notional, so a
        # $10 request with $2 executed counted as $10 deployed and suppressed
        # the remaining $8 top-up. Letting it through means the executed
        # price * quantity recompute below wins instead (Codex review: "Let
        # submitted partial executions reach the fill reader").
        if (
            status != "filled"
            and not state_confirms_fill
            and not partial_execution
            and not is_manual_filled_orders
        ):
            continue
        raw_price = _first_present(row, _FILL_PRICE_COLS)
        try:
            price = float(raw_price) if raw_price not in (None, "", "-") else None
        except (TypeError, ValueError):
            price = None
        # `> 0` alone excludes NaN (NaN > 0 is False) but NOT Infinity
        # (float('inf') > 0 is True) -- math.isfinite() also rejects a
        # corrupted/malformed CSV value of "inf"/"Infinity" so it can
        # never poison the same-day top-up guard with an unbounded
        # reference price or prior allocation (Gemini review on PR #31).
        price_valid = price is not None and math.isfinite(price) and price > 0
        raw_qty = _first_present(row, _FILL_QUANTITY_COLS)
        try:
            qty = float(raw_qty) if raw_qty not in (None, "", "-") else None
        except (TypeError, ValueError):
            qty = None
        qty_valid = qty is not None and math.isfinite(qty) and qty > 0
        raw_amount = row.get("amount_usd")
        try:
            amount = float(raw_amount) if raw_amount not in (None, "", "-") else None
        except (TypeError, ValueError):
            amount = None
        amount_valid = amount is not None and math.isfinite(amount) and amount > 0
        if partial_execution and price_valid and qty_valid:
            # For a row that did NOT reach state='filled', amount_usd is the
            # amount that was REQUESTED; only price * executed-quantity is the
            # cash that actually moved. Using the recorded amount would overstate
            # prior allocation and suppress a legitimate top-up. When the two
            # agree (amount_usd already being executed cash) this changes
            # nothing.
            amount = price * qty
            amount_valid = math.isfinite(amount) and amount > 0
        if not price_valid and not amount_valid:
            # No usable price AND no usable amount_usd -- genuinely no
            # capital-movement information to preserve for this row.
            continue
        fills[ticker] = {
            "order_id": row.get("order_id"),
            # A PARTIAL execution's notional is real and must count, but its
            # price is not a settled reference: the unexecuted remainder may
            # still reprice or be cancelled. Callers that resolve a reference
            # price check this flag; _read_csv_rows draws the same line with
            # _partial_execution, and the two readers disagreed until now
            # (Codex review: "Keep partial executions from confirming a
            # reference price").
            "_partial_execution": partial_execution,
            # Preserve amount-only rows (amount_usd present, price missing/
            # invalid) as an unknown-price allocation instead of dropping
            # them outright -- the confirmed CSV amount must not be
            # silently discarded in favor of a stale planned amount.
            "avg_fill_price": price if price_valid else None,
            "quantity_filled": qty if qty_valid else None,
            "amount_usd": amount if amount_valid else None,
        }
    return fills


def _fill_rank(fill: Mapping[str, Any]) -> int:
    """How authoritative a fill entry is about an order's outcome. Higher wins.

    Mirrors ``scripts.run_classification._dedupe_rank``: a CONFIRMED full fill
    outranks a partial execution. Used when the same ticker appears in more than
    one of a recovery's CSVs -- a stale ``partially_filled`` snapshot beside
    run_state.json versus the explicitly recorded CSV's final ``filled`` state.
    Keeping whichever was read first preserved the partial's notional and its
    unresolved reference price, forcing an unnecessary manual-review stop for an
    order that had since completed (Codex review: "Reconcile overlapping ticker
    fills instead of keeping the first").
    """
    return 0 if fill.get("_partial_execution") else 1


def _fill_order_id(fill: Mapping[str, Any]) -> str:
    """The fill's order_id, normalized; empty string when it carries none."""
    oid = fill.get("order_id")
    oid = oid.strip() if isinstance(oid, str) else ""
    return "" if oid == "-" else oid


def _combined_fill(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate two DISTINCT orders for one ticker into a single entry.

    Amounts and quantities add, because both orders really moved capital. The
    reference price becomes the quantity-weighted average of the two fills --
    the cost basis a later top-up should be measured against. When either side
    lacks a usable price or quantity the aggregate price is unknowable, so it
    is left None and the guard routes to its conservative manual-review path
    rather than comparing against a price that was never paid.
    """

    def _num(value):
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    amounts = [_num(a.get("amount_usd")), _num(b.get("amount_usd"))]
    qtys = [_num(a.get("quantity_filled")), _num(b.get("quantity_filled"))]
    prices = [_num(a.get("avg_fill_price")), _num(b.get("avg_fill_price"))]

    amount = sum(x for x in amounts if x) or None
    quantity = sum(x for x in qtys if x) or None
    price = None
    if all(p and p > 0 for p in prices) and all(q and q > 0 for q in qtys):
        price = (prices[0] * qtys[0] + prices[1] * qtys[1]) / (qtys[0] + qtys[1])
    return {
        # No single order_id describes the pair; the aggregate is what matters
        # downstream, and a wrong single ID would be worse than none.
        "order_id": None,
        # Unresolved if EITHER leg is, since the combined remainder is live.
        "_partial_execution": bool(
            a.get("_partial_execution") or b.get("_partial_execution")
        ),
        "avg_fill_price": price,
        "quantity_filled": quantity,
        "amount_usd": amount,
    }


def _merge_fill(
    fills: Dict[str, Dict[str, Any]], ticker: str, fill: Dict[str, Any]
) -> None:
    """Reconcile a fill into ``fills``, keyed by ORDER, not merely by ticker.

    Three cases, and conflating them is what went wrong before:

    * **Same order** seen again in another sibling CSV -- replace when the new
      snapshot is strictly more authoritative (a completed fill over a stale
      partial), otherwise keep the incumbent. Never sums: the same order in two
      files is one order, and adding would double-count the capital this guard
      exists to count once.
    * **A distinct order** for a ticker already present -- ADD it. Keeping only
      the first understated deployed capital and let the candidate loop apply
      one order's fill to every candidate for that ticker, permitting an
      excessive top-up (Codex review: "Merge recovery fills by order ID before
      aggregating tickers").
    * **Unidentifiable** -- either side missing an order_id. Fall back to
      rank-replacement rather than adding, since two rows that cannot be told
      apart may well be the same order, and double-counting is the direction
      that spends real money twice.
    """
    existing = fills.get(ticker)
    if existing is None:
        fills[ticker] = fill
        return
    new_oid, old_oid = _fill_order_id(fill), _fill_order_id(existing)
    if new_oid and old_oid and new_oid != old_oid:
        fills[ticker] = _combined_fill(existing, fill)
        return
    if _fill_rank(fill) > _fill_rank(existing):
        fills[ticker] = fill


def _orders_csv_terminal_non_fills(
    orders_csv: Path,
) -> tuple[set, set]:
    """(order_ids, tickers) the CSV reports as TERMINALLY not filled.

    A row whose ``state`` is rejected/canceled/failed AND that executed no
    quantity is authoritative evidence that no capital moved for that order.
    Rows that executed something before ending are excluded -- a
    cancelled-after-partial-fill is real deployed capital.

    The tickers set is a fallback for rows carrying no usable ``order_id``; a
    caller must only apply it when nothing else claims the ticker, since a
    ticker can have both a rejected order and a separate real fill.
    """
    order_ids: set = set()
    tickers: set = set()
    if not orders_csv.exists():
        return order_ids, tickers
    try:
        with open(orders_csv, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                # csv.DictReader fills omitted TRAILING cells with None rather
                # than raising, so a row observed mid-write can look like a
                # zero-execution terminal outcome purely because its
                # cumulative_quantity has not been flushed yet -- and this
                # helper would then delete a candidate whose order really did
                # partially fill, letting the later run buy its full amount.
                # _orders_csv_fills drops such a tail row; here ANY row with a
                # missing field is refused, not just the last one, because this
                # collector only ever REMOVES prior allocation and so must be
                # strictly more conservative (Codex review: "Reject truncated
                # terminal rows before excluding candidates").
                if any(value is None for value in row.values()):
                    continue
                state = (row.get("state") or "").strip().lower()
                if state not in _TERMINAL_NON_FILL_STATES:
                    continue
                # An EXPLICIT numeric zero is required, not merely the absence
                # of a positive quantity. A malformed or missing cell means the
                # artifact does not establish whether the order executed, and
                # this collector only ever REMOVES prior allocation -- so an
                # unresolved row must stay unresolved rather than be read as a
                # definitive zero fill.
                if not _executed_quantity_is_explicit_zero(row):
                    continue
                oid = (row.get("order_id") or "").strip()
                if oid and oid != "-":
                    order_ids.add(oid)
                else:
                    ticker = (row.get("ticker") or "").strip().upper()
                    if ticker:
                        tickers.add(ticker)
    except (OSError, csv.Error, UnicodeDecodeError):
        # Unreadable means "no terminal evidence", never "everything is
        # terminal": this set only ever REMOVES prior allocation, so failing to
        # read it must not be able to drop a real prior buy.
        return set(), set()
    return order_ids, tickers


def _orders_csv_submitted_notionals(
    orders_csv: Path, *, skip_sink: Optional[List[Path]] = None
) -> Dict[str, float]:
    """Map ticker -> notional_usd for submitted-but-not-filled rows in an orders CSV.

    Used in the full-report fallback to count submitted-but-unconfirmed orders as
    prior allocation with an unknown reference price (the conservative path), matching
    what the buy_candidates path does for status='submitted' candidates with no fill.

    Pass ``skip_sink`` when ``orders_csv`` is a file the caller already knows
    exists, so a read/parse failure caught mid-rewrite is recorded rather
    than silently indistinguishable from a genuinely empty CSV (Codex
    review: "Include referenced CSV failures in scan completeness").
    """
    notionals: Dict[str, float] = {}
    if not orders_csv.exists():
        return notionals
    rows = []
    fieldnames = None
    had_csv_error = False
    try:
        with orders_csv.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames  # reads the header row
            if fieldnames is not None:
                try:
                    for row in reader:
                        rows.append(row)
                except csv.Error:
                    had_csv_error = True
                    logger.exception(
                        "failed to parse %s for submitted-order context; prior allocations "
                        "from this file may be undercounted -- reconcile against broker orders",
                        orders_csv,
                    )
                    if skip_sink is not None:
                        skip_sink.append(orders_csv)
    except (OSError, UnicodeDecodeError):
        logger.exception(
            "failed to parse %s for submitted-order context; prior allocations "
            "from this file may be undercounted -- reconcile against broker orders",
            orders_csv,
        )
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        return notionals
    except Exception:
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        return notionals
    if fieldnames is None:
        # See the matching check in _orders_csv_fills: a file with no
        # header line at all was observed before even the header was
        # flushed, distinct from a header-only file (a legitimate
        # zero-orders day) that `if rows` alone can't tell apart (Codex
        # review: "Treat empty CSV snapshots as incomplete").
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        return notionals
    if not had_csv_error and rows and any(value is None for value in rows[-1].values()):
        # See the matching check in _orders_csv_fills: a truncated final row
        # (fewer fields than the header) never raises, so this is the only
        # way to catch a file observed mid-write (Codex review: "Detect
        # silently truncated CSV reads").
        if skip_sink is not None:
            skip_sink.append(orders_csv)
        rows = rows[:-1]
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        row_status = (row.get("status") or "").strip().lower()
        row_state_raw = (
            (row.get("state") or "").strip().lower() if "state" in row else None
        )
        # A broker-native recovery CSV may omit `status` entirely and report the
        # outcome only in `state` -- a shape _orders_csv_fills already treats as
        # authoritative. Requiring status=='submitted' dropped such a row even
        # when its state was still LIVE ('pending', 'queued'), so a candidate-
        # less recovery contributed no prior allocation at all and a later run
        # could submit the full amount while that order was outstanding (Codex
        # review: "Retain statusless nonterminal recovery rows").
        #
        # 'filled' is excluded because _orders_csv_fills owns that row and
        # reports a real price; this reader only carries UNRESOLVED context.
        statusless_live = (
            not row_status
            and row_state_raw is not None
            and row_state_raw != "filled"
            and row_state_raw not in _TERMINAL_NON_FILL_STATES
        )
        if row_status != "submitted" and not statusless_live:
            continue
        # The recovery shape's authoritative `state` column, applied with the
        # opposite polarity to _orders_csv_fills. A row can carry BOTH
        # status='submitted' (what we asked the broker for) and a broker outcome
        # in `state`; the broker's outcome outranks our request. Without this a
        # state='rejected' row still contributed its notional as unknown-price
        # prior allocation, pushing that ticker onto the conservative $0
        # manual-review path for an order known not to have filled (Codex
        # review: "Apply the recovery state gate to submitted notionals").
        #
        # Only TERMINAL NON-FILL states are dropped -- deliberately not
        # `state != 'filled'`. This function's whole job is to count a submitted
        # order whose outcome is not yet known, so discarding a pending/queued
        # row would UNDERCOUNT prior allocation and risk re-buying a ticker
        # whose order is still live. Fail-closed here means keeping the
        # unknown, not dropping it.
        #
        # And a terminal row that EXECUTED some quantity before ending is kept
        # too: cancelled-after-partial-fill is real deployed capital, which
        # scripts/broker_reconciliation.py already counts as prior exposure for
        # exactly this reason (Codex review: "Preserve executed partial fills in
        # recovery CSVs").
        # An EXPLICIT numeric zero is required to drop the row. `not
        # _has_executed_quantity(row)` also matches a missing column and an
        # unparsable cell, and those say UNKNOWN, not "nothing filled" --
        # dropping an unresolved terminal row removes its notional entirely, so
        # a later run sees no prior allocation and can buy the full amount again
        # (Codex review: "Require explicit zero before dropping submitted
        # notionals"). My earlier note claiming this reader RETAINED such a row
        # was wrong: this gate drops it.
        if (
            row.get("state") or ""
        ).strip().lower() in _TERMINAL_NON_FILL_STATES and _executed_quantity_is_explicit_zero(
            row
        ):
            continue
        # The broker-native recovery shape records the amount as `amount_usd`,
        # which _orders_csv_fills already accepts. Reading only `notional_usd`
        # meant a statusless live recovery row passed the gate above and then
        # contributed NOTHING, so its ticker carried no prior allocation and a
        # later run could submit the full amount while that order was still
        # outstanding (Codex review: "Read amount_usd for unresolved recovery
        # submissions"). Canonical column first, so an artifact carrying both
        # wins on the codebase-standard name.
        raw = _first_present(row, _SUBMITTED_NOTIONAL_COLS)
        try:
            notional = float(raw) if raw not in (None, "", "-") else None
        except (TypeError, ValueError):
            notional = None
        if notional and notional > 0:
            notionals[ticker] = notionals.get(ticker, 0.0) + notional
    return notionals


def _mtime_matches_trading_day(path: Path, trading_day: date) -> bool:
    """True if ``path``'s last-modified time falls on ``trading_day`` (Eastern).

    Used to scope ``skip_sink`` to PLAUSIBLY same-day artifacts: a
    ``run_state*.json`` candidate that fails to read/parse can't have its own
    ``trading_day`` field inspected (that's exactly what failed), so a
    permanently corrupted historical artifact from months or years ago --
    accumulated anywhere under a recursively (``rglob``) scanned root -- would
    otherwise poison every future scan's skip_sink forever, permanently
    disabling the bounded-reload recovery this signal exists to enable (Codex
    review: "Exclude historical parse failures from reload completeness").
    mtime is an imperfect but universal, cheap proxy: a same-day artifact still
    being flushed necessarily has a same-day mtime, while old corrupted files
    do not. If the path can't even be stat'd, err toward still treating it as
    a possible same-day failure rather than silently dropping the signal.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    return datetime.fromtimestamp(mtime, tz=EASTERN).date() == trading_day


def _resolve_recorded_orders_csv(
    run_state: Dict[str, Any],
    run_state_path: Path,
    *,
    skip_sink: Optional[List[Path]] = None,
) -> Optional[Path]:
    """Resolve ``run_state.artifacts.orders_csv_path`` to a concrete path, or None.

    Shared by both readers of the recorded CSV so they cannot disagree about
    which file a recovery artifact points at. ``run_and_report`` writes custom
    names like ``dca_<label>_<day>_<stamp>_orders.csv``, which neither
    ``run_dir / "orders.csv"`` nor ``filled_orders.csv`` will find.

    A RECOVERY artifact's recorded path must resolve inside its own run root.
    A copied one can carry a stale ABSOLUTE path still naming the run it was
    copied from -- following it verbatim would load an unrelated CSV's fills as
    this pass's recovery fills and reduce or block the 3:30 buy (Codex review:
    "Constrain recovery fill paths to their run root"). Mirrors the containment
    ``scripts.run_classification._referenced_orders_csvs_within`` already
    applies for the watchdog collector.

    Contained against the enclosing RECOVERY ROOT, not the run_state's own
    directory: a recovery run may write ``artifacts/run_state.json`` beside a
    root-level ``filled_orders.csv`` and record it as ``../filled_orders.csv``,
    which is in-root and legitimate (Codex review: "Resolve recovery CSVs
    against the artifact root").

    Containment is scoped to recovery states: a manual full-report run
    legitimately records a custom-named CSV that ``run_and_report`` may have
    written elsewhere, and that path has always been followed.
    """
    artifacts_block = run_state.get("artifacts")
    if not isinstance(artifacts_block, dict):
        return None
    orders_csv_path = artifacts_block.get("orders_csv_path")
    if orders_csv_path is None:
        return None
    # The recovery classification schema does not constrain the nested
    # artifacts block, so this value can be any JSON shape. Path([]) raises
    # TypeError, and that exception escaped load_prior_same_day_allocations()
    # and aborted the WHOLE same-day context scan -- taking the afternoon run
    # with it -- rather than treating one artifact as unreadable (Codex review:
    # "Validate recorded CSV path types before constructing Path"). A malformed
    # value is a broken artifact, not an absent one, so it goes to skip_sink:
    # the run's fills are exactly as unverifiable as an unparseable
    # run_state.json, and the scan must not report itself complete.
    if not isinstance(orders_csv_path, (str, os.PathLike)):
        logger.warning(
            "run_state %s records a non-path orders_csv_path (%r); treating the "
            "artifact as unreadable",
            run_state_path,
            orders_csv_path,
        )
        if skip_sink is not None:
            skip_sink.append(run_state_path)
        return None
    if isinstance(orders_csv_path, str) and not orders_csv_path.strip():
        # Absent rather than malformed: nothing was recorded to follow.
        return None
    orders_csv_file = Path(orders_csv_path)
    if not orders_csv_file.is_absolute():
        orders_csv_file = run_state_path.parent / orders_csv_file
    if _is_scheduled_recovery_context(run_state) and not _path_within(
        orders_csv_file, _recovery_root_for(run_state_path.parent)
    ):
        # Fall back to the SAME basename inside the run's own directory rather
        # than giving up. Rejecting the external path outright turned a wrong
        # allocation into NO allocation, which has the same money outcome: a
        # copied recovery state with a stale absolute path plus a valid local
        # filled_orders.csv contributed nothing from either loader, so the
        # ticker got bought again (Codex review: "Fall back to the recovery
        # run's local CSV"). Mirrors the basename fallback
        # scripts.run_classification._referenced_orders_csvs already applies.
        logger.warning(
            "recovery orders_csv_path %s is outside its run directory %s; "
            "falling back to the local file of the same name",
            orders_csv_file,
            run_state_path.parent,
        )
        orders_csv_file = run_state_path.parent / orders_csv_file.name
    return orders_csv_file


def load_prior_same_day_allocations(
    state_root: str | Path,
    trading_day: date,
    *,
    boundary_et: time = MORNING_MANUAL_BOUNDARY_ET,
    additional_state_roots: Sequence[str | Path] = (),
    include_scheduled_afternoon: bool = False,
    skip_sink: Optional[List[Path]] = None,
) -> Dict[str, PriorSameDayAllocation]:
    """Aggregate earlier same-day allocation context for the scheduled afternoon guard.

    Scans ``state_root`` (and any ``additional_state_roots``) recursively for
    ``run_state*.json`` artifacts written earlier the same ``trading_day`` by
    morning/midday/manual runs (including manual market-open recovery, manual
    override reruns under ``.../manual_reruns/``, and the morning live stream),
    and returns a per-ticker :class:`PriorSameDayAllocation`. These artifacts
    are read as prior-allocation context ONLY -- they are deliberately never
    surfaced as scheduled evidence, slippage, watchdog confirmation, scheduled
    cost basis, or scheduled fill counts.

    Eligibility mirrors the 2:30 PM ET boundary: only runs generated before
    ``boundary_et`` on ``trading_day`` are read, scheduled *afternoon* runs are
    ignored (morning/midday-stream runs stay eligible despite running on a
    schedule; see ``_prior_run_is_scheduled``), and only buy candidates whose
    status shows an order was placed contribute. Each contributing candidate's
    reference price is resolved only after capital movement is confirmed, by policy
    preference -- logged ``target_price``, then ``decision_price``, then the confirmed
    ``average_fill_price`` (from ``orders.csv`` or a manual override's
    ``filled_orders.csv``). An unconfirmed submitted candidate is retained as prior
    allocation context with an unknown reference even if it logged target/decision
    prices. When multiple earlier runs touched a ticker the dollar
    amounts are summed and the ticker's *most recent* valid same-day reference price
    wins (by ``generated_at``; ties and untimestamped runs fall back to later scan
    order), so the afternoon run must beat that ticker's latest earlier entry price
    before it tops up. If any contributing reference is unknown the aggregate
    reference is unknown, routing the guard to its conservative missing-price /
    manual_review $0 path.

    ``include_scheduled_afternoon`` defaults to False, matching the normal
    scheduled afternoon run's own guard invocation (which must never see another
    afternoon-scheduled run as prior context). Set it True only for a distinct,
    later same-day invocation -- e.g. a final manual buy override -- that needs
    to see the day's already-completed scheduled run so it doesn't double-buy;
    pass a ``boundary_et`` later than that run's timestamp to include it.

    ``skip_sink``, when given, has the path of every ``run_state*.json``
    candidate that could not be read or parsed appended to it (a file still
    being flushed by a sibling stream's writer, or a transient read race).
    Such a file contributes NO ticker to the returned mapping, so a caller
    that only inspects the mapping for tickers with a known amount but an
    unresolved reference price can miss this case entirely -- a skipped
    file may be the ONLY same-day artifact for some ticker, leaving that
    ticker absent rather than merely unresolved (Codex review: "Detect
    artifacts skipped during the initial context scan"). Callers that need
    to distinguish "no prior run" from "prior run present but its artifact
    could not be scanned this pass" (e.g. run_dca's bounded same-day
    context reload) should pass a list here and treat any entries in it as
    reason enough to reload, independent of what the returned mapping shows.
    """
    # Deduplicate roots by resolved path so an additional_state_root that matches
    # state_root (or another additional root) doesn't cause double-counting.
    _seen_roots: set = set()
    all_roots = []
    for _r in [Path(state_root), *[Path(r) for r in additional_state_roots]]:
        _key = _r.resolve()
        if _key not in _seen_roots:
            _seen_roots.add(_key)
            all_roots.append(_r)

    aggregated: Dict[str, Dict[str, Any]] = {}
    trading_day_iso = trading_day.isoformat()
    all_run_state_paths = sorted(
        set(
            p
            for root in all_roots
            if root.exists()
            for p in root.rglob("run_state*.json")
        )
    )

    # Parse every candidate artifact once, then process them in CHRONOLOGICAL
    # (parsed generated_at) order rather than lexical path order (Codex review:
    # "Sort first-allocation records by timestamp"): differently-named subtrees
    # do not sort chronologically by path alone -- e.g. "manual_recovery/1100"
    # sorts before "manual_runs/0900" even though 09:00 happened first -- which
    # would lock the day's first-allocation base from the wrong run. A
    # timestamp-less artifact falls back to (stable) path-scan order and sorts
    # after every timestamped one, since its true position can't be established.
    _parsed_runs: List[Tuple[Tuple[int, Any], Path, Dict[str, Any]]] = []
    for _order, run_state_path in enumerate(all_run_state_paths):
        try:
            raw_text = run_state_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            # Transient read failure (e.g. the file is still being flushed by
            # a sibling stream's writer) -- surfaced via skip_sink rather than
            # silently dropped, since a caller may need to know this scan was
            # incomplete even though no ticker in the returned mapping reflects it.
            # Scoped to plausibly same-day artifacts (by mtime) so a permanently
            # corrupted HISTORICAL file elsewhere in the recursively scanned
            # root doesn't poison every future scan's completeness signal forever.
            if skip_sink is not None and _mtime_matches_trading_day(
                run_state_path, trading_day
            ):
                skip_sink.append(run_state_path)
            continue

        # Fast path (restored -- see .jules/bolt.md 2025-01-20: rglob +
        # json.loads over a large historical artifact corpus is expensive,
        # so a raw substring check before parsing is worth keeping): skip
        # the parse ONLY for files whose mtime is NOT on trading_day. Such a
        # file cannot count toward skip_sink either way (it's scoped to
        # plausibly same-day artifacts, same as the two skip_sink call
        # sites below), so skipping its parse produces the exact same
        # observable outcome as parsing it and then discarding a would-be
        # failure. A file modified TODAY always gets a full parse regardless
        # of the substring match -- a "looks like a complete JSON object"
        # heuristic (e.g. ends with "}") isn't a reliable enough proxy for
        # real completeness to gate this on for same-day files (a sorted-key
        # writer can truncate mid-flush at a NESTED object's closing brace
        # while the outer object -- and "trading_day" -- is never written),
        # so same-day files skip the substring shortcut entirely and always
        # reach the parse-failure handler below that records skip_sink
        # (Codex review: "Parse JSON before declaring a brace-terminated
        # prefix complete"; Copilot review: restore the fast path safely).
        # Only trust the substring match when the file contains no JSON
        # unicode escape sequences -- a spec-valid encoder may spell an
        # ASCII character such as one of the date's hyphens as a unicode
        # escape, which would not literal-match trading_day_iso even though
        # the parsed value is identical.
        same_day_mtime = _mtime_matches_trading_day(run_state_path, trading_day)
        if (
            not same_day_mtime
            and trading_day_iso not in raw_text
            and "\\u" not in raw_text
        ):
            continue

        try:
            run_state = json.loads(raw_text)
        except ValueError:
            # Partial JSON mid-flush is the other half of the parse-race this
            # loader must not silently absorb -- same skip_sink signal (and
            # same same-day scoping) as the read failure above.
            if skip_sink is not None and same_day_mtime:
                skip_sink.append(run_state_path)
            continue

        if not isinstance(run_state, dict):
            continue
        if run_state.get("trading_day") != trading_day_iso:
            continue

        run_ts = _prior_run_timestamp(run_state.get("generated_at"))
        sort_key = (0, run_ts) if run_ts is not None else (1, _order)
        _parsed_runs.append((sort_key, run_state_path, run_state))

    _parsed_runs.sort(key=lambda item: item[0])

    for _, run_state_path, run_state in _parsed_runs:
        if _prior_run_is_scheduled(run_state) and not include_scheduled_afternoon:
            continue
        if not _prior_run_before_boundary(run_state, boundary_et):
            continue
        if not _prior_run_is_valid_manual_recovery_artifact(run_state, run_state_path):
            continue

        classification = run_state.get("run_classification")
        run_id = None
        source = None
        if isinstance(classification, dict):
            run_id = classification.get("run_id")
            source = classification.get("run_origin")
        run_id = run_id or run_state.get("run_id")
        source = source or "manual"
        # Recency key for most-recent reference resolution. Untimestamped runs use
        # the floor so any timestamped run outranks them; equal keys defer to later
        # scan order (all_run_state_paths is sorted ascending).
        run_ts = _prior_run_timestamp(run_state.get("generated_at")) or _TS_FLOOR

        fills = _orders_csv_fills(run_state_path.parent)
        # A full-report run_state written by run_and_report()/write_run_state()
        # stores artifact paths under "artifacts" and "summary_metrics" rather
        # than a top-level "buy_candidates" array.  When buy_candidates is absent
        # but an orders_csv_path is recorded, reconstruct prior allocations from
        # that CSV directly (filled rows only).
        # Full-report dry-run artifacts must never contribute: run_and_report
        # dry runs write filled DryRunBroker rows with no real capital deployed.
        if run_state.get("dry_run"):
            continue
        buy_candidates_raw = run_state.get("buy_candidates")
        # Persisted JSON can hold any shape here. A scalar such as 1 raised
        # TypeError out of the whole final-run context scan at the iteration
        # below, and a truthy non-list (a populated dict) was silently treated
        # as "this run's candidates were read" -- skipping the orders-CSV
        # fallback, so the run's real fills went unseen and its ticker could be
        # bought again at 3:30. Normalizing to [] routes both cases into the CSV
        # fallback, which is the fail-closed direction (Codex review: "Reject
        # malformed candidate collections in recovery states").
        if not isinstance(buy_candidates_raw, list):
            buy_candidates_raw = []
        # A recovery run may write artifacts/run_state.json beside a ROOT-level
        # filled_orders.csv, so the fills lookup beside the state found nothing.
        # Such a state can ALSO carry non-empty buy_candidates, in which case
        # the recorded orders_csv_path branch below is skipped and the fallback
        # loader treats the run as already covered -- an authoritative partial
        # fill was therefore replaced by the candidate's full planned amount at
        # an unknown price, suppressing the remaining top-up (Codex review:
        # "Read recovery fill CSVs even when candidates are embedded").
        #
        # Scoped to CANDIDATE-BACKED states because that is the only shape that
        # falls through: with no candidates the branch below already reads the
        # recorded path, and ``fills`` is never consulted there since the
        # candidate loop iterates nothing. Gated on ``not fills`` so it can only
        # ADD fills where the state's own directory had none, never
        # double-count.
        #
        # The recorded path is tried FIRST and the fixed-name scan second:
        # _orders_csv_fills() without an explicit file knows only
        # filled_orders.csv and orders.csv, so a recovery recording an in-root
        # CUSTOM name (artifacts.orders_csv_path='../executions.csv') was still
        # missed by the root retry alone -- the same suppressed top-up, one
        # filename away (Codex review: "Follow the recorded CSV for
        # candidate-backed recoveries").
        terminal_order_ids: set = set()
        terminal_tickers: set = set()
        if buy_candidates_raw and _is_scheduled_recovery_context(run_state):
            recorded_csv = _resolve_recorded_orders_csv(
                run_state, run_state_path, skip_sink=skip_sink
            )
            # MERGED per ticker, not gated on `fills` being empty. A single
            # fill beside run_state.json made the whole recorded CSV invisible,
            # so any OTHER ticker it confirmed fell back to its candidate's full
            # planned amount -- a $3 partial of a $10 candidate counted as $10,
            # suppressing the remaining top-up (Codex review: "Merge recorded
            # recovery fills even when sibling fills exist"). setdefault means a
            # later source can only ADD tickers, never overwrite what the
            # state's own directory already answered, so the path that already
            # worked is untouched.
            if recorded_csv is not None and recorded_csv.exists():
                for _t, _f in _orders_csv_fills(
                    recorded_csv.parent,
                    orders_csv_file=recorded_csv,
                    skip_sink=skip_sink,
                ).items():
                    _merge_fill(fills, _t, _f)
            recovery_root = _recovery_root_for(run_state_path.parent)
            if recovery_root != run_state_path.parent:
                for _t, _f in _orders_csv_fills(recovery_root).items():
                    _merge_fill(fills, _t, _f)
            # The same CSV also carries TERMINAL non-fill outcomes, and dropping
            # them left the candidate loop below holding the full planned amount
            # with an unknown reference price -- routing a ticker whose order is
            # KNOWN rejected to the $0 manual-review guard and suppressing a
            # legitimate top-up. The no-candidate path already honors this via
            # _orders_csv_submitted_notionals (Codex review: "Honor terminal
            # rejections for candidate-backed recoveries").
            for _csv in (
                recorded_csv if recorded_csv is not None else None,
                recovery_root / "filled_orders.csv",
                recovery_root / "orders.csv",
                run_state_path.parent / "filled_orders.csv",
                run_state_path.parent / "orders.csv",
            ):
                if _csv is None:
                    continue
                _oids, _tks = _orders_csv_terminal_non_fills(_csv)
                terminal_order_ids |= _oids
                terminal_tickers |= _tks
        if not buy_candidates_raw:
            orders_csv_file = _resolve_recorded_orders_csv(
                run_state, run_state_path, skip_sink=skip_sink
            )
            if orders_csv_file is not None:
                if orders_csv_file.exists():
                    # Pass the exact recorded file rather than run_dir/"orders.csv"
                    # because run_and_report writes custom-named CSVs.
                    report_fills = _orders_csv_fills(
                        orders_csv_file.parent,
                        orders_csv_file=orders_csv_file,
                        skip_sink=skip_sink,
                    )
                    submitted_notionals = _orders_csv_submitted_notionals(
                        orders_csv_file, skip_sink=skip_sink
                    )
                    # Merge filled rows (known price) then submitted rows (unknown price).
                    # Filled rows for a ticker win over submitted rows for the same ticker.
                    _csv_tickers: Dict[str, tuple] = {}  # ticker -> (amount, ref_price)
                    for _ticker, _fill in report_fills.items():
                        # None keeps the reference UNRESOLVED for a partial, the
                        # same rule the candidate loop applies: the executed
                        # notional counts, but the price does not settle until
                        # the order fully fills or is terminally reconciled.
                        _ref = (
                            None
                            if _fill.get("_partial_execution")
                            else _fill["avg_fill_price"]
                        )
                        if _fill.get("amount_usd"):
                            _csv_tickers[_ticker] = (_fill["amount_usd"], _ref)
                        elif _fill["quantity_filled"]:
                            _csv_tickers[_ticker] = (
                                _fill["avg_fill_price"] * _fill["quantity_filled"],
                                _ref,
                            )
                    for _ticker, _notional in submitted_notionals.items():
                        if _ticker not in _csv_tickers:
                            _csv_tickers[_ticker] = (_notional, None)
                    for _ticker, (_amount, _ref) in _csv_tickers.items():
                        if _amount <= 0:
                            continue
                        _entry = aggregated.setdefault(
                            _ticker,
                            {
                                "amount": 0.0,
                                "reference_price": None,
                                "reference_ts": None,
                                "run_id": run_id,
                                "source": source,
                                "selected_base_amount_usd": None,
                                "selected_base_amount_usd_at": None,
                                "selected_base_amount_usd_seen": False,
                            },
                        )
                        _entry["amount"] += _amount
                        if _entry.get("reference_price_unknown"):
                            pass
                        elif _ref is None:
                            _entry["reference_price"] = None
                            _entry["reference_ts"] = None
                            _entry["reference_price_unknown"] = True
                        else:
                            if (
                                _entry["reference_price"] is None
                                or run_ts >= _entry["reference_ts"]
                            ):
                                _entry["reference_price"] = _ref
                                _entry["reference_ts"] = run_ts
                        # This CSV-reconstructed shape carries no base info -- if
                        # this is the ticker's first contributing entry, lock the
                        # base permanently unknown (Codex review: "Do not infer a
                        # first-allocation base from a later run") rather than
                        # leaving it open for a later run's own base to fill in.
                        if not _entry["selected_base_amount_usd_seen"]:
                            _entry["selected_base_amount_usd_at"] = run_state.get(
                                "generated_at"
                            )
                            _entry["selected_base_amount_usd_seen"] = True
                        _entry["run_id"] = run_id
                        _entry["source"] = source
                elif skip_sink is not None:
                    # This full-report run_state RECORDED orders_csv_path
                    # as its fill source, but the file isn't there at
                    # that exact path -- deleted, relocated, or still
                    # being written by run_and_report. Its referenced
                    # fills are exactly as unverifiable as a
                    # run_state.json that itself failed to read/parse,
                    # so this scan must not report itself complete while
                    # a ticker's only fill evidence is silently invisible
                    # (Codex review: "Include referenced CSV failures in
                    # scan completeness"). BUT when a supported sibling
                    # sits right beside this run_state (e.g. the recorded
                    # path is a STALE absolute path from a relocated
                    # deployment, while the real CSV -- under its
                    # recorded basename, OR a differently-named file like
                    # submitted_orders.csv/filled_orders.csv -- still
                    # lives in this run's own directory),
                    # scripts.run_classification.load_same_day_top_up_context's
                    # own basename/glob fallback (_referenced_orders_csvs,
                    # which matches its recorded basename AND any sibling
                    # matching "*orders.csv") already recovers this run's
                    # fills unconditionally, within the SAME
                    # _compute_auto_derived_prior_allocations call that
                    # merges its result in right after this loop.
                    # Flagging a failure here anyway would be a permanent
                    # false positive -- a stale absolute path never
                    # becomes valid, so scan_had_failures would never
                    # clear, poisoning an already-correctly-recovered,
                    # resolved allocation on every future scan (Codex
                    # review: "Do not flag stale CSV paths after fallback
                    # recovery"; "Recognize every supported sibling
                    # fallback before flagging failure" -- the exact-
                    # basename-only check missed the glob fallback).
                    # Only flag the failure when NEITHER supported
                    # fallback has anything to find.
                    _has_basename_sibling = (
                        run_state_path.parent / orders_csv_file.name
                    ).exists()
                    _has_glob_sibling = any(run_state_path.parent.glob("*orders.csv"))
                    if not _has_basename_sibling and not _has_glob_sibling:
                        skip_sink.append(run_state_path)
        for candidate in buy_candidates_raw or []:
            if not isinstance(candidate, dict):
                continue
            status = candidate.get("status") or ""
            if status not in _PRIOR_ALLOCATION_STATUSES:
                continue
            ticker = candidate.get("ticker")
            if not ticker:
                continue

            # The recovery CSV said this order ended REJECTED/CANCELED having
            # executed nothing, which outranks the candidate's submission-time
            # status: no capital moved, so counting its planned amount as prior
            # allocation forces the $0 manual-review guard and suppresses a
            # legitimate top-up (Codex review: "Honor terminal rejections for
            # candidate-backed recoveries"). Ticker-level evidence applies only
            # when the row carried no order_id AND nothing confirms a fill for
            # that ticker, since one ticker can have both a rejected order and a
            # separate real fill.
            _cand_oid = (candidate.get("order_id") or "").strip()
            if _cand_oid and _cand_oid in terminal_order_ids:
                continue
            if (
                not _cand_oid
                and ticker in terminal_tickers
                and fills.get(ticker) is None
            ):
                continue

            # A "submitted" candidate in run_state.json only means an order_id was
            # obtained at submission time, not that it filled. When orders.csv
            # confirms a fill, use the actual filled notional/price. When
            # orders.csv exists but doesn't confirm a fill for this ticker (still
            # "submitted", rejected, canceled, or never refreshed), we can't tell
            # whether real capital moved -- dropping the ticker outright would
            # treat an unrefreshed-but-actually-filled order as "no_prior_run"
            # and risk a double-buy, so instead keep the known planned amount but
            # with no reference price (see below), which routes the guard to its
            # conservative manual-review $0 path rather than a blind top-up.
            fill = fills.get(ticker)
            amount = candidate.get("incremental_order_amount")
            if amount is None:
                amount = candidate.get("planned_amount_usd")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                continue
            if fill is not None and fill.get("amount_usd"):
                # Prefer the actual filled notional over the planned amount so a
                # manual override or partial fill doesn't overstate prior allocation.
                amount = fill["amount_usd"]
            elif (
                fill is not None
                and fill["quantity_filled"]
                and fill.get("avg_fill_price") is not None
            ):
                # Prefer the actual filled notional over the planned amount so a
                # partial fill doesn't overstate prior allocation. Requires a
                # confirmed price -- a quantity-only fill with no usable price
                # carries no way to derive a dollar amount and must not be
                # multiplied against a missing (None) price.
                amount = fill["avg_fill_price"] * fill["quantity_filled"]
            if amount <= 0:
                continue

            # Reference price preference (same-day top-up guard policy) applies
            # only after capital movement is confirmed. A still-unfilled
            # ``submitted`` row may later be rejected/canceled or may simply have
            # stale order history; using its logged target/decision price as a
            # known reference would let later top-ups silently take
            # price_not_below/no_incremental. Keep that path unknown so the guard
            # routes to missing_prior_price -> $0 + manual review.
            reference_price = None
            base_amount_usd = None
            fill_is_partial = bool(fill and fill.get("_partial_execution"))
            if (status == "filled" or fill is not None) and not fill_is_partial:
                reference_price = _candidate_logged_reference_price(candidate)
                if reference_price is None and fill is not None:
                    reference_price = fill["avg_fill_price"]
                # Same confirmed-capital-movement gate as reference_price: an
                # unconfirmed submitted candidate's base is not trusted as the
                # day's fixed first-allocation base either.
                try:
                    base_amount_usd = float(candidate.get("selected_base_amount_usd"))
                except (TypeError, ValueError):
                    base_amount_usd = None
                if base_amount_usd is not None and not (
                    math.isfinite(base_amount_usd) and base_amount_usd > 0
                ):
                    base_amount_usd = None

            entry = aggregated.setdefault(
                ticker,
                {
                    "amount": 0.0,
                    "reference_price": None,
                    "reference_ts": None,
                    "run_id": run_id,
                    "source": source,
                    "selected_base_amount_usd": None,
                    "selected_base_amount_usd_at": None,
                    "selected_base_amount_usd_seen": False,
                },
            )
            entry["amount"] += amount
            # If ANY contributing prior price is unknown, the whole aggregated
            # reference is treated as unknown so the guard takes the conservative
            # missing_prior_price path rather than comparing against only the
            # known portion while silently ignoring the unknown portion.
            if entry.get("reference_price_unknown"):
                pass  # already poisoned; keep None
            elif reference_price is None:
                entry["reference_price"] = None
                entry["reference_ts"] = None
                entry["reference_price_unknown"] = True
            else:
                # Most-recent reference wins: overwrite when this run is at least as
                # recent as the run that currently owns the reference (equal keys and
                # untimestamped runs defer to later scan order, which is more recent).
                if entry["reference_price"] is None or run_ts >= entry["reference_ts"]:
                    entry["reference_price"] = reference_price
                    entry["reference_ts"] = run_ts
            # Only the ticker's FIRST contributing entry sets the base (_parsed_runs
            # is processed in chronological, parsed-generated_at order -- see above)
            # -- gated on a "seen" flag, not on the stored value being None, so a
            # first allocation with an unresolved/untrusted base permanently locks
            # the base unknown rather than letting a later run's own (possibly
            # requalified) base fill it in (Codex review: "Do not infer a
            # first-allocation base from a later run"). See
            # resolve_top_up_desired_amount_usd for how the guard consumes this.
            if not entry["selected_base_amount_usd_seen"]:
                entry["selected_base_amount_usd"] = base_amount_usd
                entry["selected_base_amount_usd_at"] = run_state.get("generated_at")
                entry["selected_base_amount_usd_seen"] = True
            # Keep the most recently scanned (chronologically last) run's identity
            # for auditability.
            entry["run_id"] = run_id
            entry["source"] = source

    return {
        ticker: PriorSameDayAllocation(
            ticker=ticker,
            amount_usd=round(entry["amount"], 2),
            reference_price=entry["reference_price"],
            run_id=entry["run_id"],
            source=entry["source"],
            reference_ts=entry["reference_ts"],
            selected_base_amount_usd=entry.get("selected_base_amount_usd"),
            selected_base_amount_usd_at=entry.get("selected_base_amount_usd_at"),
        )
        for ticker, entry in aggregated.items()
    }


def load_prior_same_day_allocations_multi(
    state_roots: Sequence[str | Path],
    trading_day: date,
    *,
    boundary_et: time = MORNING_MANUAL_BOUNDARY_ET,
    include_scheduled_afternoon: bool = False,
    skip_sink: Optional[List[Path]] = None,
) -> Dict[str, PriorSameDayAllocation]:
    """Merge :func:`load_prior_same_day_allocations` across several state roots.

    The afternoon guard folds in earlier same-day fills that live under sibling
    stream roots -- the morning-live root, the midday-live root, and the manual
    override reruns under ``.../manual_reruns/`` -- as well as its own task root.
    Per ticker the dollar amounts are summed and the ticker's *most recent* known
    reference price wins (by ``generated_at``; any unknown reference poisons the
    aggregate to unknown), so the afternoon run must beat that ticker's latest
    earlier entry across every stream before it tops up. Roots that do not exist
    contribute nothing, so passing production stream roots is safe on days a stream
    did not run.

    ``include_scheduled_afternoon`` is forwarded to each per-root call; see
    :func:`load_prior_same_day_allocations` for when to set it True.

    ``skip_sink`` is forwarded to each per-root call and accumulates skipped
    artifact paths across every root; see :func:`load_prior_same_day_allocations`.
    """
    merged: Dict[str, PriorSameDayAllocation] = {}
    for root in state_roots:
        allocations = load_prior_same_day_allocations(
            root,
            trading_day,
            boundary_et=boundary_et,
            include_scheduled_afternoon=include_scheduled_afternoon,
            skip_sink=skip_sink,
        )
        for ticker, alloc in allocations.items():
            existing = merged.get(ticker)
            if existing is None:
                merged[ticker] = alloc
                continue
            if existing.reference_price is None or alloc.reference_price is None:
                ref = None
                ref_ts = None
                ref_run_id = alloc.run_id or existing.run_id
                ref_source = alloc.source or existing.source
            else:
                # Most-recent reference wins across roots. Untimestamped allocations
                # use the floor so any timestamped one outranks them; equal keys defer
                # to the newly folded root (state_roots order = later contributor).
                existing_ts = existing.reference_ts or _TS_FLOOR
                alloc_ts = alloc.reference_ts or _TS_FLOOR
                if alloc_ts >= existing_ts:
                    ref = alloc.reference_price
                    ref_ts = alloc.reference_ts
                    # The run/source that actually supplied the winning reference
                    # price must be recorded together with it, not whichever side
                    # happens to be scanned last -- otherwise a top-up audit trail
                    # can pair a newer reference price with an older run_id.
                    ref_run_id = alloc.run_id or existing.run_id
                    ref_source = alloc.source or existing.source
                else:
                    ref = existing.reference_price
                    ref_ts = existing.reference_ts
                    ref_run_id = existing.run_id or alloc.run_id
                    ref_source = existing.source or alloc.source
            # Pick the chronologically FIRST allocation's base -- state_roots is
            # not guaranteed to be scanned in true chronological order (Codex
            # review: "Select the base from the chronologically first
            # allocation"), so compare each side's own selected_base_amount_usd_at
            # rather than assuming whichever root came first in the caller's list.
            base, base_at = _earlier_selected_base(
                existing.selected_base_amount_usd,
                existing.selected_base_amount_usd_at,
                alloc.selected_base_amount_usd,
                alloc.selected_base_amount_usd_at,
            )
            merged[ticker] = PriorSameDayAllocation(
                ticker=ticker,
                amount_usd=round(existing.amount_usd + alloc.amount_usd, 2),
                reference_price=ref,
                run_id=ref_run_id,
                source=ref_source,
                reference_ts=ref_ts,
                selected_base_amount_usd=base,
                selected_base_amount_usd_at=base_at,
            )
    return merged


def merge_manual_top_up_context(
    prior_same_day_allocations: Dict[str, PriorSameDayAllocation],
    manual_context: Dict[str, "PriorAllocation"],
) -> None:
    """Fold same-day MANUAL fills into a top-up guard's prior-allocation input.

    ``load_prior_same_day_allocations[_multi]`` reads a manual run's fills from its
    embedded ``buy_candidates`` or, absent those, a recorded
    ``artifacts.orders_csv_path`` -- it has no way to find a fill that lives ONLY in
    a sibling orders CSV the run_state never names (see
    ``scripts.run_classification.load_same_day_top_up_context``, a
    :class:`scripts.run_classification.PriorAllocation` map, which scans
    ``top_up_context_roots()`` and also recognizes a bare sibling ``*orders.csv``).
    Jul 21 2026 incident: the 3:20 final run correctly excluded the manual midday
    fills under ``midday_dca/manual_runs/20260721_1345ET`` from scheduled evidence,
    but never loaded them as prior-allocation context, leaving ``prior_usd`` blank
    and ``incremental_usd`` 0 for MSFT/GOOGL/GOOG/MA/IBM despite the fills being on
    disk. This merges the two loaders' results the same way ``load_prior_same_day_
    allocations_multi`` folds in multiple roots: dollar amounts sum, and the ticker's
    MOST RECENT valid reference wins by comparing each side's own recency (ET
    seconds-of-day, not just minutes -- Codex review: "Retain sub-minute
    generated-at ordering" -- two artifacts in the same ET minute must not tie;
    both loaders are already scoped to the same single trading_day, so a
    seconds-of-day comparison is sufficient) -- NOT simply the manual fill,
    which is merged last but is not necessarily more recent (Codex review:
    "Preserve the manual-context timestamp when merging references" -- a 09:00
    manual fill must not out-rank a 12:30 scheduled fill just because it is folded
    in after it). The run_id/source recorded travels with whichever side's
    reference price actually won (Codex review: "Keep the winning manual
    reference's identity"), not unconditionally the core loader's. An unknown
    reference from either source poisons the aggregate to unknown, routing the
    guard to its conservative missing-price path.

    Shared by both ``run_dca.py`` (the 3:30 afternoon guard) and
    ``morning_dca.py``'s ``MorningDCAExecutor._compute_top_up`` (the 10:30/12:30
    morning and midday guards -- Codex review: "Wire manual CSV context into the
    midday guard"): the policy documented in config/run_classification.json
    requires every same-day guard to fold these manual artifacts in, not just the
    afternoon one.

    These manual fills stay prior-allocation context ONLY -- merging them here never
    makes their order IDs scheduled evidence; ``collect_manual_order_ids`` still
    excludes them from every scheduled-live metric.
    """
    for ticker, alloc in manual_context.items():
        if alloc.prior_usd <= 0:
            continue
        existing = prior_same_day_allocations.get(ticker)
        if existing is None:
            prior_same_day_allocations[ticker] = PriorSameDayAllocation(
                ticker=ticker,
                amount_usd=alloc.prior_usd,
                reference_price=alloc.reference_price,
                run_id=(alloc.sources[0] if alloc.sources else None),
                source="manual_top_up_context",
            )
            continue
        if existing.reference_price is None or alloc.reference_price is None:
            # Either side being unknown still poisons the aggregate to unknown.
            ref = None
            ref_ts = None
            ref_run_id = existing.run_id
            ref_source = existing.source or "manual_top_up_context"
        else:
            # Compare actual recency (ET seconds-of-day, not just minutes --
            # Codex review: "Retain sub-minute generated-at ordering") rather
            # than always letting the manual fill win merely because it is
            # merged last. scripts.run_classification.PriorAllocation.reference_ts
            # is ET seconds-since-midnight; existing.reference_ts (this
            # module's own datetime) is reduced to the same seconds-of-day for
            # comparison, since both loaders are already scoped to the same
            # trading_day. Untimestamped on either side defers to the manual
            # fill (merged last), matching the "ties/untimestamped defer to
            # later scan/merge order" convention used elsewhere in this module.
            existing_seconds = (
                existing.reference_ts.hour * 3600
                + existing.reference_ts.minute * 60
                + existing.reference_ts.second
                if existing.reference_ts is not None
                else None
            )
            alloc_seconds = alloc.reference_ts
            if (
                existing_seconds is not None
                and alloc_seconds is not None
                and existing_seconds > alloc_seconds
            ):
                ref = existing.reference_price
                ref_ts = existing.reference_ts
                ref_run_id = existing.run_id
                ref_source = existing.source or "manual_top_up_context"
            else:
                ref = alloc.reference_price
                # The manual side's real datetime isn't available (only its ET
                # seconds-of-day), so this can't be represented exactly here.
                ref_ts = None
                # The manual context's own identity travels with its winning
                # reference price -- pairing a manual reference with the
                # scheduled run's identity would point reconciliation at the
                # wrong artifact (Codex review: "Keep the winning manual
                # reference's identity").
                ref_run_id = alloc.sources[0] if alloc.sources else None
                ref_source = "manual_top_up_context"
        # PriorAllocation (scripts.run_classification) carries no full datetime,
        # but its reference_ts (ET seconds-of-day, same trading_day as
        # `existing`) DOES let us compare against `existing.selected_base_amount_
        # usd_at` -- reduced to the same ET seconds-of-day -- to tell whether
        # this bare-CSV-only manual fill happened before or after the run that
        # established `existing`'s base (Codex review discussion_r3653976740:
        # "Preserve the known base when a later manual fill is merged"). Retain
        # `existing`'s base only when the manual fill is CONFIRMED later: an
        # earlier scheduled allocation's known base is still the true first
        # allocation's base regardless of a later manual top-up being merged in.
        # When the manual fill may be EARLIER (or either timestamp is unknown),
        # it could itself be the true first allocation, whose base a bare CSV
        # cannot tell us -- poison to unknown rather than risk a stale/wrong
        # base doubling the full-day target (Codex review: "Keep the base
        # unknown after merging an earlier manual fill") -- this mirrors
        # reference_price's own poison-on-uncertainty policy above.
        base = None
        base_at = None
        if (
            existing.selected_base_amount_usd is not None
            and alloc.reference_ts is not None
        ):
            existing_base_dt = _prior_run_timestamp(
                existing.selected_base_amount_usd_at
            )
            if existing_base_dt is not None:
                existing_base_seconds = (
                    existing_base_dt.hour * 3600
                    + existing_base_dt.minute * 60
                    + existing_base_dt.second
                )
                if alloc.reference_ts > existing_base_seconds:
                    base = existing.selected_base_amount_usd
                    base_at = existing.selected_base_amount_usd_at
        prior_same_day_allocations[ticker] = PriorSameDayAllocation(
            ticker=ticker,
            amount_usd=existing.amount_usd + alloc.prior_usd,
            reference_price=ref,
            run_id=ref_run_id,
            source=ref_source,
            reference_ts=ref_ts,
            selected_base_amount_usd=base,
            selected_base_amount_usd_at=base_at,
        )


# Mutually exclusive reasons an active-universe ticker did not produce a filled order.
MISSING_FILL_FAILED_TRIGGER = "failed DCA trigger"
MISSING_FILL_BUDGET_CAPPED = "budget capped"
MISSING_FILL_BROKER_BLOCKED = "broker/tradability/order review blocked"
MISSING_FILL_DATA_ERROR = "data error"
MISSING_FILL_EXECUTION_GAP = "execution gap"
# The scheduled afternoon buy was fully offset by an earlier same-day (morning/manual)
# allocation via the top-up guard; no incremental order was placed.
MISSING_FILL_TOP_UP_OFFSET = "top-up offset"

MISSING_FILL_REASONS = (
    MISSING_FILL_FAILED_TRIGGER,
    MISSING_FILL_BUDGET_CAPPED,
    MISSING_FILL_BROKER_BLOCKED,
    MISSING_FILL_DATA_ERROR,
    MISSING_FILL_EXECUTION_GAP,
    MISSING_FILL_TOP_UP_OFFSET,
)


@dataclass
class OrderExecutionRecord:
    """Durable per-order target and slippage log for one reviewed/submitted order."""

    ticker: str
    status: str  # submitted | filled | unfilled
    order_id: Optional[str] = None
    notional_usd: float = 0.0
    action_reason: str = ""
    target_price: float = 0.0
    target_price_source: str = ""
    target_quote_time: Optional[datetime] = None
    decision_bar_close: float = 0.0
    order_submitted_at: Optional[datetime] = None
    order_filled_at: Optional[datetime] = None
    avg_fill_price: Optional[float] = None
    quantity_filled: Optional[float] = None
    slippage_abs: Optional[float] = None
    slippage_pct: Optional[float] = None
    slippage_cost: Optional[float] = None
    latency_seconds: Optional[float] = None
    execution_quality: str = "unfilled"


def classify_execution_quality(slippage_pct: Optional[float]) -> str:
    """Bucket an order by absolute slippage percentage. None means no fill."""
    if slippage_pct is None:
        return "unfilled"
    magnitude = abs(slippage_pct)
    if magnitude < 0.10:
        return "excellent"
    if magnitude < 0.25:
        return "good"
    if magnitude < 0.50:
        return "acceptable"
    return "poor"


def build_execution_record(
    *,
    ticker: str,
    target_price: float,
    target_price_source: str,
    decision_bar_close: float,
    notional_usd: float,
    action_reason: str = "",
    order_id: Optional[str] = None,
    target_quote_time: Optional[datetime] = None,
    order_submitted_at: Optional[datetime] = None,
    order_filled_at: Optional[datetime] = None,
    avg_fill_price: Optional[float] = None,
    quantity_filled: Optional[float] = None,
) -> OrderExecutionRecord:
    """Compute slippage and latency for a submitted order against its target price.

    For a buy, positive slippage means the average fill was above the target price
    (unfavorable). slippage_cost is the extra dollars paid versus target across the
    filled quantity. Records without a fill are marked ``unfilled``.
    """
    filled = avg_fill_price is not None and quantity_filled is not None
    slippage_abs: Optional[float] = None
    slippage_pct: Optional[float] = None
    slippage_cost: Optional[float] = None
    latency_seconds: Optional[float] = None

    if filled:
        slippage_abs = avg_fill_price - target_price
        if target_price:
            slippage_pct = slippage_abs / target_price * 100.0
        slippage_cost = slippage_abs * quantity_filled
    if order_submitted_at is not None and order_filled_at is not None:
        latency_seconds = (order_filled_at - order_submitted_at).total_seconds()

    return OrderExecutionRecord(
        ticker=ticker,
        status="filled" if filled else "submitted",
        order_id=order_id,
        notional_usd=notional_usd,
        action_reason=action_reason,
        target_price=target_price,
        target_price_source=target_price_source,
        target_quote_time=target_quote_time,
        decision_bar_close=decision_bar_close,
        order_submitted_at=order_submitted_at,
        order_filled_at=order_filled_at,
        avg_fill_price=avg_fill_price,
        quantity_filled=quantity_filled,
        slippage_abs=slippage_abs,
        slippage_pct=slippage_pct,
        slippage_cost=slippage_cost,
        latency_seconds=latency_seconds,
        execution_quality=classify_execution_quality(slippage_pct),
    )


def _log_execution(
    run_result: "RunResult",
    decision: ReconciledDecision,
    order_id: str,
    broker: BrokerAdapter,
    *,
    notional_usd: Optional[float] = None,
) -> None:
    """Fetch fill details for a placed order and append a durable execution record.

    ``notional_usd`` overrides ``decision.suggested_amount_usd`` when the actual
    submitted amount differs (e.g. the top-up guard's incremental dollar amount or
    the kill-switch's mid-batch cap in ``run_once``), so the durable
    orders.csv/execution record matches what was really ordered.
    """
    get_fill = getattr(broker, "get_order_fill", None)
    fill: Optional[OrderFill] = None
    if callable(get_fill):
        res = get_fill(order_id)
        if isinstance(res, OrderFill):
            fill = res
    record = build_execution_record(
        ticker=decision.ticker,
        target_price=decision.target_price,
        target_price_source=decision.target_price_source,
        target_quote_time=decision.target_quote_time,
        decision_bar_close=decision.decision_bar_close,
        notional_usd=(
            notional_usd if notional_usd is not None else decision.suggested_amount_usd
        ),
        action_reason=decision.reason,
        order_id=order_id,
        order_submitted_at=fill.order_submitted_at if fill else None,
        order_filled_at=fill.order_filled_at if fill else None,
        avg_fill_price=fill.avg_fill_price if fill else None,
        quantity_filled=fill.quantity_filled if fill else None,
    )
    run_result.execution_records.append(record)
    # A submitted order that never returned a fill is an execution gap, not a
    # trigger/budget/broker failure, so classify it explicitly for coverage.
    if record.status != "filled":
        run_result.loop_block_reasons[decision.ticker] = MISSING_FILL_EXECUTION_GAP


def classify_missing_fill(
    decision: Optional[ReconciledDecision], loop_reason: Optional[str]
) -> str:
    """Classify why an active-universe ticker produced no filled order.

    ``loop_reason`` is the outcome recorded during the order loop (cap, broker, gap),
    which takes precedence. Otherwise the reconciled action determines the category.
    """
    if loop_reason:
        return loop_reason
    action = decision.action if decision else Action.NO_DATA
    if action == Action.NO_DATA or (
        decision is not None and _decision_had_provider_error(decision)
    ):
        # A provider exception is a data problem even when reconciliation degraded
        # it to MANUAL_REVIEW instead of NO_DATA; report it as such so outages
        # surface in the "data error" bucket rather than "failed DCA trigger".
        return MISSING_FILL_DATA_ERROR
    # SKIP / NO_ACTION / MANUAL_REVIEW: no reconciled auto-buy was triggered.
    return MISSING_FILL_FAILED_TRIGGER


def _decision_had_provider_error(decision: ReconciledDecision) -> bool:
    return any(
        signal is not None and signal.provider_error
        for signal in (decision.finance_signal, decision.robinhood_signal)
    )


@dataclass
class RunResult:
    trading_day: date
    decisions: List[ReconciledDecision]
    execution_records: List[OrderExecutionRecord] = field(default_factory=list)
    # ticker -> missing-fill reason captured during the order loop (cap/broker/gap).
    loop_block_reasons: Dict[str, str] = field(default_factory=dict)
    active_universe: List[str] = field(default_factory=list)
    # Kept last so existing positional callers (e.g. RunResult(day, decisions,
    # execution_records)) keep binding to the same fields -- codex review
    # discussion_r3692009447.
    run_label: str = "manual"
    scheduled: bool = False


@dataclass
class ReportResult:
    generated_at: datetime
    run_label: str
    ranked_slippage: List[OrderExecutionRecord]
    top_slippage_tickers: List[str]
    missing_fills: Dict[str, str]
    metrics: Dict[str, object]
    report_path: Optional[str] = None
    csv_path: Optional[str] = None


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in [0, 100]); empty -> 0.0."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct / 100.0 * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac


def build_report(
    run_result: RunResult, run_label: str, generated_at: datetime
) -> ReportResult:
    """Assemble the slippage/coverage report data (no file I/O)."""
    filled = [r for r in run_result.execution_records if r.status == "filled"]
    ranked = sorted(filled, key=lambda r: abs(r.slippage_pct or 0.0), reverse=True)
    top_tickers = [r.ticker for r in ranked[:3]]

    decision_by_ticker = {d.ticker: d for d in run_result.decisions}
    filled_tickers = {r.ticker for r in filled}
    missing: Dict[str, str] = {}
    for ticker in run_result.active_universe:
        if ticker in filled_tickers:
            continue
        missing[ticker] = classify_missing_fill(
            decision_by_ticker.get(ticker),
            run_result.loop_block_reasons.get(ticker),
        )

    reason_counts: Dict[str, int] = {reason: 0 for reason in MISSING_FILL_REASONS}
    for reason in missing.values():
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # ⚡ Bolt Optimization: Consolidate multiple O(N) generator expression passes
    # into a single manual loop to calculate notional, slippage cost, and abs_pcts.
    total_filled_notional = 0.0
    net_slippage_cost = 0.0
    abs_pcts = []
    for r in filled:
        total_filled_notional += (r.avg_fill_price or 0.0) * (r.quantity_filled or 0.0)
        net_slippage_cost += r.slippage_cost or 0.0
        if r.slippage_pct is not None:
            abs_pcts.append(abs(r.slippage_pct))
    abs_pcts.sort()

    metrics: Dict[str, object] = {
        "filled_order_count": len(filled),
        "total_filled_notional": round(total_filled_notional, 6),
        "net_slippage_cost": round(net_slippage_cost, 6),
        "mean_abs_slippage_pct": (
            round(statistics.fmean(abs_pcts), 6) if abs_pcts else 0.0
        ),
        "median_abs_slippage_pct": (
            round(statistics.median(abs_pcts), 6) if abs_pcts else 0.0
        ),
        "p90_abs_slippage_pct": round(_percentile(abs_pcts, 90.0), 6),
        "top_slippage_tickers": top_tickers,
        "missing_fill_count": len(missing),
        "missing_fill_reasons": {k: v for k, v in reason_counts.items() if v},
    }

    return ReportResult(
        generated_at=generated_at,
        run_label=run_result.run_label,
        ranked_slippage=ranked,
        top_slippage_tickers=top_tickers,
        missing_fills=missing,
        metrics=metrics,
    )


_CSV_FIELDS = [
    "ticker",
    "status",
    "order_id",
    "notional_usd",
    "action_reason",
    "target_price",
    "target_price_source",
    "target_quote_time",
    "decision_bar_close",
    "order_submitted_at",
    "order_filled_at",
    "avg_fill_price",
    "quantity_filled",
    "slippage_abs",
    "slippage_pct",
    "slippage_cost",
    "latency_seconds",
    "execution_quality",
]


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def write_execution_csv(records: Sequence[OrderExecutionRecord], path: str) -> str:
    """Write the durable per-order execution/slippage log to CSV."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with atomic_open_w(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for record in records:
            row = {
                field_name: _iso(getattr(record, field_name))
                for field_name in _CSV_FIELDS
            }
            writer.writerow(row)
    return path


def _fmt(value: Optional[float], places: int = 4) -> str:
    return f"{value:.{places}f}" if isinstance(value, (int, float)) else "-"


def render_report_markdown(report: ReportResult, run_result: RunResult) -> str:
    """Render the DCA Manual Run Slippage and Coverage Report as markdown."""
    m = report.metrics
    lines: List[str] = []
    lines.append("# DCA Manual Run Slippage and Coverage Report")
    lines.append("")
    lines.append(f"- Run label: {report.run_label}")
    lines.append(f"- Generated at: {report.generated_at.isoformat()}")
    lines.append(f"- Trading day: {run_result.trading_day.isoformat()}")
    lines.append(f"- Filled orders: {m['filled_order_count']}")
    lines.append(f"- Total filled notional: ${_fmt(m['total_filled_notional'], 2)}")
    lines.append(f"- Net slippage cost: ${_fmt(m['net_slippage_cost'], 4)}")
    lines.append(f"- Mean abs slippage %: {_fmt(m['mean_abs_slippage_pct'])}")
    lines.append(f"- Median abs slippage %: {_fmt(m['median_abs_slippage_pct'])}")
    lines.append(f"- P90 abs slippage %: {_fmt(m['p90_abs_slippage_pct'])}")
    lines.append("")
    lines.append("## Top 3 Slippage Names")
    if report.top_slippage_tickers:
        for rank, record in enumerate(report.ranked_slippage[:3], start=1):
            lines.append(
                f"{rank}. {record.ticker}: {_fmt(record.slippage_pct)}% "
                f"(${_fmt(record.slippage_cost)} cost, {record.execution_quality})"
            )
    else:
        lines.append("_No filled orders._")
    lines.append("")
    lines.append("## Ranked Slippage (by absolute slippage %)")
    lines.append("")
    lines.append(
        "| Rank | Ticker | Target | Avg Fill | Slippage % | Slippage $ | Latency s | Quality |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for rank, r in enumerate(report.ranked_slippage, start=1):
        lines.append(
            f"| {rank} | {r.ticker} | {_fmt(r.target_price)} | {_fmt(r.avg_fill_price)} | "
            f"{_fmt(r.slippage_pct)} | {_fmt(r.slippage_cost)} | {_fmt(r.latency_seconds, 2)} | "
            f"{r.execution_quality} |"
        )
    if not report.ranked_slippage:
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.append("")
    lines.append("## Missing Fills vs Active Universe")
    lines.append(f"- Missing fill count: {m['missing_fill_count']}")
    lines.append(f"- Reasons: {m['missing_fill_reasons'] or 'none'}")
    lines.append("")
    lines.append("| Ticker | Classification |")
    lines.append("| --- | --- |")
    for ticker in sorted(report.missing_fills):
        lines.append(f"| {ticker} | {report.missing_fills[ticker]} |")
    if not report.missing_fills:
        lines.append("| - | - |")
    lines.append("")
    return "\n".join(lines)


def generate_slippage_report(
    run_result: RunResult,
    *,
    output_dir: str,
    now: Optional[datetime] = None,
) -> ReportResult:
    """Build and persist the slippage/coverage report + durable CSV log.

    Reusable after manual or scheduled execution. Performs no interactive prompting.
    """
    generated_at = (now or datetime.now(tz=EASTERN)).astimezone(EASTERN)
    report = build_report(run_result, run_result.run_label, generated_at)
    stamp = generated_at.strftime("%Y%m%d-%H%M%S")
    base = f"dca_{run_result.run_label}_{run_result.trading_day.isoformat()}_{stamp}"
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"{base}_orders.csv")
    report_path = os.path.join(output_dir, f"{base}_slippage_coverage.md")
    write_execution_csv(run_result.execution_records, csv_path)
    atomic_write_text(report_path, render_report_markdown(report, run_result))
    report.csv_path = csv_path
    report.report_path = report_path
    return report


def write_run_state(
    state_path: str,
    *,
    run_result: RunResult,
    report: ReportResult,
    config: StrategyConfig,
    state: StrategyState,
) -> str:
    """Write the manual/scheduled run-state JSON including artifact paths + metrics."""
    trading_day = run_result.trading_day
    payload = {
        "run_label": run_result.run_label,
        "scheduled": run_result.scheduled,
        "trading_day": trading_day.isoformat(),
        "generated_at": report.generated_at.isoformat(),
        "dry_run": config.dry_run,
        "artifacts": {
            "slippage_report_path": report.report_path,
            "orders_csv_path": report.csv_path,
        },
        "budget": {
            "budget_tier": state.current_tier().name,
            "day_spend": state.day_notional(trading_day),
            "week_spend": state.week_notional(trading_day),
            "weekly_rollover_reserve_usd": state.weekly_rollover_reserve_usd,
        },
        "summary_metrics": report.metrics,
    }
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    atomic_write_text(state_path, json.dumps(payload, indent=2, sort_keys=True))
    return state_path


# Warn this many approved trading days before the configured window's end, so the
# hard stop built into APPROVED_TEST_DATES / trading_window.json is never a
# surprise: past the last approved date every scheduled run is silently blocked.
APPROVED_WINDOW_EXPIRY_WARN_DAYS = 10


def approved_window_status(
    approved_dates: Iterable[date], trading_day: date
) -> Tuple[bool, Optional[date], int]:
    """Return ``(in_window, window_end, remaining_days)`` for the approved-dates gate.

    ``remaining_days`` counts approved dates on or after ``trading_day`` — how
    many approved trading days are left before the configured window stops all
    live trading. ``window_end`` is ``None`` when no approved dates are
    configured (the gate then blocks everything and there is no expiry to warn
    about).
    """
    dates = set(approved_dates)
    if not dates:
        return False, None, 0
    window_end = max(dates)
    remaining = sum(1 for day in dates if day >= trading_day)
    return trading_day in dates, window_end, remaining


def warn_if_window_expiring(
    approved_dates: Iterable[date],
    trading_day: date,
    *,
    log: logging.Logger = logger,
) -> None:
    """Log loudly when the approved trading window is over or about to end.

    Past the window's end every run is blocked by the date gate; without this
    the stop is silent (a blocked run is indistinguishable from a holiday).
    Within :data:`APPROVED_WINDOW_EXPIRY_WARN_DAYS` approved days of the end,
    every run carries a countdown warning so the operator extends
    ``config/trading_window.json`` before trading stops.
    """
    in_window, window_end, remaining = approved_window_status(
        approved_dates, trading_day
    )
    if window_end is None:
        return
    if trading_day > window_end:
        log.error(
            "approved trading window ended %s — live trading is hard-stopped; "
            "extend config/trading_window.json (or approved_test_dates) to resume",
            window_end,
        )
    elif in_window and remaining <= APPROVED_WINDOW_EXPIRY_WARN_DAYS:
        log.warning(
            "approved trading window ends %s — %d approved trading day(s) remaining; "
            "extend config/trading_window.json to avoid a silent stop",
            window_end,
            remaining,
        )


def _write_blocked_run_stub(
    trading_day: date,
    artifacts_dir: Optional[str | Path],
    classification_state_root: Optional[str | Path] = None,
) -> None:
    """Record a date/holiday-gate block as a complete root-level artifact set.

    "Blocked" is an outcome, not an excuse for a partial artifact set: a consumer
    that finds only run_state.json cannot tell a deliberate calendar block from a
    run that died mid-write. The stub therefore also writes run_summary.json and
    header-only signals.csv / submitted_orders.csv at the RUN ROOT (Jul 27 2026
    12:30 and 1:30 incidents).

    The state carries a non-null ``run_classification`` and an
    ``order_submit_status`` so the set satisfies the live artifact contract and a
    consumer can attribute the directory, but also ``scheduled_evidence_denied``
    so it is never counted as scheduled evidence. Both halves matter: a stub with
    a null classification fails validate_run_artifacts and reads as malformed,
    while a stub that passed as evidence would manufacture an evidence-shaped
    artifact for a day the market was shut (Codex review).

    Nothing already on disk is clobbered: when a directory is reused, only the
    files that do not exist yet are written, so a previously recorded run keeps
    its own artifacts.

    The blocked path returns before the normal finalizer, so it also runs that
    finalizer's last two steps itself: the four-file set is validated, and when
    ``classification_state_root`` is given the same-day pointer is refreshed so a
    pointer-based watchdog sees today's slot resolved as blocked rather than
    still targeting the previous trading day (Codex review). The state carries
    ``scheduled_evidence_denied``, so the pointer publishes as ``pending`` and no
    consumer counts a market-closed day as scheduled evidence.
    """
    if not artifacts_dir:
        return
    try:  # pragma: no cover - import shim for flat sys.path
        from scripts.scheduled_artifacts import (
            build_run_summary,
            write_signals_csv,
            write_submitted_orders_csv,
        )
    except ImportError:  # pragma: no cover
        from scheduled_artifacts import (  # type: ignore
            build_run_summary,
            write_signals_csv,
            write_submitted_orders_csv,
        )

    out = Path(artifacts_dir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "run_state.json"
    try:  # pragma: no cover - import shim for flat sys.path
        from scripts.run_classification import build_scheduled_classification
    except ImportError:  # pragma: no cover
        from run_classification import build_scheduled_classification  # type: ignore

    day_iso = trading_day.isoformat()
    classification = dict(build_scheduled_classification(day_iso))
    classification["valid_scheduled_evidence"] = False
    # Deliberately NO top-level run_id / generated_at. The calendar gate fired
    # before any run existed, and downstream model-usage logging keys off the
    # presence of a run_id to decide whether a run happened at all: stamping one
    # here would record a model-usage entry for a day the market was shut. The
    # nested classification still carries its own run_id for attribution.
    run_state: Dict[str, Any] = {
        "trading_day": day_iso,
        "status": RUN_STATUS_BLOCKED,
        "run_classification": classification,
        "order_submit_status": ORDER_SUBMIT_STATUS_NO_ORDERS,
        "orders_submitted_count": 0,
        "run_classification_outcome": RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
        "completion_reason": COMPLETION_REASON_DATE_GATE_BLOCKED,
        "error_class": None,
        "is_trading_failure": False,
        # The calendar gate stopped this before any slot could execute, so the
        # artifact is well-formed and attributable but never scheduled evidence.
        "scheduled_evidence_denied": True,
        "budget": {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 0},
        "counts": {"evaluated": 0, "buy_candidates": 0, "submitted": 0},
        "buy_candidates": [],
        "signal_rows": [],
    }
    # Never clobber a previously recorded run when a directory is reused: an
    # existing run_state.json is left exactly as it is. Each remaining file is
    # guarded on its own, so a directory left partial by an interrupted or legacy
    # write is COMPLETED on the next blocked pass rather than staying partial
    # forever behind a single early return (Codex review).
    if not state_path.exists():
        atomic_write_text(state_path, _serialize_run_state(run_state))
    else:
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            existing = None
        if isinstance(existing, dict):
            # Base the companion files on what is actually on disk, so a summary
            # written now mirrors the real run_state rather than this stub.
            run_state = existing
        else:
            # Truncated, non-UTF-8, or not an object: there is no run to preserve,
            # only a malformed file. Leaving it would let this helper write a
            # summary and CSVs around a broken root state and return successfully,
            # defeating the complete-artifact guarantee (Codex review).
            logger.warning(
                "replacing an unreadable run_state.json at %s with the blocked stub",
                state_path,
            )
            atomic_write_text(state_path, _serialize_run_state(run_state))
    if not (out / "run_summary.json").exists():
        atomic_write_text(
            out / "run_summary.json", _serialize_run_state(build_run_summary(run_state))
        )
    if not (out / "signals.csv").exists():
        write_signals_csv(out, [], fieldnames=("ticker",))
    if not (out / "submitted_orders.csv").exists():
        write_submitted_orders_csv(out, [])

    # The normal finalizer never runs on this path, so its validation and
    # pointer refresh happen here (Codex review). Validation gates the pointer
    # rather than rewriting the state: a preserved run_state -- including a
    # legacy one this helper deliberately leaves byte for byte -- is never
    # mutated, so failing closed means the directory is simply not advertised.
    try:  # pragma: no cover - import shim for flat sys.path
        from scripts.scheduled_artifacts import validate_run_artifacts
    except ImportError:  # pragma: no cover
        from scheduled_artifacts import validate_run_artifacts  # type: ignore

    contract = validate_run_artifacts(out, require_summary=True)
    if not contract:
        logger.error(
            "blocked-run artifact contract failed at %s; not refreshing the "
            "latest scheduled pointer: %s",
            out,
            "; ".join(contract.errors),
        )
        return

    if classification_state_root is None:
        return
    try:  # pragma: no cover - import shim for flat sys.path
        from scripts.run_classification import update_latest_scheduled_run_pointer
    except ImportError:  # pragma: no cover
        from run_classification import (  # type: ignore
            update_latest_scheduled_run_pointer,
        )
    try:
        update_latest_scheduled_run_pointer(
            run_state, state_path, Path(classification_state_root)
        )
    except Exception:  # noqa: BLE001 - a pointer failure never blocks the gate
        logger.exception(
            "failed to refresh the latest scheduled pointer under %s",
            classification_state_root,
        )


def _date_or_holiday_blocked(trading_day: date, config: StrategyConfig) -> bool:
    """Return True if the date/holiday gate stops execution for ``trading_day``.

    Called directly by both :func:`_check_execution_gates` (which acts on it) and
    :func:`run_daily_compact` (which reports it): both must agree on when only the
    blocked ``run_state.json`` stub was written, or compact callers can advertise
    signals/candidates/orders CSV paths for files ``run_once`` never created.
    """
    if trading_day not in config.approved_test_dates:
        return True
    # Independent of config.approved_test_dates (which config/trading_window.json
    # can widen to any weekday range with no holiday subtraction of its own): a
    # full NYSE closure is never a trading day, computed here so extending the
    # approved window can never silently reopen a holiday for live orders.
    return trading_day in nyse_holidays(trading_day.year)


def _check_execution_gates(
    trading_day: date,
    config: StrategyConfig,
    broker: BrokerAdapter,
    artifacts_dir: Optional[str | Path],
    classification_state_root: Optional[str | Path] = None,
) -> bool:
    warn_if_window_expiring(config.approved_test_dates, trading_day)
    if _date_or_holiday_blocked(trading_day, config):
        if trading_day not in config.approved_test_dates:
            logger.warning(
                "Date gate blocked execution for %s. No trades.", trading_day
            )
        else:
            # Independent of config.approved_test_dates (which config/trading_window.json
            # can widen to any weekday range with no holiday subtraction of its own): a
            # full NYSE closure is never a trading day, so extending the approved window
            # can never silently reopen a holiday for live orders.
            logger.warning(
                "NYSE holiday gate blocked execution for %s. No trades.", trading_day
            )
        _write_blocked_run_stub(trading_day, artifacts_dir, classification_state_root)
        return False

    # Validate the CONFIGURED account before comparing it to the broker's. The
    # equality check below is satisfied by any two values that happen to match,
    # including two empty ones -- so a live StrategyConfig(target_account_number="")
    # paired with an injected adapter whose account_number() is also "" sailed
    # through and reached placement. build_broker refuses to construct such an
    # adapter, but callers may inject a BrokerAdapter into run_once directly and
    # bypass the factory entirely, so the same condition has to hold here at the
    # execution gate, which every path crosses (Codex review, PR #219).
    if not config.dry_run:
        pinning_failure = account_pinning_failure(
            config.target_account_number, config.broker
        )
        if pinning_failure:
            raise RuntimeError(
                f"Refusing to trade live: {pinning_failure}. Configure a real account "
                "number (ROBINHOOD_AGENTIC_ACCOUNT_NUMBER) before running live, or set "
                "dry_run=True."
            )
    if broker.account_number() != config.target_account_number:
        raise RuntimeError(
            f"Refusing to trade account {broker.account_number()}; expected {config.target_account_number}"
        )
    return True


def _load_deployment_history_states(
    trading_day: date, deployment_history_root: str | Path
) -> List[Dict[str, Any]]:
    """Every readable, non-blocked run_state in the trailing kill-switch window.

    Shared scan behind :func:`_apply_deployment_history` (the pre-run
    kill-switch injection) and :func:`_same_day_ticker_deployment_already_recorded`
    (same-stream duplicate-preserved accounting), so both agree on exactly
    which artifacts count as deployment evidence.
    """
    from dca_risk import KILL_SWITCH_WINDOW_DAYS, find_run_state_files

    # ⚡ Bolt Optimization: the injected figure only sums the trailing
    # KILL_SWITCH_WINDOW_DAYS calendar days ending on ``trading_day`` (see
    # dca_risk.seven_day_deployed), so a run_state whose trading_day falls
    # outside that window can never change it. This runs on every scheduled live
    # run while the history root grows unbounded over time, so skip the expensive
    # json.load for any artifact that carries no in-window date -- the same
    # fast-substring reject used by load_prior_same_day_allocations. A file that
    # DOES match still passes through the full parse and deployed_by_day
    # stream/afternoon-evidence filters, so a match is never counted unvalidated.
    window_days = {
        (trading_day - timedelta(days=offset)).isoformat()
        for offset in range(KILL_SWITCH_WINDOW_DAYS)
    }

    prior_states = []
    for path in find_run_state_files(deployment_history_root):
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (ValueError, OSError):
            continue

        # Only trust the substring reject when the file contains no JSON unicode
        # escape sequence: a spec-valid encoder may spell an ASCII character such
        # as one of the date's hyphens as a unicode escape, which would not
        # literal-match the ISO date even though the parsed value is in-window.
        # Escaped files fall through to a full parse so a same-window artifact is
        # never silently dropped.
        if "\\u" not in raw_text and not any(day in raw_text for day in window_days):
            continue

        try:
            data = json.loads(raw_text)
        except ValueError:
            continue
        # Exclude date-gate blocked stubs from deployment accounting, but KEEP a
        # contract-rejected state (artifact_contract_failure marker): it is invalid
        # scheduled EVIDENCE, yet any orders it actually placed are real deployed
        # capital that must still count toward the rolling kill-switch/cap so the
        # next run cannot over-deploy. deployed_usd only sums status=='submitted'
        # candidates, so a partial-placement failure contributes exactly its real fills.
        if isinstance(data, dict) and (
            data.get("status") != RUN_STATUS_BLOCKED
            or data.get("artifact_contract_failure") is not None
        ):
            prior_states.append(data)
    return prior_states


def _apply_deployment_history(
    config: StrategyConfig, trading_day: date, deployment_history_root: str | Path
) -> StrategyConfig:
    from dca_risk import apply_risk_guard, deployed_by_day

    prior_states = _load_deployment_history_states(trading_day, deployment_history_root)
    return apply_risk_guard(config, deployed_by_day(prior_states), trading_day)


def _same_day_ticker_deployment_already_recorded(
    ticker: str, trading_day: date, deployment_history_root: str | Path
) -> bool:
    """True when a scheduled-afternoon artifact for exactly ``trading_day``
    already recorded a submitted order for ``ticker`` on disk (and therefore
    already summed that order's notional into ``config.deployed_last_7d_usd``
    via ``_apply_deployment_history`` above).

    A same-stream retry's preserved-duplicate broker order must add its
    notional to ``running_deployed_7d`` only when this is False: the prior
    attempt that placed the order was interrupted before it could write its
    own run_state.json, so nothing on disk has counted that capital yet.

    Checked per TICKER, not merely "does any artifact exist for today": a
    zero-order (or different-ticker) afternoon artifact can already be on
    disk for today from an earlier invocation, while a LATER invocation's
    real placement for THIS ticker is still unwritten because it was
    interrupted before overwriting that artifact. Treating any same-day
    artifact as proof this ticker's capital was counted would then silently
    under-count deployment and let later candidates evade the kill-switch
    (Codex review, PR #231: "Verify the recovered order is actually in
    deployment history").
    """
    from dca_risk import is_afternoon_scheduled_evidence

    trading_day_iso = trading_day.isoformat()
    for state in _load_deployment_history_states(trading_day, deployment_history_root):
        if state.get("trading_day") != trading_day_iso:
            continue
        if not is_afternoon_scheduled_evidence(state):
            continue
        candidates = state.get("buy_candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("ticker") == ticker
                and candidate.get("status") == "submitted"
            ):
                return True
    return False


def _write_and_validate_scheduled_contract(
    artifacts: Dict[str, Any],
    artifacts_dir: str | Path,
    *,
    require_signals: bool = True,
) -> Any:
    """Write the remaining scheduled-live contract files, then validate them.

    The compact daily path already wrote run_state.json + signals.csv. The
    scheduled afternoon (3:30 PM ET) contract additionally requires
    ``submitted_orders.csv`` (header row even with no order) and a canonical
    ``run_summary.json`` whose ``run_classification`` MIRRORS run_state's, and it
    forbids a compute-only terminal state (planned orders but nothing placed). This
    finalizes those files and runs :func:`validate_run_artifacts` so a contract
    breach — a missing/unmirrored summary or the Jul 22 2026 compute-only terminal
    state — is surfaced as a loud error on the live path itself, not only in tests.
    Validation failure is logged (mirroring the run_classification validation just
    below) rather than raised, so it never aborts a run that already placed orders.

    ``require_signals=False`` is used by the raw-persistence failure path, which is
    a fail-closed outcome where no signals could be computed (signals.csv is
    legitimately absent); the mirrored summary + header-only submitted_orders.csv
    are still written and validated so that outcome no longer bypasses the contract.
    """
    try:
        from scripts.scheduled_artifacts import (
            ORDER_ATTEMPT_STATUSES,
            artifact_contract_failure,
            build_run_summary,
            validate_run_artifacts,
            write_submitted_orders_csv,
        )
    except ImportError:  # pragma: no cover - import shim for flat sys.path
        from scheduled_artifacts import (  # type: ignore
            ORDER_ATTEMPT_STATUSES,
            artifact_contract_failure,
            build_run_summary,
            validate_run_artifacts,
            write_submitted_orders_csv,
        )

    out = Path(artifacts_dir)
    # submitted_orders.csv: one row per candidate that actually placed or attempted
    # a real order; header-only when none did. A scheduled dry run assigns each
    # candidate a synthetic DRY-RUN-SIM-* order_id while keeping status="dry_run"
    # (see _process_buy_candidate); those simulated orders must NOT be recorded as
    # broker submissions -- the drift detector treats any non-confirmation status
    # with an order_id as a real submission -- so dry_run is excluded even though it
    # carries an order_id.
    order_rows = [
        {
            "ticker": c.get("ticker"),
            "order_id": c.get("order_id"),
            "submitted_amount_usd": c.get("planned_amount_usd"),
            "status": c.get("status"),
        }
        for c in artifacts.get("buy_candidates", [])
        if c.get("status") != "dry_run"
        and (c.get("status") in ORDER_ATTEMPT_STATUSES or c.get("order_id"))
    ]
    write_submitted_orders_csv(out, order_rows)

    # run_summary.json must mirror the SAME non-null run_classification object as
    # run_state.json (Jul 22 2026 3:20 incident: a null summary classification broke
    # scheduled-vs-manual attribution downstream) AND carry the no-order outcome
    # fields, or a summary-only consumer cannot tell a healthy zero-order run from
    # a failure (Codex review). build_run_summary is the single shape both the
    # morning/midday and afternoon writers use.
    summary = build_run_summary(artifacts)
    (out / "run_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    result = validate_run_artifacts(
        out,
        require_signals=require_signals,
        require_submitted_orders=True,
        require_summary=True,
    )
    if not result:
        logger.error(
            "Scheduled-run artifact contract failed for %s: %s",
            out,
            "; ".join(result.errors),
        )
        # Persist the rejection INTO the run_state (and its summary) so every
        # consumer that discovers the file directly refuses to count it as valid
        # scheduled evidence -- not just the latest-run pointer. select_same_day_
        # run_state gates on derive_valid_scheduled_evidence (now fails closed on
        # the artifact_contract_failure marker) and load_scheduled_run_states drops
        # status=='blocked' stubs.
        failure = artifact_contract_failure(
            "scheduled-run artifact contract failed",
            detail="; ".join(result.errors),
        )
        for name in ("run_state.json", "run_summary.json"):
            fpath = out / name
            if not fpath.exists():
                continue
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            data["status"] = RUN_STATUS_BLOCKED
            data["artifact_contract_failure"] = failure
            rc = data.get("run_classification")
            if isinstance(rc, dict):
                rc["valid_scheduled_evidence"] = False
            fpath.write_text(
                json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
            )
    return result


def _finalize_and_write_artifacts(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    decisions: List[ReconciledDecision],
    candidate_outcomes: Dict[str, Dict[str, Any]],
    order_reviews_attempted: int,
    errors_count: int,
    notifications_sent: Optional[int],
    cost_control_notes: Optional[Sequence[str]],
    top_up_outcomes: Optional[Dict[str, TopUpGuardOutcome]],
    scheduled: bool,
    artifacts_dir: Optional[str | Path],
    classification_state_root: Optional[str | Path],
    raw_persistence_state: Optional[Dict[str, object]] = None,
    classification_overrides: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
) -> None:
    if artifacts_dir is None:
        return

    artifacts = build_run_artifacts(
        now,
        config,
        state,
        decisions,
        candidate_outcomes,
        order_reviews_attempted=order_reviews_attempted,
        errors_count=errors_count,
        notifications_sent=notifications_sent,
        cost_control_notes=cost_control_notes,
        top_up_outcomes=top_up_outcomes,
        manual=not scheduled,
        raw_persistence_state=raw_persistence_state,
        classification_overrides=classification_overrides,
        correlation_id=correlation_id,
    )
    paths = write_run_artifacts(artifacts, artifacts_dir)
    logger.info("Wrote run artifacts: %s", paths["run_state"])

    contract_ok = True
    if scheduled:
        contract_ok = bool(
            _write_and_validate_scheduled_contract(artifacts, artifacts_dir)
        )

    if scheduled and classification_state_root is not None:
        try:
            from scripts.run_classification import (
                update_latest_scheduled_run_pointer,
                validate_classification,
            )
        except ImportError:
            from run_classification import (
                update_latest_scheduled_run_pointer,
                validate_classification,
            )
        rc = artifacts.get("run_classification")
        result = validate_classification(rc if isinstance(rc, dict) else {})
        if result and contract_ok:
            update_latest_scheduled_run_pointer(
                artifacts, paths["run_state"], classification_state_root
            )
        elif not contract_ok:
            # A run that breached the artifact contract (e.g. the compute-only
            # terminal state: planned orders but nothing placed) must NOT be
            # recorded as valid scheduled evidence, even when its run_classification
            # is well-formed. Skip the pointer update so the invalid run cannot be
            # picked up as the latest scheduled run.
            logger.error(
                "Not updating latest_scheduled_run.json: scheduled-run artifact "
                "contract failed (see the artifact-contract error above); this run "
                "is not valid scheduled evidence."
            )
        else:
            logger.error(
                "Not updating latest_scheduled_run.json: invalid run_classification (%s)",
                "; ".join(result.errors),
            )


def _graded_dca_ref_id_prefix(stream: str, kind: str = "") -> str:
    """Build the ``graded-dca[-<stream>][-<kind>]`` prefix for ref ids and
    same-stream duplicate checks.

    The ``"afternoon"`` stream (this module's own historical, single-stream
    default) keeps the exact legacy, unprefixed ``"graded-dca[-kind]"``
    convention byte-for-byte -- ``docs/safeguards.md``'s Gate 4 sole-executor
    check and the real legacy ``10ce362c`` task's own order history depend on
    that literal string. Only a non-``"afternoon"`` stream (``"morning"``/
    ``"midday"``, from the seven-slot roster's ``run_dca.run_slot()``) gets a
    stream token inserted: ``near_close_dca.py`` is now the SAME engine
    shared across all three canonical slots, so without a per-stream token,
    ``_same_stream_order_exists`` cannot tell "this exact run's own retry"
    apart from "a different, earlier canonical slot's genuine cross-stream
    order" -- an earlier slot's real fill would be misread as this slot's own
    duplicate, and the guarded top-up it should have placed would silently
    become a no-op instead (Codex review: "Namespace idempotency keys before
    enabling cross-slot top-ups").
    """
    parts = ["graded-dca"]
    if stream and stream != "afternoon":
        parts.append(stream)
    if kind:
        parts.append(kind)
    return "-".join(parts)


def _same_stream_order_exists(
    broker: BrokerAdapter, ticker: str, trading_day: date, stream: str = "afternoon"
) -> Optional[Dict[str, Any]]:
    """Return the broker order dict when this stream's own idempotency ref id
    already exists for ``ticker`` today, else ``None``.

    A guarded top-up candidate (``ticker_guarded`` in ``_process_buy_candidate``)
    intentionally bypasses the broad ``same_day_strategy_order_exists`` check so
    that an earlier CROSS-stream (morning/midday) allocation is not mistaken for
    a duplicate. But that bypass must not also blind a retry of THIS SAME
    scheduled run (same stream) to an order it already placed for the ticker on
    an earlier attempt today -- with no narrower check, such a retry would
    recompute the top-up guard from the cross-stream prior allocation alone
    (which does not know about this stream's own already-placed top-up) and
    submit a second, duplicate order (mirrors morning_dca.py's
    ``_same_stream_order_exists``).

    Both the plain and topup scheduled ref-id prefixes are checked, matching
    the two ref_id shapes ``_process_buy_candidate`` can have used depending on
    whether the ticker was already guarded on the attempt that placed the order.
    ``stream`` (default ``"afternoon"``, this module's historical sole caller)
    scopes both prefixes to the calling slot -- see
    :func:`_graded_dca_ref_id_prefix`.
    """
    checker = getattr(broker, "same_day_strategy_order_exists_with_ref_id_prefix", None)
    if checker is None:
        raise RuntimeError(
            "broker does not support same-stream ref-id duplicate checks for guarded top-ups"
        )
    prefixes = (
        f"{_graded_dca_ref_id_prefix(stream, 'topup')}-{trading_day.isoformat()}-{ticker}",
        f"{_graded_dca_ref_id_prefix(stream)}-{trading_day.isoformat()}-{ticker}",
    )
    result = checker(ticker, trading_day, prefixes)
    if not isinstance(result, dict):
        # Normalize legacy boolean adapters: False/None -> None (no duplicate),
        # True -> treat as found but without order details.
        return None if not result else {}
    return result


def _process_buy_candidate(
    decision: ReconciledDecision,
    config: StrategyConfig,
    state: StrategyState,
    broker: BrokerAdapter,
    robinhood_data: MarketDataProvider,
    now: datetime,
    trading_day: date,
    guard_active: bool,
    prior_same_day_allocations: Dict[str, PriorSameDayAllocation],
    running_deployed_7d: float,
    scheduled: bool,
    run_result: Optional[RunResult],
    deployment_history_root: Optional[str | Path] = None,
    stream: str = "afternoon",
) -> tuple[Dict[str, Any], Optional[TopUpGuardOutcome], float, int, int]:
    ticker = decision.ticker
    order_amount = decision.suggested_amount_usd
    prior = prior_same_day_allocations.get(ticker) if guard_active else None
    ticker_guarded = guard_active and prior is not None
    top_up_outcome = None
    errors_count_inc = 0
    order_reviews_attempted_inc = 0
    candidate_outcome: Dict[str, Any] = {}

    def _block(reason: str, message: str) -> None:
        if run_result is not None:
            run_result.loop_block_reasons[ticker] = reason
        logger.info(message)

    # Computed here (before any early-return gate below) purely for context --
    # its incremental_order_amount still caps order_amount further down, but
    # its own skipped/manual_review verdict must not be honored until AFTER
    # the same-stream duplicate check just below has a chance to find and
    # preserve an order this stream already placed earlier the same run.
    if guard_active:
        effective_prior = prior
        if ticker_guarded and not config.dry_run:
            # KNOWN GAP fix (Codex review: "Include broker orders in the delta
            # before disabling the duplicate gate"): evaluate_top_up_guard's own
            # docstring already documents that `prior.amount_usd` is
            # artifact-derived only -- a same-day buy confirmed at the broker but
            # missing or incomplete in the artifact trail (or placed by an
            # entirely different ref-id convention, e.g. a legacy
            # morning_dca.py/midday_dca.py order for this same ticker) would
            # otherwise be invisible here, letting the guard compute a delta
            # larger than what is actually still unallocated. Cross-check
            # against the broker's own ground-truth same-day total for this
            # ticker (regardless of ref id/stream) and never trust an artifact
            # figure lower than what the broker itself reports.
            try:
                broker_reported_usd = broker.same_day_ticker_notional_usd(
                    ticker, trading_day
                )
            except Exception as exc:  # noqa: BLE001
                errors_count_inc += 1
                candidate_outcome = {
                    "status": "blocked",
                    "skip_reason": f"same-day notional check failed: {exc}",
                }
                _block(
                    MISSING_FILL_DATA_ERROR,
                    f"{ticker}: same-day notional check failed: {exc}; skipping",
                )
                return (
                    candidate_outcome,
                    top_up_outcome,
                    running_deployed_7d,
                    errors_count_inc,
                    order_reviews_attempted_inc,
                )
            if broker_reported_usd > prior.amount_usd:
                effective_prior = replace(prior, amount_usd=broker_reported_usd)
        top_up_outcome = evaluate_top_up_guard(decision, effective_prior, config)

    # A morning/manual run must NOT block the scheduled afternoon run: when the
    # top-up guard has a loaded prior allocation for this ticker, the earlier
    # same-day allocation is handled via the top-up guard below, so the
    # broker's broad same-day-order block further down is intentionally
    # bypassed here for guarded tickers. That bypass must not also blind a RETRY of
    # this same scheduled afternoon run to an order it already placed for this
    # ticker on an earlier attempt today -- prior_same_day_allocations
    # deliberately excludes this stream's own earlier same-day runs (see
    # load_prior_same_day_allocations), so nothing else would catch that case.
    # A narrower same-stream ref-id check closes that gap without reopening the
    # broad block for a legitimate cross-stream top-up.
    #
    # This runs BEFORE the top-up guard's own skip-return, the kill-switch
    # cap, and the per-order ceiling below: a retry's freshly recomputed
    # top-up math (or a freshly recomputed order_amount that happens to sit
    # above max_per_order_usd) must never suppress or overwrite evidence of an
    # order this same stream already placed earlier the same run (Codex
    # review, PR #231: "Check for an existing order before returning from the
    # top-up guard").
    #
    # Skipped entirely under config.dry_run: this whole check exists only to
    # prevent a REAL duplicate broker order, and a dry run never submits one
    # (the placement branch further down short-circuits into a simulated
    # order instead of calling the broker at all -- see "if config.dry_run"
    # below). Querying the broker's narrow duplicate lookup here anyway would
    # let a fail-closed adapter (e.g. AlpacaBroker, which now raises when it
    # cannot verify a real duplicate) block a harmless simulated candidate
    # for a risk that cannot occur in dry-run mode (Codex review, PR #231:
    # "Allow guarded Alpaca simulations through the duplicate hook").
    if ticker_guarded and not config.dry_run:
        try:
            dup_order = _same_stream_order_exists(broker, ticker, trading_day, stream)
        except Exception as exc:  # noqa: BLE001
            errors_count_inc += 1
            candidate_outcome = {
                "status": "blocked",
                "skip_reason": f"duplicate-check failed: {exc}",
            }
            _block(
                MISSING_FILL_DATA_ERROR,
                f"{ticker}: same-stream duplicate check failed: {exc}; skipping",
            )
            return (
                candidate_outcome,
                top_up_outcome,
                running_deployed_7d,
                errors_count_inc,
                order_reviews_attempted_inc,
            )
        if dup_order is not None:
            order_id = dup_order.get("id") or dup_order.get("order_id")
            if not order_id:
                # A legacy boolean-style adapter (True, normalized to {} by
                # _same_stream_order_exists) or a matched order dict missing
                # both id fields carries no traceable identity: recording
                # status="submitted" with a null order_id would land an
                # unidentifiable row in submitted_orders.csv that
                # reconciliation can never match to a broker fill, while
                # _log_execution can't query a fill for it either. Classify
                # it as incomplete and let a human/recovery pass resolve the
                # real order id rather than publish an untraceable
                # submission (Codex review, PR #231: "Require an order ID
                # before marking legacy duplicates submitted").
                errors_count_inc += 1
                candidate_outcome = {
                    "status": "blocked",
                    "skip_reason": "same-stream broker order exists but adapter returned no order id",
                }
                _block(
                    MISSING_FILL_DATA_ERROR,
                    f"{ticker}: same-stream duplicate found but broker returned no order id; skipping",
                )
                return (
                    candidate_outcome,
                    top_up_outcome,
                    running_deployed_7d,
                    errors_count_inc,
                    order_reviews_attempted_inc,
                )
            try:
                dup_amount = float(
                    dup_order.get("dollar_amount") or dup_order.get("notional") or 0
                )
            except (TypeError, ValueError):
                dup_amount = 0.0
            # A matched order missing both amount fields carries no real
            # dollar figure -- fall back to this run's own freshly
            # recomputed order_amount (pre-kill-switch, pre-top-up-cap at
            # this point) rather than record a nonsensical $0 buy against
            # the ticker's daily cap. The real production broker adapter
            # always reports dollar_amount for a matched order, so this
            # fallback is not reachable there.
            if not math.isfinite(dup_amount) or dup_amount <= 0:
                dup_amount = order_amount
            order_key = (ticker, trading_day)
            if order_key not in state.own_orders_recorded:
                state.record_buy(ticker, trading_day, dup_amount)
                state.own_orders_recorded.add(order_key)
            # else: this exact StrategyState instance already booked THIS
            # STREAM's own order for this ticker/day earlier in the SAME run
            # (e.g. an in-process retry that reuses its StrategyState rather
            # than starting a fresh one) -- recording it again here would
            # inflate today's/this week's spend and buy count, incorrectly
            # capping later candidates and overstating the persisted budget
            # audit (Codex review, PR #231: "Avoid rebooking duplicates in
            # reused strategy state"). Checked via own_orders_recorded, NOT
            # buys_today: _seed_prior_allocations also calls record_buy to
            # seed CROSS-stream (morning/midday) prior context into this
            # ticker's day/week spend, which bumps buys_today to a nonzero
            # value even on a freshly restarted process whose OWN afternoon
            # order was never booked into this state instance -- checking
            # buys_today alone would then wrongly skip recording a genuinely
            # new-to-this-state recovered order, omitting it from day/week
            # spend and letting later candidates exceed the corrected cap
            # (Codex review, PR #231: "Record the recovered buy separately
            # from seeded prior context").
            #
            # Whether this order is already counted toward the trailing-7-day
            # kill-switch figure depends on whether THIS ticker's submitted
            # order is recorded in an on-disk scheduled-afternoon artifact for
            # today: _apply_deployment_history sums deployment from disk once
            # before this batch starts, so a written artifact already reflects
            # it (adding it again here would double-count against the
            # kill-switch threshold -- Codex review, PR #231: "Do not add a
            # preserved duplicate to deployment again"). But when the prior
            # attempt was interrupted after the broker accepted the order yet
            # before it could write ITS OWN artifact for this ticker, nothing
            # on disk has ever counted this capital, and the recovered order
            # must be added so later candidates in this retry's batch (and the
            # kill-switch audit) see the real deployed total. Checked per
            # ticker, not merely "does any artifact exist for today's date":
            # a same-day artifact that doesn't itself contain THIS ticker's
            # submitted order (e.g. a zero-order artifact from an earlier,
            # unrelated invocation) must not be mistaken for proof that this
            # order was already counted (Codex review, PR #231: "Verify the
            # recovered order is actually in deployment history").
            if (
                deployment_history_root is not None
                and not _same_day_ticker_deployment_already_recorded(
                    ticker, trading_day, deployment_history_root
                )
            ):
                running_deployed_7d += dup_amount
            # This row's real order was authorized by the spread gate on the
            # EARLIER attempt that actually placed it, and that original
            # evidence isn't available here without threading the prior
            # artifact's path through this call (out of scope for this
            # change). Taking a fresh retry-time quote_spread_pct and
            # recording it as an ordinary spread_ok/spread_not_ok/
            # spread_unavailable verdict would be worse than recording
            # nothing: the spread can move between the original placement and
            # this retry, and a fresh unfavorable reading would relabel an
            # already-authorized, genuinely submitted order as having failed
            # or never reached the safety gate, corrupting the historical
            # evidence config/scheduled_tasks.json's audit consumers rely on
            # (Codex review, PR #231: "Retain the original spread verdict for
            # recovered orders"). A prior attempt at this used a bespoke
            # "duplicate_recovered" status, but config/scheduled_tasks.json's
            # row_level_statuses contract enumerates exactly spread_ok,
            # spread_unavailable, spread_not_ok, and not_evaluated -- emitting
            # anything outside that declared enum breaks contract consumers
            # (Codex review, PR #231: "Keep recovered spread evidence within
            # the declared contract"). SPREAD_STATUS_NOT_EVALUATED is the
            # nearest compliant value: like this row, it commits to no
            # pass/fail verdict and per the contract's own note is "never
            # recorded as spread_ok=true" -- spread_ok/spread_pct stay None
            # rather than a fabricated True/False. duplicate_preserved=True
            # on the outcome remains the signal that this status reflects
            # missing RETRY evidence, not a row that skipped the gate outright.
            spread_fields = {
                "spread_ok": None,
                "spread_pct": None,
                "spread_status": SPREAD_STATUS_NOT_EVALUATED,
            }
            candidate_outcome = {
                "status": "submitted",
                "order_id": order_id,
                "skip_reason": "same-stream broker order exists",
                "submitted_amount_usd": dup_amount,
                # Carried on the outcome so a failure handler synthesizing a
                # buy_candidates row from this outcome alone (see
                # _publish_in_flight_outcome / _failure_candidate_row) can
                # still record the real multiplier, matching the fresh-
                # placement branch below.
                "buy_multiplier": decision.buy_multiplier,
                # Preserves the real broker order rather than a fresh placement,
                # so a caller booking this candidate into a persistent ledger
                # must not double-count it against an earlier attempt's record,
                # and so _build_buy_candidates keeps this amount instead of
                # clamping it against THIS retry's freshly recomputed
                # incremental_order_amount (Codex review, PR #231: "Keep the
                # broker notional when rebuilding duplicate artifacts").
                "duplicate_preserved": True,
                **spread_fields,
            }
            _publish_in_flight_outcome(ticker, candidate_outcome)
            logger.info(
                "%s: same-stream broker order already exists; not re-submitting (order_id=%s)",
                ticker,
                candidate_outcome.get("order_id"),
            )
            if run_result is not None:
                # Log this the same way as a fresh placement (not via _block):
                # this ticker's order was actually submitted, so it must appear
                # in execution_records for the slippage/coverage report, and
                # any missing-fill classification must come from the real fill
                # status (_log_execution only classifies MISSING_FILL_EXECUTION_GAP
                # when the order truly has no fill) rather than a hardcoded
                # "budget capped" reason that would misreport a submitted order.
                _log_execution(
                    run_result,
                    decision,
                    candidate_outcome.get("order_id"),
                    broker,
                    notional_usd=dup_amount,
                )
            return (
                candidate_outcome,
                top_up_outcome,
                running_deployed_7d,
                errors_count_inc,
                order_reviews_attempted_inc,
            )

    if kill_switch_engaged(replace(config, deployed_last_7d_usd=running_deployed_7d)):
        # Cap at THIS candidate's own selected base ($1 or $2), not the fixed
        # config.base_buy_amount_usd ($2) -- a below-close-only candidate's 1x
        # kill-switch amount is $1, and capping it at $2 would let it submit
        # double the reduced-scale risk cap the kill switch is meant to enforce
        # (Codex review: "Cap the kill switch at the selected base"). When an
        # earlier same-day allocation already fixed this ticker's base for the
        # day (see resolve_top_up_desired_amount_usd), the kill-switch ceiling
        # must use THAT preserved base, not this run's own freshly recomputed
        # decision.selected_base_amount_usd -- a deeper price that newly
        # crosses the prior regular-session low can raise this run's own base
        # from $1 to $2 for a ticker whose day is still pinned to the $1
        # scale, and capping at the fresh $2 would let a guarded top-up submit
        # double the reduced-scale risk the kill switch is meant to enforce
        # for that ticker (Codex review: "Cap guarded top-ups using the
        # preserved base").
        kill_switch_base = (
            prior.selected_base_amount_usd
            if prior is not None
            and prior.selected_base_amount_usd
            and prior.selected_base_amount_usd > 0
            else decision.selected_base_amount_usd
        )
        order_amount = min(order_amount, kill_switch_base)

    if guard_active:
        order_amount = min(top_up_outcome.incremental_order_amount, order_amount)
        if top_up_outcome.skipped:
            candidate_outcome = {
                "status": "skipped",
                "order_id": None,
                "skip_reason": top_up_outcome.top_up_guard_result,
            }
            if top_up_outcome.manual_review:
                logger.warning(
                    "%s: prior same-day amount $%.2f but no comparable price; "
                    "conservative $0 top-up, manual review required",
                    ticker,
                    top_up_outcome.prior_same_day_amount,
                )
            _block(
                MISSING_FILL_TOP_UP_OFFSET,
                f"{ticker}: top-up guard offset ({top_up_outcome.top_up_guard_result}); no incremental buy",
            )
            return (
                candidate_outcome,
                top_up_outcome,
                running_deployed_7d,
                errors_count_inc,
                order_reviews_attempted_inc,
            )

    # Hard per-order ceiling is checked before any broker round-trip -- including
    # the one_order_per_ticker_per_day duplicate check just below -- so that an
    # over-cap candidate never needs a same-day-duplicate lookup or a spread
    # quote. Moving this ahead of the roster's broad broker check keeps the
    # existing cap-before-duplicate contract (see
    # test_run_daily_compact_checks_per_order_cap_before_broker_duplicate_lookup)
    # intact now that one_order_per_ticker_per_day also triggers a broker
    # round-trip on this path (Codex review, discussion_r3679633773: "Check the
    # hard ceiling before the broker duplicate lookup").
    if order_amount > config.max_per_order_usd:
        candidate_outcome = {
            "status": "blocked",
            "skip_reason": "max per-order ceiling exceeded",
            "deliberate_skip": True,
            "budget_blocked": True,
        }
        _block(
            MISSING_FILL_BUDGET_CAPPED,
            f"{ticker}: order amount ${order_amount:.2f} exceeds max per-order "
            f"${config.max_per_order_usd:.2f}; skipping",
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    if config.one_order_per_ticker_per_day and broker.same_day_strategy_order_exists(
        ticker, trading_day
    ):
        if top_up_outcome is not None:
            top_up_outcome = replace(top_up_outcome, incremental_order_amount=0.0)
        candidate_outcome = {
            "status": "skipped",
            "skip_reason": "same-day broker order exists",
        }
        _block(
            MISSING_FILL_BUDGET_CAPPED,
            f"{ticker}: same-day broker order already exists; skipping",
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    if (config.one_order_per_ticker_per_day or not ticker_guarded) and (
        state.buys_today(ticker, trading_day) >= config.max_buys_per_ticker_per_day
    ):
        # Zero the top-up guard's incremental amount the same way the broker
        # duplicate check above does: this candidate is being skipped, so a
        # downstream reader of top_up_outcome (_build_buy_candidates) must not
        # still see a nonzero incremental_order_amount for a row whose status is
        # "skipped" (Antigravity review: "Inconsistent top_up_outcome zeroing
        # when one_order_per_ticker_per_day triggers").
        if top_up_outcome is not None:
            top_up_outcome = replace(top_up_outcome, incremental_order_amount=0.0)
        candidate_outcome = {
            "status": "skipped",
            "skip_reason": "already bought today",
            "budget_blocked": True,
        }
        _block(MISSING_FILL_BUDGET_CAPPED, f"{ticker}: already bought today; skipping")
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    # A morning/manual run must NOT block the scheduled afternoon run: when the
    # top-up guard has a loaded prior allocation for this ticker, the earlier
    # same-day allocation is handled above (and any same-stream retry of THIS
    # ticker was already caught by the duplicate check at the top of this
    # function), so the broker's broad same-day-order block is intentionally
    # bypassed here for guarded tickers -- a legitimate cross-stream top-up
    # must not be blocked by the morning stream's own earlier order. When the
    # roster's one-order-per-day policy is enabled, the unconditional broker
    # check above already enforced the broader rule, so do not query it twice.
    if (
        not config.one_order_per_ticker_per_day
        and not ticker_guarded
        and broker.same_day_strategy_order_exists(ticker, trading_day)
    ):
        candidate_outcome = {
            "status": "skipped",
            "skip_reason": "same-day broker order exists",
        }
        _block(
            MISSING_FILL_BUDGET_CAPPED,
            f"{ticker}: same-day broker order already exists; skipping",
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    # Budget/cap checks run BEFORE the spread lookup, matching the per-order
    # ceiling above ("an over-cap candidate should never need a same-day-duplicate
    # lookup or a spread quote") and the blocked_before_spread_gate contract: a
    # cap-blocked candidate carries NO spread verdict, so a run stopped only by
    # caps is never misreported as missing_spread_data (Codex review).
    if not within_caps(order_amount, state, trading_day, config):
        candidate_outcome = {
            "status": "blocked",
            "skip_reason": "cap would be exceeded",
            "deliberate_skip": True,
            "budget_blocked": True,
        }
        _block(MISSING_FILL_BUDGET_CAPPED, f"{ticker}: cap would be exceeded; skipping")
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )

    # This candidate qualified on the ladder and top-up rules, so it reaches the
    # spread gate. Only rows that get this far carry a spread verdict, and only
    # they can be counted as blocked for want of a proven spread
    # (config/scheduled_tasks.json:spread_data_availability_policy.evaluation_order).
    try:
        spread_pct = quote_spread_pct(robinhood_data, ticker, now)
    except Exception as exc:  # noqa: BLE001
        errors_count_inc += 1
        candidate_outcome = {
            "status": "blocked",
            "skip_reason": f"spread quote error: {exc}",
            **spread_gate_fields(
                spread_pct=None, max_spread_pct=config.max_bid_ask_spread_pct
            ),
        }
        _block(
            MISSING_FILL_DATA_ERROR, f"{ticker}: spread quote failed: {exc}; skipping"
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    spread_fields = spread_gate_fields(
        spread_pct=spread_pct, max_spread_pct=config.max_bid_ask_spread_pct
    )
    if spread_pct is None and scheduled:
        # config/scheduled_tasks.json requires spread_ok=true before any
        # scheduled live order: a provider without bid/ask support, or a
        # quote lookup that returned nothing, must fail closed here
        # rather than silently bypass the breaker. Manual runs still go
        # through review_market_buy as a human-reviewed safety net, so
        # they are not blocked on a missing spread alone.
        candidate_outcome = {
            "status": "blocked",
            "skip_reason": "bid/ask spread unavailable; scheduled runs require spread_ok",
            "deliberate_skip": True,
            # The spread gate itself stopped this row, recorded explicitly rather
            # than inferred from spread_status (Codex review).
            "blocked_by": BLOCKED_BY_SPREAD_GATE,
            **spread_fields,
        }
        _block(
            MISSING_FILL_BROKER_BLOCKED,
            f"{ticker}: bid/ask spread unavailable for scheduled run; skipping",
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    if spread_pct is not None and spread_pct > config.max_bid_ask_spread_pct:
        candidate_outcome = {
            "status": "blocked",
            "skip_reason": (
                f"bid/ask spread {spread_pct:.2f}% exceeds "
                f"{config.max_bid_ask_spread_pct:.2f}% limit"
            ),
            "deliberate_skip": True,
            **spread_fields,
        }
        _block(
            MISSING_FILL_BROKER_BLOCKED,
            f"{ticker}: bid/ask spread {spread_pct:.2f}% exceeds "
            f"{config.max_bid_ask_spread_pct:.2f}% limit; skipping",
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    if broker.available_buying_power() < order_amount:
        candidate_outcome = {
            "status": "blocked",
            "skip_reason": "insufficient buying power",
            "deliberate_skip": True,
            "budget_blocked": True,
            **spread_fields,
        }
        _block(
            MISSING_FILL_BUDGET_CAPPED, f"{ticker}: insufficient buying power; skipping"
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )
    if not broker.is_tradable_for_fractional_buy(ticker):
        candidate_outcome = {
            "status": "blocked",
            "skip_reason": "not tradable for fractional buy",
            "deliberate_skip": True,
            **spread_fields,
        }
        _block(
            MISSING_FILL_BROKER_BLOCKED,
            f"{ticker}: not tradable for fractional/notional buy; skipping",
        )
        return (
            candidate_outcome,
            top_up_outcome,
            running_deployed_7d,
            errors_count_inc,
            order_reviews_attempted_inc,
        )

    reviewed_order = None
    if not scheduled:
        order_reviews_attempted_inc += 1
        review_ok, review_reason = broker.review_market_buy(ticker, order_amount)
        if not review_ok:
            candidate_outcome = {
                "status": "blocked",
                "skip_reason": f"review blocked: {review_reason}",
                "deliberate_skip": True,
                **spread_fields,
            }
            _block(
                MISSING_FILL_BROKER_BLOCKED,
                f"{ticker}: review blocked order: {review_reason}",
            )
            return (
                candidate_outcome,
                top_up_outcome,
                running_deployed_7d,
                errors_count_inc,
                order_reviews_attempted_inc,
            )
        reviewed_order = {
            "notional_usd": round(order_amount, 2),
            "side": "buy",
            "order_type": "market",
            "time_in_force": None,
            "estimated_price": None,
            "review_passed": True,
        }

    # A manual (non-scheduled) placement gets its own "-manual-" ref-id
    # segment, distinct from a plain scheduled order's ref_id. guard_active
    # (and so ticker_guarded) is only ever True for scheduled runs, so a
    # manual placement always falls to this branch. Without a distinct
    # segment a manual fill's ref_id would be indistinguishable from a plain
    # scheduled order's, so a LATER scheduled run that finds this manual fill
    # as cross-stream prior context and computes a guarded top-up would have
    # its same-stream duplicate check match the manual fill's ordinary ref_id
    # and wrongly treat the fresh incremental top-up as an already-submitted
    # retry -- recording it as "submitted" without ever calling
    # place_market_buy (Codex review, PR #231: "Separate manual ref IDs from
    # scheduled retry IDs"; mirrors morning_dca.py's identical fix, PR #42).
    ref_id = (
        f"{_graded_dca_ref_id_prefix(stream, 'topup')}-{trading_day.isoformat()}-{ticker}"
        if ticker_guarded
        else (
            f"{_graded_dca_ref_id_prefix(stream)}-{trading_day.isoformat()}-{ticker}"
            if scheduled
            else f"{_graded_dca_ref_id_prefix(stream, 'manual')}-{trading_day.isoformat()}-{ticker}"
        )
    )
    if config.dry_run:
        logger.info(
            "[DRY RUN] Would buy %s: $%.2f (%s)",
            ticker,
            order_amount,
            decision.reason,
        )
        if isinstance(broker, DryRunBroker):
            order_id = broker.place_market_buy(ticker, order_amount, ref_id=ref_id)
        else:
            order_id = f"DRY-RUN-SIM-{ticker}-{order_amount:.2f}-{ref_id}"
        candidate_outcome = {
            "status": "dry_run",
            "order_id": order_id,
            "skip_reason": "dry_run: order not placed",
            "reviewed_order": reviewed_order,
            "submitted_amount_usd": order_amount,
            **spread_fields,
        }
    else:
        try:
            order_id = broker.place_market_buy(ticker, order_amount, ref_id=ref_id)
        except OrderConfirmationRequiredError:
            candidate_outcome = {
                "status": "not_submitted_confirmation_required",
                "order_id": None,
                "skip_reason": "broker requires post-review confirmation before submission",
                "reviewed_order": reviewed_order,
                "submitted_amount_usd": order_amount,
                **spread_fields,
            }
            _block(
                MISSING_FILL_BROKER_BLOCKED,
                f"{ticker}: broker requires confirmation before submission; skipping",
            )
            return (
                candidate_outcome,
                top_up_outcome,
                running_deployed_7d,
                errors_count_inc,
                order_reviews_attempted_inc,
            )
        except Exception as exc:  # noqa: BLE001
            errors_count_inc += 1
            candidate_outcome = {
                "status": "failed",
                "order_id": None,
                "skip_reason": f"placement failed: {exc}",
                "reviewed_order": reviewed_order,
                "submitted_amount_usd": order_amount,
                **spread_fields,
            }
            logger.error("Placement failed for %s: %s", ticker, exc)
            return (
                candidate_outcome,
                top_up_outcome,
                running_deployed_7d,
                errors_count_inc,
                order_reviews_attempted_inc,
            )
        state.record_buy(ticker, trading_day, order_amount)
        state.own_orders_recorded.add((ticker, trading_day))
        if scheduled:
            running_deployed_7d += order_amount
        logger.info(
            "Placed buy for %s: $%.2f, order_id=%s",
            ticker,
            order_amount,
            order_id,
        )
        candidate_outcome = {
            "status": "submitted",
            "order_id": order_id,
            "skip_reason": None,
            "reviewed_order": reviewed_order,
            "submitted_amount_usd": order_amount,
            # Carried on the outcome so the failure handlers, which synthesize
            # buy_candidates rows from these outcomes alone, can record the real
            # multiplier. run_state.schema.json requires the property on every
            # candidate row, and a failure artifact that schema-based reporting
            # rejects is not the complete record it claims to be (Codex review).
            "buy_multiplier": decision.buy_multiplier,
            **spread_fields,
        }
        # Publish the accepted order NOW, before any fallible follow-up. The
        # batch only records an outcome once _process_buy_candidate RETURNS, so a
        # raise in _log_execution's fill lookup below would leave this real,
        # already-placed order out of the failure artifact entirely and let a
        # later same-day run re-allocate it (Codex review).
        _publish_in_flight_outcome(ticker, candidate_outcome)

    if run_result is not None:
        _log_execution(
            run_result,
            decision,
            candidate_outcome.get("order_id"),
            broker,
            notional_usd=order_amount,
        )

    return (
        candidate_outcome,
        top_up_outcome,
        running_deployed_7d,
        errors_count_inc,
        order_reviews_attempted_inc,
    )


def _evaluate_all_signals(
    now: datetime,
    config: StrategyConfig,
    tradable_tickers: List[str],
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
) -> tuple[List[ReconciledDecision], int]:
    errors_count = 0

    def _safe_signal(
        ticker: str, data: MarketDataProvider, interval_minutes: int
    ) -> SourceSignal:
        nonlocal errors_count
        try:
            return evaluate_source_signal(
                ticker=ticker,
                now=now,
                config=config,
                data=data,
                interval_minutes=interval_minutes,
            )
        except Exception as exc:  # noqa: BLE001 - count and degrade to NO_DATA
            not_found = _is_ticker_not_found_error(exc)
            if not_found:
                reason = f"ticker not found on {getattr(data, 'source', SourceName.FINANCE).value}: {exc}"
            else:
                errors_count += 1
                reason = f"provider error: {exc}"
            return SourceSignal(
                ticker=ticker,
                source=getattr(data, "source", SourceName.FINANCE),
                action=Action.NO_DATA,
                reason=reason,
                # Ticker-not-found is a permanent, source-specific condition documented in config/tickers.json
                # data_source_notes; it must not force MANUAL_REVIEW via reconcile_signals' provider_error guard.
                # Any other exception still sets provider_error so provider failures block auto-buy.
                provider_error=not not_found,
            )

    decisions = []
    for ticker in tradable_tickers:
        finance_signal = _safe_signal(
            ticker, finance_data, config.finance_interval_minutes
        )
        robinhood_signal = _safe_signal(
            ticker, robinhood_data, config.robinhood_interval_minutes
        )
        decisions.append(reconcile_signals(ticker, finance_signal, robinhood_signal))

    return decisions, errors_count


def _seed_prior_allocations(
    state: StrategyState,
    trading_day: date,
    guard_active: bool,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]],
    *,
    one_order_per_ticker_per_day: bool,
) -> None:
    # ``one_order_per_ticker_per_day`` no longer skips seeding: this call is
    # what folds an earlier roster slot's spend into day_notional()/
    # week_notional() for the ABSOLUTE $ cap accounting. Early-returning here
    # left duplicate-buy prevention to the broker's same_day_strategy_order_exists
    # check alone, but that check says nothing about the previously-spent
    # dollar amount -- so a later slot's own (different-ticker) candidates
    # still saw the full daily/weekly cap as if nothing had been spent yet,
    # letting successive roster slots exceed the configured absolute caps
    # (Codex review, discussion_r3679391265: "Preserve prior allocations in
    # cap bookkeeping"). Seeding also bumps buys_today() for the ticker,
    # which is a harmless belt-and-suspenders duplicate for the one-order
    # gates below since same_day_strategy_order_exists already blocks a
    # second buy for that ticker first. The bool parameter is kept for
    # call-site clarity/back-compat rather than removed.
    if guard_active and prior_same_day_allocations:
        for _ticker, _alloc in prior_same_day_allocations.items():
            if _alloc.amount_usd > 0 and state.buys_today(_ticker, trading_day) == 0:
                state.record_buy(_ticker, trading_day, _alloc.amount_usd)


def _process_buy_candidates_batch(
    buy_candidates: List[ReconciledDecision],
    config: StrategyConfig,
    state: StrategyState,
    broker: BrokerAdapter,
    robinhood_data: MarketDataProvider,
    now: datetime,
    trading_day: date,
    guard_active: bool,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]],
    scheduled: bool,
    run_result: Optional[RunResult],
    deployment_history_root: Optional[str | Path] = None,
    stream: str = "afternoon",
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, TopUpGuardOutcome], float, int, int]:
    candidate_outcomes: Dict[str, Dict[str, Any]] = {}
    # Published by reference BEFORE any placement so a failure part-way through
    # the batch can still see every order that landed (mirrors the morning
    # executor). Without it the afternoon failure writer would assert zero orders
    # for a slot that had already placed some (Codex review).
    _IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = candidate_outcomes
    top_up_outcomes: Dict[str, TopUpGuardOutcome] = {}
    running_deployed_7d = config.deployed_last_7d_usd
    errors_count_inc = 0
    order_reviews_attempted_inc = 0

    for decision in buy_candidates:
        c_outcome, t_outcome, running_deployed_7d, err_inc, rev_inc = (
            _process_buy_candidate(
                decision=decision,
                config=config,
                state=state,
                broker=broker,
                robinhood_data=robinhood_data,
                now=now,
                trading_day=trading_day,
                guard_active=guard_active,
                prior_same_day_allocations=prior_same_day_allocations or {},
                running_deployed_7d=running_deployed_7d,
                scheduled=scheduled,
                run_result=run_result,
                deployment_history_root=deployment_history_root,
                stream=stream,
            )
        )
        candidate_outcomes[decision.ticker] = c_outcome
        if t_outcome is not None:
            top_up_outcomes[decision.ticker] = t_outcome
        errors_count_inc += err_inc
        order_reviews_attempted_inc += rev_inc

    return (
        candidate_outcomes,
        top_up_outcomes,
        running_deployed_7d,
        errors_count_inc,
        order_reviews_attempted_inc,
    )


# Placement outcomes for the run currently in flight, keyed so the failure
# handler can read whatever the batch had produced when it raised.
_IN_FLIGHT_CANDIDATE_OUTCOMES: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _order_csv_covers(path: Path, expected_rows: Sequence[Dict[str, Any]]) -> bool:
    """True when every expected order already appears in the CSV at ``path``.

    validate_run_artifacts checks only the header, so a stale order CSV from an
    earlier attempt passes it while omitting orders the state records.
    """
    expected = {
        str(row.get("order_id") or f"ticker:{row.get('ticker')}")
        for row in expected_rows
    }
    if not expected:
        return True
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            present = {
                str(row.get("order_id") or f"ticker:{row.get('ticker')}")
                for row in csv.DictReader(handle)
            }
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    return expected.issubset(present)


def _failure_candidate_row(ticker: str, outcome: Dict[str, Any]) -> Dict[str, Any]:
    """A buy_candidates row synthesized from a broker outcome by a failure path.

    One builder for both failure paths -- the direct write and the retry merge --
    so a field added for the schema cannot land on one and miss the other, which
    is how the two drifted before. run_state.schema.json requires ticker,
    buy_multiplier, planned_amount_usd, and status on every candidate row;
    validate_run_artifacts does not run schema validation, so an omission here
    produces an artifact that reads as a complete failure record while
    schema-based reporting rejects it (Codex review).

    ``buy_multiplier`` is carried on the placement outcome; null only when an
    outcome predates that (schema-valid, and honest about not knowing).
    """
    return {
        "ticker": ticker,
        "status": outcome.get("status", "not_submitted"),
        "order_id": outcome.get("order_id"),
        "buy_multiplier": outcome.get("buy_multiplier"),
        "planned_amount_usd": outcome.get("submitted_amount_usd"),
        "skip_reason": outcome.get("skip_reason"),
    }


def _publish_in_flight_outcome(ticker: str, outcome: Dict[str, Any]) -> None:
    """Record a placement outcome for the failure handler as soon as it exists.

    Called from inside the per-candidate path rather than waiting for the batch
    loop to store the returned value, so an order the broker has already accepted
    is never lost to a raise in the follow-up work between placement and return.
    """
    outcomes = _IN_FLIGHT_CANDIDATE_OUTCOMES.get("outcomes")
    if outcomes is not None:
        outcomes[ticker] = outcome


def run_once(
    ctx: RunContext,
) -> List[ReconciledDecision]:
    """Run the afternoon scan, guaranteeing an artifact set even when it raises.

    The scheduled contract requires the complete root-level artifact set on every
    outcome, but nothing wrapped this path: an exception from the account lookup,
    signal evaluation, or any per-candidate broker check escaped before
    _finalize_and_write_artifacts ran, so the slot produced no artifacts at all,
    and a failure after an earlier order was accepted lost that submission from
    the canonical state (Codex review). This mirrors MorningDCAExecutor.run.
    """
    # Clear any in-flight state from a PREVIOUS call before anything can fail, or
    # a startup failure here would record the prior run's orders as this one's.
    _IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)
    scheduled = ctx.scheduled
    try:
<<<<<<< HEAD
        return _run_once_impl(ctx)
=======
        return _run_once_impl(
            now,
            config,
            state,
            finance_data,
            robinhood_data,
            broker,
            artifacts_dir,
            **kwargs,
        )
>>>>>>> origin/main
    except Exception as exc:  # noqa: BLE001 - always re-raised below
        if scheduled and ctx.artifacts_dir:
            try:
                _write_afternoon_failure_artifacts(
                    ctx.artifacts_dir,
                    ctx.now,
                    exc,
                    classification_state_root=ctx.classification_state_root,
                )
            except Exception:  # noqa: BLE001 - never mask the original failure
                logger.exception(
                    "failed to write afternoon failure artifacts to %s; re-raising "
                    "the original run failure",
                    ctx.artifacts_dir,
                )
        raise


def _write_afternoon_failure_artifacts(
    artifacts_dir: str | Path,
    now: datetime,
    exc: BaseException,
    classification_state_root: Optional[str | Path] = None,
) -> None:
    """Write the complete root-level set for an afternoon run that raised.

    Preserves any order already placed, never overwrites a run_state the run had
    already written, and completes a partial directory. The artifact proves the
    slot fired; it is never scheduled evidence.
    """
    try:  # pragma: no cover - import shim for flat sys.path
        from scripts.run_classification import (
            build_scheduled_classification,
            scheduled_run_id,
            update_latest_scheduled_run_pointer,
        )
        from scripts.scheduled_artifacts import (
            build_run_summary,
            submitted_order_rows,
            write_signals_csv,
            write_submitted_orders_csv,
        )
    except ImportError:  # pragma: no cover
        from run_classification import (  # type: ignore
            build_scheduled_classification,
            scheduled_run_id,
            update_latest_scheduled_run_pointer,
        )
        from scheduled_artifacts import (  # type: ignore
            build_run_summary,
            submitted_order_rows,
            write_signals_csv,
            write_submitted_orders_csv,
        )

    out = Path(artifacts_dir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "run_state.json"
    trading_day = now.astimezone(EASTERN).date().isoformat()

    placed = {
        ticker: outcome
        for ticker, outcome in (
            _IN_FLIGHT_CANDIDATE_OUTCOMES.get("outcomes") or {}
        ).items()
        if isinstance(outcome, dict)
    }
    submitted = sum(1 for oc in placed.values() if oc.get("status") == "submitted")

    # A truncated, non-UTF-8, or non-object existing state is not a run to
    # preserve, only a malformed file: it is replaced with the full synthesized
    # failure state exactly as the no-file branch does, rather than being wrapped
    # in fresh companions around a broken root (Codex review).
    existing_is_usable = False
    state_was_mutated = False
    if state_path.exists():
        try:
            _loaded = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            _loaded = None
        existing_is_usable = isinstance(_loaded, dict)
        if not existing_is_usable:
            logger.warning(
                "replacing an unreadable run_state.json at %s with the failure state",
                state_path,
            )

    if not existing_is_usable:
        classification = dict(build_scheduled_classification(trading_day))
        classification["valid_scheduled_evidence"] = False
        run_state: Dict[str, Any] = {
            # Unlike the deliberately runless calendar-gate stub, a failure state
            # IS a run: it carries the scheduled run id the schema requires and
            # build_run_summary reads (Codex review).
            "run_id": scheduled_run_id(trading_day),
            "trading_day": trading_day,
            "generated_at": now.astimezone(EASTERN).isoformat(),
            "broker": config_broker_name(),
            "status": "failed",
            "run_classification": classification,
            "run_classification_outcome": (
                RUN_CLASSIFICATION_FAILED_AFTER_SUBMISSION
                if submitted
                else RUN_CLASSIFICATION_STARTUP_FAILED
            ),
            "order_submit_status": (
                ORDER_SUBMIT_STATUS_SUBMITTED
                if submitted
                else ORDER_SUBMIT_STATUS_ERROR
            ),
            "error_class": (
                ERROR_CLASS_POST_SUBMISSION_FAILURE
                if submitted
                else ERROR_CLASS_STARTUP_FAILURE
            ),
            "error_detail": f"{type(exc).__name__}: {exc}",
            "is_trading_failure": True,
            "scheduled_evidence_denied": True,
            "orders_submitted_count": submitted,
            "candidates_reaching_spread_gate": 0,
            "candidates_blocked_missing_spread": 0,
            "budget": {
                "budget_tier": "normal",
                "daily_cap_usd": 0,
                "weekly_cap_usd": 0,
            },
            "counts": {
                "evaluated": 0,
                "buy_candidates": len(placed),
                "submitted": submitted,
            },
            "signal_rows": [],
            "buy_candidates": [
                _failure_candidate_row(ticker, outcome)
                for ticker, outcome in placed.items()
            ],
            "broker_outcomes": placed,
        }
        if placed:
            run_state["placement_evidence_incomplete"] = True
        atomic_write_text(state_path, _serialize_run_state(run_state))
    else:
        logger.warning(
            "afternoon run failed after run_state.json was written; preserving it "
            "and completing any missing companion artifacts"
        )
        run_state = _loaded
        # The preserved file can belong to an EARLIER invocation at this slot, so
        # fold in whatever THIS one placed. Discarding it would hide an accepted
        # order from the same-day guards and let a later run buy it again (Codex
        # review). Rewritten only when there is something to add.
        if placed:
            merged = dict(run_state)
            existing_outcomes = dict(merged.get("broker_outcomes") or {})
            rows = [
                row
                for row in (merged.get("buy_candidates") or [])
                if isinstance(row, dict)
            ]
            # Keyed by ORDER ID, not ticker. A retry can place a distinct
            # incremental order for a ticker the earlier attempt already bought,
            # and a ticker-keyed setdefault would silently keep only the old one
            # while the count and the CSV are derived from these outcomes (Codex
            # review).
            seen_orders = {
                oc.get("order_id")
                for oc in existing_outcomes.values()
                if isinstance(oc, dict) and oc.get("order_id")
            }
            seen_orders.update(
                row.get("order_id") for row in rows if row.get("order_id")
            )
            added = False
            for ticker, outcome in placed.items():
                order_id = outcome.get("order_id")
                if order_id and order_id in seen_orders:
                    continue
                if not order_id and ticker in existing_outcomes:
                    continue
                if order_id:
                    seen_orders.add(order_id)
                # A second order for the same ticker needs its own key so it is
                # not lost to the collision this fixes. The real ticker is
                # carried INSIDE the outcome, because submitted_order_rows()
                # would otherwise write the synthetic key into the canonical
                # order row as the symbol (Codex review).
                key = (
                    ticker
                    if ticker not in existing_outcomes
                    else f"{ticker}:{order_id}"
                )
                if key != ticker:
                    outcome = {**outcome, "ticker": ticker}
                existing_outcomes[key] = outcome
                rows.append(_failure_candidate_row(ticker, outcome))
                added = True

            if added:
                merged["broker_outcomes"] = existing_outcomes
                merged["buy_candidates"] = rows
                merged["orders_submitted_count"] = len(
                    {
                        oc.get("order_id") or f"ticker:{key}"
                        for key, oc in existing_outcomes.items()
                        if isinstance(oc, dict) and oc.get("status") == "submitted"
                    }
                    | {
                        row.get("order_id") or f"ticker:{row.get('ticker')}"
                        for row in rows
                        if row.get("status") in ("submitted", "filled")
                    }
                )
                merged["counts"] = {
                    **(merged.get("counts") or {}),
                    "submitted": merged["orders_submitted_count"],
                }
                # THIS invocation raised, so the merged state is a failure. The
                # label is gated on the merged submitted COUNT, not on merely
                # having an outcome to merge: a retry whose only outcomes were
                # blocked or failed placed nothing, and calling that a
                # post-submission failure would claim an order landed when none
                # did (Codex review). Shared with morning_dca so the two cannot
                # drift.
                run_state = stamp_failure_classification(
                    merged,
                    submitted=merged["orders_submitted_count"],
                    error_detail=f"{type(exc).__name__}: {exc}",
                )
                state_was_mutated = True
                atomic_write_text(state_path, _serialize_run_state(run_state))

    # A SYNTHESIZED state -- no prior file, or one replaced because it was
    # unreadable or not an object -- describes this failure and nothing else, so
    # every companion left in a reused directory belongs to some earlier attempt.
    # Preserving them on existence alone advertised a stale summary whose
    # classification does not match the new state, failing a contract this
    # handler could have repaired (Codex review). Same rule as the morning
    # runner, which had the identical defect.
    state_is_synthesized = not existing_is_usable
    if state_is_synthesized or not (out / "signals.csv").exists():
        # No explicit fieldnames: write_signals_csv derives them from the rows and
        # falls back to ["ticker"] when there are none, so pinning ("ticker",)
        # only truncated a PRESERVED state's richer signal_rows -- dropping
        # auto_buy_signal and the amounts the drift detector reads -- while
        # gaining nothing on the empty synthesized case (Copilot review).
        write_signals_csv(out, run_state.get("signal_rows") or [])
    expected_orders = submitted_order_rows(run_state.get("broker_outcomes") or {})
    orders_csv = out / "submitted_orders.csv"
    # _order_csv_covers returns True for an empty expected set without reading
    # the file, so on a synthesized state that placed nothing a stale CSV would
    # otherwise survive untouched.
    if (
        state_is_synthesized
        or not orders_csv.exists()
        or not _order_csv_covers(orders_csv, expected_orders)
    ):
        # A stale CSV from an earlier attempt must never be advertised as the
        # canonical order record for a state that knows about more orders.
        write_submitted_orders_csv(out, expected_orders)
    # Regenerate whenever the state changed: validate_run_artifacts compares only
    # the nested classification, so a pre-merge summary would pass while reporting
    # the previous attempt's order count and outcome (Codex review).
    if (
        state_is_synthesized
        or state_was_mutated
        or not (out / "run_summary.json").exists()
    ):
        atomic_write_text(
            out / "run_summary.json", _serialize_run_state(build_run_summary(run_state))
        )
    # Refresh latest_scheduled_run.json so a pointer-based watchdog sees today's
    # slot as fired-and-failed rather than still targeting the previous trading
    # day (Codex review). The state it points at carries
    # scheduled_evidence_denied, so every consumer that follows the pointer still
    # refuses to count it as evidence.
    try:
        # This invocation entered the failure handler, so the four-file set may
        # be incomplete or carry a corrupt companion from an earlier attempt.
        # Deriving pointer validity from the state's scheduled identity alone
        # would publish it as valid anyway, so validate first and fail the state
        # closed when it does not hold (Codex review).
        #
        # Validated even with no classification_state_root: the contract failure
        # is stamped INTO the state, which needs no pointer root, and gating it
        # on one left a mismatched companion set advertised unchecked whenever
        # the caller had no root to update (Codex review).
        from scripts.scheduled_artifacts import (
            artifact_contract_failure,
            validate_run_artifacts,
        )

        contract = validate_run_artifacts(out, require_summary=True)
        if not contract or not _order_csv_covers(orders_csv, expected_orders):
            run_state["artifact_contract_failure"] = artifact_contract_failure(
                "scheduled-run artifact contract failed",
                detail="; ".join(contract.errors)
                or "order CSV does not cover the state",
            )
            run_state["scheduled_evidence_denied"] = True
            rc = run_state.get("run_classification")
            if isinstance(rc, dict):
                rc["valid_scheduled_evidence"] = False
            atomic_write_text(state_path, _serialize_run_state(run_state))
        if classification_state_root is not None:
            update_latest_scheduled_run_pointer(
                run_state, state_path, Path(classification_state_root)
            )
    except Exception:  # noqa: BLE001 - never mask the original run failure
        logger.exception(
            "failed to validate the failure artifact set at %s or refresh its pointer",
            out,
        )
    logger.error(
        "afternoon run failed; wrote the failure artifact set to %s (orders already placed: %d)",
        state_path,
        submitted,
    )


def config_broker_name() -> str:
    """Execution broker recorded on a failure artifact built without a config."""
    return "robinhood"


def _run_once_impl(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    artifacts_dir: Optional[str | Path] = None,
    run_result: Optional[RunResult] = None,
    notifications_sent: Optional[int] = None,
    cost_control_notes: Optional[Sequence[str]] = None,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None,
    deployment_history_root: Optional[str | Path] = None,
    scheduled: bool = True,
    classification_state_root: Optional[str | Path] = None,
    raw_persistence_state: Optional[Dict[str, object]] = None,
    classification_overrides: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    stream: str = "afternoon",
) -> List[ReconciledDecision]:
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()
    if run_result is not None:
        run_result.trading_day = trading_day
        # Sync the resolved scheduled flag onto the result so write_run_state
        # reflects how this run actually executed, not whatever default the
        # caller's RunResult happened to be constructed with -- codex review
        # discussion_r3692009453.
        run_result.scheduled = scheduled

    errors_count = 0
    order_reviews_attempted = 0

    if not _check_execution_gates(
        trading_day, config, broker, artifacts_dir, classification_state_root
    ):
        return []

    # Inject the trailing-7-day scheduled-live deployment from history so the
    # kill-switch in graded_buy_multiplier reflects real cash burn. Imported
    # lazily to avoid a circular import (dca_risk imports from this module).
    # deployment_history_root is threaded through to _process_buy_candidates_batch
    # (and from there to _process_buy_candidate) so a duplicate-preserved retry
    # can check per-ticker, at the moment it needs to, whether ITS OWN order is
    # already reflected on disk -- see _same_day_ticker_deployment_already_recorded.
    if deployment_history_root is not None:
        config = _apply_deployment_history(config, trading_day, deployment_history_root)

    decisions: List[ReconciledDecision] = []
    tradable_tickers = [
        ticker for ticker in config.tickers if ticker not in config.excluded_tickers
    ]
    if run_result is not None:
        run_result.active_universe = list(tradable_tickers)

    decisions, signals_errors_count = _evaluate_all_signals(
        now, config, tradable_tickers, finance_data, robinhood_data
    )
    errors_count += signals_errors_count

    if run_result is not None:
        run_result.decisions = decisions

    buy_candidates = [
        decision for decision in decisions if decision.action == Action.BUY
    ]
    # Higher multiplier -> larger drop -> ticker alphabetical (see allocation_sort_key).
    buy_candidates.sort(key=allocation_sort_key)

    # ⚡ Bolt Optimization: Use a single pass loop instead of multiple sum() generators
    # to eliminate O(N) object overhead when iterating DataFrames/lists.
    planned_spend = 0.0
    high_multiplier_signals = 0
    for d in buy_candidates:
        planned_spend += d.suggested_amount_usd
        if d.buy_multiplier >= config.high_multiplier_threshold:
            high_multiplier_signals += 1

    state.select_daily_budget(planned_spend, high_multiplier_signals, config)
    # Roll the reserve forward now, before any budget query or day finalization,
    # so a quiet week (no BUY candidates) still carries prior unused Normal-cap
    # capacity into the stress tier's ceiling.
    state.roll_weekly_reserve(trading_day, config)

    # The top-up guard (and its same-day-order-check bypass) is only for the
    # scheduled afternoon run: a manual run must still be blocked by its own
    # same-day-order/max-buys checks even if a caller happens to also pass
    # prior_same_day_allocations (e.g. an auto-loaded state root shared across
    # call sites), or a manual run could unintentionally double-buy.
    guard_active = scheduled and prior_same_day_allocations is not None

    _seed_prior_allocations(
        state,
        trading_day,
        guard_active,
        prior_same_day_allocations,
        one_order_per_ticker_per_day=config.one_order_per_ticker_per_day,
    )

    candidate_outcomes, top_up_outcomes, running_deployed_7d, err_inc, rev_inc = (
        _process_buy_candidates_batch(
            buy_candidates=buy_candidates,
            config=config,
            state=state,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=guard_active,
            prior_same_day_allocations=prior_same_day_allocations,
            scheduled=scheduled,
            run_result=run_result,
            deployment_history_root=deployment_history_root,
            stream=stream,
        )
    )
    errors_count += err_inc
    order_reviews_attempted += rev_inc

    # Reflect any in-batch kill-switch engagement in the config used to build
    # this run's artifacts, so the risk_guard audit block (and
    # kill_switch_engaged) shows the guard actually having engaged during the
    # run when later candidates were capped, instead of only the pre-run
    # trailing-7-day figure.
    config = replace(config, deployed_last_7d_usd=running_deployed_7d)

    manual_review = [d for d in decisions if d.action == Action.MANUAL_REVIEW]
    if manual_review:
        logger.info(
            "Manual review tickers: %s", ", ".join(d.ticker for d in manual_review)
        )

    tier = state.current_tier()
    state.finalize_daily_budget(trading_day, config)
    logger.info(
        "Budget summary [%s]: spent $%.2f (daily cap $%.2f); weekly spent $%.2f / $%.2f; "
        "rollover reserve now $%.2f.",
        tier.name,
        state.day_notional(trading_day),
        tier.daily_cap_usd,
        state.week_notional(trading_day),
        state.effective_weekly_cap(),
        state.weekly_rollover_reserve_usd,
    )

    _finalize_and_write_artifacts(
        now=now,
        config=config,
        state=state,
        decisions=decisions,
        candidate_outcomes=candidate_outcomes,
        order_reviews_attempted=order_reviews_attempted,
        errors_count=errors_count,
        notifications_sent=notifications_sent,
        cost_control_notes=cost_control_notes,
        top_up_outcomes=top_up_outcomes if guard_active else None,
        scheduled=scheduled,
        artifacts_dir=artifacts_dir,
        classification_state_root=classification_state_root,
        raw_persistence_state=raw_persistence_state,
        classification_overrides=classification_overrides,
        correlation_id=correlation_id,
    )

    return decisions


def run_and_report(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    *,
    output_dir: str,
    state_path: str,
    run_label: str = "manual",
    scheduled: bool = False,
    deployment_history_root: Optional[str | Path] = None,
) -> Dict[str, object]:
    """Execute one DCA run and emit the full slippage/coverage narrative + state file.

    This is the heavier path intended for manual and Friday/monthly reviews: it
    renders the narrative markdown report in addition to the CSV log. For the daily
    live path use ``run_daily_compact`` instead, which writes structured JSON/CSV and
    a compact summary only. This path is fully non-interactive: it never prompts for
    confirmation, so scheduled/background execution is unaffected. Returns the
    artifact paths and summary metrics.

    ``deployment_history_root`` feeds the trailing-7-day kill-switch (see
    ``dca_risk.apply_risk_guard``), same as ``run_daily_compact``/``run_pipeline``;
    defaults to ``output_dir``'s parent when not given, so a manual/full-report run
    during a period where scheduled-live deployment exceeded the threshold is
    capped the same as the compact daily path instead of silently using the
    full 2x-5x ladder.
    """
    if deployment_history_root is None:
        deployment_history_root = Path(output_dir).parent
<<<<<<< HEAD
    run_result = RunResult(trading_day=now.astimezone(EASTERN).date(), decisions=[])
    ctx = RunContext(
        now=now,
        config=config,
        state=state,
        finance_data=finance_data,
        robinhood_data=robinhood_data,
        broker=broker,
=======
    run_result = RunResult(
        trading_day=now.astimezone(EASTERN).date(),
        decisions=[],
        run_label=run_label,
        scheduled=scheduled,
    )
    run_once(
        now,
        config,
        state,
        finance_data,
        robinhood_data,
        broker,
>>>>>>> origin/main
        run_result=run_result,
        deployment_history_root=deployment_history_root,
        scheduled=scheduled,
    )
    run_once(ctx)
    report = generate_slippage_report(
        run_result, output_dir=output_dir, now=now
    )
    written_state = write_run_state(
        state_path,
        run_result=run_result,
        report=report,
        config=config,
        state=state,
    )
    return {
        "run_label": run_label,
        "scheduled": scheduled,
        "slippage_report_path": report.report_path,
        "orders_csv_path": report.csv_path,
        "state_path": written_state,
        "summary_metrics": report.metrics,
    }


@dataclass(frozen=True)
class CompactRunOptions:
    """Grouped configuration for :func:`run_daily_compact`.

    Bundles the daily compact run's tunables -- output location, run
    labeling/scheduling flags, cross-run state carried between same-day
    passes, and identifiers threaded through to the underlying
    :func:`run_once` call -- into a single immutable options object instead
    of a long keyword-argument list.

    Attributes:
        artifacts_dir: Directory the run's structured JSON/CSV artifacts are
            written to.
        run_label: Label recorded on the run result and artifacts (e.g.
            ``"daily"``, ``"morning"``).
        scheduled: Whether this run was triggered by the scheduler (as
            opposed to a manual invocation).
        notifications_sent: Count of notifications already sent for this
            trading day, if any, used to keep the usage-proxy counter
            accurate across passes.
        prior_same_day_allocations: Per-ticker allocations from an earlier
            same-day pass, used to top up rather than double-allocate.
        deployment_history_root: Root directory for deployment-history
            lookups; defaults to the parent of ``artifacts_dir`` when unset.
        classification_state_root: Root directory for persisted signal
            classification state.
        raw_persistence_state: Raw persistence state carried in from a
            prior pass/process, if the caller is resuming one.
        classification_overrides: Manual overrides applied on top of the
            computed signal classification.
        correlation_id: Identifier used to correlate this run's artifacts
            and logs with related runs/processes.
        stream: Which scheduled stream this run belongs to (e.g.
            ``"morning"``, ``"afternoon"``).
    """

    artifacts_dir: str | Path
    run_label: str = "daily"
    scheduled: bool = True
    notifications_sent: Optional[int] = None
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None
    deployment_history_root: Optional[str | Path] = None
    classification_state_root: Optional[str | Path] = None
    raw_persistence_state: Optional[Dict[str, object]] = None
    classification_overrides: Optional[Dict[str, Any]] = None
    correlation_id: Optional[str] = None
    stream: str = "afternoon"


def run_daily_compact(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    options: Optional[CompactRunOptions] = None,
    **legacy_options: object,
) -> Dict[str, object]:
    """Run one DCA scan and write only compact, structured daily artifacts.

    This is the token/credit-lean daily live path. It deliberately does NOT:
        - render the long narrative slippage/coverage markdown report,
        - build the weekly performance matrix,
        - run any historical backtest,
        - generate the full Notion dashboard.

    Those heavier rollups belong to Friday/monthly reviews (see ``run_and_report``
    and ``dca_weekly_dashboard``). Daily output is:
        - ``run_state.json``    structured run state incl. the usage-proxy block,
        - ``signals.csv``       per-(ticker, source) signal rows,
        - ``buy_candidates.csv``per-candidate rows,
        - ``orders.csv``        durable per-order execution/slippage log.

    The compact Notion "Latest Run" sync is produced downstream from
    ``latest_run_summary.json`` (see ``dca_reporting``/``dca_notion_sync``); this
    function only writes local artifacts and does not contact Notion directly.
    It may interact with the passed-in broker adapter (dry-run by default).

    Prefer passing ``options`` (a :class:`CompactRunOptions`). The former
    keyword-only parameters (``artifacts_dir``, ``run_label``, ``scheduled``,
    etc. -- documented in ``README_REPORTING.md``) are still accepted via
    ``**legacy_options`` and used to build a ``CompactRunOptions`` for
    backwards compatibility; do not pass both ``options`` and legacy keyword
    arguments in the same call.
    """
    if options is None:
        options = CompactRunOptions(**legacy_options)
    elif legacy_options:
        raise TypeError(
            "run_daily_compact() got legacy keyword arguments "
            f"{sorted(legacy_options)} in addition to 'options'; pass either "
            "'options' or the legacy keyword arguments, not both"
        )
    trading_day = now.astimezone(EASTERN).date()
<<<<<<< HEAD
    run_result = RunResult(trading_day=trading_day, decisions=[])
    ctx = RunContext(
        now=now,
        config=config,
        state=state,
        finance_data=finance_data,
        robinhood_data=robinhood_data,
        broker=broker,
        artifacts_dir=artifacts_dir,
=======
    run_result = RunResult(
        trading_day=trading_day,
        decisions=[],
        run_label=options.run_label,
        scheduled=options.scheduled,
    )
    run_once(
        now,
        config,
        state,
        finance_data,
        robinhood_data,
        broker,
        artifacts_dir=options.artifacts_dir,
>>>>>>> origin/main
        run_result=run_result,
        notifications_sent=options.notifications_sent,
        prior_same_day_allocations=options.prior_same_day_allocations,
        deployment_history_root=options.deployment_history_root,
        scheduled=options.scheduled,
        classification_state_root=options.classification_state_root,
        raw_persistence_state=options.raw_persistence_state,
        classification_overrides=options.classification_overrides,
        correlation_id=options.correlation_id,
        stream=options.stream,
    )
    run_once(ctx)

    out = Path(options.artifacts_dir)
    out.mkdir(parents=True, exist_ok=True)
    result: Dict[str, object] = {
        "run_label": options.run_label,
        "scheduled": options.scheduled,
        "compact": True,
        "blocked": _date_or_holiday_blocked(trading_day, config),
        "artifacts_dir": str(out),
        "run_state_path": str(out / "run_state.json"),
    }
    if result["blocked"]:
        # Date or holiday gate tripped inside run_once. The blocked stub still
        # writes the complete root-level set (run_state.json, run_summary.json, and
        # header-only signals.csv / submitted_orders.csv), so advertise the signals
        # path and the summary. buy_candidates.csv / orders.csv genuinely were not
        # written -- the run never evaluated a candidate or logged an execution --
        # so those keys stay absent rather than pointing at missing files.
        result.update(
            run_summary_path=str(out / "run_summary.json"),
            signals_path=str(out / "signals.csv"),
            submitted_orders_path=str(out / "submitted_orders.csv"),
        )
        return result

    orders_csv = out / "orders.csv"
    write_execution_csv(run_result.execution_records, str(orders_csv))
    _backfill_orders_csv_usage(out / "run_state.json")
    result.update(
        signals_path=str(out / "signals.csv"),
        buy_candidates_path=str(out / "buy_candidates.csv"),
        orders_csv_path=str(orders_csv),
    )
    return result


if __name__ == "__main__":
    raise SystemExit(
        "near_close_dca is the strategy library, not a runnable entry point. "
        "Run the orchestrator instead: `python run_dca.py --demo` for a local dry-run, "
        "or drive a live/scheduled run via run_dca.run_pipeline() with providers/broker "
        "built from dca_live_adapters (wire Robinhood MCP callables). Live placement stays "
        "gated behind StrategyConfig.dry_run and the adapter's enable_live_orders flag."
    )
