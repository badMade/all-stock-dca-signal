"""
Near-close graded DCA strategy skeleton.

This mirrors the scheduled Robinhood live-test rules:
    - Run at 3:45 PM America/New_York.
    - Use completed regular-session bars only.
    - Compare finance-side and Robinhood-side signals before auto-ordering.
    - Place real orders only through a broker adapter that reviews first.
    - Never trade outside the explicit caps, account, universe, and test dates.

The file is adapter-based and dry-run by default. Fill in the FinanceDataProvider,
RobinhoodDataProvider, and RobinhoodBrokerAdapter implementations with your MCP/API
calls before using it live.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Optional, Protocol, Sequence, Set
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")

ROBINHOOD_AGENTIC_ACCOUNT_NUMBER = "562057042"

ACTIVE_TICKERS = [
    "DFTX", "TRIN", "AAAU", "SPCX", "TRLV", "RVI", "VCX", "EVR", "LUNR", "IRDM",
    "DXYZ", "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD", "CBRS",
    "MRVL", "BTX", "STM", "SNDK", "WDC", "TSM", "AVGO", "NVDA", "QBTS", "FLKR",
    "INTC", "GOOGL", "GOOG", "AMZN", "GLW", "MA", "GS", "MU", "NFLX", "XLG",
    "TOPT", "QTOP", "SATA", "RGTI", "IBM",
]

EXCLUDED_TICKERS = {"BYDDY"}

APPROVED_TEST_DATES = {
    date(2026, 6, 29),
    date(2026, 6, 30),
    date(2026, 7, 1),
}


class Action(str, Enum):
    BUY = "BUY"
    SKIP = "SKIP"
    NO_ACTION = "NO_ACTION"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NO_DATA = "NO_DATA"


class SourceName(str, Enum):
    FINANCE = "finance"
    ROBINHOOD = "robinhood"


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
class StrategyConfig:
    tickers: Sequence[str] = tuple(ACTIVE_TICKERS)
    excluded_tickers: frozenset[str] = frozenset(EXCLUDED_TICKERS)
    approved_test_dates: frozenset[date] = frozenset(APPROVED_TEST_DATES)
    decision_time_et: time = time(15, 45)
    trend_lookback_minutes: int = 60
    base_buy_amount_usd: float = 1.0
    daily_base_cap_usd: float = 60.0
    reserve_cap_usd: float = 420.0
    hard_daily_max_usd: float = 120.0
    weekly_notional_cap_usd: float = 600.0
    # Backward-compatible alias for older callers. New budget logic uses
    # daily_base_cap_usd + carry_forward_reserve_usd, capped by hard_daily_max_usd.
    daily_notional_cap_usd: float = 60.0
    minimum_drop_vs_previous_close: float = 0.001
    max_buys_per_ticker_per_day: int = 1
    target_account_number: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
    dry_run: bool = True

    # Bars from many market-data APIs are start-stamped. At a 3:45 PM decision,
    # the latest completed 15-minute or 30-minute start-stamped bar is 3:30 PM.
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


@dataclass
class StrategyState:
    buys_by_ticker_by_day: Dict[str, Dict[date, int]] = field(default_factory=dict)
    submitted_notional_by_day: Dict[date, float] = field(default_factory=dict)
    submitted_notional_by_week: Dict[tuple[int, int], float] = field(default_factory=dict)
    carry_forward_reserve_usd: float = 0.0
    finalized_budget_days: Set[date] = field(default_factory=set)

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

    def allowed_spend_today(self, trading_day: date, config: StrategyConfig) -> float:
        """Return the max strategy spend allowed today under reserve + weekly caps."""
        remaining_weekly = max(0.0, config.weekly_notional_cap_usd - self.week_notional(trading_day))
        budget_with_reserve = config.daily_base_cap_usd + self.carry_forward_reserve_usd
        return max(0.0, min(config.hard_daily_max_usd, budget_with_reserve, remaining_weekly))

    def remaining_spend_today(self, trading_day: date, config: StrategyConfig) -> float:
        """Return remaining strategy spend after same-day submitted notional."""
        return max(0.0, self.allowed_spend_today(trading_day, config) - self.day_notional(trading_day))

    def finalize_daily_budget(self, trading_day: date, config: StrategyConfig) -> None:
        """Carry unused base capacity forward and draw reserve used above the base cap.

        The reserve bucket is updated once per trading day:
            reserve_next = min(reserve_cap, reserve_start - reserve_draw + unused_base)

        where:
            unused_base = max(0, daily_base_cap - actual_spend)
            reserve_draw = max(0, actual_spend - daily_base_cap)

        This lets calm days build reserve while red days spend above the $60 base,
        but never above the hard daily max or weekly cap.
        """
        if trading_day in self.finalized_budget_days:
            return
        day_spend = self.day_notional(trading_day)
        unused_base = max(0.0, config.daily_base_cap_usd - day_spend)
        reserve_draw = min(self.carry_forward_reserve_usd, max(0.0, day_spend - config.daily_base_cap_usd))
        self.carry_forward_reserve_usd = min(
            config.reserve_cap_usd,
            max(0.0, self.carry_forward_reserve_usd - reserve_draw + unused_base),
        )
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
        """Place a notional regular-hours market buy and return an order id."""

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


def graded_buy_multiplier(drop_vs_previous_close: float, config: StrategyConfig) -> float:
    """Map a drop from prior close to a buy multiplier via the graded ladder.

    ``drop_vs_previous_close`` is a fraction (0.02 == 2.0% down). The largest tier
    threshold less than or equal to the drop determines the multiplier; a drop below
    the first threshold returns 0.0 (no buy). See StrategyConfig.sizing_tiers.
    """
    multiplier = 0.0
    for threshold, tier_multiplier in sorted(config.sizing_tiers):
        if drop_vs_previous_close >= threshold:
            multiplier = tier_multiplier
    return min(multiplier, 5.0)


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
        if quote is not None:
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
    fill: Optional[OrderFill] = None
    get_fill = getattr(broker, "get_order_fill", None)
    if callable(get_fill):
        fill = get_fill(order_id)

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
    if action == Action.NO_DATA:
        return MISSING_FILL_DATA_ERROR
    # SKIP / NO_ACTION / MANUAL_REVIEW: no reconciled auto-buy was triggered.
    return MISSING_FILL_FAILED_TRIGGER


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
            "day_spend": state.day_notional(trading_day),
            "week_spend": state.week_notional(trading_day),
            "reserve": state.carry_forward_reserve_usd,
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
    run_result: Optional[RunResult] = None,
) -> List[ReconciledDecision]:
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()
    if run_result is not None:
        run_result.trading_day = trading_day

    if trading_day not in config.approved_test_dates:
        print(f"Date gate blocked execution for {trading_day}. No trades.")
        return []

    if broker.account_number() != config.target_account_number:
        raise RuntimeError(
            f"Refusing to trade account {broker.account_number()}; expected {config.target_account_number}"
        )

    decisions: List[ReconciledDecision] = []
    tradable_tickers = [ticker for ticker in config.tickers if ticker not in config.excluded_tickers]
    if run_result is not None:
        run_result.active_universe = list(tradable_tickers)

    for ticker in tradable_tickers:
        finance_signal = evaluate_source_signal(
            ticker=ticker,
            now=now,
            config=config,
            data=finance_data,
            interval_minutes=config.finance_interval_minutes,
        )
        robinhood_signal = evaluate_source_signal(
            ticker=ticker,
            now=now,
            config=config,
            data=robinhood_data,
            interval_minutes=config.robinhood_interval_minutes,
        )
        decisions.append(reconcile_signals(ticker, finance_signal, robinhood_signal))

    if run_result is not None:
        run_result.decisions = decisions

    def _block(ticker: str, reason: str, message: str) -> None:
        if run_result is not None:
            run_result.loop_block_reasons[ticker] = reason
        print(message)

    buy_candidates = [decision for decision in decisions if decision.action == Action.BUY]
    buy_candidates.sort(
        key=lambda d: (
            d.buy_multiplier,
            max(
                d.finance_signal.drop_vs_previous_close_pct if d.finance_signal else 0.0,
                d.robinhood_signal.drop_vs_previous_close_pct if d.robinhood_signal else 0.0,
            ),
        ),
        reverse=True,
    )

    for decision in buy_candidates:
        ticker = decision.ticker

        if state.buys_today(ticker, trading_day) >= config.max_buys_per_ticker_per_day:
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: already bought today; skipping")
            continue
        if broker.same_day_strategy_order_exists(ticker, trading_day):
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: same-day broker order already exists; skipping")
            continue
        if not within_caps(decision, state, trading_day, config):
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: cap would be exceeded; skipping")
            continue
        if broker.available_buying_power() < decision.suggested_amount_usd:
            _block(ticker, MISSING_FILL_BUDGET_CAPPED, f"{ticker}: insufficient buying power; skipping")
            continue
        if not broker.is_tradable_for_fractional_buy(ticker):
            _block(ticker, MISSING_FILL_BROKER_BLOCKED, f"{ticker}: not tradable for fractional/notional buy; skipping")
            continue

        review_ok, review_reason = broker.review_market_buy(ticker, decision.suggested_amount_usd)
        if not review_ok:
            _block(ticker, MISSING_FILL_BROKER_BLOCKED, f"{ticker}: review blocked order: {review_reason}")
            continue

        ref_id = f"graded-dca-{trading_day.isoformat()}-{ticker}"
        if config.dry_run:
            print(f"[DRY RUN] Would buy {ticker}: ${decision.suggested_amount_usd:.2f} ({decision.reason})")
            if isinstance(broker, DryRunBroker):
                order_id = broker.place_market_buy(ticker, decision.suggested_amount_usd, ref_id=ref_id)
            else:
                order_id = f"DRY-RUN-SIM-{ticker}-{decision.suggested_amount_usd:.2f}-{ref_id}"
        else:
            order_id = broker.place_market_buy(ticker, decision.suggested_amount_usd, ref_id=ref_id)
            state.record_buy(ticker, trading_day, decision.suggested_amount_usd)
            print(f"Placed buy for {ticker}: ${decision.suggested_amount_usd:.2f}, order_id={order_id}")

        if run_result is not None:
            _log_execution(run_result, decision, order_id, broker)

    manual_review = [d for d in decisions if d.action == Action.MANUAL_REVIEW]
    if manual_review:
        print("Manual review tickers:", ", ".join(d.ticker for d in manual_review))

    state.finalize_daily_budget(trading_day, config)
    print(
        f"Budget summary: spent ${state.day_notional(trading_day):.2f}; "
        f"reserve now ${state.carry_forward_reserve_usd:.2f}; "
        f"weekly spent ${state.week_notional(trading_day):.2f}."
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
) -> Dict[str, object]:
    """Execute one DCA run and always emit a slippage/coverage report + state file.

    Used by both manual and scheduled runs. This path is fully non-interactive: it
    never prompts for confirmation, so scheduled/background execution is unaffected.
    Returns the artifact paths and summary metrics.
    """
    run_result = RunResult(trading_day=now.astimezone(EASTERN).date(), decisions=[])
    run_once(now, config, state, finance_data, robinhood_data, broker, run_result=run_result)
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


if __name__ == "__main__":
    raise SystemExit(
        "Wire FinanceDataProvider, RobinhoodDataProvider, and RobinhoodBrokerAdapter "
        "before running. Keep dry_run=True until adapter tests pass."
    )
