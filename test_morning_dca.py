"""Self-tests for the morning graded-DCA executor (watchlist scan + live).

Covers the morning ladder threshold brackets, the separate budget ledger
namespace, the run-state path/classification for BOTH the 9:45 watchlist scan
and the 10:30 live execution, the live-mode confidence rule, and the guarantee
that a morning run never interacts with the afternoon ce16ba6b scheduled
evidence.

Runnable with ``pytest`` or ``python test_morning_dca.py``.
"""

import csv
import os

# chatgpt-codex-connector review on PR #54: conftest.py's
# ``os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", ...)`` only runs
# when pytest collects this module, so ``python test_morning_dca.py`` (the
# standalone runner documented above) bypasses it entirely. near_close_dca
# reads the env var at import time, so the default must be set here, before
# ``from morning_dca import ...`` below triggers that import, or the
# standalone run picks up the unset-account sentinel instead of the fixture
# value test_run_state_path_and_classification asserts against. This mirrors
# conftest.py's setdefault exactly, so it's a no-op under pytest (conftest.py
# already ran first) and only matters for the standalone path.
os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "562057042")

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from morning_dca import (
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    DEFAULT_MORNING_PREFLIGHT_STATE_ROOT,
    DEFAULT_MORNING_STATE_ROOT,
    DEFAULT_MORNING_WATCHLIST_STATE_ROOT,
    EXEC_AUTO_BUY_ELIGIBLE,
    EXEC_MANUAL_REVIEW_CANDIDATE,
    MORNING_CRON_ID,
    MORNING_LIVE_CRON_ID,
    MORNING_LIVE_MODE,
    MORNING_PREFLIGHT_CRON_ID,
    MORNING_PREFLIGHT_MODE,
    MORNING_SIZING_TIERS,
    DCAExecutor,
    MorningConfig,
    MorningDCAExecutor,
    MorningLedger,
    MorningSignal,
    TopUpOutcome,
    _PersistingBrokerAdapter,
    _PersistingMarketDataProvider,
    _RawInputRecorder,
    _same_stream_order_exists,
    load_morning_config,
    load_same_day_preflight,
    nyse_holidays,
    preflight_eligibility,
    preflight_ticker_eligibility_row,
)
from near_close_dca import (
    EASTERN,
    Action,
    Bar,
    DryRunBroker,
    PriorSameDayAllocation,
    SourceName,
    _nth_weekday_of_month,
)


def _now_live():
    """Decision timestamp for the 10:30 AM ET live execution mode."""
    return datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=EASTERN)


class FixedDropProvider:
    """Deterministic provider: each ticker drops a fixed fraction vs. prior close."""

    def __init__(self, source: SourceName, previous_close: float, drops: dict) -> None:
        self.source = source
        self._prev = previous_close
        self._drops = drops

    def get_previous_close_bar(self, ticker: str, as_of: datetime):
        ts = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        p = self._prev
        return Bar(timestamp=ts, open=p, high=p, low=p, close=p, volume=0.0)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        bars = []
        cursor = start
        while cursor <= end:
            bars.append(Bar(timestamp=cursor, open=price, high=price, low=price, close=price, volume=100.0))
            cursor += timedelta(minutes=interval_minutes)
        return bars


class DecliningBarsProvider(FixedDropProvider):
    """Provider that generates strictly declining intraday bars.

    Bars start at the midpoint between previous close and the final dropped price,
    declining linearly to the final price. This satisfies the strict downward-trend
    requirement in `_trend_validated` (last close < first close).
    """

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        drop = self._drops.get(ticker, 0.0)
        final_price = self._prev * (1.0 - drop)
        start_price = self._prev * (1.0 - drop * 0.5) if drop > 0 else final_price
        timestamps = []
        cursor = start
        while cursor <= end:
            timestamps.append(cursor)
            cursor += timedelta(minutes=interval_minutes)
        n = len(timestamps)
        # Interpolate so the bar at end-interval_minutes (the completed-bar anchor
        # returned by latest_bar_at_or_before) is exactly final_price.  The last
        # bar (at end) is also set to final_price.
        anchor_idx = n - 2 if n >= 2 else 0
        bars = []
        for i, ts in enumerate(timestamps):
            if i <= anchor_idx and anchor_idx > 0:
                price = start_price + (final_price - start_price) * i / anchor_idx
            else:
                price = final_price
            bars.append(Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=100.0))
        return bars


class RisingBarsProvider(FixedDropProvider):
    """Provider that ends below prior close but rises across the live trend window."""

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        drop = self._drops.get(ticker, 0.0)
        final_price = self._prev * (1.0 - drop)
        start_price = final_price * 0.97
        timestamps = []
        cursor = start
        while cursor <= end:
            timestamps.append(cursor)
            cursor += timedelta(minutes=interval_minutes)
        anchor_idx = max(0, len(timestamps) - 2)
        bars = []
        for i, ts in enumerate(timestamps):
            if i <= anchor_idx and anchor_idx > 0:
                price = start_price + (final_price - start_price) * i / anchor_idx
            else:
                price = final_price
            bars.append(Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=100.0))
        return bars


class FakeLiveBroker:
    """Minimal successful broker for scheduled live tests."""

    def account_number(self):
        from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self):
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        # No duplicate: this fake broker never has prior orders on file, so
        # guarded top-ups always proceed as fresh placements.
        return None

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        return f"ORDER-{ticker}"


def _now():
    return datetime.combine(date(2026, 7, 8), time(9, 45), tzinfo=EASTERN)


# --- NYSE holiday calendar ---------------------------------------------------
def test_nyse_holidays_2026_matches_documented_observed_dates():
    holidays = nyse_holidays(2026)
    assert date(2026, 1, 1) in holidays  # New Year's Day
    assert date(2026, 1, 19) in holidays  # MLK Day
    assert date(2026, 2, 16) in holidays  # Washington's Birthday
    assert date(2026, 4, 3) in holidays  # Good Friday
    assert date(2026, 5, 25) in holidays  # Memorial Day
    assert date(2026, 6, 19) in holidays  # Juneteenth
    # Independence Day (Jul 4) falls on a Saturday in 2026 -> observed Friday,
    # matching near_close_dca.MARKET_HOLIDAYS' hand-documented value.
    assert date(2026, 7, 3) in holidays
    assert date(2026, 9, 7) in holidays  # Labor Day
    assert date(2026, 11, 26) in holidays  # Thanksgiving
    assert date(2026, 12, 25) in holidays  # Christmas
    assert len(holidays) == 10


def test_nyse_holidays_shifts_weekend_fixed_dates():
    # 2027: Jul 4 falls on a Sunday -> observed Monday Jul 5.
    assert date(2027, 7, 5) in nyse_holidays(2027)
    assert date(2027, 7, 4) not in nyse_holidays(2027)


# --- Threshold brackets -----------------------------------------------------
def test_ladder_brackets():
    # As of the Jul 10 2026 live-automation update the morning live stream uses
    # the shared $2-base 0x-to-5x ladder (was 0x-to-3x, now historical). This
    # test only asserts multiplier brackets, not dollar amounts, so it uses the
    # default base_buy_amount_usd rather than overriding it.
    ex = MorningDCAExecutor(MorningConfig())
    cases = {
        0.0005: 0.0,   # < 0.1% -> 0x
        0.001: 1.0,    # 0.1%   -> 1x
        0.009: 1.0,    # < 1%   -> 1x
        0.010: 1.5,    # 1%     -> 1.5x
        0.020: 2.0,    # 2%     -> 2x
        0.030: 2.5,    # 3%     -> 2.5x
        0.040: 3.0,    # 4%     -> 3x
        0.050: 3.5,    # 5%     -> 3.5x
        0.060: 4.0,    # 6%     -> 4x
        0.070: 4.5,    # 7%     -> 4.5x
        0.080: 5.0,    # 8%     -> 5x
        0.20: 5.0,     # >= 8%  -> 5x (no upper cutoff)
    }
    for drop, expected in cases.items():
        assert ex.multiplier_for_drop(drop) == expected, (drop, expected)


def test_ladder_matches_afternoon_live():
    # All live streams (morning, midday, afternoon) share one 0x-to-5x ladder.
    from near_close_dca import StrategyConfig, graded_buy_multiplier

    morning = MorningDCAExecutor(MorningConfig())
    assert morning.multiplier_for_drop(0.08) == 5.0
    assert graded_buy_multiplier(0.08, StrategyConfig()) == 5.0
    assert MORNING_SIZING_TIERS == StrategyConfig().sizing_tiers


def test_signal_amounts_and_actions():
    cfg = MorningConfig(tickers=("UP", "FLAT", "TINY", "SMALL", "BIG"), base_buy_amount_usd=2.0)
    drops = {"UP": -0.01, "FLAT": 0.0, "TINY": 0.0005, "SMALL": 0.03, "BIG": 0.10}
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, drops)
    sigs = {s.ticker: s for s in ex.evaluate(_now(), data)}

    assert sigs["UP"].action == Action.SKIP  # price above prior close
    assert sigs["UP"].multiplier == 0.0
    assert sigs["FLAT"].action == Action.SKIP  # exactly at prior close, no weakness
    assert sigs["TINY"].action == Action.NO_ACTION  # below prior close but under 0.1% min drop
    assert sigs["TINY"].multiplier == 0.0
    # Codex review ("Reset the morning base reason when the multiplier is
    # zero"): TINY's price IS below the prior close, so
    # select_first_allocation_base_usd alone would report a buy-scale reason
    # (below_prior_close_only/_and_low) even though the minimum-drop check
    # above zeroed this to a no-buy. The zero-multiplier no-action result must
    # report the canonical not_below_prior_close_no_buy reason instead.
    assert sigs["TINY"].selected_base_amount_usd == 0.0
    assert sigs["TINY"].base_selection_reason == "not_below_prior_close_no_buy"
    assert sigs["SMALL"].action == Action.BUY
    assert sigs["SMALL"].multiplier == 2.5  # 3% drop -> 2.5x on the 0x-to-5x ladder
    assert sigs["SMALL"].hypothetical_amount_usd == 5.0
    assert sigs["BIG"].multiplier == 5.0  # 10% drop -> 5x
    assert sigs["BIG"].hypothetical_amount_usd == 10.0


class FixedDropProviderWithLow(FixedDropProvider):
    """FixedDropProvider whose prior regular-session bar carries a distinct low.

    The base provider sets the prior bar's low equal to the prior close, so it cannot
    exercise the dynamic first-allocation base (which needs the prior low to differ from
    the prior close). This variant lets a test straddle the prior low.
    """

    def __init__(self, source, previous_close, drops, *, previous_low):
        super().__init__(source, previous_close, drops)
        self._prev_low = previous_low

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        return Bar(timestamp=ts, open=self._prev, high=self._prev, low=self._prev_low, close=self._prev, volume=0.0)


def test_morning_dynamic_first_allocation_base():
    # Two tickers at the same 3% drop (2.5x) but on opposite sides of the prior low.
    # SHALLOW closes at 97 (below prior close 100, above prior low 96) -> $1 base -> $2.50.
    # DEEP closes at 97 too but with a higher prior low so it is below both -> $2 base -> $5.00.
    cfg = MorningConfig(tickers=("SHALLOW",), base_buy_amount_usd=2.0, base_buy_amount_below_close_usd=1.0)
    ex = MorningDCAExecutor(cfg)
    shallow = FixedDropProviderWithLow(SourceName.ROBINHOOD, 100.0, {"SHALLOW": 0.03}, previous_low=96.0)
    sig = ex.evaluate(_now(), shallow)[0]
    assert sig.action == Action.BUY
    assert sig.multiplier == 2.5
    assert sig.previous_low == 96.0
    assert sig.selected_base_amount_usd == 1.0
    assert sig.base_selection_reason == "below_prior_close_only"
    assert sig.hypothetical_amount_usd == 2.5

    cfg_deep = MorningConfig(tickers=("DEEP",), base_buy_amount_usd=2.0, base_buy_amount_below_close_usd=1.0)
    deep = FixedDropProviderWithLow(SourceName.ROBINHOOD, 100.0, {"DEEP": 0.03}, previous_low=98.0)
    sig_deep = MorningDCAExecutor(cfg_deep).evaluate(_now(), deep)[0]
    assert sig_deep.action == Action.BUY
    assert sig_deep.selected_base_amount_usd == 2.0
    assert sig_deep.base_selection_reason == "below_prior_close_and_low"
    assert sig_deep.hypothetical_amount_usd == 5.0


class FixedQuoteProvider(FixedDropProvider):
    """FixedDropProvider that also exposes a live quote at a fixed timestamp."""

    def __init__(self, source, previous_close, drops, *, quote_price, quote_time):
        super().__init__(source, previous_close, drops)
        self._quote_price = quote_price
        self._quote_time = quote_time

    def get_quote(self, ticker, as_of):
        return (self._quote_price, self._quote_time)


def test_stale_quote_falls_back_to_completed_bar():
    # A quote captured after the 09:45 decision anchor must not size the run.
    decision_dt = _now()
    stale_quote_time = decision_dt + timedelta(minutes=5)
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=50.0, quote_time=stale_quote_time,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 90.0  # completed-bar close, not the stale live quote
    assert any("falling back to completed bar" in w for w in sig.warnings)


def test_fresh_quote_at_or_before_decision_is_used():
    decision_dt = _now()
    fresh_quote_time = decision_dt - timedelta(minutes=1)
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=91.0, quote_time=fresh_quote_time,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 91.0


def test_quote_older_than_completed_bar_anchor_falls_back_to_bar():
    # A quote at/before the decision anchor but *older* than the completed-bar
    # anchor (e.g. a stale prior-session or 09:31 quote) is not fresher than
    # the completed bar, so it must not be trusted for sizing.
    decision_dt = _now()
    stale_quote_time = decision_dt - timedelta(minutes=10)  # older than the 5-min bar anchor
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=50.0, quote_time=stale_quote_time,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 90.0  # completed-bar close, not the stale quote
    assert any("older than the completed bar anchor" in w for w in sig.warnings)


def test_non_positive_quote_price_falls_back_to_completed_bar():
    # A fresh, well-timestamped quote of $0 or negative must not be trusted --
    # evaluate_signal would otherwise treat it as a 100%+ drop and size a
    # max-multiplier buy off a clearly-bad price.
    decision_dt = _now()
    fresh_quote_time = decision_dt - timedelta(minutes=1)
    for bad_price in (0.0, -5.0):
        data = FixedQuoteProvider(
            SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
            quote_price=bad_price, quote_time=fresh_quote_time,
        )
        ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
        sig = ex.evaluate(decision_dt, data)[0]
        assert sig.current_price == 90.0  # completed-bar close, not the bad quote
        assert any("is not positive" in w for w in sig.warnings)


def test_non_positive_completed_bar_close_is_rejected():
    # A provider passing through a placeholder/bad completed bar with a
    # non-positive close (e.g. a 100%+ synthetic drop) must not be trusted as
    # the current price either -- evaluate_signal only checks
    # current_price is None, so an unrejected $0 close would read as a 100%
    # drop and size a max-multiplier BUY off a clearly-bad price.
    decision_dt = _now()
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 1.0})  # 100% drop -> close 0.0
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price is None
    assert sig.action == Action.NO_DATA
    assert any("is not positive" in w for w in sig.warnings)


def test_negative_previous_close_bar_rejected():
    # A provider-backed previous-close bar with a negative close must not be
    # trusted for sizing -- current_price=90 vs. previous_close=-100 would
    # otherwise compute a nonsensical 190% "drop" and size a 3x BUY.
    decision_dt = _now()

    class NegativePrevCloseProvider(FixedDropProvider):
        def get_previous_close_bar(self, ticker, as_of):
            ts = datetime.combine(
                as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
            )
            return Bar(timestamp=ts, open=-100.0, high=-100.0, low=-100.0, close=-100.0, volume=0.0)

    data = NegativePrevCloseProvider(SourceName.ROBINHOOD, 90.0, {"BIG": 0.0})
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.action == Action.NO_DATA
    assert sig.previous_close is None
    assert any("non-positive" in w for w in sig.warnings)


def test_untimestamped_quote_falls_back_to_completed_bar():
    # A quote with no usable timestamp cannot be proven to be at/before the
    # decision anchor, so it must not be trusted for sizing.
    decision_dt = _now()
    data = FixedQuoteProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.10},
        quote_price=50.0, quote_time=None,
    )
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.current_price == 90.0
    assert any("no usable timestamp" in w for w in sig.warnings)


def test_future_previous_close_bar_rejected():
    # A previous-close lookup that returns a bar at/after the decision anchor
    # (e.g. same-day 16:00 close from a live-quote-backed provider) must be
    # rejected rather than used to compute the morning drop.
    decision_dt = _now()

    class FuturePrevCloseProvider(FixedDropProvider):
        def get_previous_close_bar(self, ticker, as_of):
            p = self._prev
            return Bar(timestamp=as_of, open=p, high=p, low=p, close=p, volume=0.0)

    data = FuturePrevCloseProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.action == Action.NO_DATA
    assert any("rejecting" in w for w in sig.warnings)


def test_in_progress_bar_excluded_from_sizing():
    # The 09:45-09:50 bar is not yet complete at the 09:45 decision anchor; a
    # provider that only supplies that bar (no completed 09:40 bar) must yield
    # NO_DATA rather than sizing off the in-progress bar.
    decision_dt = _now()

    class InProgressBarProvider(FixedDropProvider):
        def get_intraday_bars(self, ticker, start, end, interval_minutes):
            price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
            return [Bar(timestamp=end, open=price, high=price, low=price, close=price, volume=100.0)]

    data = InProgressBarProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig.action == Action.NO_DATA
    assert any("missing completed intraday bar" in w for w in sig.warnings)


# --- Separate ledger namespace ----------------------------------------------
def test_ledger_namespace_and_caps():
    ledger = MorningLedger(daily_cap_usd=10.0, weekly_cap_usd=20.0)
    day = date(2026, 7, 8)
    assert ledger.namespace == "morning"
    ledger.record_hypothetical(day, 6.0)
    ledger.record_hypothetical(day, 6.0)
    assert ledger.day_planned(day) == 12.0
    assert ledger.over_daily_cap(day) is True
    assert ledger.remaining_daily(day) == 0.0
    assert ledger.week_planned(day) == 12.0
    assert ledger.over_weekly_cap(day) is False


def test_morning_ledger_does_not_touch_afternoon_state(tmp_path):
    from near_close_dca import StrategyState

    afternoon = StrategyState()
    day = date(2026, 7, 8)
    cfg = MorningConfig(
        tickers=("SMALL", "BIG"),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03, "BIG": 0.10})
    ex.run(_now(), data)

    # Afternoon ledger is untouched: no notional, no buys recorded.
    assert afternoon.day_notional(day) == 0.0
    assert afternoon.week_notional(day) == 0.0
    assert afternoon.buys_by_ticker_by_day == {}
    # Morning ledger recorded the hypothetical spend.
    assert ex.ledger.day_planned(day) > 0.0


def test_over_cap_emits_warning(tmp_path):
    cfg = MorningConfig(
        tickers=tuple(f"T{i}" for i in range(5)),
        base_buy_amount_usd=2.0,
        morning_daily_cap_usd=5.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg)
    drops = {f"T{i}": 0.10 for i in range(5)}  # each 5x -> $10
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, drops)
    res = ex.run(_now(), data)
    assert any("exceeds morning daily cap" in w for w in res["artifacts"]["warnings"])


def test_build_artifacts_does_not_double_count_ledger(tmp_path):
    # build_artifacts must be side-effect-free: calling it more than once (e.g.
    # inspecting artifacts before writing) must not re-record spend against the
    # ledger. Only run()'s single record_buy_candidates() call should count.
    cfg = MorningConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0, morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    now = _now()
    day = now.astimezone(EASTERN).date()

    signals = ex.evaluate(now, data)
    ex.build_artifacts(now, signals, "robinhood")
    ex.build_artifacts(now, signals, "robinhood")
    assert ex.ledger.day_planned(day) == 0.0  # build_artifacts alone never mutates the ledger

    ex.run(now, data)
    assert ex.ledger.day_planned(day) == 10.0  # 10% drop -> 5x -> $10, recorded once


def test_scheduled_run_skips_artifacts_on_market_holiday(tmp_path):
    from near_close_dca import MARKET_HOLIDAYS

    holiday = next(iter(MARKET_HOLIDAYS))
    now = datetime.combine(holiday, time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data)

    assert res["artifacts"]["status"] == "blocked"
    run_state_path = Path(res["paths"]["run_state"])
    assert run_state_path.exists()
    persisted = json.loads(run_state_path.read_text())
    assert persisted["status"] == "blocked"
    # Full artifact shape -- not a hand-rolled stub.
    assert "run_id" in persisted
    assert "counts" in persisted
    assert "signal_rows" in persisted
    # Same-day guard consumers must see today's (blocked) evidence rather than
    # a stale pointer left over from the last real trading session.
    pointer = json.loads((Path(tmp_path) / "latest_morning_run.json").read_text())
    assert pointer["trading_day"] == holiday.isoformat()
    assert pointer["valid_scheduled_evidence"] is True


def test_holiday_block_overwrites_stale_minimal_stub(tmp_path):
    # Regression: the old code wrote a hand-rolled minimal stub {status, block_reason,
    # trades, broker_outcomes} without run_id/counts/run_classification, and then
    # guarded with `if not state_path.exists()` so a subsequent cron fire on the same
    # holiday would leave the stale stub untouched. Consumers reading that file got an
    # incomplete artifact even though paths["run_state"] pointed at it.
    import json as _json

    from near_close_dca import MARKET_HOLIDAYS

    holiday = next(iter(MARKET_HOLIDAYS))
    now = datetime.combine(holiday, time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    # Pre-seed the state directory with a stale minimal stub.
    state_dir = Path(ex.run_state_dir(now))
    state_dir.mkdir(parents=True, exist_ok=True)
    stale_path = state_dir / "run_state.json"
    stale_path.write_text(
        _json.dumps({"status": "blocked", "block_reason": "market holiday",
                     "trades": False, "broker_outcomes": {}}),
        encoding="utf-8",
    )

    ex.run(now, data)

    persisted = _json.loads(stale_path.read_text())
    # Stale stub must have been overwritten with the full artifact shape.
    assert "run_id" in persisted
    assert "counts" in persisted
    assert persisted["status"] == "blocked"
    pointer = _json.loads((Path(tmp_path) / "latest_morning_run.json").read_text())
    assert pointer["trading_day"] == holiday.isoformat()
    assert pointer["valid_scheduled_evidence"] is True


def test_scheduled_run_skips_artifacts_on_future_holiday_beyond_afternoon_window(tmp_path):
    # near_close_dca.MARKET_HOLIDAYS is hand-maintained and scoped to the
    # afternoon live-test window (2026-06-29 through 2026-08-28), so it does
    # NOT include Labor Day 2026-09-07. The morning stream runs indefinitely
    # with no end date, so it must catch this via the computed nyse_holidays()
    # calendar instead of the narrower afternoon list.
    holiday = date(2026, 9, 7)  # Labor Day: 1st Monday of September
    now = datetime.combine(holiday, time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data)

    assert res["artifacts"]["status"] == "blocked"
    pointer = json.loads((Path(tmp_path) / "latest_morning_run.json").read_text())
    assert pointer["trading_day"] == holiday.isoformat()
    assert pointer["valid_scheduled_evidence"] is True


def test_manual_run_ignores_market_holiday_gate(tmp_path):
    from near_close_dca import MARKET_HOLIDAYS

    holiday = next(iter(MARKET_HOLIDAYS))
    now = datetime.combine(holiday, time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data, manual=True)

    assert "status" not in res["artifacts"] or res["artifacts"].get("status") != "blocked"
    assert res["artifacts"]["signal_rows"]


# --- Run-state path + classification ----------------------------------------
def test_run_state_path_and_classification(tmp_path):
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now(), data)

    run_state_path = Path(res["paths"]["run_state"])
    assert run_state_path.parent.name == "20260708_0945ET"
    assert run_state_path.exists()

    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "morning_dca_watchlist_scan"
    assert rc["stream"] == "morning"
    assert rc["scheduled_for_et"] == "09:45"
    assert rc["manual_run"] is False
    assert rc["state_schema_version"] == "morning-watchlist-v1"
    assert rc["run_id"] == "morning_20260708_0945ET"
    assert rc["valid_afternoon_evidence"] is False
    assert rc["trades"] is False
    assert rc["cron_id"] == MORNING_CRON_ID
    assert rc["supersedes_cron_id"] is None
    assert rc["execution_broker"] == "robinhood"
    assert rc["account_number"] == "562057042"

    # Watchlist markers present; no order fields, amounts hypothetical only.
    art = res["artifacts"]
    assert art["mode"] == "watchlist"
    assert art["trades"] is False
    for row in art["signal_rows"]:
        assert set(
            ["ticker", "current_price", "previous_close", "drop_pct", "multiplier",
             "hypothetical_amount_usd", "source", "warnings"]
        ).issubset(row.keys())
    assert art["data_source"] == SourceName.ROBINHOOD.value


def test_manual_run_id_and_no_pointer(tmp_path):
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now(), data, manual=True)
    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert rc["run_id"].startswith("manual_morning_")
    # Manual runs never write the latest-morning pointer and must not overwrite the scheduled folder.
    assert "latest_morning_run" not in res["paths"]
    assert not (Path(tmp_path) / "latest_morning_run.json").exists()
    run_state_path = Path(res["paths"]["run_state"])
    assert "manual_runs" in run_state_path.parts
    assert rc["run_id"] in run_state_path.parts


def test_manual_runs_at_same_timestamp_do_not_collide(tmp_path):
    # Two manual replays sharing the same decision timestamp (down to the
    # second) must get distinct run_ids/paths so the second never overwrites
    # the first's artifact.
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    now = _now()

    res1 = ex.run(now, data, manual=True)
    res2 = ex.run(now, data, manual=True)

    run_id_1 = res1["artifacts"]["run_classification"]["run_id"]
    run_id_2 = res2["artifacts"]["run_classification"]["run_id"]
    assert run_id_1 != run_id_2
    assert Path(res1["paths"]["run_state"]) != Path(res2["paths"]["run_state"])
    assert Path(res1["paths"]["run_state"]).exists()
    assert Path(res2["paths"]["run_state"]).exists()


def test_classification_matches_morning_schema(tmp_path):
    schema_path = Path(__file__).resolve().parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    required = set(schema["required"])
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    rc = ex.run_classification(_now(), manual=False)
    assert required.issubset(rc.keys())
    # run_type is now an enum across the two morning modes; stream is a const.
    assert rc["run_type"] in schema["properties"]["run_type"]["enum"]
    assert rc["stream"] == schema["properties"]["stream"]["const"]
    # The live-mode classification must also satisfy the shared schema.
    live_ex = MorningDCAExecutor(cfg, mode="live")
    live_rc = live_ex.run_classification(_now_live(), manual=False)
    assert required.issubset(live_rc.keys())
    assert live_rc["run_type"] in schema["properties"]["run_type"]["enum"]


# --- Live (10:30 AM) execution mode -----------------------------------------
def test_live_mode_path_and_classification(tmp_path):
    preflight_root = tmp_path / "preflight"
    _seed_preflight(preflight_root, ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        preflight_run_state_root=str(preflight_root),
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data)

    run_state_path = Path(res["paths"]["run_state"])
    assert run_state_path.parent.name == "20260708_1030ET"

    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "morning_dca_live"
    assert rc["stream"] == "morning"
    assert rc["trades"] is True
    assert rc["scheduled_for_et"] == "10:30"
    assert rc["scheduled_window_start_et"] == "10:25"
    assert rc["scheduled_window_end_et"] == "10:35"
    assert rc["state_schema_version"] == "morning-live-v2-preauthorized"
    assert rc["run_id"] == "morning_live_20260708_1030ET"
    assert rc["cron_id"] == MORNING_LIVE_CRON_ID
    assert rc["execution_broker"] == "robinhood"
    assert rc["account_number"] == "562057042"
    assert rc["preauthorized_direct_place"] is True
    assert rc["valid_morning_evidence"] is True
    assert rc["valid_afternoon_evidence"] is False

    art = res["artifacts"]
    assert art["mode"] == "live"
    assert art["trades"] is True


def test_live_run_refreshes_both_morning_pointers(tmp_path):
    """The morning live task refreshes both latest_morning_run.json and
    latest_morning_live.json, each carrying the same-day guard fields and
    pointing at the same-day scheduled run_state."""
    preflight_root = tmp_path / "preflight"
    preflight_dir = preflight_root / "20260708_0945ET"
    preflight_dir.mkdir(parents=True)
    (preflight_dir / "run_state.json").write_text(
        json.dumps({"eligible_to_execute_tickers": ["SMALL"]}), encoding="utf-8"
    )
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        preflight_run_state_root=str(preflight_root),
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data)

    run_state_path = res["paths"]["run_state"]
    run_pointer = Path(tmp_path) / "latest_morning_run.json"
    live_pointer = Path(tmp_path) / "latest_morning_live.json"
    assert run_pointer.exists()
    assert live_pointer.exists()
    assert res["paths"]["latest_morning_run"] == str(run_pointer)
    assert res["paths"]["latest_morning_live"] == str(live_pointer)

    for pointer in (run_pointer, live_pointer):
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        assert payload["run_state_path"] == run_state_path
        assert payload["stream"] == "morning"
        assert payload["same_day_required"] is True
        assert payload["pointer_status"] == "valid"
        assert payload["valid_scheduled_evidence"] is True
        assert "trading_day" in payload
        assert "updated_at_utc" in payload


# --- Jul 20 2026 pre-flight path fix + budget ledger bootstrap --------------
def test_live_run_writes_budget_ledger_json(tmp_path):
    """budget_ledger.bootstrap (config/morning_dca.json): the live task always
    persists a budget_ledger.json snapshot under its own output directory,
    bootstrapping it from the configured caps -- no pre-existing file needed.
    """
    preflight_root = tmp_path / "preflight"
    preflight_dir = preflight_root / "20260708_0945ET"
    preflight_dir.mkdir(parents=True)
    (preflight_dir / "run_state.json").write_text(
        json.dumps({"eligible_to_execute_tickers": ["SMALL"]}), encoding="utf-8"
    )
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        preflight_run_state_root=str(preflight_root),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data)

    assert "budget_ledger" in res["paths"]
    ledger_path = Path(res["paths"]["budget_ledger"])
    assert ledger_path.name == "budget_ledger.json"
    assert ledger_path.parent == Path(res["paths"]["run_state"]).parent
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger == res["artifacts"]["budget"]
    assert ledger["morning_daily_cap_usd"] == 90.0
    assert ledger["morning_weekly_cap_usd"] == 450.0


def test_budget_ledger_filename_configurable():
    cfg = MorningConfig(budget_ledger_filename="custom_ledger.json")
    assert cfg.budget_ledger_filename == "custom_ledger.json"
    assert MorningConfig().budget_ledger_filename == "budget_ledger.json"


def test_watchlist_run_does_not_write_budget_ledger(tmp_path):
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)  # default mode = watchlist
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now(), data)
    assert "budget_ledger" not in res["paths"]


def test_preflight_gate_blocks_when_root_under_forbidden_live_output_dir(tmp_path):
    """must_not_look_under: a preflight_run_state_root resolving under the
    10:30 live output directory must fail closed for a scheduled run rather
    than being read (the Jul 20 2026 incident)."""
    forbidden_root = tmp_path / "morning_dca"
    forbidden_root.mkdir()
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "out"),
        preflight_run_state_root=str(forbidden_root),
        preflight_forbidden_roots=(str(forbidden_root),),
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data)

    assert res["artifacts"]["status"] == "blocked"
    assert "forbidden" in res["artifacts"]["block_reason"]
    assert res["artifacts"]["buy_candidates"] == []


def test_preflight_gate_forbidden_root_warns_on_manual_run():
    forbidden_root = "/home/user/workspace/cron_tracking/42173154"
    cfg = MorningConfig(
        tickers=("SMALL",),
        preflight_run_state_root=forbidden_root,
        preflight_forbidden_roots=(forbidden_root,),
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    admitted = ex._apply_preflight_gate(_now_live(), ["SMALL"], manual=True)
    assert admitted == ["SMALL"]
    assert ex.preflight_gate["preflight_found"] is False
    assert "forbidden" in ex.preflight_gate["warning"]


def test_load_same_day_preflight_recovered_via_bounded_lookup_flag(tmp_path):
    preflight_root = tmp_path / "preflight"
    trading_day = date(2026, 7, 8)
    _seed_preflight(preflight_root, ("SMALL",), trading_date=trading_day)

    # Found directly via the primary path -- not a recovery.
    found = load_same_day_preflight(str(preflight_root), trading_day)
    assert found is not None
    assert found["recovered_via_bounded_lookup"] is False

    # No artifact at all: same result whether or not bounded recovery is enabled.
    missing_day = date(2026, 7, 9)
    assert load_same_day_preflight(str(preflight_root), missing_day) is None
    assert load_same_day_preflight(str(preflight_root), missing_day, bounded_recovery=False) is None


def test_load_morning_config_parses_preflight_and_ledger_blocks(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    payload = {
        "budget_ledger": {"ledger_files": ["custom_ledger.json"]},
        "live_task": {
            "preflight_artifact_resolution": {
                "must_not_look_under": ["/some/forbidden/root"],
                "bounded_recovery_lookup": "",
            }
        },
    }
    (cfg_dir / "morning_dca.json").write_text(json.dumps(payload), encoding="utf-8")
    cfg = load_morning_config(str(cfg_dir), mode=MORNING_LIVE_MODE)
    assert cfg.budget_ledger_filename == "custom_ledger.json"
    assert cfg.preflight_forbidden_roots == ("/some/forbidden/root",)
    # An empty-string bounded_recovery_lookup value is falsy -> disabled.
    assert cfg.preflight_bounded_recovery is False


def test_live_confidence_rule_auto_buys_with_validated_trend():
    # A full 60-minute window of declining 5-minute bars (>= trend_min_bars) validates
    # the strict downward intraday trend, so a qualifying candidate is auto_buy_eligible.
    cfg = MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0)
    ex = MorningDCAExecutor(cfg, mode="live")
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now_live(), data, manual=True)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is True
    assert sig.execution_status == EXEC_AUTO_BUY_ELIGIBLE


class FewBarsProvider(FixedDropProvider):
    """Provider that supplies only a couple of intraday bars near the anchor."""

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        # Two completed bars ending at the completed-bar anchor (end - one
        # interval), so current_price resolves but the trend window is too thin.
        anchor = end - timedelta(minutes=interval_minutes)
        return [
            Bar(timestamp=anchor - timedelta(minutes=interval_minutes),
                open=price, high=price, low=price, close=price, volume=100.0),
            Bar(timestamp=anchor, open=price, high=price, low=price, close=price, volume=100.0),
        ]


class StaleBarsProvider(FixedDropProvider):
    """Provider that stops updating well before the completed-bar anchor."""

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        bars = []
        cursor = start
        stale_end = start + timedelta(minutes=25)
        while cursor <= stale_end:
            bars.append(Bar(timestamp=cursor, open=price + 1.0, high=price + 1.0, low=price, close=price, volume=100.0))
            cursor += timedelta(minutes=interval_minutes)
        return bars


def test_live_confidence_rule_rejects_stale_decision_anchor_bars():
    cfg = MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0)
    ex = MorningDCAExecutor(cfg, mode="live")
    data = StaleBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    sig = ex.evaluate(_now_live(), data, manual=True)[0]

    assert sig.action == Action.NO_DATA
    assert sig.current_price is None
    assert any("rejecting stale data" in warning for warning in sig.warnings)


def test_live_confidence_rule_rejects_trend_with_stale_latest_bar():
    cfg = MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0)
    ex = MorningDCAExecutor(cfg, mode="live")
    data = StaleBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    warnings = []
    bars = data.get_intraday_bars("BIG", _now_live() - timedelta(hours=1), _now_live(), 5)

    assert ex._trend_validated(bars, _now_live(), warnings) is False
    assert any("older than one interval" in warning for warning in warnings)


def test_live_confidence_rule_withholds_when_trend_unvalidated(tmp_path):
    # Price/drop passes, but too few intraday bars means the trend cannot be
    # validated -> the candidate is a manual_review_candidate, NOT auto-bought.
    cfg = MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0,
                        morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode="live")
    data = FewBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    sig = ex.evaluate(_now_live(), data, manual=True)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is False
    assert sig.execution_status == EXEC_MANUAL_REVIEW_CANDIDATE

    res = ex.run(_now_live(), data, manual=True)
    art = res["artifacts"]
    assert art["counts"]["manual_review_candidates"] == 1
    assert art["manual_review_tickers"] == ["BIG"]
    assert art["buy_candidates"] == []
    # Codex review on the PR #43 merge: counts.buy_candidates must reflect the
    # same filtered (executable) set as the buy_candidates row list, not the raw
    # count of BUY-actioned signals -- dca_weekly_dashboard.py sums this count
    # directly, and a withheld manual-review row must not inflate it.
    assert art["counts"]["buy_candidates"] == 0
    assert art["budget"]["hypothetical_planned_spend_usd"] == 0
    assert ex.ledger.day_planned(_now_live().date()) == 0


def test_live_confidence_rule_withholds_when_trend_is_rising(tmp_path):
    cfg = MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0, morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode="live")
    data = RisingBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    sig = ex.evaluate(_now_live(), data, manual=True)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is False
    assert sig.execution_status == EXEC_MANUAL_REVIEW_CANDIDATE
    assert "flat or rising" in "; ".join(sig.warnings)


def test_live_signal_csv_schema_is_stable_for_mixed_rows(tmp_path):
    cfg = MorningConfig(tickers=("FLAT", "BIG"), base_buy_amount_usd=3.0, morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode="live")
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"FLAT": 0.0, "BIG": 0.10})

    res = ex.run(_now_live(), data, manual=True)
    signals_csv = Path(res["paths"]["signals"]).read_text(encoding="utf-8")
    header = signals_csv.splitlines()[0].split(",")
    assert "execution_status" in header
    assert "trend_validated" in header


def test_watchlist_mode_never_sets_execution_status():
    # The read-only watchlist scan never runs the trend gate; rows carry blank
    # execution_status / trend_validated values for a stable CSV schema.
    cfg = MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0)
    ex = MorningDCAExecutor(cfg)  # default watchlist mode
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now(), data)[0]
    assert sig.execution_status is None
    assert sig.trend_validated is None
    assert sig.to_row()["execution_status"] is None
    assert sig.to_row()["trend_validated"] is None


def test_live_and_watchlist_roots_are_distinct():
    assert DEFAULT_MORNING_WATCHLIST_STATE_ROOT != DEFAULT_MORNING_LIVE_STATE_ROOT
    assert DEFAULT_MORNING_STATE_ROOT == DEFAULT_MORNING_WATCHLIST_STATE_ROOT
    assert "ce16ba6b" not in DEFAULT_MORNING_LIVE_STATE_ROOT


def test_load_morning_config_uses_multiplier_not_max_drop(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({
            "sizing_ladder": {
                "base_amount_usd": 3.0,
                "no_buy_below_pct": 0.1,
                "tiers": [
                    {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 1.0},
                    {"min_drop_pct": 1.0, "max_drop_pct": 2.0, "multiplier": 1.5},
                ],
            }
        }),
        encoding="utf-8",
    )
    from morning_dca import load_morning_config

    cfg = load_morning_config(cfg_dir)
    ex = MorningDCAExecutor(cfg)

    assert ex.multiplier_for_drop(0.005) == 1.0
    assert ex.multiplier_for_drop(0.015) == 1.5


def test_morning_run_reviews_places_and_records_orders_csv(tmp_path):
    # Broker orders only place in live mode; DecliningBarsProvider confirms the
    # intraday trend-down required for auto-buy eligibility, and manual=True
    # bypasses the preflight-gate/approved-window checks for this unit test.
    cfg = MorningConfig(tickers=("SMALL",), base_buy_amount_usd=3.0, morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    broker = DryRunBroker(account="562057042", reference_prices={"SMALL": 97.0})

    res = ex.run(_now_live(), data, broker=broker, manual=True)
    candidate = res["artifacts"]["buy_candidates"][0]

    # DryRunBroker always returns a "DRY-RUN-..." order id, which is recorded
    # as status="dry_run" (never "submitted") so the top-up guard never treats
    # a dry-run order as real allocated capital.
    assert candidate["status"] == "dry_run"
    assert candidate["order_id"].startswith("DRY-RUN-SMALL")
    assert candidate["planned_amount_usd"] == 7.5
    assert res["artifacts"]["broker_outcomes"]["SMALL"]["submitted_amount_usd"] == 7.5
    orders_csv_text = Path(res["paths"]["orders_csv"]).read_text(encoding="utf-8")
    assert orders_csv_text.startswith("ticker,order_id,notional_usd,status")
    assert "SMALL" in orders_csv_text


class RecordingLiveBroker:
    """A broker that returns real-looking (non-"DRY-RUN") order ids.

    DryRunBroker always returns a "DRY-RUN-..." id, which _place_broker_orders
    deliberately excludes from the running submitted_today/submitted_this_week
    cap totals (dry-run orders are not real capital). Testing cap enforcement
    across multiple candidates therefore needs a broker whose orders count as
    real placements, like this one.
    """

    def __init__(self, account, reference_prices=None):
        self._account = account
        self._reference_prices = reference_prices or {}
        self.placed_orders = []

    def account_number(self):
        return self._account

    def available_buying_power(self):
        return 10_000.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        return False

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.placed_orders.append((ticker, notional_usd, ref_id))
        return f"ORDER-{ticker}"


def test_morning_artifacts_include_planned_amount_for_prior_allocation_loader(tmp_path):
    cfg = MorningConfig(tickers=("SMALL",), base_buy_amount_usd=3.0, morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    broker = DryRunBroker(account="562057042", reference_prices={"SMALL": 97.0})

    res = ex.run(_now_live(), data, broker=broker, manual=True)
    candidate = res["artifacts"]["buy_candidates"][0]

    assert candidate["planned_amount_usd"] == 7.5
    assert res["artifacts"]["broker_outcomes"]["SMALL"]["submitted_amount_usd"] == 7.5


def test_morning_live_orders_are_blocked_before_exceeding_daily_cap(tmp_path):
    # AAA and BBB both drop 3% -> identical multiplier/drop_pct, so the
    # priority sort (multiplier DESC, drop_pct DESC, ticker ASC) places AAA
    # first. Daily cap of $10 admits AAA ($7.50) but not BBB ($7.50 more would
    # exceed the cap), so BBB is cap_exceeded rather than placed.
    cfg = MorningConfig(
        tickers=("AAA", "BBB"),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=10.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03, "BBB": 0.03})
    broker = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0, "BBB": 97.0})

    res = ex.run(_now_live(), data, broker=broker, manual=True)
    by_ticker = {candidate["ticker"]: candidate for candidate in res["artifacts"]["buy_candidates"]}
    broker_outcomes = res["artifacts"]["broker_outcomes"]

    assert len(broker.placed_orders) == 1
    assert broker.placed_orders[0][0] == "AAA"
    assert by_ticker["AAA"]["status"] == "submitted"
    assert by_ticker["BBB"]["status"] == "cap_exceeded"
    assert broker_outcomes["BBB"]["skip_reason"] == "daily cap would be exceeded"
    # cap_exceeded outcomes carry no submitted_amount_usd, so planned_amount_usd
    # is never overwritten from the (never-submitted) hypothetical amount.
    assert "planned_amount_usd" not in by_ticker["BBB"]
    assert by_ticker["BBB"]["hypothetical_amount_usd"] == 7.5


def test_morning_live_ledger_seeds_cap_spend_from_earlier_same_process_day_run(tmp_path):
    # Codex review: MorningLedger is in-memory and this executor is normally
    # instantiated fresh per cron invocation, so a retried/rerun live task saw
    # an empty ledger and could re-spend the daily cap. Simulate two separate
    # "processes" (two distinct MorningDCAExecutor instances, as a real retry
    # would be) hitting the same live run-state root on the same trading day:
    # the first places AAA ($7.50 of a $10 daily cap); a fresh second instance
    # must read that already-written run_state.json and see only $2.50 of
    # headroom left, blocking BBB's $7.50 order as cap_exceeded rather than
    # wrongly re-spending the full $10.
    cfg = MorningConfig(
        tickers=("AAA",),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=10.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex1 = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0})
    res1 = ex1.run(_now_live(), data1, broker=broker1, manual=True)
    assert res1["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"

    # Fresh executor instance = fresh in-memory MorningLedger, mirroring a
    # brand-new cron process picking up where the first one left off.
    cfg2 = MorningConfig(
        tickers=("BBB",),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=10.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex2 = MorningDCAExecutor(cfg2, mode=MORNING_LIVE_MODE)
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res2 = ex2.run(_now_live(), data2, broker=broker2, manual=True)

    assert len(broker2.placed_orders) == 0
    bbb = res2["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "cap_exceeded"
    assert res2["artifacts"]["broker_outcomes"]["BBB"]["skip_reason"] == "daily cap would be exceeded"


def test_morning_live_ledger_seeds_weekly_cap_from_earlier_day_same_week(tmp_path):
    # Codex review (follow-up): the seeding scan skipped any run-state file
    # whose trading_day wasn't an exact match for today, so a fresh process
    # starting later in the same ISO week never folded in an earlier day's
    # spend against the shared weekly cap. Monday 2026-07-06 and Wednesday
    # 2026-07-08 (_now_live()) are both ISO week 28.
    monday = datetime.combine(date(2026, 7, 6), time(10, 30), tzinfo=EASTERN)
    cfg1 = MorningConfig(
        tickers=("AAA",),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=10.0,
        morning_run_state_root=str(tmp_path),
    )
    ex1 = MorningDCAExecutor(cfg1, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0})
    res1 = ex1.run(monday, data1, broker=broker1, manual=True)
    assert res1["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"
    assert res1["artifacts"]["buy_candidates"][0]["planned_amount_usd"] == 7.5

    # Fresh process (fresh in-memory ledger) on Wednesday of the same week.
    cfg2 = MorningConfig(
        tickers=("BBB",),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=10.0,
        morning_run_state_root=str(tmp_path),
    )
    ex2 = MorningDCAExecutor(cfg2, mode=MORNING_LIVE_MODE)
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res2 = ex2.run(_now_live(), data2, broker=broker2, manual=True)

    # Monday already spent $7.50 of the $10 weekly cap, leaving only $2.50 of
    # weekly headroom -- not the full $10 a same-day-only seed would wrongly
    # leave available -- so BBB's $7.50 must be blocked.
    assert len(broker2.placed_orders) == 0
    bbb = res2["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "cap_exceeded"
    assert res2["artifacts"]["broker_outcomes"]["BBB"]["skip_reason"] == "weekly cap would be exceeded"


def test_morning_live_ledger_seed_does_not_double_count_same_instance_rerun(tmp_path):
    # Codex review (follow-up): _seeded_prior_run_ids was only populated BY the
    # seeding scan itself, so a run's own just-written artifact (already
    # recorded into the ledger directly via record_buy_candidates) got
    # re-read from disk and folded in AGAIN the next time the SAME executor
    # instance called run() -- e.g. a long-lived worker retrying after a
    # transient broker error. That double count under-reports remaining cap
    # and wrongly blocks an otherwise-valid second order.
    cfg = MorningConfig(
        tickers=("AAA",),
        morning_daily_cap_usd=20.0,
        morning_weekly_cap_usd=200.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0})
    res1 = ex.run(_now_live(), data1, broker=broker1, manual=True)
    assert res1["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"

    # Same instance, second call (simulating a retry), different ticker.
    ex.config = MorningConfig(
        tickers=("BBB",),
        morning_daily_cap_usd=20.0,
        morning_weekly_cap_usd=200.0,
        morning_run_state_root=str(tmp_path),
    )
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res2 = ex.run(_now_live(), data2, broker=broker2, manual=True)

    # $20 cap: AAA ($7.50) + BBB ($7.50) = $15 <= $20 -- must be admitted. The
    # double-count bug would see AAA counted twice ($15) + BBB ($7.50) = $22.50
    # > $20, wrongly blocking BBB.
    assert len(broker2.placed_orders) == 1
    assert broker2.placed_orders[0][0] == "BBB"
    bbb = res2["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "submitted"


def test_morning_live_ledger_seed_naive_manual_timestamp_is_eastern_not_utc(tmp_path):
    # Codex review: the generated-at boundary filter for manual_runs/ artifacts
    # treated a naive (no-offset) timestamp as UTC, but the established
    # precedent for this exact scenario -- near_close_dca._prior_run_before_boundary,
    # which this filter's own docstring says it mirrors -- treats a naive
    # generated_at as Eastern. 14:29 (naive) is AFTER the 10:30 ET replay
    # decision time when correctly read as 14:29 ET, so it must be excluded
    # from ledger seeding; the UTC bug would instead read it as 14:29 UTC =
    # 10:29 ET (EDT), which is BEFORE 10:30 ET and would wrongly include it.
    manual_dir = tmp_path / "manual_runs" / "20260708_1429"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",
            "generated_at": "2026-07-08T14:29:00",
            "run_id": "manual-late-afternoon",
            "buy_candidates": [
                {"ticker": "ZZZ", "status": "submitted", "planned_amount_usd": 7.5}
            ],
        }),
        encoding="utf-8",
    )

    cfg = MorningConfig(
        tickers=("AAA",),
        morning_daily_cap_usd=10.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    # A 14:29 manual run must not have consumed any of the $10 daily cap for
    # this 10:30 ET run: AAA's own $7.50 order must go through in full. The
    # UTC-interpretation bug would seed $7.50 from the (wrongly-included)
    # manual run first, leaving only $2.50 of headroom and blocking AAA.
    assert broker.placed_orders, f"AAA wrongly blocked; ledger={ex.ledger.planned_by_day}"
    aaa = res["artifacts"]["buy_candidates"][0]
    assert aaa["status"] == "submitted"


def test_morning_live_ledger_seed_excludes_delayed_scheduled_artifact_on_replay(tmp_path):
    # Codex review: the generated-at boundary was only applied to manual_runs/
    # artifacts, not the day-by-day scheduled/fixed-slot paths. If a scheduled
    # invocation actually executed late (e.g. delayed by a retry or outage)
    # and wrote its fixed 20260708_1030ET/run_state.json with a generated_at
    # well after the nominal 10:30 decision time, a later replay/backfill
    # using the nominal 10:30 `now` would still read that delayed file and
    # wrongly seed its spend, suppressing orders that were valid at the
    # replay decision time.
    scheduled_dir = tmp_path / "20260708_1030ET"
    scheduled_dir.mkdir(parents=True)
    (scheduled_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",
            "generated_at": "2026-07-08T14:00:00-04:00",
            "run_id": "scheduled-delayed",
            "buy_candidates": [
                {"ticker": "ZZZ", "status": "submitted", "planned_amount_usd": 7.5}
            ],
        }),
        encoding="utf-8",
    )

    cfg = MorningConfig(
        tickers=("AAA",),
        morning_daily_cap_usd=10.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    # The delayed 14:00 scheduled artifact must not have consumed any of the
    # $10 daily cap for this 10:30 ET replay: AAA's own $7.50 order must go
    # through in full. Wrongly including the delayed artifact would seed
    # $7.50 first, leaving only $2.50 of headroom and blocking AAA.
    assert broker.placed_orders, f"AAA wrongly blocked; ledger={ex.ledger.planned_by_day}"
    aaa = res["artifacts"]["buy_candidates"][0]
    assert aaa["status"] == "submitted"


def test_morning_live_ledger_seed_rejects_non_finite_csv_notional(tmp_path):
    # Codex review: an inf/-inf/nan notional_usd/amount_usd in a same-week
    # orders.csv/filled_orders.csv survives float() and would seed the ledger
    # with infinite cap spend via max(total, csv_total), cap-exceeding every
    # later order for the rest of the day/week.
    monday_dir = tmp_path / "20260706_1030ET"
    monday_dir.mkdir(parents=True)
    (monday_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-06",
            "run_id": "scheduled-monday",
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 1.0}
            ],
        }),
        encoding="utf-8",
    )
    with (monday_dir / "filled_orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "ticker", "amount_usd"])
        writer.writerow(["X-1", "AAA", "inf"])

    cfg = MorningConfig(
        tickers=("BBB",),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=10.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    # $10 weekly cap: Monday's JSON total ($1, the inf notional rejected) plus
    # BBB's $7.50 = $8.50 <= $10 -- must be admitted. The infinite-seed bug
    # would leave -inf headroom and block BBB outright.
    assert broker.placed_orders, f"BBB wrongly blocked; ledger={ex.ledger.planned_by_week}"
    bbb = res["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "submitted"


def test_morning_live_ledger_seed_survives_corrupt_filled_orders_csv(tmp_path):
    # Codex review: a non-UTF-8 filled_orders.csv raised UnicodeDecodeError
    # while csv.DictReader iterated it, which the seeding loop's except OSError
    # did not catch, aborting the entire scheduled retry over one corrupt
    # auxiliary fill file instead of falling back to the JSON-derived total.
    monday_dir = tmp_path / "20260706_1030ET"
    monday_dir.mkdir(parents=True)
    (monday_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-06",
            "run_id": "scheduled-monday",
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 1.0}
            ],
        }),
        encoding="utf-8",
    )
    (monday_dir / "filled_orders.csv").write_bytes(b"order_id,ticker,amount_usd\n\xff\xfe,AAA,3.0\n")

    cfg = MorningConfig(
        tickers=("BBB",),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    # Must not raise UnicodeDecodeError; falls back to the JSON total ($1).
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    assert broker.placed_orders, f"BBB wrongly blocked; ledger={ex.ledger.planned_by_week}"
    bbb = res["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "submitted"


def test_morning_live_ledger_seed_falls_back_to_orders_csv_when_filled_orders_unusable(tmp_path):
    # Codex review: filled_orders.csv is preferred over orders.csv when both
    # exist, but a corrupt/empty/no-qualifying-rows filled_orders.csv used to
    # be the end of the line -- the JSON total was accepted as-is even when a
    # distinct, valid orders.csv (with a larger, more accurate total) also
    # existed alongside it. A stale/under-reported JSON planned amount could
    # then let a retry seed too little spend and submit past the cap.
    monday_dir = tmp_path / "20260706_1030ET"
    monday_dir.mkdir(parents=True)
    (monday_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-06",
            "run_id": "scheduled-monday",
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 1.0}
            ],
        }),
        encoding="utf-8",
    )
    # filled_orders.csv exists but is corrupt (non-UTF-8) -- unusable.
    (monday_dir / "filled_orders.csv").write_bytes(b"order_id,ticker,amount_usd\n\xff\xfe,AAA,3.0\n")
    # orders.csv is valid and reports the true (larger) confirmed spend.
    with (monday_dir / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "ticker", "status", "notional_usd"])
        writer.writerow(["X-1", "AAA", "submitted", "8.0"])

    cfg = MorningConfig(
        tickers=("BBB",),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=10.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    # $10 weekly cap: the orders.csv fallback total ($8, not the JSON's stale
    # $1) plus BBB's $7.50 = $15.50 > $10 -- must be blocked. Silently
    # accepting the JSON-only $1 would leave $9 of headroom and wrongly admit
    # BBB's $7.50.
    assert len(broker.placed_orders) == 0, f"BBB wrongly admitted; placed={broker.placed_orders}"
    bbb = res["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "cap_exceeded"


def test_morning_live_ledger_seed_uses_larger_of_partial_filled_orders_and_orders_csv(tmp_path):
    # Codex review: filled_orders.csv having ANY qualifying row (not just being
    # totally unusable) used to short-circuit the orders.csv fallback entirely.
    # A partially reconciled filled_orders.csv (some rows confirmed, others
    # still only recorded as submitted in orders.csv) would then under-report
    # relative to orders.csv's larger total, letting a retry seed too little
    # spend and submit past the cap.
    monday_dir = tmp_path / "20260706_1030ET"
    monday_dir.mkdir(parents=True)
    (monday_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-06",
            "run_id": "scheduled-monday",
            "buy_candidates": [
                {"ticker": "AAA", "status": "submitted", "planned_amount_usd": 1.0}
            ],
        }),
        encoding="utf-8",
    )
    # filled_orders.csv is valid and has a qualifying row, but under-reports
    # relative to orders.csv (e.g. only one of several submitted orders has
    # been confirmed as filled so far).
    with (monday_dir / "filled_orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "ticker", "amount_usd"])
        writer.writerow(["X-1", "AAA", "3.0"])
    with (monday_dir / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "ticker", "status", "notional_usd"])
        writer.writerow(["X-1", "AAA", "submitted", "3.0"])
        writer.writerow(["X-2", "AAA", "submitted", "5.0"])

    cfg = MorningConfig(
        tickers=("BBB",),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=12.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    # $12 weekly cap: orders.csv's larger total ($8, not filled_orders.csv's
    # partial $3) plus BBB's $7.50 = $15.50 > $12 -- must be blocked. Stopping
    # at filled_orders.csv's $3 would leave $9 of headroom, enough to wrongly
    # admit BBB's $7.50.
    assert len(broker.placed_orders) == 0, f"BBB wrongly admitted; placed={broker.placed_orders}"
    bbb = res["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "cap_exceeded"


def test_morning_live_retry_preserves_prior_submitted_candidate_on_scheduled_path(tmp_path):
    # Codex review (follow-up): a scheduled (non-manual) retry writes to the
    # SAME fixed run_state_dir path every time, so write_artifacts() fully
    # overwrote the earlier attempt's persisted buy_candidates. The in-memory
    # ledger total survives a retry (the seeding fix above), but the earlier
    # attempt's own submitted-candidate ROW vanished from the final artifact --
    # and load_prior_same_day_allocations() (used by the afternoon top-up
    # guard) reads directly from these persisted files, not the ledger, so it
    # lost visibility into that morning fill entirely after a retry.
    # Same-day 9:45 pre-flight artifact admitting both tickers, so the 10:30
    # scheduled (non-manual) live gate doesn't fail-closed for lack of one.
    preflight_root = tmp_path / "preflight"
    _seed_preflight(preflight_root, ("AAA", "BBB"))

    cfg1 = MorningConfig(
        tickers=("AAA",),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=1000.0,
        morning_run_state_root=str(tmp_path),
        preflight_run_state_root=str(preflight_root),
    )
    ex1 = MorningDCAExecutor(cfg1, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0})
    res1 = ex1.run(_now_live(), data1, broker=broker1, manual=False)
    assert res1["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"
    scheduled_path = Path(res1["paths"]["run_state"])
    assert scheduled_path.exists()

    # Fresh process retries the same scheduled slot -- new executor instance,
    # same fixed run_state_dir path -- and this time only BBB newly qualifies.
    cfg2 = MorningConfig(
        tickers=("BBB",),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=1000.0,
        morning_run_state_root=str(tmp_path),
        preflight_run_state_root=str(preflight_root),
    )
    ex2 = MorningDCAExecutor(cfg2, mode=MORNING_LIVE_MODE)
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res2 = ex2.run(_now_live(), data2, broker=broker2, manual=False)
    assert res2["artifacts"]["broker_outcomes"]["BBB"]["status"] == "submitted"
    assert Path(res2["paths"]["run_state"]) == scheduled_path

    persisted = json.loads(scheduled_path.read_text(encoding="utf-8"))
    by_ticker = {row["ticker"]: row for row in persisted["buy_candidates"]}
    assert by_ticker["BBB"]["status"] == "submitted"
    # AAA's earlier submitted fill must survive the retry's overwrite so the
    # afternoon top-up guard (which reads run_state.json directly) still sees it.
    assert "AAA" in by_ticker
    assert by_ticker["AAA"]["status"] == "submitted"


# --- No interaction with afternoon scheduled evidence -----------------------
def test_does_not_write_afternoon_artifacts(tmp_path):
    afternoon_root = tmp_path / "ce16ba6b"
    (afternoon_root / "scheduled_runs" / "20260708_1520ET").mkdir(parents=True)
    latest = afternoon_root / "latest_scheduled_run.json"
    latest.write_text(json.dumps({"run_id": "scheduled_20260708_1520ET"}), encoding="utf-8")
    before = latest.read_text(encoding="utf-8")

    morning_root = tmp_path / "morning_dca"
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(morning_root))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now(), data)

    # Afternoon pointer untouched; nothing written under the afternoon root.
    assert latest.read_text(encoding="utf-8") == before
    afternoon_files = [p for p in afternoon_root.rglob("run_state*.json")]
    assert afternoon_files == []
    # Everything the morning run wrote is under the morning root.
    for path in res["paths"].values():
        assert str(morning_root) in path


def test_default_root_is_separate_from_afternoon():
    assert "morning_dca" in DEFAULT_MORNING_STATE_ROOT
    assert "ce16ba6b" not in DEFAULT_MORNING_STATE_ROOT


def test_default_tickers_mirror_active_universe():
    from near_close_dca import ACTIVE_TICKERS, EXCLUDED_TICKERS

    cfg = MorningConfig()
    assert tuple(cfg.tickers) == tuple(ACTIVE_TICKERS)
    assert cfg.excluded_tickers == frozenset(EXCLUDED_TICKERS)


def test_base_executor_is_abstract():
    base = DCAExecutor()
    for call in (
        lambda: base.sizing_config,
        lambda: base.run_state_dir(_now()),
        lambda: base.run_classification(_now()),
    ):
        try:
            call()
        except NotImplementedError:
            continue
        raise AssertionError("expected NotImplementedError from abstract base hook")



# --- Thread fixes (PR #13 review comments) -----------------------------------

def test_nyse_holidays_stays_open_dec_31_before_saturday_new_year():
    """NYSE Rule 7.2 carves out a year-end-accounting exception for New Year's
    Day: when the following January 1 falls on a Saturday, the Exchange trades
    a full session on December 31 rather than observing the holiday early
    (this is why NYSE traded a full, record-setting session on 2021-12-31
    despite 2022-01-01 being a Saturday). 2027-12-31 sits in the identical
    position ahead of Saturday 2028-01-01, so neither date should be a
    computed closure.
    """
    from datetime import date as _date

    # Verify the premise: Jan 1 2028 is indeed a Saturday.
    assert _date(2028, 1, 1).weekday() == 5, "test premise: 2028-01-01 should be Saturday"

    holidays_2027 = nyse_holidays(2027)
    assert _date(2027, 12, 31) not in holidays_2027, (
        "NYSE Rule 7.2's year-end exception keeps the Exchange open 2027-12-31"
    )
    assert _date(2028, 1, 1) not in nyse_holidays(2028)

    # Sanity: a Sunday New Year's Day still shifts to the following Monday.
    assert _date(2034, 1, 1).weekday() == 6, "test premise: 2034-01-01 should be Sunday"
    assert _date(2034, 1, 2) in nyse_holidays(2034)


class _NaNCloseProvider:
    """Provider that returns a bar with NaN close for testing non-finite rejection."""

    def __init__(self, nan_prev=False, nan_intraday=False):
        self.source = SourceName.ROBINHOOD
        self._nan_prev = nan_prev
        self._nan_intraday = nan_intraday

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        close = float("nan") if self._nan_prev else 100.0
        return Bar(timestamp=ts, open=close, high=close, low=close, close=close, volume=0.0)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        close = float("nan") if self._nan_intraday else 97.0
        bars = []
        cursor = start
        while cursor <= end:
            bars.append(Bar(timestamp=cursor, open=close, high=close, low=close, close=close, volume=100.0))
            cursor += timedelta(minutes=interval_minutes)
        return bars


def test_nan_previous_close_bar_rejected():
    """A NaN previous-close must be rejected and signal NO_DATA."""
    cfg = MorningConfig(tickers=("AAPL",))
    ex = MorningDCAExecutor(cfg)
    sig = ex.evaluate_signal("AAPL", _now(), _NaNCloseProvider(nan_prev=True))
    assert sig.action == Action.NO_DATA, f"expected NO_DATA, got {sig.action}"
    assert any("non-positive" in w.lower() or "nan" in w.lower() for w in sig.warnings), (
        f"expected a rejection warning for NaN close; got: {sig.warnings}"
    )


def test_nan_completed_bar_close_is_rejected():
    """A NaN completed-bar close must be rejected and signal NO_DATA."""
    cfg = MorningConfig(tickers=("AAPL",))
    ex = MorningDCAExecutor(cfg)
    sig = ex.evaluate_signal("AAPL", _now(), _NaNCloseProvider(nan_prev=False, nan_intraday=True))
    assert sig.action == Action.NO_DATA, f"expected NO_DATA, got {sig.action}"
    assert any("non-positive" in w.lower() or "nan" in w.lower() for w in sig.warnings), (
        f"expected a rejection warning for NaN completed bar close; got: {sig.warnings}"
    )


def test_load_morning_config_reads_config_file(tmp_path):
    """load_morning_config reads config/morning_dca.json and maps fields to MorningConfig."""
    from morning_dca import load_morning_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({
            "sizing_ladder": {
                "base_amount_usd": 5.0,
                "no_buy_below_pct": 0.2,
                "tiers": [
                    {"min_drop_pct": 0.2, "max_drop_pct": 3.0, "multiplier": 1.0},
                    {"min_drop_pct": 3.0, "max_drop_pct": None, "multiplier": 2.0},
                ],
            },
            "budget_ledger": {
                "morning_daily_cap_usd": 60.0,
                "morning_weekly_cap_usd": 300.0,
            },
            "artifact_paths": {
                "morning_run_state_root": "/custom/morning/root",
            },
        }),
        encoding="utf-8",
    )

    cfg = load_morning_config(str(cfg_dir))
    assert cfg.base_buy_amount_usd == 5.0
    assert abs(cfg.minimum_drop_vs_previous_close - 0.002) < 1e-9
    assert cfg.morning_daily_cap_usd == 60.0
    assert cfg.morning_weekly_cap_usd == 300.0
    assert cfg.morning_run_state_root == "/custom/morning/root"



def test_load_morning_config_reads_approved_dates_from_trading_window(tmp_path):
    from morning_dca import load_morning_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "trading_window.json").write_text(
        json.dumps({"start_date": "2026-07-02", "end_date": "2026-07-06", "weekdays_only": True}),
        encoding="utf-8",
    )

    cfg = load_morning_config(str(cfg_dir))

    assert cfg.approved_test_dates == frozenset({date(2026, 7, 2), date(2026, 7, 3), date(2026, 7, 6)})
    assert cfg.decision_time_et is None


def test_load_morning_config_fails_closed_on_malformed_trading_window(tmp_path):
    # Codex review on the PR #45 merge: trading_window.json existing but being
    # malformed (missing start_date/end_date here) makes load_approved_dates()
    # raise. Falling back to an empty approved_test_dates would silently
    # disable the live date gate exactly when its configuration is broken
    # (_maybe_block_for_holiday's `bool(approved) and ...` check treats empty
    # as "no restriction"). The fallback must instead be a non-empty,
    # unreachable date set so every real trading day is still blocked.
    from morning_dca import load_morning_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"morning_run_state_root": str(tmp_path / "state")}}),
        encoding="utf-8",
    )
    (cfg_dir / "trading_window.json").write_text(
        json.dumps({"weekdays_only": True}),  # missing required start_date/end_date
        encoding="utf-8",
    )

    cfg = load_morning_config(str(cfg_dir))

    assert bool(cfg.approved_test_dates) is True
    assert date(2026, 7, 8) not in cfg.approved_test_dates
    ex = MorningDCAExecutor(cfg, mode="live")
    blocked = ex._maybe_block_for_holiday(_now_live(), manual=False)
    assert blocked is not None
    assert blocked["artifacts"]["block_reason"] == "outside approved trading window"


def test_maybe_block_for_holiday_preserves_submitted_run_state(tmp_path):
    """_maybe_block_for_holiday must not overwrite a state with submitted evidence.

    If the live slot already has submitted orders and a later cron retry fires
    with is_outside_window=True (e.g. trading_window.json was briefly malformed),
    the existing run_state.json must be preserved so midday's prior-allocation
    scan still offsets those orders and avoids duplicate allocation.
    """
    from morning_dca import MorningConfig, MorningDCAExecutor

    state_root = tmp_path / "state"
    state_root.mkdir()

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"morning_run_state_root": str(state_root)}}),
        encoding="utf-8",
    )

    from morning_dca import _APPROVED_DATES_LOAD_FAILED, load_morning_config
    cfg = load_morning_config(str(cfg_dir))
    cfg = MorningConfig(
        **{
            **cfg.__dict__,
            "morning_run_state_root": str(state_root),
            "approved_test_dates": _APPROVED_DATES_LOAD_FAILED,
        }
    )
    ex = MorningDCAExecutor(cfg, mode="live")

    now = _now_live()
    run_dir = ex.run_state_dir(now)
    run_dir.mkdir(parents=True, exist_ok=True)
    submitted_state = {
        "run_id": "morning_live_20260708_1030ET",
        "status": "completed",
        "block_reason": None,
        "buy_candidates": [{"ticker": "AAPL", "status": "submitted", "amount_usd": 3.0}],
        "broker_outcomes": {},
    }
    state_path = run_dir / "run_state.json"
    state_path.write_text(json.dumps(submitted_state), encoding="utf-8")

    result = ex._maybe_block_for_holiday(now, manual=False)

    assert result is not None
    # Must return existing submitted state, not a fresh blocked artifact
    assert result["artifacts"]["status"] != "blocked"
    assert result["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    # File must be unchanged
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["status"] != "blocked"


def test_load_morning_config_falls_back_to_defaults_if_missing(tmp_path):
    """load_morning_config gracefully falls back when file is absent."""
    from morning_dca import load_morning_config

    cfg = load_morning_config(str(tmp_path / "nonexistent"))
    defaults = MorningConfig()
    assert cfg.base_buy_amount_usd == defaults.base_buy_amount_usd
    assert cfg.morning_daily_cap_usd == defaults.morning_daily_cap_usd


def test_load_morning_config_enables_model_usage_log_by_default(tmp_path):
    # load_morning_config is the "normal configuration path" real scheduled
    # runs use, so it must resolve a working model_usage_log_root by default
    # (unlike the bare MorningConfig() dataclass default of None, which
    # protects ad-hoc/test construction) -- otherwise the audit log the
    # manifest/docs promise is silently disabled in production.
    from morning_dca import DEFAULT_MODEL_USAGE_LOG_ROOT, load_morning_config

    # Missing config file.
    cfg = load_morning_config(str(tmp_path / "nonexistent"))
    assert cfg.model_usage_log_root == DEFAULT_MODEL_USAGE_LOG_ROOT

    # Present but empty config file.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text("{}", encoding="utf-8")
    cfg = load_morning_config(str(cfg_dir))
    assert cfg.model_usage_log_root == DEFAULT_MODEL_USAGE_LOG_ROOT

    # Explicit override via artifact_paths.model_usage_log_root.
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"model_usage_log_root": "/custom/model_usage"}}),
        encoding="utf-8",
    )
    cfg = load_morning_config(str(cfg_dir))
    assert cfg.model_usage_log_root == "/custom/model_usage"

    # Explicit null disables it for this stream.
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"model_usage_log_root": None}}), encoding="utf-8"
    )
    cfg = load_morning_config(str(cfg_dir))
    assert cfg.model_usage_log_root is None


# --- PR #22 bot-review regression coverage (batch: morning config loading) ---


def test_load_morning_config_reads_configured_ticker_whitelist(tmp_path):
    # Codex review #71: the morning live stream must load its ticker whitelist
    # from config/tickers.json (the same source near_close_dca's afternoon
    # stream reads via dca_config.load_strategy_config), not only the
    # hardcoded module-level ACTIVE_TICKERS/EXCLUDED_TICKERS defaults.
    from morning_dca import load_morning_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "tickers.json").write_text(
        json.dumps({"active": ["ZZZZ"], "excluded": ["YYYY"]}),
        encoding="utf-8",
    )

    cfg = load_morning_config(str(cfg_dir))

    assert cfg.tickers == ("ZZZZ",)
    assert cfg.excluded_tickers == frozenset({"YYYY"})


def test_load_morning_config_raises_on_invalid_configured_order_cap(tmp_path):
    # Codex review #85: an explicitly configured but invalid
    # circuit_breakers.max_per_order_usd (non-finite, non-positive, or
    # unparseable) must fail closed -- raise -- rather than silently keep the
    # default cap for a live trading stream.
    from morning_dca import load_morning_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_per_order_usd": -5}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_morning_config(str(cfg_dir), mode=MORNING_LIVE_MODE)


def test_augment_artifacts_appends_top_up_manual_review_candidate_row():
    # Codex review #82: a new top-up-manual-review ticker must also get a row
    # in the manual_review_candidates detail list, not just an incremented
    # count and a ticker appended to manual_review_tickers.
    cfg = MorningConfig(tickers=("AAPL",))
    ex = MorningDCAExecutor(cfg)
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=6.0,
        decision_price=99.0,
        prior_same_day_amount=3.0,
        prior_same_day_reference_price=None,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        incremental_order_amount=0.0,
        top_up_guard_result="missing_prior_price",
        top_up_skipped=True,
        top_up_manual_review=True,
    )
    artifacts = {
        "counts": {"manual_review_candidates": 0},
        "manual_review_tickers": [],
        "manual_review_candidates": [],
        # _augment_artifacts pulls the full row (not a re-derived minimal dict)
        # from buy_candidates for any new top-up-manual-review ticker, so
        # downstream readers see the same shape as trend-unvalidated cases.
        "buy_candidates": [{"ticker": "AAPL", "action": "buy", "reason": "graded ladder"}],
    }

    ex._augment_artifacts(artifacts, {"AAPL": outcome})

    assert artifacts["counts"]["manual_review_candidates"] == 1
    assert artifacts["manual_review_tickers"] == ["AAPL"]
    assert len(artifacts["manual_review_candidates"]) == 1
    assert artifacts["manual_review_candidates"][0]["ticker"] == "AAPL"


def test_morning_executor_default_init_loads_config_file():
    """MorningDCAExecutor() with no args loads from the shipped config/morning_dca.json."""
    ex = MorningDCAExecutor()
    assert ex.config.base_buy_amount_usd == 2.0
    assert ex.config.morning_daily_cap_usd == 90.0
    assert date(2026, 7, 2) in ex.config.approved_test_dates
    assert date(2026, 8, 28) in ex.config.approved_test_dates


def test_load_morning_config_reads_multipliers_not_max_drop(tmp_path):
    """load_morning_config must parse the 'multiplier' field, not max_drop_pct."""
    from morning_dca import load_morning_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({
            "sizing_ladder": {
                "base_amount_usd": 3.0,
                "no_buy_below_pct": 0.1,
                "tiers": [
                    {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 1.0},
                    {"min_drop_pct": 1.0, "max_drop_pct": 2.0, "multiplier": 1.5},
                    {"min_drop_pct": 8.0, "max_drop_pct": None, "multiplier": 5.0},
                ],
            },
        }),
        encoding="utf-8",
    )
    cfg = load_morning_config(str(cfg_dir))
    # Each tier tuple must be (threshold_fraction, multiplier).
    # A 0.1%-1% drop should give 1x ($3), not 0.01x ($0.03).
    tiers = dict(cfg.sizing_tiers)
    assert tiers[0.001] == 1.0, f"expected 1.0x for 0.1% threshold, got {tiers[0.001]}"
    assert tiers[0.010] == 1.5
    assert tiers[0.080] == 5.0


def test_run_with_broker_places_orders_and_records_outcomes(tmp_path):
    """MorningDCAExecutor.run(broker=...) places orders for buy candidates.

    Broker orders are only placed in live mode (see the ``self.mode ==
    MORNING_LIVE_MODE`` gate in ``run()``); a broker passed in watchlist mode
    is intentionally a no-op so hypothetical scans never risk real capital.
    ``manual=True`` bypasses the preflight-gate/approved-window checks that
    otherwise apply to scheduled live runs.
    """
    from near_close_dca import DryRunBroker

    cfg = MorningConfig(tickers=("AAPL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # DecliningBarsProvider (not FixedDropProvider) so the intraday trend-down
    # confirmation required for auto-buy eligibility actually passes.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.02})  # 2% drop -> buy

    broker = DryRunBroker(cash=1000.0)
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    artifacts = res["artifacts"]
    candidates = artifacts["buy_candidates"]
    assert len(candidates) == 1
    c = candidates[0]
    assert c["ticker"] == "AAPL"
    # DryRunBroker always returns a "DRY-RUN-..." order id, which
    # _place_broker_orders deliberately records as status="dry_run" (never
    # "submitted") so the top-up guard never treats a dry-run order as real
    # allocated capital.
    assert c["status"] == "dry_run"
    assert c["order_id"] is not None
    assert c["order_id"].startswith("DRY-RUN")

    # orders.csv must be written
    assert "orders_csv" in res["paths"]
    orders_csv = Path(res["paths"]["orders_csv"])
    assert orders_csv.exists()
    rows = list(csv.DictReader(orders_csv.open(newline="", encoding="utf-8")))
    assert any(r["ticker"] == "AAPL" for r in rows)


def test_run_without_broker_does_not_place_orders(tmp_path):
    """MorningDCAExecutor.run() without broker records no order outcomes."""
    cfg = MorningConfig(tickers=("AAPL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.02})

    res = ex.run(_now(), data)
    candidates = res["artifacts"]["buy_candidates"]
    assert len(candidates) == 1
    assert "order_id" not in candidates[0]
    assert "orders_csv" not in res["paths"]


def test_classifications_validate_against_json_schema():
    """Both morning classifications must validate against the JSON schema.

    Skipped gracefully when jsonschema is not installed so the stdlib-only
    smoke run still passes.
    """
    try:
        import jsonschema
    except ImportError:
        return

    schema_path = Path(__file__).resolve().parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    cfg = MorningConfig(tickers=("SMALL",))

    watch = MorningDCAExecutor(cfg).run_classification(_now(), manual=False)
    live = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    manual = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=True)
    for rc in (watch, live, manual):
        jsonschema.validate(rc, schema)


def test_historical_v1_live_classification_still_validates_against_schema():
    # Codex/Copilot review (PR #78): the enum retains 'morning-live-v1'
    # explicitly tagged as "HISTORICAL/SUPERSEDED ... retained for backwards
    # compatibility with pre-2026-07-15 artifacts", but the schema's
    # scheduled-live branch used to require the v2-only
    # 'preauthorized_direct_place'/'valid_morning_evidence' fields for every
    # run_type='morning_dca_live' scheduled document regardless of
    # state_schema_version, contradicting that claim. A prior test cloned a
    # freshly generated v2 classification and only swapped the version tag,
    # which still carried the v2-only fields and so never exercised the real
    # v1 gap -- this builds an actual pre-2026-07-15 v1-shaped dict (those
    # fields did not exist yet) to verify it validates.
    try:
        import jsonschema
    except ImportError:
        return
    schema_path = Path(__file__).resolve().parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    cfg = MorningConfig(tickers=("SMALL",))
    historical_v1_rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    historical_v1_rc["state_schema_version"] = "morning-live-v1"
    historical_v1_rc.pop("preauthorized_direct_place", None)
    historical_v1_rc.pop("valid_morning_evidence", None)
    jsonschema.validate(historical_v1_rc, schema)


def test_morning_scheduled_run_counts_as_afternoon_prior_context():
    """A morning-stream scheduled run must be eligible prior-allocation context.

    _prior_run_is_scheduled excludes only the AFTERNOON scheduled run; a morning
    run (stream='morning') is scheduled within its own stream but must still be
    read as prior context for the afternoon same-day top-up guard.
    """
    from near_close_dca import _prior_run_is_scheduled

    cfg = MorningConfig(tickers=("SMALL",))
    morning_rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    assert morning_rc["run_origin"] == "scheduled"
    assert _prior_run_is_scheduled({"run_classification": morning_rc}) is False

    # An afternoon scheduled run (no morning stream marker) is still excluded.
    afternoon_rc = {"run_origin": "scheduled", "cron_id": "10ce362c"}
    assert _prior_run_is_scheduled({"run_classification": afternoon_rc}) is True


def test_market_open_scheduled_run_counts_as_afternoon_prior_context():
    """A market-open-stream scheduled run must be eligible prior-allocation context.

    Codex review ("Feed market-open fills into later top-up guards"): the
    9:35 AM market-open live task (2ba19526) is scheduled within its own
    stream but must still be read as prior context by the morning, midday,
    and afternoon same-day top-up guards, exactly like morning/midday.
    """
    from near_close_dca import _prior_run_is_scheduled

    market_open_rc = {
        "run_origin": "scheduled",
        "stream": "market_open",
        "cron_id": "2ba19526",
    }
    assert _prior_run_is_scheduled({"run_classification": market_open_rc}) is False


# --- 9:45 pre-flight validation + 10:30 live gate ---------------------------
def _now_preflight():
    """Decision timestamp for the 9:45 AM ET pre-flight validation mode."""
    return datetime.combine(date(2026, 7, 8), time(9, 45), tzinfo=EASTERN)


def test_preflight_eligibility_helper():
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG", "FLAT")), mode=MORNING_PREFLIGHT_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "FLAT": 0.0})
    sigs = {s.ticker: s for s in ex.evaluate(_now_preflight(), data)}
    # BIG dropped 3% -> BUY with positive multiplier -> eligible.
    assert preflight_eligibility(sigs["BIG"]) is True
    # FLAT is at prior close -> no buy -> not eligible.
    assert preflight_eligibility(sigs["FLAT"]) is False


def test_preflight_run_writes_validation_and_summary_and_pointer(tmp_path):
    cfg = MorningConfig(tickers=("BIG", "FLAT"), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_PREFLIGHT_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "FLAT": 0.0})
    res = ex.run(_now_preflight(), data)

    art = res["artifacts"]
    assert art["mode"] == "preflight"
    assert art["trades"] is False
    assert art["run_classification"]["run_type"] == "morning_dca_preflight"
    assert art["run_classification"]["state_schema_version"] == "morning-preflight-v2-machine-complete"
    assert art["eligible_to_execute_tickers"] == ["BIG"]
    assert art["counts"]["eligible_to_execute"] == 1
    # Every signal row carries the per-ticker verdict.
    verdicts = {r["ticker"]: r["eligible_to_execute"] for r in art["signal_rows"]}
    assert verdicts == {"BIG": True, "FLAT": False}

    run_dir = Path(res["paths"]["run_state"]).parent
    assert run_dir.name == "20260708_0945ET"
    assert (run_dir / "validation.csv").exists()
    assert (run_dir / "run_summary.json").exists()
    # Scheduled pre-flight advances the preflight-specific pointer only.
    assert (Path(tmp_path) / "latest_preflight.json").exists()
    assert not (Path(tmp_path) / "latest_morning_run.json").exists()


# --- Machine-complete pre-flight contract (morning-preflight-v2) ------------
_MACHINE_TICKER_FIELDS = {
    "symbol", "active_universe", "quote_available", "prior_close_available",
    "tradable", "account_ok", "buying_power_ok", "spread_pct", "spread_ok",
    "trend_status", "data_confidence_status", "early_drop_pct", "early_multiplier",
    "hypothetical_amount", "eligible_to_execute", "ineligible_reason",
}


class _UnresolvedProvider(FixedDropProvider):
    """Marks ``unresolved`` symbols as fully unresolvable (no quote, no prior close)."""

    def __init__(self, source, previous_close, drops, unresolved):
        super().__init__(source, previous_close, drops)
        self._unresolved = set(unresolved)

    def get_previous_close_bar(self, ticker, as_of):
        if ticker in self._unresolved:
            ts = datetime.combine(
                as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
            )
            return Bar(timestamp=ts, open=-1.0, high=-1.0, low=-1.0, close=-1.0, volume=0.0)
        return super().get_previous_close_bar(ticker, as_of)


def test_preflight_machine_complete_envelope(tmp_path):
    # Regression: this test previously constructed MorningConfig without a
    # morning_run_state_root override, so the executor fell back to the
    # production DEFAULT_MORNING_PREFLIGHT_STATE_ROOT
    # ("/home/user/workspace/..."). That path doesn't exist (and isn't
    # writable) on CI runners, so the test failed with PermissionError there
    # even though it happened to pass on a dev machine that already had the
    # directory. Every artifact-writing test must scope state under tmp_path.
    ex = MorningDCAExecutor(
        MorningConfig(tickers=("BIG", "FLAT"), morning_run_state_root=str(tmp_path)),
        mode=MORNING_PREFLIGHT_MODE,
    )
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "FLAT": 0.0})
    art = ex.run(_now_preflight(), data)["artifacts"]

    # Every required top-level machine field is present.
    for key in ("overall_ok", "eligible_count", "ineligible_count",
                "unresolved_tickers", "blocking_errors", "warnings",
                "run_classification", "per_ticker_eligibility"):
        assert key in art, key

    # No global failure -> overall_ok true even with an ineligible ticker.
    assert art["overall_ok"] is True
    assert art["blocking_errors"] == []

    rows = {r["symbol"]: r for r in art["per_ticker_eligibility"]}
    # Every active symbol appears exactly once with the full field set.
    assert set(rows) == {"BIG", "FLAT"}
    assert len(art["per_ticker_eligibility"]) == 2
    for row in art["per_ticker_eligibility"]:
        assert set(row.keys()) == _MACHINE_TICKER_FIELDS
        # Coarse read-only pre-flight does not bind account/spread/trend.
        assert row["account_ok"] is None
        assert row["buying_power_ok"] is None
        assert row["spread_pct"] is None
        assert row["spread_ok"] is None
        assert row["trend_status"] == "not_evaluated_preflight"

    assert rows["BIG"]["eligible_to_execute"] is True
    assert rows["BIG"]["ineligible_reason"] is None
    assert rows["FLAT"]["eligible_to_execute"] is False
    assert rows["FLAT"]["ineligible_reason"] == "no_qualifying_drop"

    assert art["eligible_count"] == 1
    assert art["ineligible_count"] == 1
    assert art["unresolved_tickers"] == []


def test_preflight_unresolved_ticker_does_not_invalidate_run(tmp_path):
    # An unresolved symbol (no quote, no prior close) must be a per-symbol row
    # with eligible_to_execute=false and ineligible_reason='unresolved_ticker',
    # listed in unresolved_tickers, and MUST NOT flip overall_ok.
    # Same tmp_path scoping fix as test_preflight_machine_complete_envelope
    # above: without morning_run_state_root, this writes to the hardcoded
    # production path and fails with PermissionError on CI runners.
    ex = MorningDCAExecutor(
        MorningConfig(tickers=("BIG", "AAAU"), morning_run_state_root=str(tmp_path)),
        mode=MORNING_PREFLIGHT_MODE,
    )
    data = _UnresolvedProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "AAAU": 1.0}, unresolved=("AAAU",)
    )
    art = ex.run(_now_preflight(), data)["artifacts"]

    assert art["overall_ok"] is True
    assert "AAAU" in art["unresolved_tickers"]
    rows = {r["symbol"]: r for r in art["per_ticker_eligibility"]}
    assert rows["AAAU"]["eligible_to_execute"] is False
    assert rows["AAAU"]["ineligible_reason"] == "unresolved_ticker"
    assert rows["AAAU"]["data_confidence_status"] == "unresolved"
    assert rows["AAAU"]["quote_available"] is False
    assert rows["AAAU"]["prior_close_available"] is False
    # BIG still resolves and stays eligible alongside the unresolved row.
    assert rows["BIG"]["eligible_to_execute"] is True


def test_preflight_ticker_eligibility_row_unresolved_unit():
    sig = MorningSignal(
        ticker="SPCX",
        source=SourceName.ROBINHOOD.value,
        action=Action.NO_DATA,
        reason="unresolved",
        current_price=None,
        previous_close=None,
        drop_pct=None,
        multiplier=0.0,
        hypothetical_amount_usd=0.0,
        warnings=("lookup failed",),
    )
    row = preflight_ticker_eligibility_row(sig)
    assert set(row.keys()) == _MACHINE_TICKER_FIELDS
    assert row["symbol"] == "SPCX"
    assert row["eligible_to_execute"] is False
    assert row["ineligible_reason"] == "unresolved_ticker"
    assert row["data_confidence_status"] == "unresolved"
    assert row["tradable"] is False


def test_preflight_machine_complete_validates_against_json_schema(tmp_path):
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return

    schemas_dir = Path(__file__).resolve().parent / "schemas"
    main = json.loads((schemas_dir / "morning_preflight.schema.json").read_text(encoding="utf-8"))
    resources = [
        (name, Resource.from_contents(
            json.loads((schemas_dir / name).read_text(encoding="utf-8"))))
        for name in ("morning_preflight.schema.json", "morning_run_classification.schema.json")
    ]
    registry = Registry().with_resources(resources)

    ex = MorningDCAExecutor(
        MorningConfig(tickers=("BIG", "AAAU"), morning_run_state_root=str(tmp_path)),
        mode=MORNING_PREFLIGHT_MODE,
    )
    data = _UnresolvedProvider(
        SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "AAAU": 1.0}, unresolved=("AAAU",)
    )
    art = ex.run(_now_preflight(), data)["artifacts"]
    jsonschema.Draft7Validator(main, registry=registry).validate(art)


def test_load_same_day_preflight_reads_eligible_set(tmp_path):
    cfg = MorningConfig(tickers=("BIG", "FLAT"), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_PREFLIGHT_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "FLAT": 0.0})
    ex.run(_now_preflight(), data)

    loaded = load_same_day_preflight(str(tmp_path), date(2026, 7, 8))
    assert loaded is not None
    assert loaded["eligible_tickers"] == {"BIG"}
    # No artifact for a different day.
    assert load_same_day_preflight(str(tmp_path), date(2026, 7, 9)) is None


def test_load_same_day_preflight_reads_per_ticker_eligibility_v2(tmp_path):
    # Codex review #92: a strictly schema-compliant v2 preflight artifact is
    # only required to carry per_ticker_eligibility, not the legacy
    # eligible_to_execute_tickers convenience field. The reader must derive
    # eligibility from per_ticker_eligibility instead of silently returning
    # an empty set when the legacy field is absent.
    run_dir = tmp_path / "20260708_0945ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "stream": "morning",
            },
            "per_ticker_eligibility": [
                {"symbol": "BIG", "eligible_to_execute": True},
                {"symbol": "FLAT", "eligible_to_execute": False},
            ],
        }),
        encoding="utf-8",
    )

    loaded = load_same_day_preflight(str(tmp_path), date(2026, 7, 8))

    assert loaded is not None
    assert loaded["eligible_tickers"] == {"BIG"}


def test_load_same_day_preflight_primary_path_manual_state_rejected(tmp_path):
    # Codex review r3622685590: the primary direct-path lookup only checked
    # overall_ok, so a manual state left/copied into today's expected
    # directory would satisfy the 10:30 scheduled live gate just because a
    # file happened to exist there. Must be rejected the same as a stale or
    # wrong-stream pointer target.
    run_dir = tmp_path / "20260708_0945ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "manual_morning_preflight_20260708",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "run_classification": {"run_origin": "manual", "manual_run": True, "stream": "morning"},
            "eligible_to_execute_tickers": ["BIG"],
        }),
        encoding="utf-8",
    )

    assert load_same_day_preflight(str(tmp_path), date(2026, 7, 8), bounded_recovery=False) is None


def test_load_same_day_preflight_primary_path_prior_day_state_rejected(tmp_path):
    # Codex review r3622685590: a prior-day run_state copied (or never
    # cleaned up) into today's expected directory name must not be trusted
    # just because the directory name and file location happen to be right --
    # the state's OWN trading_day must independently match today.
    run_dir = tmp_path / "20260708_0945ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "morning_preflight_20260707",
            "trading_day": "2026-07-07",
            "overall_ok": True,
            "run_classification": {"run_origin": "scheduled", "manual_run": False, "stream": "morning"},
            "eligible_to_execute_tickers": ["BIG"],
        }),
        encoding="utf-8",
    )

    assert load_same_day_preflight(str(tmp_path), date(2026, 7, 8), bounded_recovery=False) is None


def test_load_same_day_preflight_primary_path_wrong_stream_rejected(tmp_path):
    # Codex review r3622685597: a same-day, scheduled, non-manual state whose
    # OWN run_classification.stream is a different stream must not be trusted
    # just because it was found at the morning preflight's expected path
    # (e.g. a misplaced/copied midday artifact).
    run_dir = tmp_path / "20260708_0945ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "midday_live_20260708_1230ET",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "run_classification": {"run_origin": "scheduled", "manual_run": False, "stream": "midday"},
            "eligible_to_execute_tickers": ["BIG"],
        }),
        encoding="utf-8",
    )

    assert load_same_day_preflight(str(tmp_path), date(2026, 7, 8), bounded_recovery=False) is None


def test_load_same_day_preflight_pointer_wrong_stream_rejected(tmp_path):
    # Codex review r3622223176: the primary-path miss must route
    # latest_preflight.json through same_day_pointer.validate_pointer rather
    # than trusting any same-date, existing run_state_path. A pointer
    # claiming the wrong stream must not satisfy the preflight gate.
    target_dir = tmp_path / "manual_runs" / "morning_preflight_20260708"
    target_dir.mkdir(parents=True)
    target_path = target_dir / "run_state.json"
    target_path.write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "run_classification": {"run_origin": "scheduled", "manual_run": False},
            "eligible_to_execute_tickers": ["BIG"],
        }),
        encoding="utf-8",
    )
    pointer = tmp_path / "latest_preflight.json"
    pointer.write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "run_state_path": str(target_path),
            "trading_day": "2026-07-08",
            "stream": "midday",
        }),
        encoding="utf-8",
    )

    assert load_same_day_preflight(str(tmp_path), date(2026, 7, 8), bounded_recovery=False) is None


def test_load_same_day_preflight_pointer_manual_target_rejected(tmp_path):
    # A pointer whose target run_state is a manual run must not satisfy the
    # preflight gate even though it names an existing, same-date file.
    target_dir = tmp_path / "manual_runs" / "manual_x"
    target_dir.mkdir(parents=True)
    target_path = target_dir / "run_state.json"
    target_path.write_text(
        json.dumps({
            "run_id": "manual_x",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "eligible_to_execute_tickers": ["BIG"],
        }),
        encoding="utf-8",
    )
    pointer = tmp_path / "latest_preflight.json"
    pointer.write_text(
        json.dumps({
            "run_id": "manual_x",
            "run_state_path": str(target_path),
            "trading_day": "2026-07-08",
            "stream": "morning",
        }),
        encoding="utf-8",
    )

    assert load_same_day_preflight(str(tmp_path), date(2026, 7, 8), bounded_recovery=False) is None


def test_load_same_day_preflight_pointer_valid_used_as_fallback(tmp_path):
    # A well-formed same-day pointer whose target is a genuine scheduled,
    # non-manual preflight run_state IS used when the primary path misses.
    target_dir = tmp_path / "some_other_dir"
    target_dir.mkdir(parents=True)
    target_path = target_dir / "run_state.json"
    target_path.write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "run_classification": {"run_origin": "scheduled", "manual_run": False},
            "eligible_to_execute_tickers": ["BIG"],
        }),
        encoding="utf-8",
    )
    pointer = tmp_path / "latest_preflight.json"
    pointer.write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "run_state_path": str(target_path),
            "trading_day": "2026-07-08",
            "stream": "morning",
            "pointer_status": "valid",
        }),
        encoding="utf-8",
    )

    loaded = load_same_day_preflight(str(tmp_path), date(2026, 7, 8), bounded_recovery=False)
    assert loaded is not None
    assert loaded["eligible_tickers"] == {"BIG"}


def test_load_same_day_preflight_fails_closed_when_overall_ok_false(tmp_path):
    # Codex review #93: a preflight artifact that explicitly declares itself
    # invalid (overall_ok=False, e.g. a global/account/buying-power/auth/
    # schema-write failure) must be treated the same as "no preflight found",
    # not consumed for whatever eligibility data happens to be present.
    run_dir = tmp_path / "20260708_0945ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "trading_day": "2026-07-08",
            "overall_ok": False,
            "blocking_errors": ["account lookup failed"],
            # Even if some eligibility data is present, it must not be trusted.
            "eligible_to_execute_tickers": ["BIG"],
        }),
        encoding="utf-8",
    )

    assert load_same_day_preflight(str(tmp_path), date(2026, 7, 8)) is None


def test_live_gate_restricts_to_preflight_eligible(tmp_path):
    preflight_root = tmp_path / "preflight"
    pf = MorningDCAExecutor(
        MorningConfig(tickers=("BIG", "FLAT"), morning_run_state_root=str(preflight_root)),
        mode=MORNING_PREFLIGHT_MODE,
    )
    pf_data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "FLAT": 0.0})
    pf.run(_now_preflight(), pf_data)

    live = MorningDCAExecutor(
        MorningConfig(
            tickers=("BIG", "FLAT"),
            morning_run_state_root=str(tmp_path / "live"),
            preflight_run_state_root=str(preflight_root),
        ),
        mode=MORNING_LIVE_MODE,
    )
    live_data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10, "FLAT": 0.10})
    res = live.run(_now_live(), live_data, broker=FakeLiveBroker())
    art = res["artifacts"]
    # Only BIG was admitted; FLAT (ineligible at 9:45) is gated out even though it
    # would have qualified at 10:30.
    gate = art["preflight_gate"]
    assert gate["preflight_found"] is True
    assert gate["admitted_tickers"] == ["BIG"]
    assert gate["gated_out_tickers"] == ["FLAT"]
    evaluated = {r["ticker"] for r in art["signal_rows"]}
    assert evaluated == {"BIG"}


def test_live_gate_fails_closed_when_preflight_missing_scheduled(tmp_path):
    # Scheduled live runs must write a durable blocked run_state (fail-closed)
    # when no preflight artifact exists, rather than raising and leaving no
    # artifact for watchdog/reporting to observe.
    state_root = str(tmp_path / "live")
    live = MorningDCAExecutor(
        MorningConfig(
            tickers=("BIG", "FLAT"),
            morning_run_state_root=state_root,
            preflight_run_state_root=str(tmp_path / "empty_preflight"),
        ),
        mode=MORNING_LIVE_MODE,
    )
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10, "FLAT": 0.10})
    res = live.run(_now_live(), data)
    art = res["artifacts"]
    assert art["status"] == "blocked"
    assert "pre-flight" in art["block_reason"]
    assert "run_id" in art
    assert "generated_at" in art
    assert "counts" in art
    assert "run_classification" in art
    state_path = Path(res["paths"]["run_state"])
    assert state_path.exists()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "blocked"
    assert persisted["trades"] is False
    assert persisted["run_id"] == art["run_id"]
    # Latest pointer must be updated for scheduled runs.
    pointer_key = live.latest_pointer_name.replace(".json", "")
    assert pointer_key in res["paths"]

    # Codex review #77: "write a valid blocked run_state for missing
    # preflight" -- the persisted artifact must use the normal scheduled
    # artifact shape (run_id, generated_at, counts, run_classification), and
    # the latest scheduled pointer must still be updated, so watchdog and
    # reporting can validate this as a real (blocked) scheduled-run attempt
    # rather than a malformed/missing one.
    for key in ("run_id", "generated_at", "counts", "run_classification"):
        assert key in persisted, f"blocked run_state is missing {key!r}"
    assert persisted["run_classification"]["run_origin"] == "scheduled"
    assert persisted["run_classification"]["manual_run"] is False
    pointer_path = Path(state_root) / "latest_morning_run.json"
    assert pointer_path.exists()
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["run_id"] == persisted["run_id"]


def test_live_gate_degrades_to_full_universe_when_preflight_missing_manual(tmp_path):
    # Manual live runs degrade to the full universe with a warning when no preflight exists.
    live = MorningDCAExecutor(
        MorningConfig(
            tickers=("BIG", "FLAT"),
            morning_run_state_root=str(tmp_path / "live"),
            preflight_run_state_root=str(tmp_path / "empty_preflight"),
        ),
        mode=MORNING_LIVE_MODE,
    )
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10, "FLAT": 0.10})
    res = live.run(_now_live(), data, manual=True)
    art = res["artifacts"]
    assert art["preflight_gate"]["preflight_found"] is False
    evaluated = {r["ticker"] for r in art["signal_rows"]}
    assert evaluated == {"BIG", "FLAT"}
    assert any("no same-day 9:45 pre-flight" in w for w in art["warnings"])


def test_preflight_classification_matches_morning_schema():
    schema_path = Path(__file__).resolve().parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    cfg = MorningConfig(tickers=("BIG",))
    rc = MorningDCAExecutor(cfg, mode=MORNING_PREFLIGHT_MODE).run_classification(
        _now_preflight(), manual=False
    )
    assert rc["run_type"] == "morning_dca_preflight"
    assert rc["cron_id"] == "morning-dca-0945"
    assert rc["state_schema_version"] == "morning-preflight-v2-machine-complete"
    assert rc["run_id"] == "morning_preflight_20260708_0945ET"
    assert set(schema["required"]).issubset(rc.keys())
    try:
        import jsonschema
    except ImportError:
        return
    jsonschema.validate(rc, schema)
    manual_rc = MorningDCAExecutor(cfg, mode=MORNING_PREFLIGHT_MODE).run_classification(
        _now_preflight(), manual=True
    )
    jsonschema.validate(manual_rc, schema)


def test_preflight_root_distinct_from_watchlist_and_live():
    assert DEFAULT_MORNING_PREFLIGHT_STATE_ROOT != DEFAULT_MORNING_LIVE_STATE_ROOT
    assert DEFAULT_MORNING_PREFLIGHT_STATE_ROOT != DEFAULT_MORNING_WATCHLIST_STATE_ROOT
    assert "morning_dca_preflight" in DEFAULT_MORNING_PREFLIGHT_STATE_ROOT


def _seed_preflight(tmp_path, tickers, trading_date=None):
    """Write a preflight run_state.json for the given tickers into tmp_path.

    Carries a real (scheduled, non-manual, morning-stream) run_classification
    and trading_day matching production shape, so it passes the same-day
    scheduled-evidence checks applied to the primary preflight path.
    """
    if trading_date is None:
        trading_date = _now_live().date()
    folder = trading_date.strftime("%Y%m%d") + "_0945ET"
    pf_dir = tmp_path / folder
    pf_dir.mkdir(parents=True, exist_ok=True)
    (pf_dir / "run_state.json").write_text(
        json.dumps({
            "eligible_to_execute_tickers": list(tickers),
            "trading_day": trading_date.isoformat(),
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "stream": "morning",
            },
        }),
        encoding="utf-8",
    )
    return tmp_path


def test_live_gate_blocks_outside_approved_window(tmp_path):
    # A scheduled live run on a day outside approved_test_dates returns a blocked result.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    live_root = tmp_path / "live"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 7)}),  # not 2026-07-08 (_now_live())
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, manual=False)
    assert res["artifacts"]["status"] == "blocked"
    assert res["artifacts"].get("block_reason") == "outside approved trading window"
    # Codex review r3622495870: this date-gate block must still refresh the
    # same-day pointer(s) -- both latest_morning_run.json and
    # latest_morning_live.json for the morning live stream -- so a consumer
    # checking "did today's scheduled task fire" doesn't see a stale pointer
    # left over from the last real trading session.
    for name in ("latest_morning_run.json", "latest_morning_live.json"):
        pointer = json.loads((live_root / name).read_text())
        assert pointer["trading_day"] == "2026-07-08"
        assert pointer["valid_scheduled_evidence"] is True
    # Codex review r3623389445: this gate bypasses write_artifacts() entirely,
    # so it must still write submitted_orders.csv (header only) itself, or the
    # run directory fails scripts/scheduled_artifacts.py's live artifact
    # contract and detect_drift() sees a missing-CSV failure.
    orders_path = Path(res["paths"]["submitted_orders"])
    assert orders_path.exists()
    with orders_path.open(newline="", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh)) == []


def test_outside_window_retry_refreshes_pointer_to_preserved_submitted_evidence(tmp_path):
    # Codex review r3622495870, existing-evidence branch: a retry that lands in
    # _maybe_block_for_holiday's "already has submitted-order evidence" path
    # (e.g. the 10:30 live run placed orders, then trading_window.json briefly
    # made a retry look outside the approved window) must still refresh the
    # same-day pointer -- to the PRESERVED submitted state, not a fresh blocked
    # stub -- so it doesn't fall back to a stale prior-day pointer.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    live_root = tmp_path / "live"
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),  # in window
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    assert first["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    run_state_path = Path(first["paths"]["run_state"])

    # Retry at the same slot with a config that now excludes today -> hits the
    # is_outside_window gate, but must preserve (not overwrite) the submitted
    # evidence and still refresh the pointer to point at it.
    retry_cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 7)}),  # no longer approved
    )
    retry_ex = MorningDCAExecutor(retry_cfg, mode=MORNING_LIVE_MODE)
    second = retry_ex.run(_now_live() + timedelta(minutes=1), data, broker=FakeLiveBroker())

    assert second["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    on_disk = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert on_disk["buy_candidates"][0]["status"] == "submitted"
    for name in ("latest_morning_run.json", "latest_morning_live.json"):
        pointer = json.loads((live_root / name).read_text())
        assert pointer["trading_day"] == "2026-07-08"
        assert pointer["valid_scheduled_evidence"] is True
        assert pointer["run_state_path"] == str(run_state_path)


def test_live_gate_permits_when_in_approved_window(tmp_path):
    # A scheduled live run on a day inside approved_test_dates is not date-gated.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),  # matches _now_live()
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, manual=False, broker=FakeLiveBroker())
    assert res["artifacts"].get("status") != "blocked"


def test_live_gate_manual_bypasses_approved_window(tmp_path):
    # Manual runs are never blocked by the approved_test_dates gate.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 7)}),  # not today
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, manual=True)
    assert res["artifacts"].get("status") != "blocked"


def test_morning_live_no_broker_does_not_record_hypothetical_ledger_spend(tmp_path):
    # Copilot review: no regression test covered the no-broker live-mode path
    # this fix introduced. broker=None means no orders were actually placed
    # (broker_outcomes stays None), so record_buy_candidates() must be skipped
    # entirely for the morning live stream -- the ledger must stay at $0
    # despite SMALL's real hypothetical $5.00 signal, so a later broker-backed
    # retry sees the FULL cap available rather than being pre-charged for
    # spend that never happened.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),  # matches _now_live()
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # DecliningBarsProvider (not FixedDropProvider): the fix's target code path
    # is only reached when SMALL clears trend validation into auto_buy_candidates
    # (hypothetical_amount_usd > 0); FixedDropProvider's flat intraday bars fail
    # trend validation, routing SMALL to manual_review_candidate instead, which
    # would make this test pass vacuously regardless of the fix.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, manual=True, broker=None)
    assert res["artifacts"].get("status") != "blocked"
    sm = res["artifacts"]["buy_candidates"][0]
    assert sm["hypothetical_amount_usd"] == 5.0  # confirms this exercises the fix's target path
    trading_day = _now_live().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(trading_day) == 0.0
    assert ex.ledger.week_planned(trading_day) == 0.0


def test_scheduled_live_run_without_broker_fails_closed(tmp_path):
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(_now_live(), data, broker=None)

    assert res["artifacts"]["status"] == "blocked"
    assert res["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    assert res["artifacts"]["trades"] is True
    assert res["artifacts"]["broker_outcomes"] == {}
    written = json.loads(Path(res["paths"]["run_state"]).read_text(encoding="utf-8"))
    assert written["status"] == "blocked"
    assert written["run_classification"]["run_type"] == "morning_dca_live"
    # Codex review on PR #22: this fail-closed stub must go through the normal
    # build_artifacts()/write_artifacts() pipeline, not a hand-rolled dict, so
    # watchdog/latest-pointer consumers see it as an intentional block rather
    # than a malformed or missing run.
    assert written["run_id"]
    assert written["generated_at"]
    assert written["counts"]["evaluated"] == 1
    assert len(written["signal_rows"]) == 1
    assert "run_state" in res["paths"]
    assert "latest_morning_run" in res["paths"]
    pointer_path = Path(res["paths"]["latest_morning_run"])
    assert pointer_path.exists()
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["run_id"] == written["run_id"]


def test_scheduled_live_run_with_wrong_broker_account_fails_closed_before_ordering(tmp_path):
    class _WrongAccountBroker:
        def __init__(self):
            self.placed = False

        def account_number(self):
            return "not-the-approved-account"

        def available_buying_power(self):
            raise AssertionError("buying power should not be checked for wrong account")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed = True
            raise AssertionError("orders must not be placed for wrong account")

    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        target_account_number="562057042",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _WrongAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(_now_live(), data, broker=broker)

    assert res["artifacts"]["status"] == "blocked"
    assert res["artifacts"]["block_reason"] == (
        "broker account mismatch: expected 562057042, got not-the-approved-account"
    )
    assert broker.placed is False
    assert Path(res["paths"]["run_state"]).exists()
    # Same normal-artifact-pipeline requirement as the missing-broker case above.
    assert res["artifacts"]["run_id"]
    assert res["artifacts"]["generated_at"]
    assert res["artifacts"]["counts"]["evaluated"] == 1
    assert "latest_morning_run" in res["paths"]
    assert Path(res["paths"]["latest_morning_run"]).exists()


def test_manual_live_run_with_wrong_broker_account_raises_before_ordering(tmp_path):
    import pytest

    class _WrongAccountBroker:
        def __init__(self):
            self.buying_power_checked = False
            self.placed = False

        def account_number(self):
            return "not-the-approved-account"

        def available_buying_power(self):
            self.buying_power_checked = True
            raise AssertionError("buying power should not be checked for wrong account")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed = True
            raise AssertionError("orders must not be placed for wrong account")

    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        target_account_number="562057042",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _WrongAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="broker account mismatch: expected 562057042, got not-the-approved-account"):
        ex.run(_now_live(), data, manual=True, broker=broker)

    assert broker.buying_power_checked is False
    assert broker.placed is False


def test_scheduled_live_run_with_broker_account_number_error_fails_closed(tmp_path):
    # broker.account_number() can raise (misconfigured/uninitialized adapter,
    # connector network/API error). Left uncaught this crashes the process
    # with a raw traceback instead of producing the durable run_state.json a
    # scheduled retry depends on.
    class _RaisingAccountBroker:
        def __init__(self):
            self.placed = False

        def account_number(self):
            raise ConnectionError("robinhood connector unavailable")

        def available_buying_power(self):
            raise AssertionError("buying power should not be checked when account lookup fails")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed = True
            raise AssertionError("orders must not be placed when account lookup fails")

    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        target_account_number="562057042",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _RaisingAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(_now_live(), data, broker=broker)

    assert res["artifacts"]["status"] == "blocked"
    assert "failed to retrieve broker account number" in res["artifacts"]["block_reason"]
    assert broker.placed is False
    assert Path(res["paths"]["run_state"]).exists()


def test_manual_live_run_with_broker_account_number_error_raises_runtime_error(tmp_path):
    import pytest

    class _RaisingAccountBroker:
        def __init__(self):
            self.placed = False

        def account_number(self):
            raise ConnectionError("robinhood connector unavailable")

        def available_buying_power(self):
            raise AssertionError("buying power should not be checked when account lookup fails")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed = True
            raise AssertionError("orders must not be placed when account lookup fails")

    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        target_account_number="562057042",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _RaisingAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="Failed to retrieve broker account number"):
        ex.run(_now_live(), data, manual=True, broker=broker)

    assert broker.placed is False


def test_blocked_live_retry_preserves_prior_submitted_evidence(tmp_path):
    # Codex/Gemini review on PR #46: a scheduled retry that hits a
    # scheduler-wiring safety failure (missing broker, wrong account) must not
    # overwrite an EARLIER scheduled attempt's run_state.json for the same slot
    # if that state already has real submitted-order evidence -- that evidence
    # is required for same-day top-up guards and reporting, and a sparse
    # blocked stub would erase it.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    # First scheduled attempt: broker wired correctly, order submitted.
    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    assert first["artifacts"].get("status") != "blocked"
    assert first["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    run_state_path = Path(first["paths"]["run_state"])
    assert run_state_path.exists()

    # Second scheduled attempt at the SAME slot: broker adapter missing this
    # time (a wiring regression). The prior submitted evidence must survive.
    second = ex.run(_now_live(), data, broker=None)

    assert second["artifacts"].get("status") != "blocked", (
        "a later blocked scheduler-wiring retry must not overwrite prior submitted evidence"
    )
    assert second["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    on_disk = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert on_disk["buy_candidates"][0]["status"] == "submitted"


def test_blocked_live_retry_does_not_preserve_stale_non_submitted_block(tmp_path):
    # Codex review on PR #46: the preservation above must NOT apply blindly --
    # if the existing state is itself just a prior blocked stub (no submitted
    # evidence), a later retry with a DIFFERENT block_reason must overwrite it
    # with the new reason, not hide the new failure behind the old one.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        target_account_number="562057042",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    # First scheduled attempt: broker adapter missing -> blocked stub, no
    # submitted evidence.
    first = ex.run(_now_live(), data, broker=None)
    assert first["artifacts"]["status"] == "blocked"
    assert first["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    run_state_path = Path(first["paths"]["run_state"])

    # Second scheduled attempt at the SAME slot: broker present but wrong
    # account. This is a DIFFERENT failure and must be recorded, not masked by
    # the stale first block_reason.
    class _WrongAccountBroker:
        def account_number(self):
            return "not-the-approved-account"

    second = ex.run(_now_live(), data, broker=_WrongAccountBroker())

    assert second["artifacts"]["status"] == "blocked"
    assert second["artifacts"]["block_reason"] == (
        "broker account mismatch: expected 562057042, got not-the-approved-account"
    )
    on_disk = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert on_disk["block_reason"] == (
        "broker account mismatch: expected 562057042, got not-the-approved-account"
    )


def test_blocked_live_retry_recovers_from_corrupted_existing_run_state(tmp_path):
    # Codex/Gemini review on PR #46: an existing run_state.json that is
    # unreadable or invalid JSON (e.g. a crash mid-write) must not crash the
    # scheduler-wiring safety block -- it must fall back to writing a fresh
    # blocked stub, preserving the fail-closed guarantee this path exists for.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text("{not valid json", encoding="utf-8")

    result = ex.run(_now_live(), data, broker=None)

    assert result["artifacts"]["status"] == "blocked"
    assert result["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_text(encoding="utf-8"))
    assert on_disk["block_reason"] == "missing broker adapter for scheduled live execution"


def test_blocked_live_retry_recovers_from_non_utf8_existing_run_state(tmp_path):
    # Codex review on PR #47: a run_state.json containing invalid UTF-8 bytes
    # (e.g. truncated mid-write on a multi-byte boundary, or corrupted on disk)
    # raises UnicodeDecodeError from read_text(encoding="utf-8"), which is
    # neither OSError nor json.JSONDecodeError. That must not crash the
    # scheduler-wiring safety block -- it must fall back to a fresh blocked
    # stub the same as any other unreadable existing state.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_bytes(b"\xff\xfe\x00invalid-utf8")

    result = ex.run(_now_live(), data, broker=None)

    assert result["artifacts"]["status"] == "blocked"
    assert result["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_text(encoding="utf-8"))
    assert on_disk["block_reason"] == "missing broker adapter for scheduled live execution"


def test_blocked_live_retry_recovers_from_non_string_trading_day_in_existing_state(tmp_path):
    # Codex review on PR #77: an existing run_state.json that is valid,
    # parseable UTF-8 JSON but has a non-string, truthy "trading_day" field
    # (e.g. a partially recovered artifact where the field was clobbered to a
    # list) raises TypeError from date.fromisoformat(), which the seeding
    # loop's narrower `except ValueError` did not catch (an empty `[]` is
    # falsy and short-circuits to None before fromisoformat is ever called,
    # so the field must be truthy to exercise this). Because ledger seeding
    # runs before the scheduler-wiring fail-closed safety block, that
    # TypeError used to crash the retry instead of producing the expected
    # blocked artifact. Must fall back to a fresh blocked stub, same as any
    # other unreadable/malformed existing state.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({"trading_day": ["2026-07-08"], "run_id": "prior-run", "buy_candidates": []}),
        encoding="utf-8",
    )

    result = ex.run(_now_live(), data, broker=None)

    assert result["artifacts"]["status"] == "blocked"
    assert result["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_text(encoding="utf-8"))
    assert on_disk["block_reason"] == "missing broker adapter for scheduled live execution"


def test_blocked_live_retry_recovers_from_non_object_existing_run_state(tmp_path):
    # Codex review on PR #77: an existing run_state.json that is valid,
    # parseable JSON but whose root is a list (or other non-object value)
    # rather than a dict -- e.g. a partially recovered/truncated artifact --
    # raises AttributeError from state.get(...), since json.loads() succeeds
    # but the result has no .get(). Because ledger seeding runs before the
    # scheduler-wiring fail-closed safety block, that AttributeError used to
    # crash the retry instead of producing the expected blocked artifact.
    # Must fall back to a fresh blocked stub, same as any other
    # unreadable/malformed existing state.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    result = ex.run(_now_live(), data, broker=None)

    assert result["artifacts"]["status"] == "blocked"
    assert result["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_text(encoding="utf-8"))
    assert on_disk["block_reason"] == "missing broker adapter for scheduled live execution"


def test_blocked_live_retry_recovers_from_null_broker_outcomes_in_existing_state(tmp_path):
    # Codex review (PR #88): a parseable but structurally corrupted existing
    # run_state.json with "broker_outcomes": null (or "buy_candidates": null)
    # used to raise AttributeError from .values() / TypeError from iterating
    # None, since dict.get(key, default) only substitutes the default when the
    # KEY IS ABSENT, not when it's present with value null. That crashed the
    # scheduler's fail-closed safety path on exactly the kind of malformed
    # prior state it exists to recover from. Must fall back to a fresh blocked
    # stub instead, same as a missing or unreadable existing file.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({"buy_candidates": None, "broker_outcomes": None}), encoding="utf-8"
    )

    result = ex.run(_now_live(), data, broker=None)

    assert result["artifacts"]["status"] == "blocked"
    assert result["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_text(encoding="utf-8"))
    assert on_disk["block_reason"] == "missing broker adapter for scheduled live execution"


def test_preflight_missing_block_retry_preserves_prior_submitted_evidence(tmp_path):
    # Codex review (PR #88): "Preserve submitted state on a preflight-blocked
    # retry". Unlike _blocked_live_result(), the preflight_missing_block
    # branch in run() built its blocked stub directly and never called
    # _existing_submitted_run_state() first. If an earlier attempt at the
    # same scheduled slot already submitted a real order, and the same-day
    # preflight artifact becomes unavailable by the time a later retry runs
    # (e.g. deleted, or a transient read failure), this path would overwrite
    # the dated-slot run_state.json with a blocked stub and erase that
    # submitted-order evidence.
    pf_dir = tmp_path / "pf"
    pf_root = _seed_preflight(pf_dir, ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    # First attempt: preflight available, order genuinely submitted.
    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    assert first["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    run_state_path = Path(first["paths"]["run_state"])
    assert run_state_path.exists()

    # The same-day preflight artifact becomes unavailable before the retry
    # (e.g. deleted, transient read failure, disk issue).
    for f in pf_dir.glob("*/run_state.json"):
        f.unlink()

    # Retry at the same slot: preflight load now fails -> preflight_missing_block.
    # The prior submitted evidence must survive, not be overwritten by the
    # blocked stub.
    retry_now = _now_live() + timedelta(minutes=1)
    second = ex.run(retry_now, data, broker=FakeLiveBroker())

    assert second["artifacts"].get("status") != "blocked", (
        "a preflight-missing-block retry must not overwrite prior submitted evidence"
    )
    assert second["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    on_disk = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert on_disk["buy_candidates"][0]["status"] == "submitted"


def test_auto_buy_candidates_prioritized_by_multiplier_when_cap_hit(tmp_path):
    # When the daily cap is tight, higher-multiplier tickers are placed first.
    # BIGDROP: 8% drop -> 5x -> $10 notional. SMALLDROP: 1% drop -> 1.5x -> $3.00 notional.
    # Daily cap of $11 allows BIGDROP ($10 < $11) then blocks SMALLDROP ($10+$3 > $11).
    # The sort ensures the high-multiplier ticker gets the budget slot, not whichever
    # happened to be evaluated first.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _FakeLiveBroker:
        """Returns real-looking order IDs so submitted_today accumulates."""
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id): return f"ORDER-{ticker}"

    pf_root = _seed_preflight(tmp_path / "pf", ("BIGDROP", "SMALLDROP"))
    cfg = MorningConfig(
        tickers=("BIGDROP", "SMALLDROP"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        morning_daily_cap_usd=11.0,
        morning_weekly_cap_usd=500.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # BIGDROP: 8% drop -> 5x multiplier -> $10. SMALLDROP: 1% drop -> 1.5x -> $3.00.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIGDROP": 0.08, "SMALLDROP": 0.01})
    res = ex.run(_now_live(), data, broker=_FakeLiveBroker())
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("BIGDROP", {}).get("status") == "submitted", "BIGDROP (5x) should be placed first"
    assert broker_outcomes.get("SMALLDROP", {}).get("status") == "cap_exceeded", "SMALLDROP (1.5x) should be cap-gated out"


# ── Morning top-up guard ─────────────────────────────────────────────────────

def _write_prior_run_state(
    root: Path,
    ticker: str,
    *,
    amount_usd: float,
    decision_price: float,
    selected_base_amount_usd=None,
) -> None:
    """Write a minimal manual prior-run run_state.json under a dated directory.

    Uses status="filled": near_close_dca._candidate_logged_reference_price's
    reference-price policy only trusts a candidate's logged target/decision
    price once capital movement is confirmed (status="filled" or a matching
    fill row) -- an unconfirmed "submitted" row keeps an unknown reference
    price and routes the top-up guard to missing_prior_price/manual_review
    instead of comparing prices, which would make every top-up-guard test in
    this file that asserts a price-comparison outcome (incremental/
    price_not_below) fail against that (correct, safety-hardened) policy.

    ``selected_base_amount_usd`` mirrors a real artifact's recorded
    first-allocation base; omitting it (the default) simulates a legacy
    artifact that predates the field, which now routes the top-up guard to
    missing_prior_base/manual review (Codex review: "Fail closed when the
    first allocation base is unknown") rather than silently recomputing from
    this run's own price.
    """
    run_dir = root / "20260708_0900ET"
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate = {
        "ticker": ticker,
        "status": "filled",
        "planned_amount_usd": amount_usd,
        "decision_price": decision_price,
    }
    if selected_base_amount_usd is not None:
        candidate["selected_base_amount_usd"] = selected_base_amount_usd
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",
            "generated_at": "2026-07-08T09:00:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [candidate],
        }),
        encoding="utf-8",
    )


def test_morning_evaluate_top_up_reuses_first_allocation_base():
    # Codex review ("Preserve the first allocation's base for same-day top-ups"),
    # exercised through morning_dca's own _evaluate_top_up (the same class of
    # same-day top-up guard as near_close_dca.evaluate_top_up_guard). An earlier
    # same-day run bought using the $1 base for $2 total (1.0 * 2x); this run's OWN
    # price would freshly select the $2 base, but the guard must keep the $1 base
    # with this run's own 4x multiplier rather than upgrade the whole day's target.
    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=2.0,
        reference_price=98.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=94.0,
        previous_close=100.0,
        drop_pct=6.0,
        multiplier=4.0,
        hypothetical_amount_usd=8.0,  # recomputed (incorrectly) at this run's own $2 base: 2.0 * 4.0
        selected_base_amount_usd=2.0,
    )
    outcome = ex._evaluate_top_up(signal, prior)
    assert outcome.desired_amount == 4.0  # 1.0 (preserved base) * 4x, not 2.0 * 4x
    assert outcome.incremental_order_amount == 2.0  # 4.0 - 2.0, not 8.0 - 2.0 == 6.0
    # Codex review ("Persist the preserved base on top-up candidates"): the
    # guard must expose the PRESERVED $1 base it actually used, not this
    # signal's own freshly recomputed $2 base.
    assert outcome.effective_base_amount_usd == 1.0

    row = signal.to_row()
    ex._augment_buy_candidate_row(row, signal, {"AAPL": outcome})
    assert row["selected_base_amount_usd"] == 1.0


def test_morning_top_up_guard_buys_incremental_when_prior_fill_exists(tmp_path):
    # Prior manual fill of $4.50 at $99.0 before the 10:30 AM live run. Stock
    # has dropped 3% to $97.0 (below the $99.0 reference), so the guard allows
    # an incremental top-up of $3.00 (desired $7.50 minus prior $4.50).
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _Broker:
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Guarded top-ups use the narrower same-stream ref-id duplicate check;
            # this fixture never has prior broker orders on file, so it always
            # proceeds as a fresh placement.
            return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    prior_root = tmp_path / "prior"
    _write_prior_run_state(
        prior_root, "TOPUP", amount_usd=4.50, decision_price=99.0, selected_base_amount_usd=3.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    # 3% drop: current $97.0 < prior ref $99.0 → incremental eligible
    # desired = 2.5x * $3.00 = $7.50; incremental = $7.50 - $4.50 = $3.00
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    candidates = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert "TOPUP" in candidates, "TOPUP should appear as a buy candidate"
    row = candidates["TOPUP"]
    assert row.get("top_up_guard_result") == TOP_UP_INCREMENTAL
    assert row.get("incremental_order_amount") == 3.0
    assert broker.placed.get("TOPUP") == 3.0, "broker should place $3.00 incremental order"


def test_morning_top_up_guard_skips_when_price_not_below_reference(tmp_path):
    # Prior manual fill at $96.0 reference. Morning run current price $97.0 is
    # ABOVE $96.0 → TOP_UP_PRICE_NOT_BELOW → $0 order, broker not called.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_PRICE_NOT_BELOW

    class _Broker:
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    prior_root = tmp_path / "prior"
    _write_prior_run_state(
        prior_root, "TOPUP", amount_usd=4.50, decision_price=96.0, selected_base_amount_usd=2.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    # 3% drop: current $97.0 > prior ref $96.0 → not below → skipped
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    candidates = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    row = candidates.get("TOPUP", {})
    assert row.get("top_up_guard_result") == TOP_UP_PRICE_NOT_BELOW
    assert row.get("incremental_order_amount") == 0.0
    assert "TOPUP" not in broker.placed, "broker should not be called when top-up is skipped"
    assert "TOPUP" in res["artifacts"].get("top_up_offset_tickers", [])


def test_morning_top_up_guard_inactive_in_watchlist_mode(tmp_path):
    # Watchlist mode is analytics-only: prior fills must never reduce hypothetical
    # amounts even when prior_allocation_roots is configured.
    prior_root = tmp_path / "prior"
    _write_prior_run_state(prior_root, "TOPUP", amount_usd=4.50, decision_price=99.0)
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "watchlist"),
        prior_allocation_roots=(str(prior_root),),
    )
    # Default mode is watchlist — no preflight root needed, no broker.
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now(), data)
    candidates = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert "TOPUP" in candidates
    row = candidates["TOPUP"]
    assert "top_up_guard_result" not in row, "top-up guard must not run in watchlist mode"
    # Full hypothetical amount, not reduced by any prior fill
    assert row.get("hypothetical_amount_usd") == 7.5


def test_morning_top_up_guard_seeds_ledger_from_prior_fills(tmp_path):
    # Prior MORNING fill of $85 on a DIFFERENT ticker (PRIOR_TICKER) before the
    # 10:30 live run. Both prior_allocation_roots and cap_seed_roots point to the
    # same morning root. The $90 daily cap has only $5 remaining. TOPUP graded at
    # 2.5x * $3 = $7.50 desired, which exceeds the $5 remaining → cap_exceeded.
    # This verifies that prior fills on other tickers reduce the ledger cap, not
    # just the per-ticker incremental offset.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker:
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    morning_root = tmp_path / "morning_prior"
    # $85 prior morning fill on a different ticker at a reference price well below
    # current so per-ticker top-up would be eligible, but the cap is the binding constraint.
    _write_prior_run_state(morning_root, "PRIOR_TICKER", amount_usd=85.0, decision_price=50.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(morning_root),),
        cap_seed_roots=(str(morning_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    # 3% drop: TOPUP desired = 2.5x * $3 = $7.50; remaining cap = $90 - $85 = $5
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "cap_exceeded", (
        "TOPUP ($7.50 desired) should be cap-gated when prior morning fill consumed $85 of the $90 daily cap"
    )
    assert "TOPUP" not in broker.placed, "broker should not be called when cap is exhausted"


def test_morning_top_up_guard_afternoon_fills_do_not_reduce_morning_cap(tmp_path):
    # Afternoon recovery fill of $85 on AFTERNOON_TICKER recorded in a ce16ba6b-style
    # root. That root is in prior_allocation_roots (for per-ticker offset guard) but NOT
    # in cap_seed_roots (morning-only). The $90 morning daily cap must be unaffected;
    # TOPUP at $7.50 is below the $90 cap and should be placed.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker:
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    afternoon_root = tmp_path / "afternoon_prior"
    _write_prior_run_state(afternoon_root, "AFTERNOON_TICKER", amount_usd=85.0, decision_price=50.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        # Afternoon root in prior_allocation_roots for per-ticker offset check…
        prior_allocation_roots=(str(afternoon_root),),
        # …but NOT in cap_seed_roots, so it never reduces the morning cap.
        cap_seed_roots=None,
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    # 3% drop: TOPUP desired = 2.5x * $3 = $7.50; full $90 cap available
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "submitted", (
        "TOPUP should be placed: afternoon fill in prior_allocation_roots must not exhaust morning cap"
    )
    assert broker.placed.get("TOPUP") == 7.5, "full $7.50 graded order should be placed"


def test_morning_top_up_guard_inactive_in_manual_live_run(tmp_path):
    # Manual live runs must bypass the top-up guard so the normal
    # same_day_strategy_order_exists duplicate-order safeguard remains the sole
    # gate, rather than the incremental path submitting a second order.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker:
        def __init__(self):
            self.placed = {}
            self.dup_checked = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day):
            self.dup_checked[ticker] = True
            return True  # simulate: order already placed → should block
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    prior_root = tmp_path / "prior"
    _write_prior_run_state(prior_root, "TOPUP", amount_usd=4.50, decision_price=99.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        cap_seed_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    # manual=True: top-up guard must be inactive; broker duplicate check fires instead
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    assert "TOPUP" not in broker.placed, (
        "manual live run must not use incremental top-up path; broker dup-check should block the order"
    )
    assert broker.dup_checked.get("TOPUP"), "same_day_strategy_order_exists must be called in manual runs"
    candidates = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert "top_up_guard_result" not in candidates.get("TOPUP", {}), (
        "top_up_guard_result must not appear in manual live run candidates"
    )
    # Cap seeding must still run for manual live runs so prior fills reduce the cap.
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 4.5, (
        "cap ledger must be seeded from prior fills even in manual live runs"
    )


def test_morning_cap_seeding_is_idempotent_across_multiple_runs(tmp_path):
    # Running the same executor instance twice for the same trading day must not
    # double-count the prior fill in the ledger cap. The incremental-amount logic
    # must also pick up a NEW fill that appears between the two runs.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker:
        def __init__(self):
            self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    morning_root = tmp_path / "morning_prior"
    # $85 prior fill leaves $5 of the $90 cap. TOPUP graded at $7.50 exceeds $5.
    _write_prior_run_state(morning_root, "PRIOR_TICKER", amount_usd=85.0, decision_price=50.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(morning_root),),
        cap_seed_roots=(str(morning_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)

    # First run: $85 seeded → $5 remaining → TOPUP cap_exceeded
    broker1 = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res1 = ex.run(_now_live(), data, broker=broker1)
    assert res1["artifacts"].get("broker_outcomes", {}).get("TOPUP", {}).get("status") == "cap_exceeded"
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 85.0, "ledger should show $85 after first run"

    # Second run on same instance with no new fills: ledger stays at $85 (no double-count)
    broker2 = _Broker()
    ex.run(_now_live(), data, broker=broker2)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 85.0, (
        "ledger must not double-count prior fills across repeated runs on the same executor instance"
    )

    # Write a new fill between runs ($4 more → total $89) — the incremental logic
    # must pick it up so the ledger reflects the real prior spend.
    run_dir2 = morning_root / "20260708_0901ET"
    run_dir2.mkdir(parents=True, exist_ok=True)
    import json as _json
    (run_dir2 / "run_state.json").write_text(
        _json.dumps({
            "trading_day": "2026-07-08",
            "generated_at": "2026-07-08T09:01:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [{"ticker": "OTHER", "status": "submitted",
                                "planned_amount_usd": 4.0, "decision_price": 55.0}],
        }),
        encoding="utf-8",
    )
    broker3 = _Broker()
    ex.run(_now_live(), data, broker=broker3)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 89.0, (
        "ledger must pick up new fills written between runs ($85 + $4 = $89)"
    )


def test_morning_cap_seeding_does_not_double_count_own_manual_submission(tmp_path):
    # Codex review on 4975a4f: when the same MorningDCAExecutor instance first
    # runs a manual live run (which records its buy amount into self.ledger and
    # writes an artifact under cap_seed_roots/manual_runs/) and then runs the
    # scheduled live evaluation on the same day, the cap-seeding scan must NOT
    # re-count the amount already recorded by the manual run's record_buy_candidates.
    #
    # The failure scenario:
    #   1. manual run buys $7.50 → record_buy_candidates records $7.50 in ledger
    #      and writes run_state.json under manual_runs/
    #   2. scheduled run: cap_seed scan reads manual_runs/ → sees $7.50; since
    #      _cap_seeded_amounts has no watermark yet, seeds another $7.50 into ledger
    #   3. ledger = $15, cap_gate fires for the $20 daily cap, blocking orders
    #      even though only $7.50 of the cap has been genuinely spent.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _DupBlockingBroker:
        """First call places; subsequent calls for same ticker return dup=True."""
        def __init__(self):
            self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return ticker in self.placed  # block the ticker if already placed
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    # The cap_seed_roots and morning_run_state_root share the same tmp dir so the
    # manual run's artifact ends up inside cap_seed_roots (manual_runs/) as in prod.
    live_root = tmp_path / "live"
    # Use a tight cap ($20) so the double-count ($15) would block the $7.50 scheduled
    # order even though genuine spend is only $7.50 — leaving $12.50 available.
    pf_root = _seed_preflight(tmp_path / "pf", ("TICK",))
    cfg = MorningConfig(
        tickers=("TICK",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(live_root / "manual_runs"),),
        cap_seed_roots=(str(live_root / "manual_runs"),),
        morning_daily_cap_usd=20.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # 3% drop → 2.5x → $7.50 per run
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.03})
    shared_broker = _DupBlockingBroker()

    # Step 1: manual run — buys TICK ($7.50), writes artifact to manual_runs/
    ex.run(_now_live(), data, broker=shared_broker, manual=True)
    assert "TICK" in shared_broker.placed, "manual run should have placed the order"
    manual_amount = shared_broker.placed["TICK"]
    assert ex.ledger.day_planned(date(2026, 7, 8)) == manual_amount, (
        "ledger should equal the manual buy amount after the manual run"
    )

    # Step 2: scheduled run — TICK already in shared_broker.placed → dup-blocked.
    # The cap seeding scan will find manual_runs/ artifact with $7.50.
    # Without the fix: prev_total=0, incremental=7.50 → ledger becomes $15, cap blocks.
    # With the fix: _own_recorded[day]=7.50, prev_total=7.50, incremental=0 → ledger stays at $7.50.
    ex.run(_now_live(), data, broker=shared_broker)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == manual_amount, (
        "cap-seeding must not re-count the manual run's own submission; "
        f"expected {manual_amount}, got {ex.ledger.day_planned(date(2026, 7, 8))}"
    )


def test_morning_cap_seeding_watermark_exclusive_of_own_recorded(tmp_path):
    # Codex review on 955cbd5: if _cap_seeded_amounts stored new_total (instead of
    # new_total - own_amount), a scheduled-run record_buy_candidates call would grow
    # _own_recorded beyond the filesystem scan's new_total, making prev_total > new_total
    # on the next scan and suppressing pickup of newly-written external fills.
    #
    # Scenario:
    #   1. Manual run buys $7.50 → _own_recorded[day]=7.50, artifact in manual_runs/
    #   2. External $4 fill appears in manual_runs/
    #   3. Scheduled run 1: seeds $4 (incremental), writes $6 to ledger via record_buy_candidates
    #      (but NOT to _own_recorded since manual=False)
    #   4. Another external $4 fill appears in manual_runs/
    #   5. Scheduled run 2: should seed another $4 — without the watermark fix it would
    #      compute prev_total = old_watermark + _own_recorded and get a negative incremental.
    import json as _json

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _SimpleBroker:
        def __init__(self):
            self.placed = {}
            self._placed_tickers = set()
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return ticker in self._placed_tickers
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            self._placed_tickers.add(ticker)
            return f"ORDER-{ticker}"

    live_root = tmp_path / "live"
    manual_root = live_root / "manual_runs"
    pf_root = _seed_preflight(tmp_path / "pf", ("TICK",))
    cfg = MorningConfig(
        tickers=("TICK",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(manual_root),),
        cap_seed_roots=(str(manual_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.03})

    # Step 1: manual run at 9:00 AM — buys TICK ($7.50), writes artifact to manual_runs/.
    # Use an earlier timestamp so the artifact's generated_at is before the 10:30 AM
    # boundary used by the scheduled run's cap-seeding scan (boundary check is strict <).
    _now_manual = datetime.combine(date(2026, 7, 8), time(9, 0), tzinfo=EASTERN)
    broker = _SimpleBroker()
    ex.run(_now_manual, data, broker=broker, manual=True)
    manual_amount = broker.placed.get("TICK", 0.0)
    assert manual_amount == 7.5

    # Step 2: an external fill of $4 appears in manual_runs/ (simulates another executor/session)
    ext_dir1 = manual_root / "20260708_0901ET"
    ext_dir1.mkdir(parents=True, exist_ok=True)
    (ext_dir1 / "run_state.json").write_text(
        _json.dumps({
            "trading_day": "2026-07-08", "generated_at": "2026-07-08T09:01:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [{"ticker": "EXT1", "status": "submitted",
                                "planned_amount_usd": 4.0, "decision_price": 55.0}],
        }),
        encoding="utf-8",
    )

    # Step 3: scheduled run — TICK already placed (dup-blocked); no new buy but cap seeding
    # should pick up the external $4 fill.
    ex.run(_now_live(), data, broker=broker)
    ledger_after_sched1 = ex.ledger.day_planned(date(2026, 7, 8))
    assert ledger_after_sched1 == manual_amount + 4.0, (
        f"ledger should be manual($7.50) + external-seed($4) = $11.50, got {ledger_after_sched1}"
    )

    # Step 4: another external $4 fill appears in manual_runs/
    ext_dir2 = manual_root / "20260708_0902ET"
    ext_dir2.mkdir(parents=True, exist_ok=True)
    (ext_dir2 / "run_state.json").write_text(
        _json.dumps({
            "trading_day": "2026-07-08", "generated_at": "2026-07-08T09:02:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [{"ticker": "EXT2", "status": "submitted",
                                "planned_amount_usd": 4.0, "decision_price": 55.0}],
        }),
        encoding="utf-8",
    )

    # Step 5: another scheduled run — must pick up the second external $4 fill.
    # Without the watermark fix: _cap_seeded_amounts stored new_total=$11.50, so
    # prev_total = 11.50 + _own_recorded(7.50) = 19.0 > new_total($15.50) → no increment.
    # With the fix: _cap_seeded_amounts stored new_total - own = $4.0, so
    # prev_total = 4.0 + 7.50 = 11.50, incremental = 15.50 - 11.50 = 4.0 → seeds $4.
    ex.run(_now_live(), data, broker=broker)
    ledger_after_sched2 = ex.ledger.day_planned(date(2026, 7, 8))
    assert ledger_after_sched2 == manual_amount + 8.0, (
        f"ledger should be manual($7.50) + ext1-seed($4) + ext2-seed($4) = $15.50, "
        f"got {ledger_after_sched2}"
    )


def test_morning_cap_seeding_does_not_double_count_own_scheduled_submission(tmp_path):
    # Codex review (PR #88): "Avoid double-counting this executor's scheduled
    # fills". cap_seed_roots was widened in an earlier PR #88 fix to cover the
    # full state_root (not just manual_runs/), so it now also scans the
    # SCHEDULED dated-slot artifact. But record_buy_candidates only tracked
    # _track_own for MANUAL submissions, so a scheduled run's own submission
    # was never credited back out of a later cap-seeding scan of that same
    # dated-slot file -- e.g. a scheduled retry (same executor instance,
    # same slot) would re-count a prior $7.50 scheduled order as $15.
    #
    # cap_seed_roots == morning_run_state_root here, matching the real
    # production default (config/morning_dca.json: cap_seed_roots points at
    # the same live_run_state_root), so the dated-slot artifact is in scan
    # scope -- unlike the narrower manual_runs/-only roots used above.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _DupBlockingBroker:
        """First call places; subsequent calls for same ticker return dup=True."""
        def __init__(self):
            self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return ticker in self.placed
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    live_root = tmp_path / "live"
    pf_root = _seed_preflight(tmp_path / "pf", ("TICK",))
    cfg = MorningConfig(
        tickers=("TICK",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(live_root / "manual_runs"),),
        cap_seed_roots=(str(live_root),),
        morning_daily_cap_usd=20.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # 3% drop -> 2.5x -> $7.50 per run
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.03})
    broker = _DupBlockingBroker()

    # First scheduled attempt: submits TICK, writes the dated-slot artifact.
    ex.run(_now_live(), data, broker=broker)
    assert "TICK" in broker.placed, "first scheduled attempt should have placed the order"
    first_amount = broker.placed["TICK"]
    assert ex.ledger.day_planned(date(2026, 7, 8)) == first_amount

    # Scheduled RETRY a minute later, same slot/directory (run_state_dir keys
    # only on the date + folder_suffix, not the exact minute), same executor
    # instance. Must be strictly later than the first attempt's generated_at:
    # the boundary check in load_prior_same_day_allocations_multi is strict
    # "<", so a retry using the EXACT same timestamp would never see its own
    # just-written artifact and wouldn't exercise this bug at all -- a real
    # retry always happens some seconds/minutes after the original attempt.
    retry_now = _now_live() + timedelta(minutes=1)
    ex.run(retry_now, data, broker=broker)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == first_amount, (
        "cap-seeding must not re-count this executor's own scheduled submission; "
        f"expected {first_amount}, got {ex.ledger.day_planned(date(2026, 7, 8))}"
    )


def test_morning_cap_seeding_picks_up_external_fill_after_own_retry_overwrite(tmp_path):
    # Codex review (PR #88): "Do not subtract overwritten scheduled
    # submissions from cap seeding". The fix above (crediting
    # _own_recorded_scheduled back out of the cap-seeding scan) has to
    # account for the ordinary duplicate-check retry path REWRITING the
    # ticker's own dated-slot row from "submitted" to "skipped" (only the
    # top-up-guard duplicate path preserves "submitted" -- see
    # _place_broker_orders). If _own_recorded_scheduled kept accumulating
    # (+=) across calls instead of being replaced by each call's own batch,
    # a later external/manual fill under the same cap_seed_roots would be
    # silently swallowed: the scan sees only the external amount (the
    # ticker's own row no longer counts, since it's "skipped" on disk) but
    # a stale, too-large own_amount is still subtracted, driving the
    # incremental negative and seeding nothing.
    import json as _json

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _DupBlockingBroker:
        def __init__(self):
            self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return ticker in self.placed
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    live_root = tmp_path / "live"
    pf_root = _seed_preflight(tmp_path / "pf", ("TICK",))
    cfg = MorningConfig(
        tickers=("TICK",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(live_root / "manual_runs"),),
        cap_seed_roots=(str(live_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.03})
    broker = _DupBlockingBroker()

    # First scheduled attempt: submits TICK ($7.50).
    ex.run(_now_live(), data, broker=broker)
    first_amount = broker.placed["TICK"]
    assert ex.ledger.day_planned(date(2026, 7, 8)) == first_amount

    # Scheduled retry: TICK is dup-blocked via the ordinary same-day check
    # (no cross-stream prior allocation for TICK), so its row is rewritten
    # to status="skipped" in the dated-slot artifact this retry overwrites.
    retry_now = _now_live() + timedelta(minutes=1)
    ex.run(retry_now, data, broker=broker)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == first_amount

    # An external/manual $4 fill appears under cap_seed_roots afterward
    # (e.g. a manual override for a different ticker).
    ext_dir = live_root / "manual_runs" / "20260708_1032ET"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "run_state.json").write_text(
        _json.dumps({
            "trading_day": "2026-07-08", "generated_at": "2026-07-08T10:32:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [{"ticker": "EXT", "status": "submitted",
                                "planned_amount_usd": 4.0, "decision_price": 55.0}],
        }),
        encoding="utf-8",
    )

    # A third scheduled call must pick up the external $4 fill -- the total
    # ledger should be the genuine $7.50 + $4.00 = $11.50, not stuck at $7.50.
    third_now = _now_live() + timedelta(minutes=3)
    ex.run(third_now, data, broker=broker)
    expected = first_amount + 4.0
    assert ex.ledger.day_planned(date(2026, 7, 8)) == expected, (
        f"cap-seeding must still pick up the external $4 fill after its own "
        f"submission was overwritten to skipped; expected {expected}, "
        f"got {ex.ledger.day_planned(date(2026, 7, 8))}"
    )


def test_morning_ledger_seed_still_covers_scheduled_slot_when_cap_roots_are_manual_only(tmp_path):
    # Codex review ("Seed today's scheduled slot when cap roots do not cover
    # it"): cap_seed_roots may be configured narrowly to cover only
    # manual_runs/ -- a case _compute_top_up's own_manual_in_scan/
    # own_scheduled_in_scan split explicitly supports. _seed_ledger_from_prior_runs
    # must not blanket-skip today's scheduled-slot seeding just because
    # cap_seed_roots is truthy; it must check whether the scheduled slot is
    # actually CONTAINED by a configured cap root. Otherwise, with a narrow
    # cap_seed_roots, NEITHER mechanism seeds the first scheduled submission
    # and a fresh retry (a new process/executor, as a real cron retry would
    # be) can spend the full daily cap a second time.
    pf_root = _seed_preflight(tmp_path / "pf", ("AAA", "BBB"))
    live_root = tmp_path / "live"

    cfg1 = MorningConfig(
        tickers=("AAA",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(live_root / "manual_runs"),),  # narrow: excludes the scheduled slot
        morning_daily_cap_usd=10.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex1 = MorningDCAExecutor(cfg1, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="562057042", reference_prices={"AAA": 97.0})
    res1 = ex1.run(_now_live(), data1, broker=broker1)
    assert res1["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"

    # Fresh executor instance = fresh in-memory MorningLedger, mirroring a
    # brand-new cron process retrying the same scheduled slot.
    cfg2 = MorningConfig(
        tickers=("BBB",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(live_root / "manual_runs"),),
        morning_daily_cap_usd=10.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex2 = MorningDCAExecutor(cfg2, mode=MORNING_LIVE_MODE)
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="562057042", reference_prices={"BBB": 97.0})
    res2 = ex2.run(_now_live() + timedelta(minutes=1), data2, broker=broker2)

    assert len(broker2.placed_orders) == 0, (
        "a fresh retry must still see AAA's earlier scheduled spend against the "
        "daily cap even though cap_seed_roots doesn't cover the scheduled slot"
    )
    # index by ticker, not position: AAA's earlier submitted row is carried
    # forward into ex2's own artifact (a separate, already-covered fix) and
    # prepended ahead of BBB's own row.
    bbb = next(row for row in res2["artifacts"]["buy_candidates"] if row["ticker"] == "BBB")
    assert bbb["status"] == "cap_exceeded"


def test_morning_top_up_guard_no_prior_run_when_default_roots_empty(tmp_path):
    # Default MorningConfig has prior_allocation_roots=None, which means the guard
    # uses the default afternoon root (not disabled). With an empty default root the
    # guard finds no prior fill → no_prior_run → full graded amount placed.
    # (Use prior_allocation_roots=() to fully disable the guard instead.)
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_NO_PRIOR_RUN

    class _Broker:
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        # prior_allocation_roots=None → uses default root; empty default → no_prior_run
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    # 3% drop → desired 2.5x * $3.00 = $7.50; no prior fill → full amount placed
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    candidates = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert "TOPUP" in candidates
    row = candidates["TOPUP"]
    assert row.get("top_up_guard_result") == TOP_UP_NO_PRIOR_RUN
    assert broker.placed.get("TOPUP") == 7.5, "full graded amount placed when no prior fill exists"


# ---------------------------------------------------------------------------
# Top-up guard regression tests (scheduled-run path)
# ---------------------------------------------------------------------------

def _seed_prior(
    root, ticker, amount_usd, *,
    status="filled", reference_price=95.0, trading_date=None, selected_base_amount_usd=None,
):
    """Write a prior-fill run_state.json for one ticker under root.

    ``selected_base_amount_usd`` mirrors a real artifact's recorded
    first-allocation base; omitting it (the default) simulates a legacy
    artifact that predates the field, which now routes the top-up guard to
    missing_prior_base/manual review (Codex review: "Fail closed when the
    first allocation base is unknown") rather than silently recomputing from
    this run's own price.
    """
    if trading_date is None:
        trading_date = _now_live().date()
    folder = trading_date.strftime("%Y%m%d") + "_0900ET"
    run_dir = Path(root) / folder
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate: dict = {"ticker": ticker, "status": status, "planned_amount_usd": amount_usd}
    if status == "filled" and reference_price is not None:
        candidate["target_price"] = reference_price
    if selected_base_amount_usd is not None:
        candidate["selected_base_amount_usd"] = selected_base_amount_usd
    state = {
        "trading_day": trading_date.isoformat(),
        "generated_at": f"{trading_date.isoformat()}T09:00:00-04:00",
        "run_classification": {"run_id": "test_prior", "run_origin": "manual", "stream": "morning"},
        "buy_candidates": [candidate],
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    return root


class _TopUpBroker:
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER as _acct
    def __init__(self):
        self.placed = {}
    def account_number(self): return self._acct
    def available_buying_power(self): return 1000.0
    def same_day_strategy_order_exists(self, ticker, trading_day): return False
    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        # Guarded top-ups use the narrower same-stream ref-id duplicate check;
        # this fixture never has prior broker orders on file, so it always
        # proceeds as a fresh placement.
        return False
    def is_tradable_for_fractional_buy(self, ticker): return True
    def review_market_buy(self, ticker, notional_usd): return True, "ok"
    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.placed[ticker] = notional_usd
        return f"ORDER-{ticker}"


def test_morning_live_applies_prior_manual_offset_incremental(tmp_path):
    # Prior manual fill: NVDA $3 at $95 reference. Morning price $90 (10% drop, below ref).
    # Desired = 5x * $2 = $10. Incremental = $15 - $3 = $8.
    prior_root = _seed_prior(
        tmp_path / "prior", "NVDA", 2.0, reference_price=95.0, selected_base_amount_usd=2.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" in broker.placed, f"expected incremental order; broker_outcomes={res['artifacts'].get('broker_outcomes')}"
    assert abs(broker.placed["NVDA"] - 8.0) < 0.01, f"expected $8 incremental; got {broker.placed['NVDA']}"
    rows = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert rows["NVDA"]["top_up_guard_result"] == "incremental_top_up"


class _RefIdTrackingBroker:
    """Broker double that actually remembers placed ref_ids, so the same-stream
    duplicate check (same_day_strategy_order_exists_with_ref_id_prefix) reflects
    real prior placements instead of a fixture that always returns False.
    """

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER as _acct

    def __init__(self):
        self.placed = {}  # ticker -> notional (last placement)
        self._ref_ids = []  # list of (ticker, ref_id) placed so far

    def account_number(self):
        return self._acct

    def available_buying_power(self):
        return 1000.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        return any(t == ticker for t, _ in self._ref_ids)

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        return any(
            t == ticker and any(rid.startswith(p) for p in ref_id_prefixes)
            for t, rid in self._ref_ids
        )

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.placed[ticker] = notional_usd
        self._ref_ids.append((ticker, ref_id))
        return f"ORDER-{ticker}-{len(self._ref_ids)}"


def test_morning_scheduled_top_up_not_blocked_by_manual_fills_ordinary_ref_id(tmp_path):
    # Codex review on PR #42: a manual morning fill (manual + MORNING_STREAM)
    # disables per-ticker top-up computation, so it places with the ORDINARY
    # ref_id format (no "-topup-" segment) -- the same format a plain,
    # no-prior-allocation scheduled order would use. When the scheduled run
    # later finds that manual fill as a prior allocation and computes a
    # guarded incremental top-up, _same_stream_order_exists checks both the
    # topup ref prefix AND the ordinary ref prefix -- so it must not mistake
    # the manual fill's ordinary ref_id for an earlier attempt of THIS
    # scheduled top-up, or the real incremental order never gets placed while
    # run_state still (falsely) records it as submitted.
    live_root = tmp_path / "live"
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(live_root / "manual_runs"),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _RefIdTrackingBroker()

    # Step 1: manual run, 3% drop -> 2.5x * $3 = $7.50 placed with the
    # ORDINARY ref_id (top-up computation is disabled for manual+MORNING).
    manual_now = datetime.combine(date(2026, 7, 8), time(9, 0), tzinfo=EASTERN)
    manual_data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.03})
    manual_res = ex.run(manual_now, manual_data, broker=broker, manual=True)
    assert broker.placed.get("NVDA") == pytest.approx(7.5)
    manual_ref_ids = list(broker._ref_ids)
    assert manual_ref_ids and "topup" not in manual_ref_ids[0][1]

    # Simulate a later reporting pass confirming the fill (real production
    # flow: orders.csv/broker refresh marks it "filled"), so the scheduled
    # run's top-up guard has a known reference price to top up against
    # instead of routing to missing_prior_price/manual review.
    manual_state_path = Path(manual_res["paths"]["run_state"])
    manual_state = json.loads(manual_state_path.read_text(encoding="utf-8"))
    for row in manual_state["buy_candidates"]:
        if row["ticker"] == "NVDA":
            row["status"] = "filled"
    manual_state_path.write_text(json.dumps(manual_state), encoding="utf-8")

    # Step 2: scheduled run, deeper 10% drop -> 5x * $3 = $15 desired. The
    # manual fill above is now visible as a prior allocation ($7.50 at the
    # manual run's decision price), so this should compute an incremental
    # top-up and actually place it, not skip it as a phantom duplicate.
    scheduled_data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), scheduled_data, broker=broker)
    outcomes = res["artifacts"].get("broker_outcomes", {})
    nvda_outcome = outcomes.get("NVDA", {})
    assert nvda_outcome.get("status") == "submitted", nvda_outcome
    assert nvda_outcome.get("order_id") is not None, (
        f"top-up recorded as submitted without ever calling place_market_buy: {nvda_outcome}"
    )
    # The broker must show a SECOND real placement for NVDA (the incremental
    # top-up), not just the original manual fill.
    assert len(broker._ref_ids) == 2, f"expected 2 real placements, got {broker._ref_ids}"


def test_morning_scheduled_top_up_duplicate_preserves_original_submitted_amount(tmp_path):
    # Codex review (PR #88): "Preserve original scheduled notional across
    # top-up retries". A same-stream top-up DUPLICATE retry (no new order
    # placed -- _same_stream_order_exists finds the earlier attempt's ref_id)
    # used to overwrite the dated-slot artifact's submitted_amount_usd with a
    # FRESHLY recomputed top-up incremental, which can be smaller than what
    # was genuinely submitted if new prior-allocation context (e.g. another
    # external fill) appeared between the two attempts. That understates the
    # true submitted total on disk, and a later cap-seeding scan of this
    # artifact would under-count real spend.
    #
    # Scenario: no prior allocation at first -> full $15 (5x, 10% drop)
    # placed with the ORDINARY ref_id. Then a $3 external fill for the SAME
    # ticker appears. A retry now sees has_prior_allocation=True and
    # incremental = $15 - $3 = $12 -- smaller than the $15 actually placed.
    # The retry must still record $15 as submitted_amount_usd, not $12.
    live_root = tmp_path / "live"
    prior_root = tmp_path / "prior"
    prior_root.mkdir(parents=True, exist_ok=True)
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        base_buy_amount_usd=3.0,
        max_per_order_usd=15.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _RefIdTrackingBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    # First scheduled attempt: no prior allocation yet -> full $15 placed
    # with the ordinary (non-topup) ref_id.
    first = ex.run(_now_live(), data, broker=broker)
    assert broker.placed.get("NVDA") == pytest.approx(15.0)
    first_rows = {r["ticker"]: r for r in first["artifacts"].get("buy_candidates", [])}
    assert first_rows["NVDA"]["top_up_guard_result"] == "no_prior_run"

    # A $3 external fill for NVDA appears afterward (new prior-allocation context).
    _seed_prior(prior_root, "NVDA", 3.0, reference_price=95.0, selected_base_amount_usd=2.0)

    # Scheduled retry: has_prior_allocation is now True and the freshly
    # recomputed incremental ($15 - $3 = $12) is SMALLER than what was
    # actually submitted ($15). _same_stream_order_exists finds NVDA's
    # earlier ordinary-ref_id placement -> same-stream duplicate, no new
    # order placed.
    retry_now = _now_live() + timedelta(minutes=1)
    retry = ex.run(retry_now, data, broker=broker)
    assert len(broker._ref_ids) == 1, (
        "a genuine duplicate must not place a second real order"
    )
    outcomes = retry["artifacts"].get("broker_outcomes", {})
    nvda_outcome = outcomes.get("NVDA", {})
    assert nvda_outcome.get("status") == "submitted", nvda_outcome
    assert nvda_outcome.get("submitted_amount_usd") == pytest.approx(15.0), (
        f"duplicate retry must preserve the ORIGINAL $15 submitted amount, "
        f"not the freshly recomputed $12 incremental; got {nvda_outcome}"
    )
    # The buy_candidates row copies submitted_amount_usd into
    # planned_amount_usd (see run()'s broker-outcome sync step), not a
    # separate submitted_amount_usd key on the row itself.
    on_disk_rows = {
        r["ticker"]: r for r in retry["artifacts"].get("buy_candidates", [])
    }
    assert on_disk_rows["NVDA"].get("planned_amount_usd") == pytest.approx(15.0)


def test_morning_scheduled_duplicate_check_preserved_when_cap_fully_consumed(tmp_path):
    # Codex review (PR #88): "Preserve submitted evidence when a cap-seeded
    # retry is full". The daily/weekly cap check used to run BEFORE the
    # same-stream duplicate check. Once cap-seeding recovers an earlier
    # scheduled order that exactly consumes the remaining cap, a retry for
    # that same ticker would hit the cap check first (submitted_today=0 this
    # call + a freshly recomputed notional > remaining_daily=0, since the
    # ledger already has the first attempt's amount booked), overwriting the
    # dated-slot artifact's "submitted" row with "cap_exceeded" -- erasing
    # evidence a fresh executor's cap-seeding scan depends on.
    prior_root = _seed_prior(
        tmp_path / "prior", "NVDA", 3.0, reference_price=95.0, selected_base_amount_usd=3.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        base_buy_amount_usd=3.0,
        max_per_order_usd=15.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        # 10% drop -> 5x * $3 = $15 desired; incremental = $15 - $3 = $12.
        # Cap set to exactly $12 so the first attempt consumes it fully.
        morning_daily_cap_usd=12.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _RefIdTrackingBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    first = ex.run(_now_live(), data, broker=broker)
    assert broker.placed.get("NVDA") == pytest.approx(12.0)
    first_outcome = first["artifacts"].get("broker_outcomes", {}).get("NVDA", {})
    assert first_outcome.get("status") == "submitted", first_outcome

    # Retry: nothing changed (no new external fill), so the recomputed
    # incremental is still $12, but the ledger already has $12 booked from
    # the first attempt against a $12 cap -- remaining_daily is now $0.
    retry_now = _now_live() + timedelta(minutes=1)
    retry = ex.run(retry_now, data, broker=broker)
    assert len(broker._ref_ids) == 1, "a genuine duplicate must not place a second real order"
    retry_outcome = retry["artifacts"].get("broker_outcomes", {}).get("NVDA", {})
    assert retry_outcome.get("status") == "submitted", (
        f"the duplicate check must run before the now-exhausted cap check, "
        f"preserving submitted evidence instead of cap_exceeded; got {retry_outcome}"
    )
    assert retry_outcome.get("submitted_amount_usd") == pytest.approx(12.0)


def test_morning_scheduled_duplicate_not_rebooked_into_ledger(tmp_path):
    # Codex review (PR #88): "Avoid booking same-stream top-up retries a
    # second time". A same-stream top-up duplicate is preserved as
    # status="submitted" in the artifact (so the dated-slot file keeps
    # showing real evidence), but its amount is already reflected via the
    # cap-seeding filesystem scan of that same on-disk artifact. If
    # record_buy_candidates() also books it directly into self.ledger every
    # retry, the ledger balance keeps growing without any new money ever
    # being submitted.
    prior_root = _seed_prior(
        tmp_path / "prior", "NVDA", 3.0, reference_price=95.0, selected_base_amount_usd=3.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        base_buy_amount_usd=3.0,
        max_per_order_usd=15.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _RefIdTrackingBroker()
    # 10% drop -> 5x * $3 = $15 desired; incremental = $15 - $3 = $12.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    ex.run(_now_live(), data, broker=broker)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == pytest.approx(12.0)

    # Two retries in a row, nothing new happening: the ledger must stay at
    # $12 -- not grow by another $12 on every retry.
    ex.run(_now_live() + timedelta(minutes=1), data, broker=broker)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == pytest.approx(12.0), (
        "a same-stream duplicate retry must not re-book its amount into the ledger"
    )
    ex.run(_now_live() + timedelta(minutes=2), data, broker=broker)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == pytest.approx(12.0), (
        "a second retry must still not re-book the duplicate amount"
    )
    assert len(broker._ref_ids) == 1, "only the original attempt should have placed a real order"


def test_morning_live_skips_when_price_not_below_prior_reference(tmp_path):
    # Prior: NVDA $2 at $85 reference. Morning price $90 (10% drop from $100).
    # $90 >= $85 → price_not_below → skip, no order.
    prior_root = _seed_prior(
        tmp_path / "prior", "NVDA", 2.0, reference_price=85.0, selected_base_amount_usd=2.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" not in broker.placed, f"expected no order; broker.placed={broker.placed}"
    art = res["artifacts"]
    assert "NVDA" in art.get("top_up_offset_tickers", []), art.get("top_up_offset_tickers")


def test_morning_live_full_buy_when_no_prior_allocation(tmp_path):
    # Empty prior root → no_prior_run → buy the full desired amount ($10 = 5x * $2).
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" in broker.placed, f"expected full buy; broker.placed={broker.placed}"
    assert abs(broker.placed["NVDA"] - 10.0) < 0.01, f"expected $10; got {broker.placed['NVDA']}"
    rows = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert rows["NVDA"]["top_up_guard_result"] == "no_prior_run"


def test_morning_live_missing_price_triggers_manual_review(tmp_path):
    # Prior: NVDA "submitted" with unknown reference price → missing_prior_price → $0 + manual review.
    prior_root = _seed_prior(tmp_path / "prior", "NVDA", 9.0, status="submitted", reference_price=None)
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" not in broker.placed, f"expected no order; broker.placed={broker.placed}"
    art = res["artifacts"]
    assert "NVDA" in art.get("top_up_manual_review_tickers", []), art.get("top_up_manual_review_tickers")
    assert "NVDA" in art.get("manual_review_tickers", []), art.get("manual_review_tickers")
    assert art["counts"].get("manual_review_candidates", 0) >= 1, art["counts"]
    # Row must also appear in the manual_review_candidates array so downstream
    # consumers that read the full list see top-up missing-price cases.
    mr_tickers = [r.get("ticker") for r in art.get("manual_review_candidates", [])]
    assert "NVDA" in mr_tickers, f"manual_review_candidates rows: {mr_tickers}"


def test_morning_live_no_incremental_when_prior_covers_full_desired(tmp_path):
    # Prior: NVDA $20 at $95 reference. Desired = $10. $10 - $20 = -$10 ≤ 0 → no_incremental.
    prior_root = _seed_prior(
        tmp_path / "prior", "NVDA", 20.0, reference_price=95.0, selected_base_amount_usd=2.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" not in broker.placed, f"expected no order; broker.placed={broker.placed}"
    art = res["artifacts"]
    assert "NVDA" in art.get("top_up_offset_tickers", []), art.get("top_up_offset_tickers")
    rows = {r["ticker"]: r for r in art.get("buy_candidates", [])}
    assert rows["NVDA"]["top_up_guard_result"] == "no_incremental_difference"


def test_morning_live_prior_from_configured_root_only(tmp_path):
    # Only roots in prior_allocation_roots contribute; fills under other_root are invisible.
    # configured_root: NVDA $2 at $95 → incremental $8.
    # other_root: NVDA $20 at $95 → would produce no_incremental if scanned.
    configured_root = _seed_prior(
        tmp_path / "configured", "NVDA", 2.0, reference_price=95.0, selected_base_amount_usd=2.0
    )
    _seed_prior(tmp_path / "other", "NVDA", 20.0, reference_price=95.0, selected_base_amount_usd=2.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(configured_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    # Configured root contributes $3 only → incremental $8, not no_incremental.
    assert "NVDA" in broker.placed, f"expected incremental order; broker_outcomes={res['artifacts'].get('broker_outcomes')}"
    assert abs(broker.placed["NVDA"] - 8.0) < 0.01, f"expected $8; got {broker.placed['NVDA']}"


def test_morning_live_default_roots_include_market_open(tmp_path, monkeypatch):
    # Codex review ("Feed market-open fills into later top-up guards"): when
    # prior_allocation_roots is left at its default (None), the morning
    # 10:30 live guard must still scan the 9:35 market-open root, not just
    # the afternoon root, so a market-open fill offsets the morning desired
    # amount instead of being re-bought as a first allocation.
    import morning_dca

    afternoon_root = tmp_path / "afternoon"
    market_open_root = tmp_path / "market_open"
    monkeypatch.setattr(morning_dca, "DEFAULT_AFTERNOON_TASK_ROOT", str(afternoon_root))
    monkeypatch.setattr(morning_dca, "DEFAULT_MARKET_OPEN_TASK_ROOT", str(market_open_root))

    _seed_prior(market_open_root, "NVDA", 2.0, reference_price=95.0, selected_base_amount_usd=2.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        # Left at the class default (None) so the guard auto-derives its
        # roots, exercising the [DEFAULT_AFTERNOON_TASK_ROOT,
        # DEFAULT_MARKET_OPEN_TASK_ROOT] fallback directly.
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" in broker.placed, f"expected incremental order; broker_outcomes={res['artifacts'].get('broker_outcomes')}"
    assert abs(broker.placed["NVDA"] - 8.0) < 0.01, f"expected $8 incremental; got {broker.placed['NVDA']}"


def test_morning_live_scheduled_artifact_not_prior_context_on_retry(tmp_path):
    # Regression for Codex P2: a scheduled run artifact under YYYYMMDD_1030ET/
    # inside self.state_root must NOT be treated as prior context on a same-day
    # retry, because that would route the retry through the top-up path instead
    # of the normal broker duplicate-order guard.
    live_root = tmp_path / "live"
    live_root.mkdir()
    # Seed an earlier scheduled run artifact directly under YYYYMMDD_1030ET/
    # (the pattern used by MorningDCAExecutor._run_folder_path for scheduled runs).
    trading_date = _now_live().date()
    scheduled_folder = live_root / f"{trading_date.strftime('%Y%m%d')}_1030ET"
    scheduled_folder.mkdir(parents=True)
    state = {
        "trading_day": trading_date.isoformat(),
        "generated_at": f"{trading_date.isoformat()}T10:30:00-04:00",
        "run_classification": {"run_id": "morning_live_first", "run_origin": "scheduled", "stream": "morning"},
        "buy_candidates": [{"ticker": "NVDA", "status": "filled", "planned_amount_usd": 20.0, "target_price": 95.0}],
    }
    (scheduled_folder / "run_state.json").write_text(json.dumps(state), encoding="utf-8")

    configured_root = tmp_path / "configured_prior"
    configured_root.mkdir()

    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(configured_root),),  # enable guard without scanning state_root itself
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    rows = {r["ticker"]: r for r in artifacts.get("buy_candidates", [])}
    assert rows["NVDA"]["top_up_guard_result"] == "no_prior_run"

    # The scheduled artifact must be invisible to _compute_top_up.
    assert "NVDA" in broker.placed, f"expected full buy; broker={broker.placed}"
    top_up_offset = artifacts.get("top_up_offset_tickers", [])
    assert "NVDA" not in top_up_offset, (
        f"scheduled artifact incorrectly treated as prior context: top_up_offset_tickers={top_up_offset}"
    )


def test_morning_live_empty_roots_disables_guard(tmp_path):
    # Setting prior_allocation_roots=() must fully disable the top-up guard so
    # that a full buy is placed even when a prior fill exists.  This honours the
    # [] opt-out in morning_dca.json (load_morning_config preserves [] as ()).
    _seed_prior(tmp_path / "prior", "NVDA", 20.0, reference_price=95.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(),  # explicit empty → guard fully disabled
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    art = res["artifacts"]
    # Guard disabled → no top_up_offset_tickers, full buy placed.
    assert "NVDA" in broker.placed, f"expected full buy; broker.placed={broker.placed}"
    assert art.get("top_up_offset_tickers", []) == [], art.get("top_up_offset_tickers")
    assert art.get("top_up_manual_review_tickers", []) == [], art.get("top_up_manual_review_tickers")


def test_morning_top_up_guard_seeds_daily_cap_with_prior_spend(tmp_path):
    # A prior same-day fill on a DIFFERENT ticker has consumed $85 of the $90
    # daily cap. Without cap seeding the live run would see $90 remaining and
    # place a $7.50 order; with seeding it correctly sees only $5 remaining and
    # blocks the order as cap_exceeded.
    prior_root = _seed_prior(tmp_path / "prior", "PREV", 85.0, reference_price=50.0)
    cfg = MorningConfig(
        tickers=("TOPUP",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(prior_root),),
        cap_seed_roots=(str(prior_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=500.0,
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    # TOPUP: 10% drop -> 5x -> $15 desired; no prior fill for TOPUP itself.
    # $85 prior spend leaves only $5 -> $15 must be blocked as cap_exceeded.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "cap_exceeded", (
        f"TOPUP must be cap_exceeded: $15 > $5.00 remaining after $85 prior spend; "
        f"broker_outcomes={broker_outcomes}"
    )
    assert "TOPUP" not in broker.placed, "broker.place_market_buy must not be called"
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 85.0, (
        "cap ledger must be seeded from the prior $85 fill via cap_seed_roots"
    )
    budget = res["artifacts"]["budget"]
    assert budget["remaining_daily_usd"] == 5.0, (
        f"remaining_daily_usd should be 5.0 (90 - 85), got {budget['remaining_daily_usd']}"
    )


def test_morning_cap_seeding_runs_even_when_per_ticker_guard_disabled(tmp_path):
    # Codex review (PR #42): setting prior_allocation_roots=[] disables the per-ticker
    # guard but must NOT skip cap seeding. A morning manual fill of $85 recorded in
    # cap_seed_roots must still reduce the ledger so a subsequent live run respects
    # the daily cap even when the per-ticker offset guard is opted out.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker:
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    morning_root = tmp_path / "morning_prior"
    _write_prior_run_state(morning_root, "PRIOR_TICKER", amount_usd=85.0, decision_price=50.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(),  # guard disabled via []
        cap_seed_roots=(str(morning_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    # 3% drop → desired $7.50; cap = $90 - $85 = $5 remaining → cap_exceeded
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    # Cap seeding must have run: ledger reflects the $85 prior fill
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 85.0, (
        "cap ledger must be seeded from prior fills even when per-ticker guard is disabled"
    )
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "cap_exceeded", (
        "TOPUP should be cap-gated: prior $85 fill consumed most of $90 daily cap"
    )
    assert "TOPUP" not in broker.placed, "broker should not be called when cap is exhausted"


def test_morning_cap_seeding_own_amount_scoped_to_cap_roots(tmp_path):
    # Codex review (PR #42 line 960): when cap_seed_roots does NOT cover
    # state_root/manual_runs/, own_amount must not be subtracted from prev_total
    # because own fills are absent from new_total. Without the fix an in-memory
    # $7.50 own fill reduces prev_total below new_total and the external $4 is
    # never seeded, giving $4 too much daily headroom.
    import json as _json

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _SimpleBroker:
        def __init__(self):
            self.placed = {}
            self._placed_tickers: set = set()
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return ticker in self._placed_tickers
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            self._placed_tickers.add(ticker)
            return f"ORDER-{ticker}"

    live_root = tmp_path / "live"
    # cap_seed_roots points at a separate external root, NOT state_root/manual_runs
    external_cap_root = tmp_path / "external_cap"
    external_cap_root.mkdir(parents=True)
    pf_root = _seed_preflight(tmp_path / "pf", ("TICK",))
    cfg = MorningConfig(
        tickers=("TICK",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(live_root / "manual_runs"),),
        # cap_seed_roots does NOT cover state_root/manual_runs → own fills absent
        cap_seed_roots=(str(external_cap_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.03})

    # Step 1: manual run buys TICK ($7.50) → _own_recorded[day]=7.50
    _now_manual = datetime.combine(date(2026, 7, 8), time(9, 0), tzinfo=EASTERN)
    broker = _SimpleBroker()
    ex.run(_now_manual, data, broker=broker, manual=True)
    assert broker.placed.get("TICK") == 7.5

    # Step 2: an external $4 fill appears in external_cap_root (NOT under manual_runs)
    ext_dir = external_cap_root / "20260708_0901ET"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "run_state.json").write_text(
        _json.dumps({
            "trading_day": "2026-07-08", "generated_at": "2026-07-08T09:01:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [{"ticker": "EXT", "status": "submitted",
                                "planned_amount_usd": 4.0, "decision_price": 55.0}],
        }),
        encoding="utf-8",
    )

    # Step 3: scheduled run — TICK is dup-blocked. Cap seeding must pick up the
    # external $4 fill even though own_amount=$7.50 is not in external_cap_root.
    ex.run(_now_live(), data, broker=broker)
    ledger_after = ex.ledger.day_planned(date(2026, 7, 8))
    assert ledger_after == 7.5 + 4.0, (
        f"ledger should be own($7.50) + external-seed($4) = $11.50, got {ledger_after}"
    )


def test_morning_cap_seeding_ignores_own_amount_from_no_broker_manual_run(tmp_path):
    # Codex review on PR #42: a manual live invocation that intentionally runs
    # without a broker (broker=None) still went through the else branch of
    # run()'s record_buy_candidates call, which passes _track_own=manual with
    # EVERY hypothetical candidate -- not just genuinely submitted ones (there
    # is no broker_outcomes at all in that branch, so nothing was actually
    # submitted). That phantom own_amount then gets subtracted from a later
    # cap-seeding scan's new_total, and can suppress pickup of a real
    # external fill that appears afterward.
    import json as _json

    live_root = tmp_path / "live"
    # cap_seed_roots covers state_root/manual_runs, so own_amount WOULD be
    # credited if any own submission were genuinely on file.
    manual_root = live_root / "manual_runs"
    pf_root = _seed_preflight(tmp_path / "pf", ("TICK",))
    cfg = MorningConfig(
        tickers=("TICK",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(manual_root),),
        cap_seed_roots=(str(manual_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TICK": 0.03})

    # Step 1: manual run with NO broker -- purely hypothetical, nothing
    # submitted. Must NOT inflate _own_recorded_manual.
    manual_now = datetime.combine(date(2026, 7, 8), time(9, 0), tzinfo=EASTERN)
    ex.run(manual_now, data, manual=True)
    assert ex._own_recorded_manual.get(date(2026, 7, 8), 0.0) == 0.0, (
        f"no-broker manual run must not record a phantom own amount, got {ex._own_recorded_manual}"
    )

    # Step 2: a genuine external $4 fill appears under manual_root (as if a
    # different process/session actually placed and recorded it).
    ext_dir = manual_root / "20260708_0901ET"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "run_state.json").write_text(
        _json.dumps({
            "trading_day": "2026-07-08", "generated_at": "2026-07-08T09:01:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [{"ticker": "EXT", "status": "submitted",
                                "planned_amount_usd": 4.0, "decision_price": 55.0}],
        }),
        encoding="utf-8",
    )

    # Step 3: scheduled run must seed the full external $4 fill -- unaffected
    # by the earlier no-broker manual invocation.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker:
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id): return f"ORDER-{ticker}"

    ex.run(_now_live(), data, broker=_Broker())
    ledger_after = ex.ledger.day_planned(date(2026, 7, 8))
    assert ledger_after >= 4.0, (
        f"external $4 fill must be seeded even after an earlier no-broker manual run; "
        f"ledger={ledger_after}"
    )


def test_per_order_cap_blocks_without_any_broker_call(tmp_path):
    # Codex review #25 on PR #22: the per-order cap should block a candidate
    # without needing any broker/account round-trip, but _place_broker_orders
    # unconditionally called broker.available_buying_power() once per batch
    # BEFORE the per-candidate cap check ran for even the first candidate.
    # When every candidate in the batch exceeds max_per_order_usd, the broker
    # must never be touched at all.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _NoBuyingPowerCallBroker:
        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            raise AssertionError(
                "available_buying_power() must not be called when the "
                "per-order cap alone blocks every candidate"
            )

        def same_day_strategy_order_exists(self, ticker, trading_day):
            raise AssertionError("duplicate-check must not be reached for an over-cap candidate")

        def is_tradable_for_fractional_buy(self, ticker):
            raise AssertionError("tradability lookup must not be reached for an over-cap candidate")

        def review_market_buy(self, ticker, notional_usd):
            raise AssertionError("review must not be reached for an over-cap candidate")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("placement must not be reached for an over-cap candidate")

    pf_root = _seed_preflight(tmp_path / "pf", ("BIGDROP",))
    cfg = MorningConfig(
        tickers=("BIGDROP",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        max_per_order_usd=1.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIGDROP": 0.08})
    res = ex.run(_now_live(), data, broker=_NoBuyingPowerCallBroker())
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("BIGDROP", {}).get("status") == "blocked"
    assert "per-order ceiling" in broker_outcomes["BIGDROP"]["skip_reason"]


class _WideSpreadProvider(DecliningBarsProvider):
    """A provider exposing a live bid/ask quote with an excessive spread."""

    def __init__(self, *args, spread_pct=5.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._spread_pct = spread_pct

    def get_bid_ask(self, ticker, as_of):
        midpoint = 100.0
        half_spread = midpoint * (self._spread_pct / 100.0) / 2.0
        return (midpoint - half_spread, midpoint + half_spread)


def test_spread_breaker_blocks_wide_spread_before_placement(tmp_path):
    # Codex review #12 on PR #22: the afternoon stream enforces a bid/ask
    # spread circuit breaker before placing an order (PR #37), but the morning
    # and midday live streams share the same _place_broker_orders path and had
    # no equivalent check at all -- a wide-spread 10:30/12:30 candidate would
    # place directly through tradability/buying-power without ever reading a
    # quote.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _FakeLiveBroker:
        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def is_tradable_for_fractional_buy(self, ticker):
            raise AssertionError("tradability must not be reached once the spread breaker blocks")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("placement must not be reached once the spread breaker blocks")

    pf_root = _seed_preflight(tmp_path / "pf", ("BIGDROP",))
    cfg = MorningConfig(
        tickers=("BIGDROP",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        max_bid_ask_spread_pct=1.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = _WideSpreadProvider(SourceName.ROBINHOOD, 100.0, {"BIGDROP": 0.08}, spread_pct=5.0)
    res = ex.run(_now_live(), data, broker=_FakeLiveBroker())
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("BIGDROP", {}).get("status") == "blocked"
    assert "spread" in broker_outcomes["BIGDROP"]["skip_reason"]


def test_spread_breaker_allows_tight_spread(tmp_path):
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _FakeLiveBroker:
        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            return f"ORDER-{ticker}"

    pf_root = _seed_preflight(tmp_path / "pf", ("BIGDROP",))
    cfg = MorningConfig(
        tickers=("BIGDROP",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        max_bid_ask_spread_pct=1.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = _WideSpreadProvider(SourceName.ROBINHOOD, 100.0, {"BIGDROP": 0.08}, spread_pct=0.2)
    res = ex.run(_now_live(), data, broker=_FakeLiveBroker())
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("BIGDROP", {}).get("status") == "submitted"


def test_same_stream_order_exists_normalizes_legacy_boolean_adapters(tmp_path):
    # Codex review on PR #51: _same_stream_order_exists() changed its return
    # type from bool to Optional[Dict] so callers could preserve the real
    # broker order_id/amount. An adapter still implementing the previous
    # boolean contract (returning False for "no duplicate" or True for
    # "duplicate exists") must not be misread: `dup_order is not None` would
    # treat a legitimate False as a duplicate (skipping place_market_buy and
    # recording a phantom submission), and a legacy True would reach later
    # .get(...) calls on a bool and raise.
    class _LegacyBoolBroker:
        def __init__(self, result):
            self._result = result

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return self._result

    trading_day = date(2026, 7, 8)
    # Legacy False -> no duplicate -> None, not a truthy "duplicate found" value.
    assert _same_stream_order_exists(_LegacyBoolBroker(False), "NVDA", trading_day, "morning") is None
    # Legacy None -> no duplicate -> None.
    assert _same_stream_order_exists(_LegacyBoolBroker(None), "NVDA", trading_day, "morning") is None
    # Legacy True -> duplicate found, but with no order details to preserve;
    # must be a dict (supports .get(...)) rather than the bare True.
    result = _same_stream_order_exists(_LegacyBoolBroker(True), "NVDA", trading_day, "morning")
    assert result == {}
    assert result.get("id") is None
    # A real order dict passes through unchanged so real order_id/amount survive.
    order = {"id": "O1", "dollar_amount": 7.5}
    assert _same_stream_order_exists(_LegacyBoolBroker(order), "NVDA", trading_day, "morning") == order


def test_morning_live_same_stream_check_ignores_manual_fill_ref_id(tmp_path):
    # Regression for Codex P1 (PR #42): when a same-day manual fill placed BEFORE
    # the scheduled run uses the `morning-dca-manual-{date}-{ticker}` ref ID,
    # _same_stream_order_exists must NOT treat it as a same-stream duplicate.
    # Manual fills use the distinct `morning-dca-manual-*` prefix (note: `-manual-`
    # segment comes after `-dca-`, diverging from the plain scheduled prefix
    # `-dca-<date>-` at the fourth segment); _same_stream_order_exists checks
    # `morning-dca-topup-*` and `morning-dca-<date>-*`, so they never match it.
    prior_root = _seed_prior(
        tmp_path / "prior", "NVDA", 3.0, reference_price=95.0, selected_base_amount_usd=3.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))

    class _ManualFillBroker(_TopUpBroker):
        """Broker that has the manual fill's ref ID on record.

        Returns True only for the dca-manual prefix to simulate the manual fill
        sitting in broker history.  The scheduled same-stream check must NOT match
        this prefix; it should only look for morning-dca-topup-* and morning-dca-*.
        """
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Simulate the manual fill's distinct ref ID being present at the broker.
            manual_prefix = f"morning-dca-manual-{trading_day.isoformat()}-{ticker}"
            return any(p == manual_prefix for p in ref_id_prefixes)

    cfg = MorningConfig(
        tickers=("NVDA",),
        base_buy_amount_usd=3.0,
        max_per_order_usd=15.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _ManualFillBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    # The incremental top-up ($15 - $3 = $12) must be placed.  _same_stream_order_exists
    # checks morning-dca-topup-* and morning-dca-<date>-*, neither of which matches the
    # manual fill's morning-dca-manual-* prefix at the broker.
    assert "NVDA" in broker.placed, (
        f"incremental top-up must be placed; broker_outcomes={res['artifacts'].get('broker_outcomes')}"
    )
    assert abs(broker.placed["NVDA"] - 12.0) < 0.01, (
        f"expected $12 incremental; got {broker.placed['NVDA']}"
    )


def test_morning_live_retry_after_manual_fill_detects_plain_scheduled_order(tmp_path):
    # Regression for Codex P1 retry scenario (PR #42): if a scheduled run first
    # places morning-dca-{date}-NVDA (no prior allocation), then a manual fill
    # appears before a retry, _same_stream_order_exists must STILL detect the
    # already-live plain scheduled order via the morning-dca-* prefix.
    # Manual fills now use morning-dca-manual-* so the plain prefix check remains
    # safe to keep in _same_stream_order_exists.
    prior_root = _seed_prior(tmp_path / "prior", "NVDA", 3.0, reference_price=95.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))

    class _RetryBroker(_TopUpBroker):
        """Broker that already has the plain scheduled order (morning-dca-*) on record."""
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Simulate the already-live plain scheduled order sitting in broker history.
            plain_prefix = f"morning-dca-{trading_day.isoformat()}-{ticker}"
            return any(p == plain_prefix for p in ref_id_prefixes)

    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _RetryBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    # The retry must NOT place a second order: _same_stream_order_exists detects
    # the already-live plain morning-dca-* order via the plain prefix check.
    assert "NVDA" not in broker.placed, (
        f"retry must be blocked by same-stream check; broker.placed={broker.placed}; "
        f"broker_outcomes={res['artifacts'].get('broker_outcomes')}"
    )


# --- Raw-input persistence / failure-state guarantee (live_schedule_hardening_2026_07_20) --

def test_raw_input_recorder_persists_and_lists_files(tmp_path):
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    recorder.persist("prior_close", "AAPL", {"close": 100.0})
    recorder.persist("quote", "AAPL", {"price": 99.5})
    assert recorder.error is None
    assert len(recorder.saved_filenames) == 2
    for name in recorder.saved_filenames:
        assert (tmp_path / "raw" / name).exists()


def test_raw_input_recorder_records_error_without_raising(tmp_path):
    # A file (not a directory) sitting at the target raw/ path makes mkdir()
    # fail; persist() must record the failure rather than propagate it, so a
    # persistence problem never crashes deep inside signal evaluation.
    raw_path = tmp_path / "raw"
    raw_path.write_text("not a directory", encoding="utf-8")
    recorder = _RawInputRecorder(raw_path, "2026-07-08")
    recorder.persist("prior_close", "AAPL", {"close": 100.0})
    assert recorder.error is not None
    assert recorder.saved_filenames == []


def test_persisting_market_data_provider_records_bar_and_quote_responses(tmp_path):
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.03})
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(data, recorder)
    now = _now_live()
    bar = wrapped.get_previous_close_bar("AAPL", now)
    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=15
    )
    assert bar is not None
    assert bars
    assert recorder.error is None
    assert any(name.startswith("prior_close_AAPL_") for name in recorder.saved_filenames)
    assert any(name.startswith("intraday_bars_AAPL_") for name in recorder.saved_filenames)
    # Attributes/methods not explicitly wrapped still reach the inner provider.
    assert wrapped.source == data.source


def test_persisting_broker_adapter_records_buying_power_and_tradability(tmp_path):
    broker = FakeLiveBroker()
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(broker, recorder)
    assert wrapped.available_buying_power() == 500.0
    assert wrapped.is_tradable_for_fractional_buy("AAPL") is True
    assert recorder.error is None
    assert any(name.startswith("buying_power_") for name in recorder.saved_filenames)
    assert any(name.startswith("tradability_AAPL_") for name in recorder.saved_filenames)
    # Optional non-protocol broker methods still forward through __getattr__.
    assert (
        wrapped.same_day_strategy_order_exists_with_ref_id_prefix("AAPL", date(2026, 7, 8), [])
        is None
    )


def test_scheduled_live_run_persists_raw_inputs_on_success(tmp_path):
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, broker=FakeLiveBroker())
    artifacts = res["artifacts"]
    assert artifacts.get("status") != "blocked"
    raw_dir = Path(artifacts["raw_input_dir"])
    assert raw_dir.is_dir()
    saved = artifacts["raw_inputs_saved"]
    assert saved
    for name in saved:
        assert (raw_dir / name).exists()
    assert any(name.startswith("prior_close_") for name in saved)
    assert any(name.startswith("intraday_bars_") for name in saved)
    assert any(name.startswith("buying_power_") for name in saved)
    assert any(name.startswith("tradability_") for name in saved)
    assert "raw_persistence_error" not in artifacts


def test_manual_live_run_does_not_persist_raw_inputs(tmp_path):
    # live_schedule_hardening_2026_07_20 is scoped to the SCHEDULED live task;
    # a manual run gains no (and requires no) raw persistence.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, manual=True, broker=FakeLiveBroker())
    assert "raw_input_dir" not in res["artifacts"]
    assert "raw_inputs_saved" not in res["artifacts"]


def test_scheduled_live_run_fails_closed_when_raw_persistence_fails_before_placement(tmp_path):
    # A raw-input persistence failure discovered during evaluate() (price
    # data, fetched before any broker call) must block the run BEFORE any
    # order is placed, writing a valid failure run_state rather than
    # escalating with no state file at all -- the exact Jul 20 2026 incident
    # this policy targets (config/morning_dca.json:
    # live_schedule_hardening_2026_07_20).
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "raw").write_text("not a directory", encoding="utf-8")

    broker = FakeLiveBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["error_class"] == "missing_or_invalid_raw_inputs"
    assert "raw input persistence failed" in artifacts["block_reason"]
    assert artifacts["broker_outcomes"] == {}
    assert Path(res["paths"]["run_state"]).exists()


class _Unserializable:
    def __repr__(self):
        return "<unserializable>"


class _BadTradabilityBroker(FakeLiveBroker):
    def is_tradable_for_fractional_buy(self, ticker):
        return _Unserializable()


def test_scheduled_live_run_surfaces_non_fatal_raw_persistence_error(tmp_path):
    # A persistence failure discovered only once broker-side calls begin
    # (evaluate() already succeeded, so nothing blocks up front) must not
    # retroactively block an order that already passed every safety check --
    # it is recorded on the final run_state for operator visibility instead.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, broker=_BadTradabilityBroker())
    artifacts = res["artifacts"]
    assert artifacts.get("status") != "blocked"
    assert "raw_persistence_error" in artifacts
    assert "SMALL" in artifacts.get("broker_outcomes", {})


def test_run_does_not_write_model_usage_log_by_default(tmp_path):
    # model_usage_log_root defaults to None -- run() must not touch any
    # model-usage path unless a caller opts in explicitly.
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    ex.run(_now(), data)
    assert not (tmp_path / "model_usage").exists()


def test_run_appends_model_usage_log_entry_when_root_configured(tmp_path):
    import json as _json

    from scripts.model_usage_log import resolve_log_path

    log_root = tmp_path / "model_usage"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "state"),
        model_usage_log_root=str(log_root),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_PREFLIGHT_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    now = _now()
    res = ex.run(now, data)

    log_path = resolve_log_path(now.astimezone(timezone.utc), root=log_root)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = _json.loads(lines[0])
    assert entry["cron_id"] == MORNING_PREFLIGHT_CRON_ID
    assert entry["run_id"] == res["artifacts"]["run_id"]
    assert entry["artifact_path"] == res["paths"]["run_state"]
    assert entry["run_origin"] == "scheduled"
    assert entry["model_actual"] == "not_exposed"
    assert entry["model_routing_mode"] == "cost_conscious"


def test_run_manual_logs_manual_run_origin(tmp_path):
    import json as _json

    from scripts.model_usage_log import resolve_log_path

    log_root = tmp_path / "model_usage"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "state"),
        model_usage_log_root=str(log_root),
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    now = _now()
    ex.run(now, data, manual=True)

    log_path = resolve_log_path(now.astimezone(timezone.utc), root=log_root)
    entry = _json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["run_origin"] == "manual"


def test_run_records_premium_routing_metadata_when_config_supplies_it(tmp_path):
    # By default every run logs as cost_conscious (this code has no way to
    # know the real routing mode). A caller that DOES know -- e.g. the
    # scheduler escalated to a premium model for failure recovery -- can
    # override it via MorningConfig's model_usage_model_* fields.
    import json as _json

    from scripts.model_usage_log import resolve_log_path

    log_root = tmp_path / "model_usage"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "state"),
        model_usage_log_root=str(log_root),
        model_usage_model_requested="claude-opus-4-8",
        model_usage_model_actual="claude-opus-4-8",
        model_usage_model_routing_mode="premium",
        model_usage_reason_for_premium_model="failure_recovery",
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    now = _now()
    ex.run(now, data)

    log_path = resolve_log_path(now.astimezone(timezone.utc), root=log_root)
    entry = _json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["model_routing_mode"] == "premium"
    assert entry["reason_for_premium_model"] == "failure_recovery"
    assert entry["model_requested"] == "claude-opus-4-8"
    assert entry["model_actual"] == "claude-opus-4-8"


def test_model_usage_routing_overrides():
    from unittest.mock import Mock

    from morning_dca import _model_usage_routing_overrides

    # Test all fields set
    config_all = Mock(spec=["model_usage_model_requested", "model_usage_model_actual", "model_usage_model_routing_mode", "model_usage_reason_for_premium_model"])
    config_all.model_usage_model_requested = "model_a"
    config_all.model_usage_model_actual = "model_b"
    config_all.model_usage_model_routing_mode = "fast"
    config_all.model_usage_reason_for_premium_model = "needed"
    assert _model_usage_routing_overrides(config_all) == {
        "model_requested": "model_a",
        "model_actual": "model_b",
        "model_routing_mode": "fast",
        "reason_for_premium_model": "needed",
    }

    # Test partial fields set
    config_partial = Mock(spec=["model_usage_model_requested", "model_usage_model_actual", "model_usage_model_routing_mode", "model_usage_reason_for_premium_model"])
    config_partial.model_usage_model_requested = "model_a"
    config_partial.model_usage_model_actual = None
    config_partial.model_usage_model_routing_mode = "fast"
    config_partial.model_usage_reason_for_premium_model = None
    assert _model_usage_routing_overrides(config_partial) == {
        "model_requested": "model_a",
        "model_routing_mode": "fast",
    }

    # Test no fields set
    config_empty = Mock(spec=["model_usage_model_requested", "model_usage_model_actual", "model_usage_model_routing_mode", "model_usage_reason_for_premium_model"])
    config_empty.model_usage_model_requested = None
    config_empty.model_usage_model_actual = None
    config_empty.model_usage_model_routing_mode = None
    config_empty.model_usage_reason_for_premium_model = None
    assert _model_usage_routing_overrides(config_empty) == {}

    # Test fields completely missing from config object
    config_missing = Mock(spec=[])
    assert _model_usage_routing_overrides(config_missing) == {}


def test_nth_weekday_of_month():
    cases = [
        (2024, 1, 0, 1, date(2024, 1, 1)),  # 1st Monday of Jan 2024 (month starts on Monday)
        (2024, 1, 4, 3, date(2024, 1, 19)),  # 3rd Friday of Jan 2024 (options exp)
        (2024, 11, 3, 4, date(2024, 11, 28)),  # 4th Thursday of Nov 2024 (Thanksgiving)
        (2024, 2, 3, 5, date(2024, 2, 29)),  # 5th Thursday of Feb 2024 (Leap Year)
        (2025, 3, 2, 2, date(2025, 3, 12)),  # 2nd Wednesday of Mar 2025
    ]
    for year, month, weekday, n, expected in cases:
        assert _nth_weekday_of_month(year, month, weekday, n) == expected


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                    with tempfile.TemporaryDirectory() as d:
                        fn(Path(d))
                else:
                    fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
