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

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ReconciledDecision:
    ticker: str
    action: Action
    reason: str
    finance_signal: Optional[SourceSignal]
    robinhood_signal: Optional[SourceSignal]
    buy_multiplier: float = 0.0
    suggested_amount_usd: float = 0.0


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


class DryRunBroker:
    def __init__(self, cash: float = 300.0, account: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER) -> None:
        self._cash = cash
        self._account = account

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
        return f"DRY-RUN-{ticker}-{notional_usd:.2f}-{ref_id}"


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
    )


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


def run_once(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
) -> List[ReconciledDecision]:
    now_et = now.astimezone(EASTERN)
    trading_day = now_et.date()

    if trading_day not in config.approved_test_dates:
        print(f"Date gate blocked execution for {trading_day}. No trades.")
        return []

    if broker.account_number() != config.target_account_number:
        raise RuntimeError(
            f"Refusing to trade account {broker.account_number()}; expected {config.target_account_number}"
        )

    decisions: List[ReconciledDecision] = []
    tradable_tickers = [ticker for ticker in config.tickers if ticker not in config.excluded_tickers]

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
            print(f"{ticker}: already bought today; skipping")
            continue
        if broker.same_day_strategy_order_exists(ticker, trading_day):
            print(f"{ticker}: same-day broker order already exists; skipping")
            continue
        if not within_caps(decision, state, trading_day, config):
            print(f"{ticker}: cap would be exceeded; skipping")
            continue
        if broker.available_buying_power() < decision.suggested_amount_usd:
            print(f"{ticker}: insufficient buying power; skipping")
            continue
        if not broker.is_tradable_for_fractional_buy(ticker):
            print(f"{ticker}: not tradable for fractional/notional buy; skipping")
            continue

        review_ok, review_reason = broker.review_market_buy(ticker, decision.suggested_amount_usd)
        if not review_ok:
            print(f"{ticker}: review blocked order: {review_reason}")
            continue

        ref_id = f"graded-dca-{trading_day.isoformat()}-{ticker}"
        if config.dry_run:
            print(f"[DRY RUN] Would buy {ticker}: ${decision.suggested_amount_usd:.2f} ({decision.reason})")
        else:
            order_id = broker.place_market_buy(ticker, decision.suggested_amount_usd, ref_id=ref_id)
            state.record_buy(ticker, trading_day, decision.suggested_amount_usd)
            print(f"Placed buy for {ticker}: ${decision.suggested_amount_usd:.2f}, order_id={order_id}")

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


if __name__ == "__main__":
    raise SystemExit(
        "Wire FinanceDataProvider, RobinhoodDataProvider, and RobinhoodBrokerAdapter "
        "before running. Keep dry_run=True until adapter tests pass."
    )
