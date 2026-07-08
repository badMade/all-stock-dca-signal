"""
Near-close graded DCA strategy skeleton.

This mirrors the scheduled live-test rules:
    - Run at 3:20 PM America/New_York (the canonical live schedule).
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
"""

from __future__ import annotations

import csv
import json
import logging
import os
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# Module logger. Library code never configures handlers (that is the entry point's job
# in run_dca.py / the scheduler); operator-facing run output goes through this logger so
# an unattended live task can route it to files/alerting instead of bare stdout.
logger = logging.getLogger("near_close_dca")

ROBINHOOD_AGENTIC_ACCOUNT_NUMBER = "562057042"

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
    "DFTX", "TRIN", "AAAU", "SPCX", "TRLV", "RVI", "VCX", "EVR", "LUNR", "IRDM",
    "DXYZ", "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD", "CBRS",
    "MRVL", "BTX", "STM", "SNDK", "WDC", "TSM", "AVGO", "NVDA", "QBTS", "FLKR",
    "INTC", "GOOGL", "GOOG", "AMZN", "GLW", "MA", "GS", "MU", "NFLX", "XLG",
    "TOPT", "QTOP", "SATA", "RGTI", "IBM",
]

EXCLUDED_TICKERS = {"BYDDY"}


def _weekday_range(start: date, end: date) -> set:
    """All Mon-Fri dates in [start, end] inclusive."""
    days = set()
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:  # 0=Mon .. 4=Fri
            days.add(cursor)
        cursor += timedelta(days=1)
    return days


# US equity-market full closures that fall inside the live window (weekdays only,
# so weekend holidays are already excluded). 2026-07-03 is the observed
# Independence Day (Jul 4 2026 is a Saturday). Labor Day (2026-09-07) is past the
# window. Excluding these keeps the date gate meaning "an actual trading day."
MARKET_HOLIDAYS = frozenset({date(2026, 7, 3)})

# Approved live-test window. The original pilot dates (Jun 29 - Jul 1 2026) plus
# the canonical live window of weekdays 2026-07-02 through 2026-08-28 inclusive
# (minus market holidays), matching the ce16ba6b/10ce362c trigger_window in
# config/scheduled_tasks.json. Without this, run_once()'s date gate would mark
# every scheduled run in the live window blocked/no-trade.
APPROVED_TEST_DATES = (
    {date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)}
    | _weekday_range(date(2026, 7, 2), date(2026, 8, 28))
) - MARKET_HOLIDAYS


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
    # Execution target derived from the source that drives the buy. target_price is
    # the signal-time quote by default; decision_bar_close is retained separately.
    target_price: float = 0.0
    target_price_source: str = ""
    target_quote_time: Optional[datetime] = None
    decision_bar_close: float = 0.0


@dataclass(frozen=True)
class BudgetTier:
    """A budget tier with its absolute daily and weekly notional ceilings."""

    name: str
    daily_cap_usd: float
    weekly_cap_usd: float
    uses_rollover_reserve: bool = False


# Ordered low-to-high. The live $3-allocation policy selects the highest tier
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


def select_budget_tier(planned_spend_usd: float, high_multiplier_signal_count: int) -> BudgetTier:
    """Select the budget tier for a run from planned spend and 3x+ signal count.

    Each dimension maps to a tier index; the higher of the two wins:
        spend:   <=129 -> 0, >129 -> 1, >213 -> 2, >272 -> 3
        signals: <=5   -> 0, 6-10 -> 1, 11-17 -> 2, >17 -> 3
    """
    if planned_spend_usd > 272.0:
        spend_idx = 3
    elif planned_spend_usd > 213.0:
        spend_idx = 2
    elif planned_spend_usd > 129.0:
        spend_idx = 1
    else:
        spend_idx = 0

    n = high_multiplier_signal_count
    if n > 17:
        signal_idx = 3
    elif n >= 11:
        signal_idx = 2
    elif n >= 6:
        signal_idx = 1
    else:
        signal_idx = 0

    return BUDGET_TIERS[max(spend_idx, signal_idx)]


@dataclass(frozen=True)
class StrategyConfig:
    tickers: Sequence[str] = tuple(ACTIVE_TICKERS)
    excluded_tickers: frozenset[str] = frozenset(EXCLUDED_TICKERS)
    approved_test_dates: frozenset[date] = frozenset(APPROVED_TEST_DATES)
    decision_time_et: time = time(15, 20)  # matches the 3:20 PM ET canonical live schedule
    trend_lookback_minutes: int = 60
    base_buy_amount_usd: float = 3.0
    weekly_rollover_reserve_cap_usd: float = 900.0
    high_multiplier_threshold: float = 3.0
    minimum_drop_vs_previous_close: float = 0.001
    max_buys_per_ticker_per_day: int = 1
    target_account_number: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
    broker: BrokerName = BrokerName.ROBINHOOD
    dry_run: bool = True

    # Bars from many market-data APIs are start-stamped. At a 3:20 PM decision,
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


@dataclass
class StrategyState:
    buys_by_ticker_by_day: Dict[str, Dict[date, int]] = field(default_factory=dict)
    submitted_notional_by_day: Dict[date, float] = field(default_factory=dict)
    submitted_notional_by_week: Dict[tuple[int, int], float] = field(default_factory=dict)
    weekly_rollover_reserve_usd: float = 0.0
    selected_tier: Optional[BudgetTier] = None
    finalized_budget_days: Set[date] = field(default_factory=set)
    # ISO-week key the current ``weekly_rollover_reserve_usd`` was computed for.
    # Guards the roll-forward so it runs exactly once per week, independent of the
    # order in which days are finalized vs. queried (a quiet week with no BUY
    # candidates must still roll the reserve before that day is finalized).
    reserve_week_key: Optional[tuple[int, int]] = None

    def buys_today(self, ticker: str, trading_day: date) -> int:
        return self.buys_by_ticker_by_day.get(ticker, {}).get(trading_day, 0)

    def record_buy(self, ticker: str, trading_day: date, notional_usd: float) -> None:
        self.buys_by_ticker_by_day.setdefault(ticker, {})
        self.buys_by_ticker_by_day[ticker][trading_day] = self.buys_today(ticker, trading_day) + 1
        self.submitted_notional_by_day[trading_day] = self.day_notional(trading_day) + notional_usd
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
        self, planned_spend_usd: float, high_multiplier_signal_count: int, config: StrategyConfig
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
                tier.weekly_cap_usd, NORMAL_WEEKLY_CAP_USD + self.weekly_rollover_reserve_usd
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
        return max(0.0, self.allowed_spend_today(trading_day, config) - self.day_notional(trading_day))

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
        list_orders_fn: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None,
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
            info = self._account_info_fn(build_alpaca_account_info_payload(self.config.is_paper_api))
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

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        return True

    def review_market_buy(self, ticker: str, notional_usd: float) -> tuple[bool, str]:
        return True, "alpaca dry-run review approved"

    def build_market_buy_payload(self, ticker: str, notional_usd: float) -> Dict[str, Any]:
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
    """
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


def completed_bar_anchor(decision_dt: datetime, interval_minutes: int, config: StrategyConfig) -> datetime:
    if config.assume_bar_timestamps_are_start_times:
        return decision_dt - timedelta(minutes=interval_minutes)
    return decision_dt


def latest_bar_at_or_before(bars: Iterable[Bar], target: datetime) -> Optional[Bar]:
    eligible = [bar for bar in bars if bar.timestamp <= target]
    return max(eligible, key=lambda bar: bar.timestamp) if eligible else None


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


def graded_buy_multiplier(drop_vs_previous_close: float, config: StrategyConfig) -> float:
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


def evaluate_source_signal(
    ticker: str,
    now: datetime,
    config: StrategyConfig,
    data: MarketDataProvider,
    interval_minutes: int,
) -> SourceSignal:
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()
    decision_dt = datetime.combine(trading_day, config.decision_time_et, tzinfo=EASTERN)
    decision_anchor = completed_bar_anchor(decision_dt, interval_minutes, config)
    lookback_anchor = decision_anchor - timedelta(minutes=config.trend_lookback_minutes)

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
    suggested_amount_usd = config.base_buy_amount_usd * buy_multiplier

    below_previous_close = decision_price < previous_close
    minimum_drop_met = drop_vs_previous_close >= config.minimum_drop_vs_previous_close
    intraday_trend_down = decision_price < lookback_price
    above_previous_close = decision_price > previous_close
    intraday_trend_up = decision_price > lookback_price

    if below_previous_close and minimum_drop_met and intraday_trend_down and buy_multiplier > 0:
        action = Action.BUY
        reason = "below prior close, minimum drop met, 60-minute trend down"
    elif above_previous_close and intraday_trend_up:
        action = Action.SKIP
        reason = "above prior close and 60-minute trend up"
    elif below_previous_close and not minimum_drop_met:
        action = Action.NO_ACTION
        reason = "below prior close but below 0.1% minimum drop"
    elif below_previous_close and not intraday_trend_down:
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
            for name, signal in (("finance", finance_signal), ("robinhood", robinhood_signal))
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
        amount = min(
            finance_signal.suggested_amount_usd,
            robinhood_signal.suggested_amount_usd,
        )
        multiplier = min(finance_signal.buy_multiplier, robinhood_signal.buy_multiplier)
        return ReconciledDecision(
            ticker=ticker,
            action=Action.BUY,
            reason="finance and Robinhood agree on BUY",
            finance_signal=finance_signal,
            robinhood_signal=robinhood_signal,
            buy_multiplier=multiplier,
            suggested_amount_usd=amount,
            **_target_fields(finance_signal),
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
        action=Action.NO_ACTION if Action.NO_DATA not in {finance_action, robinhood_action} else Action.NO_DATA,
        reason=f"no reconciled BUY; finance={finance_action.value}, robinhood={robinhood_action.value}",
        finance_signal=finance_signal,
        robinhood_signal=robinhood_signal,
    )


def within_caps(decision: ReconciledDecision, state: StrategyState, trading_day: date, config: StrategyConfig) -> bool:
    if decision.suggested_amount_usd <= 0:
        return False
    if decision.suggested_amount_usd > state.remaining_spend_today(trading_day, config):
        return False
    return True


def allocation_sort_key(decision: ReconciledDecision) -> Tuple[float, float, str]:
    """Deterministic allocation priority for buy candidates.

    Orders by higher multiplier first, then larger percent drop, then ticker
    alphabetical (A->Z) as the final tie-break so a capped run always fills the
    same names the strategy state promises. Numeric keys are negated (rather than
    sorting with ``reverse=True``) so ticker still sorts ascending.
    """
    drop = max(
        decision.finance_signal.drop_vs_previous_close_pct if decision.finance_signal else 0.0,
        decision.robinhood_signal.drop_vs_previous_close_pct if decision.robinhood_signal else 0.0,
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


def _signal_row(ticker: str, source: str, signal: Optional[SourceSignal]) -> Dict[str, Any]:
    if signal is None:
        return {
            "ticker": ticker,
            "source": source,
            "action": Action.NO_DATA.value,
            "reason": "no signal produced",
            "provider_error": None,
            "decision_bar_close": None,
            "previous_close": None,
            "lookback_price": None,
            "price_vs_previous_close_pct": None,
            "intraday_trend_pct": None,
            "drop_vs_previous_close_pct": None,
            "buy_multiplier": None,
            "decision_bar_timestamp": None,
            "previous_close_timestamp": None,
            "lookback_bar_timestamp": None,
        }
    return {
        "ticker": ticker,
        "source": source,
        "action": signal.action.value,
        "reason": signal.reason,
        "provider_error": signal.provider_error,
        "decision_bar_close": signal.decision_price,
        "previous_close": signal.previous_close,
        "lookback_price": signal.lookback_price,
        "price_vs_previous_close_pct": signal.price_vs_previous_close_pct,
        "intraday_trend_pct": signal.intraday_trend_pct,
        "drop_vs_previous_close_pct": signal.drop_vs_previous_close_pct,
        "buy_multiplier": signal.buy_multiplier,
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
    orders_placed_count = sum(
        1
        for outcome in candidate_outcomes.values()
        if outcome.get("order_id") and outcome.get("status") in {"submitted", "dry_run"}
    )
    return {
        "tickers_scanned": tickers_scanned,
        "finance_calls_attempted": (
            finance_calls_attempted if finance_calls_attempted is not None else tickers_scanned
        ),
        "robinhood_calls_attempted": (
            robinhood_calls_attempted if robinhood_calls_attempted is not None else tickers_scanned
        ),
        "signal_rows_written": len(signal_rows),
        "buy_candidates_written": len(buy_candidates),
        "order_reviews_attempted": (
            order_reviews_attempted if order_reviews_attempted is not None else len(buy_candidates)
        ),
        "orders_placed_count": orders_placed_count,
        # Filled in write_run_artifacts once files are written (approximate proxy).
        "state_file_bytes": None,
        "csv_artifact_count": None,
        "total_artifact_bytes": None,
        "notifications_sent": notifications_sent,
        "errors_count": errors_count,
        "manual_recovery_required": manual_review_count > 0 or errors_count > 0,
        "cost_control_notes": list(cost_control_notes)
        if cost_control_notes is not None
        else list(DAILY_COST_CONTROL_NOTES),
    }


def _derive_order_submit_status(
    config: "StrategyConfig", buy_candidates: Sequence[Dict[str, Any]], errors_count: int
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
    if any(c.get("status") == "not_submitted_confirmation_required" for c in buy_candidates):
        return ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED
    if errors_count > 0:
        return ORDER_SUBMIT_STATUS_ERROR
    return ORDER_SUBMIT_STATUS_BUDGET_EXHAUSTED


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
    manual: bool = False,
) -> Dict[str, Any]:
    """Build the structured run-state dict written by the daily live-run path.

    The artifact captures everything downstream reporting needs: per-source signal
    rows, buy candidates with the target price at signal time and the per-symbol
    planned dollar amount, order ids, skip/block reasons, and budget/reserve fields.

    Also stamps a ``run_classification`` block (via ``scripts/run_classification``)
    so downstream consumers -- ``dca_reporting``'s manual-order exclusion and
    ``dca_risk``'s scheduled-stream deployment accounting -- can tell a scheduled
    live run from a manual one from the artifact content alone. ``manual``
    selects which block is stamped: False (default) is the scheduled 3:20 PM ET
    live task, True is a manual/ad hoc invocation.
    """
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()
    run_id = f"{trading_day.isoformat()}-{now_et.strftime('%H%M%S')}"
    triggered_at_utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        from scripts.run_classification import build_manual_classification, build_scheduled_classification
    except ImportError:
        from run_classification import build_manual_classification, build_scheduled_classification
    run_classification = (
        build_manual_classification(triggered_at_utc=triggered_at_utc)
        if manual
        else build_scheduled_classification(trading_day.isoformat(), triggered_at_utc=triggered_at_utc)
    )

    signal_rows: List[Dict[str, Any]] = []
    for decision in decisions:
        signal_rows.append(_signal_row(decision.ticker, SourceName.FINANCE.value, decision.finance_signal))
        signal_rows.append(_signal_row(decision.ticker, SourceName.ROBINHOOD.value, decision.robinhood_signal))

    buy_candidates: List[Dict[str, Any]] = []
    for decision in decisions:
        if decision.action != Action.BUY:
            continue
        outcome = candidate_outcomes.get(decision.ticker, {})
        target_price = _signal_target_price(decision)
        # A candidate whose amount was reduced by the mid-batch kill-switch cap
        # (see run_once) records the amount actually attempted/submitted, not
        # the pre-cap ladder amount -- dca_risk.deployed_by_day() sums this
        # field from historical artifacts, so an uncapped figure here would
        # both misreport this run and inflate every later run's trailing-7-day
        # deployment total.
        submitted_amount = outcome.get("submitted_amount_usd")
        planned_amount_usd = (
            submitted_amount if submitted_amount is not None else decision.suggested_amount_usd
        )
        buy_candidates.append(
            {
                "ticker": decision.ticker,
                "reason": decision.reason,
                "buy_multiplier": decision.buy_multiplier,
                "planned_amount_usd": round(planned_amount_usd, 2),
                "target_price": target_price,
                "target_price_missing": target_price is None,
                "decision_bar_close": target_price,
                "previous_close": (
                    decision.finance_signal.previous_close
                    if decision.finance_signal is not None
                    else (decision.robinhood_signal.previous_close if decision.robinhood_signal else None)
                ),
                "order_id": outcome.get("order_id"),
                "status": outcome.get("status", "not_submitted"),
                "skip_reason": outcome.get("skip_reason"),
                "reviewed_order": outcome.get("reviewed_order"),
            }
        )

    planned_spend = sum(c["planned_amount_usd"] for c in buy_candidates)
    high_multiplier_signals = sum(
        1 for c in buy_candidates if (c["buy_multiplier"] or 0.0) >= config.high_multiplier_threshold
    )
    if state.selected_tier is None:
        state.selected_tier = select_budget_tier(planned_spend, high_multiplier_signals)
    tier = state.selected_tier
    # Ensure the rollover reserve reflects this trading day's week before the
    # effective weekly cap is reported.
    state.roll_weekly_reserve(trading_day, config)

    return {
        "run_id": run_id,
        "run_classification": run_classification,
        "order_submit_status": _derive_order_submit_status(config, buy_candidates, errors_count),
        "generated_at": now_et.isoformat(),
        "trading_day": trading_day.isoformat(),
        "iso_week": list(iso_week_key(trading_day)),
        "broker": config.broker.value,
        "dry_run": config.dry_run,
        "account_number": None,
        "budget": {
            "base_amount_usd": config.base_buy_amount_usd,
            "base_buy_amount_usd": config.base_buy_amount_usd,
            "budget_tier": tier.name,
            "planned_spend_usd": round(planned_spend, 2),
            "high_multiplier_signal_count": high_multiplier_signals,
            "daily_cap_usd": tier.daily_cap_usd,
            "weekly_cap_usd": round(state.effective_weekly_cap(), 2),
            "weekly_rollover_reserve_usd": round(state.weekly_rollover_reserve_usd, 2),
            "weekly_rollover_reserve_cap_usd": config.weekly_rollover_reserve_cap_usd,
            "allowed_spend_today_usd": round(state.allowed_spend_today(trading_day, config), 2),
            "remaining_spend_today_usd": round(state.remaining_spend_today(trading_day, config), 2),
            "day_notional_usd": round(state.day_notional(trading_day), 2),
            "week_notional_usd": round(state.week_notional(trading_day), 2),
        },
        "risk_guard": {
            "monthly_cash_allocation_usd": config.monthly_cash_allocation_usd,
            "kill_switch_7d_deploy_fraction": config.kill_switch_7d_deploy_fraction,
            "kill_switch_7d_threshold_usd": round(
                config.kill_switch_7d_deploy_fraction * config.monthly_cash_allocation_usd, 2
            ),
            "deployed_last_7d_usd": round(config.deployed_last_7d_usd, 2),
            "kill_switch_engaged": kill_switch_engaged(config),
        },
        "counts": {
            "evaluated": len(decisions),
            "buy_candidates": len(buy_candidates),
            "submitted": sum(1 for c in buy_candidates if c["status"] == "submitted"),
            "skipped": sum(1 for c in buy_candidates if c["status"] not in {"submitted"}),
            "manual_review": sum(1 for d in decisions if d.action == Action.MANUAL_REVIEW),
        },
        "signal_rows": signal_rows,
        "buy_candidates": buy_candidates,
        "manual_review_tickers": [d.ticker for d in decisions if d.action == Action.MANUAL_REVIEW],
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


# CSV artifacts a run may leave beside run_state.json, and the status recorded in
# the minimal run_state stub written when the date gate blocks execution.
_RUN_CSV_NAMES = ("signals.csv", "buy_candidates.csv", "orders.csv")
RUN_STATUS_BLOCKED = "blocked"


def _existing_csv_artifacts(out: Path) -> List[Path]:
    return [out / name for name in _RUN_CSV_NAMES if (out / name).exists()]


def _serialize_run_state(run_state: Dict[str, Any]) -> str:
    return json.dumps(run_state, indent=2, sort_keys=True)


def _settle_usage_bytes(run_state: Dict[str, Any], usage: Dict[str, Any], csv_bytes: int) -> str:
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


def write_run_artifacts(artifacts: Dict[str, Any], output_dir: str | Path) -> Dict[str, str]:
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
    run_state_path.write_text(serialized, encoding="utf-8")

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
    run_state_path.write_text(_settle_usage_bytes(run_state, usage, csv_bytes), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# Mutually exclusive reasons an active-universe ticker did not produce a filled order.
MISSING_FILL_FAILED_TRIGGER = "failed DCA trigger"
MISSING_FILL_BUDGET_CAPPED = "budget capped"
MISSING_FILL_BROKER_BLOCKED = "broker/tradability/order review blocked"
MISSING_FILL_DATA_ERROR = "data error"
MISSING_FILL_EXECUTION_GAP = "execution gap"

MISSING_FILL_REASONS = (
    MISSING_FILL_FAILED_TRIGGER,
    MISSING_FILL_BUDGET_CAPPED,
    MISSING_FILL_BROKER_BLOCKED,
    MISSING_FILL_DATA_ERROR,
    MISSING_FILL_EXECUTION_GAP,
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
) -> None:
    """Fetch fill details for a placed order and append a durable execution record."""
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
        notional_usd=decision.suggested_amount_usd,
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


def classify_missing_fill(decision: Optional[ReconciledDecision], loop_reason: Optional[str]) -> str:
    """Classify why an active-universe ticker produced no filled order.

    ``loop_reason`` is the outcome recorded during the order loop (cap, broker, gap),
    which takes precedence. Otherwise the reconciled action determines the category.
    """
    if loop_reason:
        return loop_reason
    action = decision.action if decision else Action.NO_DATA
    if action == Action.NO_DATA or (decision is not None and _decision_had_provider_error(decision)):
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


def build_report(run_result: RunResult, run_label: str, generated_at: datetime) -> ReportResult:
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

    abs_pcts = sorted(abs(r.slippage_pct) for r in filled if r.slippage_pct is not None)
    reason_counts: Dict[str, int] = {reason: 0 for reason in MISSING_FILL_REASONS}
    for reason in missing.values():
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    metrics: Dict[str, object] = {
        "filled_order_count": len(filled),
        "total_filled_notional": round(
            sum((r.avg_fill_price or 0.0) * (r.quantity_filled or 0.0) for r in filled), 6
        ),
        "net_slippage_cost": round(sum(r.slippage_cost or 0.0 for r in filled), 6),
        "mean_abs_slippage_pct": round(statistics.fmean(abs_pcts), 6) if abs_pcts else 0.0,
        "median_abs_slippage_pct": round(statistics.median(abs_pcts), 6) if abs_pcts else 0.0,
        "p90_abs_slippage_pct": round(_percentile(abs_pcts, 90.0), 6),
        "top_slippage_tickers": top_tickers,
        "missing_fill_count": len(missing),
        "missing_fill_reasons": {k: v for k, v in reason_counts.items() if v},
    }

    return ReportResult(
        generated_at=generated_at,
        run_label=run_label,
        ranked_slippage=ranked,
        top_slippage_tickers=top_tickers,
        missing_fills=missing,
        metrics=metrics,
    )


_CSV_FIELDS = [
    "ticker", "status", "order_id", "notional_usd", "action_reason",
    "target_price", "target_price_source", "target_quote_time", "decision_bar_close",
    "order_submitted_at", "order_filled_at", "avg_fill_price", "quantity_filled",
    "slippage_abs", "slippage_pct", "slippage_cost", "latency_seconds", "execution_quality",
]


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def write_execution_csv(records: Sequence[OrderExecutionRecord], path: str) -> str:
    """Write the durable per-order execution/slippage log to CSV."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for record in records:
            row = {field_name: _iso(getattr(record, field_name)) for field_name in _CSV_FIELDS}
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
    lines.append("| Rank | Ticker | Target | Avg Fill | Slippage % | Slippage $ | Latency s | Quality |")
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
    run_label: str,
    output_dir: str,
    now: Optional[datetime] = None,
) -> ReportResult:
    """Build and persist the slippage/coverage report + durable CSV log.

    Reusable after manual or scheduled execution. Performs no interactive prompting.
    """
    generated_at = (now or datetime.now(tz=EASTERN)).astimezone(EASTERN)
    report = build_report(run_result, run_label, generated_at)
    stamp = generated_at.strftime("%Y%m%d-%H%M%S")
    base = f"dca_{run_label}_{run_result.trading_day.isoformat()}_{stamp}"
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"{base}_orders.csv")
    report_path = os.path.join(output_dir, f"{base}_slippage_coverage.md")
    write_execution_csv(run_result.execution_records, csv_path)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(render_report_markdown(report, run_result))
    report.csv_path = csv_path
    report.report_path = report_path
    return report


def write_run_state(
    state_path: str,
    *,
    run_label: str,
    scheduled: bool,
    run_result: RunResult,
    report: ReportResult,
    config: StrategyConfig,
    state: StrategyState,
) -> str:
    """Write the manual/scheduled run-state JSON including artifact paths + metrics."""
    trading_day = run_result.trading_day
    payload = {
        "run_label": run_label,
        "scheduled": scheduled,
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
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return state_path


def run_once(
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
    deployment_history_root: Optional[str | Path] = None,
    scheduled: bool = True,
    classification_state_root: Optional[str | Path] = None,
) -> List[ReconciledDecision]:
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()
    if run_result is not None:
        run_result.trading_day = trading_day

    errors_count = 0
    order_reviews_attempted = 0

    if trading_day not in config.approved_test_dates:
        logger.warning("Date gate blocked execution for %s. No trades.", trading_day)
        if artifacts_dir:
            out = Path(artifacts_dir)
            out.mkdir(parents=True, exist_ok=True)
            state_path = out / "run_state.json"
            # Never clobber a previously recorded run when a directory is reused;
            # the stub only marks days that produced no artifact at all.
            if not state_path.exists():
                state_path.write_text(
                    _serialize_run_state(
                        {"trading_day": trading_day.isoformat(), "status": RUN_STATUS_BLOCKED}
                    ),
                    encoding="utf-8",
                )
        return []

    if broker.account_number() != config.target_account_number:
        raise RuntimeError(
            f"Refusing to trade account {broker.account_number()}; expected {config.target_account_number}"
        )

    # Inject the trailing-7-day scheduled-live deployment from history so the
    # kill-switch in graded_buy_multiplier reflects real cash burn. Imported
    # lazily to avoid a circular import (dca_risk imports from this module).
    if deployment_history_root is not None:
        from dca_risk import apply_risk_guard, deployed_by_day, find_run_state_files

        prior_states = []
        for path in find_run_state_files(deployment_history_root):
            try:
                with path.open(encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, OSError):
                continue
            if isinstance(data, dict) and data.get("status") != RUN_STATUS_BLOCKED:
                prior_states.append(data)
        config = apply_risk_guard(config, deployed_by_day(prior_states), trading_day)

    decisions: List[ReconciledDecision] = []
    tradable_tickers = [ticker for ticker in config.tickers if ticker not in config.excluded_tickers]
    if run_result is not None:
        run_result.active_universe = list(tradable_tickers)

    def _safe_signal(ticker: str, data: MarketDataProvider, interval_minutes: int) -> SourceSignal:
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
            errors_count += 1
            return SourceSignal(
                ticker=ticker,
                source=getattr(data, "source", SourceName.FINANCE),
                action=Action.NO_DATA,
                reason=f"provider error: {exc}",
                provider_error=True,
            )

    for ticker in tradable_tickers:
        finance_signal = _safe_signal(ticker, finance_data, config.finance_interval_minutes)
        robinhood_signal = _safe_signal(ticker, robinhood_data, config.robinhood_interval_minutes)
        decisions.append(reconcile_signals(ticker, finance_signal, robinhood_signal))

    if run_result is not None:
        run_result.decisions = decisions

    def _block(ticker: str, reason: str, message: str) -> None:
        if run_result is not None:
            run_result.loop_block_reasons[ticker] = reason
        logger.info(message)

    buy_candidates = [decision for decision in decisions if decision.action == Action.BUY]
    # Higher multiplier -> larger drop -> ticker alphabetical (see allocation_sort_key).
    buy_candidates.sort(key=allocation_sort_key)

    planned_spend = sum(d.suggested_amount_usd for d in buy_candidates)
    high_multiplier_signals = sum(
        1 for d in buy_candidates if d.buy_multiplier >= config.high_multiplier_threshold
    )
    state.select_daily_budget(planned_spend, high_multiplier_signals, config)
    # Roll the reserve forward now, before any budget query or day finalization,
    # so a quiet week (no BUY candidates) still carries prior unused Normal-cap
    # capacity into the stress tier's ceiling.
    state.roll_weekly_reserve(trading_day, config)

    candidate_outcomes: Dict[str, Dict[str, Any]] = {}
    # Tracks the trailing-7-day scheduled-live deployment total live as orders
    # are submitted within this batch (starting from the prior-history figure
    # injected above), so a volatile day that crosses the kill-switch threshold
    # mid-run caps the REMAINING candidates in the same batch to the 1x base
    # amount instead of only affecting tomorrow's run.
    running_deployed_7d = config.deployed_last_7d_usd

    for decision in buy_candidates:
        ticker = decision.ticker
        order_amount = decision.suggested_amount_usd
        if kill_switch_engaged(replace(config, deployed_last_7d_usd=running_deployed_7d)):
            order_amount = min(order_amount, config.base_buy_amount_usd)

        if state.buys_today(ticker, trading_day) >= config.max_buys_per_ticker_per_day:
            candidate_outcomes[ticker] = {"status": "skipped", "skip_reason": "already bought today"}
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: already bought today; skipping")
            continue
        if broker.same_day_strategy_order_exists(ticker, trading_day):
            candidate_outcomes[ticker] = {"status": "skipped", "skip_reason": "same-day broker order exists"}
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: same-day broker order already exists; skipping")
            continue
        if not within_caps(decision, state, trading_day, config):
            candidate_outcomes[ticker] = {"status": "blocked", "skip_reason": "cap would be exceeded"}
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: cap would be exceeded; skipping")
            continue
        if broker.available_buying_power() < order_amount:
            candidate_outcomes[ticker] = {"status": "blocked", "skip_reason": "insufficient buying power"}
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: insufficient buying power; skipping")
            continue
        if not broker.is_tradable_for_fractional_buy(ticker):
            candidate_outcomes[ticker] = {"status": "blocked", "skip_reason": "not tradable for fractional buy"}
            _block(ticker, MISSING_FILL_BROKER_BLOCKED, f"{ticker}: not tradable for fractional/notional buy; skipping")
            continue

        order_reviews_attempted += 1
        review_ok, review_reason = broker.review_market_buy(ticker, order_amount)
        if not review_ok:
            candidate_outcomes[ticker] = {"status": "blocked", "skip_reason": f"review blocked: {review_reason}"}
            _block(ticker, MISSING_FILL_BROKER_BLOCKED, f"{ticker}: review blocked order: {review_reason}")
            continue

        # Populated whenever review passed, regardless of the eventual submission
        # outcome, so a confirmation-blocked or failed placement still records
        # the order details that were reviewed (schemas/run_state.schema.json).
        reviewed_order = {
            "notional_usd": round(order_amount, 2),
            "side": "buy",
            "order_type": "market",
            "time_in_force": None,
            "estimated_price": None,
            "review_passed": review_ok,
        }

        ref_id = f"graded-dca-{trading_day.isoformat()}-{ticker}"
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
            candidate_outcomes[ticker] = {
                "status": "dry_run",
                "order_id": order_id,
                "skip_reason": "dry_run: order not placed",
                "reviewed_order": reviewed_order,
                "submitted_amount_usd": order_amount,
            }
        else:
            try:
                order_id = broker.place_market_buy(ticker, order_amount, ref_id=ref_id)
            except OrderConfirmationRequiredError:
                # Review passed but the broker requires additional post-review
                # confirmation before it will submit -- not an error: no order
                # was attempted, and this run remains valid scheduled-run
                # evidence that it executed and passed review.
                candidate_outcomes[ticker] = {
                    "status": "not_submitted_confirmation_required",
                    "order_id": None,
                    "skip_reason": "broker requires post-review confirmation before submission",
                    "reviewed_order": reviewed_order,
                    "submitted_amount_usd": order_amount,
                }
                _block(
                    ticker,
                    MISSING_FILL_BROKER_BLOCKED,
                    f"{ticker}: broker requires confirmation before submission; skipping",
                )
                continue
            except Exception as exc:  # noqa: BLE001 - isolate one bad placement from the rest
                # A failed placement (no order id, terminal/failed state, wrapper error) must
                # not abort the whole run: earlier successful orders would then lose their
                # durable run_state/orders.csv record. Record the failure and keep going so
                # artifacts are always written and other candidates still get a chance.
                errors_count += 1
                candidate_outcomes[ticker] = {
                    "status": "failed",
                    "order_id": None,
                    "skip_reason": f"placement failed: {exc}",
                    "reviewed_order": reviewed_order,
                    "submitted_amount_usd": order_amount,
                }
                logger.error("Placement failed for %s: %s", ticker, exc)
                continue
            state.record_buy(ticker, trading_day, order_amount)
            running_deployed_7d += order_amount
            logger.info(
                "Placed buy for %s: $%.2f, order_id=%s",
                ticker,
                order_amount,
                order_id,
            )
            candidate_outcomes[ticker] = {
                "status": "submitted",
                "order_id": order_id,
                "skip_reason": None,
                "reviewed_order": reviewed_order,
                "submitted_amount_usd": order_amount,
            }

        if run_result is not None:
            _log_execution(run_result, decision, order_id, broker)

    manual_review = [d for d in decisions if d.action == Action.MANUAL_REVIEW]
    if manual_review:
        logger.info("Manual review tickers: %s", ", ".join(d.ticker for d in manual_review))

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

    if artifacts_dir is not None:
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
            manual=not scheduled,
        )
        paths = write_run_artifacts(artifacts, artifacts_dir)
        logger.info("Wrote run artifacts: %s", paths["run_state"])

        if scheduled and classification_state_root is not None:
            # Update the latest_scheduled_run.json pointer that reporting/the
            # watchdog rely on to find the scheduled run -- without this, a
            # correctly-classified run_state is still invisible to them. Reuse
            # write_run_artifacts' own directory (rather than recomputing one
            # from classification_state_root) so this never creates a second
            # copy of run_state.json in a different location.
            #
            # Gated on an explicit classification_state_root (mirroring the
            # deployment_history_root pattern used elsewhere in this module)
            # rather than defaulting to scripts.run_classification's own
            # production STATE_ROOT: a caller that doesn't pass one (including
            # every existing test that predates this feature) must never
            # silently write to a real absolute filesystem path.
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
            if result:
                update_latest_scheduled_run_pointer(
                    artifacts, paths["run_state"], classification_state_root
                )
            else:
                logger.error(
                    "Not updating latest_scheduled_run.json: invalid run_classification (%s)",
                    "; ".join(result.errors),
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
    run_result = RunResult(trading_day=now.astimezone(EASTERN).date(), decisions=[])
    run_once(
        now, config, state, finance_data, robinhood_data, broker,
        run_result=run_result, deployment_history_root=deployment_history_root,
        scheduled=scheduled,
    )
    report = generate_slippage_report(
        run_result, run_label=run_label, output_dir=output_dir, now=now
    )
    written_state = write_run_state(
        state_path,
        run_label=run_label,
        scheduled=scheduled,
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


def run_daily_compact(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    *,
    artifacts_dir: str | Path,
    run_label: str = "daily",
    scheduled: bool = True,
    notifications_sent: Optional[int] = None,
    deployment_history_root: Optional[str | Path] = None,
    classification_state_root: Optional[str | Path] = None,
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
    """
    trading_day = now.astimezone(EASTERN).date()
    run_result = RunResult(trading_day=trading_day, decisions=[])
    run_once(
        now,
        config,
        state,
        finance_data,
        robinhood_data,
        broker,
        artifacts_dir=artifacts_dir,
        run_result=run_result,
        notifications_sent=notifications_sent,
        deployment_history_root=deployment_history_root,
        scheduled=scheduled,
        classification_state_root=classification_state_root,
    )

    out = Path(artifacts_dir)
    out.mkdir(parents=True, exist_ok=True)
    result: Dict[str, object] = {
        "run_label": run_label,
        "scheduled": scheduled,
        "compact": True,
        "blocked": trading_day not in config.approved_test_dates,
        "artifacts_dir": str(out),
        "run_state_path": str(out / "run_state.json"),
    }
    if result["blocked"]:
        # Date gate tripped inside run_once: only the blocked run_state stub was
        # written, so return no CSV paths rather than paths to missing files.
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
