"""Self-tests for the morning graded-DCA executor (watchlist scan + live).

Covers the morning ladder threshold brackets, the separate budget ledger
namespace, the run-state path/classification for BOTH the 9:45 watchlist scan
and the 10:30 live execution, the live-mode confidence rule, and the guarantee
that a morning run never interacts with the afternoon ce16ba6b scheduled
evidence.

Runnable with ``pytest`` or ``python test_morning_dca.py``.
"""

import json
from datetime import date, datetime, time, timedelta
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
    MORNING_PREFLIGHT_MODE,
    MORNING_SIZING_TIERS,
    DCAExecutor,
    MorningConfig,
    MorningDCAExecutor,
    MorningLedger,
    MorningSignal,
    TopUpOutcome,
    load_same_day_preflight,
    nyse_holidays,
    preflight_eligibility,
    preflight_ticker_eligibility_row,
)
from near_close_dca import EASTERN, Action, Bar, SourceName


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
        return False

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
    # the shared $3-base 0x-to-5x ladder (was 0x-to-3x, now historical).
    ex = MorningDCAExecutor(MorningConfig(base_buy_amount_usd=3.0))
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
    cfg = MorningConfig(tickers=("UP", "FLAT", "TINY", "SMALL", "BIG"), base_buy_amount_usd=3.0)
    drops = {"UP": -0.01, "FLAT": 0.0, "TINY": 0.0005, "SMALL": 0.03, "BIG": 0.10}
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, drops)
    sigs = {s.ticker: s for s in ex.evaluate(_now(), data)}

    assert sigs["UP"].action == Action.SKIP  # price above prior close
    assert sigs["UP"].multiplier == 0.0
    assert sigs["FLAT"].action == Action.SKIP  # exactly at prior close, no weakness
    assert sigs["TINY"].action == Action.NO_ACTION  # below prior close but under 0.1% min drop
    assert sigs["TINY"].multiplier == 0.0
    assert sigs["SMALL"].action == Action.BUY
    assert sigs["SMALL"].multiplier == 2.5  # 3% drop -> 2.5x on the 0x-to-5x ladder
    assert sigs["SMALL"].hypothetical_amount_usd == 7.5
    assert sigs["BIG"].multiplier == 5.0  # 10% drop -> 5x
    assert sigs["BIG"].hypothetical_amount_usd == 15.0


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
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=5.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg)
    drops = {f"T{i}": 0.10 for i in range(5)}  # each 5x -> $15
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, drops)
    res = ex.run(_now(), data)
    assert any("exceeds morning daily cap" in w for w in res["artifacts"]["warnings"])


def test_build_artifacts_does_not_double_count_ledger(tmp_path):
    # build_artifacts must be side-effect-free: calling it more than once (e.g.
    # inspecting artifacts before writing) must not re-record spend against the
    # ledger. Only run()'s single record_buy_candidates() call should count.
    cfg = MorningConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0, morning_run_state_root=str(tmp_path),
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
    assert ex.ledger.day_planned(day) == 15.0  # 10% drop -> 5x -> $15, recorded once


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
    assert json.loads(run_state_path.read_text())["status"] == "blocked"
    # The scheduled pointer must never advance to a non-trading-day placeholder.
    assert not (Path(tmp_path) / "latest_morning_run.json").exists()


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
    assert not (Path(tmp_path) / "latest_morning_run.json").exists()


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
    assert rc["state_schema_version"] == "morning-live-v1"
    assert rc["run_id"] == "morning_live_20260708_1030ET"
    assert rc["cron_id"] == MORNING_LIVE_CRON_ID
    assert rc["valid_afternoon_evidence"] is False

    art = res["artifacts"]
    assert art["mode"] == "live"
    assert art["trades"] is True


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


def test_watchlist_mode_never_sets_execution_status():
    # The read-only watchlist scan never runs the trend gate; rows carry no
    # execution_status / trend_validated at all.
    cfg = MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0)
    ex = MorningDCAExecutor(cfg)  # default watchlist mode
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now(), data)[0]
    assert sig.execution_status is None
    assert sig.trend_validated is None
    assert "execution_status" not in sig.to_row()


def test_live_and_watchlist_roots_are_distinct():
    assert DEFAULT_MORNING_WATCHLIST_STATE_ROOT != DEFAULT_MORNING_LIVE_STATE_ROOT
    assert DEFAULT_MORNING_STATE_ROOT == DEFAULT_MORNING_WATCHLIST_STATE_ROOT
    assert "ce16ba6b" not in DEFAULT_MORNING_LIVE_STATE_ROOT


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

def test_nyse_holidays_includes_observed_next_years_new_year_in_current_year():
    """When Jan 1 of year+1 falls on Saturday, Dec 31 of year must be in nyse_holidays(year).

    2027-12-31 is the NYSE-observed closure for New Year's Day 2028 (Jan 1 2028
    is a Saturday).  nyse_holidays(2027) must include it even though the
    'canonical' New Year's Day is in 2028.
    """
    from datetime import date as _date

    # Verify the premise: Jan 1 2028 is indeed a Saturday.
    assert _date(2028, 1, 1).weekday() == 5, "test premise: 2028-01-01 should be Saturday"

    holidays_2027 = nyse_holidays(2027)
    assert _date(2027, 12, 31) in holidays_2027, (
        "nyse_holidays(2027) must include 2027-12-31 (observed New Year\'s Day 2028)"
    )

    # Sanity: a year whose next Jan 1 is not Saturday should NOT include Dec 31
    # (unless Christmas observance lands there, which it doesn't for 2025).
    assert _date(2026, 1, 1).weekday() != 5
    holidays_2025 = nyse_holidays(2025)
    assert _date(2025, 12, 31) not in holidays_2025


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

def test_load_morning_config_falls_back_to_defaults_if_missing(tmp_path):
    """load_morning_config gracefully falls back when file is absent."""
    from morning_dca import load_morning_config

    cfg = load_morning_config(str(tmp_path / "nonexistent"))
    defaults = MorningConfig()
    assert cfg.base_buy_amount_usd == defaults.base_buy_amount_usd
    assert cfg.morning_daily_cap_usd == defaults.morning_daily_cap_usd


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
        load_morning_config(str(cfg_dir))


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
    assert ex.config.base_buy_amount_usd == 3.0
    assert ex.config.morning_daily_cap_usd == 90.0
    assert date(2026, 7, 2) in ex.config.approved_test_dates
    assert date(2026, 8, 28) in ex.config.approved_test_dates


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
    state_path = Path(res["paths"]["run_state"])
    assert state_path.exists()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "blocked"
    assert persisted["trades"] is False
    assert persisted["broker_outcomes"] == {}

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
    """Write a preflight run_state.json for the given tickers into tmp_path."""
    if trading_date is None:
        trading_date = _now_live().date()
    folder = trading_date.strftime("%Y%m%d") + "_0945ET"
    pf_dir = tmp_path / folder
    pf_dir.mkdir(parents=True, exist_ok=True)
    (pf_dir / "run_state.json").write_text(
        json.dumps({"eligible_to_execute_tickers": list(tickers)}),
        encoding="utf-8",
    )
    return tmp_path


def test_live_gate_blocks_outside_approved_window(tmp_path):
    # A scheduled live run on a day outside approved_test_dates returns a blocked result.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 7)}),  # not 2026-07-08 (_now_live())
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, manual=False)
    assert res["artifacts"]["status"] == "blocked"
    assert res["artifacts"].get("block_reason") == "outside approved trading window"


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


def test_auto_buy_candidates_prioritized_by_multiplier_when_cap_hit(tmp_path):
    # When the daily cap is tight, higher-multiplier tickers are placed first.
    # BIGDROP: 8% drop -> 5x -> $15 notional. SMALLDROP: 1% drop -> 1.5x -> $4.50 notional.
    # Daily cap of $16 allows BIGDROP ($15 < $16) then blocks SMALLDROP ($15+$4.50 > $16).
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
        morning_daily_cap_usd=16.0,
        morning_weekly_cap_usd=500.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # BIGDROP: 8% drop -> 5x multiplier -> $15. SMALLDROP: 1% drop -> 1.5x -> $4.50.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIGDROP": 0.08, "SMALLDROP": 0.01})
    res = ex.run(_now_live(), data, broker=_FakeLiveBroker())
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("BIGDROP", {}).get("status") == "submitted", "BIGDROP (5x) should be placed first"
    assert broker_outcomes.get("SMALLDROP", {}).get("status") == "cap_exceeded", "SMALLDROP (1.5x) should be cap-gated out"


# ---------------------------------------------------------------------------
# Top-up guard regression tests
# ---------------------------------------------------------------------------

def _seed_prior(root, ticker, amount_usd, *, status="filled", reference_price=95.0, trading_date=None):
    """Write a prior-fill run_state.json for one ticker under root."""
    if trading_date is None:
        trading_date = _now_live().date()
    folder = trading_date.strftime("%Y%m%d") + "_0900ET"
    run_dir = Path(root) / folder
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate: dict = {"ticker": ticker, "status": status, "planned_amount_usd": amount_usd}
    if status == "filled" and reference_price is not None:
        candidate["target_price"] = reference_price
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
    # Desired = 5x * $3 = $15. Incremental = $15 - $3 = $12.
    prior_root = _seed_prior(tmp_path / "prior", "NVDA", 3.0, reference_price=95.0)
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    assert "NVDA" in broker.placed, f"expected incremental order; broker_outcomes={res['artifacts'].get('broker_outcomes')}"
    assert abs(broker.placed["NVDA"] - 12.0) < 0.01, f"expected $12 incremental; got {broker.placed['NVDA']}"
    rows = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert rows["NVDA"]["top_up_guard_result"] == "incremental_top_up"


def test_morning_live_skips_when_price_not_below_prior_reference(tmp_path):
    # Prior: NVDA $3 at $85 reference. Morning price $90 (10% drop from $100).
    # $90 >= $85 → price_not_below → skip, no order.
    prior_root = _seed_prior(tmp_path / "prior", "NVDA", 3.0, reference_price=85.0)
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    assert "NVDA" not in broker.placed, f"expected no order; broker.placed={broker.placed}"
    art = res["artifacts"]
    assert "NVDA" in art.get("top_up_offset_tickers", []), art.get("top_up_offset_tickers")


def test_morning_live_full_buy_when_no_prior_allocation(tmp_path):
    # Empty prior root → no_prior_run → buy the full desired amount ($15 = 5x * $3).
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    assert "NVDA" in broker.placed, f"expected full buy; broker.placed={broker.placed}"
    assert abs(broker.placed["NVDA"] - 15.0) < 0.01, f"expected $15; got {broker.placed['NVDA']}"
    rows = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert rows["NVDA"]["top_up_guard_result"] == "no_prior_run"


def test_morning_live_missing_price_triggers_manual_review(tmp_path):
    # Prior: NVDA "submitted" with unknown reference price → missing_prior_price → $0 + manual review.
    prior_root = _seed_prior(tmp_path / "prior", "NVDA", 9.0, status="submitted", reference_price=None)
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
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
    # Prior: NVDA $20 at $95 reference. Desired = $15. $15 - $20 = -$5 ≤ 0 → no_incremental.
    prior_root = _seed_prior(tmp_path / "prior", "NVDA", 20.0, reference_price=95.0)
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    assert "NVDA" not in broker.placed, f"expected no order; broker.placed={broker.placed}"
    art = res["artifacts"]
    assert "NVDA" in art.get("top_up_offset_tickers", []), art.get("top_up_offset_tickers")
    rows = {r["ticker"]: r for r in art.get("buy_candidates", [])}
    assert rows["NVDA"]["top_up_guard_result"] == "no_incremental_difference"


def test_morning_live_prior_from_configured_root_only(tmp_path):
    # Only roots in prior_allocation_roots contribute; fills under other_root are invisible.
    # configured_root: NVDA $3 at $95 → incremental $12.
    # other_root: NVDA $20 at $95 → would produce no_incremental if scanned.
    configured_root = _seed_prior(tmp_path / "configured", "NVDA", 3.0, reference_price=95.0)
    _seed_prior(tmp_path / "other", "NVDA", 20.0, reference_price=95.0)
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(configured_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    # Configured root contributes $3 only → incremental $12, not no_incremental.
    assert "NVDA" in broker.placed, f"expected incremental order; broker_outcomes={res['artifacts'].get('broker_outcomes')}"
    assert abs(broker.placed["NVDA"] - 12.0) < 0.01, f"expected $12; got {broker.placed['NVDA']}"


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

    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(live_root),
        prior_allocation_roots=(str(configured_root),),  # enable guard without scanning state_root itself
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

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
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(),  # explicit empty → guard fully disabled
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    art = res["artifacts"]
    # Guard disabled → no top_up_offset_tickers, full buy placed.
    assert "NVDA" in broker.placed, f"expected full buy; broker.placed={broker.placed}"
    assert art.get("top_up_offset_tickers", []) == [], art.get("top_up_offset_tickers")
    assert art.get("top_up_manual_review_tickers", []) == [], art.get("top_up_manual_review_tickers")

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
