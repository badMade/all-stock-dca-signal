"""Self-tests for the morning graded-DCA executor (watchlist scan + live).

Covers the morning ladder threshold brackets, the separate budget ledger
namespace, the run-state path/classification for BOTH the 9:45 watchlist scan
and the 10:30 live execution, the live-mode confidence rule, and the guarantee
that a morning run never interacts with the afternoon ce16ba6b scheduled
evidence.

Runnable with ``pytest`` or ``python test_morning_dca.py``.
"""

import contextlib
import csv
import os
import re
import subprocess
import sys

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
os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, cast

import pytest

# Moved into tests/ from the repo root (2026-08-23). ``pytest``/``python -m
# pytest`` already resolve `import morning_dca`/`near_close_dca`/etc. via
# pyproject.toml's `pythonpath = [".", "tests"]`, but this file's own
# docstring documents a second, direct-execution contract (``python
# tests/test_morning_dca.py``) that pythonpath doesn't cover -- running a
# script directly only adds ITS OWN directory (tests/) to sys.path, not the
# repo root (Codex review: "Retain direct execution for relocated tests").
# A bare `if` guard (no assignment), like the `os.environ.setdefault` call
# above: ruff's E402 exempts conditionals, so every import below still
# needs no `noqa`.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import morning_dca
from dca_config import DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES, MissingConfigEnvError
from dca_live_adapters import FinanceDataProvider, RobinhoodBroker, RobinhoodDataProvider
from dca_paths import CRON_TRACKING_ROOT_ENV
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
    MORNING_WATCHLIST_MODE,
    DCAExecutor,
    MorningConfig,
    MorningDCAExecutor,
    MorningLedger,
    MorningSignal,
    RawInputPersistenceError,
    TopUpOutcome,
    _csv_qualifying_notional_total,
    _forbidden_preflight_root_reason,
    _PersistingBrokerAdapter,
    _PersistingMarketDataProvider,
    _RawInputRecorder,
    _same_stream_order_exists,
    _sanitize_budget_ledger_filename,
    _stable_broker_order_id,
    load_max_consecutive_placement_failures,
    load_max_orders_per_day,
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
    account_pinning_failure,
)
from scripts.file_lock import cross_process_lock

REPO_ROOT = Path(__file__).resolve().parent.parent


# C:F1 (2026-09-07 diagnostic): a LIVE RobinhoodBroker now refuses to answer a
# lookup whose callable is unwired (simulated buying power, "tradable", "no
# order today" are dry-run defaults only), so fixtures that exercise placement
# wire the three lookups explicitly with permissive stubs.
def _no_orders_today(**_kwargs):
    # An explicit, provable empty page (an ``orders`` list), not a bare
    # ``results`` page: a live broker now refuses a page shape it cannot
    # recognize instead of reading it as "no order today" (2026-09-07 mock
    # beta run of the executor classes).
    return {"orders": [], "next": None}


def _all_tradable(**kwargs):
    return {
        "results": [
            {"symbol": symbol, "tradeable": True, "state": "active", "fractional_tradability": "tradable"}
            for symbol in kwargs.get("symbols", ())
        ]
    }


def _ample_buying_power(**_kwargs):
    return {"buying_power": "300"}


_LIVE_LOOKUP_STUBS = {
    "buying_power_fn": _ample_buying_power,
    "orders_fn": _no_orders_today,
    "tradability_fn": _all_tradable,
}


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

    def get_bid_ask(self, ticker, as_of):
        """A tight, well-inside-tolerance quote (0.1% spread).

        A scheduled live run fails a candidate closed when its spread cannot be
        proven, so a provider double that exposes no bid/ask would block every
        placement. Tests that specifically exercise an unavailable or wide spread
        override this.
        """
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        return (price * 0.9995, price * 1.0005)


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
    supports_concurrent_fill_lookup: bool = False

    def account_number(self) -> str:
        from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self) -> float:
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool:
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes) -> Optional[Dict[str, Any]]:
        return None

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id) -> Optional[Dict[str, Any]]:
        return None

    def same_day_ticker_notional_usd(self, ticker, trading_day) -> float:
        return 0.0

    def is_tradable_for_fractional_buy(self, ticker) -> bool:
        return True

    def review_market_buy(self, ticker, notional_usd) -> Tuple[bool, str]:
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
        return f"ORDER-{ticker}"

    def get_order_fill(self, order_id) -> Any:
        return None

    def same_day_order_with_ref_id_prefix_any_state(
        self, ticker, trading_day, ref_id_prefixes
    ) -> Optional[Dict[str, Any]]:
        return self.same_day_strategy_order_exists_with_ref_id_prefix(
            ticker, trading_day, ref_id_prefixes
        )

    def is_tradable_for_whole_share_buy(self, ticker) -> bool:
        return True

    def review_whole_share_buy(self, ticker, quantity) -> Tuple[bool, str]:
        return True, "ok"

    def place_whole_share_buy(self, ticker, quantity, ref_id) -> str:
        return f"ORDER-WS-{ticker}"

    def get_raw_placement_responses(self) -> list:
        return []


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


def test_nyse_holidays_excludes_juneteenth_before_nyse_adoption():
    # The NYSE first observed Juneteenth as a full-market closure in 2022
    # (June 20, the observed date for June 19 falling on a Sunday); June 19,
    # 2021 -- before that rulemaking took effect -- was an ordinary open
    # trading day and must not be misclassified as a closure.
    assert date(2021, 6, 19) not in nyse_holidays(2021)
    assert len(nyse_holidays(2021)) == 9
    assert date(2022, 6, 20) in nyse_holidays(2022)  # first observed closure


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
        0.0005: 0.0,   # < 0.1%  -> 0x
        0.001: 1.0,    # 0.1%    -> 1x
        0.019: 1.0,    # < 2%    -> 1x
        0.020: 1.5,    # 2%      -> 1.5x
        0.039: 1.5,    # < 4%    -> 1.5x
        0.040: 2.0,    # 4%      -> 2x
        0.050: 2.5,    # 5%      -> 2.5x
        0.060: 3.0,    # 6%      -> 3x
        0.065: 3.5,    # 6.5%    -> 3.5x
        0.070: 4.0,    # 7%      -> 4x
        0.075: 4.5,    # 7.5%    -> 4.5x
        0.080: 5.0,    # 8%      -> 5x
        0.20: 5.0,     # >= 8%   -> 5x (no upper cutoff)
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
    assert sigs["SMALL"].multiplier == 1.5  # 3% drop -> 1.5x on the 0x-to-5x ladder
    assert sigs["SMALL"].hypothetical_amount_usd == 3.0
    assert sigs["BIG"].multiplier == 5.0  # 10% drop -> 5x
    assert sigs["BIG"].hypothetical_amount_usd == 10.0


def test_signal_zero_multiplier_below_configured_minimum_drop_normalizes_to_no_action():
    # Codex review ("Normalize every zero-multiplier morning signal"): a
    # customized config can set minimum_drop_vs_previous_close BELOW the
    # first positive sizing tier (0.1%). A drop of 0.05% then clears the
    # configured minimum (0.01%) but multiplier_for_drop() still returns 0x
    # for it. Before the fix this fell into the BUY branch with
    # multiplier=0.0/hypothetical=0.0 -- action=BUY with a zeroed amount,
    # which downstream serializes auto_buy_signal=true even though
    # _buy_candidates() (multiplier > 0) still excludes it, a false
    # execution-gap alert. The signal must be normalized to a genuine
    # no-action/skip result instead.
    cfg = MorningConfig(
        tickers=("BELOWTIER",), base_buy_amount_usd=2.0, minimum_drop_vs_previous_close=0.0001
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BELOWTIER": 0.0005})
    sig = {s.ticker: s for s in ex.evaluate(_now(), data)}["BELOWTIER"]

    assert sig.action == Action.NO_ACTION
    assert sig.multiplier == 0.0
    assert sig.hypothetical_amount_usd == 0.0
    assert sig.selected_base_amount_usd == 0.0
    assert sig.base_selection_reason == "not_below_prior_close_no_buy"


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
    # Two tickers at the same 3% drop (1.5x) but on opposite sides of the prior low.
    # SHALLOW closes at 97 (below prior close 100, above prior low 96) -> $1 base -> $1.50.
    # DEEP closes at 97 too but with a higher prior low so it is below both -> $2 base -> $3.00.
    cfg = MorningConfig(tickers=("SHALLOW",), base_buy_amount_usd=2.0, base_buy_amount_below_close_usd=1.0)
    ex = MorningDCAExecutor(cfg)
    shallow = FixedDropProviderWithLow(SourceName.ROBINHOOD, 100.0, {"SHALLOW": 0.03}, previous_low=96.0)
    sig = ex.evaluate(_now(), shallow)[0]
    assert sig.action == Action.BUY
    assert sig.multiplier == 1.5
    assert sig.previous_low == 96.0
    assert sig.selected_base_amount_usd == 1.0
    assert sig.base_selection_reason == "below_prior_close_only"
    assert sig.hypothetical_amount_usd == 1.5

    cfg_deep = MorningConfig(tickers=("DEEP",), base_buy_amount_usd=2.0, base_buy_amount_below_close_usd=1.0)
    deep = FixedDropProviderWithLow(SourceName.ROBINHOOD, 100.0, {"DEEP": 0.03}, previous_low=98.0)
    sig_deep = MorningDCAExecutor(cfg_deep).evaluate(_now(), deep)[0]
    assert sig_deep.action == Action.BUY
    assert sig_deep.selected_base_amount_usd == 2.0
    assert sig_deep.base_selection_reason == "below_prior_close_and_low"
    assert sig_deep.hypothetical_amount_usd == 3.0


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


def test_bar_count_excludes_the_unfinished_decision_bar():
    # Codex review ("Count only completed bars in archived observations"):
    # the provider's request window ends at decision_dt, so when bar
    # timestamps denote bar starts (the default) the bar AT decision_dt is
    # not yet complete. session_stats already excludes it from the OHLCV
    # aggregates, but bar_count independently counted the full unfiltered
    # list -- reporting one more completed interval than the aggregates and
    # decision price actually cover.
    decision_dt = _now()  # 09:45; FixedDropProvider yields bars at 09:30/35/40/45
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex = MorningDCAExecutor(MorningConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    sig = ex.evaluate(decision_dt, data)[0]
    assert sig is not None
    assert sig.observation is not None
    obs = sig.observation
    assert obs.bar_count == 3


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
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted["status"] == "blocked"
    # Full artifact shape -- not a hand-rolled stub.
    assert "run_id" in persisted
    assert "counts" in persisted
    assert "signal_rows" in persisted
    # Same-day guard consumers must see today's (blocked) evidence rather than
    # a stale pointer left over from the last real trading session.
    pointer = json.loads((Path(tmp_path) / "latest_morning_run.json").read_bytes())
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

    persisted = _json.loads(stale_path.read_bytes())
    # Stale stub must have been overwritten with the full artifact shape.
    assert "run_id" in persisted
    assert "counts" in persisted
    assert persisted["status"] == "blocked"
    pointer = _json.loads((Path(tmp_path) / "latest_morning_run.json").read_bytes())
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
    pointer = json.loads((Path(tmp_path) / "latest_morning_run.json").read_bytes())
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


def test_scheduled_live_run_blocked_outside_slot_window(tmp_path):
    # Codex review ("Reject scheduled runs outside their slot window"):
    # self.profile's window_start_et/window_end_et (10:25-10:35 for the
    # morning live slot) were only ever copied into run_classification for
    # display -- nothing compared the actual firing time against them, so a
    # delayed non-manual invocation (e.g. a stuck scheduler firing hours
    # late) would still evaluate and place orders labeled as the on-time
    # 10:30 scheduled run. 9:45 ET on a normal trading Wednesday is well
    # outside that window.
    now = datetime.combine(date(2026, 7, 8), time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data)

    assert res["artifacts"]["status"] == "blocked"
    assert res["artifacts"]["block_reason"] == "outside scheduled slot window"
    assert res["artifacts"]["orders_submitted_count"] == 0


def test_scheduled_live_run_allowed_inside_slot_window(tmp_path):
    # Control: 10:30 ET is inside the morning live slot's 10:25-10:35 window.
    now = _now_live()
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    broker = RecordingLiveBroker(account="999000111", reference_prices={"SMALL": 97.0})

    res = ex.run(now, data, broker=broker)

    assert res["artifacts"].get("status") != "blocked"
    assert res["artifacts"]["signal_rows"]


def test_manual_run_ignores_slot_window_gate(tmp_path):
    now = datetime.combine(date(2026, 7, 8), time(9, 45), tzinfo=EASTERN)
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(now, data, manual=True)

    assert res["artifacts"].get("status") != "blocked"
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
    # Read-only mode: account_number must be omitted even though
    # MorningConfig's default target_account_number resolves to a real,
    # well-formed account -- the field is documented as "used for morning
    # live execution" and a watchlist scan places no orders (Codex review,
    # PR #219: "Omit account numbers from read-only classifications").
    assert "account_number" not in rc

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


def test_manual_credit_idempotency_key_does_not_leak_across_run_invocations(tmp_path):
    # Codex review ("Reset the manual credit key for every run invocation"):
    # the manual credit-ledger idempotency key is cached on first use so
    # every credit-ledger call WITHIN one run() invocation agrees, but the
    # executor is reused across manual run() calls (see
    # test_manual_runs_at_same_timestamp_do_not_collide above) -- without a
    # reset, a SECOND manual run would silently inherit the FIRST's key
    # despite getting its own distinct run_id, so a later, genuinely
    # lower-price observation would look like an idempotent replay of the
    # earlier one and have its new pending credit silently discarded.
    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    now = _now()

    ex.run(now, data, manual=True)
    key_1 = ex._credit_idempotency_key(now, manual=True)
    ex.run(now, data, manual=True)
    key_2 = ex._credit_idempotency_key(now, manual=True)

    assert key_1 != key_2


def test_classification_matches_morning_schema(tmp_path):
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
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
    assert rc["account_number"] == "999000111"
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
        payload = json.loads(pointer.read_bytes())
        assert payload["run_state_path"] == run_state_path
        assert payload["stream"] == "morning"
        assert payload["same_day_required"] is True
        assert payload["pointer_status"] == "valid"
        # The pointer is valid -- it exists, it is same-day, and it resolves --
        # and the run it points at is NOT evidence the slot executed: with no
        # broker adapter this run is failed_scheduled_run_startup, blocked
        # before placement. Those are two different questions and the pointer
        # answers both (Codex review).
        assert payload["valid_scheduled_evidence"] is False
        assert "trading_day" in payload
        assert "updated_at_utc" in payload


def test_a_run_blocked_before_placement_is_not_scheduled_evidence(tmp_path):
    """A missing pre-flight, absent broker, or account mismatch stops the run
    before placement. build_artifacts() has already stamped the scheduled
    identity and these paths write immediately, so without an explicit denial
    the pointer publishes them as valid scheduled runs (Codex review)."""
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        preflight_run_state_root=str(tmp_path / "missing_preflight"),
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    res = ex.run(_now_live(), FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03}))

    run_state = json.loads(Path(res["paths"]["run_state"]).read_bytes())
    assert run_state["run_classification_outcome"] == "failed_scheduled_run_startup"
    assert run_state["is_trading_failure"] is True
    assert run_state["scheduled_evidence_denied"] is True
    assert run_state["run_classification"]["valid_scheduled_evidence"] is False
    assert derive_valid_scheduled_evidence(run_state) is False
    # The artifact is still written and the pointer still refreshed: proving the
    # slot fired is the whole reason this state exists.
    payload = json.loads((Path(tmp_path) / "latest_morning_run.json").read_bytes())
    assert payload["valid_scheduled_evidence"] is False


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
    ledger = json.loads(ledger_path.read_bytes())
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


def test_preflight_gate_forbidden_root_degrades_scheduled_run_not_a_block(tmp_path):
    """must_not_look_under: a preflight_run_state_root resolving under the
    10:30 live output directory must still be recognized as forbidden (the
    Jul 20 2026 incident) -- but as of 2026-08-02 (Geoff: "should not block
    any later runs") this degrades a SCHEDULED run to the full universe with
    a warning, exactly like a manual run, rather than blocking it to zero
    tickers."""
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
    res = ex.run(_now_live(), data, broker=FakeLiveBroker())

    assert res["artifacts"] is not None
    assert res["artifacts"].get("status") != "blocked"
    assert res["artifacts"]["preflight_gate"]["preflight_found"] is False
    assert "forbidden" in res["artifacts"]["preflight_gate"]["warning"]
    evaluated = {r["ticker"] for r in res["artifacts"]["signal_rows"]}
    assert evaluated == {"SMALL"}


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
    assert ex.preflight_gate is not None
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
    broker = DryRunBroker(account="999000111", reference_prices={"SMALL": 97.0})

    res = ex.run(_now_live(), data, broker=broker, manual=True)
    candidate = res["artifacts"]["buy_candidates"][0]

    # DryRunBroker always returns a "DRY-RUN-..." order id, which is recorded
    # as status="dry_run" (never "submitted") so the top-up guard never treats
    # a dry-run order as real allocated capital.
    assert candidate["status"] == "dry_run"
    assert candidate["order_id"].startswith("DRY-RUN-SMALL")
    assert candidate["planned_amount_usd"] == 4.5
    assert res["artifacts"]["broker_outcomes"]["SMALL"]["submitted_amount_usd"] == 4.5
    orders_csv_text = Path(res["paths"]["orders_csv"]).read_text(encoding="utf-8")
    assert orders_csv_text.startswith("ticker,order_id,notional_usd,status")
    assert "SMALL" in orders_csv_text


class RecordingLiveBroker(FakeLiveBroker):
    supports_concurrent_fill_lookup: bool = False

    def __init__(self, account, reference_prices=None):
        self._account = account
        self._reference_prices = reference_prices or {}
        self.placed_orders = []

    def account_number(self) -> str:
        return self._account

    def available_buying_power(self) -> float:
        return 10_000.0

    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool:
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes) -> Optional[Dict[str, Any]]:
        return None

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id) -> Optional[Dict[str, Any]]:
        return None

    def same_day_ticker_notional_usd(self, ticker, trading_day) -> float:
        return 0.0

    def is_tradable_for_fractional_buy(self, ticker) -> bool:
        return True

    def review_market_buy(self, ticker, notional_usd) -> Tuple[bool, str]:
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
        self.placed_orders.append((ticker, notional_usd, ref_id))
        return f"ORDER-{ticker}"

    def get_order_fill(self, order_id) -> Any:
        return None

    def get_raw_placement_responses(self) -> list:
        return []


def test_morning_artifacts_include_planned_amount_for_prior_allocation_loader(tmp_path):
    cfg = MorningConfig(tickers=("SMALL",), base_buy_amount_usd=3.0, morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    broker = DryRunBroker(account="999000111", reference_prices={"SMALL": 97.0})

    res = ex.run(_now_live(), data, broker=broker, manual=True)
    candidate = res["artifacts"]["buy_candidates"][0]

    assert candidate["planned_amount_usd"] == 4.5
    assert res["artifacts"]["broker_outcomes"]["SMALL"]["submitted_amount_usd"] == 4.5


def test_morning_live_orders_are_blocked_before_exceeding_daily_cap(tmp_path):
    # AAA and BBB both drop 3% -> identical multiplier/drop_pct, so the
    # priority sort (multiplier DESC, drop_pct DESC, ticker ASC) places AAA
    # first. Daily cap of $6 admits AAA ($4.50) but not BBB ($4.50 more would
    # exceed the cap), so BBB is cap_exceeded rather than placed.
    cfg = MorningConfig(
        tickers=("AAA", "BBB"),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=6.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03, "BBB": 0.03})
    broker = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0, "BBB": 97.0})

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
    assert by_ticker["BBB"]["hypothetical_amount_usd"] == 4.5


def test_morning_live_ledger_seeds_cap_spend_from_earlier_same_process_day_run(tmp_path):
    # Codex review: MorningLedger is in-memory and this executor is normally
    # instantiated fresh per cron invocation, so a retried/rerun live task saw
    # an empty ledger and could re-spend the daily cap. Simulate two separate
    # "processes" (two distinct MorningDCAExecutor instances, as a real retry
    # would be) hitting the same live run-state root on the same trading day:
    # the first places AAA ($4.50 of a $6 daily cap); a fresh second instance
    # must read that already-written run_state.json and see only $1.50 of
    # headroom left, blocking BBB's $4.50 order as cap_exceeded rather than
    # wrongly re-spending the full $6.
    cfg = MorningConfig(
        tickers=("AAA",),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=6.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex1 = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0})
    res1 = ex1.run(_now_live(), data1, broker=broker1, manual=True)
    assert res1["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"

    # Fresh executor instance = fresh in-memory MorningLedger, mirroring a
    # brand-new cron process picking up where the first one left off.
    cfg2 = MorningConfig(
        tickers=("BBB",),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=6.0,
        morning_weekly_cap_usd=100.0,
        morning_run_state_root=str(tmp_path),
    )
    ex2 = MorningDCAExecutor(cfg2, mode=MORNING_LIVE_MODE)
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
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
        morning_weekly_cap_usd=6.0,
        morning_run_state_root=str(tmp_path),
    )
    ex1 = MorningDCAExecutor(cfg1, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0})
    res1 = ex1.run(monday, data1, broker=broker1, manual=True)
    assert res1["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"
    assert res1["artifacts"]["buy_candidates"][0]["planned_amount_usd"] == 4.5

    # Fresh process (fresh in-memory ledger) on Wednesday of the same week.
    cfg2 = MorningConfig(
        tickers=("BBB",),
        base_buy_amount_usd=3.0,
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=6.0,
        morning_run_state_root=str(tmp_path),
    )
    ex2 = MorningDCAExecutor(cfg2, mode=MORNING_LIVE_MODE)
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
    res2 = ex2.run(_now_live(), data2, broker=broker2, manual=True)

    # Monday already spent $4.50 of the $6 weekly cap, leaving only $1.50 of
    # weekly headroom -- not the full $6 a same-day-only seed would wrongly
    # leave available -- so BBB's $4.50 must be blocked.
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
    broker1 = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0})
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
    broker2 = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
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
    broker = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0})
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
    broker = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0})
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
    #
    # Monday's filled_orders.csv is THIS stream's own earlier-weekday
    # artifact -- since "Fail closed on unreadable own-stream prior-week
    # states", a rejected (non-finite) qualifying-row amount there is
    # recorded via csv_qualifying_notional_total's own skip_sink just like
    # any other malformed CSV, setting _cap_seed_scan_incomplete and forcing
    # the shared weekly bucket to its ceiling -- BBB is correctly BLOCKED,
    # not silently admitted against the JSON-only fallback total.
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
    broker = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
    # Must not raise/crash on the non-finite value (the original infinite-seed
    # bug this test was written against): the run completes and fails closed.
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    assert ex._cap_seed_scan_incomplete is True
    assert not broker.placed_orders, "a rejected non-finite CSV amount must fail closed, not admit BBB"
    bbb = res["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "cap_exceeded"


def test_morning_live_ledger_seed_survives_corrupt_filled_orders_csv(tmp_path):
    # Codex review: a non-UTF-8 filled_orders.csv raised UnicodeDecodeError
    # while csv.DictReader iterated it, which the seeding loop's except OSError
    # did not catch, aborting the entire scheduled retry over one corrupt
    # auxiliary fill file instead of failing closed gracefully.
    #
    # Monday's filled_orders.csv is THIS stream's own earlier-weekday
    # artifact -- since "Fail closed on unreadable own-stream prior-week
    # states", an unreadable file there sets _cap_seed_scan_incomplete and
    # forces the shared weekly bucket to its ceiling -- BBB is correctly
    # BLOCKED, not silently admitted against the JSON-only fallback total.
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
    broker = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
    # Must not raise UnicodeDecodeError; fails closed gracefully instead.
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    assert ex._cap_seed_scan_incomplete is True
    assert not broker.placed_orders, "an unreadable CSV must fail closed, not admit BBB"
    bbb = res["artifacts"]["buy_candidates"][0]
    assert bbb["status"] == "cap_exceeded"


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
    broker = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
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
        morning_weekly_cap_usd=10.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.05})
    broker = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    # $10 weekly cap: orders.csv's larger total ($8, not filled_orders.csv's
    # partial $3) plus BBB's $3.00 = $11 > $10 -- must be blocked. Stopping
    # at filled_orders.csv's $3 would leave $7 of headroom, enough to wrongly
    # admit BBB's $3.00.
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
    broker1 = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0})
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
    broker2 = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
    res2 = ex2.run(_now_live(), data2, broker=broker2, manual=False)
    assert res2["artifacts"]["broker_outcomes"]["BBB"]["status"] == "submitted"
    assert Path(res2["paths"]["run_state"]) == scheduled_path

    persisted = json.loads(scheduled_path.read_bytes())
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


def test_calibrated_first_tier_overrides_global_minimum_drop():
    # Codex review ("Let calibrated first tiers override the global floor" --
    # "the equivalent check in MorningDCAExecutor has the same problem"):
    # AAAU-style calibration with a first tier below the shared 0.1% minimum.
    # A 0.05% drop clears the calibrated 0.01% first tier (multiplier=1x) but
    # is still below the global 0.1% floor -- flooring it at the global
    # minimum would silently veto a buy the checked-in per-symbol model
    # already approved. Mirrors test_calibrated_first_tier_overrides_global_
    # minimum_drop in test_near_close_dca.py.
    cfg = MorningConfig(
        tickers=("AAAU", "AAPL"),
        per_ticker_sizing={"AAAU": {"tiers": ((0.0001, 1.0), (0.02, 1.5))}},
    )
    ex = MorningDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"AAAU": 0.0005, "AAPL": 0.0005})

    aaau_sig = ex.evaluate_signal("AAAU", _now(), data)
    assert aaau_sig.action == Action.BUY
    assert aaau_sig.multiplier == 1.0

    # Control: the SAME drop for an uncalibrated symbol must still be
    # floored at the global 0.1% minimum.
    aapl_sig = ex.evaluate_signal("AAPL", _now(), data)
    assert aapl_sig.action == Action.NO_ACTION


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


def test_load_morning_config_appends_dated_manual_override_to_configured_cap_seed_roots(
    tmp_path, monkeypatch
):
    # Codex review, PR #656 follow-up on discussion_r3839652545: "Add
    # override roots to configured cap seeds". default_pooled_cap_seed_
    # roots()'s own perplexity_primary_*/manual_override_* glob fix is
    # bypassed by every production stream config, which supplies an
    # EXPLICIT artifact_paths.cap_seed_roots list that replaces the
    # default_factory output wholesale. The loader must still append any
    # existing dated override root on top of that configured list.
    from morning_dca import load_morning_config

    cron_root = tmp_path / "cron_tracking"
    override_dir = cron_root / "perplexity_primary_20260818" / "manual_override_1555ET"
    override_dir.mkdir(parents=True)
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(cron_root))

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    configured_root = str(cron_root / "midday_dca")
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"cap_seed_roots": [configured_root]}}),
        encoding="utf-8",
    )

    cfg = load_morning_config(str(cfg_dir))

    assert configured_root in cfg.cap_seed_roots
    assert str(override_dir) in cfg.cap_seed_roots


def test_load_morning_config_respects_an_explicitly_empty_cap_seed_roots(tmp_path, monkeypatch):
    # An explicit [] deliberately disables cap seeding entirely (mirrors
    # prior_allocation_roots' own empty-list contract) -- appending the
    # override root here would silently reactivate cap seeding an operator
    # explicitly turned off.
    from morning_dca import load_morning_config

    cron_root = tmp_path / "cron_tracking"
    (cron_root / "perplexity_primary_20260818" / "manual_override_1555ET").mkdir(parents=True)
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(cron_root))

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text(
        json.dumps({"artifact_paths": {"cap_seed_roots": []}}),
        encoding="utf-8",
    )

    cfg = load_morning_config(str(cfg_dir))

    assert cfg.cap_seed_roots == ()



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
        **cast(
            Any,
            {
                **cfg.__dict__,
                "morning_run_state_root": str(state_root),
                "approved_test_dates": cast(Any, _APPROVED_DATES_LOAD_FAILED),
            },
        )
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
    on_disk = json.loads(state_path.read_bytes())
    assert on_disk["status"] != "blocked"


def test_load_morning_config_falls_back_to_defaults_if_missing(tmp_path):
    """load_morning_config gracefully falls back when file is absent."""
    from morning_dca import load_morning_config

    cfg = load_morning_config(str(tmp_path / "nonexistent"))
    defaults = MorningConfig()
    assert cfg.base_buy_amount_usd == defaults.base_buy_amount_usd
    assert cfg.morning_daily_cap_usd == defaults.morning_daily_cap_usd


def test_load_morning_config_falls_back_to_defaults_if_corrupt_utf8(tmp_path):
    from morning_dca import load_morning_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_bytes(b"\xff\xfe\xff")

    cfg = load_morning_config(str(cfg_dir))
    defaults = MorningConfig()
    assert cfg.base_buy_amount_usd == defaults.base_buy_amount_usd
    assert cfg.morning_daily_cap_usd == defaults.morning_daily_cap_usd


def test_load_morning_config_enables_model_usage_log_by_default(tmp_path):
    # load_morning_config is the "normal configuration path" real scheduled
    # runs use, so it must resolve a working model_usage_log_root by default
    # (unlike the bare MorningConfig() dataclass default of None, which
    # protects ad-hoc/test construction) -- otherwise the audit log the
    # manifest/docs promise is silently disabled in production.
    # stream_root("model_usage"), not a module constant: the loader resolves this
    # when it RUNS, so comparing against an import-time value would re-assert the
    # staleness that was just removed (Codex review, PR #219).
    from dca_paths import stream_root
    from morning_dca import load_morning_config

    # Missing config file.
    cfg = load_morning_config(str(tmp_path / "nonexistent"))
    assert cfg.model_usage_log_root == stream_root("model_usage")

    # Present but empty config file.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "morning_dca.json").write_text("{}", encoding="utf-8")
    cfg = load_morning_config(str(cfg_dir))
    assert cfg.model_usage_log_root == stream_root("model_usage")

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
    # Shared Normal-tier ceiling (near_close_dca.BUDGET_TIERS[0]), not the old
    # isolated 90.0 per-stream half-share -- see morning_dca.py's
    # DEFAULT_MORNING_DAILY_CAP_USD comment (pooling fix, 2026-08-02).
    assert ex.config.morning_daily_cap_usd == 180.0
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

    jsonschema is a hard dev dependency (requirements-dev.txt), so this is a
    real assertion in CI rather than a silent no-op.
    """
    jsonschema = pytest.importorskip("jsonschema")

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
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
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
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
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    Registry, Resource = referencing.Registry, referencing.Resource

    schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
    main = json.loads((schemas_dir / "morning_preflight.schema.json").read_bytes())
    resources = [
        (name, Resource.from_contents(
            json.loads((schemas_dir / name).read_bytes())))
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


def test_load_same_day_preflight_treats_non_object_json_as_unavailable(tmp_path):
    """Codex review: "Treat non-object preflight JSON as unavailable". Valid
    JSON whose root is a list, scalar, or null (e.g. after a partial
    recovery) has no .get() method; the function used to call state.get(...)
    immediately after decoding without checking this, so it raised instead
    of returning None. A crash here means the 10:30 run never reaches its
    own non-blocking fallback and the documented "None means no pre-flight
    found, degrade to full universe with a warning" contract is broken."""
    trading_day = date(2026, 7, 8)
    run_dir = tmp_path / f"{trading_day.strftime('%Y%m%d')}_0945ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text("[]", encoding="utf-8")

    assert load_same_day_preflight(str(tmp_path), trading_day, bounded_recovery=False) is None

    (run_dir / "run_state.json").write_text("null", encoding="utf-8")
    assert load_same_day_preflight(str(tmp_path), trading_day, bounded_recovery=False) is None

    (run_dir / "run_state.json").write_text('"a string"', encoding="utf-8")
    assert load_same_day_preflight(str(tmp_path), trading_day, bounded_recovery=False) is None


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
                "run_type": "morning_dca_preflight",
                "state_schema_version": "morning-preflight-v2-machine-complete",
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
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "run_type": "morning_dca_preflight",
                "state_schema_version": "morning-preflight-v2-machine-complete",
            },
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


@pytest.mark.parametrize("alias_kind", ["symlink", "parent_segment"])
def test_forbidden_preflight_root_canonicalizes_aliases(tmp_path, alias_kind):
    forbidden_root = tmp_path / "live" / "morning_dca"
    forbidden_root.mkdir(parents=True)
    if alias_kind == "symlink":
        alias = tmp_path / "preflight_alias"
        alias.symlink_to(forbidden_root, target_is_directory=True)
    else:
        alias_parent = forbidden_root / "discarded"
        alias_parent.mkdir()
        alias = alias_parent / ".."

    reason = _forbidden_preflight_root_reason(str(alias), (str(forbidden_root),))

    assert reason is not None
    assert "forbidden" in reason


def test_load_same_day_preflight_rejects_artifact_symlink_into_forbidden_root(tmp_path):
    allowed_root = tmp_path / "preflight"
    allowed_run_dir = allowed_root / "20260708_0945ET"
    allowed_run_dir.mkdir(parents=True)
    forbidden_root = tmp_path / "live" / "morning_dca"
    forbidden_root.mkdir(parents=True)
    forbidden_state = forbidden_root / "run_state.json"
    forbidden_state.write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "eligible_to_execute_tickers": ["SMALL"],
            "per_ticker_eligibility": [{"symbol": "SMALL", "eligible_to_execute": True}],
            "run_classification": {
                "stream": "morning",
                "run_type": "morning_dca_preflight",
                "manual_run": False,
                "state_schema_version": "morning-preflight-v2-machine-complete",
            },
        }),
        encoding="utf-8",
    )
    (allowed_run_dir / "run_state.json").symlink_to(forbidden_state)

    loaded = load_same_day_preflight(
        str(allowed_root),
        date(2026, 7, 8),
        forbidden_roots=(str(forbidden_root),),
    )
    assert loaded is None


def test_load_same_day_preflight_bounded_recovery_handles_oserror(tmp_path, monkeypatch):
    root = tmp_path / "preflight"
    root.mkdir()

    def _raising_iterdir(_self):
        raise OSError("Simulated disk error or permission error")

    monkeypatch.setattr(Path, "iterdir", _raising_iterdir)

    assert load_same_day_preflight(str(root), date(2026, 7, 8), bounded_recovery=True) is None


def test_load_same_day_preflight_rejects_artifact_symlink_outside_preflight_root(tmp_path):
    allowed_root = tmp_path / "preflight"
    allowed_run_dir = allowed_root / "20260708_0945ET"
    allowed_run_dir.mkdir(parents=True)
    external_dir = tmp_path / "somewhere_else"
    external_dir.mkdir(parents=True)
    external_state = external_dir / "run_state.json"
    external_state.write_text(
        json.dumps({
            "run_id": "morning_preflight_20260708",
            "trading_day": "2026-07-08",
            "overall_ok": True,
            "eligible_to_execute_tickers": ["SMALL"],
            "per_ticker_eligibility": [{"symbol": "SMALL", "eligible_to_execute": True}],
            "run_classification": {
                "stream": "morning",
                "run_type": "morning_dca_preflight",
                "manual_run": False,
                "state_schema_version": "morning-preflight-v2-machine-complete",
            },
        }),
        encoding="utf-8",
    )
    (allowed_run_dir / "run_state.json").symlink_to(external_state)

    loaded = load_same_day_preflight(
        str(allowed_root),
        date(2026, 7, 8),
    )
    assert loaded is None


@pytest.mark.parametrize(
    "raw_filename",
    [
        "..",
        ".",
        "foo/..",
        "foo/.",
        "/",
        "\\",
        "",
        None,
        "raw",
        "orders.csv",
        "RAW",
        "Orders.csv",
        "buy_candidates.csv",
        "run_state.json",
        "submitted_orders.csv",
        "../..",
        "../foo/..",
    ],
)
def test_sanitize_budget_ledger_filename_rejects_parent_and_reserved_names(raw_filename):
    assert _sanitize_budget_ledger_filename(raw_filename) == "budget_ledger.json"


def test_write_artifacts_rejects_parent_directory_ledger_filename(tmp_path):
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "out"),
        budget_ledger_filename="..",
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    artifacts = ex.build_artifacts(_now_live(), [], "none", manual=False)
    artifacts["budget"] = {"daily_cap_usd": 180.0, "morning_spend_usd": 0.0}

    paths = ex.write_artifacts(artifacts, _now_live())

    assert "budget_ledger" in paths
    ledger_path = Path(paths["budget_ledger"])
    assert ledger_path.parent == ex.run_state_dir(_now_live())
    assert ledger_path.name == "budget_ledger.json"
    assert ledger_path.exists()


@pytest.mark.parametrize(
    "bad_name",
    [
        "raw",
        "orders.csv",
        "RAW",
        "Orders.csv",
        "submitted_orders.csv",
        "buy_candidates.csv",
        "run_summary.json",
        "drift_report.json",
    ],
)
def test_write_artifacts_rejects_run_owned_destinations(tmp_path, bad_name):
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "out"),
        budget_ledger_filename=bad_name,
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    artifacts = ex.build_artifacts(_now_live(), [], "none", manual=False)
    artifacts["budget"] = {"daily_cap_usd": 180.0, "morning_spend_usd": 0.0}

    paths = ex.write_artifacts(artifacts, _now_live())

    assert "budget_ledger" in paths
    ledger_path = Path(paths["budget_ledger"])
    assert ledger_path.parent == ex.run_state_dir(_now_live())
    assert ledger_path.name == "budget_ledger.json"
    assert ledger_path.exists()


def test_write_artifacts_sanitizes_budget_ledger_filename(tmp_path):
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "out"),
        budget_ledger_filename="../../escaped_ledger.json",
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    artifacts = ex.build_artifacts(_now_live(), [], "none", manual=False)
    artifacts["budget"] = {"daily_cap_usd": 180.0, "morning_spend_usd": 0.0}

    paths = ex.write_artifacts(artifacts, _now_live())

    assert "budget_ledger" in paths
    ledger_path = Path(paths["budget_ledger"])
    assert ledger_path.parent == ex.run_state_dir(_now_live())
    assert ledger_path.name == "escaped_ledger.json"
    assert ledger_path.exists()


def test_maybe_block_for_holiday_writes_budget_ledger(tmp_path):
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "out"),
        budget_ledger_filename="budget_ledger.json",
        approved_test_dates=frozenset(),
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    weekend_now = datetime(2026, 7, 11, 10, 30, tzinfo=EASTERN)
    res = ex._maybe_block_for_holiday(weekend_now, manual=False)

    assert res is not None
    assert "budget_ledger" in res["paths"]
    ledger_path = Path(res["paths"]["budget_ledger"])
    assert ledger_path.exists()
    assert ledger_path.name == "budget_ledger.json"


def test_maybe_block_for_holiday_seeds_prior_spend_in_budget_ledger(tmp_path):
    # Set up earlier spend on Monday 2026-07-06 ($50)
    state_root = tmp_path / "out"
    prior_dir = state_root / "20260706_1030ET"
    prior_dir.mkdir(parents=True)
    prior_state = {
        "run_id": "morning_live_20260706_1030ET",
        "trading_day": "2026-07-06",
        "broker_outcomes": {
            "SMALL": {"status": "submitted", "submitted_amount_usd": 50.0, "order_id": "ord-1"}
        },
        "buy_candidates": [
            {
                "symbol": "SMALL",
                "planned_amount_usd": 50.0,
                "status": "submitted",
            }
        ],
        "budget": {"daily_cap_usd": 180.0, "weekly_spent_usd": 50.0, "weekly_cap_usd": 900.0},
    }
    (prior_dir / "run_state.json").write_text(json.dumps(prior_state), encoding="utf-8")

    # Run blocked holiday/weekend check on Saturday 2026-07-11
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(state_root),
        budget_ledger_filename="budget_ledger.json",
        approved_test_dates=frozenset(),
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    weekend_now = datetime(2026, 7, 11, 10, 30, tzinfo=EASTERN)
    res = ex._maybe_block_for_holiday(weekend_now, manual=False)

    assert res is not None
    assert "budget_ledger" in res["paths"]
    ledger_data = json.loads(Path(res["paths"]["budget_ledger"]).read_bytes())
    assert ledger_data["hypothetical_week_spend_usd"] == 50.0
    assert ledger_data["remaining_weekly_usd"] == 850.0


def test_maybe_block_for_holiday_seeds_same_day_pooled_spend(tmp_path):
    # Set up sibling market-open stream running at 9:35 AM on Wednesday 2026-07-08 with $30 spend
    sibling_root = tmp_path / "market_open"
    sibling_dir = sibling_root / "20260708_0935ET"
    sibling_dir.mkdir(parents=True)
    sibling_state = {
        "run_id": "market_open_live_20260708_0935ET",
        "trading_day": "2026-07-08",
        "generated_at": "2026-07-08T09:35:05-04:00",
        "broker_outcomes": {
            "SMALL": {"status": "submitted", "submitted_amount_usd": 30.0, "order_id": "ord-mo-1"}
        },
        "buy_candidates": [
            {
                "ticker": "SMALL",
                "planned_amount_usd": 30.0,
                "status": "submitted",
            }
        ],
        "budget": {"daily_cap_usd": 180.0, "weekly_spent_usd": 30.0, "weekly_cap_usd": 900.0},
    }
    (sibling_dir / "run_state.json").write_text(json.dumps(sibling_state), encoding="utf-8")

    state_root = tmp_path / "morning"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(state_root),
        cap_seed_roots=(str(sibling_root), str(state_root)),
        budget_ledger_filename="budget_ledger.json",
        approved_test_dates=frozenset({date(2026, 7, 7)}),  # 2026-07-08 is outside approved window
    )
    ex = MorningDCAExecutor(cfg, mode="live")
    # Invocation at 10:30 ET on the same day 2026-07-08
    blocked_now = datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN)
    res = ex._maybe_block_for_holiday(blocked_now, manual=False)

    assert res is not None
    assert "budget_ledger" in res["paths"]
    ledger_data = json.loads(Path(res["paths"]["budget_ledger"]).read_bytes())
    assert ledger_data["hypothetical_day_spend_usd"] == 30.0
    assert ledger_data["remaining_daily_usd"] == 150.0
    assert ledger_data["hypothetical_week_spend_usd"] == 30.0
    assert ledger_data["remaining_weekly_usd"] == 870.0


def test_live_gate_degrades_to_full_universe_when_preflight_found_but_empty(tmp_path):
    """Codex review: "Keep valid empty preflights non-blocking". A VALID
    preflight that found zero eligible tickers (e.g. nothing was below its
    prior close at 9:45) must not be treated as authoritative -- it is
    exactly as coarse/stale-by-10:30 as a missing artifact, and the 10:30
    slot's own signal/budget/spread/trend/top-up checks are the real safety
    gates. A quiet preflight must not be MORE restrictive than a missing
    one, which already degrades to the full universe."""
    preflight_root = tmp_path / "preflight"
    pf = MorningDCAExecutor(
        MorningConfig(tickers=("BIG", "FLAT"), morning_run_state_root=str(preflight_root)),
        mode=MORNING_PREFLIGHT_MODE,
    )
    # Nothing down at 9:45: preflight legitimately finds zero eligible tickers.
    pf_data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.0, "FLAT": 0.0})
    pf.run(_now_preflight(), pf_data)

    live = MorningDCAExecutor(
        MorningConfig(
            tickers=("BIG", "FLAT"),
            morning_run_state_root=str(tmp_path / "live"),
            preflight_run_state_root=str(preflight_root),
        ),
        mode=MORNING_LIVE_MODE,
    )
    # A dip has since appeared by 10:30 -- the full universe must still be
    # evaluated, not silently zeroed out by the stale, empty preflight.
    live_data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10, "FLAT": 0.10})
    res = live.run(_now_live(), live_data, broker=FakeLiveBroker())
    art = res["artifacts"]
    gate = art["preflight_gate"]
    assert gate["preflight_found"] is True
    assert "zero eligible" in gate["warning"]
    evaluated = {r["ticker"] for r in art["signal_rows"]}
    assert evaluated == {"BIG", "FLAT"}


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


def test_live_csv_artifacts_include_gated_out_ticker_rows(tmp_path):
    preflight_root = tmp_path / "preflight"
    pf = MorningDCAExecutor(
        MorningConfig(tickers=("BIG", "FLAT"), morning_run_state_root=str(preflight_root)),
        mode=MORNING_PREFLIGHT_MODE,
    )
    pf.run(
        _now_preflight(),
        FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.03, "FLAT": 0.0}),
    )

    live = MorningDCAExecutor(
        MorningConfig(
            tickers=("BIG", "FLAT"),
            morning_run_state_root=str(tmp_path / "live"),
            preflight_run_state_root=str(preflight_root),
        ),
        mode=MORNING_LIVE_MODE,
    )
    res = live.run(
        _now_live(),
        DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10, "FLAT": 0.10}),
        broker=FakeLiveBroker(),
    )

    run_dir = Path(res["paths"]["run_state"]).parent
    assert not (run_dir / "validation.csv").exists()
    with (run_dir / "morning_signals.csv").open(newline="", encoding="utf-8") as handle:
        rows = {row["ticker"]: row for row in csv.DictReader(handle)}
    assert set(rows) == {"BIG", "FLAT"}
    assert rows["BIG"]["auto_buy_signal"] == "True"
    assert rows["FLAT"]["action"] == Action.SKIP.value
    assert rows["FLAT"]["reason"] == "preflight_gate_gated_out"
    assert rows["FLAT"]["auto_buy_signal"] == "False"


def test_live_gate_degrades_to_full_universe_when_preflight_missing_scheduled(tmp_path):
    # NON-BLOCKING as of 2026-08-02 (Geoff: "should not block any later
    # runs"), superseding the old fail-closed contract (Codex review #77):
    # a SCHEDULED live run with no same-day preflight artifact now degrades
    # to the full configured universe with a warning, exactly like a manual
    # run (see test_live_gate_degrades_to_full_universe_when_preflight_missing_manual
    # below), and still writes a normal (not "blocked") durable run_state so
    # watchdog/reporting see a real completed scheduled-run attempt.
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
    res = live.run(_now_live(), data, broker=FakeLiveBroker())
    art = res["artifacts"]
    assert art.get("status") != "blocked"
    assert art["preflight_gate"]["preflight_found"] is False
    assert "pre-flight" in art["preflight_gate"]["warning"]
    evaluated = {r["ticker"] for r in art["signal_rows"]}
    assert evaluated == {"BIG", "FLAT"}
    assert "run_id" in art
    assert "generated_at" in art
    assert "counts" in art
    assert "run_classification" in art
    state_path = Path(res["paths"]["run_state"])
    assert state_path.exists()
    persisted = json.loads(state_path.read_bytes())
    assert persisted.get("status") != "blocked"
    assert persisted["trades"] is True
    assert persisted["run_id"] == art["run_id"]
    # Latest pointer must be updated for scheduled runs.
    pointer_key = live.latest_pointer_name.replace(".json", "")
    assert pointer_key in res["paths"]

    for key in ("run_id", "generated_at", "counts", "run_classification"):
        assert key in persisted, f"run_state is missing {key!r}"
    assert persisted["run_classification"]["run_origin"] == "scheduled"
    assert persisted["run_classification"]["manual_run"] is False
    pointer_path = Path(state_root) / "latest_morning_run.json"
    assert pointer_path.exists()
    pointer = json.loads(pointer_path.read_bytes())
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
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    cfg = MorningConfig(tickers=("BIG",))
    rc = MorningDCAExecutor(cfg, mode=MORNING_PREFLIGHT_MODE).run_classification(
        _now_preflight(), manual=False
    )
    assert rc["run_type"] == "morning_dca_preflight"
    assert rc["cron_id"] == "bb4f6ef1"
    assert rc["state_schema_version"] == "morning-preflight-v2-machine-complete"
    assert rc["run_id"] == "morning_preflight_20260708_0945ET"
    assert set(schema["required"]).issubset(rc.keys())
    jsonschema = pytest.importorskip("jsonschema")
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
                "run_type": "morning_dca_preflight",
                "state_schema_version": "morning-preflight-v2-machine-complete",
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
        pointer = json.loads((live_root / name).read_bytes())
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
    on_disk = json.loads(run_state_path.read_bytes())
    assert on_disk["buy_candidates"][0]["status"] == "submitted"
    for name in ("latest_morning_run.json", "latest_morning_live.json"):
        pointer = json.loads((live_root / name).read_bytes())
        assert pointer["trading_day"] == "2026-07-08"
        assert pointer["valid_scheduled_evidence"] is True
        assert pointer["run_state_path"] == str(run_state_path)


def test_outside_window_retry_preserves_pending_and_unconfirmed_evidence(tmp_path):
    # PR #631 review discussion_r3914248608: _existing_submitted_run_state()
    # (the helper the is_outside_window/is_outside_time_window gate above
    # calls) must admit an artifact on ANY order-bearing status -- not just
    # the literal "submitted" a fresh order starts at -- because a broker
    # refresh between the original attempt and this date/time-gated retry can
    # advance the row to "filled", or leave it at the still-live "pending"/
    # "unconfirmed" acceptance a not-yet-terminal order carries. Restricting
    # the gate to "submitted" exactly would fall through to the "no existing
    # evidence" branch below and overwrite the fixed slot's real order with a
    # zero-order blocked stub, erasing it from every later same-day
    # cap/top-up scan (dca_risk.DEPLOYED_CANDIDATE_STATUSES is the canonical
    # set this helper's own _ORDER_BEARING_CANDIDATE_STATUSES mirrors).
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

    # Simulate a broker refresh reconciling the order to "pending" (still
    # live at the venue, not yet a terminal fill) between the original
    # attempt and the retry below.
    on_disk_first = json.loads(run_state_path.read_bytes())
    on_disk_first["buy_candidates"][0]["status"] = "pending"
    on_disk_first["broker_outcomes"][on_disk_first["buy_candidates"][0]["ticker"]]["status"] = "pending"
    run_state_path.write_text(json.dumps(on_disk_first, indent=2, sort_keys=True))

    # Retry at the same slot with a config that now excludes today -> hits the
    # is_outside_window gate. Must preserve the "pending" evidence rather
    # than overwriting it with a fresh zero-order blocked stub.
    retry_cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 7)}),  # no longer approved
    )
    retry_ex = MorningDCAExecutor(retry_cfg, mode=MORNING_LIVE_MODE)
    second = retry_ex.run(_now_live() + timedelta(minutes=1), data, broker=FakeLiveBroker())

    assert second["artifacts"]["buy_candidates"][0]["status"] == "pending"
    on_disk = json.loads(run_state_path.read_bytes())
    assert on_disk["buy_candidates"][0]["status"] == "pending"
    for name in ("latest_morning_run.json", "latest_morning_live.json"):
        pointer = json.loads((live_root / name).read_bytes())
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


def test_scheduled_live_run_writing_run_state_then_failing_is_a_late_error(tmp_path):
    # guarded_run's run_artifact_written probe was never wired into this
    # scheduled call site (the afternoon run_dca.py call already had it): a
    # run that persisted run_state.json and only then failed writing
    # morning_signals.csv/submitted_orders.csv/a same-day pointer was
    # unconditionally recorded as failure_phase='before_run_artifact',
    # fabricating an earlier failed start plus a successful retry -- codex
    # review discussion_r3660858010.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()
    run_state_path = ex.run_state_dir(now) / "run_state.json"

    def _boom(*args, **kwargs):
        run_state_path.parent.mkdir(parents=True, exist_ok=True)
        run_state_path.write_text('{"run_id": "morning_live_written_then_crashed"}', encoding="utf-8")
        raise RuntimeError("crashed after writing run_state.json")

    ex._run_impl = _boom
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="crashed after writing run_state.json"):
        ex.run(now, data, manual=False, broker=FakeLiveBroker())

    attempt_log_root = Path(os.environ["DCA_ATTEMPT_LOG_ROOT"])
    marker_path = next(attempt_log_root.glob("*/morning.json"))
    marker = json.loads(marker_path.read_bytes())
    assert marker["failure_phase"] == "after_run_artifact"


def test_freshness_probe_ignores_a_write_made_while_waiting_for_the_attempt_lock(
    tmp_path, monkeypatch
):
    # Codex review discussion_r3849268549 ("Snapshot artifact freshness after
    # acquiring the attempt lock"): when two scheduled attempts overlap, the
    # waiter used to snapshot run_state.json BEFORE blocking on
    # .scheduled_attempt.lock, so the HOLDER's write while the waiter was
    # blocked later satisfied artifact_freshness_probe() and was mistaken for
    # the waiter's own. A waiter that then failed before publishing anything
    # had guarded_run() record failure_phase='after_run_artifact', and
    # reporting suppressed a genuine failed-start/retry event. The snapshot is
    # taken INSIDE the lock now, so a foreign write that lands while waiting
    # cannot satisfy it.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()
    run_state_path = ex.run_state_dir(now) / "run_state.json"

    @contextlib.contextmanager
    def _lock_held_by_a_concurrent_attempt(path):
        # Stands in for the wait: by the time this call gets the lock, the
        # attempt that held it has already published this slot's run_state.json.
        run_state_path.parent.mkdir(parents=True, exist_ok=True)
        run_state_path.write_text(
            '{"run_id": "morning_live_written_by_the_other_attempt"}', encoding="utf-8"
        )
        yield

    monkeypatch.setattr(morning_dca, "cross_process_lock", _lock_held_by_a_concurrent_attempt)

    def _boom(*args, **kwargs):
        # This attempt publishes NOTHING of its own before failing.
        raise RuntimeError("crashed without writing run_state.json")

    ex._run_impl = _boom
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="crashed without writing run_state.json"):
        ex.run(now, data, manual=False, broker=FakeLiveBroker())

    attempt_log_root = Path(os.environ["DCA_ATTEMPT_LOG_ROOT"])
    marker_path = next(attempt_log_root.glob("*/morning.json"))
    marker = json.loads(marker_path.read_bytes())
    assert marker["failure_phase"] == "before_run_artifact"


def test_freshness_probe_is_cleared_when_the_attempt_lock_times_out(tmp_path, monkeypatch):
    # Codex review ("Clear the pre-lock freshness probe on lock timeout"):
    # cross_process_lock raises TimeoutError after 30s rather than yielding, so
    # a waiter that times out never enters the block and never reaches the
    # in-lock re-snapshot. Leaving the pre-lock probe installed let the HOLDER's
    # run_state.json write satisfy it, so guarded_run() recorded
    # failure_phase='after_run_artifact' for a waiter that published nothing.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()
    run_state_path = ex.run_state_dir(now) / "run_state.json"

    def _held_until_timeout(path, **kwargs):
        # The holder publishes the slot's run_state.json, then this waiter
        # exhausts its window without ever acquiring the lock.
        run_state_path.parent.mkdir(parents=True, exist_ok=True)
        run_state_path.write_text(
            '{"run_id": "morning_live_written_by_the_other_attempt"}', encoding="utf-8"
        )
        raise TimeoutError("could not acquire flock within 30s")

    monkeypatch.setattr(morning_dca, "cross_process_lock", _held_until_timeout)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("the guarded body must not run when the lock times out")

    ex._run_impl = _must_not_run
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(TimeoutError):
        ex.run(now, data, manual=False, broker=FakeLiveBroker())

    attempt_log_root = Path(os.environ["DCA_ATTEMPT_LOG_ROOT"])
    marker_path = next(attempt_log_root.glob("*/morning.json"))
    marker = json.loads(marker_path.read_bytes())
    assert marker["failure_phase"] == "before_run_artifact"


def test_lock_timeout_does_not_publish_into_the_active_slot(tmp_path, monkeypatch):
    # Codex review ("Keep lock-timeout artifacts out of the active slot"): the
    # except handler around guarded_run writes the full startup-failure
    # artifact set OUTSIDE the critical section. A waiter that exhausted
    # cross_process_lock's 30s window failed precisely because the holder is
    # still evaluating/placing, so publishing a zero-order failure run_state
    # into that shared slot exposes incomplete evidence and reintroduces the
    # last-writer-wins race the lock exists to prevent.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()
    run_state_dir = ex.run_state_dir(now)

    def _held_until_timeout(path, **kwargs):
        raise TimeoutError("could not acquire flock within 30s")

    monkeypatch.setattr(morning_dca, "cross_process_lock", _held_until_timeout)

    wrote = []
    monkeypatch.setattr(
        MorningDCAExecutor,
        "_write_startup_failure_artifacts",
        lambda self, now_, exc: wrote.append(exc),
    )
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(TimeoutError):
        ex.run(now, data, manual=False, broker=FakeLiveBroker())

    assert wrote == []  # the holder publishes this slot, not this waiter
    assert not (run_state_dir / "run_state.json").exists()
    # The failure is still recorded where a run that published nothing belongs.
    attempt_log_root = Path(os.environ["DCA_ATTEMPT_LOG_ROOT"])
    marker = json.loads(next(attempt_log_root.glob("*/morning.json")).read_bytes())
    assert marker["failure_phase"] == "before_run_artifact"


def test_a_scheduled_failure_that_owns_the_slot_still_publishes_artifacts(tmp_path, monkeypatch):
    # Companion guard: the ownership check must not suppress the startup-failure
    # artifact set for the case it was built for -- a run that acquired the slot
    # and then raised before reaching any writer.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()

    wrote = []
    monkeypatch.setattr(
        MorningDCAExecutor,
        "_write_startup_failure_artifacts",
        lambda self, now_, exc: wrote.append(exc),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("evaluate() raised before any writer")

    ex._run_impl = _boom
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="evaluate\\(\\) raised before any writer"):
        ex.run(now, data, manual=False, broker=FakeLiveBroker())

    assert len(wrote) == 1


def test_an_owned_slot_failure_publishes_before_releasing_the_lock(tmp_path, monkeypatch):
    # Codex review ("Publish owned-slot failures before releasing the lock"):
    # skipping the write for a waiter that never owned the slot fixed only half
    # the race. When _invoke() raised for an attempt that DID own the slot, the
    # `with cross_process_lock(...)` block unwound before control reached the
    # outer handler around guarded_run, so the lock was already free when the
    # zero-order failure artifacts were written. A retry polling every 20ms
    # takes the slot the instant it frees -- and guarded_run's own failure path
    # spends seconds on alert_failure()/send_heartbeat() network calls first --
    # so the failed attempt wrote into a slot another run was already
    # evaluating or placing for. Publication has to happen inside the original
    # lock scope.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()
    real_lock = morning_dca.cross_process_lock

    published_while_held = []

    @contextlib.contextmanager
    def _observing_lock(path):
        with real_lock(path):
            try:
                yield
            finally:
                # Still inside the real lock: whatever this attempt published
                # for the slot it owned must already be on disk by now.
                published_while_held.append(list(wrote))

    monkeypatch.setattr(morning_dca, "cross_process_lock", _observing_lock)

    wrote: list = []
    monkeypatch.setattr(
        MorningDCAExecutor,
        "_write_startup_failure_artifacts",
        lambda self, now_, exc: wrote.append(exc),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("evaluate() raised before any writer")

    ex._run_impl = _boom
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="evaluate\\(\\) raised before any writer"):
        ex.run(now, data, manual=False, broker=FakeLiveBroker())

    assert len(wrote) == 1, "an attempt that owned the slot must still publish"
    assert published_while_held and len(published_while_held[0]) == 1, (
        "the startup-failure artifact set must be published INSIDE the slot "
        "lock, not after the `with` block releases it"
    )


def test_an_owned_slot_failure_still_records_a_failed_start(tmp_path, monkeypatch):
    # Publishing inside the lock happens BEFORE guarded_run probes freshness,
    # and the failure artifact set writes a run_state.json of its own. Without
    # latching the freshness answer at the moment of failure, every scheduled
    # failure would record failure_phase='after_run_artifact' -- the failure
    # record is evidence the slot fired and crashed, never the run's own
    # completed work product.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()

    def _boom(*args, **kwargs):
        raise RuntimeError("crashed without writing run_state.json")

    ex._run_impl = _boom
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="crashed without writing run_state.json"):
        ex.run(now, data, manual=False, broker=FakeLiveBroker())

    # The real writer ran (this is not a stubbed executor), so run_state.json
    # exists -- but it is the FAILURE record, not this run's own work.
    assert (ex.run_state_dir(now) / "run_state.json").exists()
    attempt_log_root = Path(os.environ["DCA_ATTEMPT_LOG_ROOT"])
    marker = json.loads(next(attempt_log_root.glob("*/morning.json")).read_bytes())
    assert marker["failure_phase"] == "before_run_artifact"


def test_raw_sequence_numbers_come_from_existing_suffixes_not_a_file_count(tmp_path):
    # Codex review ("Allocate raw sequence numbers from existing suffixes"):
    # _RawInputRecorder.persist() derived the next sequence from
    # len(glob("*.json")), which reuses a number as soon as the numbering has a
    # gap -- a persist() whose save_raw_input() raised advances _seq without
    # leaving a file, and an operator can prune a raw response by hand. A
    # directory holding _1 and _3 counted two and wrote _3 again, silently
    # overwriting the earlier raw evidence for a different kind/key.
    import morning_dca as _md

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "quote_AAA_1.json").write_text("{}", encoding="utf-8")
    (raw_dir / "quote_BBB_3.json").write_text('{"original": true}', encoding="utf-8")

    recorder = _md._RawInputRecorder(raw_dir, "2026-07-08")
    recorder.persist("quote", "CCC", {"new": True})

    assert recorder.error is None
    assert recorder.saved_filenames == ["quote_CCC_4.json"]
    # The pre-existing _3 file is untouched.
    assert json.loads((raw_dir / "quote_BBB_3.json").read_bytes()) == {"original": True}
    assert (raw_dir / "quote_CCC_4.json").exists()


def test_raw_sequence_ignores_non_numeric_suffixes(tmp_path):
    # Files that were not written by this recorder (no trailing _<n>) must not
    # derail allocation -- the suffix scan skips them rather than raising.
    import morning_dca as _md

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (raw_dir / "quote_AAA_2.json").write_text("{}", encoding="utf-8")

    recorder = _md._RawInputRecorder(raw_dir, "2026-07-08")
    recorder.persist("quote", "DDD", {"new": True})

    assert recorder.error is None
    assert recorder.saved_filenames == ["quote_DDD_3.json"]


def test_concurrent_raw_recorders_never_allocate_the_same_sequence_number(tmp_path):
    """Two overlapping scheduler attempts for the same dated slot must never
    collide on a sequence number and silently overwrite each other's evidence.

    Codex review, PR #688 discussion_r3917126971 ("Allocate raw sequence
    numbers atomically"): two concurrent ``_RawInputRecorder`` instances for
    the SAME raw_dir used to both scan the directory, both see the same max
    ``_seq``, and both write, e.g., ``prior_close_AAPL_1.json`` -- the second
    write silently destroying the first attempt's own audit evidence.
    ``persist()`` now reserves its sequence number under
    ``scripts.file_lock.cross_process_lock`` (a real file lock, held for the
    scan-then-write together), so two recorders hammering the same directory
    from separate threads must still produce as many DISTINCT, non-colliding
    filenames as calls made -- never a silent overwrite.
    """
    import concurrent.futures

    import morning_dca as _md

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    recorder_a = _md._RawInputRecorder(raw_dir, "2026-07-08")
    recorder_b = _md._RawInputRecorder(raw_dir, "2026-07-08")

    def _persist(recorder, key):
        return recorder.persist("quote", key, {"key": key})

    # Interleave writes from both recorders across a real thread pool so the
    # lock is actually contended, not just called twice sequentially.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for i in range(10):
            futures.append(pool.submit(_persist, recorder_a, f"A{i}"))
            futures.append(pool.submit(_persist, recorder_b, f"B{i}"))
        for future in futures:
            future.result()

    assert recorder_a.error is None
    assert recorder_b.error is None
    all_filenames = recorder_a.saved_filenames + recorder_b.saved_filenames
    # 20 writes must produce 20 DISTINCT filenames -- a collision would mean
    # two writes share a filename and one silently clobbered the other.
    assert len(all_filenames) == 20
    assert len(set(all_filenames)) == 20
    # Every file that was reported saved must actually exist on disk with its
    # own content -- proving no write silently overwrote another's.
    on_disk = {p.name for p in raw_dir.glob("*.json")}
    assert set(all_filenames) <= on_disk


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
    assert sm["hypothetical_amount_usd"] == 3.0  # confirms this exercises the fix's target path
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
    written = json.loads(Path(res["paths"]["run_state"]).read_bytes())
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
    pointer = json.loads(pointer_path.read_bytes())
    assert pointer["run_id"] == written["run_id"]


def test_scheduled_live_run_with_wrong_broker_account_fails_closed_before_ordering(tmp_path):
    class _WrongAccountBroker(FakeLiveBroker):
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
        target_account_number="999000111",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _WrongAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(_now_live(), data, broker=broker)

    assert res["artifacts"]["status"] == "blocked"
    assert res["artifacts"]["block_reason"] == (
        "broker account mismatch: expected 999000111, got not-the-approved-account"
    )
    assert broker.placed is False
    assert Path(res["paths"]["run_state"]).exists()
    # Same normal-artifact-pipeline requirement as the missing-broker case above.
    assert res["artifacts"]["run_id"]
    assert res["artifacts"]["generated_at"]
    assert res["artifacts"]["counts"]["evaluated"] == 1
    assert "latest_morning_run" in res["paths"]
    assert Path(res["paths"]["latest_morning_run"]).exists()


class _AnyAccountBroker(FakeLiveBroker):
    """Broker whose account_number() must never be trusted to pin a live run."""

    def __init__(self, account="whatever-account-the-adapter-was-wired-to"):
        self._account = account
        self.placed = False

    def account_number(self):
        return self._account

    def available_buying_power(self):
        raise AssertionError("buying power should not be checked for an unpinned account")

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.placed = True
        raise AssertionError("orders must not be placed for an unpinned account")


@pytest.mark.parametrize(
    "unpinned",
    ["", None, "UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"],
    ids=["empty", "none", "sentinel"],
)
def test_scheduled_live_run_without_a_pinned_account_fails_closed(tmp_path, unpinned):
    """An unset target_account_number must BLOCK, not silently skip the check.

    The guard used to read ``if expected_account and actual != expected``, so a
    falsy configured account short-circuited the comparison and the run placed
    orders against whatever account the adapter happened to hold. Both live
    execution paths now share near_close_dca.account_pinning_failure, which
    treats empty/None/sentinel as unpinnable.
    """
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        target_account_number=unpinned,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _AnyAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(_now_live(), data, broker=broker)

    assert res["artifacts"]["status"] == "blocked"
    assert res["artifacts"]["block_reason"].startswith("live account pinning unavailable:")
    assert broker.placed is False
    # Fail-closed still writes a durable, complete artifact like every other
    # scheduler-wiring block.
    assert Path(res["paths"]["run_state"]).exists()
    assert res["artifacts"]["run_id"]
    assert res["artifacts"]["counts"]["evaluated"] == 1


@pytest.mark.parametrize(
    "unpinned",
    ["", None, "UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"],
    ids=["empty", "none", "sentinel"],
)
def test_manual_live_run_without_a_pinned_account_raises(tmp_path, unpinned):
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        target_account_number=unpinned,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _AnyAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="live account pinning unavailable"):
        ex.run(_now_live(), data, broker=broker, manual=True)

    assert broker.placed is False


def test_manual_live_run_with_wrong_broker_account_raises_before_ordering(tmp_path):
    import pytest

    class _WrongAccountBroker(FakeLiveBroker):
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
        target_account_number="999000111",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _WrongAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="broker account mismatch: expected 999000111, got not-the-approved-account"):
        ex.run(_now_live(), data, manual=True, broker=broker)

    assert broker.buying_power_checked is False
    assert broker.placed is False


def test_scheduled_live_run_with_broker_account_number_error_fails_closed(tmp_path):
    # broker.account_number() can raise (misconfigured/uninitialized adapter,
    # connector network/API error). Left uncaught this crashes the process
    # with a raw traceback instead of producing the durable run_state.json a
    # scheduled retry depends on.
    class _RaisingAccountBroker(FakeLiveBroker):
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
        target_account_number="999000111",
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

    class _RaisingAccountBroker(FakeLiveBroker):
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
        target_account_number="999000111",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _RaisingAccountBroker()
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="Failed to retrieve broker account number"):
        ex.run(_now_live(), data, manual=True, broker=broker)

    assert broker.placed is False


def test_blocked_live_retry_preserves_prior_order_bearing_evidence(tmp_path):
    # Codex/Gemini review on PR #46: a scheduled retry that hits a
    # scheduler-wiring safety failure (missing broker, wrong account) must not
    # overwrite an EARLIER scheduled attempt's run_state.json for the same slot
    # if that state already has real submitted-order evidence -- that evidence
    # is required for same-day top-up guards and reporting, and a sparse
    # blocked stub would erase it.
    #
    # UPDATED for Codex review "Publish retry failures while preserving prior
    # orders": preserving the evidence must not mean republishing the earlier
    # SUCCESS as this slot's current state. Returning the prior artifact
    # verbatim (and pointing the latest pointer at it) hid the retry's failure,
    # its reason and its failed classification, so a watchdog saw a healthy run
    # where one had just failed. Both halves are now required: the artifact is
    # marked blocked with the current reason AND still carries the orders that
    # really were placed.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL", "OTHER"))
    cfg = MorningConfig(
        tickers=("SMALL", "OTHER"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03, "OTHER": 0.03})

    # First scheduled attempt: broker wired correctly, order submitted.
    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    assert first["artifacts"].get("status") != "blocked"
    assert {row["status"] for row in first["artifacts"]["buy_candidates"]} == {"submitted"}
    run_state_path = Path(first["paths"]["run_state"])
    assert run_state_path.exists()

    # Simulate reconciliation between attempts: one order filled while the
    # other remains in flight. Both are deployed capital and must survive the
    # blocked retry merge.
    prior = json.loads(run_state_path.read_bytes())
    prior["buy_candidates"][0]["status"] = "filled"
    filled_ticker = prior["buy_candidates"][0]["ticker"]
    prior["broker_outcomes"][filled_ticker]["status"] = "filled"
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    # Second scheduled attempt at the SAME slot: broker adapter missing this
    # time (a wiring regression). The prior submitted evidence must survive.
    second = ex.run(_now_live(), data, broker=None)

    # The retry's own failure is published...
    assert second["artifacts"].get("status") == "blocked", (
        "the failed retry must be visible, not hidden behind the earlier success"
    )
    assert second["artifacts"].get("block_reason")
    assert second["artifacts"].get("preserved_prior_submitted_orders") is True
    # ...while the earlier attempt's real order evidence survives in it.
    assert {row["status"] for row in second["artifacts"]["buy_candidates"]} == {"submitted", "filled"}
    on_disk = json.loads(run_state_path.read_bytes())
    assert {row["status"] for row in on_disk["buy_candidates"]} == {"submitted", "filled"}
    assert {outcome["status"] for outcome in on_disk["broker_outcomes"].values()} == {"submitted", "filled"}
    assert on_disk["status"] == "blocked"

    # UPDATED AGAIN for Codex review "Preserve the carried submission count
    # after classification": the carry-forward ran BEFORE
    # _apply_blocked_before_placement_fields(), which zeroes
    # orders_submitted_count and stamps a never-reached-placement outcome. The
    # published artifact therefore kept the submitted rows while reporting zero
    # submissions and a before-placement failure -- summaries and operational
    # consumers read a contradiction of the very evidence the merge preserves.
    # The counts and the classification must now agree with the rows.
    assert second["artifacts"]["orders_submitted_count"] == 2
    assert on_disk["orders_submitted_count"] == 2
    assert on_disk["run_classification_outcome"] == "failed_after_partial_submission"
    assert on_disk["order_submit_status"] == "submitted"
    # The retry itself still proved nothing about the slot executing, so the
    # scheduled-evidence denial the blocked stamp applies has to stand.
    assert on_disk["scheduled_evidence_denied"] is True

    # UPDATED AGAIN for Codex review discussion_r3867947151 ("Synchronize
    # nested counts after carrying orders forward"): only the TOP-LEVEL
    # orders_submitted_count moved with the carried rows. counts.submitted --
    # which build_artifacts() never writes -- stayed absent, and
    # counts.buy_candidates still described this failed attempt's own freshly
    # evaluated list. dca_weekly_dashboard.build_performance_snapshot() sums
    # those nested fields directly, so the preserved orders and candidates
    # dropped out of dashboard totals even though the artifact carried them.
    assert on_disk["counts"]["submitted"] == 2
    assert on_disk["counts"]["buy_candidates"] == len(on_disk["buy_candidates"])
    assert second["artifacts"]["counts"]["submitted"] == 2


def test_blocked_retry_counts_track_a_candidate_that_no_longer_qualifies(tmp_path):
    # The nested-count sync above is not merely "copy orders_submitted_count":
    # counts.buy_candidates has to follow the MERGED list. A ticker whose order
    # really went in is carried forward as an order-bearing row even when this
    # attempt no longer evaluates it as a buy, so the merged list can be longer
    # than the count build_artifacts() computed (Codex review
    # discussion_r3867947151).
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL", "OTHER"))
    cfg = MorningConfig(
        tickers=("SMALL", "OTHER"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03, "OTHER": 0.03})

    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    run_state_path = Path(first["paths"]["run_state"])

    # The earlier attempt submitted a ticker this retry will not evaluate at
    # all -- it is not even in the configured universe any more.
    prior = json.loads(run_state_path.read_bytes())
    prior["buy_candidates"] = [dict(prior["buy_candidates"][0], ticker="GONE", status="filled")]
    prior["broker_outcomes"] = {"GONE": {"status": "filled"}}
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    second = ex.run(_now_live(), data, broker=None)
    artifacts = second["artifacts"]

    assert artifacts["status"] == "blocked"
    assert "GONE" in {row["ticker"] for row in artifacts["buy_candidates"]}
    # The nested count describes the artifact that was actually written.
    assert artifacts["counts"]["buy_candidates"] == len(artifacts["buy_candidates"])
    assert artifacts["counts"]["submitted"] == artifacts["orders_submitted_count"] == 1


def test_blocked_retry_synthesizes_a_row_for_outcome_only_evidence(tmp_path):
    # Codex review discussion_r3871148488 ("Rebuild candidate rows from
    # outcome-only orders"): a prior artifact can carry its accepted order in
    # broker_outcomes ALONE -- buy_candidates and orders_submitted_count are
    # both optional on a valid run_state, and
    # _existing_submitted_run_state() explicitly recognizes that shape (see
    # its own docstring: "buy_candidates/broker_outcomes entry"). The
    # carry-forward's order_bearing_rows was blind to it: with no matching
    # row, order_bearing_rows stayed empty for that ticker and the retry's
    # own freshly-evaluated, unsubmitted candidate was left representing it.
    # dca_risk.deployed_usd() and the same-day allocation loader both derive
    # deployed capital from buy_candidates, not broker_outcomes, so they read
    # zero for a real order and a later stream could buy the ticker again.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL", "OTHER"))
    cfg = MorningConfig(
        tickers=("SMALL", "OTHER"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03, "OTHER": 0.03})

    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    run_state_path = Path(first["paths"]["run_state"])

    # Rewrite the earlier attempt into the outcome-only shape: the accepted
    # order lives in broker_outcomes, but buy_candidates carries no row for
    # it at all (not even an unsubmitted one) -- the schema does not require
    # one, and _existing_submitted_run_state() reads broker_outcomes on its
    # own for exactly this reason.
    prior = json.loads(run_state_path.read_bytes())
    submitted_ticker = prior["buy_candidates"][0]["ticker"]
    submitted_amount = prior["buy_candidates"][0]["planned_amount_usd"]
    prior["buy_candidates"] = []
    prior["broker_outcomes"] = {
        submitted_ticker: {
            "status": "submitted",
            "order_id": "ORD-OUTCOME-ONLY",
            "submitted_amount_usd": submitted_amount,
        }
    }
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    second = ex.run(_now_live(), data, broker=None)
    artifacts = second["artifacts"]

    assert artifacts["status"] == "blocked"
    synthesized = [
        row for row in artifacts["buy_candidates"] if row["ticker"] == submitted_ticker
    ]
    assert len(synthesized) == 1, "the outcome-only order must be represented by a row"
    row = synthesized[0]
    assert row["status"] == "submitted"
    assert row["order_id"] == "ORD-OUTCOME-ONLY"
    assert row["planned_amount_usd"] == submitted_amount
    # dca_risk.deployed_usd() and the same-day allocation loader both read
    # this field, so it must be a real deployed-capital status, not the
    # retry's own freshly-evaluated, unsubmitted classification.
    assert row["status"] in ("submitted", "filled")
    assert artifacts["orders_submitted_count"] == 1
    assert artifacts["counts"]["submitted"] == 1
    assert artifacts["counts"]["buy_candidates"] == len(artifacts["buy_candidates"])


@pytest.mark.parametrize(
    ("outcome", "expects_unreconstructable"),
    [
        ({"status": "submitted", "submitted_amount_usd": True}, True),
        ({"ambiguous_placement_exception": True, "submitted_amount_usd": 2.0}, False),
    ],
    ids=("boolean-amount", "ambiguous-missing-status"),
)
def test_blocked_retry_validates_outcome_only_order_amount_and_status(
    tmp_path, outcome, expects_unreconstructable
):
    """Outcome-only carry-forward must not normalize corrupt evidence into a safe order."""
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    state_path = Path(first["paths"]["run_state"])
    prior = json.loads(state_path.read_bytes())
    ticker = prior["buy_candidates"][0]["ticker"]
    prior["buy_candidates"] = []
    prior["broker_outcomes"] = {ticker: outcome}
    state_path.write_text(json.dumps(prior), encoding="utf-8")

    artifacts = ex.run(_now_live(), data, broker=None)["artifacts"]

    if expects_unreconstructable:
        assert artifacts["unreconstructable_submitted_tickers"] == [ticker]
        assert not any(
            row["ticker"] == ticker and row.get("planned_amount_usd") == 1.0
            for row in artifacts["buy_candidates"]
        )
    else:
        row = next(row for row in artifacts["buy_candidates"] if row["ticker"] == ticker)
        assert row["status"] == "failed"
        assert row["ambiguous_placement_exception"] is True
        assert row["synthesized_from_broker_outcome"] is True


@pytest.mark.parametrize("pre_terminal_status", ["pending", "unconfirmed"])
def test_blocked_retry_preserves_pending_and_unconfirmed_orders(tmp_path, pre_terminal_status):
    # Codex review ("Preserve pending and unconfirmed orders on blocked
    # retries"): a broker acceptance that has not yet reached a terminal state
    # (pending/unconfirmed) is still real committed capital --
    # scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES and
    # dca_risk.DEPLOYED_CANDIDATE_STATUSES both already treat it as such.
    # _existing_submitted_run_state() and the carry-forward filters were
    # restricted to submitted/filled only, so a wiring failure on the NEXT
    # retry overwrote a still-pending order's evidence with a fresh
    # zero-order blocked stub, and a later stream could reallocate capital
    # that may still fill.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    run_state_path = Path(first["paths"]["run_state"])

    # Simulate a broker that accepted the order but has not yet confirmed a
    # terminal state (e.g. a post-review confirmation still outstanding).
    prior = json.loads(run_state_path.read_bytes())
    pending_ticker = prior["buy_candidates"][0]["ticker"]
    prior["buy_candidates"][0]["status"] = pre_terminal_status
    prior["broker_outcomes"][pending_ticker]["status"] = pre_terminal_status
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    # Second scheduled attempt: broker adapter missing (a wiring regression).
    # The pending/unconfirmed evidence must survive, not be overwritten by an
    # empty blocked stub.
    second = ex.run(_now_live(), data, broker=None)
    artifacts = second["artifacts"]

    assert artifacts["status"] == "blocked"
    assert artifacts.get("preserved_prior_submitted_orders") is True, (
        f"a {pre_terminal_status} order must still count as evidence to preserve"
    )
    carried = [row for row in artifacts["buy_candidates"] if row["ticker"] == pending_ticker]
    assert len(carried) == 1
    assert carried[0]["status"] == pre_terminal_status
    assert artifacts["broker_outcomes"][pending_ticker]["status"] == pre_terminal_status
    assert artifacts["orders_submitted_count"] == 1

    on_disk = json.loads(run_state_path.read_bytes())
    assert on_disk["status"] == "blocked"
    assert any(
        row["ticker"] == pending_ticker and row["status"] == pre_terminal_status
        for row in on_disk["buy_candidates"]
    ), "the pending/unconfirmed order must not be erased from the persisted artifact"


def test_blocked_retry_preserves_ambiguous_placement_reservation(tmp_path):
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    run_state_path = Path(first["paths"]["run_state"])
    prior = json.loads(run_state_path.read_bytes())
    ticker = prior["buy_candidates"][0]["ticker"]
    # Leave the ambiguous placement only in broker_outcomes. The blocked
    # retry must synthesize a candidate and copy the marker onto it, because
    # deployment and same-day allocation accounting consume candidate rows.
    prior["buy_candidates"] = []
    prior["broker_outcomes"][ticker].update(status="failed", ambiguous_placement_exception=True)
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    second = ex.run(_now_live(), data, broker=None)
    carried = [row for row in second["artifacts"]["buy_candidates"] if row["ticker"] == ticker]
    assert len(carried) == 1
    assert carried[0]["ambiguous_placement_exception"] is True
    assert second["artifacts"]["broker_outcomes"][ticker]["ambiguous_placement_exception"] is True
    assert second["artifacts"]["orders_submitted_count"] == 1


@pytest.mark.parametrize("pre_terminal_status", ["pending", "unconfirmed"])
def test_blocked_retry_carries_candidate_only_pending_orders_into_the_csv(
    tmp_path, pre_terminal_status
):
    # Codex review ("Carry in-flight candidates into submitted_orders.csv"):
    # the prior test covers the shape where broker_outcomes ALSO carries the
    # pending/unconfirmed status -- submitted_order_rows() picks that up on
    # its own via ORDER_ATTEMPT_STATUSES, so it never actually exercised
    # _carried_forward_order_rows()'s own buy_candidates-side filter. Here the
    # prior artifact's ONLY evidence is the buy_candidates row -- no matching
    # broker_outcomes entry at all, a valid shape the schema allows -- so the
    # row can reach submitted_orders.csv ONLY through
    # _carried_forward_order_rows(), which still checked ("submitted",
    # "filled") only. The drift detector reads this CSV to build its
    # submitted set, so a dropped row here reports a real in-flight order as
    # unsubmitted.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    run_state_path = Path(first["paths"]["run_state"])

    prior = json.loads(run_state_path.read_bytes())
    pending_ticker = prior["buy_candidates"][0]["ticker"]
    prior["buy_candidates"][0]["status"] = pre_terminal_status
    prior["broker_outcomes"] = {}  # candidate-only evidence: no matching outcome
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    second = ex.run(_now_live(), data, broker=None)
    assert second["artifacts"]["status"] == "blocked"
    carried = [
        row for row in second["artifacts"]["buy_candidates"] if row["ticker"] == pending_ticker
    ]
    assert len(carried) == 1 and carried[0]["status"] == pre_terminal_status

    csv_path = run_state_path.parent / "submitted_orders.csv"
    with csv_path.open(newline="", encoding="utf-8") as handle:
        order_rows = list(csv.DictReader(handle))
    assert any(
        row["ticker"] == pending_ticker and row["status"] == pre_terminal_status
        for row in order_rows
    ), "a candidate-only pending/unconfirmed order must still reach submitted_orders.csv"


@pytest.mark.parametrize("outcome_status", ["pending", "unconfirmed", "filled"])
def test_carried_forward_order_rows_skips_an_outcome_already_carried_by_submitted_order_rows(
    outcome_status,
):
    # Codex review ("Deduplicate pending outcomes before carrying candidate
    # rows"): submitted_order_rows() already emits a row for any broker
    # outcome whose status is in ORDER_ATTEMPT_STATUSES (submitted, filled,
    # pending, unconfirmed, not_submitted_confirmation_required) or that
    # carries a concrete order_id -- not just "submitted". _carried_forward_
    # order_rows()'s own dedup set used to seed from status == "submitted"
    # only, so the NORMAL preserved shape -- a pending/unconfirmed/filled
    # outcome with a matching deployed-status candidate row, both from the
    # SAME earlier attempt -- got the candidate appended a second time,
    # double-counting the same order in submitted_orders.csv and inflating
    # downstream row-count totals such as
    # generate_credit_usage_proxy_report._count_csv_data_rows().
    from scripts.scheduled_artifacts import submitted_order_rows

    broker_outcomes = {
        "SMALL": {"status": outcome_status, "order_id": "O1", "submitted_amount_usd": 25.0},
    }
    buy_candidates = [
        {"ticker": "SMALL", "status": outcome_status, "order_id": "O1", "planned_amount_usd": 25.0},
    ]
    rows = submitted_order_rows(broker_outcomes)
    rows.extend(morning_dca._carried_forward_order_rows(broker_outcomes, buy_candidates))
    matching = [row for row in rows if row.get("order_id") == "O1"]
    assert len(matching) == 1, f"a {outcome_status} order must not be counted twice"


def test_is_deployed_candidate_status_ignores_an_unhashable_status():
    # Codex review ("Validate persisted statuses before set membership"):
    # switching the carry-forward filters and _existing_submitted_run_state()
    # from an ("submitted", "filled") TUPLE check to
    # DEPLOYED_CANDIDATE_STATUSES set membership introduced a regression --
    # `x in a_tuple` never requires `x` to be hashable, but `x in a_frozenset`
    # hashes `x` first. A persisted run_state.json is arbitrary JSON accepted
    # on the strength of being a dict, so a corrupt artifact can carry
    # "status": [] or {}; that used to be simply "not submitted" and is now
    # a TypeError, mirroring exactly the dca_risk._is_deployed_status() defect
    # (and fix) of the same name.
    from morning_dca import _is_deployed_candidate_status

    assert _is_deployed_candidate_status([]) is False
    assert _is_deployed_candidate_status({}) is False
    assert _is_deployed_candidate_status(7) is False
    assert _is_deployed_candidate_status(None) is False
    assert _is_deployed_candidate_status("submitted") is True
    assert _is_deployed_candidate_status("pending") is True
    assert _is_deployed_candidate_status("skipped") is False


def test_blocked_retry_survives_a_corrupt_status_in_the_existing_artifact(tmp_path):
    # The end-to-end consequence: _blocked_live_result() reads the existing
    # slot's run_state.json to decide what to carry forward. Before the fix,
    # an unhashable status there raised TypeError straight out of run(),
    # escaping the blocked-artifact write entirely -- the opposite of the
    # documented fail-closed behavior ("an existing state that is ...
    # not a dict, or has no submitted evidence contributes nothing").
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    run_state_path = Path(first["paths"]["run_state"])

    # Corrupt the persisted status into an unhashable shape.
    prior = json.loads(run_state_path.read_bytes())
    prior["buy_candidates"][0]["status"] = []
    ticker = prior["buy_candidates"][0]["ticker"]
    prior["broker_outcomes"][ticker]["status"] = {}
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    # Must not raise. With nothing hashable to recognize as order-bearing,
    # this is treated as "no evidence to preserve" and overwritten with a
    # fresh blocked stub -- never a crash escaping the write entirely.
    second = ex.run(_now_live(), data, broker=None)
    assert second["artifacts"]["status"] == "blocked"

    on_disk = json.loads(run_state_path.read_bytes())
    assert on_disk["status"] == "blocked"


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
        target_account_number="999000111",
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
    class _WrongAccountBroker(FakeLiveBroker):
        def account_number(self):
            return "not-the-approved-account"

    second = ex.run(_now_live(), data, broker=_WrongAccountBroker())

    assert second["artifacts"]["status"] == "blocked"
    assert second["artifacts"]["block_reason"] == (
        "broker account mismatch: expected 999000111, got not-the-approved-account"
    )
    on_disk = json.loads(run_state_path.read_bytes())
    assert on_disk["block_reason"] == (
        "broker account mismatch: expected 999000111, got not-the-approved-account"
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
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_bytes())
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
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_bytes())
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
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_bytes())
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
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_bytes())
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
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_bytes())
    assert on_disk["block_reason"] == "missing broker adapter for scheduled live execution"


def test_preflight_missing_on_retry_degrades_to_full_universe_not_a_block(tmp_path):
    # NON-BLOCKING as of 2026-08-02 (Geoff: "should not block any later runs"),
    # superseding the old "preflight_missing_block retry preserves prior
    # submitted evidence" test (Codex review, PR #88), which pinned a
    # belt-and-suspenders local-artifact preservation path that only existed
    # because a missing preflight used to overwrite the dated-slot run_state
    # with a blocked stub. That branch is gone: a missing preflight now always
    # degrades to the full universe, so there is nothing to preserve against
    # here -- the real, unconditional duplicate-order protection for a same-
    # slot retry is broker.same_day_strategy_order_exists() in
    # _place_broker_orders, checked before every placement regardless of
    # preflight status. This test just confirms a retry whose same-day
    # preflight artifact disappeared between attempts is not treated as a
    # failure or a block.
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

    first = ex.run(_now_live(), data, broker=FakeLiveBroker())
    assert first["artifacts"]["buy_candidates"][0]["status"] == "submitted"

    # The same-day preflight artifact becomes unavailable before the retry
    # (e.g. deleted, transient read failure, disk issue).
    for f in pf_dir.glob("*/run_state.json"):
        f.unlink()

    retry_now = _now_live() + timedelta(minutes=1)
    second = ex.run(retry_now, data, broker=FakeLiveBroker())
    run_state_path = Path(second["paths"]["run_state"])

    assert second["artifacts"].get("status") != "blocked", (
        "a missing same-day preflight artifact must not block a retry"
    )
    assert second["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    on_disk = json.loads(run_state_path.read_bytes())
    assert on_disk["buy_candidates"][0]["status"] == "submitted"
    assert second["artifacts"]["preflight_gate"]["preflight_found"] is False
    evaluated = {r["ticker"] for r in second["artifacts"]["signal_rows"]}
    assert evaluated == {"SMALL"}


def test_auto_buy_candidates_prioritized_by_multiplier_when_cap_hit(tmp_path):
    # When the daily cap is tight, higher-multiplier tickers are placed first.
    # BIGDROP: 8% drop -> 5x -> $10 notional. SMALLDROP: 1% drop -> 1.5x -> $3.00 notional.
    # Daily cap of $11 allows BIGDROP ($10 < $11) then blocks SMALLDROP ($10+$3 > $11).
    # The sort ensures the high-multiplier ticker gets the budget slot, not whichever
    # happened to be evaluated first.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _FakeLiveBroker(FakeLiveBroker):
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


def test_morning_evaluate_top_up_routes_subdollar_ladder_increment_to_pending_credit():
    # Codex review discussion_r3705349440 ("Route sub-dollar top-ups through
    # the credit policy"): a ladder tier crossing from 1x to 1.3x on a $1 base
    # produces a clean ladder_incremental of $0.30 -- below both Robinhood's
    # $1 minimum and cumulative_top_up_min_drop_usd ($0.50, so the price-step
    # floor is $0). The old code returned this $0.30 directly as
    # incremental_order_amount (a sub-$1 broker order) or, once it hit $0,
    # simply dropped it. It must instead be routed through
    # apply_cumulative_credit (final_session=False, since this is never the
    # 3:30 PM final session): a $0.30 credit alone stays pending.
    from near_close_dca import TOP_UP_NO_INCREMENTAL

    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.95,
        previous_close=100.0,
        drop_pct=0.05,
        multiplier=1.3,
        hypothetical_amount_usd=1.3,
        selected_base_amount_usd=1.0,
    )
    outcome = ex._evaluate_top_up(signal, prior)
    assert outcome.top_up_guard_result == TOP_UP_NO_INCREMENTAL
    assert outcome.incremental_order_amount == 0.0
    assert outcome.top_up_skipped is True
    # The decline must still be reported so the caller can durably persist
    # the new pending balance even though no broker order happens this run --
    # a None here is exactly how a credit gets silently dropped.
    assert outcome.cumulative_credit_decline_usd == 0.30


def test_morning_top_up_deducts_partially_settled_slot_credit():
    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=2.0,
        hypothetical_amount_usd=2.0,
        selected_base_amount_usd=1.0,
    )

    outcome = ex._evaluate_top_up(signal, prior, settled_credit_for_slot_usd=0.80)

    assert outcome.incremental_order_amount == 0.0
    assert outcome.cumulative_credit_decline_usd == 0.20


def test_morning_evaluate_top_up_sub_dollar_floor_still_deducts_reflected_settlement():
    # Codex review, PR #656 follow-up: "Keep the settled deduction for
    # non-ladder floors". settled_credit_reflected_in_prior=True only means
    # prior.amount_usd (and therefore ladder_incremental) already nets out
    # the settled amount -- it does NOT mean price_step_incremental/
    # decline_usd (computed purely from prices) reflect it too. Those must
    # still deduct settled_credit_for_slot_usd directly, or this sub-dollar
    # floor recomputes the FULL pre-settlement decline and a recovered
    # order's idempotent settlement no-ops, letting a later stream
    # repurchase the already-paid portion.
    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="own_slot",
        source="scheduled",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=50.0,
        previous_close=100.0,
        drop_pct=50.0,
        multiplier=1.3,
        hypothetical_amount_usd=1.3,
        selected_base_amount_usd=1.0,
    )

    outcome = ex._evaluate_top_up(
        signal,
        prior,
        settled_credit_for_slot_usd=0.80,
        settled_credit_reflected_in_prior=True,
    )

    # decline_usd = 100 - 50 = 50.0, the dominant floor; minus the settled
    # $0.80 (still owed here since it never touched prior.amount_usd) gives
    # $49.20 -- NOT $50.00 (settlement wrongly dropped) and NOT $49.20 minus
    # a second ladder-side deduction (ladder_incremental itself is only
    # $0.30, already net, so it never becomes the binding floor here).
    assert outcome.cumulative_credit_decline_usd == 49.20
    assert outcome.incremental_order_amount == 49.20


def test_morning_evaluate_top_up_full_dollar_ladder_surfaces_preserved_slot_credit():
    # Codex review: "Deduct settled credit from full-dollar retries too". A
    # ladder increment >= $1 used to return with cumulative_credit_decline_usd
    # left at its None default, so _compute_top_up's reconciliation and
    # _place_broker_orders' settle-on-placement both silently skipped this
    # slot's own outstanding preserved credit, orphaning it forever. The
    # ladder branch must surface it on the outcome even though the ladder's
    # own sizing math never touches the credit ledger.
    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )

    outcome = ex._evaluate_top_up(signal, prior, preserved_credit_for_slot_usd=0.20)

    assert outcome.incremental_order_amount > 1.0
    assert outcome.cumulative_credit_decline_usd == 0.20

    # No outstanding preserved credit for this slot -> unchanged (None), as
    # before this fix, so an ordinary ladder order without any credit
    # provenance still never touches the ledger.
    plain_outcome = ex._evaluate_top_up(signal, prior)
    assert plain_outcome.cumulative_credit_decline_usd is None


def test_morning_evaluate_top_up_full_dollar_ladder_deducts_settled_slot_credit():
    # Codex review: "Deduct settled credit in the full-dollar branch". A
    # partially settled preserved slot ($0.80 already paid, $0.20 still
    # outstanding) that later crosses back above the $1 ladder threshold used
    # to size its retry from the FULL desired - prior.amount_usd, ignoring
    # settled_credit_for_slot_usd entirely and repurchasing the paid $0.80. The
    # full-dollar branch must subtract it just like the sub-dollar branch
    # already does.
    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )

    outcome = ex._evaluate_top_up(
        signal, prior, settled_credit_for_slot_usd=0.80, preserved_credit_for_slot_usd=0.20
    )

    unsettled_outcome = ex._evaluate_top_up(signal, prior, preserved_credit_for_slot_usd=0.20)
    assert outcome.incremental_order_amount == pytest.approx(
        unsettled_outcome.incremental_order_amount - 0.80
    )


def test_morning_evaluate_top_up_routes_post_settlement_sub_dollar_remainder_to_credit_policy():
    # Codex review, PR #656 follow-up on discussion_r3837494241 ("Route
    # post-settlement sub-dollar amounts through credit handling"):
    # ladder_incremental itself is $1.20 (>= the $1 broker minimum, so the
    # full-dollar branch is entered), but settled_credit_for_slot_usd=$0.80
    # brings the remainder down to $0.40 -- below the broker minimum. That
    # $0.40 must be routed through apply_cumulative_credit() and stay
    # pending (not returned as a directly placeable sub-$1 order the broker
    # would reject).
    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=2.2,
        hypothetical_amount_usd=2.2,
        selected_base_amount_usd=1.0,
    )

    outcome = ex._evaluate_top_up(signal, prior, settled_credit_for_slot_usd=0.80)

    assert outcome.incremental_order_amount == 0.0
    assert outcome.top_up_skipped is True
    assert outcome.cumulative_credit_decline_usd == 0.40


def test_morning_full_dollar_retry_keeps_outstanding_preserve_as_ledger_basis():
    """A grown ladder retry must not reconstruct the paid preserve portion."""
    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=2.0,
        hypothetical_amount_usd=2.0,
        selected_base_amount_usd=1.0,
    )

    outcome = ex._evaluate_top_up(
        signal,
        prior,
        settled_credit_for_slot_usd=0.50,
        preserved_credit_for_slot_usd=0.30,
    )

    # The $1 ladder increment is netted by the already-paid $0.50 before it
    # reaches cumulative-credit sizing (1.00 - 0.50 = 0.50). That recomputed
    # net decline, not a stale prior preserve snapshot, must be carried for
    # reconciliation so ladder growth between retries is not lost.
    assert outcome.incremental_order_amount == 1.0
    assert outcome.cumulative_credit_decline_usd == 0.50


def test_morning_evaluate_top_up_routes_subdollar_ladder_increment_to_a_dollar_order():
    # Same ladder-tier-crossing shape as above but with existing pending
    # credit from an earlier slot already at $0.25: $0.25 (existing) + $0.30
    # (this decline) = $0.55, which crosses the 50-cent round-up threshold.
    # The old code would have returned the raw $0.30 ladder_incremental
    # (ignoring pending state entirely) as a sub-$1 order; the fix must
    # instead round the ACCUMULATED total up to a full $1 order.
    from near_close_dca import TOP_UP_INCREMENTAL

    ex = MorningDCAExecutor(MorningConfig())
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.95,
        previous_close=100.0,
        drop_pct=0.05,
        multiplier=1.3,
        hypothetical_amount_usd=1.3,
        selected_base_amount_usd=1.0,
    )
    outcome = ex._evaluate_top_up(signal, prior, existing_pending_credit_usd=0.25)
    assert outcome.top_up_guard_result == TOP_UP_INCREMENTAL
    assert outcome.incremental_order_amount == 1.0
    assert outcome.top_up_skipped is False
    assert outcome.cumulative_credit_decline_usd == 0.30


def test_morning_retry_keeps_partial_preserve_out_of_minimum_order_pool(tmp_path):
    from dca_cumulative import CumulativeCreditLedger

    prior_root = tmp_path / "prior"
    _write_prior_run_state(prior_root, "AAPL", amount_usd=1.0, decision_price=100.0, selected_base_amount_usd=1.0)
    cfg = MorningConfig(
        tickers=("AAPL",),
        prior_allocation_roots=(str(prior_root),),
        cap_seed_roots=None,
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "credits")
    now = datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=EASTERN)
    slot_key = ex._credit_idempotency_key(now, manual=False)
    ex.credit_ledger.preserve_pending("AAPL", "2026-07-08", 1.0, idempotency_key=slot_key)
    ex.credit_ledger.settle("AAPL", "2026-07-08", 0.80, stream="midday")
    signal = MorningSignal(
        ticker="AAPL", source="finance", action=Action.BUY, reason="test",
        current_price=99.2, previous_close=100.0, drop_pct=0.8, multiplier=1.8,
        hypothetical_amount_usd=1.8, selected_base_amount_usd=1.0,
    )

    outcome = ex._compute_top_up(now, [signal])["AAPL"]
    state = json.loads((tmp_path / "credits" / "2026-07-08" / "ledger.json").read_bytes())

    assert outcome.cumulative_credit_decline_usd == 0.20
    assert ex.credit_ledger.pending_usd("AAPL", "2026-07-08") == 0.20
    assert state["minimum_order_pending_usd_by_ticker"] == {}


def test_morning_compute_top_up_persists_pending_credit_and_is_retry_safe(tmp_path):
    # End-to-end through _compute_top_up (the only caller that reads/writes
    # the ledger; _evaluate_top_up itself stays pure). The sub-dollar decline
    # must be durably recorded via CumulativeCreditLedger so a later same-day
    # slot or the 3:30 PM final session can read it back with pending_usd().
    # A retry of this exact same scheduled slot (the dated-slot run_state.json
    # already reflects last attempt's credited decline) must NOT re-apply the
    # same decline on top of the already-persisted balance.
    from dca_cumulative import CumulativeCreditLedger

    prior_root = tmp_path / "prior"
    _write_prior_run_state(prior_root, "AAPL", amount_usd=1.0, decision_price=100.0, selected_base_amount_usd=1.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL",))
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        cap_seed_roots=(),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "credits")
    now = datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=EASTERN)
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.95,
        previous_close=100.0,
        drop_pct=0.05,
        multiplier=1.3,
        hypothetical_amount_usd=1.3,
        selected_base_amount_usd=1.0,
    )

    outcomes = ex._compute_top_up(now, [signal])
    assert outcomes["AAPL"].incremental_order_amount == 0.0
    assert ex.credit_ledger.pending_usd("AAPL", "2026-07-08") == 0.30

    # Simulate this slot's own dated artifact having already been written
    # (as run() would do) with the credited decline on the AAPL row.
    run_dir = ex.run_state_dir(now)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",
            "buy_candidates": [{"ticker": "AAPL", "cumulative_credit_decline_usd": 0.30}],
        }),
        encoding="utf-8",
    )

    # A retry recomputing the identical signal/prior must not double-apply
    # the same $0.30 decline on top of the $0.30 already persisted.
    ex._compute_top_up(now, [signal])
    assert ex.credit_ledger.pending_usd("AAPL", "2026-07-08") == 0.30


def test_morning_compute_top_up_fails_closed_when_settlement_provenance_read_fails(tmp_path, monkeypatch):
    """An unreadable settlement snapshot is a run failure, never a quiet skip.

    The morning stream reads pending/preserved/settled provenance for a
    retried slot from ONE locked ``provenance_snapshot`` read (PR #1051). If
    that read fails, the executor must raise rather than emit a healthy
    no-incremental outcome for the ticker: a completed-no-orders artifact
    would mask the outage, and a later retry could then rebuild the
    already-paid $0.80 below from scratch. The ledger itself must be left
    untouched (the $0.20 remainder still pending).
    """
    from dca_cumulative import CumulativeCreditLedger

    prior_root = tmp_path / "prior"
    _write_prior_run_state(prior_root, "AAPL", amount_usd=1.0, decision_price=100.0, selected_base_amount_usd=1.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL",))
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        cap_seed_roots=None,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "credits")
    now = datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=EASTERN)
    slot_key = ex._credit_idempotency_key(now, manual=False)
    ex.credit_ledger.preserve_pending("AAPL", "2026-07-08", 1.0, idempotency_key=slot_key)
    ex.credit_ledger.settle("AAPL", "2026-07-08", 0.80)
    monkeypatch.setattr(
        ex.credit_ledger,
        "provenance_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("temporary read failure")),
    )
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.20,
        previous_close=100.0,
        drop_pct=0.8,
        multiplier=1.3,
        hypothetical_amount_usd=1.3,
        selected_base_amount_usd=1.0,
    )

    with pytest.raises(RuntimeError, match="ledger balance read failed for AAPL"):
        ex._compute_top_up(now, [signal])

    assert ex.credit_ledger.pending_usd("AAPL", "2026-07-08") == 0.20
    assert ex.credit_ledger.settled_credit_for_slot("AAPL", "2026-07-08", slot_key) == 0.80


@pytest.mark.parametrize(
    "expected_remainder",
    [0.10, 0.30, 0.20],
)
def test_morning_partial_preserve_retry_does_not_create_minimum_order_provenance(
    tmp_path, monkeypatch, expected_remainder
):
    from dataclasses import replace

    from dca_cumulative import CumulativeCreditLedger

    prior_root = tmp_path / "prior"
    _write_prior_run_state(prior_root, "AAPL", amount_usd=1.0, decision_price=100.0, selected_base_amount_usd=1.0)
    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL",))
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        cap_seed_roots=None,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "credits")
    now = datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=EASTERN)
    slot_key = ex._credit_idempotency_key(now, manual=False)
    ex.credit_ledger.preserve_pending("AAPL", "2026-07-08", 1.00, idempotency_key=slot_key)
    ex.credit_ledger.settle("AAPL", "2026-07-08", 0.80, stream="afternoon")
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.20,
        previous_close=100.0,
        drop_pct=0.8,
        multiplier=1.3,
        hypothetical_amount_usd=1.3,
        selected_base_amount_usd=1.0,
    )
    evaluate_top_up = ex._evaluate_top_up

    def adjusted_retry(*args, **kwargs):
        return replace(
            evaluate_top_up(*args, **kwargs),
            cumulative_credit_decline_usd=expected_remainder,
        )

    monkeypatch.setattr(ex, "_evaluate_top_up", adjusted_retry)

    outcome = ex._compute_top_up(now, [signal])["AAPL"]

    assert outcome.incremental_order_amount == 0.0
    assert outcome.cumulative_credit_decline_usd == expected_remainder
    assert ex.credit_ledger.preserved_credit_for_slot("AAPL", "2026-07-08", slot_key) == expected_remainder
    assert ex.credit_ledger.pending_usd("AAPL", "2026-07-08") == expected_remainder
    assert ex.credit_ledger.minimum_order_pending_tickers("2026-07-08") == {}


def test_morning_scheduled_duplicate_recovery_settles_preserved_credit(tmp_path):
    # Codex review: "Settle reconciled preserves during duplicate recovery".
    # reconcile_preserved_credit durably updates a slot's preserve entry
    # BEFORE the broker call. If the process crashes after the broker
    # confirms the order but before the ORIGINAL attempt's own settle() call,
    # every later retry just keeps finding the same same-stream duplicate and
    # (before this fix) never settled -- permanently stranding the preserved
    # credit as pending even though its order already went through, letting a
    # later pass (e.g. the 15:30 aggregate top-up) repurchase it. The
    # scheduled duplicate-recovery branch must settle the ledger too.
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _DuplicateBroker:
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {"id": "ORDER-AAPL", "dollar_amount": 1.0}

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):  # pragma: no cover
            raise AssertionError("a genuine duplicate must never place a new order")

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    slot_key = "morning-2026-07-08-1030"
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 0.20, idempotency_key=slot_key
    )
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.20

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )
    # A prior allocation known -- has_prior_allocation is True, routing this
    # candidate into the same-stream ref-id duplicate check instead of the
    # broader same-day check.
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=5.0,
        decision_price=90.0,
        prior_same_day_amount=1.0,
        prior_same_day_reference_price=100.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.20,
    )
    broker = _DuplicateBroker()
    outcomes = ex._place_broker_orders(
        [signal],
        trading_day,
        broker,
        {"AAPL": outcome},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )
    assert outcomes["AAPL"]["status"] == "submitted"
    assert outcomes["AAPL"]["duplicate_preserved"] is True
    assert "AAPL" not in broker.placed
    # The recovered duplicate's settle() call must retire the preserved
    # credit exactly as a fresh confirmed placement would have.
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    assert ex.credit_ledger.preserved_credit_for_slot("AAPL", trading_day.isoformat(), slot_key) == 0.0
    events = ex.credit_ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "settled"
    assert events[-1]["submitted_amount_usd"] == 1.0


def test_morning_compute_top_up_skips_settled_deduction_when_prior_already_reflects_it(tmp_path):
    # Codex review, PR #656 discussion_r3837494241 ("Avoid deducting settled
    # credit already included in prior spend"): when a caller's
    # prior_allocation_roots is configured to include THIS stream's OWN
    # scheduled root (config/morning_dca.json and config/midday_dca.json
    # deliberately exclude it by default, but nothing stops an override),
    # prior.amount_usd already reflects an earlier settling order's dollar
    # amount from that same root. ladder_incremental = desired -
    # prior.amount_usd is then already net of it, so ALSO subtracting
    # settled_credit_for_slot_usd (as _evaluate_top_up's full-dollar branch does
    # by default) would double-deduct the same paid amount and under-order
    # the retry.
    from dca_cumulative import CumulativeCreditLedger

    state_root = tmp_path / "morning_live"
    trading_day = date(2026, 7, 8)
    now = datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN)
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(state_root),
        prior_allocation_roots=(str(state_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # Seed a prior artifact directly under the stream's own root, with THIS
    # EXACT slot's own run_id, reflecting an EARLIER settling order for this
    # ticker (as if an earlier retry of this same slot already placed a
    # $1.00 order).
    _seed_prior(
        state_root, "AAPL", 1.0,
        reference_price=100.0, trading_date=trading_day, selected_base_amount_usd=1.0,
        run_id=ex.run_id(now, manual=False),
    )
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "ledger")
    slot_key = ex._credit_idempotency_key(now, manual=False)
    # Preserve $1.00 for this slot, then partially settle $0.80 of it (as a
    # duplicate-recovery settle() call from an earlier attempt would have) --
    # leaving settled_credit_for_slot_usd=0.80 and preserved_credit_for_slot_usd=0.20
    # outstanding, matching test_morning_top_up_deducts_partially_settled_
    # slot_credit's own numbers.
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 1.0, idempotency_key=slot_key
    )
    ex.credit_ledger.settle(
        "AAPL", trading_day.isoformat(), 0.80, final_session=False, stream="morning",
    )
    assert ex.credit_ledger.settled_credit_for_slot("AAPL", trading_day.isoformat(), slot_key) == 0.80
    assert ex.credit_ledger.preserved_credit_for_slot("AAPL", trading_day.isoformat(), slot_key) == 0.20

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )
    outcomes = ex._compute_top_up(now, [signal])
    outcome = outcomes["AAPL"]

    # prior.amount_usd == 1.0 (from the seeded artifact under this stream's
    # own root, whose run_id matches this exact slot) already reflects the
    # settling order. ladder_incremental = desired(5.0) - prior(1.0) = 4.0
    # must NOT be further reduced by the separately-tracked
    # settled_credit_for_slot_usd(0.80) -- that would double-count the same
    # $0.80 and under-order by that amount.
    assert outcome.prior_same_day_amount == 1.0
    assert outcome.incremental_order_amount == 4.0


def test_morning_compute_top_up_still_deducts_settled_credit_when_prior_is_from_an_unrelated_run(tmp_path):
    # Codex review, PR #656 discussion_r3839800765 ("Verify that prior
    # allocation contains the settlement"): a magnitude-only comparison
    # (loaded prior >= settled amount) is not sufficient. Here the ticker
    # already has an UNRELATED $2.00 prior allocation from a DIFFERENT run
    # (a different run_id) -- large enough to satisfy that magnitude
    # inequality on its own ($2.00 >= $0.80) -- while the actual $0.80
    # settling order's own artifact was never loaded at all (e.g. lost in a
    # crash). The old magnitude-only check would wrongly conclude the prior
    # reflects the settlement and skip the deduction, letting this retry
    # repurchase the $0.80; the run_id check must catch this.
    from dca_cumulative import CumulativeCreditLedger

    state_root = tmp_path / "morning_live"
    trading_day = date(2026, 7, 8)
    now = datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN)
    _seed_prior(
        state_root, "AAPL", 2.0,
        reference_price=100.0, trading_date=trading_day, selected_base_amount_usd=1.0,
        run_id="some_other_run_20260708",
    )
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(state_root),
        prior_allocation_roots=(str(state_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "ledger")
    slot_key = ex._credit_idempotency_key(now, manual=False)
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 1.0, idempotency_key=slot_key
    )
    ex.credit_ledger.settle(
        "AAPL", trading_day.isoformat(), 0.80, final_session=False, stream="morning",
    )

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )
    outcomes = ex._compute_top_up(now, [signal])
    outcome = outcomes["AAPL"]

    # desired ($5) - prior.amount_usd ($2) = $3 ladder increment. The
    # unrelated $2 prior (wrong run_id) must NOT be mistaken for reflecting
    # the $0.80 settlement, so settled_credit_for_slot_usd is still subtracted:
    # $3.00 - $0.80 = $2.20.
    assert outcome.prior_same_day_amount == 2.0
    assert outcome.incremental_order_amount == 2.20


def test_morning_compute_top_up_skips_settled_deduction_when_this_slot_is_any_contributor(tmp_path):
    # Codex review, PR #656 follow-up: "Check all contributors for the
    # loaded settlement". ticker_prior.run_id names only the MOST RECENT
    # contributor (for reference_price ownership) -- a newer same-day
    # allocation for this ticker can overwrite run_id to itself while THIS
    # slot's own settled spend remains folded into amount_usd via
    # contributing_run_ids. An exact run_id equality wrongly concludes "not
    # loaded" in that case and re-deducts settled_credit_for_slot_usd, under-
    # ordering the retry by the already-paid amount.
    from dca_cumulative import CumulativeCreditLedger

    state_root = tmp_path / "morning_live"
    trading_day = date(2026, 7, 8)
    now = datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN)
    own_run_id = MorningDCAExecutor(
        MorningConfig(tickers=("AAPL",), morning_run_state_root=str(state_root)),
        mode=MORNING_LIVE_MODE,
    ).run_id(now, manual=False)

    # THIS slot's own earlier contribution ($0.80, matching the settled
    # amount below), followed by a NEWER unrelated run for the same ticker
    # (both strictly before `now`'s 10:30 boundary_et, so both are read as
    # prior context) that becomes the reference-owning run_id.
    own_dir = state_root / f"{trading_day.strftime('%Y%m%d')}_1000ET"
    own_dir.mkdir(parents=True, exist_ok=True)
    (own_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": trading_day.isoformat(),
            "generated_at": f"{trading_day.isoformat()}T10:00:00-04:00",
            "run_classification": {"run_id": own_run_id, "run_origin": "scheduled", "stream": "morning"},
            "buy_candidates": [
                {
                    "ticker": "AAPL", "status": "filled", "planned_amount_usd": 0.80,
                    "target_price": 100.0, "selected_base_amount_usd": 1.0,
                }
            ],
        }),
        encoding="utf-8",
    )
    newer_dir = state_root / f"{trading_day.strftime('%Y%m%d')}_1015ET"
    newer_dir.mkdir(parents=True, exist_ok=True)
    (newer_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": trading_day.isoformat(),
            "generated_at": f"{trading_day.isoformat()}T10:15:00-04:00",
            "run_classification": {"run_id": "newer_unrelated_run", "run_origin": "manual", "stream": "morning"},
            "buy_candidates": [
                {
                    "ticker": "AAPL", "status": "filled", "planned_amount_usd": 1.20,
                    "target_price": 99.0, "selected_base_amount_usd": 1.0,
                }
            ],
        }),
        encoding="utf-8",
    )

    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(state_root),
        prior_allocation_roots=(str(state_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "ledger")
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 1.0, idempotency_key=own_run_id
    )
    ex.credit_ledger.settle(
        "AAPL", trading_day.isoformat(), 0.80, final_session=False, stream="morning",
    )

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )
    outcomes = ex._compute_top_up(now, [signal])
    outcome = outcomes["AAPL"]

    # Combined prior amount_usd = $0.80 + $1.20 = $2.00; desired $5 - $2 =
    # $3 ladder increment. The $0.80 settlement IS reflected in that $2.00
    # (via this slot's own contributing_run_ids membership, even though
    # run_id now names the newer $1.20 contributor), so
    # settled_credit_for_slot_usd must NOT be subtracted again: incremental
    # stays at $3.00, not $2.20.
    assert outcome.prior_same_day_amount == 2.0
    assert outcome.incremental_order_amount == 3.0


def test_morning_compute_top_up_skips_settled_deduction_when_prior_root_is_an_ancestor(tmp_path):
    # Codex review, PR #656 follow-up on discussion_r3837494241 ("Recognize
    # ancestor roots that include the scheduled stream"): the fix above only
    # tested EXACT equality between self.state_root and each configured
    # root. An explicit override can instead name a common ANCESTOR of
    # self.state_root (e.g. the whole cron-tracking parent directory) --
    # load_prior_same_day_allocations_multi scans that root recursively, so
    # it still picks up this stream's own scheduled dated-slot artifacts
    # from underneath it, and the same double-deduction risk applies even
    # though state_root never equals the configured root literally.
    from dca_cumulative import CumulativeCreditLedger

    cron_root = tmp_path / "cron_tracking"
    state_root = cron_root / "morning_dca"
    trading_day = date(2026, 7, 8)
    now = datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN)
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(state_root),
        # The ANCESTOR of state_root, not state_root itself.
        prior_allocation_roots=(str(cron_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    _seed_prior(
        state_root, "AAPL", 1.0,
        reference_price=100.0, trading_date=trading_day, selected_base_amount_usd=1.0,
        run_id=ex.run_id(now, manual=False),
    )
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "ledger")
    slot_key = ex._credit_idempotency_key(now, manual=False)
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 1.0, idempotency_key=slot_key
    )
    ex.credit_ledger.settle(
        "AAPL", trading_day.isoformat(), 0.80, final_session=False, stream="morning",
    )

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )
    outcomes = ex._compute_top_up(now, [signal])
    outcome = outcomes["AAPL"]

    assert outcome.prior_same_day_amount == 1.0
    assert outcome.incremental_order_amount == 4.0


def test_morning_compute_top_up_still_deducts_settled_credit_when_prior_understates_it(tmp_path):
    # Codex review, PR #656 follow-up on discussion_r3837494241 ("Base
    # settlement deduction on the loaded allocation"): root coverage alone
    # does not prove the settling order is reflected in THIS ticker's
    # loaded prior. A crash after settle(order_id=...) succeeded but before
    # the original attempt's own run-state artifact was persisted leaves
    # prior.amount_usd smaller than settled_credit_for_slot_usd even though the
    # configured root covers this stream's own scheduled root. In that
    # case settled_credit_for_slot_usd must still be deducted -- skipping it
    # would let the retry reconcile the paid amount back into the
    # preserved balance, reviving credit a later stream could repurchase.
    from dca_cumulative import CumulativeCreditLedger

    cron_root = tmp_path / "cron_tracking"
    state_root = cron_root / "morning_dca"
    trading_day = date(2026, 7, 8)
    now = datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN)
    # The persisted prior artifact only reflects a stale $0.20 -- much less
    # than the $0.80 this slot's ledger already shows settled, simulating
    # the crash-before-persist window.
    _seed_prior(
        state_root, "AAPL", 0.20,
        reference_price=100.0, trading_date=trading_day, selected_base_amount_usd=1.0,
    )
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(state_root),
        prior_allocation_roots=(str(cron_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "ledger")
    slot_key = ex._credit_idempotency_key(now, manual=False)
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 1.0, idempotency_key=slot_key
    )
    ex.credit_ledger.settle(
        "AAPL", trading_day.isoformat(), 0.80, final_session=False, stream="morning",
    )

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )
    outcomes = ex._compute_top_up(now, [signal])
    outcome = outcomes["AAPL"]

    # desired ($5) - prior.amount_usd ($0.20) - settled_credit_for_slot_usd
    # ($0.80) = $4.00: the settled amount is still subtracted since the
    # stale $0.20 prior does not yet reflect it.
    assert outcome.prior_same_day_amount == 0.20
    assert outcome.incremental_order_amount == 4.0


def test_morning_compute_top_up_reconciles_a_zeroed_preserve_entry_on_revival(tmp_path):
    # Codex review, PR #656 discussion_r3838839294 ("Reconcile zeroed
    # preserve entries before re-preserving"): a preserve entry that a PRIOR
    # retry already reconciled down to $0.00 (a temporary price recovery)
    # still EXISTS in the ledger under this slot's idempotency key. Gating
    # the reconcile call on preserved_credit_for_slot() > 0 alone treats that
    # zeroed-but-existing entry as "nothing to reconcile" -- so when THIS
    # retry's price declines again and recomputes a fresh positive credit,
    # the revived amount must still reach reconcile_preserved_credit here,
    # or it is later silently dropped by preserve_pending()'s own dedup
    # check finding the stale entry already present.
    from dca_cumulative import CumulativeCreditLedger

    prior_root = tmp_path / "prior"
    trading_day = date(2026, 7, 8)
    now = datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN)
    _seed_prior(
        prior_root, "AAPL", 1.0,
        reference_price=100.0, trading_date=trading_day, selected_base_amount_usd=1.0,
    )
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=1.0,
        morning_run_state_root=str(tmp_path / "live"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "ledger")
    slot_key = ex._credit_idempotency_key(now, manual=False)

    # An earlier attempt of THIS SAME slot preserved a $1.00 threshold-
    # crossing credit that got blocked before placement...
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 1.00, idempotency_key=slot_key, stream="morning",
    )
    # ...then a later retry (before this one) reconciled it down to $0.00
    # because the price temporarily recovered.
    ex.credit_ledger.reconcile_preserved_credit(
        "AAPL", trading_day.isoformat(), slot_key, 0.0,
        expected_settled_credit_usd=0.0, expected_outstanding_credit_usd=1.00,
        expected_pending_usd=1.00, stream="morning", recorded_at=now.isoformat(),
    )
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    assert ex.credit_ledger.preserved_credit_for_slot("AAPL", trading_day.isoformat(), slot_key) == 0.0
    assert ex.credit_ledger.has_preserved_entry_for_slot("AAPL", trading_day.isoformat(), slot_key) is True

    # THIS retry's price has fallen again, producing a small ($0.10) fresh
    # sub-dollar decline that stays pending (below the $0.50 discard/round-up
    # threshold) rather than crossing to an order.
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.90,
        previous_close=100.0,
        drop_pct=0.1,
        multiplier=1.1,
        hypothetical_amount_usd=1.1,
        selected_base_amount_usd=1.0,
    )
    outcomes = ex._compute_top_up(now, [signal])
    outcome = outcomes["AAPL"]

    assert outcome.cumulative_credit_decline_usd == 0.10
    assert outcome.incremental_order_amount == 0.0
    # The revived $0.10 decline must actually reach the ledger's shared
    # pending balance via reconcile_preserved_credit -- not be silently
    # dropped because preserve_pending()'s dedup found the stale zeroed
    # entry and returned early.
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.10
    assert ex.credit_ledger.preserved_credit_for_slot("AAPL", trading_day.isoformat(), slot_key) == 0.10
    events = ex.credit_ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "reconciled_preserved_pending"


def test_morning_scheduled_duplicate_recovery_settlement_is_idempotent(tmp_path):
    # Codex review: "Make duplicate-recovery settlement idempotent". If the
    # ORIGINAL attempt's own settle() already applied this exact broker order
    # (e.g. it succeeded but the process crashed before the run artifact was
    # persisted), a later retry that reaches the same-stream duplicate-
    # recovery path must not settle() it a SECOND time and double-subtract
    # from the shared pending balance.
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _DuplicateBroker:
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {"id": "ORDER-AAPL", "dollar_amount": 1.0}

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):  # pragma: no cover
            raise AssertionError("a genuine duplicate must never place a new order")

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    slot_key = "morning-2026-07-08-1030"
    ex.credit_ledger.preserve_pending(
        "AAPL", trading_day.isoformat(), 0.20, idempotency_key=slot_key
    )
    # Simulate the ORIGINAL attempt's own settle() call already having
    # applied this exact recovered order before the crash.
    ex.credit_ledger.settle(
        "AAPL",
        trading_day.isoformat(),
        1.0,
        final_session=False,
        stream="morning",
        order_id="ORDER-AAPL",
    )
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=5.0,
        selected_base_amount_usd=1.0,
    )
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=5.0,
        decision_price=90.0,
        prior_same_day_amount=1.0,
        prior_same_day_reference_price=100.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.20,
    )
    broker = _DuplicateBroker()
    outcomes = ex._place_broker_orders(
        [signal],
        trading_day,
        broker,
        {"AAPL": outcome},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )
    assert outcomes["AAPL"]["status"] == "submitted"
    # The already-settled balance must stay at zero -- the repeat settle()
    # call for the same order id must be a no-op, not a second deduction.
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    events = ex.credit_ledger.events(trading_day.isoformat())
    assert sum(1 for e in events if e["decision"] == "settled") == 1


def test_morning_top_up_guard_buys_incremental_when_prior_fill_exists(tmp_path):
    # Prior manual fill of $4.50 at $99.0 before the 10:30 AM live run. Stock
    # has dropped 3% to $97.0 (below the $99.0 reference). 3% drop → 1.5x →
    # desired $4.50 = prior $4.50, so ladder_incremental is $0. The price-step
    # floor (max($1, $99-$97=$2) = $2) applies as a sub-dollar credit, placing
    # $2.00.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _Broker(FakeLiveBroker):
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Guarded top-ups use the narrower same-stream ref-id duplicate check;
            # this fixture never has prior broker orders on file, so it always
            # proceeds as a fresh placement.
            return None
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
    # desired = 1.5x * $3.00 = $4.50; ladder_inc = $4.50 - $4.50 = $0;
    # price_step = max($1, $99-$97=$2) = $2 → incremental = $2.00
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    candidates = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert "TOPUP" in candidates, "TOPUP should appear as a buy candidate"
    row = candidates["TOPUP"]
    assert row.get("top_up_guard_result") == TOP_UP_INCREMENTAL
    assert row.get("incremental_order_amount") == 2.0
    assert broker.placed.get("TOPUP") == 2.0, "broker should place $2.00 incremental order"


def test_morning_duplicate_recovery_settles_the_preserved_credit(tmp_path):
    # If the first scheduled top-up attempt reaches the broker but dies before
    # its settle() call, a retry lands in the duplicate branch and records the
    # broker order as submitted. That outcome carries status="submitted", so
    # the post-loop preserve pass skipped it, and it never reaches the settle()
    # call in the placement path either -- so the credit that ALREADY funded a
    # real order stayed pending, and the 15:30 aggregate pass could buy it a
    # second time (Codex review: "Settle preserved credit when recovering a
    # duplicate").
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _DuplicateBroker(FakeLiveBroker):
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            # The first attempt's order is already sitting at the broker.
            return {"id": "ORDER-ALREADY-PLACED", "dollar_amount": 1.0}

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):  # pragma: no cover
            raise AssertionError("a duplicate must never place a second order")

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    # The credit the crashed first attempt left pending after it had already
    # funded the real broker order.
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.30

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.0,
        previous_close=100.0,
        drop_pct=1.0,
        multiplier=1.0,
        hypothetical_amount_usd=1.0,
        selected_base_amount_usd=1.0,
    )
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=1.30,
        decision_price=99.0,
        prior_same_day_amount=1.0,
        prior_same_day_reference_price=100.0,
        prior_same_day_run_id="morning_am",
        prior_same_day_source="morning",
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.30,
    )
    broker = _DuplicateBroker()
    outcomes = ex._place_broker_orders(
        [signal],
        trading_day,
        broker,
        {"AAPL": outcome},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert outcomes["AAPL"]["status"] == "submitted"
    assert outcomes["AAPL"]["duplicate_preserved"] is True
    assert outcomes["AAPL"]["order_id"] == "ORDER-ALREADY-PLACED"
    # The credit is settled against the recovered order, not left pending for
    # the 15:30 aggregate pass to spend a second time.
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    assert ex.credit_ledger.events(trading_day.isoformat())[-1]["decision"] == "settled"

    # Repeated recoveries settle EXACTLY ONCE for this order id. Clearing down
    # to zero is not enough on its own: if another stream adds fresh pending
    # credit for the ticker in between, a second settlement would consume that
    # instead, and it never funded this order (Codex review: "Settle a
    # recovered duplicate only once").
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.45, stream="midday")
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.45

    ex._place_broker_orders(
        [signal],
        trading_day,
        broker,
        {"AAPL": outcome},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    # The other stream's $0.45 is untouched.
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.45


def test_morning_duplicate_recovery_settles_once_for_an_order_with_no_id(tmp_path):
    """A recovered duplicate whose broker dict carries NO usable id must still
    be settled exactly once.

    Two independent fixes settle a recovered duplicate: _process_candidate's
    own call (keyed on recovered_order_id) and the post-loop credit pass's
    (keyed on outcome["order_id"]). The post-loop guard -- top_up present,
    cumulative_credit_decline_usd present, incremental_order_amount > 0 -- is a
    strict SUBSET of _process_candidate's, so it fired on every recovery that
    had already settled. For an order dict carrying a real id the ledger's
    order-id dedup absorbed the repeat, but when the dict has no id the two
    disagree: _process_candidate falls back to the synthetic "legacy-dup-..."
    key while the post-loop pass passes order_id=None, which disables
    settle()'s `if order_id:` dedup entirely and subtracts the amount from the
    shared pending pool a second time -- consuming credit that never funded
    this order (Codex review: "Make duplicate-recovery settlement idempotent";
    "Require an ID for idempotent duplicate settlement").

    The ``duplicate_credit_settled`` flag closed that gap, but its own
    regression tests all use a duplicate carrying a REAL order id, where the
    ledger's dedup masks a second settle() and only the audit event goes
    wrong. This case is the one where the flag is load-bearing for the
    BALANCE: without it the repeat is not deduped at all and real credit is
    destroyed, so pin it separately.
    """
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _IdlessDuplicateBroker(FakeLiveBroker):
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # A real order dict, but with no "id"/"order_id" field at all.
            return {"dollar_amount": 1.0}

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):  # pragma: no cover
            raise AssertionError("a duplicate must never place a second order")

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.0,
        previous_close=100.0,
        drop_pct=1.0,
        multiplier=1.0,
        hypothetical_amount_usd=1.0,
        selected_base_amount_usd=1.0,
    )
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=1.30,
        decision_price=99.0,
        prior_same_day_amount=1.0,
        prior_same_day_reference_price=100.0,
        prior_same_day_run_id="morning_am",
        prior_same_day_source="morning",
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.30,
    )
    broker = _IdlessDuplicateBroker()
    outcomes = ex._place_broker_orders(
        [signal], trading_day, broker, {"AAPL": outcome}, remaining_daily=180.0, remaining_weekly=900.0
    )

    assert outcomes["AAPL"]["duplicate_preserved"] is True
    assert outcomes["AAPL"]["order_id"] is None
    # Settled exactly once: the $0.30 that funded the real order is cleared,
    # and the settlement is recorded as such rather than as an ignored repeat.
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    assert ex.credit_ledger.events(trading_day.isoformat())[-1]["decision"] == "settled"

    # Another stream's fresh credit must survive this recovery -- the second,
    # unkeyed settle() used to consume it.
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.45, stream="midday")
    ex._place_broker_orders(
        [signal], trading_day, broker, {"AAPL": outcome}, remaining_daily=180.0, remaining_weekly=900.0
    )
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.45


def test_morning_original_settlement_is_keyed_so_the_first_recovery_is_deduped(tmp_path):
    # The order-scoped guard has to be written by the attempt that PLACED the
    # order, not only by the recovery that rediscovers it. Writing it from the
    # recovery branch alone would leave the FIRST recovery after a successful
    # original settlement undeduplicated: it would find no key, settle again,
    # and consume whatever fresh credit another stream had added in the
    # meantime (Codex review: "Key the original settlement with the broker
    # order ID").
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _PlacingBroker(FakeLiveBroker):
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return "ORDER-AAPL"

    class _DuplicateBroker(_PlacingBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # The retry rediscovers the SAME physical order.
            return {"id": "ORDER-AAPL", "dollar_amount": 1.0}

        def place_market_buy(self, ticker, notional_usd, ref_id):  # pragma: no cover
            raise AssertionError("a duplicate must never place a second order")

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    signal = MorningSignal(
        ticker="AAPL", source="finance", action=Action.BUY, reason="test",
        current_price=99.0, previous_close=100.0, drop_pct=1.0, multiplier=1.0,
        hypothetical_amount_usd=1.0, selected_base_amount_usd=1.0,
    )
    outcome = TopUpOutcome(
        ticker="AAPL", desired_amount=1.30, decision_price=99.0,
        prior_same_day_amount=1.0, prior_same_day_reference_price=100.0,
        prior_same_day_run_id="morning_am", prior_same_day_source="morning",
        incremental_order_amount=1.0, top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False, effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.30,
    )

    # The ORIGINAL placement settles successfully, keyed on ORDER-AAPL.
    ex._place_broker_orders(
        [signal], trading_day, _PlacingBroker(), {"AAPL": outcome},
        remaining_daily=180.0, remaining_weekly=900.0,
    )
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0

    # Another stream now adds fresh credit for the same ticker.
    # Sub-threshold so it stays PENDING rather than flushing to an order.
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.45, stream="midday")
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.45

    # The FIRST recovery of that same order must be deduped, not settle again.
    ex._place_broker_orders(
        [signal], trading_day, _DuplicateBroker(), {"AAPL": outcome},
        remaining_daily=180.0, remaining_weekly=900.0,
    )
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.45


def test_morning_place_broker_orders_settles_credit_ledger_after_confirmed_submission(tmp_path):
    # Codex review discussion_r3705349440 ("Route sub-dollar top-ups through
    # the credit policy"): once a credit-derived order is actually confirmed
    # submitted, the durable ledger must be settled (cleared) using the real
    # submitted notional -- mirroring near_close_dca's own "Defer ledger
    # updates until the submitted amount is known" fix. The pre-existing
    # $0.25 pending balance this test seeds must NOT still be sitting there
    # after a confirmed $1.00 submission.
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_INCREMENTAL

    class _Broker(FakeLiveBroker):
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return None

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.25, stream="morning")
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.25

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.0,
        previous_close=100.0,
        drop_pct=1.0,
        multiplier=1.0,
        hypothetical_amount_usd=1.0,
        selected_base_amount_usd=1.0,
    )
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=1.30,
        decision_price=99.0,
        prior_same_day_amount=1.0,
        prior_same_day_reference_price=100.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.30,
    )
    broker = _Broker()
    outcomes = ex._place_broker_orders(
        [signal],
        trading_day,
        broker,
        {"AAPL": outcome},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )
    assert outcomes["AAPL"]["status"] == "submitted"
    assert broker.placed["AAPL"] == 1.0
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    events = ex.credit_ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "settled"
    assert events[-1]["submitted_amount_usd"] == 1.0


def test_morning_terminal_partial_fill_settles_credit_ledger(tmp_path):
    """A terminal partial fill must not leave its consumed credit pending."""
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import TOP_UP_INCREMENTAL, MarketBuyPlacementTerminalError

    class _PartiallyFillingBroker(FakeLiveBroker):
        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return None

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise MarketBuyPlacementTerminalError(
                "cancelled after partial fill",
                {
                    "id": "ORDER-PARTIAL",
                    "cumulative_quantity": "0.01",
                    "average_price": "50.00",
                },
            )

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.50, stream="morning")
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=50.0,
        previous_close=51.0,
        drop_pct=1.0,
        multiplier=1.0,
        hypothetical_amount_usd=1.0,
        selected_base_amount_usd=1.0,
    )
    top_up = TopUpOutcome(
        ticker="AAPL",
        desired_amount=1.0,
        decision_price=50.0,
        prior_same_day_amount=0.0,
        prior_same_day_reference_price=51.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.50,
    )

    outcomes = ex._place_broker_orders(
        [signal],
        trading_day,
        _PartiallyFillingBroker(),
        {"AAPL": top_up},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert outcomes["AAPL"]["status"] == "submitted"
    assert outcomes["AAPL"]["submitted_amount_usd"] == 0.50
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    events = ex.credit_ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "settled"
    assert events[-1]["submitted_amount_usd"] == 0.50


def test_morning_place_broker_orders_quarantines_credits_on_ambiguous_placement(tmp_path):
    """An ambiguous placement must settle/quarantine its credit rather than calling preserve_pending."""
    from dca_cumulative import CumulativeCreditLedger
    from morning_dca import TOP_UP_INCREMENTAL, TopUpOutcome
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _AmbiguousBroker(FakeLiveBroker):
        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise RuntimeError("network timeout after order acceptance")

    trading_day = date(2026, 7, 8)
    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.25, stream="morning")
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.25

    signal = _isolation_signal("AAPL")
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=1.0,
        decision_price=90.0,
        prior_same_day_amount=0.0,
        prior_same_day_reference_price=None,
        prior_same_day_run_id=None,
        prior_same_day_source=None,
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.30,
    )
    broker = _AmbiguousBroker()
    outcomes = ex._place_broker_orders(
        [signal],
        trading_day,
        broker,
        {"AAPL": outcome},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )
    assert outcomes["AAPL"]["status"] == "failed"
    assert outcomes["AAPL"]["ambiguous_placement_exception"] is True
    # The pending credit was consumed/settled rather than restored by preserve_pending
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    events = ex.credit_ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "settled"
    assert events[-1]["submitted_amount_usd"] == 1.0


def _isolation_signal(ticker):
    return MorningSignal(
        ticker=ticker,
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=90.0,
        previous_close=100.0,
        drop_pct=10.0,
        multiplier=5.0,
        hypothetical_amount_usd=2.0,
        selected_base_amount_usd=2.0,
    )


def test_settled_slot_credit_is_deducted_before_the_minimum_order_threshold():
    """A partial settlement must leave credit pending, not make a $0.60 order."""
    from near_close_dca import PriorSameDayAllocation

    ex = MorningDCAExecutor(MorningConfig(tickers=("AAPL",)), mode=MORNING_LIVE_MODE)
    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.60,
        previous_close=100.0,
        drop_pct=0.4,
        multiplier=1.8,
        hypothetical_amount_usd=1.8,
        selected_base_amount_usd=1.0,
    )
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=100.0,
        run_id="prior",
        source="morning",
        selected_base_amount_usd=1.0,
    )

    outcome = ex._evaluate_top_up(
        signal,
        prior,
        settled_credit_for_slot_usd=0.40,
    )

    assert outcome.incremental_order_amount == 0.0
    assert outcome.top_up_skipped is True
    assert outcome.cumulative_credit_decline_usd == 0.40


def test_credit_ledger_read_failure_aborts_top_up_computation(tmp_path):
    """An unreadable ledger is a run failure, never a normal top-up skip."""

    class _UnreadableLedger:
        def recorded_decision(self, *args, **kwargs):
            raise OSError("ledger unavailable")

    cfg = MorningConfig(
        tickers=("AAPL",),
        prior_allocation_roots=(str(tmp_path / "prior"),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = _UnreadableLedger()

    with pytest.raises(RuntimeError, match="ledger idempotency read failed"):
        ex._compute_top_up(_now_live(), [_isolation_signal("AAPL")])


def test_an_ambiguous_placement_exception_halts_the_executor_batch(tmp_path):
    """A connector may accept an order and then raise before returning its id.

    The executor cannot account for that possible spend, so all four morning
    streams must halt rather than continuing until the failure-streak limit.
    """
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _RefusingBroker(FakeLiveBroker):
        def __init__(self):
            self.attempted = []
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.attempted.append(ticker)
            if ticker == "GS":
                raise RuntimeError("rejected by broker")
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    cfg = MorningConfig(tickers=("AAPL", "GS", "EVR"), base_buy_amount_usd=2.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    broker = _RefusingBroker()

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in ("AAPL", "GS", "EVR")],
        date(2026, 7, 8),
        broker,
        None,
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert broker.attempted == ["AAPL", "GS"]
    assert outcomes["AAPL"]["status"] == "submitted"
    assert outcomes["GS"]["status"] == "failed"
    assert outcomes["GS"]["skip_reason"] == "ambiguous placement exception: RuntimeError"
    assert outcomes["GS"]["ambiguous_placement_exception"] is True
    assert outcomes["GS"]["placement_breaker_halted"] is True
    assert outcomes["EVR"]["status"] == "skipped"
    assert outcomes["EVR"]["placement_breaker_halted"] is True
    assert "may have left an accepted order" in outcomes["EVR"]["skip_reason"]
    # No retry: the refused ticker is attempted exactly once.
    assert broker.attempted.count("GS") == 1


def test_run_persists_ambiguous_placement_marker_in_candidate_rows(tmp_path):
    """The durable candidate row must retain an ambiguous spend reservation."""
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _AmbiguousBroker(FakeLiveBroker):
        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise RuntimeError("response lost after placement")

    cfg = MorningConfig(tickers=("AAPL",), morning_run_state_root=str(tmp_path))
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.02})

    result = ex.run(_now_live(), data, broker=_AmbiguousBroker(), manual=True)

    row = result["artifacts"]["buy_candidates"][0]
    assert row["ambiguous_placement_exception"] is True
    persisted = json.loads(Path(result["paths"]["run_state"]).read_bytes())
    assert persisted["buy_candidates"][0]["ambiguous_placement_exception"] is True
    assert result["artifacts"]["budget"]["hypothetical_day_spend_usd"] == 3.0
    assert result["artifacts"]["budget"]["hypothetical_week_spend_usd"] == 3.0
    assert ex.ledger.day_planned(_now_live().date()) == 3.0
    assert ex.ledger.week_planned(_now_live().date()) == 3.0


def test_a_definitive_placement_rejection_uses_the_failure_streak(tmp_path):
    """A proven no-order response is ticker-scoped, not acceptance-ambiguous."""
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, OrderPlacementRejectedError

    class _RejectingBroker(FakeLiveBroker):
        def __init__(self):
            self.attempted = []

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.attempted.append(ticker)
            if ticker == "GS":
                raise OrderPlacementRejectedError("explicit success=false")
            return f"ORDER-{ticker}"

    cfg = MorningConfig(
        tickers=("AAPL", "GS", "EVR"),
        base_buy_amount_usd=2.0,
        max_consecutive_placement_failures=2,
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    broker = _RejectingBroker()

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in ("AAPL", "GS", "EVR")],
        date(2026, 7, 8),
        broker,
        None,
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert broker.attempted == ["AAPL", "GS", "EVR"]
    assert outcomes["GS"]["status"] == "failed"
    assert outcomes["GS"]["skip_reason"] == "broker rejected placement: OrderPlacementRejectedError"
    assert "ambiguous_placement_exception" not in outcomes["GS"]
    assert outcomes["EVR"]["status"] == "submitted"


def test_consecutive_placement_failures_halt_the_executor_batch(tmp_path):
    """The streak breaker must fire in the LIVE Python executors too, not only
    in dca_runner (which no prompt calls today) and in prompt prose -- this
    loop is inherited by the market-open, midday, and intraday streams (Codex
    review, PR #786: "Enforce the streak breaker in every live executor")."""
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, OrderConfirmationRequiredError

    class _DownBroker(FakeLiveBroker):
        def __init__(self):
            self.attempted = []

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.attempted.append(ticker)
            raise OrderConfirmationRequiredError("broker unavailable")

    tickers = ("AAPL", "GS", "EVR", "IBM", "MSFT")
    cfg = MorningConfig(
        tickers=tickers, base_buy_amount_usd=2.0, max_consecutive_placement_failures=3
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    broker = _DownBroker()

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in tickers],
        date(2026, 7, 8),
        broker,
        None,
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert len(broker.attempted) == 3, "the breaker must stop the batch, not attempt all five"
    assert all(
        outcomes[t]["status"] == "not_submitted_confirmation_required"
        for t in broker.attempted
    )
    # The unattempted remainder is RECORDED, not simply absent -- see
    # test_the_breaker_preserves_credits_for_candidates_it_skips for why that
    # matters beyond reporting.
    unattempted = [t for t in tickers if t not in broker.attempted]
    assert unattempted, "fixture must leave some candidates unattempted"
    for ticker in unattempted:
        row = outcomes[ticker]
        assert row["status"] == "skipped"
        assert row["placement_breaker_halted"] is True
        assert row["deliberate_skip"] is True
        assert "consecutive placement failures" in row["skip_reason"]


def test_the_breaker_preserves_credits_for_candidates_it_skips(tmp_path):
    """The threshold-crossing credit-preservation pass skips any ticker whose
    outcome is not a dict, and that pass is the ONLY place such a credit is
    durably recorded. Leaving breaker-skipped candidates absent from `outcomes`
    therefore lost the credit outright, shorting a later slot's order (Codex
    review, PR #786: "Preserve credits for candidates skipped by the
    breaker")."""
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import (
        ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
        TOP_UP_INCREMENTAL,
        OrderConfirmationRequiredError,
    )

    class _DownBroker(FakeLiveBroker):
        def __init__(self):
            self.attempted = []

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.attempted.append(ticker)
            raise OrderConfirmationRequiredError("broker unavailable")

    tickers = ("AAPL", "GS", "EVR", "IBM", "MSFT")
    cfg = MorningConfig(
        tickers=tickers, base_buy_amount_usd=2.0, max_consecutive_placement_failures=3
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    broker = _DownBroker()

    # The LAST ticker -- one the breaker will never reach -- carries a credit
    # that has crossed the order threshold.
    stranded = tickers[-1]
    top_up_outcomes = {
        stranded: TopUpOutcome(
            ticker=stranded,
            desired_amount=2.0,
            decision_price=90.0,
            prior_same_day_amount=1.0,
            prior_same_day_reference_price=100.0,
            prior_same_day_run_id="manual_am",
            prior_same_day_source="manual",
            incremental_order_amount=1.0,
            top_up_guard_result=TOP_UP_INCREMENTAL,
            top_up_skipped=False,
            effective_base_amount_usd=1.0,
            cumulative_credit_decline_usd=0.40,
        )
    }

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in tickers],
        trading_day,
        broker,
        top_up_outcomes,
        remaining_daily=180.0,
        remaining_weekly=900.0,
        now=datetime(2026, 7, 8, 10, 30, tzinfo=EASTERN),
    )

    assert len(broker.attempted) == 3, "the breaker must stop the batch"
    assert stranded not in broker.attempted, "fixture must leave the credit ticker unattempted"
    # The skipped remainder is recorded...
    assert outcomes[stranded]["status"] == "skipped"
    assert outcomes[stranded]["placement_breaker_halted"] is True
    # ...which is what lets the preservation pass see it and keep the credit.
    assert ex.credit_ledger.pending_usd(stranded, trading_day.isoformat()) == 0.40


def test_a_success_between_failures_resets_the_executor_streak(tmp_path):
    """Cumulative counting would re-create halt-on-first-rejection on any batch
    with a few independently bad symbols; only an UNBROKEN run is systemic."""
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, OrderConfirmationRequiredError

    class _AlternatingBroker(FakeLiveBroker):
        def __init__(self):
            self.attempted = []

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.attempted.append(ticker)
            if len(self.attempted) % 2 == 1:
                raise OrderConfirmationRequiredError("rejected")
            return f"ORDER-{ticker}"

    tickers = ("AAPL", "GS", "EVR", "IBM", "MSFT", "NVDA")
    cfg = MorningConfig(
        tickers=tickers, base_buy_amount_usd=2.0, max_consecutive_placement_failures=3
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    broker = _AlternatingBroker()

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in tickers],
        date(2026, 7, 8),
        broker,
        None,
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert len(broker.attempted) == len(tickers), "a success between failures resets the streak"
    assert sum(o["status"] == "submitted" for o in outcomes.values()) == 3


def test_confirmation_required_placements_count_toward_the_executor_streak(tmp_path):
    """A broker refusing every direct submission produced no order and is a
    real broker-facing failure, so it counts toward the streak exactly like a
    raised placement -- only a SUCCESS resets it (Codex review, PR #786:
    "Count confirmation-required placement failures")."""
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, OrderConfirmationRequiredError

    class _ConfirmationBroker(FakeLiveBroker):
        def __init__(self):
            self.attempted = []

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.attempted.append(ticker)
            raise OrderConfirmationRequiredError("confirmation required")

    tickers = ("AAPL", "GS", "EVR", "IBM", "MSFT")
    cfg = MorningConfig(
        tickers=tickers, base_buy_amount_usd=2.0, max_consecutive_placement_failures=3
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    broker = _ConfirmationBroker()

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in tickers],
        date(2026, 7, 8),
        broker,
        None,
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert len(broker.attempted) == 3, "the breaker must stop the batch after three refusals"
    assert all(
        outcomes[t]["status"] == "not_submitted_confirmation_required" for t in broker.attempted
    )


def test_every_live_stream_config_loads_the_streak_breaker():
    """FIVE separate config loaders reach the live placement loops -- morning,
    market-open, midday, intraday, and the afternoon StrategyConfig. midday has
    its OWN loader that neither shared one covers, so a knob added to only some
    of them silently leaves that stream's breaker at None (Codex review,
    PR #786: "Load the streak breaker in the midday configuration")."""
    import dca_runner
    from dca_config import get_config_dir, load_strategy_config
    from intraday_dca import load_intraday_config
    from market_open_dca import load_market_open_config
    from midday_dca import load_midday_config

    expected = dca_runner.MAX_CONSECUTIVE_PLACEMENT_FAILURES
    config_dir = get_config_dir()

    assert load_strategy_config(config_dir).max_consecutive_placement_failures == expected
    assert (
        load_morning_config(config_dir, mode=MORNING_LIVE_MODE).max_consecutive_placement_failures
        == expected
    )
    for loader in (load_market_open_config, load_midday_config, load_intraday_config):
        assert loader(config_dir).max_consecutive_placement_failures == expected, loader.__name__


def test_a_live_fallback_still_carries_the_shared_circuit_breakers(tmp_path):
    """budgets.json is a separate file from morning_dca.json.

    `load_morning_config` returns early twice -- morning_dca.json missing, and
    morning_dca.json unparseable -- and both returns ran before the breakers
    were read, so a valid budgets.json was ignored entirely. A live run pinned
    past either fallback (`live_execution_enabled` defaults to True) then kept
    attempting the whole batch through a broker outage with the breaker unset
    (Codex review, PR #786: "Load the breaker before morning-config
    fallbacks").

    All THREE breakers were lost, not just the one reported -- and
    `max_per_order_usd` is the one that stings: it did not go unset, it ROSE
    from the configured $4 to the $10 dataclass default. A missing stream
    config silently raising a spend ceiling is round 43's harm, arriving by a
    different route."""
    import json as _json

    (tmp_path / "budgets.json").write_text(
        _json.dumps({"circuit_breakers": {
            "max_per_order_usd": 4.0,
            "max_orders_per_day": 6,
            "max_consecutive_placement_failures": 2,
        }}),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "morning_dca.json"

    def load(mode=MORNING_LIVE_MODE):
        return load_morning_config(
            config_dir=str(tmp_path), mode=mode, allow_unpinned_account=True
        )

    # 1. morning_dca.json missing, 2. unparseable, 3. present and valid.
    cfg_path.write_text(_json.dumps({"live_task": {}}), encoding="utf-8")
    valid = load()
    cfg_path.unlink()
    missing = load()
    cfg_path.write_text("{ not json", encoding="utf-8")
    unparseable = load()

    for label, cfg in (("missing", missing), ("unparseable", unparseable),
                       ("valid", valid)):
        assert cfg.max_per_order_usd == 4.0, label
        assert cfg.max_orders_per_day == 6, label
        assert cfg.max_consecutive_placement_failures == 2, label
        # The fallbacks are a LIVE path -- which is what makes losing the
        # breakers matter rather than being merely untidy.
        assert cfg.live_execution_enabled is True, label

    # Watchlist/preflight never reach broker placement, and must stay off this
    # path so a malformed budgets.json cannot block the 9:45 preflight from
    # writing the artifact the 10:30 live gate requires.
    cfg_path.unlink()
    preflight = load(MORNING_PREFLIGHT_MODE)
    assert preflight.max_orders_per_day is None
    assert preflight.max_consecutive_placement_failures is None

    # And a malformed budgets.json now fails closed on the fallback path too,
    # where previously it was never read at all.
    (tmp_path / "budgets.json").write_text(
        _json.dumps({"circuit_breakers": {"max_consecutive_placement_failures": True}}),
        encoding="utf-8",
    )
    with pytest.raises((ValueError, RuntimeError)):
        load()


def test_the_executor_streak_breaker_is_loaded_from_budgets_json(tmp_path):
    """Same knob dca_runner reads, so the two cannot disagree about when a live
    batch stops."""
    import json as _json

    import dca_runner

    (tmp_path / "budgets.json").write_text(
        _json.dumps({"circuit_breakers": {"max_consecutive_placement_failures": 4}}),
        encoding="utf-8",
    )
    assert load_max_consecutive_placement_failures(tmp_path) == 4
    assert (
        load_max_consecutive_placement_failures(tmp_path / "missing")
        == DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES
    )

    (tmp_path / "budgets.json").write_text(
        _json.dumps({"circuit_breakers": {"max_consecutive_placement_failures": True}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_consecutive_placement_failures"):
        load_max_consecutive_placement_failures(tmp_path)

    # The shipped config is what both live paths actually run on.
    from dca_config import get_config_dir

    assert (
        load_max_consecutive_placement_failures(get_config_dir())
        == dca_runner.MAX_CONSECUTIVE_PLACEMENT_FAILURES
    )


def test_a_tradability_refusal_does_not_stop_the_executor_batch(tmp_path):
    """The RVII shape specifically: a symbol the broker will not sell
    fractionally is blocked, and the rest of the batch still buys."""
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _PartlyUntradableBroker(FakeLiveBroker):
        def __init__(self):
            self.placed = {}

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def is_tradable_for_fractional_buy(self, ticker):
            return ticker != "GS"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    cfg = MorningConfig(tickers=("AAPL", "GS", "EVR"), base_buy_amount_usd=2.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    broker = _PartlyUntradableBroker()

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in ("AAPL", "GS", "EVR")],
        date(2026, 7, 8),
        broker,
        None,
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert sorted(broker.placed) == ["AAPL", "EVR"]
    assert outcomes["GS"]["status"] == "blocked"
    assert outcomes["GS"]["skip_reason"] == "not tradable for fractional buy"


def test_morning_top_up_guard_skips_when_price_not_below_reference(tmp_path):
    # Prior manual fill at $96.0 reference. Morning run current price $97.0 is
    # ABOVE $96.0 → TOP_UP_PRICE_NOT_BELOW → $0 order, broker not called.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_PRICE_NOT_BELOW

    class _Broker(FakeLiveBroker):
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
    assert row.get("hypothetical_amount_usd") == 4.5


def test_morning_top_up_guard_seeds_ledger_from_prior_fills(tmp_path):
    # Prior MORNING fill of $85 on a DIFFERENT ticker (PRIOR_TICKER) before the
    # 10:30 live run. Both prior_allocation_roots and cap_seed_roots point to the
    # same morning root. The $90 daily cap has only $5 remaining. TOPUP graded at
    # 2.5x * $3 = $7.50 desired, which exceeds the $5 remaining → cap_exceeded.
    # This verifies that prior fills on other tickers reduce the ledger cap, not
    # just the per-ticker incremental offset.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker(FakeLiveBroker):
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
    # $86 prior morning fill on a different ticker at a reference price well below
    # current so per-ticker top-up would be eligible, but the cap is the binding constraint.
    _write_prior_run_state(morning_root, "PRIOR_TICKER", amount_usd=86.0, decision_price=50.0)
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
    # 3% drop: TOPUP desired = 1.5x * $3 = $4.50; remaining cap = $90 - $86 = $4
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "cap_exceeded", (
        "TOPUP ($4.50 desired) should be cap-gated when prior morning fill consumed $86 of the $90 daily cap"
    )
    assert "TOPUP" not in broker.placed, "broker should not be called when cap is exhausted"


def test_morning_cap_seeding_counts_every_prior_buy_attempt_not_one_per_ticker(tmp_path):
    # Codex review discussion_r3705349432 ("Seed the cap from actual order
    # attempts"): the cap-seeding block previously derived
    # MorningConfig.max_orders_per_day's seed from "one attempt per DISTINCT
    # ticker with a positive prior allocation" -- collapsing two same-day
    # buys of MULTI into a single attempt, and missing FAILTICK's failed
    # attempt entirely since it moved no capital (amount_usd stays 0). Two
    # prior "filled" rows for MULTI plus one "failed" row for FAILTICK is a
    # real attempt count of 3, not the 1 the old "one per ticker with a
    # positive allocation" approximation would have produced.
    prior_root = tmp_path / "prior"
    first_dir = prior_root / "20260708_0900ET"
    first_dir.mkdir(parents=True)
    (first_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",
            "generated_at": "2026-07-08T09:00:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [
                {
                    "ticker": "MULTI",
                    "status": "filled",
                    "planned_amount_usd": 1.0,
                    "decision_price": 50.0,
                    "selected_base_amount_usd": 1.0,
                },
                {
                    "ticker": "FAILTICK",
                    "status": "failed",
                },
            ],
        }),
        encoding="utf-8",
    )
    second_dir = prior_root / "20260708_0930ET"
    second_dir.mkdir(parents=True)
    (second_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",
            "generated_at": "2026-07-08T09:30:00-04:00",
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "buy_candidates": [
                {
                    "ticker": "MULTI",
                    "status": "filled",
                    "planned_amount_usd": 1.0,
                    "decision_price": 49.0,
                    "selected_base_amount_usd": 1.0,
                },
            ],
        }),
        encoding="utf-8",
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(prior_root),),
        max_orders_per_day=8,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=EASTERN)

    ex._compute_top_up(now, ())

    assert ex.ledger.day_attempts(date(2026, 7, 8)) == 3


def test_morning_top_up_guard_afternoon_fills_do_not_reduce_morning_cap(tmp_path):
    # Afternoon recovery fill of $85 on AFTERNOON_TICKER recorded in a ce16ba6b-style
    # root. That root is in prior_allocation_roots (for per-ticker offset guard) but NOT
    # in cap_seed_roots (morning-only). The $90 morning daily cap must be unaffected;
    # TOPUP at $7.50 is below the $90 cap and should be placed.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker(FakeLiveBroker):
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
        cap_seed_roots=(),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _Broker()
    # 3% drop: TOPUP desired = 1.5x * $3 = $4.50; full $90 cap available
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "submitted", (
        "TOPUP should be placed: afternoon fill in prior_allocation_roots must not exhaust morning cap"
    )
    assert broker.placed.get("TOPUP") == 4.5, "full $4.50 graded order should be placed"


def test_morning_top_up_guard_inactive_in_manual_live_run(tmp_path):
    # Manual live runs must bypass the top-up guard so the normal
    # same_day_strategy_order_exists duplicate-order safeguard remains the sole
    # gate, rather than the incremental path submitting a second order.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker(FakeLiveBroker):
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

    class _Broker(FakeLiveBroker):
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
    # $86 prior fill leaves $4 of the $90 cap. TOPUP graded at $4.50 exceeds $4.
    _write_prior_run_state(morning_root, "PRIOR_TICKER", amount_usd=86.0, decision_price=50.0)
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

    # First run: $86 seeded → $4 remaining → TOPUP cap_exceeded
    broker1 = _Broker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.05})
    res1 = ex.run(_now_live(), data, broker=broker1)
    assert res1["artifacts"].get("broker_outcomes", {}).get("TOPUP", {}).get("status") == "cap_exceeded"
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 86.0, "ledger should show $86 after first run"

    # Second run on same instance with no new fills: ledger stays at $86 (no double-count)
    broker2 = _Broker()
    ex.run(_now_live(), data, broker=broker2)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 86.0, (
        "ledger must not double-count prior fills across repeated runs on the same executor instance"
    )

    # Write a new fill between runs ($4 more → total $90) — the incremental logic
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
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 90.0, (
        "ledger must pick up new fills written between runs ($86 + $4 = $90)"
    )


def test_cap_seed_scan_failure_poison_clears_on_a_recovered_rescan(tmp_path, monkeypatch):
    """Codex review ("Clear fail-closed cap poisoning after a recovered
    retry"): when a sibling stream's cap artifact is transiently unreadable,
    _compute_top_up's fail-closed branch pins planned_by_day/planned_by_week
    to the cap ceiling. A long-lived executor instance retrying against the
    SAME self.ledger after the artifact becomes readable must see real
    remaining headroom again -- not stay permanently pinned at the ceiling
    just because an earlier scan on this instance failed."""
    import morning_dca as _md
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker(FakeLiveBroker):
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def is_tradable_for_fractional_buy(self, ticker): return True
        def review_market_buy(self, ticker, notional_usd): return True, "ok"
        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    cap_root = str(tmp_path / "cap_root")
    pf_root = _seed_preflight(tmp_path / "pf", ("TOPUP",))
    cfg = MorningConfig(
        tickers=("TOPUP",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(cap_root,),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)

    real_loader = _md.load_prior_same_day_allocations_multi
    scan_state = {"n": 0}

    def _fake_loader(roots, trading_day, **kwargs):
        if roots and str(roots[0]) == cap_root:
            scan_state["n"] += 1
            skip_sink = kwargs.get("skip_sink")
            if scan_state["n"] == 1:
                # Simulate a sibling stream's cap artifact that is transiently
                # unreadable (mid-write, malformed, etc.) on the FIRST scan.
                if skip_sink is not None:
                    skip_sink.append(Path(cap_root) / "run_state.json")
                return {}
            # Second scan: the artifact is now readable with a real, modest
            # $10 fill -- far below the $90/$450 ceilings the first scan
            # poisoned the ledger with.
            return {
                "OTHER": PriorSameDayAllocation(
                    "OTHER",
                    amount_usd=10.0,
                    reference_price=50.0,
                    run_id="cap_seed_recovered",
                    source="manual",
                )
            }
        return real_loader(roots, trading_day, **kwargs)

    monkeypatch.setattr(_md, "load_prior_same_day_allocations_multi", _fake_loader)

    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})

    # First run: cap artifact unreadable -> fail closed, cap fully exhausted.
    broker1 = _Broker()
    ex.run(_now_live(), data, broker=broker1)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 90.0
    assert ex.ledger.week_planned(date(2026, 7, 8)) == 450.0
    assert ex.ledger.remaining_daily(date(2026, 7, 8)) == 0.0
    assert ex.ledger.remaining_weekly(date(2026, 7, 8)) == 0.0

    # Second run, SAME executor instance: the artifact is now readable with a
    # real $10 fill, restoring $80 of real headroom -- enough for TOPUP's
    # $4.50 desired order to place. The ledger must reflect the real $10
    # external fill plus this run's own $4.50 placed order (14.50 total), not
    # stay pinned at the $90/$450 ceilings the first (failed) scan injected.
    broker2 = _Broker()
    ex.run(_now_live(), data, broker=broker2)
    assert broker2.placed.get("TOPUP") == 4.5, (
        "TOPUP should place once real remaining headroom is restored by the recovered scan"
    )
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 14.5, (
        "the earlier fail-closed poisoning must be cleared once the scan recovers"
    )
    assert ex.ledger.week_planned(date(2026, 7, 8)) == 14.5
    assert ex.ledger.remaining_daily(date(2026, 7, 8)) == 75.5
    assert ex.ledger.remaining_weekly(date(2026, 7, 8)) == 435.5


def test_own_stream_earlier_weekday_run_state_failure_fails_closed(tmp_path):
    """Codex review ("Fail closed on unreadable own-stream prior-week
    states"): _seed_ledger_from_prior_runs's own day-loop scans THIS
    stream's own root for the current ISO week's fixed scheduled paths,
    which discovers EARLIER weekdays too (e.g. Monday's own run_state.json,
    scanned again on a Tuesday run) -- but those paths were never added to
    _earlier_weekday_sibling_paths, so an unreadable one silently skipped
    without failing closed. Neither _compute_top_up (which only ever covers
    TODAY) nor the cross-stream sibling scan (which deliberately never
    scans the current stream's own bare root, since the day-loop already
    covers it) catches this case either -- Monday's own spend could
    silently vanish from the weekly ledger. cap_seed_roots is deliberately
    None here to isolate this from the (separately tested) cross-stream
    sibling scan."""
    state_root = tmp_path / "live"
    monday_dir = state_root / "20260629_1030ET"
    monday_dir.mkdir(parents=True)
    # Malformed JSON -- caught mid-write.
    (monday_dir / "run_state.json").write_text("{not valid json", encoding="utf-8")

    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL",), trading_date=date(2026, 6, 30))
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(state_root),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 6, 30)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = datetime.combine(date(2026, 6, 30), time(10, 30), tzinfo=EASTERN)

    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)

    assert ex._cap_seed_scan_incomplete is True
    assert ex.ledger.week_planned(date(2026, 6, 30)) == 450.0


@pytest.mark.parametrize("notional", ["nan", 0, -5.0])
def test_earlier_weekday_pending_with_unusable_notional_fails_closed(tmp_path, notional):
    """An in-flight order with an unusable notional cannot be treated as zero spend."""
    state_root = tmp_path / "live"
    monday_dir = state_root / "20260629_1030ET"
    monday_dir.mkdir(parents=True)
    (monday_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-06-29",
            "run_id": "pending-with-unknown-notional",
            "buy_candidates": [
                {"ticker": "AAPL", "status": "pending", "planned_amount_usd": notional},
            ],
        }),
        encoding="utf-8",
    )
    cfg = MorningConfig(
        morning_run_state_root=str(state_root),
        cap_seed_roots=(),
        morning_weekly_cap_usd=450.0,
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = datetime.combine(date(2026, 6, 30), time(10, 30), tzinfo=EASTERN)

    ex._seed_ledger_from_prior_runs(now)

    assert ex._cap_seed_scan_incomplete is True
    assert ex.ledger.week_planned(now.date()) == 450.0


def test_seed_ledger_from_prior_runs_includes_ambiguous_placement_notional(tmp_path):
    """An ambiguous placement candidate from a prior run must be reserved in the ledger."""
    state_root = tmp_path / "live"
    monday_dir = state_root / "20260629_1030ET"
    monday_dir.mkdir(parents=True)
    state = {
        "run_id": "run-monday-ambiguous",
        "trading_day": "2026-06-29",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "failed",
                "ambiguous_placement_exception": True,
                "planned_amount_usd": 15.0,
            }
        ],
    }
    (monday_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")

    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(state_root),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 6, 30)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = datetime.combine(date(2026, 6, 30), time(10, 30), tzinfo=EASTERN)

    ex._seed_ledger_from_prior_runs(now)

    assert ex.ledger.week_planned(date(2026, 6, 30)) == 15.0


def test_cap_seed_scan_finds_sibling_suffix_fills_under_the_current_stream_root(tmp_path):
    """Codex review ("Seed the weekly cap from every intraday slot"):
    intraday_dca's three slots (11:30/13:30/14:30) all share ONE state root
    (IntradayConfig.cap_seed_roots includes intraday_dca's own root) but each
    fires with its own folder_suffix. The day-loop above only ever matches
    THIS instance's own folder_suffix, so alone it would miss an earlier
    weekday's fill written under a DIFFERENT suffix under the same root --
    and the generic cross-stream sibling scan used to skip the bare root
    entirely whenever it equaled the current stream's own state_root
    (reasoning: "the day-loop above already covers it"), which is only true
    when a stream has exactly one folder_suffix per root. For a multi-suffix
    stream like intraday, neither mechanism ever found the other suffixes'
    earlier-weekday fills, silently undercounting the shared weekly cap.

    Modeled with plain MorningConfig/MorningDCAExecutor, whose own
    folder_suffix is fixed "1030ET", against a root holding two OTHER-suffix
    prior-weekday artifacts -- exactly like a Tuesday 1:30 intraday run
    relative to Monday's 11:30 and 2:30 fills.
    """
    live_root = tmp_path / "live"

    def _write_prior_suffix_run(folder_suffix, run_id, amount):
        run_dir = live_root / f"20260706_{folder_suffix}"
        run_dir.mkdir(parents=True)
        (run_dir / "run_state.json").write_text(
            json.dumps({
                "trading_day": "2026-07-06",
                "run_id": run_id,
                "buy_candidates": [
                    {"ticker": "XXX", "status": "submitted", "planned_amount_usd": amount}
                ],
            }),
            encoding="utf-8",
        )

    # Monday 2026-07-06, ISO week 28 -- same week as _now_live()'s Wednesday.
    _write_prior_suffix_run("1130ET", "intraday_live_20260706_1130ET", 10.0)
    _write_prior_suffix_run("1430ET", "intraday_live_20260706_1430ET", 15.0)

    pf_root = _seed_preflight(tmp_path / "pf", ("CCC",))
    cfg = MorningConfig(
        tickers=("CCC",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(live_root),),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()

    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)

    assert ex.ledger.week_planned(date(2026, 7, 8)) == 25.0


def test_cap_seed_scan_reads_a_leaf_override_roots_own_run_state(tmp_path):
    """Codex review, PR #631 (issue comment "Seed run states from
    leaf override roots"): _dated_manual_override_cap_seed_roots() globs
    perplexity_primary_*/manual_override_* directly, so each cap_seed_roots
    entry it contributes IS ALREADY the leaf directory holding run_state.json
    -- not a parent with dated child directories beneath it the earlier-
    weekday day_str glob can match. Without reading the leaf's run_state.json
    directly, an override's spend from an earlier weekday in the current ISO
    week never enters candidate_paths, silently omitting it from
    planned_by_week.
    """
    live_root = tmp_path / "live"
    override_root = tmp_path / "perplexity_primary_20260706" / "manual_override_1015ET"
    override_root.mkdir(parents=True)
    (override_root / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-06",  # Monday, same ISO week as _now_live()'s Wednesday
            "run_id": "manual_override_20260706_1015ET",
            "buy_candidates": [
                {"ticker": "CCC", "status": "submitted", "planned_amount_usd": 12.0}
            ],
        }),
        encoding="utf-8",
    )

    pf_root = _seed_preflight(tmp_path / "pf", ("CCC",))
    cfg = MorningConfig(
        tickers=("CCC",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(override_root),),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()

    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)

    assert ex.ledger.week_planned(date(2026, 7, 8)) == 12.0


def test_cap_seed_scan_fails_closed_on_an_unreadable_leaf_override_state(tmp_path):
    """Codex review, PR #631 ("Fail closed on unreadable leaf override
    states"): the leaf-override direct read above used to silently
    `continue` on a truncated/malformed run_state.json -- but that leaf is
    the ONLY path capable of finding this override at all (unlike the
    same-day loader, which can fall back to mtime ordering), so a silent
    skip let the weekly scan report complete with $0 credited from spend it
    could not actually verify. Must fail closed instead, poisoning the week
    to the configured cap ceiling, the same as every other unreadable
    earlier-weekday candidate in this function.
    """
    live_root = tmp_path / "live"
    override_root = tmp_path / "perplexity_primary_20260706" / "manual_override_1015ET"
    override_root.mkdir(parents=True)
    # Malformed JSON -- caught mid-write.
    (override_root / "run_state.json").write_text("{not valid json", encoding="utf-8")

    pf_root = _seed_preflight(tmp_path / "pf", ("CCC",))
    cfg = MorningConfig(
        tickers=("CCC",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(override_root),),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()

    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)

    assert ex._cap_seed_scan_incomplete is True
    assert ex.ledger.week_planned(date(2026, 7, 8)) == 100.0


def test_cap_seed_scan_ignores_a_same_day_leaf_override_root(tmp_path):
    """The leaf-override direct read above must stay scoped to STRICTLY earlier
    weekdays: a same-day override is already folded in by _compute_top_up's own
    cap_seed_roots scan (guarded by _today_seeded_by_cap_roots), so admitting it
    again here would double-count the same fill.
    """
    live_root = tmp_path / "live"
    override_root = tmp_path / "perplexity_primary_20260708" / "manual_override_0900ET"
    override_root.mkdir(parents=True)
    (override_root / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-08",  # same day as _now_live()
            "run_id": "manual_override_20260708_0900ET",
            "buy_candidates": [
                {"ticker": "CCC", "status": "submitted", "planned_amount_usd": 12.0}
            ],
        }),
        encoding="utf-8",
    )

    pf_root = _seed_preflight(tmp_path / "pf", ("CCC",))
    cfg = MorningConfig(
        tickers=("CCC",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(override_root),),
        morning_daily_cap_usd=100.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = _now_live()

    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)

    # The leaf-override direct read must not itself add the $12 for TODAY;
    # any credit for it (if e.g. _compute_top_up's own scan also finds it) is
    # that mechanism's responsibility, not this one's.
    assert ex.ledger.week_planned(date(2026, 7, 8)) < 24.0


def test_earlier_weekday_cap_seed_poison_clears_on_a_recovered_rescan(tmp_path):
    """Codex review ("Clear prior-week cap poisoning after a recovered
    scan"): _seed_ledger_from_prior_runs's earlier-weekday sibling scan fails
    closed the same way _compute_top_up's cap_seed_roots scan does, pinning
    planned_by_week to the cap ceiling when an earlier-weekday sibling
    artifact is transiently unreadable. A long-lived executor instance
    retrying against the SAME self.ledger after that artifact becomes
    readable must see its real remaining weekly headroom again -- not stay
    permanently pinned just because an earlier scan on this instance failed
    to parse it."""
    cap_root = tmp_path / "cap_root"
    sibling_dir = cap_root / "20260629_manualET"
    sibling_dir.mkdir(parents=True)
    sibling_path = sibling_dir / "run_state.json"
    # First pass: caught mid-write -- invalid JSON.
    sibling_path.write_text("{not valid json", encoding="utf-8")

    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL",), trading_date=date(2026, 6, 30))
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(cap_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 6, 30)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = datetime.combine(date(2026, 6, 30), time(10, 30), tzinfo=EASTERN)

    # First pass: the malformed sibling artifact fails to parse -> fail closed.
    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)
    assert ex._cap_seed_scan_incomplete
    assert ex.ledger.week_planned(date(2026, 6, 30)) == 450.0
    assert ex.ledger.remaining_weekly(date(2026, 6, 30)) == 0.0

    # The artifact becomes readable with a real, modest $10 fill.
    sibling_path.write_text(
        json.dumps({
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:00:00-04:00",
            "run_id": "earlier_week_recovered",
            "buy_candidates": [
                {"ticker": "OTHER", "status": "filled", "planned_amount_usd": 10.0},
            ],
        }),
        encoding="utf-8",
    )

    # Second pass, SAME executor instance: the ledger must reflect the real
    # $10, not stay pinned at the $450 weekly ceiling the first (failed)
    # scan injected.
    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)
    assert not ex._cap_seed_scan_incomplete
    assert ex.ledger.week_planned(date(2026, 6, 30)) == 10.0, (
        "the earlier fail-closed poisoning must be cleared once the scan recovers"
    )
    assert ex.ledger.remaining_weekly(date(2026, 6, 30)) == 440.0


def test_earlier_weekday_run_with_a_failed_csv_scan_is_rescanned_once_repaired(tmp_path):
    """Codex review ("Do not cache prior runs whose CSV scan failed"): an
    earlier-weekday sibling run_state.json can parse fine (no buy_candidates,
    e.g. a market-open/intraday stream) while its canonical
    submitted_orders.csv is transiently malformed/mid-write. The old code
    unconditionally cached this run_id as seeded even when its CSV scan
    failed this pass, so a later successful rescan (via the already_seeded
    check) skipped it forever -- permanently losing whatever spend existed
    only in that CSV, even after the overall week-level fail-closed
    poisoning cleared."""
    cap_root = tmp_path / "cap_root"
    sibling_dir = cap_root / "20260629_manualET"
    sibling_dir.mkdir(parents=True)
    (sibling_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:00:00-04:00",
            "run_id": "earlier_week_csv_run",
            "buy_candidates": [],
        }),
        encoding="utf-8",
    )
    csv_path = sibling_dir / "submitted_orders.csv"
    # First pass: caught mid-write -- headerless (empty file).
    csv_path.write_text("", encoding="utf-8")

    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL",), trading_date=date(2026, 6, 30))
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(str(cap_root),),
        morning_daily_cap_usd=90.0,
        morning_weekly_cap_usd=450.0,
        approved_test_dates=frozenset({date(2026, 6, 30)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = datetime.combine(date(2026, 6, 30), time(10, 30), tzinfo=EASTERN)

    # First pass: the headerless CSV fails to parse -> fail closed, and this
    # run_id must NOT be cached as seeded.
    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)
    assert ex._cap_seed_scan_incomplete
    assert "earlier_week_csv_run" not in ex._seeded_prior_run_ids
    assert ex.ledger.week_planned(date(2026, 6, 30)) == 450.0  # fail-closed ceiling

    # The CSV becomes readable with a real $25 fill.
    csv_path.write_text(
        "ticker,order_id,status,submitted_amount_usd,avg_fill_price,quantity_filled\n"
        "AAPL,ORD-1,filled,25.00,50.00,0.5\n",
        encoding="utf-8",
    )

    # Second pass, SAME executor instance: the run must be rescanned (not
    # skipped as already-seeded) and its real $25 fill seeded, not stay
    # pinned at the $450 ceiling the first (failed) scan injected.
    ex._compute_top_up(now, ())
    ex._seed_ledger_from_prior_runs(now)
    assert not ex._cap_seed_scan_incomplete
    assert "earlier_week_csv_run" in ex._seeded_prior_run_ids
    assert ex.ledger.week_planned(date(2026, 6, 30)) == pytest.approx(25.0), (
        "the run's real $25 fill must be seeded once its CSV becomes readable, not "
        "permanently skipped because an earlier failed pass cached it as seeded"
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

    class _DupBlockingBroker(FakeLiveBroker):
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
    ex.run(_now_live(), data, broker=shared_broker)
    assert ex.ledger.day_planned(date(2026, 7, 8)) == manual_amount, (
        "cap-seeding must not re-count the manual run's own submission; "
        f"expected {manual_amount}, got {ex.ledger.day_planned(date(2026, 7, 8))}"
    )


def test_morning_cap_seeding_watermark_exclusive_of_own_recorded(tmp_path):
    # _cap_seeded_amounts must store new_total - own_amount. If it stored new_total,
    # a scheduled-run record_buy_candidates call would grow _own_recorded beyond
    # the filesystem scan's new_total, making prev_total > new_total on the next scan
    # and suppressing pickup of newly-written external fills.
    #
    # Scenario:
    #   1. Manual run buys $4.50 → _own_recorded[day]=4.50, artifact in manual_runs/
    #   2. External $4 fill appears in manual_runs/
    #   3. Scheduled run 1: seeds $4 (incremental), writes $4.50 to ledger via record_buy_candidates
    #      (but NOT to _own_recorded since manual=False)
    #   4. Another external $4 fill appears in manual_runs/
    #   5. Scheduled run 2: should seed another $4 — computing prev_total = old_watermark + _own_recorded
    #      correctly avoids a negative incremental.
    import json as _json

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _SimpleBroker(FakeLiveBroker):
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

    # Step 1: manual run at 9:00 AM — buys TICK ($4.50), writes artifact to manual_runs/.
    # Use an earlier timestamp so the artifact's generated_at is before the 10:30 AM
    # boundary used by the scheduled run's cap-seeding scan (boundary check is strict <).
    _now_manual = datetime.combine(date(2026, 7, 8), time(9, 0), tzinfo=EASTERN)
    broker = _SimpleBroker()
    ex.run(_now_manual, data, broker=broker, manual=True)
    manual_amount = broker.placed.get("TICK", 0.0)
    assert manual_amount == 4.5

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
        f"ledger should be manual($4.50) + external-seed($4) = $8.50, got {ledger_after_sched1}"
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
    ex.run(_now_live(), data, broker=broker)
    ledger_after_sched2 = ex.ledger.day_planned(date(2026, 7, 8))
    assert ledger_after_sched2 == manual_amount + 8.0, (
        f"ledger should be manual($4.50) + ext1-seed($4) + ext2-seed($4) = $12.50, "
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

    class _DupBlockingBroker(FakeLiveBroker):
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

    class _DupBlockingBroker(FakeLiveBroker):
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


def test_shadow_run_suppresses_drift_check(tmp_path, monkeypatch):
    """Codex review ("Suppress drift alerts for deliberate shadow runs"):
    live_execution_enabled=False (market_open_dca.json/intraday_dca.json's
    "SHIPS DISABLED" mode) drops the broker, so broker_outcomes stays None
    and the auto_buy_signal reconciliation that clears it for deliberate
    non-submissions never runs -- every eligible signal row would still read
    auto_buy_signal=True next to an empty submitted_orders.csv. Running
    drift detection against that mismatch would report every deliberate
    shadow candidate as an unexpectedly missing order. A scheduled live run
    with execution disabled must not invoke the drift check at all."""
    import morning_dca as _md
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker(FakeLiveBroker):
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

    calls = []
    monkeypatch.setattr(_md, "_run_drift_check", lambda run_dir: calls.append(run_dir))

    pf_root = _seed_preflight(tmp_path / "pf", ("AAA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})

    # Shadow mode: drift check must NOT run even though the ladder produces
    # an eligible buy-signal row.
    shadow_cfg = MorningConfig(
        tickers=("AAA",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "shadow"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
        live_execution_enabled=False,
    )
    MorningDCAExecutor(shadow_cfg, mode=MORNING_LIVE_MODE).run(_now_live(), data, broker=_Broker())
    assert calls == []

    # Control: the same scenario with execution enabled (the morning/midday
    # default) must still run the drift check as before.
    live_cfg = MorningConfig(
        tickers=("AAA",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    MorningDCAExecutor(live_cfg, mode=MORNING_LIVE_MODE).run(_now_live(), data, broker=_Broker())
    assert len(calls) == 1


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
        morning_daily_cap_usd=6.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex1 = MorningDCAExecutor(cfg1, mode=MORNING_LIVE_MODE)
    data1 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03})
    broker1 = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0})
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
        morning_daily_cap_usd=6.0,
        morning_weekly_cap_usd=100.0,
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex2 = MorningDCAExecutor(cfg2, mode=MORNING_LIVE_MODE)
    data2 = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BBB": 0.03})
    broker2 = RecordingLiveBroker(account="999000111", reference_prices={"BBB": 97.0})
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

    class _Broker(FakeLiveBroker):
        def __init__(self): self.placed = {}
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None
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
    # 3% drop → desired 1.5x * $3.00 = $4.50; no prior fill → full amount placed
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    candidates = {r["ticker"]: r for r in res["artifacts"].get("buy_candidates", [])}
    assert "TOPUP" in candidates
    row = candidates["TOPUP"]
    assert row.get("top_up_guard_result") == TOP_UP_NO_PRIOR_RUN
    assert broker.placed.get("TOPUP") == 4.5, "full graded amount placed when no prior fill exists"


# ---------------------------------------------------------------------------
# Top-up guard regression tests (scheduled-run path)
# ---------------------------------------------------------------------------

def _seed_prior(
    root, ticker, amount_usd, *,
    status="filled", reference_price=95.0, trading_date=None, selected_base_amount_usd=None,
    run_id="test_prior",
):
    """Write a prior-fill run_state.json for one ticker under root.

    ``selected_base_amount_usd`` mirrors a real artifact's recorded
    first-allocation base; omitting it (the default) simulates a legacy
    artifact that predates the field, which now routes the top-up guard to
    missing_prior_base/manual review (Codex review: "Fail closed when the
    first allocation base is unknown") rather than silently recomputing from
    this run's own price.

    ``run_id`` defaults to a generic fixture value; pass the real
    ``MorningDCAExecutor.run_id(now, manual=...)`` value when a test needs
    this seeded artifact to be recognized as THIS EXACT slot's own
    contribution (see prior_reflects_settlement's run_id-equality check in
    _compute_top_up).
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
        "run_classification": {"run_id": run_id, "run_origin": "manual", "stream": "morning"},
        "buy_candidates": [candidate],
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    return root


class _TopUpBroker(FakeLiveBroker):
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER as _acct
    supports_concurrent_fill_lookup: bool = False

    def __init__(self):
        self.placed = {}
    def account_number(self) -> str: return self._acct
    def available_buying_power(self) -> float: return 1000.0
    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool: return False
    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes) -> Optional[Dict[str, Any]]:
        return None
    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id) -> Optional[Dict[str, Any]]: return None
    def same_day_ticker_notional_usd(self, ticker, trading_day) -> float: return 0.0
    def is_tradable_for_fractional_buy(self, ticker) -> bool: return True
    def review_market_buy(self, ticker, notional_usd) -> Tuple[bool, str]: return (True, "ok")
    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
        self.placed[ticker] = notional_usd
        return f"ORDER-{ticker}"
    def get_order_fill(self, order_id) -> Any: return None
    def get_raw_placement_responses(self) -> list: return []


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


class _RefIdTrackingBroker(FakeLiveBroker):
    """Broker double that actually remembers placed ref_ids, so the same-stream
    duplicate check (same_day_strategy_order_exists_with_ref_id_prefix) reflects
    real prior placements instead of a fixture that always returns False.
    """

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER as _acct
    supports_concurrent_fill_lookup: bool = False

    def __init__(self):
        self.placed = {}  # ticker -> notional (last placement)
        self._ref_ids = []  # list of (ticker, ref_id) placed so far

    def account_number(self) -> str:
        return self._acct

    def available_buying_power(self) -> float:
        return 1000.0

    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool:
        return any(t == ticker for t, _ in self._ref_ids)

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes) -> Optional[Dict[str, Any]]:
        for t, rid in self._ref_ids:
            if t == ticker and any(rid.startswith(p) for p in ref_id_prefixes):
                return {"id": f"ORDER-{ticker}", "dollar_amount": self.placed.get(ticker, 0.0)}
        return None

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id) -> Optional[Dict[str, Any]]:
        for t, rid in self._ref_ids:
            if t == ticker and rid == ref_id:
                return {"id": f"ORDER-{ticker}", "dollar_amount": self.placed.get(ticker, 0.0)}
        return None

    def same_day_ticker_notional_usd(self, ticker, trading_day) -> float:
        return sum(amt for t, amt in self.placed.items() if t == ticker)

    def is_tradable_for_fractional_buy(self, ticker) -> bool:
        return True

    def review_market_buy(self, ticker, notional_usd) -> Tuple[bool, str]:
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
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
        max_per_order_usd=15.0,
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(live_root / "manual_runs"),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _RefIdTrackingBroker()

    # Step 1: manual run, 3% drop -> 1.5x * $3 = $4.50 placed with the
    # ORDINARY ref_id (top-up computation is disabled for manual+MORNING).
    manual_now = datetime.combine(date(2026, 7, 8), time(9, 0), tzinfo=EASTERN)
    manual_data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.03})
    manual_res = ex.run(manual_now, manual_data, broker=broker, manual=True)
    assert broker.placed.get("NVDA") == pytest.approx(4.5)
    manual_ref_ids = list(broker._ref_ids)
    assert manual_ref_ids and "topup" not in manual_ref_ids[0][1]

    # Simulate a later reporting pass confirming the fill (real production
    # flow: orders.csv/broker refresh marks it "filled"), so the scheduled
    # run's top-up guard has a known reference price to top up against
    # instead of routing to missing_prior_price/manual review.
    manual_state_path = Path(manual_res["paths"]["run_state"])
    manual_state = json.loads(manual_state_path.read_bytes())
    for row in manual_state["buy_candidates"]:
        if row["ticker"] == "NVDA":
            row["status"] = "filled"
    manual_state_path.write_text(json.dumps(manual_state), encoding="utf-8")

    # Step 2: scheduled run, deeper 10% drop -> 5x * $3 = $15 desired. The
    # manual fill above is now visible as a prior allocation ($4.50 at the
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


@pytest.mark.parametrize("carried_status", ["pending", "unconfirmed"])
def test_morning_scheduled_duplicate_with_pending_prior_preserves_sent_amount(tmp_path, carried_status):
    # Codex review, PR #1364 discussion_r3946703223 ("Preserve amounts for
    # pending retry orders"): a pending/unconfirmed carried row proves the
    # order exists, and the dollars persisted alongside are the notional this
    # executor SENT. The broker's eventual figure can only be at or below
    # that, so the sent amount is the conservative one for cap accounting.
    # This test used to assert the opposite (that a duplicate-check response
    # without a notional falls back to the retry's fresh, smaller amount);
    # that let an $8 pending order be re-persisted as $4 into the fixed
    # dated-slot run_state path and freed cap headroom already consumed.
    class _DuplicateWithoutAmountBroker(_RefIdTrackingBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            for t, rid in self._ref_ids:
                if t == ticker and any(rid.startswith(p) for p in ref_id_prefixes):
                    return {"id": f"ORDER-{ticker}"}
            return None

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
    broker = _DuplicateWithoutAmountBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    first = ex.run(_now_live(), data, broker=broker)
    run_state_path = Path(first["paths"]["run_state"])
    prior = json.loads(run_state_path.read_bytes())
    row = prior["buy_candidates"][0]
    row["status"] = carried_status
    sent_amount = 14.0
    row["planned_amount_usd"] = sent_amount
    row["submitted_amount_usd"] = sent_amount
    prior["broker_outcomes"]["NVDA"]["status"] = carried_status
    prior["broker_outcomes"]["NVDA"]["submitted_amount_usd"] = sent_amount
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")

    # Force the retry's fresh incremental down to $12.00 (15 target minus a new
    # $3 prior allocation), so a fallback to the recomputed amount would be
    # visibly SMALLER than what was actually sent.
    _seed_prior(prior_root, "NVDA", 3.0, reference_price=95.0, selected_base_amount_usd=2.0)

    retry = ex.run(_now_live() + timedelta(minutes=1), data, broker=broker)
    nvda_outcome = retry["artifacts"].get("broker_outcomes", {}).get("NVDA", {})
    assert nvda_outcome.get("status") == "submitted", nvda_outcome
    retry_row = next(
        row for row in retry["artifacts"].get("buy_candidates", []) if row["ticker"] == "NVDA"
    )
    # The persisted duplicate carries the $14 that was sent, not the $12 the
    # retry recomputed, so the dated-slot artifact never understates spend;
    # the candidate row is rewritten to the same preserved figure.
    assert nvda_outcome.get("submitted_amount_usd") == pytest.approx(sent_amount)
    assert retry_row["planned_amount_usd"] == pytest.approx(sent_amount)
    assert nvda_outcome.get("submitted_amount_usd") != pytest.approx(12.0)
    assert nvda_outcome.get("duplicate_preserved") is True


def test_morning_scheduled_duplicate_with_pending_prior_without_amount_uses_fresh_amount(tmp_path):
    # The one case that still falls through to the retry's recomputed amount:
    # the carried pending row has NO usable amount at all, so there is nothing
    # sent to preserve. Kept as the boundary for the parametrized test above.
    class _DuplicateWithoutAmountBroker(_RefIdTrackingBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            for t, rid in self._ref_ids:
                if t == ticker and any(rid.startswith(p) for p in ref_id_prefixes):
                    return {"id": f"ORDER-{ticker}"}
            return None

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
    broker = _DuplicateWithoutAmountBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    first = ex.run(_now_live(), data, broker=broker)
    run_state_path = Path(first["paths"]["run_state"])
    prior = json.loads(run_state_path.read_bytes())
    row = prior["buy_candidates"][0]
    row["status"] = "pending"
    row.pop("planned_amount_usd", None)
    row["submitted_amount_usd"] = None
    prior["broker_outcomes"]["NVDA"]["status"] = "pending"
    prior["broker_outcomes"]["NVDA"]["submitted_amount_usd"] = None
    run_state_path.write_text(json.dumps(prior), encoding="utf-8")
    _seed_prior(prior_root, "NVDA", 3.0, reference_price=95.0, selected_base_amount_usd=2.0)

    retry = ex.run(_now_live() + timedelta(minutes=1), data, broker=broker)
    nvda_outcome = retry["artifacts"].get("broker_outcomes", {}).get("NVDA", {})
    assert nvda_outcome.get("status") == "submitted", nvda_outcome
    retry_row = next(
        row for row in retry["artifacts"].get("buy_candidates", []) if row["ticker"] == "NVDA"
    )
    assert nvda_outcome.get("submitted_amount_usd") == pytest.approx(retry_row["planned_amount_usd"])


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


def test_morning_live_insufficient_buying_power_blocks_order(tmp_path):
    # available_buying_power() below the candidate's notional must block the
    # order before any placement, distinctly from a cap or spread block.
    class _PoorBroker(_TopUpBroker):
        def available_buying_power(self):
            return 0.50

    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _PoorBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" not in broker.placed
    outcome = res["artifacts"].get("broker_outcomes", {}).get("NVDA", {})
    assert outcome.get("status") == "blocked"
    assert outcome.get("skip_reason") == "insufficient buying power"


def test_morning_live_not_tradable_blocks_order(tmp_path):
    class _UntradableBroker(_TopUpBroker):
        def is_tradable_for_fractional_buy(self, ticker) -> bool:
            return False

    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _UntradableBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)
    assert "NVDA" not in broker.placed
    outcome = res["artifacts"].get("broker_outcomes", {}).get("NVDA", {})
    assert outcome.get("status") == "blocked"
    assert outcome.get("skip_reason") == "not tradable for fractional buy"


def test_morning_manual_review_block_prevents_placement(tmp_path):
    # Scheduled runs are pre-authorized to skip review_market_buy (config/
    # budgets.json execution_authorization_policy: calls_review_equity_order=
    # false); a MANUAL run still calls it as a human-reviewed safety net, and a
    # broker-side rejection there must block the order.
    class _ReviewBlockedBroker(_TopUpBroker):
        def review_market_buy(self, ticker, notional_usd) -> Tuple[bool, str]:
            return False, "flagged for manual review"

    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    broker = _ReviewBlockedBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)
    assert "NVDA" not in broker.placed
    outcome = res["artifacts"].get("broker_outcomes", {}).get("NVDA", {})
    assert outcome.get("status") == "blocked"
    assert outcome.get("skip_reason") == "review blocked: flagged for manual review"


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
    # default_cap_seed_roots() resolves via stream_root() at call time (Codex
    # review, PR #219), so DEFAULT_AFTERNOON_TASK_ROOT/DEFAULT_MARKET_OPEN_
    # TASK_ROOT no longer affect what _compute_top_up scans; redirect the
    # function itself instead of the now-inert module constants.
    monkeypatch.setattr(
        morning_dca, "default_cap_seed_roots", lambda: (str(afternoon_root), str(market_open_root))
    )

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


def test_default_cap_seed_roots_include_dated_manual_overrides(monkeypatch):
    """Direct dataclass construction must retain dynamically discovered override fills."""
    import morning_dca

    override_root = "/tmp/perplexity_primary_20260829/manual_override_1030"
    monkeypatch.setattr(
        morning_dca,
        "_dated_manual_override_cap_seed_roots",
        lambda: (override_root,),
    )

    assert override_root in MorningConfig().cap_seed_roots


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


def test_max_orders_per_day_blocks_attempts_beyond_the_shared_cap(tmp_path):
    # Codex review ("Enforce the shared daily order-count cap in new
    # executors"): market_open_dca.py/intraday_dca.py place orders through
    # this same inherited _place_broker_orders path, which previously had no
    # max_orders_per_day enforcement at all -- only notional caps. With the
    # cap set to 2, a batch of 3 qualifying candidates must place only the
    # first 2 and block the 3rd on the daily attempt ceiling.
    cfg = MorningConfig(
        tickers=("AAA", "BBB", "CCC"),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path),
        max_orders_per_day=2,
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.03, "BBB": 0.03, "CCC": 0.03})
    broker = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 97.0, "BBB": 97.0, "CCC": 97.0})

    res = ex.run(_now_live(), data, broker=broker, manual=True)

    broker_outcomes = res["artifacts"]["broker_outcomes"]
    submitted = [t for t, oc in broker_outcomes.items() if oc["status"] == "submitted"]
    blocked = [t for t, oc in broker_outcomes.items() if oc["status"] == "blocked"]
    assert len(submitted) == 2
    assert len(blocked) == 1
    assert "order daily attempt cap" in broker_outcomes[blocked[0]]["skip_reason"]


def test_max_orders_per_day_none_by_default_does_not_block():
    # Control: without an explicit cap (the class default, matching every
    # existing test in this file), the new gate must be a complete no-op.
    cfg = MorningConfig(tickers=("AAA",), base_buy_amount_usd=3.0)
    assert cfg.max_orders_per_day is None


def test_load_max_orders_per_day_reads_budgets_json(tmp_path):
    (tmp_path / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_orders_per_day": 8}}), encoding="utf-8"
    )
    assert load_max_orders_per_day(tmp_path) == 8


def test_load_max_orders_per_day_missing_file_returns_none(tmp_path):
    assert load_max_orders_per_day(tmp_path) is None


def test_load_max_orders_per_day_rejects_non_positive_value(tmp_path):
    (tmp_path / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_orders_per_day": 0}}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_max_orders_per_day(tmp_path)


def test_morning_cap_seeding_runs_even_when_per_ticker_guard_disabled(tmp_path):
    # Codex review (PR #42): setting prior_allocation_roots=[] disables the per-ticker
    # guard but must NOT skip cap seeding. A morning manual fill of $85 recorded in
    # cap_seed_roots must still reduce the ledger so a subsequent live run respects
    # the daily cap even when the per-ticker offset guard is opted out.
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _Broker(FakeLiveBroker):
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
    _write_prior_run_state(morning_root, "PRIOR_TICKER", amount_usd=86.0, decision_price=50.0)
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
    # 3% drop → desired $4.50; cap = $90 - $86 = $4 remaining → cap_exceeded
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"TOPUP": 0.03})
    res = ex.run(_now_live(), data, broker=broker)
    # Cap seeding must have run: ledger reflects the $86 prior fill
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 86.0, (
        "cap ledger must be seeded from prior fills even when per-ticker guard is disabled"
    )
    broker_outcomes = res["artifacts"].get("broker_outcomes", {})
    assert broker_outcomes.get("TOPUP", {}).get("status") == "cap_exceeded", (
        "TOPUP should be cap-gated: prior $86 fill consumed most of $90 daily cap"
    )
    assert "TOPUP" not in broker.placed, "broker should not be called when cap is exhausted"


def test_morning_cap_seeding_own_amount_scoped_to_cap_roots(tmp_path):
    # Codex review (PR #42 line 960): when cap_seed_roots does NOT cover
    # state_root/manual_runs/, own_amount must not be subtracted from prev_total
    # because own fills are absent from new_total.
    import json as _json

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    class _SimpleBroker(FakeLiveBroker):
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

    # Step 1: manual run buys TICK ($4.50) → _own_recorded[day]=4.50
    _now_manual = datetime.combine(date(2026, 7, 8), time(9, 0), tzinfo=EASTERN)
    broker = _SimpleBroker()
    ex.run(_now_manual, data, broker=broker, manual=True)
    assert broker.placed.get("TICK") == 4.5

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
    # external $4 fill even though own_amount=$4.50 is not in external_cap_root.
    ex.run(_now_live(), data, broker=broker)
    ledger_after = ex.ledger.day_planned(date(2026, 7, 8))
    assert ledger_after == 4.5 + 4.0, (
        f"ledger should be own($4.50) + external-seed($4) = $8.50, got {ledger_after}"
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

    class _Broker(FakeLiveBroker):
        def account_number(self): return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
        def available_buying_power(self): return 500.0
        def same_day_strategy_order_exists(self, ticker, trading_day): return False
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes): return None
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

    class _NoBuyingPowerCallBroker(FakeLiveBroker):
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

    class _FakeLiveBroker(FakeLiveBroker):
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

    class _FakeLiveBroker(FakeLiveBroker):
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
    class _LegacyBoolBroker(FakeLiveBroker):
        def __init__(self, result):
            self._result = result

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return self._result

    trading_day = date(2026, 7, 8)
    # Legacy False -> no duplicate -> None, not a truthy "duplicate found" value.
    assert _same_stream_order_exists(_LegacyBoolBroker(False), "NVDA", trading_day, "morning") is None
    # Legacy None -> no duplicate -> None.
    assert _same_stream_order_exists(_LegacyBoolBroker(None), "NVDA", trading_day, "morning") is None
    # Legacy True -> duplicate found, with no REAL order details to preserve,
    # but must still be a dict (supports .get(...)) carrying a synthesized,
    # deterministic "id" -- not the bare True, and not a dict with no id at
    # all. Without an id here, the caller's duplicate-recovery settle() call
    # passes order_id=None through, disabling settle()'s own per-order_id
    # dedup: a retry that finds this same legacy-boolean duplicate again
    # would subtract the same dollars from the shared pending balance a
    # second time (Codex review, PR #656 discussion_r3837494237: "Require an
    # ID for idempotent duplicate settlement"). Deterministic and stable
    # across repeat calls for the SAME ticker/day/stream, so settle()'s
    # dedup set actually recognizes the retry.
    result = _same_stream_order_exists(cast(Any, _LegacyBoolBroker(True)), "NVDA", trading_day, "morning")
    assert result is not None
    assert result.get("id") == "legacy-bool-dup:morning:2026-07-08:NVDA"
    assert result.get("dollar_amount") is None
    again = _same_stream_order_exists(cast(Any, _LegacyBoolBroker(True)), "NVDA", trading_day, "morning")
    assert again.get("id") == result.get("id")
    # A real order dict passes through unchanged so real order_id/amount survive.
    order = {"id": "O1", "dollar_amount": 7.5}
    assert _same_stream_order_exists(_LegacyBoolBroker(order), "NVDA", trading_day, "morning") == order


def test_stable_broker_order_id_rejects_missing_and_blank_ids():
    assert _stable_broker_order_id(None) is None
    assert _stable_broker_order_id({}) is None
    assert _stable_broker_order_id({"id": "   "}) is None
    assert _stable_broker_order_id({"order_id": " ORDER-1 "}) == "ORDER-1"
    assert _stable_broker_order_id({"id": "   ", "order_id": " ORDER-1 "}) == "ORDER-1"


@pytest.mark.parametrize("invalid_primary", [False, 0, [], {}])
def test_stable_broker_order_id_uses_fallback_for_malformed_primary_id(invalid_primary):
    assert _stable_broker_order_id({"id": invalid_primary, "order_id": " ORDER-1 "}) == "ORDER-1"


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
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes) -> Any:
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
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes) -> Any:
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


def test_raw_input_recorder_namespaces_filenames_across_retry_attempts(tmp_path):
    # Codex review, PR #538 discussion_r3809442201 ("Namespace persisted raw
    # inputs by retry attempt"): a scheduled retry builds a brand-new recorder
    # over the SAME dated-slot raw_dir. Its filenames must never collide with
    # (and overwrite) the previous attempt's -- especially when that earlier
    # attempt already submitted an order the raw evidence justified.
    raw_dir = tmp_path / "raw"
    first_attempt = _RawInputRecorder(raw_dir, "2026-07-08")
    first_attempt.persist("prior_close", "AAPL", {"close": 100.0})
    first_attempt.persist("quote", "AAPL", {"price": 99.5})
    assert first_attempt.error is None
    first_names = set(first_attempt.saved_filenames)
    assert len(first_names) == 2

    second_attempt = _RawInputRecorder(raw_dir, "2026-07-08")
    second_attempt.persist("prior_close", "AAPL", {"close": 101.0})
    assert second_attempt.error is None
    second_names = set(second_attempt.saved_filenames)

    # The second attempt's filename(s) must be brand new, not a name the first
    # attempt already used -- overwriting it would destroy the first attempt's
    # persisted evidence.
    assert not (first_names & second_names)
    # And the first attempt's files must still be on disk, untouched.
    for name in first_names:
        assert (raw_dir / name).exists()
    first_close_filename = next(n for n in first_names if n.startswith("prior_close"))
    on_disk_first_close = json.loads((raw_dir / first_close_filename).read_bytes())
    assert on_disk_first_close["close"] == 100.0


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


def test_persisting_market_data_provider_computes_from_round_tripped_payload(tmp_path):
    class _MutableProvider(DecliningBarsProvider):
        def get_quote(self, ticker, as_of):
            return (99.0, as_of)

    data = _MutableProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.03})
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(data, recorder)
    quote = wrapped.get_quote("AAPL", _now_live())

    # JSON round-tripping changes the connector tuple to a list. Returning
    # that persisted shape proves computation does not retain the connector's
    # original in-memory object; datetime restoration keeps the provider API.
    assert isinstance(quote, list)
    assert quote[0] == 99.0
    assert isinstance(quote[1], datetime)


def test_persisting_robinhood_provider_normalizes_pointer_before_parsing(tmp_path):
    missing = tmp_path / "missing-historicals.json"
    provider = RobinhoodDataProvider(historicals_fn=lambda **kwargs: {"output_path": str(missing)})
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()

    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=10
    )

    assert bars == []
    assert recorder.saved_filenames == []
    assert recorder.error is not None
    assert "could not read output_path" in recorder.error


def test_raw_input_recorder_rejects_missing_current_session_context_pointer(tmp_path):
    missing = tmp_path / "missing-context.json"
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")

    persisted = recorder.persist("historicals", "AAPL", {"current_session_context": str(missing)})

    assert persisted is None
    assert recorder.saved_filenames == []
    assert recorder.error is not None
    assert "could not read output_path" in recorder.error


def test_raw_input_recorder_normalizes_current_session_context_pointer(tmp_path):
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"results": [{"symbol": "AAPL"}]}), encoding="utf-8")
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")

    persisted = recorder.persist("historicals", "AAPL", {"current_session_context": str(context)})

    assert persisted == {"results": [{"symbol": "AAPL"}]}
    archived = json.loads((tmp_path / "raw" / recorder.saved_filenames[0]).read_bytes())
    assert archived["current_session_context"] == str(context)
    assert archived["output_path"] == str(context)
    assert archived["normalized"] is True


def test_persisting_finance_provider_archives_connectors_before_parsing(tmp_path):
    bars_payload = [
        {
            "timestamp": "2026-07-08T10:20:00-04:00",
            "open": 99.0,
            "high": 100.0,
            "low": 98.0,
            "close": 99.5,
            "volume": 10,
            "vendor_trace_id": "bars-raw-evidence",
        }
    ]
    close_payload = {"price": "101.25", "vendor_trace_id": "close-raw-evidence"}
    provider = FinanceDataProvider(
        bars_fn=lambda *args: bars_payload,
        previous_close_fn=lambda *args: close_payload,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()

    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=10
    )
    previous_close = wrapped.get_previous_close_bar("AAPL", now)

    assert len(bars) == 1
    assert previous_close is not None and previous_close.close == 101.25
    payloads = [json.loads((tmp_path / "raw" / name).read_bytes()) for name in recorder.saved_filenames]
    assert any(payload.get("data") == bars_payload for payload in payloads)
    assert close_payload in payloads


def test_persisting_robinhood_provider_parses_intraday_bars_after_raw_round_trip(tmp_path):
    # Codex review, PR #538 discussion_r3837449172 ("Preserve RFC3339 strings for
    # Robinhood parsing"): _restore_raw_datetimes() used to eagerly convert every
    # ordinary RFC3339 "begins_at" string into a real datetime object on replay.
    # RobinhoodDataProvider._parse_bars() -> _parse_rfc3339() only accepts strings,
    # so every intraday bar silently failed to parse (empty result, no
    # recorder.error) -- starving the Robinhood signal path of data. This proves
    # bars survive persist-then-replay and parse correctly for the Robinhood path.
    historicals_payload = {
        "results": [
            {
                "symbol": "AAPL",
                "bars": [
                    {
                        "begins_at": "2026-07-08T14:20:00Z",
                        "open_price": "99.0",
                        "high_price": "100.0",
                        "low_price": "98.0",
                        "close_price": "99.5",
                        "volume": "10",
                    }
                ],
            }
        ]
    }
    provider = RobinhoodDataProvider(historicals_fn=lambda **kwargs: historicals_payload)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()

    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=10
    )

    assert recorder.error is None
    assert len(bars) == 1
    assert bars[0].close == 99.5


def test_persisting_finance_provider_materializes_generator_bars_payload(tmp_path):
    # Codex review, PR #538 discussion_r3821694971 ("Materialize iterable Finance
    # bar payloads"): a Finance bars_fn may legitimately return a generator (a
    # valid iterable per FinanceDataProvider's documented contract). Before the
    # fix, _raw_json_safe() left it unchanged, json.dumps() inside
    # save_raw_input() raised on the un-serializable generator, and the run
    # blocked instead of processing the market data that was actually available.
    def _bars_gen(*args):
        yield {
            "timestamp": "2026-07-08T10:20:00-04:00",
            "open": 99.0, "high": 100.0, "low": 98.0, "close": 99.5, "volume": 10,
        }

    provider = FinanceDataProvider(
        bars_fn=lambda *args: _bars_gen(*args),
        previous_close_fn=lambda *args: None,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()

    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=10
    )

    assert recorder.error is None
    assert len(bars) == 1
    assert bars[0].close == 99.5


def test_persisting_finance_provider_preserves_bare_date_bar_timestamp(tmp_path):
    # Codex review, PR #538 discussion_r3821694980 ("Preserve bare-date bar
    # timestamps across persistence"): a bare datetime.date timestamp (the
    # documented yfinance/pandas shape) must anchor at 16:00 ET on replay, just
    # as dca_live_adapters._coerce_bar() anchors it for the ORIGINAL in-memory
    # value. Losing the date/datetime distinction on the persist round trip
    # would have it parsed as midnight ET instead -- a silent 16-hour shift.
    bar_date = date(2026, 7, 8)
    provider = FinanceDataProvider(
        bars_fn=lambda *args: [
            {"timestamp": bar_date, "open": 99.0, "high": 100.0, "low": 98.0, "close": 99.5, "volume": 10}
        ],
        previous_close_fn=lambda *args: None,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()

    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=10
    )

    assert recorder.error is None
    assert len(bars) == 1
    expected = datetime.combine(bar_date, time(16, 0), tzinfo=EASTERN)
    assert bars[0].timestamp == expected


class _FakeNumpyScalar:
    """Mimics a numpy scalar's ``.item()`` interface without a numpy dependency.

    numpy.int64/numpy.bool_ are not subclasses of the plain Python int/bool
    equivalents json.dumps() accepts, but they DO expose ``.item()`` -- the
    standard NumPy accessor that returns the equivalent plain Python value.
    This repo is standard-library-only at runtime (CLAUDE.md), so the
    production fix duck-types on ``.item()`` rather than importing numpy;
    this fake proves that duck-typed path without adding a runtime numpy
    dependency to the test.
    """

    def __init__(self, value):
        self._value = value

    def item(self):
        return self._value


def test_persisting_finance_provider_serializes_vendor_scalar_bar_fields(tmp_path):
    # Codex review, PR #538 discussion_r3837489856 ("Serialize vendor scalar
    # values before archiving Finance bars"): a pandas/NumPy-backed Finance
    # bar can carry a numpy.int64 volume or numpy.bool_ flag. Before the fix,
    # _raw_json_safe()'s fallback left such a scalar unchanged, json.dumps()
    # raised TypeError, persist() set recorder.error, and the scheduled run
    # blocked even though FinanceDataProvider._coerce_bar() could consume the
    # original numeric value fine.
    provider = FinanceDataProvider(
        bars_fn=lambda *args: [
            {
                "timestamp": "2026-07-08T10:20:00-04:00",
                "open": 99.0,
                "high": 100.0,
                "low": 98.0,
                "close": 99.5,
                "volume": _FakeNumpyScalar(12345),
                "is_regular_session": _FakeNumpyScalar(True),
            }
        ],
        previous_close_fn=lambda *args: None,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()

    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=10
    )

    assert recorder.error is None
    assert len(bars) == 1
    assert bars[0].volume == 12345
    archived = json.loads((tmp_path / "raw" / recorder.saved_filenames[0]).read_bytes())
    archived_bar = archived["data"][0]
    assert archived_bar["volume"] == 12345
    assert archived_bar["is_regular_session"] is True


def test_persisting_market_data_provider_clears_copied_quote_cache_before_wrapping(tmp_path):
    # Codex review, PR #538 discussion_r3837489860 ("Clear copied provider
    # caches before wrapping connectors"): _wrap_raw_connectors() only takes
    # a SHALLOW copy.copy() of the inner provider, which leaves a populated
    # RobinhoodDataProvider._signal_quote_cache/_signal_quote_as_of pointing
    # at the SAME cache dict the caller's original instance still owns. If
    # that cache is not reset on the copy, the persisting wrapper serves the
    # stale cached quote without ever invoking the newly wrapped
    # _quotes_fn -- so no raw artifact is written and no recorder.error is
    # raised, even though a quote lookup nominally happened.
    call_count = {"n": 0}

    def _quotes_fn(**kwargs):
        call_count["n"] += 1
        return {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "101.00"}}]}

    provider = RobinhoodDataProvider(
        historicals_fn=lambda **kwargs: {"results": []}, quotes_fn=_quotes_fn
    )
    now = _now_live()
    # Pre-populate the cache exactly as an earlier caller reusing this same
    # provider instance at this same `as_of` would have.
    provider.get_quote("AAPL", now)
    assert call_count["n"] == 1
    assert provider._signal_quote_cache

    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)

    quote = wrapped.get_quote("AAPL", now)

    assert quote is not None
    # The wrapped _quotes_fn must actually have been invoked again -- not
    # served from the stale copied cache.
    assert call_count["n"] == 2
    assert recorder.saved_filenames, "raw artifact must be written when the connector is actually invoked"


def test_persisting_broker_adapter_detaches_raw_placement_responses_before_wrapping(tmp_path):
    # Codex review, PR #654 discussion_r3837443188 ("Detach mutable broker state
    # before wrapping connectors"): _wrap_raw_connectors() only takes a SHALLOW
    # copy.copy() of the inner broker, which leaves the copy's
    # _raw_placement_responses pointing at the SAME list object the caller's
    # original (unwrapped) broker instance still owns. If that list is not
    # replaced on the copy, placing an order through the persisting wrapper
    # appends the response onto the SHARED list, so a caller that later reuses
    # the original broker for a different run would see -- and could
    # misattribute -- this run's order response via its own
    # get_raw_placement_responses().
    broker = RobinhoodBroker(
        place_fn=lambda **kwargs: {"order": {"id": "order-789", "state": "confirmed"}},
        enable_live_orders=True,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(broker, recorder)

    wrapped.place_market_buy("AAPL", 10.0, "ref-1")

    # The wrapper's own view sees the placement...
    assert wrapped.get_raw_placement_responses()
    # ...but the ORIGINAL, unwrapped broker instance must NOT: its list must
    # be independent of whatever the wrapper's copy accumulated.
    assert broker.get_raw_placement_responses() == []


def test_persisting_robinhood_adapters_archive_connector_envelopes_before_parsing(tmp_path):
    quote_payload = {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "99.00"}}]}
    buying_power_payload = {"data": {"buying_power": "137.00", "unparsed": "evidence"}}
    provider = RobinhoodDataProvider(
        historicals_fn=lambda **kwargs: {"results": []}, quotes_fn=lambda **kwargs: quote_payload
    )
    broker = RobinhoodBroker(buying_power_fn=lambda **kwargs: buying_power_payload)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")

    assert _PersistingMarketDataProvider(provider, recorder).get_quote("AAPL", _now_live())[0] == 99.0
    assert _PersistingBrokerAdapter(broker, recorder).available_buying_power() == 137.0

    payloads = [json.loads((tmp_path / "raw" / name).read_bytes()) for name in recorder.saved_filenames]
    assert quote_payload in payloads
    assert buying_power_payload in payloads


def test_persisting_broker_adapter_preserves_outer_failure_flag_on_place(tmp_path):
    # A connector can report failure as data (rather than raising) while still carrying
    # an inner order id -- the outer "success": false must survive the raw-persist round
    # trip so RobinhoodBroker.place_market_buy() raises instead of treating the id as a
    # submitted order (see PR #560 review discussion r3816204627).
    failed_placement = {"success": False, "data": {"order": {"id": "order-123", "state": "confirmed"}}}
    broker = RobinhoodBroker(
        place_fn=lambda **kwargs: failed_placement,
        enable_live_orders=True,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(broker, recorder)

    with pytest.raises(RuntimeError, match="failed"):
        wrapped.place_market_buy("AAPL", 10.0, "ref-1")

    assert recorder.error is None
    payloads = [json.loads((tmp_path / "raw" / name).read_bytes()) for name in recorder.saved_filenames]
    assert failed_placement in payloads


def test_persisting_broker_adapter_preserves_outer_failure_flag_on_orders_lookup(tmp_path):
    # An unsuccessful order-history response with an empty inner "orders" list must not
    # be read as "no same-day orders exist"; the outer failure flag has to survive so the
    # duplicate-order guard fails closed instead of silently passing.
    failed_lookup = {"success": False, "data": {"orders": []}}
    broker = RobinhoodBroker(orders_fn=lambda **kwargs: failed_lookup)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(broker, recorder)

    with pytest.raises(RuntimeError, match="failed"):
        wrapped.same_day_strategy_order_exists("AAPL", date(2026, 7, 8))

    assert recorder.error is None


def test_persisting_broker_adapter_preserves_is_error_envelope_on_orders_lookup(tmp_path):
    # An mcp-bridge {"isError": true, ...} envelope carries no truthy "error"/
    # "success"/"ok" field of its own -- without an explicit isError check,
    # persist() unwrapped straight to the inner "data": {"orders": []}, which the
    # duplicate-order guard would read as "no same-day orders exist" even though the
    # underlying broker lookup actually failed (Codex review, PR #538
    # discussion_r3837449174: "Preserve isError envelopes during replay").
    failed_lookup = {"isError": True, "data": {"orders": []}}
    broker = RobinhoodBroker(orders_fn=lambda **kwargs: failed_lookup)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(broker, recorder)

    with pytest.raises(RuntimeError, match="failed"):
        wrapped.same_day_strategy_order_exists("AAPL", date(2026, 7, 8))

    assert recorder.error is None


def test_persisting_broker_adapter_preserves_submitted_order_when_persist_write_fails(tmp_path, monkeypatch):
    # If the connector call itself succeeds (a real order was submitted) but writing
    # the raw archive fails afterward, the in-memory response must still reach
    # place_market_buy so the submitted order id is not lost -- only the raw-persist
    # write failure (recorder.error) should trip the run to fail closed, not the
    # already-completed broker action being treated as if it never happened (see PR
    # #560 review discussion r3816306601).
    placed = {"order": {"id": "order-456", "state": "confirmed"}}
    broker = RobinhoodBroker(place_fn=lambda **kwargs: placed, enable_live_orders=True, **_LIVE_LOOKUP_STUBS)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(broker, recorder)

    # Make the raw-archive write fail so save_raw_input() raises inside persist().
    # save_raw_input() writes via dca_fileio.atomic_write_text (temp file + fsync +
    # os.replace, not Path.write_text), so the failure is injected at that call
    # rather than by monkeypatching Path.write_text, which the atomic writer never
    # invokes.
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    import scripts.run_classification as _run_classification_module

    def _failing_atomic_write_text(path, *args, **kwargs):
        if Path(path).parent == tmp_path / "raw":
            raise OSError("disk full")
        raise AssertionError("unexpected atomic_write_text call in this test")

    monkeypatch.setattr(
        _run_classification_module, "atomic_write_text", _failing_atomic_write_text
    )
    order_id = wrapped.place_market_buy("AAPL", 10.0, "ref-1")

    assert order_id == "order-456"
    assert recorder.error is not None
    assert "failed to persist" in recorder.error


def test_persisting_provider_archives_the_raw_connector_response_before_parsing(tmp_path):
    # The persist-before-compute contract is about the CONNECTOR's response,
    # not the adapter's parsed return value. Archiving only the parsed Bar
    # would mean a malformed payload -- the case the archive exists for --
    # left no raw record at all, because the parse raised before anything
    # reached the recorder (Codex review: "Persist connector payloads before
    # parsing them"). The injected MCP callables are wrapped so the raw dict
    # is on disk before the adapter ever sees it.
    def _exploding_historicals(**kwargs):
        return {"results": [{"symbol": kwargs["symbols"][0], "historicals": "not-a-list"}]}

    provider = RobinhoodDataProvider(historicals_fn=_exploding_historicals)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()
    try:
        wrapped.get_intraday_bars(
            ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=5
        )
    except Exception:  # noqa: BLE001 - whether parsing raises is the adapter's business
        pass

    archived = [name for name in recorder.saved_filenames if name.startswith("historicals_")]
    assert archived, f"raw connector payload was not archived: {recorder.saved_filenames}"
    payload = json.loads((tmp_path / "raw" / archived[0]).read_bytes())
    assert payload["results"][0]["historicals"] == "not-a-list"


def test_each_run_intercepts_its_own_private_view_of_the_provider(tmp_path):
    # The decorator rebinds a SHALLOW COPY, so the caller's provider is never
    # mutated and each run's interception is bound to its own recorder. Rebinding
    # the caller's object could not survive two runs overlapping on one shared
    # instance: the second wrapper replaced the first, so the first run's later
    # connector calls archived into the second run's raw/ -- or nowhere at all --
    # while its recorder stayed error-free and it went on placing orders (Codex
    # review: "Archive connector responses for every overlapping run").
    def _historicals(**kwargs):
        return {"results": [{"symbol": kwargs["symbols"][0], "historicals": []}]}

    def _quotes(**kwargs):
        return {"results": [{"symbol": kwargs["symbols"][0], "last_trade_price": "1.0"}]}

    provider = RobinhoodDataProvider(historicals_fn=_historicals, quotes_fn=_quotes)
    first = _RawInputRecorder(tmp_path / "run1" / "raw", "2026-07-08")
    run_one = _PersistingMarketDataProvider(provider, first)
    second = _RawInputRecorder(tmp_path / "run2" / "raw", "2026-07-08")
    run_two = _PersistingMarketDataProvider(provider, second)

    # The caller's provider is untouched by either run, at any point.
    assert provider._historicals_fn is _historicals
    assert provider._quotes_fn is _quotes
    assert run_one._inner is not provider
    assert run_two._inner is not provider
    assert run_one._inner is not run_two._inner

    now = _now_live()
    for run in (run_two, run_one):
        try:
            run.get_intraday_bars(
                ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=5
            )
        except Exception:  # noqa: BLE001 - only the archiving side matters here
            pass

    # Each run archived its OWN call, in its own directory. The first run calling
    # second -- after the later run was constructed -- is the overlap case.
    assert any(n.startswith("historicals_") for n in first.saved_filenames), (
        f"the earlier run stopped archiving once a later one started: {first.saved_filenames}"
    )
    assert any(n.startswith("historicals_") for n in second.saved_filenames), (
        f"the later run captured nothing: {second.saved_filenames}"
    )
    # Exactly one connector payload each -- neither run picked up the other's.
    for recorder in (first, second):
        connector_files = [n for n in recorder.saved_filenames if n.startswith("historicals_")]
        assert len(connector_files) == 1, recorder.saved_filenames
    # Only ONE layer of wrapping, however many runs share the provider.
    assert run_two._inner._historicals_fn._raw_connector_original is _historicals
    assert run_two._inner._quotes_fn._raw_connector_original is _quotes


def test_the_callers_provider_is_never_mutated_by_a_run(tmp_path):
    # The completed slot's raw/ directory is closed to later calls because the
    # caller's provider was never rebound in the first place: manual, watchlist
    # and preflight calls -- none of which install a recorder, and all of which
    # the raw-input policy exempts -- reach the untouched original.
    def _historicals(**kwargs):
        return {"results": [{"symbol": kwargs["symbols"][0], "historicals": []}]}

    provider = RobinhoodDataProvider(historicals_fn=_historicals)
    recorder = _RawInputRecorder(tmp_path / "run1" / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)

    assert wrapped._inner is not provider
    assert provider._historicals_fn is _historicals

    now = _now_live()
    try:
        provider.get_intraday_bars(
            ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=5
        )
    except Exception:  # noqa: BLE001 - only the archiving side matters here
        pass

    assert recorder.saved_filenames == [], (
        f"a post-run call contaminated the completed slot's archive: {recorder.saved_filenames}"
    )


def test_an_uncopyable_provider_fails_the_run_closed(tmp_path):
    # The copy is what keeps concurrent runs apart. Falling back to mutating an
    # uncopyable provider would reinstate the exact fail-open the copy exists
    # to prevent -- a second run replacing the callables while the first keeps
    # placing orders with no persistence error (Codex review: "Fail closed
    # instead of sharing an uncopyable provider").
    class _Uncopyable:
        source = SourceName.ROBINHOOD

        def __init__(self, fn):
            self._historicals_fn = fn

        def __copy__(self):
            raise TypeError("not copyable")

    def _historicals(**kwargs):
        return {"results": []}

    provider = _Uncopyable(_historicals)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")

    with pytest.raises(RawInputPersistenceError, match="_Uncopyable"):
        _PersistingMarketDataProvider(provider, recorder)

    # And the caller's provider is left exactly as it arrived.
    assert provider._historicals_fn is _historicals


def test_a_copy_that_returns_the_same_object_is_rejected(tmp_path):
    # A successful copy is not automatically a PRIVATE one: __copy__ is
    # caller-defined and may return self (a singleton, a cached connection
    # wrapper). Rebinding would then land on the shared original and every
    # overlapping-run hazard is back, this time with no exception to notice it
    # by (Codex review: "Verify that the provider copy is actually private").
    class _SelfCopying:
        source = SourceName.ROBINHOOD

        def __init__(self, fn):
            self._historicals_fn = fn

        def __copy__(self):
            return self

    def _historicals(**kwargs):
        return {"results": []}

    provider = _SelfCopying(_historicals)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")

    with pytest.raises(RawInputPersistenceError, match="returned the same object"):
        _PersistingMarketDataProvider(provider, recorder)

    # The shared provider is left exactly as it arrived.
    assert provider._historicals_fn is _historicals


def test_a_copy_sharing_attribute_storage_is_rejected(tmp_path):
    # A distinct object can still share attribute storage: a __copy__ that
    # hands its own __dict__ to the new instance passes the identity check
    # while every rebinding still lands on the caller's provider, silently
    # restoring the overlapping-run fail-open (Codex review: "Verify copied
    # providers do not share attribute storage").
    class _SharedDict:
        source = SourceName.ROBINHOOD

        def __init__(self, fn):
            self._historicals_fn = fn

        def __copy__(self):
            twin = _SharedDict.__new__(_SharedDict)
            twin.__dict__ = self.__dict__  # distinct object, same storage
            return twin

    def _historicals(**kwargs):
        return {"results": []}

    provider = _SharedDict(_historicals)
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")

    with pytest.raises(RawInputPersistenceError, match="shares attribute storage"):
        _PersistingMarketDataProvider(provider, recorder)

    # The probe used to detect the sharing is fully undone.
    assert provider._historicals_fn is _historicals


def test_a_scheduled_run_blocks_when_the_provider_cannot_be_isolated(tmp_path):
    # The refusal reaches the caller as a normal blocked run_state -- written
    # before evaluate(), so nothing was computed and nothing placed -- rather
    # than as an escaping exception.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)

    class _UncopyableProvider(DecliningBarsProvider):
        def __copy__(self):
            raise TypeError("not copyable")

    data = _UncopyableProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    # The decorator only copies a provider that exposes a connector callable
    # to intercept (the bars come from the provider itself here; the callable
    # is never reached), so give it one -- exactly the shape the live
    # RobinhoodDataProvider has.
    data._historicals_fn = lambda **kwargs: {"results": []}
    broker = FakeLiveBroker()
    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["error_class"] == "missing_or_invalid_raw_inputs"
    assert "cannot isolate the injected market-data provider" in artifacts["block_reason"]
    assert artifacts["broker_outcomes"] == {}
    assert Path(res["paths"]["run_state"]).exists()


def test_persisting_adapters_do_not_mutate_caller_owned_broker_or_provider(tmp_path):
    # The wrappers must not leave a recorder-bound connector callable installed on the
    # caller's RobinhoodDataProvider/RobinhoodBroker instance after wrapping: a later
    # manual/watchlist run reusing the same instance must see the ORIGINAL connector,
    # not one bound to this (short-lived, possibly since-deleted) run's raw/ directory
    # (see PR #560 review discussion r3816306593).
    provider = RobinhoodDataProvider(
        historicals_fn=lambda **kwargs: {"results": []}, quotes_fn=lambda **kwargs: {"results": []}
    )
    broker = RobinhoodBroker(buying_power_fn=lambda **kwargs: {"buying_power": "1.00"})
    original_historicals_fn = provider._historicals_fn
    original_quotes_fn = provider._quotes_fn
    original_buying_power_fn = broker._buying_power_fn

    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped_provider = _PersistingMarketDataProvider(provider, recorder)
    wrapped_broker = _PersistingBrokerAdapter(broker, recorder)
    wrapped_provider.get_quote("AAPL", _now_live())
    wrapped_broker.available_buying_power()

    assert provider._historicals_fn is original_historicals_fn
    assert provider._quotes_fn is original_quotes_fn
    assert broker._buying_power_fn is original_buying_power_fn
    # The decorators still operate on wrapped connectors internally.
    assert wrapped_provider._inner is not provider
    assert wrapped_broker._inner is not broker


def test_scheduled_live_run_blocks_all_orders_when_one_market_fetch_raises(tmp_path):
    """A per-ticker connector outage IS caught by evaluate_signal() and
    degrades that one ticker to NO_DATA for reporting purposes, but the
    scheduled-live raw-persistence wrapper (_PersistingMarketDataProvider)
    records the SAME exception onto raw_recorder.error before re-raising it
    for evaluate_signal to catch. run() checks raw_recorder.error right after
    evaluate() (before any placement), so one ticker's connector outage fails
    the whole batch closed rather than silently placing orders from a batch
    whose raw evidence is incomplete (CLAUDE.md "Fail closed, always")."""

    class _PartialOutageProvider(DecliningBarsProvider):
        def get_previous_close_bar(self, ticker, as_of):
            if ticker == "BBB":
                raise RuntimeError("connector outage")
            return super().get_previous_close_bar(ticker, as_of)

    class _RecordingBroker(FakeLiveBroker):
        def __init__(self):
            self.place_calls = []

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.place_calls.append((ticker, notional_usd, ref_id))
            return super().place_market_buy(ticker, notional_usd, ref_id)

    pf_root = _seed_preflight(tmp_path / "pf", ("AAA", "BBB"))
    cfg = MorningConfig(
        tickers=("AAA", "BBB"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _RecordingBroker()
    data = _PartialOutageProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05, "BBB": 0.05})

    result = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE).run(_now_live(), data, broker=broker)

    assert result["artifacts"]["status"] == "blocked"
    assert result["artifacts"]["error_class"] == "missing_or_invalid_raw_inputs"
    assert "connector outage" in result["artifacts"]["raw_persistence_error"]
    assert broker.place_calls == []


def test_persisting_broker_adapter_records_buying_power_and_tradability(tmp_path):
    broker = FakeLiveBroker()
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(cast(Any, broker), recorder)
    assert wrapped.available_buying_power() == 500.0
    assert wrapped.is_tradable_for_fractional_buy("AAPL") is True
    assert recorder.error is None
    assert any(name.startswith("buying_power_") for name in recorder.saved_filenames)
    assert any(name.startswith("tradability_AAPL_") for name in recorder.saved_filenames)
    # The guarded same-stream duplicate query is also raw input, rather than
    # being forwarded unrecorded through __getattr__.
    assert (
        wrapped.same_day_strategy_order_exists_with_ref_id_prefix("AAPL", date(2026, 7, 8), [])
        is None
    )
    assert any(name.startswith("orders_since_midnight_AAPL_") for name in recorder.saved_filenames)


def test_persisting_broker_keeps_placement_responses_for_final_drain(tmp_path):
    class BufferedBroker(FakeLiveBroker):
        def __init__(self):
            super().__init__()
            self.responses = [{"id": "order-1", "state": "queued"}]

        def get_raw_placement_responses(self):
            responses, self.responses = self.responses, []
            return responses

    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(cast(Any, BufferedBroker()), recorder)
    assert wrapped.place_market_buy("AAPL", 5.0, "ref-1")
    assert wrapped.get_raw_placement_responses() == [{"id": "order-1", "state": "queued"}]
    assert wrapped.get_raw_placement_responses() == []


def test_persisting_broker_preserves_order_id_when_response_drain_fails(tmp_path):
    class DrainFailureBroker(FakeLiveBroker):
        def get_raw_placement_responses(self):
            raise OSError("unavailable")

    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(cast(Any, DrainFailureBroker()), recorder)
    assert wrapped.place_market_buy("AAPL", 5.0, "ref-1")
    assert recorder.error and "drain placement responses" in recorder.error


def test_persisting_broker_adapter_records_placement_response(tmp_path):
    # Codex review ("Persist broker placement responses"): a real-money
    # order response is exactly the evidence a disk/serialization failure
    # at this boundary must be able to set raw_persistence_error for -- so
    # the wrapper must persist it under raw/, not merely pass it through.
    broker = FakeLiveBroker()
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(cast(Any, broker), recorder)
    order_id = wrapped.place_market_buy("AAPL", 2.0, "ref-1")
    assert order_id
    assert recorder.error is None
    assert any(name.startswith("order_response_AAPL_") for name in recorder.saved_filenames)


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


def test_blocked_live_result_carries_raw_persistence_fields(tmp_path):
    # Codex review on PR #420 ("Retain the standard raw-persistence failure
    # fields"): the fresh blocked-stub path in _blocked_live_result() bypasses
    # the normal end-of-run() block that stamps raw_input_dir/raw_inputs_saved/
    # raw_persistence_error, so a blocked scheduled run must not lose those
    # documented machine-readable fields just because the failure happened
    # before broker placement.
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
    assert artifacts["raw_input_dir"] == str(run_dir / "raw")
    assert artifacts["raw_inputs_saved"] == []
    assert "failed to persist" in artifacts["raw_persistence_error"]
    on_disk = json.loads(Path(res["paths"]["run_state"]).read_bytes())
    assert "raw_persistence_error" in on_disk


class _PartialSubmitThenRawFailBroker(FakeLiveBroker):
    """Submits the first candidate, then breaks raw persistence for the next.

    Simulates a raw-persistence failure that happens mid-loop, AFTER an
    earlier candidate has already been submitted -- the scenario Codex/Copilot
    review flagged on PR #420 ("Preserve orders submitted before a later
    persistence failure" / "Surface persistence failures on scheduled
    retries"): the already-placed order must survive in the final artifact
    instead of being discarded by a blank ``broker_outcomes={}`` stub.
    """

    def __init__(self, raw_dir):
        self._raw_dir = raw_dir
        self._tradability_calls = 0

    def is_tradable_for_fractional_buy(self, ticker):
        self._tradability_calls += 1
        if self._tradability_calls > 1:
            # Corrupt the raw directory so the NEXT persisted broker response
            # (this tradability check, for the second candidate) fails to
            # save -- but only after the first candidate has already been
            # fully placed.
            if self._raw_dir.is_dir():
                import shutil

                shutil.rmtree(self._raw_dir)
            self._raw_dir.write_text("not a directory", encoding="utf-8")
        return True


def test_blocked_live_run_preserves_earlier_submission_on_later_raw_failure(tmp_path):
    pf_root = _seed_preflight(tmp_path / "pf", ("AAA", "BBB"))
    cfg = MorningConfig(
        tickers=("AAA", "BBB"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    raw_dir = ex.run_state_dir(_now_live()) / "raw"
    broker = _PartialSubmitThenRawFailBroker(raw_dir)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05, "BBB": 0.05})

    res = ex.run(_now_live(), data, broker=broker)
    artifacts = res["artifacts"]

    assert artifacts["status"] == "blocked"
    assert artifacts["error_class"] == "missing_or_invalid_raw_inputs"
    assert artifacts["broker_outcomes"]["AAA"]["status"] == "submitted"
    submitted_rows = [c for c in artifacts["buy_candidates"] if c.get("status") == "submitted"]
    assert {c["ticker"] for c in submitted_rows} == {"AAA"}
    trading_day = _now_live().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(trading_day) == artifacts["broker_outcomes"]["AAA"]["submitted_amount_usd"]
    on_disk = json.loads(Path(res["paths"]["run_state"]).read_bytes())
    assert on_disk["broker_outcomes"]["AAA"]["status"] == "submitted"


def test_blocked_partial_submission_run_denies_scheduled_evidence(tmp_path):
    # Codex review, PR #538 discussion_r3837449173 ("Deny scheduled evidence
    # for partial persistence failures"): _apply_no_order_classification() sees
    # a "submitted" outcome with no failed/blocked rows of its own (the later
    # candidate never got evaluated at all) and returns EARLY without setting
    # run_classification_outcome/is_trading_failure. Without an explicit
    # scheduled_evidence_denied marker, derive_valid_scheduled_evidence() would
    # incorrectly treat this failed partial run as valid "the scheduled slot
    # ran successfully" evidence.
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    pf_root = _seed_preflight(tmp_path / "pf", ("AAA", "BBB"))
    cfg = MorningConfig(
        tickers=("AAA", "BBB"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    raw_dir = ex.run_state_dir(_now_live()) / "raw"
    broker = _PartialSubmitThenRawFailBroker(raw_dir)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05, "BBB": 0.05})

    res = ex.run(_now_live(), data, broker=broker)
    artifacts = res["artifacts"]

    assert artifacts["status"] == "blocked"
    assert artifacts["broker_outcomes"]["AAA"]["status"] == "submitted"
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(artifacts) is False
    on_disk = json.loads(Path(res["paths"]["run_state"]).read_bytes())
    assert on_disk["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(on_disk) is False


def test_blocked_live_retry_clears_stale_raw_failure_metadata(tmp_path):
    # Codex review on PR #420 ("Clear stale failure metadata when rewriting a
    # retry"): when the preserved artifact came from a raw-persistence
    # failure (error_class/raw_persistence_error set) and a LATER retry fails
    # for an unrelated, non-raw reason (missing broker), the rewritten
    # artifact must not keep attributing the current wiring failure to the
    # earlier, different raw-input error.
    pf_root = _seed_preflight(tmp_path / "pf", ("AAA", "BBB"))
    cfg = MorningConfig(
        tickers=("AAA", "BBB"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    raw_dir = ex.run_state_dir(_now_live()) / "raw"
    broker = _PartialSubmitThenRawFailBroker(raw_dir)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05, "BBB": 0.05})

    first = ex.run(_now_live(), data, broker=broker)
    assert first["artifacts"]["status"] == "blocked"
    assert first["artifacts"]["error_class"] == "missing_or_invalid_raw_inputs"
    assert "raw_persistence_error" in first["artifacts"]
    run_state_path = Path(first["paths"]["run_state"])

    # The first attempt's broker deliberately corrupted raw/ (a file instead
    # of a directory) to force its own raw-persistence failure -- repair it so
    # the second attempt's own raw writes succeed and its failure is purely
    # the (unrelated) missing-broker wiring problem under test.
    if raw_dir.is_file():
        raw_dir.unlink()

    # Second attempt at the SAME slot: a wiring failure unrelated to raw
    # persistence. The earlier submission must survive, but the stale raw
    # error_class/raw_persistence_error must NOT be carried forward.
    second = ex.run(_now_live(), data, broker=None)

    assert second["artifacts"]["status"] == "blocked"
    assert second["artifacts"]["block_reason"] == "missing broker adapter for scheduled live execution"
    # The stale "missing_or_invalid_raw_inputs" from the FIRST attempt must not
    # bleed into this attempt's own, unrelated wiring failure -- a generic
    # before-placement fallback is stamped instead, never the earlier value.
    from scripts.run_classification import ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT

    assert second["artifacts"]["error_class"] == ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT
    assert "raw_persistence_error" not in second["artifacts"]
    submitted_rows = [c for c in second["artifacts"]["buy_candidates"] if c.get("status") == "submitted"]
    assert {c["ticker"] for c in submitted_rows} == {"AAA"}
    on_disk = json.loads(run_state_path.read_bytes())
    assert on_disk["error_class"] == ERROR_CLASS_BLOCKED_BEFORE_PLACEMENT
    assert "raw_persistence_error" not in on_disk
    assert on_disk["broker_outcomes"]["AAA"]["status"] == "submitted"


def test_blocked_retry_after_full_earlier_submission_denies_scheduled_evidence(tmp_path):
    # Codex review, PR #538 discussion_r3837489852 ("Deny scheduled evidence
    # for preserved blocked retries"). Distinct from
    # test_blocked_partial_submission_run_denies_scheduled_evidence (which
    # covers raw persistence failing WITHIN the same run, right after a
    # partial submission, via raw_persistence_partial_block): here an EARLIER
    # attempt at this slot fully completed and submitted an order, and a
    # LATER retry is blocked for an unrelated wiring reason (a missing broker
    # adapter) that never touches raw persistence at all. The wholesale-
    # preservation path in _blocked_live_result() copied the earlier
    # attempt's success classification unchanged, so
    # derive_valid_scheduled_evidence() still reported this failed retry as
    # valid scheduled evidence.
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    preflight_root = tmp_path / "preflight"
    _seed_preflight(preflight_root, ("AAA",))
    cfg = MorningConfig(
        tickers=("AAA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(preflight_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05})
    broker = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 95.0})

    first = ex.run(_now_live(), data, broker=broker, manual=False)
    assert first["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"
    assert derive_valid_scheduled_evidence(first["artifacts"]) is True

    # Second attempt at the SAME scheduled slot: this time the broker wiring
    # itself is missing, an unrelated failure from the earlier submission.
    second = ex.run(_now_live(), data, broker=None)

    artifacts = second["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["block_reason"] == "missing broker adapter for scheduled live execution"
    # The earlier submission must still be intact and paired with its signals...
    assert artifacts["broker_outcomes"]["AAA"]["status"] == "submitted"
    # ...but THIS attempt's own failure must not be hidden behind it.
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(artifacts) is False
    on_disk = json.loads(Path(second["paths"]["run_state"]).read_bytes())
    assert on_disk["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(on_disk) is False


def test_blocked_retry_preserves_raw_input_references_after_full_submission(tmp_path):
    # Codex review, PR #538 discussion_r3837489853 ("Preserve original
    # raw-input references on blocked retries"). Same scenario as the
    # scheduled-evidence-denial test above (earlier attempt fully submitted,
    # later retry blocked by a missing broker) but asserting the OTHER half
    # of that finding: the earlier attempt's raw_input_dir/raw_inputs_saved
    # must survive being layered with THIS attempt's own (non-raw) failure,
    # since the original raw files that justified the submitted order are
    # still on disk and this retry's own raw_recorder is never even passed
    # to _blocked_live_result() on the missing-broker path.
    preflight_root = tmp_path / "preflight"
    _seed_preflight(preflight_root, ("AAA",))
    cfg = MorningConfig(
        tickers=("AAA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(preflight_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05})
    broker = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 95.0})

    first = ex.run(_now_live(), data, broker=broker, manual=False)
    assert first["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"
    original_raw_input_dir = first["artifacts"].get("raw_input_dir")
    original_raw_inputs_saved = first["artifacts"].get("raw_inputs_saved")
    assert original_raw_input_dir
    assert original_raw_inputs_saved

    second = ex.run(_now_live(), data, broker=None)

    artifacts = second["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["broker_outcomes"]["AAA"]["status"] == "submitted"
    assert artifacts["raw_input_dir"] == original_raw_input_dir
    assert artifacts["raw_inputs_saved"] == original_raw_inputs_saved
    on_disk = json.loads(Path(second["paths"]["run_state"]).read_bytes())
    assert on_disk["raw_input_dir"] == original_raw_input_dir
    assert on_disk["raw_inputs_saved"] == original_raw_inputs_saved


def test_blocked_retry_merges_raw_filenames_with_preserved_submission(tmp_path):
    # Codex review, PR #654 discussion_r3837649719 ("Retain earlier raw
    # filenames when rewriting a blocked retry"): unlike the missing-broker
    # scenario above (where THIS retry's raw_recorder is never even passed to
    # _blocked_live_result()), a retry that saves at least one NEW raw file
    # before its own raw persistence fails was replacing the preserved
    # artifact's raw_inputs_saved list outright instead of merging with it --
    # even though the earlier attempt's raw files are still on disk under a
    # lower sequence number in the very same raw_dir.
    preflight_root = tmp_path / "preflight"
    _seed_preflight(preflight_root, ("AAA",))
    cfg = MorningConfig(
        tickers=("AAA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(preflight_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05})
    broker = RecordingLiveBroker(account="999000111", reference_prices={"AAA": 95.0})

    first = ex.run(_now_live(), data, broker=broker, manual=False)
    assert first["artifacts"]["broker_outcomes"]["AAA"]["status"] == "submitted"
    original_raw_inputs_saved = first["artifacts"]["raw_inputs_saved"]
    assert original_raw_inputs_saved

    # Simulate a second attempt whose own raw_recorder manages to save one
    # new file (a later ticker's prior-close, say) before hitting an error.
    raw_recorder = _RawInputRecorder(Path(first["artifacts"]["raw_input_dir"]), "2026-07-08")
    new_filename = "prior_close_ZZZZ_99.json"
    raw_recorder.saved_filenames.append(new_filename)
    raw_recorder.error = "simulated failure on a later ticker"

    result = ex._blocked_live_result(
        _now_live(),
        f"raw input persistence failed: {raw_recorder.error}",
        [],
        "robinhood",
        error_class="missing_or_invalid_raw_inputs",
        raw_recorder=raw_recorder,
    )

    artifacts = result["artifacts"]
    assert artifacts["broker_outcomes"]["AAA"]["status"] == "submitted"
    assert artifacts["raw_inputs_saved"][: len(original_raw_inputs_saved)] == original_raw_inputs_saved
    assert new_filename in artifacts["raw_inputs_saved"]
    on_disk = json.loads(Path(result["paths"]["run_state"]).read_bytes())
    assert on_disk["raw_inputs_saved"][: len(original_raw_inputs_saved)] == original_raw_inputs_saved
    assert new_filename in on_disk["raw_inputs_saved"]


def test_blocked_live_retry_preserves_evidence_from_partial_run_state(tmp_path):
    # Codex review on PR #420 ("Validate preserved artifacts before rewriting
    # them"): a preserved run_state that only has the minimal shape
    # _existing_submitted_run_state() actually validates (a submitted
    # buy_candidates/broker_outcomes row) -- and is otherwise missing
    # run_classification/budget/signal_rows -- must not raise KeyError when
    # the next blocked retry rewrites it. Regression for
    # {"buy_candidates": [{"status": "submitted"}]}.
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
    (run_dir / "run_state.json").write_text(
        json.dumps({"buy_candidates": [{"ticker": "SMALL", "status": "submitted"}]}),
        encoding="utf-8",
    )

    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, broker=None)

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["block_reason"] == "missing broker adapter for scheduled live execution"
    submitted_rows = [c for c in artifacts["buy_candidates"] if c.get("status") == "submitted"]
    assert {c["ticker"] for c in submitted_rows} == {"SMALL"}
    assert Path(res["paths"]["run_state"]).exists()


def test_blocked_live_retry_falls_back_when_preserved_fields_are_wrong_typed(tmp_path):
    # Codex review, PR #538 discussion_r3837489862 ("Validate preserved field
    # types before rewriting retries"). Distinct from
    # test_blocked_live_retry_preserves_evidence_from_partial_run_state
    # (which covers those keys being ABSENT): here run_classification,
    # signal_rows, and budget are all PRESENT but hold invalid values (null),
    # exactly the shape a presence-only check would wave through and
    # write_artifacts() would then subscript/iterate and raise on. This must
    # fall back to the graft-based path instead of raising.
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
    (run_dir / "run_state.json").write_text(
        json.dumps(
            {
                "run_id": "stale-run-id",
                "run_classification": None,
                "signal_rows": None,
                "budget": None,
                "buy_candidates": [{"ticker": "SMALL", "status": "submitted"}],
            }
        ),
        encoding="utf-8",
    )

    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, broker=None)

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["block_reason"] == "missing broker adapter for scheduled live execution"
    assert isinstance(artifacts["run_classification"], dict)
    assert isinstance(artifacts["signal_rows"], list)
    assert isinstance(artifacts["budget"], dict)
    submitted_rows = [c for c in artifacts["buy_candidates"] if c.get("status") == "submitted"]
    assert {c["ticker"] for c in submitted_rows} == {"SMALL"}
    assert Path(res["paths"]["run_state"]).exists()


class _Unserializable:
    def __repr__(self):
        return "<unserializable>"


class _BadTradabilityBroker(FakeLiveBroker):
    def __init__(self):
        self.place_calls = []

    def is_tradable_for_fractional_buy(self, ticker):
        return _Unserializable()

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.place_calls.append((ticker, notional_usd, ref_id))
        return super().place_market_buy(ticker, notional_usd, ref_id)

def test_raw_persistence_failure_during_this_candidate_blocks_this_candidate(tmp_path):
    # Codex review ("Recheck persistence before submitting the current
    # candidate"): _BadTradabilityBroker sets raw_recorder.error from inside
    # is_tradable_for_fractional_buy() -- AFTER the top-of-loop gate for this
    # very candidate has already passed. Checking only at the top of the loop
    # blocked the NEXT candidate while still submitting THIS one from an input
    # nothing had durably recorded, which is exactly the fail-closed contract
    # the mid-batch gate exists to enforce. With only one ticker in the
    # universe there IS no next candidate, so the single order went in.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    broker = _BadTradabilityBroker()
    res = ex.run(_now_live(), data, broker=broker)
    artifacts = res["artifacts"]

    outcome = artifacts.get("broker_outcomes", {}).get("SMALL")
    assert outcome is not None
    assert outcome["status"] == "blocked", (
        "the candidate whose OWN broker call broke persistence must not be "
        "submitted -- the re-check before place_market_buy is what catches it"
    )
    assert outcome["raw_persistence_blocked"] is True
    assert outcome.get("order_id") is None
    assert artifacts["orders_submitted_count"] == 0

    # The run itself is still published as the failure it is.
    assert "raw_persistence_error" in artifacts
    assert artifacts.get("status") == "blocked"
    assert artifacts.get("error_class") == "missing_or_invalid_raw_inputs"
    assert "raw input persistence failed during placement" in artifacts.get("block_reason", "")

    # Codex review ("Deny scheduled evidence after raw persistence fails"): the
    # classification was stamped BEFORE _apply_no_order_classification, which
    # then overwrote error_class with None and marked the run
    # completed_no_orders, and nothing ever denied the scheduled evidence.
    # derive_valid_scheduled_evidence() does not read the top-level status, so
    # the pointer could advertise a run that violated the raw-input persistence
    # contract as a valid scheduled run, suppressing recovery and watchdog
    # handling despite the failed heartbeat.
    assert artifacts["run_classification_outcome"] == "failed_scheduled_run_startup"
    assert artifacts["order_submit_status"] == "not_submitted_error"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["scheduled_evidence_denied"] is True
    assert artifacts["run_classification"]["valid_scheduled_evidence"] is False


class _BuyingPowerFailureBroker(FakeLiveBroker):
    def available_buying_power(self):
        raise ConnectionError("broker temporarily unavailable")


def test_scheduled_live_run_persists_block_when_buying_power_lookup_fails(tmp_path, monkeypatch):
    """A transient account lookup failure must leave durable scheduled evidence."""
    heartbeat_results = []
    monkeypatch.setattr("dca_alerts.send_heartbeat", lambda _stream, *, ok: heartbeat_results.append(ok))
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    live_root = tmp_path / "live"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(_now_live(), data, broker=_BuyingPowerFailureBroker())

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["block_reason"] == (
        "failed to retrieve broker buying power: broker temporarily unavailable"
    )
    assert artifacts["error_class"] == "buying_power_lookup_failed"
    assert artifacts["broker_outcomes"] == {}
    assert heartbeat_results == [False]
    assert Path(res["paths"]["run_state"]).exists()
    pointer = json.loads((live_root / ex.latest_pointer_name).read_bytes())
    assert pointer["run_state_path"] == res["paths"]["run_state"]


def test_scheduled_live_run_fails_the_heartbeat_when_a_trading_failure_is_recorded(tmp_path, monkeypatch):
    """A recorded trading failure must clear a FAILED heartbeat, not a healthy one.

    Codex review, PR #688 discussion_r3925372792 ("Fail the heartbeat for
    placement failures"): _apply_no_order_classification() stamps
    is_trading_failure=True on the artifact when a placement attempt itself
    failed (timeout, terminal rejection) after passing every earlier gate --
    the call still returns normally rather than raising, so without
    guarded_run's result_ok predicate reading this field an intended order
    lost to a placement failure read as a healthy run and cleared the dead-
    man switch exactly like a genuinely clean, zero-order day.
    """
    heartbeat_results = []
    monkeypatch.setattr("dca_alerts.send_heartbeat", lambda _stream, *, ok: heartbeat_results.append(ok))

    def _reject_for_small(**kwargs):
        return {"data": {"order": {"id": "ord-1", "state": "rejected", "cumulative_quantity": "0"}}}

    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    broker = RobinhoodBroker(place_fn=_reject_for_small, enable_live_orders=True, **_LIVE_LOOKUP_STUBS)

    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    assert artifacts["is_trading_failure"] is True
    # The mocked connector returns a definitive terminal "rejected" state (not
    # a raw timeout with no broker response observed), so this is a PROVEN
    # rejection -- "failed" -- not the "potentially_submitted" status reserved
    # for a genuinely ambiguous OrderPlacementAmbiguousError outcome (Codex
    # review, PR #1300 discussion_r3920155064: "Emit the new
    # ambiguous-placement status" describes that separate, timeout-shaped
    # scenario, not this one).
    assert artifacts["broker_outcomes"]["SMALL"]["status"] == "failed"
    # The heartbeat must report the run as FAILED, never healthy, even though
    # the call returned a structured result instead of raising.
    assert heartbeat_results == [False]


def test_blocked_buying_power_failure_retains_raw_input_metadata(tmp_path):
    # Codex review, PR #538 discussion_r3837449175 ("Retain raw-input metadata
    # on buying-power failures"): market data and earlier broker checks are
    # already persisted via the raw recorder by the time the buying-power
    # lookup raises _BuyingPowerLookupError, but the _blocked_live_result()
    # call in that branch omitted raw_recorder -- so the blocked artifact had
    # no raw_input_dir/raw_inputs_saved fields even though the raw files
    # actually exist on disk, breaking the audit/reporting contract that
    # promises those fields whenever raw inputs were captured.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    live_root = tmp_path / "live"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    res = ex.run(_now_live(), data, broker=_BuyingPowerFailureBroker())

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["error_class"] == "buying_power_lookup_failed"
    raw_dir = ex.run_state_dir(_now_live()) / "raw"
    assert artifacts["raw_input_dir"] == str(raw_dir)
    assert artifacts["raw_inputs_saved"]
    on_disk = json.loads(Path(res["paths"]["run_state"]).read_bytes())
    assert on_disk["raw_input_dir"] == str(raw_dir)
    assert on_disk["raw_inputs_saved"]
    assert on_disk["scheduled_evidence_denied"] is True
    assert on_disk["error_class"] == "buying_power_lookup_failed"


def test_scheduled_live_run_blocks_when_production_broker_swallows_buying_power_failure(tmp_path, monkeypatch):
    """A real ``RobinhoodBroker`` never lets a buying-power connector error escape.

    PR #1299 discussion_r3925731478 ("Propagate swallowed buying-power
    connector failures"): unlike the test-double brokers above,
    ``RobinhoodBroker.available_buying_power()`` catches ANY exception from
    its injected ``_buying_power_fn`` and returns a clean ``0.0`` -- so
    ``broker.available_buying_power()`` never raises and the
    ``_BuyingPowerLookupError`` branch in ``_process_candidate`` is never
    reached. Without ``_RawInputRecorder.record_error()`` also stamping the
    unified ``error`` field for this failure (see its own docstring), the
    run would read the swallowed ``0.0`` as a genuine, deliberate
    insufficient-buying-power skip and publish a clean completed result,
    even though the broker connector never actually answered -- silently
    suppressing recovery for a real outage. Assert the run instead fails
    closed via ``block_if_raw_persistence_failed()``, which reads
    ``raw_recorder.error``.
    """
    heartbeat_results = []
    monkeypatch.setattr("dca_alerts.send_heartbeat", lambda _stream, *, ok: heartbeat_results.append(ok))
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    live_root = tmp_path / "live"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(live_root),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    def _raising_buying_power_fn(**kwargs):
        raise ConnectionError("broker temporarily unavailable")

    broker = RobinhoodBroker(
        buying_power_fn=_raising_buying_power_fn,
        orders_fn=_no_orders_today,
        tradability_fn=_all_tradable,
        enable_live_orders=True,
    )

    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    outcome = artifacts["broker_outcomes"]["SMALL"]
    assert outcome["status"] == "blocked"
    assert outcome["blocked_by_raw_persistence"] is True
    assert "broker temporarily unavailable" in outcome["skip_reason"]
    assert heartbeat_results == [False]


class _FailAfterFirstTradabilityBroker(FakeLiveBroker):
    """Serializable for the FIRST ticker, unserializable from the second on.

    Stands in for a disk/serialization failure that begins partway through the
    batch -- the shape the pre-loop gate cannot see.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen: set = set()

    def is_tradable_for_fractional_buy(self, ticker) -> Any:
        if self._seen:
            return _Unserializable()
        self._seen.add(ticker)
        return True


def test_placement_stops_after_raw_persistence_fails_mid_batch(tmp_path):
    # Codex review ("Stop placement when broker raw persistence fails"): the
    # recorder was only checked once BEFORE the loop and again after the run.
    # A failure that begins during a broker call therefore let every LATER
    # candidate keep submitting from inputs nothing had durably recorded.
    pf_root = _seed_preflight(tmp_path / "pf", ("AAA", "BBB", "CCC"))
    cfg = MorningConfig(
        tickers=("AAA", "BBB", "CCC"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(
        SourceName.ROBINHOOD, 100.0, {"AAA": 0.03, "BBB": 0.03, "CCC": 0.03}
    )

    res = ex.run(_now_live(), data, broker=_FailAfterFirstTradabilityBroker())
    artifacts = res["artifacts"]
    outcomes = artifacts.get("broker_outcomes", {})

    # Once the recorder has an error, every remaining candidate is explicitly
    # blocked for that reason rather than quietly submitted.
    blocked_for_raw = [
        ticker
        for ticker, outcome in outcomes.items()
        if isinstance(outcome, dict) and outcome.get("raw_persistence_blocked")
    ]
    assert blocked_for_raw, "later candidates must be stopped, not submitted unaudited"
    for ticker in blocked_for_raw:
        assert outcomes[ticker]["status"] == "blocked"
        assert "raw input persistence failed" in outcomes[ticker]["skip_reason"]

    # The order that DID land before the failure keeps its evidence: it passed
    # every safety check, and a failure to also write its audit copy to disk
    # does not retroactively un-place it.
    assert outcomes["AAA"]["status"] == "submitted"
    assert artifacts["orders_submitted_count"] == 1
    assert artifacts["partial_placement_before_failure"] is True

    # And the run is classified as the partial failure it is.
    assert artifacts.get("status") == "blocked"
    assert artifacts.get("error_class") == "missing_or_invalid_raw_inputs"
    # Codex review ("Deny scheduled evidence after raw persistence fails"): on
    # THIS branch _apply_no_order_classification left the submitted run's
    # ordinary classification intact, so without the stamp below the artifact
    # read as a healthy scheduled run that had simply placed one order. Gated
    # on the submitted count, so a slot that really did deploy capital is
    # labelled a post-submission failure rather than a startup failure.
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["order_submit_status"] == "submitted"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["scheduled_evidence_denied"] is True
    assert artifacts["run_classification"]["valid_scheduled_evidence"] is False


class _UnserializableFalsy:
    """Unserializable like _Unserializable, but falsy, so the tradability gate
    deliberately skips the ticker and the run submits nothing at all."""

    def __repr__(self):
        return "<unserializable>"

    def __bool__(self):
        return False


class _BadTradabilityNoOrderBroker(FakeLiveBroker):
    def is_tradable_for_fractional_buy(self, ticker) -> Any:
        return _UnserializableFalsy()


class _PersistenceFailsAfterFirstTradabilityBroker(FakeLiveBroker):
    """Tradability succeeds for the first ticker and returns an unserializable
    value for the second, so raw persistence fails partway through the batch."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tradability_calls = 0

    def is_tradable_for_fractional_buy(self, ticker) -> Any:
        self._tradability_calls += 1
        if self._tradability_calls == 1:
            return True
        return _Unserializable()


def test_raw_persistence_abort_records_an_outcome_for_every_candidate(tmp_path):
    # Codex review, P2 ("Record outcomes for all candidates before aborting"):
    # a mid-batch raw-persistence failure stops placement, but it used to record
    # only the ticker that happened to be current. Every remaining candidate was
    # left absent from broker_outcomes, so its buy_candidates row kept NO status
    # -- and status is REQUIRED on every candidate by run_state.schema.json, so
    # the very failure artifact this path exists to write would not validate.
    # (The same absence also made the post-loop preserve_pending pass skip them,
    # dropping any threshold-crossing cumulative credit they had computed.)
    tickers = ("SMALL", "BIG", "MID")
    pf_root = _seed_preflight(tmp_path / "pf", tickers)
    cfg = MorningConfig(
        tickers=tickers,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(
        SourceName.ROBINHOOD, 100.0, {t: 0.03 for t in tickers}
    )

    artifacts = ex.run(
        _now_live(), data, broker=_PersistenceFailsAfterFirstTradabilityBroker()
    )["artifacts"]

    # The premise: persistence failed partway through a multi-candidate batch.
    assert artifacts["raw_persistence_error"]
    outcomes = artifacts["broker_outcomes"]
    blocked = [t for t, oc in outcomes.items() if oc.get("raw_persistence_blocked")]
    assert blocked, "expected at least one candidate blocked by the abort"

    # Every candidate has an outcome, so every row carries the schema's
    # required status field.
    rows = artifacts["buy_candidates"]
    assert rows, "expected buy candidates"
    for row in rows:
        assert row["ticker"] in outcomes, f"{row['ticker']} has no broker outcome"
        assert row.get("status"), f"{row['ticker']} row has no status"


def test_raw_persistence_failure_survives_the_no_order_classifier(tmp_path):
    # Codex review, P2 ("Preserve the raw-persistence failure classification"):
    # when raw persistence fails before any order is submitted, the run is
    # fail-classified -- but _apply_no_order_classification then rewrote
    # run_classification_outcome, order_submit_status, error_class, and
    # is_trading_failure wholesale, leaving an artifact whose status="failed"
    # and raw_persistence_error contradicted a completed_no_orders /
    # no_orders_submitted / is_trading_failure=False classification. Reporting
    # and recovery keyed on classification then read the persistence failure as
    # an ordinary no-order completion. The failure stamp is authoritative and
    # must be applied last.
    pf_root = _seed_preflight(tmp_path / "pf", ("SMALL",))
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    artifacts = ex.run(_now_live(), data, broker=_BadTradabilityNoOrderBroker())["artifacts"]

    # The premise: persistence failed and nothing was submitted.
    assert artifacts["raw_persistence_error"]
    assert artifacts["orders_submitted_count"] == 0

    # "blocked", not "failed": the run was stopped by a safeguard, not by an
    # escaping exception, and error_class names the actual cause more precisely
    # than the shared helper's generic startup label. Both are deliberate
    # overrides applied on top of stamp_failure_classification -- the point of
    # this regression is that the failure CLASSIFICATION survives
    # _apply_no_order_classification, which it does.
    assert artifacts["status"] == "blocked"
    assert artifacts["run_classification_outcome"] == "failed_scheduled_run_startup"
    assert artifacts["order_submit_status"] == "not_submitted_error"
    assert artifacts["error_class"] == "missing_or_invalid_raw_inputs"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["placement_evidence_incomplete"] is True
    # A failed run is never valid scheduled evidence.
    assert artifacts["run_classification"]["valid_scheduled_evidence"] is False


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
    entry = _json.loads(log_path.read_bytes().splitlines()[0])
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
    entry = _json.loads(log_path.read_bytes().splitlines()[0])
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



# --- Root-level artifact set and the spread gate (Jul 27 2026 12:30 / 1:30) ---


class _NoBidAskProvider(DecliningBarsProvider):
    """A live provider that captures no bid/ask, exactly as on 2026-07-27."""

    def get_bid_ask(self, ticker, as_of) -> Any:
        return None


class _PartialBidAskProvider(DecliningBarsProvider):
    """Bid/ask for some tickers only; the rest must keep being evaluated."""

    def __init__(self, source, previous_close, drops, *, with_quote):
        super().__init__(source, previous_close, drops)
        self._with_quote = set(with_quote)

    def get_bid_ask(self, ticker, as_of) -> Any:
        if ticker not in self._with_quote:
            return None
        return super().get_bid_ask(ticker, as_of)


def _live_executor(tmp_path, tickers):
    pf_root = _seed_preflight(tmp_path / "pf", tickers)
    cfg = MorningConfig(
        tickers=tuple(tickers),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
    )
    return MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)


def _lock_is_held(lock_path: Path) -> bool:
    """True when ``lock_path`` cannot be acquired right now."""
    if not lock_path.exists():
        return False
    try:
        with cross_process_lock(lock_path, timeout_seconds=0.05):
            return False
    except (TimeoutError, OSError):
        return True


class TestScheduledSlotLock:
    """A scheduled live attempt is serialized against other attempts for its slot.

    Without the lock, an external-scheduler retry that overlaps the original
    invocation has nothing serializing the two: both can pass the broker's
    check-then-place duplicate guard before either order becomes visible, and
    submit the same ticker allocation twice (Codex review: "Restore the
    scheduled-slot lock around live execution").
    """

    def test_scheduled_live_run_holds_the_slot_lock_through_placement(self, tmp_path):
        ex = _live_executor(tmp_path, ("NVDA",))
        lock_path = ex._slot_lock_path(_now_live())
        held: list[bool] = []

        # The broker call is the deepest point of the attempt: if the lock is
        # held there, it covers the calendar gate, raw recording and placement.
        class _LockObservingBroker(_TopUpBroker):
            def place_market_buy(self, *args, **kwargs):
                held.append(_lock_is_held(lock_path))
                return super().place_market_buy(*args, **kwargs)

        data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
        res = ex.run(_now_live(), data, broker=_LockObservingBroker())

        assert res["artifacts"]["broker_outcomes"]["NVDA"]["status"] == "submitted"
        assert held == [True], "the slot lock was not held while placing the order"
        # Released on the way out, so the next scheduled attempt is not blocked.
        assert not _lock_is_held(lock_path)

    def test_manual_and_watchlist_runs_never_take_the_slot_lock(self, tmp_path):
        # live_schedule_hardening_2026_07_20's applies_to is scoped to the
        # scheduled live task, so a manual retry must not be blocked by a
        # scheduled attempt's lock (nor take one of its own).
        ex = _live_executor(tmp_path, ("NVDA",))
        now = _now_live()
        lock_path = ex._slot_lock_path(now)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        holder = cross_process_lock(lock_path)
        holder.__enter__()
        try:
            data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
            res = ex.run(now, data, broker=_TopUpBroker(), manual=True)
        finally:
            holder.__exit__(None, None, None)

        assert res["artifacts"].get("status") != "blocked"
        assert res["paths"].get("run_state")


def test_morning_live_run_writes_the_complete_root_level_artifact_set(tmp_path):
    """Before this, only the read-only 9:45 pre-flight wrote run_summary.json, so
    every live morning/midday run directory failed the scheduled artifact
    contract's require_summary check."""
    from scripts.scheduled_artifacts import validate_run_artifacts

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_TopUpBroker())

    run_dir = Path(res["paths"]["run_state"]).parent
    for name in ("run_state.json", "run_summary.json", "morning_signals.csv", "submitted_orders.csv"):
        assert (run_dir / name).exists(), f"{name} missing at the run root"
    assert validate_run_artifacts(run_dir, require_summary=True)

    summary = json.loads((run_dir / "run_summary.json").read_bytes())
    assert summary["run_classification"] == res["artifacts"]["run_classification"]


def test_morning_live_no_signal_run_writes_csv_headers_not_empty_files(tmp_path):
    """A run that evaluated nothing previously wrote a zero-byte signals CSV."""
    ex = _live_executor(tmp_path, ("NVDA",))
    # No drop at all: nothing qualifies, so no signal row makes it to the CSV.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    res = ex.run(_now_live(), data, broker=_TopUpBroker())

    run_dir = Path(res["paths"]["run_state"]).parent
    for name in ("morning_signals.csv", "submitted_orders.csv"):
        with (run_dir / name).open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle), None)
        assert header and any(col.strip() for col in header), f"{name} has no header row"


def test_scheduled_live_run_fails_a_row_closed_when_its_spread_is_unproven(tmp_path):
    """A scheduled run is pre-authorized to place without review, so an unproven
    spread must block that row rather than be placed on."""
    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _TopUpBroker()
    data = _NoBidAskProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)

    assert broker.placed == {}, "placed an order on an unproven spread"
    outcome = res["artifacts"]["broker_outcomes"]["NVDA"]
    assert outcome["status"] == "blocked"
    assert outcome["spread_ok"] is False
    assert outcome["spread_status"] == "spread_unavailable"
    # Every candidate reached the gate and every one was failed closed there.
    assert res["artifacts"]["error_class"] == "missing_spread_data"
    assert res["artifacts"]["run_classification_outcome"] == "completed_no_orders"
    assert res["artifacts"]["order_submit_status"] == "no_orders_submitted"
    assert res["artifacts"]["is_trading_failure"] is False


def test_missing_spread_for_one_ticker_does_not_suppress_another(tmp_path):
    """Fail only the unproven rows closed; every other qualifying ticker proceeds."""
    ex = _live_executor(tmp_path, ("NVDA", "AMD"))
    broker = _TopUpBroker()
    data = _PartialBidAskProvider(
        SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10, "AMD": 0.10}, with_quote={"AMD"}
    )
    res = ex.run(_now_live(), data, broker=broker)

    assert "AMD" in broker.placed, "a proven-spread ticker must still be submitted"
    assert "NVDA" not in broker.placed
    outcomes = res["artifacts"]["broker_outcomes"]
    assert outcomes["NVDA"]["spread_status"] == "spread_unavailable"
    assert outcomes["AMD"]["spread_status"] == "spread_ok"
    # The run submitted an order, so it is not a no-order run at all.
    assert res["artifacts"]["orders_submitted_count"] == 1
    assert "run_classification_outcome" not in res["artifacts"]


def test_no_ladder_candidate_records_a_completion_reason_not_a_spread_error(tmp_path):
    """The 1:30 PM ET incident: nothing qualified, so nothing reached the spread
    gate and the missing spread was irrelevant to the outcome."""
    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _TopUpBroker()
    # No drop: no ticker qualifies on the ladder, and the provider has no bid/ask
    # either -- the outcome must still be attributed to the ladder, not the spread.
    data = _NoBidAskProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    res = ex.run(_now_live(), data, broker=broker)

    assert broker.placed == {}
    assert res["artifacts"]["run_classification_outcome"] == "completed_no_orders"
    assert res["artifacts"]["completion_reason"] == "no_ladder_candidates"
    assert res["artifacts"]["error_class"] is None
    assert res["artifacts"]["candidates_reaching_spread_gate"] == 0
    assert res["artifacts"]["is_trading_failure"] is False


def test_manual_run_is_not_blocked_by_an_unproven_spread(tmp_path):
    """Manual runs still go through review_market_buy as a human-reviewed safety
    net, so they are not blocked on a missing spread alone."""
    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _TopUpBroker()
    data = _NoBidAskProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    ex.run(_now_live(), data, broker=broker, manual=True)

    assert "NVDA" in broker.placed


def test_scheduled_startup_failure_still_writes_the_complete_artifact_set(tmp_path):
    """The 2026-07-27 12:30 failure mode: the run raised BEFORE any writer, so the
    slot produced no artifact root at all and looked like it never fired."""
    from scripts.scheduled_artifacts import validate_run_artifacts

    ex = _live_executor(tmp_path, ("NVDA",))
    # No qualifying drop, so the run reaches no placement at all: a true startup
    # failure with nothing to preserve.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})

    def _explode(*_args, **_kwargs):
        raise RuntimeError("connector exploded before any artifact was written")

    # Fails after the run starts but before write_artifacts is ever reached --
    # the shape of the 12:30 startup failure.
    ex.build_artifacts = _explode

    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    run_dir = ex.run_state_dir(_now_live())
    for name in ("run_state.json", "run_summary.json", "morning_signals.csv", "submitted_orders.csv"):
        assert (run_dir / name).exists(), f"{name} missing after a startup failure"
    assert validate_run_artifacts(run_dir, require_summary=True)

    run_state = json.loads((run_dir / "run_state.json").read_bytes())
    assert run_state["status"] == "failed"
    assert run_state["error_class"] == "scheduled_run_startup_failure"
    # NOT the placement-failure outcome: no placement was attempted, so reporting
    # keyed on that would misattribute a startup failure as a broker failure.
    assert run_state["run_classification_outcome"] == "failed_scheduled_run_startup"
    assert run_state["is_trading_failure"] is True
    assert run_state["orders_submitted_count"] == 0
    # Still attributable to its stream, but never scheduled evidence.
    assert run_state["run_classification"]["stream"] == "morning"
    assert run_state["run_classification"]["valid_scheduled_evidence"] is False


def test_startup_failure_preserves_ambiguous_placement_candidate_marker(tmp_path):
    """A synthesized failure artifact must continue reserving ambiguous spend."""
    ex = _live_executor(tmp_path, ("NVDA",))
    ex._in_flight_signals_by_ticker = {"NVDA": _isolation_signal("NVDA")}
    ex._in_flight_broker_outcomes = {
        "NVDA": {
            "status": "failed",
            "order_id": None,
            "skip_reason": "ambiguous placement exception: RuntimeError",
            "submitted_amount_usd": 2.0,
            "ambiguous_placement_exception": True,
        }
    }

    ex._write_startup_failure_artifacts(_now_live(), RuntimeError("later artifact failure"))

    run_state = json.loads((ex.run_state_dir(_now_live()) / "run_state.json").read_bytes())
    assert run_state["buy_candidates"][0]["ambiguous_placement_exception"] is True


def test_startup_failure_artifacts_are_published_under_the_slot_lock(tmp_path, monkeypatch):
    # Codex review, P1 ("Keep failure-artifact writes inside the slot lock"):
    # run() serializes the dated live slot, but the failure writer used to run
    # OUTSIDE that lock. Publishing from an outer handler is a race even for an
    # attempt that legitimately owned the slot: the `with` releases the lock as
    # the exception unwinds, a waiter polling every 20ms takes it immediately,
    # and guarded_run's own failure path spends seconds on alert/heartbeat
    # network calls before the write lands. So the publication happens inside
    # the SAME critical section as the run body -- ONE acquisition spanning
    # both, never a release-and-reacquire (Codex review: "Publish owned-slot
    # failures before releasing the lock").
    import morning_dca as _morning_dca
    from scripts.file_lock import cross_process_lock

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})

    acquired: list = []
    real_lock = cross_process_lock

    @contextlib.contextmanager
    def _tracking_lock(lock_path, **kwargs):
        acquired.append(Path(lock_path))
        with real_lock(lock_path, **kwargs):
            yield

    monkeypatch.setattr(_morning_dca, "cross_process_lock", _tracking_lock)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("connector exploded before any artifact was written")

    ex.build_artifacts = _explode

    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    run_dir = ex.run_state_dir(_now_live())
    lock_path = run_dir / "scheduled-slot.lock"
    # Exactly once on the slot lock: the run body and the failure publication
    # that follows it share the one acquisition, so no waiter can enter the
    # slot between them. (Other lock files -- the raw recorder's own sequence
    # lock -- are unrelated and not part of this assertion.)
    assert [path for path in acquired if path == lock_path] == [lock_path]
    assert (run_dir / "run_state.json").exists()
    assert json.loads((run_dir / "run_state.json").read_bytes())["status"] == "failed"


def test_a_timed_out_retry_is_recorded_as_a_genuine_failed_start(tmp_path, monkeypatch):
    # The same misattribution, one step earlier: an attempt whose acquisition
    # TIMES OUT never enters the slot and provably writes nothing, but its
    # pre-wait probe baseline (no artifact yet) plus the holder's write during
    # the wait made the probe report "this attempt's artifact is on disk". The
    # marker must record a genuine failed start instead.
    import functools
    import threading
    import time

    import dca_alerts
    import morning_dca as _morning_dca
    from scripts.file_lock import cross_process_lock

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "run_state.json"
    assert not state_path.exists()  # nothing to baseline against yet

    phases = []
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda *args, **kwargs: phases.append(kwargs.get("run_artifact_written")),
    )
    # Real lock semantics without the 30-second production wait.
    monkeypatch.setattr(
        _morning_dca,
        "cross_process_lock",
        functools.partial(cross_process_lock, timeout_seconds=0.5, poll_seconds=0.01),
    )

    holder_entered = threading.Event()

    def _holder():
        with cross_process_lock(run_dir / "scheduled-slot.lock"):
            holder_entered.set()
            # Publish AFTER the waiter has taken its pre-wait snapshot and
            # well before the waiter's 0.5s acquisition timeout expires.
            time.sleep(0.1)
            state_path.write_text('{"holder": true}', encoding="utf-8")
            # Keep holding past the waiter's timeout.
            time.sleep(0.9)

    holder = threading.Thread(target=_holder)
    holder.start()
    assert holder_entered.wait(timeout=5)

    with pytest.raises(TimeoutError):
        ex.run(_now_live(), data, broker=_TopUpBroker())
    holder.join(timeout=5)

    assert phases == [False]


def test_a_queued_retry_is_not_credited_with_the_holders_artifact_write(tmp_path, monkeypatch):
    # Codex review, P2 ("Snapshot artifact freshness after acquiring the slot
    # lock"): the freshness probe used to be baselined before _invoke() even
    # tried to acquire the lock. A retry queued behind the lock can wait there
    # while the HOLDER writes run_state.json; that write postdates the
    # waiter's baseline, so if the waiter then crashes before writing anything
    # of its own, guarded_run credited it with the holder's mtime bump and
    # recorded failure_phase="after_run_artifact" -- which
    # _is_failed_start_marker() excludes from the recovery and error counts,
    # losing a genuine failed start.
    import threading
    import time

    import dca_alerts
    from scripts.file_lock import cross_process_lock

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "run_state.json"
    # A stale artifact from an earlier attempt at this same slot.
    state_path.write_text('{"stale": true}', encoding="utf-8")

    phases = []
    monkeypatch.setattr(
        dca_alerts,
        "write_failed_start_marker",
        lambda *args, **kwargs: phases.append(kwargs.get("run_artifact_written")),
    )

    holder_entered = threading.Event()

    def _holder():
        with cross_process_lock(run_dir / "scheduled-slot.lock"):
            holder_entered.set()
            # Publish while the retry below is queued on the lock, so the
            # write lands AFTER the retry's pre-lock snapshot would have been
            # taken and BEFORE the retry gets in.
            time.sleep(0.2)
            state_path.write_text('{"holder": true}', encoding="utf-8")

    holder = threading.Thread(target=_holder)
    holder.start()
    assert holder_entered.wait(timeout=5)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("the retry crashed before writing anything of its own")

    ex.build_artifacts = _explode

    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())
    holder.join(timeout=5)

    # The retry wrote nothing of its own, so it is a genuine failed start.
    assert phases == [False]


def test_a_timed_out_slot_lock_publishes_no_failure_artifacts(tmp_path, monkeypatch):
    # The case the P1 was actually about: an overlapping retry whose own lock
    # acquisition times out never entered the slot, while the holder is still
    # running and will publish its own canonical set. Writing this attempt's
    # failure artifacts anyway would interleave with that publication. The
    # reacquisition fails closed instead -- the directory is left entirely to
    # the holder, and the original TimeoutError still propagates.
    import functools

    import morning_dca as _morning_dca
    from scripts.file_lock import cross_process_lock

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)

    # Real lock semantics, just without the 30-second production wait: two
    # timeouts at the default would make this test a minute long.
    monkeypatch.setattr(
        _morning_dca,
        "cross_process_lock",
        functools.partial(cross_process_lock, timeout_seconds=0.05, poll_seconds=0.01),
    )

    # A separate open() is a separate file description, so flock genuinely
    # contends here even though the holder is this same process.
    with cross_process_lock(run_dir / "scheduled-slot.lock"):
        with pytest.raises(TimeoutError):
            ex.run(_now_live(), data, broker=_TopUpBroker())

    assert not (run_dir / "run_state.json").exists()
    assert not (run_dir / "submitted_orders.csv").exists()


def test_a_late_failure_never_erases_already_written_artifacts(tmp_path):
    """A failure AFTER artifacts were written (possibly after orders were placed)
    must not overwrite that record with a failure stub."""
    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_TopUpBroker())
    run_dir = Path(res["paths"]["run_state"]).parent
    original = (run_dir / "run_state.json").read_text(encoding="utf-8")

    ex._write_startup_failure_artifacts(_now_live(), RuntimeError("later boom"))

    assert (run_dir / "run_state.json").read_text(encoding="utf-8") == original


def test_placement_failures_are_recorded_as_a_trading_failure(tmp_path):
    """Every qualifying placement raised: a genuine failure, not a healthy
    no-order run (Codex review)."""

    class _FailingBroker(_TopUpBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise RuntimeError("broker rejected the order")

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_FailingBroker())

    artifacts = res["artifacts"]
    assert artifacts["broker_outcomes"]["NVDA"]["status"] == "failed"
    assert artifacts["run_classification_outcome"] == "failed_after_connector_native_recovery_attempt"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["error_class"] == "failed_after_connector_native_recovery_attempt"
    assert artifacts["order_submit_status"] == "not_submitted_error"


def test_candidates_blocked_before_the_spread_gate_are_not_no_ladder_candidates(tmp_path):
    """A cap-blocked candidate DID qualify on the ladder; it just never reached
    the spread lookup (Codex review)."""

    # The daily cap is checked BEFORE the spread lookup, so the candidate never
    # reaches the gate and carries no spread verdict.
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
        morning_daily_cap_usd=0.0,
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_TopUpBroker())

    artifacts = res["artifacts"]
    assert artifacts["broker_outcomes"]["NVDA"]["status"] == "cap_exceeded"
    assert "spread_status" not in artifacts["broker_outcomes"]["NVDA"]
    assert artifacts["run_classification_outcome"] == "completed_no_orders"
    assert artifacts["completion_reason"] == "blocked_before_spread_gate"
    assert artifacts["error_class"] is None
    assert artifacts["is_trading_failure"] is False


def test_calendar_blocked_live_run_carries_the_blocked_no_order_outcome(tmp_path):
    """This path writes state and summary directly and returns before the
    classification call, so it must stamp the outcome itself (Codex review)."""
    pf_root = _seed_preflight(tmp_path / "pf", ("NVDA",), trading_date=date(2026, 7, 4))
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    # 2026-07-04 is a Saturday: the calendar gate blocks before any evaluation.
    holiday_now = datetime.combine(date(2026, 7, 4), time(10, 30), tzinfo=EASTERN)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(holiday_now, data, broker=_TopUpBroker())

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["run_classification_outcome"] == "completed_no_orders"
    assert artifacts["completion_reason"] == "date_or_holiday_gate_blocked"
    assert artifacts["is_trading_failure"] is False
    assert artifacts["orders_submitted_count"] == 0

    run_dir = Path(res["paths"]["run_state"]).parent
    summary = json.loads((run_dir / "run_summary.json").read_bytes())
    assert summary["completion_reason"] == "date_or_holiday_gate_blocked"


def test_manual_review_block_is_not_recorded_as_a_missing_spread_block(tmp_path):
    """A manual run does not fail closed on an unproven spread, so a row can carry
    spread_status='spread_unavailable' and still be stopped by review. That is not
    a spread-caused no-order run (Codex review)."""

    class _ReviewBlockingBroker(_TopUpBroker):
        def review_market_buy(self, ticker, notional_usd) -> Tuple[bool, str]:
            return False, "review declined"

    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _ReviewBlockingBroker()
    data = _NoBidAskProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker, manual=True)

    artifacts = res["artifacts"]
    outcome = artifacts["broker_outcomes"]["NVDA"]
    assert outcome["status"] == "blocked"
    assert "review blocked" in outcome["skip_reason"]
    # The row still carries the unproven-spread verdict...
    assert outcome["spread_status"] == "spread_unavailable"
    # ...but the spread gate is not what blocked it, so the run is not a
    # missing-spread outcome.
    assert outcome.get("blocked_by") != "spread_gate"
    assert artifacts["error_class"] != "missing_spread_data"
    assert artifacts["candidates_blocked_missing_spread"] == 0


def test_preflight_missing_no_longer_blocks_and_still_evaluates(tmp_path):
    """Superseded 2026-08-02 (Geoff: "should not block any later runs"): a
    missing same-day preflight artifact used to end the run here as a
    blocked-before-placement outcome (Codex review, PR #77/#88), which this
    test used to verify carried the right outcome fields
    (run_classification_outcome/order_submit_status/is_trading_failure/etc via
    _apply_blocked_before_placement_fields). That branch and its trigger are
    both gone; the same field-stamping helper is still exercised by other
    genuinely-blocking paths (e.g. a missing broker adapter, account pinning
    failure -- see the many other run_classification_outcome ==
    'failed_scheduled_run_startup' assertions elsewhere in this file). This
    test now just confirms the missing-preflight case reaches a normal,
    non-blocked evaluation instead."""
    cfg = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "live"),
        # Empty preflight root: no same-day pre-flight artifact exists.
        preflight_run_state_root=str(tmp_path / "no_preflight"),
        prior_allocation_roots=(str(tmp_path / "empty_prior"),),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_TopUpBroker())

    artifacts = res["artifacts"]
    assert artifacts.get("status") != "blocked"
    assert artifacts["preflight_gate"]["preflight_found"] is False
    evaluated = {r["ticker"] for r in artifacts["signal_rows"]}
    assert evaluated == {"NVDA"}


def test_confirmation_required_survives_the_morning_no_order_classification(tmp_path):
    """Replacing it would make is_confirmation_blocked() return false and lose the
    fact that broker confirmation is pending (Codex review)."""
    from near_close_dca import OrderConfirmationRequiredError

    class _ConfirmationRequiredBroker(_TopUpBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise OrderConfirmationRequiredError("broker requires step-up confirmation")

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_ConfirmationRequiredBroker())

    artifacts = res["artifacts"]
    assert artifacts["broker_outcomes"]["NVDA"]["status"] == "not_submitted_confirmation_required"
    assert artifacts["order_submit_status"] == "not_submitted_confirmation_required"
    # The outcome fields are still recorded alongside it.
    assert artifacts["run_classification_outcome"] == "completed_no_orders"
    assert artifacts["orders_submitted_count"] == 0


def test_raising_placement_connector_retains_ambiguous_broker_outcome(tmp_path):
    """A placement timeout is retained without raw-persistence reclassification."""

    placement_attempts = []

    def _raise_after_possible_submission(**kwargs):
        placement_attempts.append(kwargs["symbol"])
        raise TimeoutError("broker response timed out")

    ex = _live_executor(tmp_path, ("NVDA", "AAPL"))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10, "AAPL": 0.10})
    broker = RobinhoodBroker(
        place_fn=_raise_after_possible_submission,
        enable_live_orders=True,
        **_LIVE_LOOKUP_STUBS,
    )

    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    assert set(placement_attempts) == {"NVDA", "AAPL"}
    for ticker in ("NVDA", "AAPL"):
        outcome = artifacts["broker_outcomes"][ticker]
        assert outcome["status"] == "potentially_submitted"
        assert "placement ambiguous" in outcome["skip_reason"]
        assert outcome["order_id"] is None
    assert "raw_persistence_error" not in artifacts
    assert artifacts.get("error_class") != "missing_or_invalid_raw_inputs"


def test_definitive_placement_rejection_continues_the_batch(tmp_path):
    """A terminal broker rejection for one ticker must not halt later candidates.

    Unlike a genuinely ambiguous placement failure (timeout, connection reset --
    covered by test_raising_placement_connector_retains_ambiguous_broker_outcome
    above), a response the broker's OWN placement call already reports as a
    PROVEN zero-fill rejection (a terminal state with an explicit zero
    cumulative_quantity) PROVES no live order exists for that ticker.
    RobinhoodBroker.place_market_buy raises the distinct
    FractionalPlacementRejectedError (public alias OrderPlacementRejectedError)
    for this proven case, and _process_candidate must catch it separately to
    `continue` rather than `break` the batch (Codex review, PR #688: "Continue
    after definitive broker rejections"; PR #859: "Preserve the terminal order
    ID from proven rejections").
    """
    from near_close_dca import OrderPlacementRejectedError

    def _place_fn(**kwargs):
        if kwargs["symbol"] == "AAPL":
            return {
                "data": {
                    "order": {
                        "id": "ORD-REJECTED",
                        "state": "rejected",
                        "cumulative_quantity": "0",
                    }
                }
            }
        return {"data": {"order": {"id": f"ORD-{kwargs['symbol']}", "state": "confirmed"}}}

    ex = _live_executor(tmp_path, ("AAPL", "NVDA"))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.10, "NVDA": 0.10})
    broker = RobinhoodBroker(place_fn=_place_fn, enable_live_orders=True, **_LIVE_LOOKUP_STUBS)

    # Sanity-check the adapter itself raises the specific type this test relies on.
    with pytest.raises(OrderPlacementRejectedError):
        broker.place_market_buy("AAPL", 1.0, "ref-sanity-check")

    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    aapl_outcome = artifacts["broker_outcomes"]["AAPL"]
    assert aapl_outcome["status"] == "failed"
    assert "broker rejected placement" in aapl_outcome["skip_reason"]
    assert aapl_outcome["order_id"] == "ORD-REJECTED"
    # The later, unrelated candidate must still have been attempted and
    # submitted -- the definitive rejection above must not have halted it.
    nvda_outcome = artifacts["broker_outcomes"]["NVDA"]
    assert nvda_outcome["status"] == "submitted"
    assert nvda_outcome["order_id"] == "ORD-NVDA"
    assert "raw_persistence_error" not in artifacts
    assert artifacts.get("error_class") != "missing_or_invalid_raw_inputs"


def test_terminal_partial_fill_is_booked_before_continuing_batch(tmp_path):
    """A cancelled fractional order can still have spent real buying power."""

    placement_attempts = []

    def _place_fn(**kwargs):
        placement_attempts.append(kwargs["symbol"])
        if kwargs["symbol"] == "AAPL":
            return {
                "data": {
                    "order": {
                        "id": "ORD-PARTIAL",
                        "state": "cancelled",
                        "cumulative_quantity": "0.06",
                        "average_price": "100.00",
                    }
                }
            }
        return {"data": {"order": {"id": "ORD-NVDA", "state": "confirmed"}}}

    ex = _live_executor(tmp_path, ("AAPL", "NVDA"))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.10, "NVDA": 0.10})
    broker = RobinhoodBroker(
        place_fn=_place_fn,
        buying_power_fn=lambda **_kwargs: {"buying_power": "15"},
        orders_fn=_no_orders_today,
        tradability_fn=_all_tradable,
        enable_live_orders=True,
    )

    res = ex.run(_now_live(), data, broker=broker)

    outcomes = res["artifacts"]["broker_outcomes"]
    assert placement_attempts == ["AAPL"]
    assert outcomes["AAPL"]["status"] == "submitted"
    assert outcomes["AAPL"]["order_id"] == "ORD-PARTIAL"
    assert outcomes["AAPL"]["submitted_amount_usd"] == 6.0
    assert outcomes["NVDA"]["status"] == "blocked"
    assert outcomes["NVDA"]["skip_reason"] == "insufficient buying power"
    assert res["artifacts"]["orders_submitted_count"] == 1
    assert ex.ledger.day_planned(date(2026, 7, 8)) == 6.0


def test_terminal_rejection_with_unreadable_quantity_halts_the_batch(tmp_path):
    """An unreconcilable terminal fill must halt the batch, never fall through.

    A cancelled/rejected order whose ``cumulative_quantity`` is missing (or
    otherwise unreadable) proves nothing about whether capital moved -- it is
    neither the "exactly zero, proven non-fill" case nor a reconcilable
    partial fill. The `except MarketBuyPlacementTerminalError` handler used to
    fall through the bottom of its `if filled_quantity == 0` check with no
    `return` in every other case, which inside an `except` block means
    control falls all the way past every sibling `except` clause to the
    NORMAL SUCCESS PATH below -- silently overwriting the "failed" outcome
    this handler had just recorded as "submitted" and moving on to place the
    next ticker as if the run were healthy. Reproduces exactly that: a
    cancelled AAPL order recorded as a $10 submission with NVDA placed right
    after it (Codex review, PR #1361 discussion_r3939066629: "Security: Halt
    when terminal fill evidence is unreadable").
    """

    placement_attempts = []

    def _place_fn(**kwargs):
        placement_attempts.append(kwargs["symbol"])
        if kwargs["symbol"] == "AAPL":
            return {
                "data": {
                    "order": {
                        "id": "ORD-CANCELLED",
                        # "cancelled" (unlike "rejected"/"failed"/"voided")
                        # is not a DECLINED state: the order EXISTED before
                        # being killed, so it needs an EXPLICIT zero fill to
                        # be proven a clean non-fill
                        # (terminal_broker_row_proves_no_order). No
                        # cumulative_quantity at all is unreadable, not a
                        # proven zero.
                        "state": "cancelled",
                    }
                }
            }
        return {"data": {"order": {"id": "ORD-NVDA", "state": "confirmed"}}}

    ex = _live_executor(tmp_path, ("AAPL", "NVDA"))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.10, "NVDA": 0.10})
    broker = RobinhoodBroker(
        place_fn=_place_fn,
        buying_power_fn=lambda **_kwargs: {"buying_power": "1000"},
        orders_fn=_no_orders_today,
        tradability_fn=_all_tradable,
        enable_live_orders=True,
    )

    res = ex.run(_now_live(), data, broker=broker)

    outcomes = res["artifacts"]["broker_outcomes"]
    # NVDA must never be attempted: the batch halts on AAPL's unreconcilable
    # terminal rejection instead of falling through to place it.
    assert placement_attempts == ["AAPL"]
    assert outcomes["AAPL"]["status"] == "failed"
    assert outcomes["AAPL"].get("ambiguous_placement_exception") is True
    assert "NVDA" not in outcomes or outcomes["NVDA"]["status"] != "submitted"


def test_terminal_rejection_with_positive_quantity_and_no_usable_price_halts_the_batch(tmp_path):
    """A positive fill quantity with no usable average_price is unreconcilable.

    Same fall-through bug as the unreadable-quantity case: a positive
    cumulative_quantity without a usable average_price cannot be booked as a
    reconcilable fill (no price to compute notional from), and is not the
    proven-zero case either, so it must halt the batch rather than silently
    becoming "submitted" (Codex review, PR #1361 discussion_r3939066629).
    """

    placement_attempts = []

    def _place_fn(**kwargs):
        placement_attempts.append(kwargs["symbol"])
        if kwargs["symbol"] == "AAPL":
            return {
                "data": {
                    "order": {
                        "id": "ORD-REJECTED",
                        "state": "rejected",
                        "cumulative_quantity": "0.05",
                        "average_price": None,
                    }
                }
            }
        return {"data": {"order": {"id": "ORD-NVDA", "state": "confirmed"}}}

    ex = _live_executor(tmp_path, ("AAPL", "NVDA"))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.10, "NVDA": 0.10})
    broker = RobinhoodBroker(
        place_fn=_place_fn,
        buying_power_fn=lambda **_kwargs: {"buying_power": "1000"},
        orders_fn=_no_orders_today,
        tradability_fn=_all_tradable,
        enable_live_orders=True,
    )

    res = ex.run(_now_live(), data, broker=broker)

    outcomes = res["artifacts"]["broker_outcomes"]
    assert placement_attempts == ["AAPL"]
    assert outcomes["AAPL"]["status"] == "failed"
    assert outcomes["AAPL"].get("ambiguous_placement_exception") is True
    assert "NVDA" not in outcomes or outcomes["NVDA"]["status"] != "submitted"


def test_terminal_partial_fill_response_is_archived_for_raw_replay(tmp_path):
    """A terminal partial fill's own broker payload must reach the raw
    placement-response archive, even though the broker RAISED instead of
    returning normally.

    The normal (non-exception) placement path archives its response via
    RobinhoodBroker.place_market_buy's own append to
    ``self._raw_placement_responses`` (read back for raw/orders_after_place.json),
    but that append is never reached when place_market_buy raises
    MarketBuyPlacementTerminalError instead -- so a terminal partial fill
    recorded as "submitted" here would otherwise be missing from the raw
    archive even though a per-ticker place_*.json file exists for it (Codex
    review, PR #688 discussion_r3925372800: "Archive terminal partial-fill
    responses").
    """

    def _place_fn(**kwargs):
        if kwargs["symbol"] == "AAPL":
            return {
                "data": {
                    "order": {
                        "id": "ORD-PARTIAL",
                        "state": "cancelled",
                        "cumulative_quantity": "0.06",
                        "average_price": "100.00",
                    }
                }
            }
        return {"data": {"order": {"id": f"ORD-{kwargs['symbol']}", "state": "confirmed"}}}

    ex = _live_executor(tmp_path, ("AAPL",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.10})
    broker = RobinhoodBroker(
        place_fn=_place_fn,
        buying_power_fn=lambda **_kwargs: {"buying_power": "15"},
        orders_fn=_no_orders_today,
        tradability_fn=_all_tradable,
        enable_live_orders=True,
    )

    res = ex.run(_now_live(), data, broker=broker)

    assert res["artifacts"]["broker_outcomes"]["AAPL"]["status"] == "submitted"
    archived = broker.get_raw_placement_responses()
    matching = [row for row in archived if row.get("ticker") == "AAPL"]
    assert len(matching) == 1, "the terminal partial-fill response must reach the raw archive"
    assert matching[0]["order_id"] == "ORD-PARTIAL"
    assert matching[0]["state"] == "cancelled"
    assert matching[0]["cumulative_quantity"] == "0.06"


def test_scheduled_live_run_drains_placement_responses_onto_the_callers_broker(tmp_path):
    """The caller's own broker object must retain fill evidence after run().

    A scheduled live run wraps the caller-supplied broker in
    ``_PersistingBrokerAdapter``, which -- to avoid leaking THIS run's orders
    onto the caller's instance for a later, unrelated run -- works against a
    DETACHED copy with its own, empty ``_raw_placement_responses`` list (see
    ``_PersistingBrokerAdapter._wrap_raw_connectors``'s own comment). Without
    draining that copy's buffer back onto the caller's broker before
    ``_run_impl`` returns, the caller -- which has no reference to the
    short-lived wrapper or its detached copy once ``run()`` returns -- sees
    only its own, still-empty buffer and loses this run's fill evidence
    entirely, even though a per-ticker outcome and CSV row exist for it
    (Codex review, PR #688 discussion_r3917126964: "Retain placement
    responses after detaching broker state").
    """

    def _place_fn(**kwargs):
        return {"data": {"order": {"id": f"ORD-{kwargs['symbol']}", "state": "confirmed"}}}

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    broker = RobinhoodBroker(place_fn=_place_fn, enable_live_orders=True, **_LIVE_LOOKUP_STUBS)

    # Sanity check: nothing recorded on the caller's own instance before the run.
    assert broker.get_raw_placement_responses() == []

    res = ex.run(_now_live(), data, broker=broker)

    assert res["artifacts"]["broker_outcomes"]["NVDA"]["status"] == "submitted"
    # The caller's OWN broker object -- not some internal wrapper it never
    # sees -- must carry the order's raw response so it can still build its
    # own raw/orders_after_place.json after the call returns.
    drained = broker.get_raw_placement_responses()
    assert len(drained) == 1
    assert drained[0]["ticker"] == "NVDA"
    assert drained[0]["order_id"] == "ORD-NVDA"


def test_generic_placement_exception_halt_preserves_credit_for_unreached_candidate(tmp_path):
    """A candidate the batch never reaches must not lose its computed credit.

    When an EARLIER ticker's placement raises an unexpected exception type
    (not OrderPlacementAmbiguousError),
    _place_broker_orders `break`s the batch -- so a LATER candidate whose
    already-computed cumulative_credit_decline_usd crossed the order
    threshold (incremental_order_amount > 0) never gets an `outcomes` entry
    at all. The post-loop credit-preservation pass must still durably record
    that decline as pending rather than silently dropping it because there is
    no outcome to check (Codex review, PR #688: "Preserve credits for
    candidates skipped by the batch halt").
    """
    from dca_cumulative import CumulativeCreditLedger

    class _UnexpectedPlacementExceptionBroker(_TopUpBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            if ticker == "AAPL":
                raise RuntimeError("unexpected broker connector crash")
            return super().place_market_buy(ticker, notional_usd, ref_id)

    # TOPUP: prior same-day fill of $4.50 at reference $99.0, base $3.00.
    # 3% drop to $97.0 -> ladder_incremental == 0 but the $2.00 price-step
    # floor routes through the cumulative-credit policy, crossing the order
    # threshold immediately (cumulative_credit_decline_usd == 2.0,
    # incremental_order_amount == 2.0) -- see
    # test_morning_top_up_guard_buys_incremental_when_prior_fill_exists for
    # the identical math worked out in isolation.
    prior_root = tmp_path / "prior"
    _write_prior_run_state(
        prior_root, "TOPUP", amount_usd=4.50, decision_price=99.0, selected_base_amount_usd=3.0
    )
    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL", "TOPUP"))
    cfg = MorningConfig(
        tickers=("AAPL", "TOPUP"),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
        # A 10% drop's 5x graded multiplier * $3 base = $15, over the default
        # $10 per-order ceiling -- that gate runs BEFORE the broker call, so
        # a raised ceiling is needed here for AAPL's placement (not just its
        # ladder eligibility) to actually be attempted.
        max_per_order_usd=20.0,
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path / "credits")
    broker = _UnexpectedPlacementExceptionBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.10, "TOPUP": 0.03})

    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    # AAPL's unexpected placement exception halted the batch before TOPUP was reached --
    # TOPUP gets only a "never attempted" skip record, never a placement
    # outcome of its own.
    assert artifacts["broker_outcomes"]["AAPL"]["status"] == "failed"
    assert "ambiguous placement exception" in artifacts["broker_outcomes"]["AAPL"]["skip_reason"]
    assert artifacts["broker_outcomes"]["TOPUP"].get("placement_breaker_halted") is True
    assert artifacts["broker_outcomes"]["TOPUP"].get("status") in ("skipped", "blocked")
    # TOPUP was never placed, but its already-computed credit must still be
    # durably preserved as pending for a later slot / the 3:30 PM session to
    # pick up -- not silently lost because the batch halted before it had an
    # outcomes entry at all.
    assert ex.credit_ledger.pending_usd("TOPUP", "2026-07-08") == 2.0


def test_morning_provider_errors_are_not_a_clean_ladder_no_op(tmp_path):
    """evaluate_signal() itself catches a raising provider and emits NO_DATA --
    but for a SCHEDULED live run, the same exception was already recorded onto
    raw_recorder.error by the wrapping _PersistingMarketDataProvider before
    evaluate_signal ever sees it, and run() checks raw_recorder.error right
    after evaluate() completes, before any placement. So the run never even
    reaches the NO_DATA/classify_no_order_run() path this docstring used to
    describe -- it fails closed as a startup failure instead of a healthy
    empty ladder no-op (CLAUDE.md "Fail closed, always")."""

    class _RaisingProvider(DecliningBarsProvider):
        def get_previous_close_bar(self, ticker, as_of):
            raise RuntimeError("provider is down")

    ex = _live_executor(tmp_path, ("NVDA",))
    data = _RaisingProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_TopUpBroker())

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    assert artifacts["run_classification_outcome"] == "failed_scheduled_run_startup"
    assert artifacts["error_class"] == "missing_or_invalid_raw_inputs"
    assert "provider is down" in artifacts["raw_persistence_error"]
    assert artifacts["is_trading_failure"] is True


def test_a_raising_broker_check_is_not_a_healthy_no_order_run(tmp_path):
    """A caught pre-placement connector error is recorded as a non-deliberate
    block; broker availability prevented execution (Codex review)."""

    class _RaisingCheckBroker(_TopUpBroker):
        def is_tradable_for_fractional_buy(self, ticker):
            raise RuntimeError("tradability lookup exploded")

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_RaisingCheckBroker())

    artifacts = res["artifacts"]
    outcome = artifacts["broker_outcomes"]["NVDA"]
    assert outcome["status"] == "blocked"
    assert not outcome.get("deliberate_skip")
    assert artifacts["error_class"] == "degraded_market_data"


def test_a_deliberate_safety_skip_is_still_a_clean_no_order_run(tmp_path):
    """The counterpart: a deliberate gate is not an error and must not degrade
    the run's classification."""

    class _NotTradableBroker(_TopUpBroker):
        def is_tradable_for_fractional_buy(self, ticker):
            return False

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_NotTradableBroker())

    artifacts = res["artifacts"]
    outcome = artifacts["broker_outcomes"]["NVDA"]
    assert outcome["status"] == "blocked" and outcome["deliberate_skip"] is True
    assert artifacts["run_classification_outcome"] == "completed_no_orders"
    assert artifacts["error_class"] is None
    assert artifacts["is_trading_failure"] is False


def test_a_late_failure_never_erases_an_order_already_placed(tmp_path):
    """The handler wraps the WHOLE run, so an order can be accepted and a later
    step can then raise before run_state.json exists. Asserting zero orders there
    would hide a real fill from the same-day top-up guard and let a later run
    re-allocate that capital (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    def _explode(*_args, **_kwargs):
        raise RuntimeError("blew up after placement, before any artifact")

    # build_artifacts runs AFTER _place_broker_orders.
    ex.build_artifacts = _explode

    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=broker)

    assert "NVDA" in broker.placed, "fixture precondition: the order was placed"
    run_dir = ex.run_state_dir(_now_live())
    run_state = json.loads((run_dir / "run_state.json").read_bytes())

    assert run_state["orders_submitted_count"] == 1
    assert run_state["broker_outcomes"]["NVDA"]["status"] == "submitted"
    assert run_state["broker_outcomes"]["NVDA"]["order_id"]
    # A prior-allocation reader walks buy_candidates, so the row must be there.
    [candidate] = run_state["buy_candidates"]
    assert candidate["ticker"] == "NVDA"
    assert candidate["status"] == "submitted"
    assert candidate["planned_amount_usd"] == broker.placed["NVDA"]
    # Flagged as a floor on what was placed, never proof nothing else was.
    assert run_state["placement_evidence_incomplete"] is True
    # And the order appears in submitted_orders.csv.
    with (run_dir / "submitted_orders.csv").open(newline="", encoding="utf-8") as handle:
        order_rows = list(csv.DictReader(handle))
    assert [row["ticker"] for row in order_rows] == ["NVDA"]


def test_a_partial_artifact_set_is_completed_after_a_mid_write_failure(tmp_path):
    """write_artifacts can write run_state.json and then raise on a later file;
    the existing state is preserved but the companions are still written."""
    from scripts.scheduled_artifacts import validate_run_artifacts

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    # write_artifacts serializes the full artifacts dict first, so a genuinely
    # partial directory holds a complete run_state and nothing else.
    partial = json.dumps(
        {
            "run_id": "partial",
            "trading_day": "2026-07-08",
            "run_classification": {"run_origin": "scheduled", "manual_run": False,
                                   "stream": "morning"},
        },
        sort_keys=True,
    )
    (run_dir / "run_state.json").write_text(partial, encoding="utf-8")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("raised after run_state.json landed")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    # The already-written state is preserved byte for byte...
    assert (run_dir / "run_state.json").read_text(encoding="utf-8") == partial
    # ...and every missing companion is now present.
    for name in ("run_summary.json", "morning_signals.csv", "submitted_orders.csv"):
        assert (run_dir / name).exists(), f"{name} still missing"
    assert validate_run_artifacts(run_dir, require_summary=True)


def test_startup_failure_refreshes_the_same_day_pointer(tmp_path):
    """Otherwise a pointer-based watchdog keeps reporting that today's slot never
    fired, even though the failure state exists (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})

    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    pointer = Path(ex.state_root) / ex.latest_pointer_name
    assert pointer.exists(), "the stream pointer was never refreshed"
    payload = json.loads(pointer.read_bytes())
    assert payload["valid_scheduled_evidence"] is False
    assert payload["trading_day"] == "2026-07-08"


def test_stale_in_flight_outcomes_never_leak_into_a_later_run(tmp_path):
    """The executor is reused across calls, so a startup failure on a later run
    must not copy the previous run's placements into the new day's artifact and
    record them as current-day allocations (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    # First run places a real order and completes.
    ex.run(_now_live(), data, broker=broker)
    assert "NVDA" in broker.placed

    # A LATER day's run on the same executor raises during evaluation.
    later = datetime.combine(date(2026, 7, 9), time(10, 30), tzinfo=EASTERN)
    _seed_preflight(tmp_path / "pf", ("NVDA",), trading_date=later.date())

    def _explode(*_args, **_kwargs):
        raise RuntimeError("blew up during evaluation")

    ex.evaluate = _explode
    with pytest.raises(RuntimeError):
        ex.run(later, data, broker=broker)

    later_state = json.loads(
        (ex.run_state_dir(later) / "run_state.json").read_text(encoding="utf-8")
    )
    assert later_state["orders_submitted_count"] == 0
    assert later_state["broker_outcomes"] == {}
    assert later_state["buy_candidates"] == []


def test_a_failure_after_submission_does_not_contradict_the_order_it_recorded(tmp_path):
    """orders_submitted_count and submitted_orders.csv say an order was placed, so
    the aggregate status must not say no submission occurred (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    def _explode(*_args, **_kwargs):
        raise RuntimeError("blew up after placement")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=broker)

    run_state = json.loads(
        (ex.run_state_dir(_now_live()) / "run_state.json").read_text(encoding="utf-8")
    )
    assert run_state["orders_submitted_count"] == 1
    assert run_state["order_submit_status"] == "submitted"
    assert run_state["run_classification_outcome"] == "failed_after_partial_submission"
    # Still a failure, just an honest one.
    assert run_state["is_trading_failure"] is True
    # The error class matches the outcome: a late post-trade failure is not
    # startup trouble.
    assert run_state["error_class"] == "scheduled_run_failed_after_submission"


def test_a_completed_run_keeps_a_valid_pointer_when_only_the_pointer_write_fails(tmp_path):
    """validate_pointer rejects any non-'valid' status, so forcing pending here
    would make a successfully completed run read as unavailable (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})

    calls = {"n": 0}
    real_write = ex._write_same_day_pointers

    def _fail_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pointer write failed")
        return real_write(*args, **kwargs)

    ex._write_same_day_pointers = _fail_first
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    pointer = Path(ex.state_root) / ex.latest_pointer_name
    payload = json.loads(pointer.read_bytes())
    # The run itself completed, so its pointer stays valid evidence.
    assert payload["pointer_status"] == "valid"
    assert payload["valid_scheduled_evidence"] is True


def test_a_manual_placement_failure_is_not_a_failed_scheduled_recovery(tmp_path):
    """The manual path never runs the connector-native recovery policy (Codex
    review)."""

    class _FailingBroker(_TopUpBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise RuntimeError("broker rejected the order")

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_FailingBroker(), manual=True)

    artifacts = res["artifacts"]
    assert artifacts["run_classification_outcome"] == "failed_placement"
    assert artifacts["error_class"] == "failed_placement"
    assert artifacts["is_trading_failure"] is True


def test_a_synthesized_failure_state_is_never_valid_scheduled_evidence(tmp_path):
    """select_same_day_run_state prefers the date-specific state over the pending
    pointer, so the failure artifact needs a marker the derivation honors, not
    just a false flag inside the classification (Codex review)."""
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})

    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    run_state = json.loads(
        (ex.run_state_dir(_now_live()) / "run_state.json").read_text(encoding="utf-8")
    )
    assert run_state["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(run_state) is False


def test_a_healthy_scheduled_run_is_still_valid_evidence(tmp_path):
    """The denial marker must not disturb the normal path."""
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_TopUpBroker())

    assert "scheduled_evidence_denied" not in res["artifacts"]
    assert derive_valid_scheduled_evidence(res["artifacts"]) is True


def test_a_partially_submitted_batch_records_the_failed_placement(tmp_path):
    """One order landed and another ticker raised: an ordinary success record
    would hide the failed order (Codex review)."""

    class _PartlyFailingBroker(_TopUpBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            if ticker == "NVDA":
                raise RuntimeError("connector timed out after accepting NVDA")
            return super().place_market_buy(ticker, notional_usd, ref_id)

    ex = _live_executor(tmp_path, ("NVDA", "AMD"))
    broker = _PartlyFailingBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10, "AMD": 0.10})
    res = ex.run(_now_live(), data, broker=broker)

    artifacts = res["artifacts"]
    assert "AMD" in broker.placed and "NVDA" not in broker.placed
    # The submitted count and status survive...
    assert artifacts["orders_submitted_count"] == 1
    # ...and the failure is recorded rather than silently dropped.
    assert artifacts["orders_failed_count"] == 1
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["error_class"] == "partial_placement_failure"


def test_a_stale_companion_blocks_a_valid_pointer_on_the_preserved_path(tmp_path):
    """In a reused directory the summary can belong to an earlier attempt, so the
    full set is revalidated before a valid pointer is published (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({"run_id": "fresh", "trading_day": "2026-07-08",
                    "run_classification": {"run_origin": "scheduled", "manual_run": False,
                                           "stream": "morning"}}),
        encoding="utf-8",
    )
    # A summary left from an earlier attempt whose classification does not mirror.
    (run_dir / "run_summary.json").write_text(
        json.dumps({"run_id": "stale", "run_classification": {"run_origin": "scheduled",
                                                              "manual_run": False,
                                                              "stream": "afternoon"}}),
        encoding="utf-8",
    )

    def _explode(*_args, **_kwargs):
        raise RuntimeError("raised after the state landed")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    payload = json.loads(
        (Path(ex.state_root) / ex.latest_pointer_name).read_text(encoding="utf-8")
    )
    # The mismatched summary must not be advertised as valid scheduled evidence.
    assert payload["pointer_status"] == "pending"
    assert payload["valid_scheduled_evidence"] is False


def test_a_broken_companion_set_denies_evidence_in_the_state_itself(tmp_path):
    """select_same_day_run_state prefers the state file over the pointer, so
    downgrading only the pointer still hands the run to consumers as valid
    evidence (Codex review)."""
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps({"run_id": "fresh", "trading_day": "2026-07-08",
                    "run_classification": {"run_origin": "scheduled", "manual_run": False,
                                           "stream": "morning"}}),
        encoding="utf-8",
    )
    (run_dir / "run_summary.json").write_text(
        json.dumps({"run_id": "stale", "run_classification": {"run_origin": "scheduled",
                                                              "manual_run": False,
                                                              "stream": "afternoon"}}),
        encoding="utf-8",
    )

    def _explode(*_args, **_kwargs):
        raise RuntimeError("raised after the state landed")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    persisted = json.loads((run_dir / "run_state.json").read_bytes())
    assert persisted["artifact_contract_failure"]["failure"] == "artifact_contract_failure"
    assert persisted["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(persisted) is False


def test_a_successful_batch_records_the_submitted_aggregate_status(tmp_path):
    """build_artifacts never sets this field, so a fully successful batch used to
    write a null order_submit_status in both artifacts (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    broker = _TopUpBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=broker)

    assert "NVDA" in broker.placed
    assert res["artifacts"]["order_submit_status"] == "submitted"
    assert res["artifacts"]["orders_submitted_count"] == 1
    # And it reaches the summary a status-based consumer reads.
    run_dir = Path(res["paths"]["run_state"]).parent
    summary = json.loads((run_dir / "run_summary.json").read_bytes())
    assert summary["order_submit_status"] == "submitted"


def test_a_retry_that_raises_merges_its_order_into_the_preserved_state(tmp_path):
    """The preserved run_state can belong to an EARLIER invocation. Discarding
    this retry's placement would hide an accepted order from the same-day guards
    and let a later run buy it again (Codex review)."""
    import morning_dca as _m

    earlier = {
        "run_id": "earlier",
        "trading_day": "2026-07-08",
        "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "o-earlier"}},
        "buy_candidates": [{"ticker": "AAA", "status": "submitted", "order_id": "o-earlier"}],
        "orders_submitted_count": 1,
    }
    placed = {"BBB": {"status": "submitted", "order_id": "o-retry",
                      "submitted_amount_usd": 2.0}}
    rows = [{"ticker": "BBB", "status": "submitted", "order_id": "o-retry",
             "planned_amount_usd": 2.0}]

    merged = _m._merge_placements_into_state(earlier, placed, rows)

    assert merged["run_id"] == "earlier", "the earlier state's identity is preserved"
    assert set(merged["broker_outcomes"]) == {"AAA", "BBB"}
    assert {r["order_id"] for r in merged["buy_candidates"]} == {"o-earlier", "o-retry"}
    assert merged["orders_submitted_count"] == 2
    assert merged["placement_evidence_incomplete"] is True


def test_a_retry_merge_preserves_a_new_order_over_a_stale_ticker_only_block(tmp_path):
    # Codex review ("Preserve same-ticker placements by order identity"): the
    # preserved state can already hold a blocked/failed outcome for this
    # exact ticker from an EARLIER attempt at this same slot -- no order
    # ever reached the broker for it. The old `ticker not in outcomes` check
    # treated that stale, order-less entry as already covering the ticker
    # and silently dropped THIS attempt's real submitted outcome, leaving
    # broker_outcomes saying no order exists for a ticker that was actually
    # just bought.
    import morning_dca as _m

    earlier = {
        "run_id": "earlier",
        "trading_day": "2026-07-08",
        "broker_outcomes": {"AAA": {"status": "blocked", "skip_reason": "spread_not_ok"}},
        "buy_candidates": [{"ticker": "AAA", "status": "blocked"}],
        "orders_submitted_count": 0,
    }
    placed = {"AAA": {"status": "submitted", "order_id": "o-retry", "submitted_amount_usd": 2.0}}
    rows = [{"ticker": "AAA", "status": "submitted", "order_id": "o-retry", "planned_amount_usd": 2.0}]

    merged = _m._merge_placements_into_state(earlier, placed, rows)

    assert merged["broker_outcomes"]["AAA"]["status"] == "submitted"
    assert merged["broker_outcomes"]["AAA"]["order_id"] == "o-retry"


def test_a_placement_failure_outranks_a_confirmation_required_row(tmp_path):
    """A batch where another order RAISED is not an ordinary confirmation hold
    (Codex review)."""
    import morning_dca as _m

    rows = [
        {"status": "not_submitted_confirmation_required"},
        {"status": "failed"},
    ]
    assert _m._concrete_order_submit_status(rows) is None
    # Without the failure it is still a confirmation block.
    assert (
        _m._concrete_order_submit_status([{"status": "not_submitted_confirmation_required"}])
        == "not_submitted_confirmation_required"
    )


def test_a_stale_order_csv_does_not_satisfy_companion_validation(tmp_path):
    """validate_run_artifacts checks only the order CSV's header, so a stale file
    omitting the state's orders would otherwise be published as canonical
    (Codex review)."""
    import morning_dca as _m
    from scripts.scheduled_artifacts import write_scheduled_run_artifacts

    run_dir = tmp_path / "slot"
    state = {
        "run_id": "r1",
        "trading_day": "2026-07-08",
        "run_classification": {"run_origin": "scheduled", "manual_run": False,
                               "stream": "morning"},
        "broker_outcomes": {"AAA": {"status": "submitted", "order_id": "o-new",
                                    "submitted_amount_usd": 2.0}},
        "buy_candidates": [],
    }
    write_scheduled_run_artifacts(run_dir, state)
    # Replace the order CSV with a header-only file from an "earlier attempt".
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8"
    )

    assert _m.MorningDCAExecutor._companions_match_state(run_dir, state) is False


def test_dry_run_simulations_never_reach_the_morning_order_csv(tmp_path):
    """DryRunBroker assigns synthetic DRY-RUN-* ids and submitted_order_rows
    includes any outcome with an id, so without a filter a dry-run report writes
    simulations into submitted_orders.csv and the drift detector counts them as
    genuine submissions (Codex review)."""

    class _DryRunBroker(_TopUpBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            return f"DRY-RUN-{ticker}"

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_DryRunBroker())

    outcome = res["artifacts"]["broker_outcomes"]["NVDA"]
    assert outcome["status"] == "dry_run" and outcome["order_id"].startswith("DRY-RUN")

    run_dir = Path(res["paths"]["run_state"]).parent
    with (run_dir / "submitted_orders.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [], "a simulated order was written as a broker submission"


def test_a_merged_morning_retry_is_stamped_as_a_failure(tmp_path):
    """Leaving the earlier attempt's classification would republish a prior
    successful state as valid though the retry failed (Codex review)."""
    import morning_dca as _m

    earlier = {
        "run_id": "earlier",
        "status": "completed",
        "run_classification": {"run_origin": "scheduled", "manual_run": False,
                               "stream": "morning", "valid_scheduled_evidence": True},
        "run_classification_outcome": "completed_no_orders",
        "order_submit_status": "no_orders_submitted",
        "is_trading_failure": False,
        "broker_outcomes": {},
        "buy_candidates": [],
    }
    placed = {"NVDA": {"status": "submitted", "order_id": "o-retry",
                       "submitted_amount_usd": 2.0}}
    rows = [{"ticker": "NVDA", "status": "submitted", "order_id": "o-retry"}]

    merged = _m._merge_placements_into_state(earlier, placed, rows, error_detail="RuntimeError: x")

    assert merged["status"] == "failed"
    assert merged["run_classification_outcome"] == "failed_after_partial_submission"
    assert merged["order_submit_status"] == "submitted"
    assert merged["is_trading_failure"] is True
    assert merged["scheduled_evidence_denied"] is True
    assert merged["run_classification"]["valid_scheduled_evidence"] is False


def test_a_merge_that_adds_nothing_leaves_the_state_untouched(tmp_path):
    """A run that wrote its own artifacts and then had a later step raise has
    nothing to add and nothing to re-label."""
    import morning_dca as _m

    state = {
        "run_id": "done",
        "status": "completed",
        "broker_outcomes": {"NVDA": {"status": "submitted", "order_id": "o-1"}},
        "buy_candidates": [{"ticker": "NVDA", "status": "submitted", "order_id": "o-1"}],
    }
    placed = {"NVDA": {"status": "submitted", "order_id": "o-1"}}

    merged = _m._merge_placements_into_state(state, placed, [], error_detail="x")

    assert merged is state
    assert merged["status"] == "completed"


def test_a_merged_retry_that_placed_nothing_is_not_a_post_submission_failure():
    """Only blocked/failed outcomes to merge: no order landed, so the label must
    not claim one did (Codex review)."""
    from scripts.run_classification import stamp_failure_classification

    stamped = stamp_failure_classification({}, submitted=0, error_detail="RuntimeError: x")

    assert stamped["run_classification_outcome"] == "failed_scheduled_run_startup"
    assert stamped["order_submit_status"] == "not_submitted_error"
    assert stamped["error_class"] == "scheduled_run_startup_failure"
    assert stamped["is_trading_failure"] is True


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


def test_a_synthesized_morning_failure_state_carries_the_schema_required_fields(tmp_path):
    """schemas/run_state.schema.json requires broker, budget, and counts on every
    run_state. validate_run_artifacts does not run schema validation, so omitting
    them let this handler advertise a complete failure artifact set whose
    canonical state schema-based reporting then rejects (Codex review)."""
    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})

    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom before any writer")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    run_dir = ex.run_state_dir(_now_live())
    run_state = json.loads((run_dir / "run_state.json").read_bytes())

    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "schemas" / "run_state.schema.json").read_text(
            encoding="utf-8"
        )
    )
    for field in schema["required"]:
        assert field in run_state, field
    # Real values from the executor's configuration, not placeholders.
    assert run_state["broker"] == "robinhood"
    # budget_tier/daily_cap_usd/weekly_cap_usd moved from an unconditional
    # `required` on the budget object into the schema's `else` branch (Codex
    # review, PR #232: "Define a schema-valid budget for pre-budget exits"),
    # so the seven-slot roster can write budget: {"budget_evaluated": false}
    # on a fire that exits before Gate 6 ever selects a tier. morning_dca.py
    # does not use that marker at all -- it always falls into the `else`
    # branch, which is exactly what these fields being present here proves.
    for field in schema["properties"]["budget"]["else"]["required"]:
        assert field in run_state["budget"], field
    assert run_state["counts"]["submitted"] == 0
    # Not run through jsonschema: run_state.schema.json $refs
    # run_classification.schema.json and needs a resolver this assertion does not
    # need. The required-field check IS the defect.


def test_a_synthesized_morning_failure_replaces_stale_companions(tmp_path):
    """A synthesized state describes THIS failure, so every companion left in a
    reused directory belongs to an earlier attempt. Preserving them on existence
    alone advertised stale or zero-byte files as this run's record, and because
    the state was synthesized the companion check was skipped too (Codex
    review)."""
    from scripts.scheduled_artifacts import validate_run_artifacts

    ex = _live_executor(tmp_path, ("NVDA",))
    run_dir = ex.run_state_dir(_now_live())
    run_dir.mkdir(parents=True, exist_ok=True)
    # A reused directory holding an earlier attempt's leftovers and no usable
    # run_state.json.
    (run_dir / "morning_signals.csv").write_text("", encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text("stale,garbage\n", encoding="utf-8")
    (run_dir / "run_summary.json").write_text(
        json.dumps({"run_classification": None, "stale": True}), encoding="utf-8"
    )

    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.0})

    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom before any writer")

    ex.build_artifacts = _explode
    with pytest.raises(RuntimeError):
        ex.run(_now_live(), data, broker=_TopUpBroker())

    assert (run_dir / "morning_signals.csv").read_text(encoding="utf-8").strip() != ""
    orders = (run_dir / "submitted_orders.csv").read_text(encoding="utf-8")
    assert "stale" not in orders
    summary = json.loads((run_dir / "run_summary.json").read_bytes())
    assert "stale" not in summary
    assert summary["run_classification"] is not None
    assert validate_run_artifacts(run_dir, require_summary=True)


def test_a_connector_failure_beside_a_submission_is_not_a_clean_run(tmp_path):
    """A pre-placement broker check that RAISES is recorded as a non-deliberate
    block, not 'failed'. The zero-order path already counted these as errors; the
    submitted path did not, so a batch that placed one order and lost another
    ticker to a connector failure persisted as an ordinary success (Codex
    review)."""
    from morning_dca import _apply_no_order_classification

    artifacts = {
        "broker_outcomes": {
            "AAA": {"status": "submitted", "order_id": "o-1"},
            # Raised inside a pre-placement lookup: blocked WITHOUT
            # deliberate_skip.
            "BBB": {"status": "blocked", "skip_reason": "spread quote failed: boom"},
        },
        "buy_candidates": [],
        "signal_rows": [],
    }

    _apply_no_order_classification(artifacts)

    assert artifacts["orders_submitted_count"] == 1
    assert artifacts["order_submit_status"] == "submitted"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["error_class"] == "degraded_market_data"


def test_a_breaker_halt_beside_a_submission_is_not_a_clean_run(tmp_path):
    """A tripped placement breaker is a trading failure whatever the row
    statuses say -- and its own rows say nothing. The unattempted tail is a
    deliberate skip, and the failures that TRIP the breaker can be
    confirmation-required holds, whose status is neither 'failed' nor a
    non-deliberate 'blocked'. So a batch that submitted one order and then hit
    three confirmation-required holds counted zero of each and persisted as an
    ordinary submitted run, with no is_trading_failure and no failure outcome,
    while the breaker had halted it and left candidates unattempted (Codex
    review, PR #786: "Classify placement-breaker halts as trading failures")."""
    from morning_dca import _apply_no_order_classification

    artifacts = {
        "broker_outcomes": {
            "AAA": {"status": "submitted", "order_id": "o-1"},
            # The three holds that tripped the breaker: real broker calls that
            # produced no order, and not 'failed'.
            "BBB": {"status": "not_submitted_confirmation_required", "order_id": None},
            "CCC": {"status": "not_submitted_confirmation_required", "order_id": None},
            "DDD": {"status": "not_submitted_confirmation_required", "order_id": None},
            # The tail the breaker never reached.
            "EEE": {
                "status": "skipped",
                "order_id": None,
                "deliberate_skip": True,
                "placement_breaker_halted": True,
                "skip_reason": "not attempted: batch halted after 3 consecutive placement failures",
            },
        },
        "buy_candidates": [],
        "signal_rows": [],
    }

    _apply_no_order_classification(artifacts)

    assert artifacts["orders_submitted_count"] == 1
    assert artifacts["order_submit_status"] == "submitted"
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    # A PLACEMENT failure, not degraded market data: the batch stopped because
    # the broker leg was unhealthy, and filing it under the market-data class
    # purely because its own rows contribute nothing to `failed` would point
    # every downstream alert at the wrong subsystem.
    assert artifacts["error_class"] == "partial_placement_failure"


def test_a_breaker_trip_on_the_final_candidate_still_marks_the_run(tmp_path):
    """The zero-length-tail case. `_record_breaker_skipped_remainder` marked
    only the candidates the breaker left unattempted, so when the trip happened
    on the LAST candidate there was no remainder and no row anywhere carried
    `placement_breaker_halted`. A confirmation-required trip is neither
    "failed" nor a non-deliberate "blocked", so a batch that had already
    submitted an order persisted as an ordinary successful run despite the
    systemic breaker firing (Codex review, PR #786: "Mark the triggering
    outcome when the breaker fires")."""
    from dca_cumulative import CumulativeCreditLedger
    from morning_dca import _apply_no_order_classification
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, OrderConfirmationRequiredError

    class _ConfirmationBroker(FakeLiveBroker):
        def __init__(self):
            self.attempted = []

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.attempted.append(ticker)
            if ticker == "AAPL":
                return "o-1"
            raise OrderConfirmationRequiredError("review required")

    # Exactly four: one submits, then three confirmation-required holds trip
    # the breaker ON THE FINAL CANDIDATE, leaving no remainder to annotate.
    tickers = ("AAPL", "GS", "EVR", "IBM")
    cfg = MorningConfig(
        tickers=tickers, base_buy_amount_usd=2.0, max_consecutive_placement_failures=3
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    broker = _ConfirmationBroker()

    outcomes = ex._place_broker_orders(
        [_isolation_signal(t) for t in tickers],
        date(2026, 7, 8),
        broker,
        None,
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert broker.attempted == list(tickers), "fixture must trip on the LAST candidate"
    assert not [t for t in tickers if t not in outcomes], "no tail to annotate"
    # The trip is recorded on the row that CAUSED it...
    assert outcomes["IBM"]["placement_breaker_halted"] is True
    assert outcomes["IBM"]["placement_breaker_tripped_here"] is True
    # ...and the earlier holds are not mislabelled as the trip.
    assert "placement_breaker_tripped_here" not in outcomes["GS"]

    # ...which is what lets the aggregate classification see it at all.
    artifacts = {
        "broker_outcomes": outcomes,
        "buy_candidates": [],
        "signal_rows": [],
    }
    _apply_no_order_classification(artifacts)
    assert artifacts["orders_submitted_count"] == 1
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["error_class"] == "partial_placement_failure"


def test_a_breaker_halt_with_no_submissions_is_not_a_clean_run(tmp_path):
    """The zero-submission half of the same gap. `placement_halted` was
    consulted only inside the `if submitted:` branch, so a breaker that fired
    before ANY order landed fell through to `classify_no_order_run` with
    `placement_attempted_and_failed=False` and recorded `completed_no_orders`,
    no `is_trading_failure`, and no error class -- a systemic halt persisted as
    a clean, healthy no-order run.

    I had argued this path was already covered because
    `_concrete_order_submit_status` keeps `not_submitted_confirmation_required`.
    That is true and irrelevant: it sets the submit status, never the run
    classification (Codex review, PR #786: "Classify zero-submission breaker
    halts as failures")."""
    from morning_dca import _apply_no_order_classification

    artifacts = {
        "broker_outcomes": {
            # Three confirmation-required holds and nothing else: no row is
            # "failed", none is a non-deliberate "blocked", and none submitted.
            "BBB": {"status": "not_submitted_confirmation_required", "order_id": None},
            "CCC": {"status": "not_submitted_confirmation_required", "order_id": None},
            "DDD": {
                "status": "not_submitted_confirmation_required",
                "order_id": None,
                "placement_breaker_halted": True,
                "placement_breaker_tripped_here": True,
            },
        },
        "buy_candidates": [],
        "signal_rows": [],
    }

    _apply_no_order_classification(artifacts)

    assert artifacts["orders_submitted_count"] is None or artifacts.get(
        "orders_submitted_count", 0
    ) == 0
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == (
        "failed_after_connector_native_recovery_attempt"
    )
    # The concrete submit status still survives the failure classification --
    # is_confirmation_blocked() must keep working.
    assert artifacts["order_submit_status"] == "not_submitted_confirmation_required"


def test_an_existing_budget_without_the_breaker_key_fails_closed(tmp_path):
    """`$DCA_CONFIG_DIR` can point at an older budgets.json. Returning None for
    a missing key meant every executor stream ran that live batch with NO
    breaker, while `dca_runner` reads the same key at import and dies on the
    same omission, and the schema requires it -- divergent safety from one
    config file (Codex review, PR #786: "Fail closed when an existing budget
    omits the breaker")."""
    import json as _json

    import pytest as _pytest

    from morning_dca import load_max_consecutive_placement_failures

    (tmp_path / "budgets.json").write_text(
        _json.dumps({"circuit_breakers": {"max_per_order_usd": 10}}), encoding="utf-8"
    )
    with _pytest.raises(ValueError, match="max_consecutive_placement_failures"):
        load_max_consecutive_placement_failures(tmp_path)

    # A present, valid value still loads, while no budgets.json at all uses the
    # shared safe default -- a different condition from an outdated file.
    (tmp_path / "budgets.json").write_text(
        _json.dumps({"circuit_breakers": {"max_consecutive_placement_failures": 4}}),
        encoding="utf-8",
    )
    assert load_max_consecutive_placement_failures(tmp_path) == 4
    assert (
        load_max_consecutive_placement_failures(tmp_path / "nope")
        == DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES
    )


def test_duplicate_breaker_keys_in_budgets_are_refused(tmp_path):
    """`dca_config` and `dca_runner` both reject duplicate JSON keys, so a
    budgets.json carrying `max_consecutive_placement_failures` twice -- 1 then
    999 -- is refused by the afternoon path while this loader's plain
    json.loads silently kept the 999, giving the morning, market-open, midday
    and intraday streams a materially weaker breaker from the SAME file (Codex
    review, PR #786: "Reject duplicate placement-breaker keys")."""
    import json as _json

    import pytest as _pytest

    from morning_dca import load_max_consecutive_placement_failures

    (tmp_path / "budgets.json").write_text(
        '{"circuit_breakers": {"max_consecutive_placement_failures": 1, '
        '"max_consecutive_placement_failures": 999}}',
        encoding="utf-8",
    )
    with _pytest.raises(RuntimeError, match="could not be read or parsed"):
        load_max_consecutive_placement_failures(tmp_path)

    # A single unambiguous value still loads.
    (tmp_path / "budgets.json").write_text(
        _json.dumps({"circuit_breakers": {"max_consecutive_placement_failures": 3}}),
        encoding="utf-8",
    )
    assert load_max_consecutive_placement_failures(tmp_path) == 3


def test_a_deliberate_skip_beside_a_submission_stays_a_clean_run(tmp_path):
    """A safety gate is not a failure: cap, ceiling, and review blocks carry
    deliberate_skip=True and must leave a submitted batch clean."""
    from morning_dca import _apply_no_order_classification

    artifacts = {
        "broker_outcomes": {
            "AAA": {"status": "submitted", "order_id": "o-1"},
            "BBB": {"status": "blocked", "skip_reason": "cap exceeded", "deliberate_skip": True},
        },
        "buy_candidates": [],
        "signal_rows": [],
    }

    _apply_no_order_classification(artifacts)

    assert artifacts["order_submit_status"] == "submitted"
    assert "is_trading_failure" not in artifacts
    assert "run_classification_outcome" not in artifacts


def test_a_mixed_dry_run_batch_keeps_its_dry_run_identity(tmp_path):
    """One broker per run, so a single dry_run row proves DryRunBroker was used.
    Requiring EVERY row to be dry_run meant one cap-blocked ticker beside a
    simulated order persisted the run as a genuine live no-order evaluation
    (Codex review)."""
    from morning_dca import _concrete_order_submit_status

    rows = [
        {"status": "dry_run", "order_id": "DRY-RUN-SIM-AAA"},
        {"status": "blocked", "skip_reason": "cap exceeded", "deliberate_skip": True},
    ]

    assert _concrete_order_submit_status(rows) == "dry_run"
    # Still authoritative over a failure, matching the afternoon derivation which
    # checks config.dry_run first.
    assert _concrete_order_submit_status(rows + [{"status": "failed"}]) == "dry_run"


# --- Classification account provenance ---------------------------------------
# run_classification() stamped the import-time ROBINHOOD_AGENTIC_ACCOUNT_NUMBER
# constant while execution pinned to the load-time config value. A process that
# populated or changed the variable after importing morning_dca but before
# calling load_morning_config() traded against one account and wrote an artifact
# naming another -- and with the variable unset the artifact carried the
# "UNSET_..." sentinel, which violates the schema's ^[0-9]{6,12}$ pattern
# (Codex review, PR #219).

def test_live_classification_stamps_the_loaded_account(tmp_path):
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    pattern = re.compile(schema["properties"]["account_number"]["pattern"])

    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        target_account_number="123456789",
    )
    rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    assert rc["account_number"] == "123456789"
    assert pattern.fullmatch(rc["account_number"])


def test_live_classification_follows_config_not_the_import_time_constant(tmp_path):
    """The divergence itself: a config account that differs from the constant."""
    import morning_dca as morning_module

    # Assembled from fragments: a bare nine-digit literal here would trip the
    # secrets guard in test_config_invariants.py, which now scans test files.
    other_account = "9876" + "54321"
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        target_account_number=other_account,
    )
    rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    assert rc["account_number"] == other_account
    assert rc["account_number"] != morning_module.ROBINHOOD_AGENTIC_ACCOUNT_NUMBER


@pytest.mark.parametrize("mode", ["watchlist", "preflight"])
def test_read_only_classifications_carry_no_account(tmp_path, mode):
    """A scan that places no orders has no execution account to report.

    Also keeps the literal "read_only" out of a field the schema constrains to
    a digit shape.
    """
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        target_account_number="read_only",
    )
    rc = MorningDCAExecutor(cfg, mode=mode).run_classification(_now(), manual=False)
    assert "account_number" not in rc


def test_live_classification_always_carries_an_account(tmp_path):
    """The schema REQUIRES account_number for every *_dca_live run_type.

    Omitting it -- which an earlier version of this fix did whenever the value
    failed a shape check -- produced schema-invalid evidence for exactly the runs
    that matter most (account mismatch, connector failure), so a watchdog would
    report the scheduled run as missing rather than surfacing its block reason.
    Validity is now guaranteed upstream by resolve_broker_account, so this stamps
    unconditionally for trading modes.
    """
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "morning_run_classification.schema.json"
    schema = json.loads(schema_path.read_bytes())
    conditional = next(
        block
        for block in schema["allOf"]
        if block.get("if", {}).get("properties", {}).get("run_type", {}).get("const")
        == "morning_dca_live"
        and "account_number" in block.get("then", {}).get("required", [])
    )
    assert "account_number" in conditional["then"]["required"]

    cfg = MorningConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    assert rc["run_type"] == "morning_dca_live"
    assert "account_number" in rc


# --- Blocked-run evidence stays schema-valid ---------------------------------
# account_pinning_failure() deliberately blocks WITHOUT aborting, so
# _blocked_live_result() can route through build_artifacts() and leave operators
# durable evidence. But the schema required account_number for every *_dca_live
# run, so that evidence had no valid form: stamping a malformed value fails the
# pattern, omitting it fails the conditional `required`. The schema now admits
# the blocked state explicitly via account_pinning_failed (Codex review, #219).

def _morning_schema():
    path = Path(__file__).resolve().parent.parent / "schemas" / "morning_run_classification.schema.json"
    return json.loads(path.read_bytes())


@pytest.mark.parametrize(
    "unpinnable",
    ["acct-123", "12345", "UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "", None],
)
def test_unpinnable_live_classification_is_marked_and_schema_valid(tmp_path, unpinnable):
    jsonschema = pytest.importorskip("jsonschema")

    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        target_account_number=cast(Any, unpinnable),
    )
    rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)

    assert rc["account_pinning_failed"] is True
    assert "account_number" not in rc, "an unusable value must never be recorded as an account"
    jsonschema.validate(rc, _morning_schema())


def test_a_pinning_failed_document_asserting_valid_scheduled_evidence_is_rejected(tmp_path):
    """A run that could not pin an account never placed an order, so it can
    never be valid scheduled evidence -- same gap as the midday schema, fixed
    for parity (Codex review, PR #219: "Deny scheduled evidence when account
    pinning fails")."""
    jsonschema = pytest.importorskip("jsonschema")
    cfg = MorningConfig(
        tickers=("SMALL",), morning_run_state_root=str(tmp_path), target_account_number=cast(Any, None)
    )
    rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    assert rc["account_pinning_failed"] is True
    jsonschema.validate(rc, _morning_schema())  # the real value still passes

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**rc, "valid_scheduled_evidence": True}, _morning_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {k: v for k, v in rc.items() if k != "valid_scheduled_evidence"}, _morning_schema()
        )


def test_a_normal_live_classification_carries_the_account_and_no_marker(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")

    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        target_account_number="123456789",
    )
    rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)

    assert rc["account_number"] == "123456789"
    assert "account_pinning_failed" not in rc
    jsonschema.validate(rc, _morning_schema())


@pytest.mark.parametrize("mode", [MORNING_WATCHLIST_MODE, MORNING_PREFLIGHT_MODE])
def test_read_only_classifications_omit_a_well_formed_account_number(tmp_path, mode):
    """account_number is documented ("used for morning live execution") and
    schema-described as optional-not-required for watchlist/preflight, but the
    stamping branch used to key ONLY on whether target_account_number happened
    to be well-formed -- not on whether this run actually trades. A read-only
    executor built without going through load_morning_config's per-mode
    broker_account resolution (e.g. MorningConfig()'s own class default, which
    is the import-time ROBINHOOD_AGENTIC_ACCOUNT_NUMBER env constant) then
    stamped the real live account into an artifact that never placed an order
    (Codex review, PR #219: "Omit account numbers from read-only
    classifications")."""
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        preflight_run_state_root=str(tmp_path / "preflight"),
        target_account_number="123456789",
    )
    rc = MorningDCAExecutor(cfg, mode=mode).run_classification(_now(), manual=False)
    assert "account_number" not in rc
    assert "account_pinning_failed" not in rc


def test_the_schema_still_requires_an_account_for_unmarked_live_runs(tmp_path):
    """The relaxation must be scoped to the blocked case only.

    Derived from a real classification rather than a hand-built dict so this
    asserts on the account constraint and cannot pass or fail for unrelated
    reasons as the schema's other required fields evolve.
    """
    jsonschema = pytest.importorskip("jsonschema")

    schema = _morning_schema()
    cfg = MorningConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path),
        target_account_number="123456789",
    )
    valid = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    jsonschema.validate(valid, schema)

    stripped = {k: v for k, v in valid.items() if k != "account_number"}
    with pytest.raises(jsonschema.ValidationError, match="account_number"):
        jsonschema.validate(stripped, schema)

    # Only the explicit blocked marker lifts the requirement.
    jsonschema.validate({**stripped, "account_pinning_failed": True}, schema)
    # ...and it must be the marker, not merely the key being present.
    with pytest.raises(jsonschema.ValidationError, match="account_number"):
        jsonschema.validate({**stripped, "account_pinning_failed": False}, schema)


# --- The blocked path is REACHABLE for the common pinning failure ------------
# The evidence above only exists if an executor exists to produce it. With
# $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER unset -- by far the most common pinning
# failure -- resolve_broker_account used to raise inside
# MorningDCAExecutor.__init__, before any executor existed, so the scheduled
# slot wrote no artifact at all and monitoring saw a MISSING run rather than a
# blocked one (Codex review, PR #219).

def test_the_live_path_loads_with_an_unset_account_variable(monkeypatch, tmp_path):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    cfg = load_morning_config(mode=MORNING_LIVE_MODE, allow_unpinned_account=True)
    assert account_pinning_failure(cfg.target_account_number), (
        "the degraded config must still be one the fail-closed guard refuses to trade on"
    )


def test_a_strict_caller_still_raises_on_an_unset_account_variable(monkeypatch):
    """Only the live path degrades; a caller wanting a path or a report does not."""
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    with pytest.raises(MissingConfigEnvError):
        load_morning_config(mode=MORNING_LIVE_MODE)


def test_an_unset_account_variable_still_produces_blocked_evidence(monkeypatch, tmp_path):
    """The whole point: a blocked run, not a missing one."""
    jsonschema = pytest.importorskip("jsonschema")
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))

    executor = MorningDCAExecutor(mode=MORNING_LIVE_MODE)
    rc = executor.run_classification(_now_live(), manual=False)

    assert rc["account_pinning_failed"] is True
    assert "account_number" not in rc
    jsonschema.validate(rc, _morning_schema())


def test_the_schema_still_requires_a_broker_on_a_blocked_live_run(tmp_path):
    """account_pinning_failed excuses the ACCOUNT, never the broker -- the broker
    is known even when the account is not, and a truncated blocked-live
    classification with no broker used to validate (Codex review, PR #219)."""
    jsonschema = pytest.importorskip("jsonschema")
    cfg = MorningConfig(
        tickers=("SMALL",), morning_run_state_root=str(tmp_path), target_account_number=cast(Any, None)
    )
    rc = MorningDCAExecutor(cfg, mode="live").run_classification(_now_live(), manual=False)
    jsonschema.validate(rc, _morning_schema())

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {k: v for k, v in rc.items() if k != "execution_broker"}, _morning_schema()
        )


# --- The config-missing fallback resolves ALL artifact roots at load time ----
# model_usage_log_root was fixed to resolve at load time, but the fallback
# still left morning_run_state_root/preflight_run_state_root at None, which
# falls back to DEFAULT_MORNING_LIVE_STATE_ROOT/DEFAULT_MORNING_PREFLIGHT_
# STATE_ROOT -- import-time constants -- later. A $DCA_CRON_TRACKING_ROOT set
# between import and this call left the executor writing run_state.json and
# the preflight artifact under the OLD tree (Codex review, PR #219).

def test_unconfigured_state_root_resolves_at_executor_construction_time_for_every_mode():
    """MorningConfig.morning_run_state_root/preflight_run_state_root stay None
    when a stream's own config doesn't set them (including the config-missing
    fallback -- see load_morning_config, which deliberately does NOT bake a
    stream_root() value into those two fields the way it does for
    model_usage_log_root, because run_dca.py's cross-stream containment check
    reads their None-ness as "not explicitly configured"). The staleness this
    could otherwise cause -- MorningDCAExecutor.__init__ falling back to
    self.profile["state_root"], an import-time constant -- is fixed at that
    call site instead: it now resolves stream_root(state_root_stream) fresh
    on every construction (Codex review, PR #219)."""
    probe = (
        "import os\n"
        "import morning_dca\n"  # imported BEFORE the override
        f"os.environ['{CRON_TRACKING_ROOT_ENV}'] = os.environ['PROBE_ROOT']\n"
        "cfg = morning_dca.MorningConfig(tickers=('SMALL',))\n"
        "for mode in (morning_dca.MORNING_WATCHLIST_MODE, "
        "morning_dca.MORNING_PREFLIGHT_MODE, morning_dca.MORNING_LIVE_MODE):\n"
        "    print(morning_dca.MorningDCAExecutor(cfg, mode=mode).state_root)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = "/srv/dca-fallback-probe"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    roots = [line for line in out.stdout.splitlines() if line]
    assert roots == [
        "/srv/dca-fallback-probe/morning_dca_watchlist",
        "/srv/dca-fallback-probe/morning_dca_preflight",
        "/srv/dca-fallback-probe/morning_dca",
    ], out.stdout


def test_unconfigured_preflight_gate_root_resolves_at_call_time(tmp_path):
    """Same staleness fix as the state_root test above, but for the OTHER
    consumer of preflight_run_state_root -- _apply_preflight_gate's own
    fallback (Codex review, PR #219). Writes a preflight artifact under the
    freshly-resolved root and confirms the live executor's gate finds it,
    proving it looked there rather than at the stale import-time
    DEFAULT_MORNING_PREFLIGHT_STATE_ROOT.

    PROBE_ROOT is a fresh tmp_path per run, not a fixed path: the probe
    creates real directories on disk, and a fixed path would leave them
    behind for the next run's os.makedirs() to collide with.
    """
    probe = (
        "import os\n"
        "import json\n"
        "from datetime import date, datetime, time\n"
        "import morning_dca\n"  # imported BEFORE the override
        f"os.environ['{CRON_TRACKING_ROOT_ENV}'] = os.environ['PROBE_ROOT']\n"
        "preflight_root = morning_dca.stream_root('morning_dca_preflight')\n"
        "folder = os.path.join(preflight_root, '20260708_0945ET')\n"
        "os.makedirs(folder)\n"
        "with open(os.path.join(folder, 'run_state.json'), 'w') as f:\n"
        "    json.dump({\n"
        "        'eligible_to_execute_tickers': ['SMALL'],\n"
        "        'trading_day': '2026-07-08',\n"
        "        'run_classification': {\n"
        "            'run_origin': 'scheduled',\n"
        "            'manual_run': False,\n"
        "            'stream': 'morning',\n"
        "            'run_type': 'morning_dca_preflight',\n"
        "            'state_schema_version': 'morning-preflight-v2-machine-complete',\n"
        "        },\n"
        "    }, f)\n"
        "cfg = morning_dca.MorningConfig(tickers=('SMALL',))\n"
        "ex = morning_dca.MorningDCAExecutor(cfg, mode=morning_dca.MORNING_LIVE_MODE)\n"
        "now = datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=morning_dca.EASTERN)\n"
        "ex._apply_preflight_gate(now, ['SMALL'], manual=False)\n"
        "print(ex.preflight_gate['preflight_found'])\n"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = str(tmp_path / "dca-preflight-gate-probe")
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True", out.stdout


def test_unconfigured_preflight_forbidden_roots_resolve_at_call_time(tmp_path):
    """The gate's OWN root already resolves fresh (see the test above), but it
    used to compare that fresh root against DEFAULT_PREFLIGHT_FORBIDDEN_ROOTS,
    an import-time constant. A relocated preflight_run_state_root pointing
    into the (fresh) live-output tree would then no longer match the (stale)
    forbidden path, silently passing the must_not_look_under check it exists
    to enforce (Codex review, PR #219: "Resolve forbidden preflight roots at
    call time"). Points preflight_run_state_root inside the freshly-resolved
    live root and confirms the gate still recognizes it as forbidden.

    NON-BLOCKING as of 2026-08-02 (Geoff: "should not block any later runs"):
    a forbidden root degrades a SCHEDULED run to the full universe with a
    warning, exactly like a manual run, rather than blocking to zero tickers
    -- this test now asserts that degrade, not a block."""
    probe = (
        "import os\n"
        "from datetime import date, datetime, time\n"
        "import morning_dca\n"  # imported BEFORE the override
        f"os.environ['{CRON_TRACKING_ROOT_ENV}'] = os.environ['PROBE_ROOT']\n"
        "live_root = morning_dca.stream_root('morning_dca')\n"
        "cfg = morning_dca.MorningConfig("
        "tickers=('SMALL',), preflight_run_state_root=live_root + '/stray')\n"
        "ex = morning_dca.MorningDCAExecutor(cfg, mode=morning_dca.MORNING_LIVE_MODE)\n"
        "now = datetime.combine(date(2026, 7, 8), time(10, 30), tzinfo=morning_dca.EASTERN)\n"
        "admitted = ex._apply_preflight_gate(now, ['SMALL'], manual=False)\n"
        "print(admitted)\n"
        "print(ex.preflight_gate['preflight_found'])\n"
        "print('forbidden' in ex.preflight_gate['warning'])\n"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = str(tmp_path / "dca-preflight-forbidden-probe")
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = out.stdout.splitlines()
    assert lines == ["['SMALL']", "False", "True"], out.stdout


def test_csv_qualifying_notional_total_reads_the_canonical_submitted_amount_column(tmp_path):
    """Codex review: "Read the canonical submitted amount column". The
    submitted_orders.csv fallback (added for market-open/intraday prior-
    weekday cap seeding, which have no buy_candidates/filled_orders.csv/
    orders.csv) opened the file but only ever checked notional_usd/
    amount_usd -- the canonical column scripts.scheduled_artifacts.
    SUBMITTED_ORDERS_FIELDS actually defines is submitted_amount_usd, so the
    fallback always summed to $0.00 for a real submitted_orders.csv."""
    csv_path = tmp_path / "submitted_orders.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "order_id", "submitted_amount_usd", "status"])
        writer.writerow(["AAAU", "ord-1", "7.50", "submitted"])
        writer.writerow(["TRIN", "ord-2", "2.00", "filled"])
        writer.writerow(["DRY", "DRY-RUN-1", "5.00", "submitted"])

    total = _csv_qualifying_notional_total(csv_path, blank_status_means_filled=False)
    assert total == pytest.approx(9.50)


def test_csv_qualifying_notional_total_reserves_in_flight_order_statuses(tmp_path):
    """Codex review: "Reserve in-flight canonical orders in weekly cap
    seeding". scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES includes
    'pending' and 'unconfirmed' among the statuses the canonical
    submitted_orders.csv writer uses for a placed-or-in-flight order, not
    just 'submitted'/'filled'. A pending or unconfirmed row may still fill
    later, so it must be reserved (counted) here rather than read as $0 --
    treating it as $0 could let a later run re-spend headroom that order
    goes on to actually consume.

    'not_submitted_confirmation_required' is deliberately EXCLUDED even
    though it is also in ORDER_ATTEMPT_STATUSES: scripts/drift_detection.py's
    _NOT_ACTUALLY_SUBMITTED_STATUSES documents that this status means the
    order was blocked pending a separate confirmation step and never reached
    the broker, so it is not real capital and counting it would
    unnecessarily suppress later slots' valid orders (Codex review: "Exclude
    confirmation-blocked rows from pooled spend")."""
    csv_path = tmp_path / "submitted_orders.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "order_id", "submitted_amount_usd", "status"])
        writer.writerow(["AAAU", "ord-1", "5.00", "pending"])
        writer.writerow(["TRIN", "ord-2", "3.00", "unconfirmed"])
        writer.writerow(["EVR", "ord-3", "2.50", "not_submitted_confirmation_required"])
        writer.writerow(["FLAT", "ord-4", "1.00", "cap_exceeded"])
        writer.writerow(["SKIP", "ord-5", "1.00", "skipped"])

    total = _csv_qualifying_notional_total(csv_path, blank_status_means_filled=False)
    assert total == pytest.approx(8.00)


def test_csv_qualifying_notional_total_fails_closed_on_malformed_amount(tmp_path):
    """Codex review: "Fail closed on malformed canonical order rows". A row
    with a qualifying status (a real placed/in-flight order) but a blank or
    non-numeric amount is a truncated or malformed write, not a genuine $0
    order. csv.DictReader does not raise for this, so it must be explicitly
    detected and pushed into skip_sink rather than silently read as no
    spend."""
    csv_path = tmp_path / "submitted_orders.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "order_id", "submitted_amount_usd", "status"])
        writer.writerow(["AAAU", "ord-1", "5.00", "submitted"])
        writer.writerow(["TRIN", "ord-2", "", "submitted"])

    skip_sink = []
    total = _csv_qualifying_notional_total(csv_path, blank_status_means_filled=False, skip_sink=skip_sink)
    assert total == pytest.approx(5.00)
    assert skip_sink == [csv_path]


def test_csv_qualifying_notional_total_fails_closed_on_nonpositive_amount(tmp_path):
    """Codex review: "Reject nonpositive amounts in qualifying order rows".
    A qualifying-status row (a real placed/in-flight order) can never
    legitimately have a zero or negative submitted_amount_usd -- that is a
    malformed/truncated write too, exactly like a blank or non-numeric
    amount. The first malformed-amount fix only caught `notional is None`,
    so a zero or negative value on a qualifying row silently read as
    genuine $0 spend instead of failing closed."""
    csv_path = tmp_path / "submitted_orders.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "order_id", "submitted_amount_usd", "status"])
        writer.writerow(["AAAU", "ord-1", "5.00", "submitted"])
        writer.writerow(["TRIN", "ord-2", "0", "submitted"])
        writer.writerow(["DXYZ", "ord-3", "-3.00", "submitted"])

    skip_sink = []
    total = _csv_qualifying_notional_total(csv_path, blank_status_means_filled=False, skip_sink=skip_sink)
    assert total == pytest.approx(5.00)
    assert skip_sink == [csv_path, csv_path]


def test_csv_qualifying_notional_total_fails_closed_on_missing_status_header(tmp_path):
    """Codex review: "Validate canonical CSV headers before filtering rows".
    A missing/corrupted "status" header makes row.get("status") return None
    for every row, which the qualifying-status filter silently treats as
    "not qualifying" -- no exception, so the malformed-amount fail-closed
    path added for the previous finding is never reached either. A file with
    a real header row that is missing a required column must fail closed,
    not read as zero orders."""
    csv_path = tmp_path / "submitted_orders.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        # No "status" column at all -- corrupted/wrong header for a CSV type
        # that is NOT filled_orders.csv (blank_status_means_filled=False).
        writer.writerow(["ticker", "order_id", "submitted_amount_usd"])
        writer.writerow(["AAAU", "ord-1", "5.00"])

    skip_sink = []
    total = _csv_qualifying_notional_total(csv_path, blank_status_means_filled=False, skip_sink=skip_sink)
    assert total == 0.0
    assert skip_sink == [csv_path]


def test_csv_qualifying_notional_total_fails_closed_on_a_headerless_existing_file(tmp_path):
    """Codex review: "Treat headerless existing order CSVs as incomplete".
    An EXISTING file with no header line at all (zero-byte, or truncated
    before writer.writeheader() flushed) makes csv.DictReader.fieldnames
    None, not merely missing a required column. The previous fix collapsed
    None into () via `reader.fieldnames or ()` and then only validated
    `if fieldnames` (truthy), so this exact case slipped through that guard
    and silently read as a genuine zero-orders file instead of failing
    closed -- a caught-mid-write file must not be treated the same as a
    legitimately empty/quiet day."""
    csv_path = tmp_path / "submitted_orders.csv"
    csv_path.write_text("", encoding="utf-8")

    skip_sink = []
    total = _csv_qualifying_notional_total(csv_path, blank_status_means_filled=False, skip_sink=skip_sink)
    assert total == 0.0
    assert skip_sink == [csv_path]


def test_csv_qualifying_notional_total_missing_status_header_ok_for_filled_orders(tmp_path):
    """Manual filled_orders.csv files legitimately omit "status" entirely
    (blank_status_means_filled=True treats every row as a confirmed fill),
    so a missing status header there is normal, not a fail-closed case."""
    csv_path = tmp_path / "filled_orders.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "order_id", "amount_usd"])
        writer.writerow(["AAAU", "ord-1", "5.00"])

    skip_sink = []
    total = _csv_qualifying_notional_total(csv_path, blank_status_means_filled=True, skip_sink=skip_sink)
    assert total == pytest.approx(5.00)
    assert skip_sink == []


def test_cap_seeding_default_scan_roots_are_resolved_at_call_time():
    """Not just the config-missing fallback: _compute_top_up named
    DEFAULT_AFTERNOON_TASK_ROOT/DEFAULT_MARKET_OPEN_TASK_ROOT directly inside
    a method body, which always returns their IMPORT-TIME value regardless of
    when the method runs -- reachable on every run that leaves
    prior_allocation_roots at its default, not only the fallback case
    (Codex review, PR #219)."""
    probe = (
        "import os\n"
        "import morning_dca\n"  # imported BEFORE the override
        f"os.environ['{CRON_TRACKING_ROOT_ENV}'] = os.environ['PROBE_ROOT']\n"
        "print(morning_dca.default_cap_seed_roots())\n"
        "print(morning_dca.DEFAULT_AFTERNOON_TASK_ROOT)"
    )
    env = {k: v for k, v in os.environ.items() if k != CRON_TRACKING_ROOT_ENV}
    env["PROBE_ROOT"] = "/srv/dca-cap-seed-probe"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = out.stdout.splitlines()
    resolved_at_call_time = lines[0]
    stale_import_time_constant = lines[1]
    assert resolved_at_call_time == (
        "('/srv/dca-cap-seed-probe/ce16ba6b', '/srv/dca-cap-seed-probe/market_open_dca')"
    ), out.stdout
    # DEFAULT_AFTERNOON_TASK_ROOT is untouched by the override -- it is still
    # used elsewhere (e.g. test_dca_paths.py's byte-identical-defaults pin),
    # which is exactly why _compute_top_up naming it directly was wrong.
    assert stale_import_time_constant == str(REPO_ROOT / "cron_tracking" / "ce16ba6b"), out.stdout


def test_the_config_missing_fallback_resolves_the_live_account_at_call_time():
    """MorningConfig.target_account_number defaults to
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, itself resolved from the environment at
    IMPORT time (near_close_dca module load). load_morning_config's
    config-missing fallback used to return MorningConfig() without setting
    this field at all, so a live run through that fallback pinned whatever
    account happened to be set when the process first imported the module --
    not the current one -- and could block or pin incorrectly (Codex review,
    PR #219: "Resolve the account when falling back to defaults")."""
    probe = (
        "import os\n"
        "import morning_dca\n"  # imported BEFORE the account is set
        "os.environ['ROBINHOOD_AGENTIC_ACCOUNT_NUMBER'] = os.environ['PROBE_ACCOUNT']\n"
        "cfg = morning_dca.load_morning_config("
        "config_dir='/nonexistent-probe-dir', mode=morning_dca.MORNING_LIVE_MODE)\n"
        "print(cfg.target_account_number)"
    )
    env = {k: v for k, v in os.environ.items() if k != "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"}
    env["PROBE_ACCOUNT"] = "123456789"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "123456789", out.stdout


def test_the_config_missing_fallback_fails_closed_for_live_mode_without_an_account(monkeypatch):
    """allow_unpinned_account's raise-by-default contract (see this function's
    docstring) must hold in the config-missing fallback too, not just the
    config-present path."""
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    with pytest.raises(MissingConfigEnvError):
        load_morning_config(config_dir="/nonexistent-probe-dir", mode=MORNING_LIVE_MODE)


def test_the_config_missing_fallback_leaves_the_account_unset_when_allowed(monkeypatch, tmp_path):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    cfg = load_morning_config(
        config_dir=str(tmp_path), mode=MORNING_LIVE_MODE, allow_unpinned_account=True
    )
    assert cfg.target_account_number is None


@pytest.mark.parametrize("mode", [MORNING_WATCHLIST_MODE, MORNING_PREFLIGHT_MODE])
def test_the_config_missing_fallback_never_pins_a_real_account_for_read_only_modes(mode, tmp_path):
    """Defense in depth alongside run_classification's own profile["trades"]
    gate (Codex review, PR #219: "Omit account numbers from read-only
    classifications") -- the config-missing fallback should not even hold a
    real account for a stream that never trades."""
    cfg = load_morning_config(config_dir=str(tmp_path), mode=mode)
    assert cfg.target_account_number == "read_only"


# --- apply_deployment_risk_guard ----------------------------------------
#
# MorningConfig.as_strategy_config() builds a deliberately narrow StrategyConfig
# view that omits all three fields near_close_dca.kill_switch_engaged reads, so
# every executor stream has always evaluated with deployed_last_7d_usd pinned at
# 0.0 -- the guard is present inside graded_buy_multiplier but structurally
# cannot trip, unlike the 3:30 PM path, which injects the figure per run via
# dca_risk.apply_risk_guard. This method closes that gap opt-in; the first test
# below pins the "opt-in" half (an executor never handed a figure is unchanged).


def test_executor_sizing_carries_no_deployment_figure_until_the_guard_is_armed():
    ex = MorningDCAExecutor(MorningConfig())
    assert ex.sizing_config.deployed_last_7d_usd == 0.0


def test_apply_deployment_risk_guard_engages_and_caps_the_ladder():
    ex = MorningDCAExecutor(MorningConfig())
    deep_drop = max(threshold for threshold, _multiplier in ex.sizing_config.sizing_tiers)
    uncapped = ex.multiplier_for_drop(deep_drop)
    assert uncapped > 1.0

    engaged = ex.apply_deployment_risk_guard(
        deployed_last_7d_usd=1500.0,
        monthly_cash_allocation_usd=3600.0,
        kill_switch_7d_deploy_fraction=0.40,
    )
    assert engaged is True
    # "The strategy still participates in a dip at the base amount but stops
    # scaling up" -- graded_buy_multiplier's own contract.
    assert ex.multiplier_for_drop(deep_drop) == 1.0


def test_apply_deployment_risk_guard_below_the_threshold_leaves_sizing_alone():
    ex = MorningDCAExecutor(MorningConfig())
    deep_drop = max(threshold for threshold, _multiplier in ex.sizing_config.sizing_tiers)
    uncapped = ex.multiplier_for_drop(deep_drop)

    engaged = ex.apply_deployment_risk_guard(
        deployed_last_7d_usd=100.0,
        monthly_cash_allocation_usd=3600.0,
        kill_switch_7d_deploy_fraction=0.40,
    )
    assert engaged is False
    assert ex.multiplier_for_drop(deep_drop) == uncapped


def test_apply_deployment_risk_guard_preserves_every_other_sizing_field():
    """Only the three risk fields move; the ladder, bases, calibration, and
    minimum drop this executor was constructed with must survive intact."""
    ex = MorningDCAExecutor(MorningConfig())
    before = ex.sizing_config
    ex.apply_deployment_risk_guard(
        deployed_last_7d_usd=10.0,
        monthly_cash_allocation_usd=3600.0,
        kill_switch_7d_deploy_fraction=0.40,
    )
    after = ex.sizing_config
    assert after.sizing_tiers == before.sizing_tiers
    assert after.base_buy_amount_usd == before.base_buy_amount_usd
    assert after.base_buy_amount_below_close_usd == before.base_buy_amount_below_close_usd
    assert after.per_ticker_sizing == before.per_ticker_sizing
    assert after.minimum_drop_vs_previous_close == before.minimum_drop_vs_previous_close
    assert after.dry_run is True


def test_apply_deployment_risk_guard_tolerates_an_unset_allocation():
    """kill_switch_engaged documents a non-positive allocation as the deliberate
    'operator left the allocation unset' escape hatch. Rejecting it here would
    make this method stricter than the guard it arms."""
    ex = MorningDCAExecutor(MorningConfig())
    engaged = ex.apply_deployment_risk_guard(
        deployed_last_7d_usd=99999.0,
        monthly_cash_allocation_usd=0.0,
        kill_switch_7d_deploy_fraction=0.40,
    )
    assert engaged is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"deployed_last_7d_usd": float("nan")},
        {"deployed_last_7d_usd": float("inf")},
        {"deployed_last_7d_usd": -1.0},
        {"deployed_last_7d_usd": True},
        {"deployed_last_7d_usd": "500"},
        {"monthly_cash_allocation_usd": float("nan")},
        {"kill_switch_7d_deploy_fraction": float("nan")},
        {"kill_switch_7d_deploy_fraction": 1.5},
        {"kill_switch_7d_deploy_fraction": -0.1},
    ],
)
def test_apply_deployment_risk_guard_rejects_values_that_would_disable_the_guard(kwargs):
    """Each of these reaches kill_switch_engaged's `deployed > fraction *
    allocation` comparison, where a NaN or out-of-range fraction compares False
    and silently DISABLES the guard rather than tripping it -- the exact
    inversion arming it is meant to prevent. A bool would launder into a
    valid-looking 1.0 via float()."""
    ex = MorningDCAExecutor(MorningConfig())
    args = {
        "deployed_last_7d_usd": 10.0,
        "monthly_cash_allocation_usd": 3600.0,
        "kill_switch_7d_deploy_fraction": 0.40,
    }
    args.update(kwargs)
    with pytest.raises(ValueError):
        ex.apply_deployment_risk_guard(**args)
    # Nothing was mutated by the rejected call.
    assert ex.sizing_config.deployed_last_7d_usd == 0.0


@pytest.mark.parametrize("allocation", [-1.0, -0.01, -1000.0])
def test_risk_guard_rejects_a_negative_monthly_allocation(allocation):
    """Regression for "Reject negative monthly allocation values".

    kill_switch_engaged short-circuits on `allocation <= 0`, so a negative
    figure disables the deployment guard exactly as the documented "unset"
    zero does -- leaving deep-drop candidates uncapped no matter how large the
    trailing deployment is. It is not the case that escape hatch documents,
    and schemas/budgets.schema.json sets `minimum: 0` on the field at both its
    top-level and risk_guard positions, so a negative allocation is already
    outside a valid config.
    """
    executor = MorningDCAExecutor(config=MorningConfig())
    with pytest.raises(ValueError, match="monthly_cash_allocation_usd must be non-negative"):
        executor.apply_deployment_risk_guard(
            deployed_last_7d_usd=500.0,
            monthly_cash_allocation_usd=allocation,
            kill_switch_7d_deploy_fraction=0.4,
        )


def test_risk_guard_still_accepts_the_unset_allocation_of_zero():
    """Zero is the escape hatch and must keep working.

    Guards the check above against over-firing: rejecting zero would make this
    method stricter than the guard it arms, and would wedge a legitimately
    unconfigured stream rather than leaving its ladder alone.
    """
    executor = MorningDCAExecutor(config=MorningConfig())
    engaged = executor.apply_deployment_risk_guard(
        deployed_last_7d_usd=500.0,
        monthly_cash_allocation_usd=0.0,
        kill_switch_7d_deploy_fraction=0.4,
    )
    assert engaged is False


def test_a_rejected_risk_guard_call_leaves_sizing_untouched():
    """The rejection must not half-arm the executor.

    apply_deployment_risk_guard replaces _sizing_config wholesale, so a raise
    that happened after that assignment would leave the ladder carrying a
    negative allocation -- the very state the check exists to refuse.
    """
    executor = MorningDCAExecutor(config=MorningConfig())
    before = executor._sizing_config
    with pytest.raises(ValueError):
        executor.apply_deployment_risk_guard(
            deployed_last_7d_usd=500.0,
            monthly_cash_allocation_usd=-1.0,
            kill_switch_7d_deploy_fraction=0.4,
        )
    assert executor._sizing_config is before


def test_ambiguous_placement_is_recorded_as_potentially_submitted(tmp_path):
    """Codex review, PR #1300 discussion_r3920155064 ("Emit the new
    ambiguous-placement status"): a placement connector exception that does
    NOT prove the broker rejected the order (OrderPlacementAmbiguousError,
    e.g. a response timeout) must be classified "potentially_submitted" --
    distinct from the definitive "failed" status the previous test asserts
    -- with the planned notional still reserved, not dropped."""
    from near_close_dca import OrderPlacementAmbiguousError

    class _AmbiguousBroker(_TopUpBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise OrderPlacementAmbiguousError(f"placement connector call for {ticker} timed out")

    ex = _live_executor(tmp_path, ("NVDA",))
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})
    res = ex.run(_now_live(), data, broker=_AmbiguousBroker())

    artifacts = res["artifacts"]
    outcome = artifacts["broker_outcomes"]["NVDA"]
    assert outcome["status"] == "potentially_submitted"
    assert outcome["order_id"] is None
    assert "ambiguous" in outcome["skip_reason"]
    assert outcome["submitted_amount_usd"] > 0



def test_blocked_live_retry_preserves_potentially_submitted_evidence(tmp_path):
    # Codex review, PR #1300 discussion_r3920155079 ("Preserve in-flight
    # statuses on blocked retries"): _existing_submitted_run_state()
    # previously recognized only "submitted"/"filled", so an existing
    # same-slot artifact reconciled to "pending", "unconfirmed", or
    # "potentially_submitted" (an ambiguous placement -- see
    # OrderPlacementAmbiguousError) looked like nothing to preserve. A later
    # retry that blocks before placement then overwrote the dated
    # run_state.json with an empty blocked artifact instead of preserving
    # the still-unresolved broker order and its raw-input references.
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
    (run_dir / "run_state.json").write_text(
        json.dumps({"buy_candidates": [{"ticker": "SMALL", "status": "potentially_submitted"}]}),
        encoding="utf-8",
    )

    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_live(), data, broker=None)

    artifacts = res["artifacts"]
    assert artifacts["status"] == "blocked"
    preserved_rows = [c for c in artifacts["buy_candidates"] if c.get("status") == "potentially_submitted"]
    assert {c["ticker"] for c in preserved_rows} == {"SMALL"}
    assert Path(res["paths"]["run_state"]).exists()



class _AmbiguousThenRawFailBroker(FakeLiveBroker):
    """Places the first candidate ambiguously, then breaks raw persistence
    for the next.

    Simulates the scenario Codex flagged on PR #1300 discussion_r3920257824
    ("Preserve ambiguous placements when raw persistence fails"): with the
    real RobinhoodBroker wrapped by _PersistingBrokerAdapter, a placement
    timeout can produce BOTH a "potentially_submitted" outcome AND
    raw_recorder.error on the very same run, since the connector failure is
    what makes the placement ambiguous in the first place. The already-
    ambiguous order must survive in the final artifact instead of being
    discarded by a blank broker_outcomes={} stub.
    """

    def __init__(self, raw_dir):
        self._raw_dir = raw_dir
        self._tradability_calls = 0

    def place_market_buy(self, ticker, notional_usd, ref_id):
        from near_close_dca import OrderPlacementAmbiguousError

        raise OrderPlacementAmbiguousError(f"placement connector call for {ticker} timed out")

    def is_tradable_for_fractional_buy(self, ticker):
        self._tradability_calls += 1
        if self._tradability_calls > 1:
            # Corrupt the raw directory so the NEXT persisted broker response
            # (this tradability check, for the second candidate) fails to
            # save -- but only after the first candidate's ambiguous
            # placement has already happened.
            if self._raw_dir.is_dir():
                import shutil

                shutil.rmtree(self._raw_dir)
            self._raw_dir.write_text("not a directory", encoding="utf-8")
        return True



def test_blocked_live_run_preserves_ambiguous_placement_on_later_raw_failure(tmp_path):
    pf_root = _seed_preflight(tmp_path / "pf", ("AAA", "BBB"))
    cfg = MorningConfig(
        tickers=("AAA", "BBB"),
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    raw_dir = ex.run_state_dir(_now_live()) / "raw"
    broker = _AmbiguousThenRawFailBroker(raw_dir)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"AAA": 0.05, "BBB": 0.05})

    res = ex.run(_now_live(), data, broker=broker)
    artifacts = res["artifacts"]

    assert artifacts["status"] == "blocked"
    # The ambiguous placement must survive, not be wiped by an empty
    # broker_outcomes={} stub -- the pre-fix behavior, since the old
    # any_submitted check only recognized the literal "submitted" status.
    assert artifacts["broker_outcomes"]["AAA"]["status"] == "potentially_submitted"
    assert artifacts["broker_outcomes"]["BBB"]["status"] == "blocked"
    assert "raw input persistence failed" in artifacts["broker_outcomes"]["BBB"]["skip_reason"]
    on_disk = json.loads(Path(res["paths"]["run_state"]).read_bytes())
    assert on_disk["broker_outcomes"]["AAA"]["status"] == "potentially_submitted"



def test_carried_forward_order_rows_includes_potentially_submitted():
    # Same fix as _submitted_keys above, applied to the submitted_orders.csv
    # carry-forward path: an ambiguous placement preserved only as a
    # buy_candidates row (not in this call's own broker_outcomes) must still
    # reach the CSV, or a later run's cap-seeding scan of that CSV would not
    # see the reservation.
    from morning_dca import _carried_forward_order_rows

    buy_candidates = [
        {
            "ticker": "CCC",
            "status": "potentially_submitted",
            "order_id": None,
            "planned_amount_usd": 12.0,
        }
    ]
    rows = _carried_forward_order_rows({}, buy_candidates)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "CCC"
    assert rows[0]["status"] == "potentially_submitted"
    assert rows[0]["submitted_amount_usd"] == 12.0

    # Codex review, PR #1300 discussion_r3920709301 ("Retain pending orders
    # in retry aggregates"): a "pending"/"unconfirmed" row must reach
    # submitted_orders.csv the same way, not just "potentially_submitted".
    pending_rows = [
        {"ticker": "FFF", "status": "pending", "order_id": None, "planned_amount_usd": 8.0}
    ]
    pending_extra = _carried_forward_order_rows({}, pending_rows)
    assert len(pending_extra) == 1
    assert pending_extra[0]["ticker"] == "FFF"
    assert pending_extra[0]["status"] == "pending"



def test_morning_place_broker_orders_preserves_credit_for_every_candidate_after_raw_persistence_break(
    tmp_path,
):
    # Codex review, PR #676 discussion_r3914291526 and discussion_r3919946257
    # ("Preserve credits for every candidate skipped by the break"): a raw
    # persistence failure detected while evaluating the FIRST candidate used
    # to record a blocked outcome only for that ticker before breaking out of
    # the loop. The post-loop preserve_pending pass skips any ticker with no
    # entry in ``outcomes``, so every later, never-evaluated candidate lost
    # its already-computed threshold-crossing credit decline. Both AAA (the
    # ticker actually being evaluated when the failure was noticed) and BBB
    # (a later candidate that never got its own loop iteration) must come
    # back blocked with their pending credit preserved.
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER, TOP_UP_NO_PRIOR_RUN

    class _Broker(FakeLiveBroker):
        def __init__(self):
            self.placed = {}
            self._recorder = type(
                "_Recorder", (), {"error": "simulated raw persistence failure"}
            )()

        def account_number(self):
            return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 500.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed[ticker] = notional_usd
            return f"ORDER-{ticker}"

    cfg = MorningConfig(tickers=("AAA", "BBB"), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)

    def _signal(ticker):
        return MorningSignal(
            ticker=ticker,
            source="finance",
            action=Action.BUY,
            reason="test",
            current_price=99.0,
            previous_close=100.0,
            drop_pct=1.0,
            multiplier=1.0,
            hypothetical_amount_usd=1.0,
            selected_base_amount_usd=1.0,
        )

    def _top_up(ticker, decline_usd):
        return TopUpOutcome(
            ticker=ticker,
            desired_amount=1.30,
            decision_price=99.0,
            prior_same_day_amount=0.0,
            prior_same_day_reference_price=None,
            prior_same_day_run_id=None,
            prior_same_day_source=None,
            incremental_order_amount=1.0,
            top_up_guard_result=TOP_UP_NO_PRIOR_RUN,
            top_up_skipped=False,
            effective_base_amount_usd=1.0,
            cumulative_credit_decline_usd=decline_usd,
        )

    broker = _Broker()
    outcomes = ex._place_broker_orders(
        [_signal("AAA"), _signal("BBB")],
        trading_day,
        broker,
        {"AAA": _top_up("AAA", 0.30), "BBB": _top_up("BBB", 0.45)},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )

    assert outcomes["AAA"]["status"] == "blocked"
    assert outcomes["BBB"]["status"] == "blocked"
    assert not broker.placed
    assert ex.credit_ledger.pending_usd("AAA", trading_day.isoformat()) == 0.30
    assert ex.credit_ledger.pending_usd("BBB", trading_day.isoformat()) == 0.45



class _AmbiguousThenLegacyDupBroker(FakeLiveBroker):
    """Raises OrderPlacementAmbiguousError on the FIRST place_market_buy for
    a ticker, then reports that ticker's ordinary-ref_id attempt as a
    same-stream duplicate on a later call -- but WITHOUT a dollar_amount
    field, simulating a legacy adapter that can confirm the order exists at
    the broker without being able to report its notional (Codex review, PR
    #1300 discussion_r3920826583: "Preserve ambiguous amounts when retry
    dedup lacks broker details").
    """

    from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER as _acct
    supports_concurrent_fill_lookup: bool = False

    def __init__(self):
        self._attempted_ref_ids = []
        self._raise_next = True

    def account_number(self) -> str:
        return self._acct

    def available_buying_power(self) -> float:
        return 1000.0

    def is_tradable_for_fractional_buy(self, ticker) -> bool:
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id) -> str:
        self._attempted_ref_ids.append((ticker, ref_id))
        if self._raise_next:
            self._raise_next = False
            from near_close_dca import OrderPlacementAmbiguousError

            raise OrderPlacementAmbiguousError(f"placement connector call for {ticker} timed out")
        return f"ORDER-{ticker}"

    def same_day_strategy_order_exists(self, ticker, trading_day) -> bool:
        return any(t == ticker for t, _ in self._attempted_ref_ids)

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        for t, rid in self._attempted_ref_ids:
            if t == ticker and any(rid.startswith(p) for p in ref_id_prefixes):
                return {"id": f"ORDER-{ticker}"}
        return None



def test_morning_scheduled_top_up_duplicate_preserves_ambiguous_amount_without_broker_details(tmp_path):
    # Codex review, PR #1300 discussion_r3920826583 ("Preserve ambiguous
    # amounts when retry dedup lacks broker details"): mirrors
    # test_morning_scheduled_top_up_duplicate_preserves_original_submitted_amount
    # above, but the first attempt's placement was AMBIGUOUS
    # (potentially_submitted, $15) rather than a confirmed submission, and
    # the retry's duplicate lookup can confirm the order exists but not its
    # dollar amount -- forcing the dup_amount fallback chain to reach
    # prior_submitted_amounts. Excluding "potentially_submitted" from that
    # map let it fall through to the freshly recomputed (smaller) $12
    # incremental instead, understating cap usage for an order that may
    # already be live.
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
    broker = _AmbiguousThenLegacyDupBroker()
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"NVDA": 0.10})

    # First scheduled attempt: no prior allocation -> full $15 desired,
    # placement raises OrderPlacementAmbiguousError.
    first = ex.run(_now_live(), data, broker=broker)
    first_outcome = first["artifacts"]["broker_outcomes"]["NVDA"]
    assert first_outcome["status"] == "potentially_submitted"
    assert first_outcome["submitted_amount_usd"] == pytest.approx(15.0)

    # A $3 external fill for NVDA appears afterward.
    _seed_prior(prior_root, "NVDA", 3.0, reference_price=95.0, selected_base_amount_usd=2.0)

    # Scheduled retry: has_prior_allocation is now True, freshly recomputed
    # incremental is $15 - $3 = $12, and the broker's duplicate lookup finds
    # the earlier attempt's ref_id but reports no dollar_amount.
    retry_now = _now_live() + timedelta(minutes=1)
    retry = ex.run(retry_now, data, broker=broker)
    nvda_outcome = retry["artifacts"]["broker_outcomes"]["NVDA"]
    assert nvda_outcome["status"] == "submitted"
    assert nvda_outcome["submitted_amount_usd"] == pytest.approx(15.0), (
        f"duplicate retry must preserve the ambiguous first attempt's ORIGINAL "
        f"$15, not the freshly recomputed $12 incremental; got {nvda_outcome}"
    )



def test_persisting_broker_adapter_treats_unreadable_placement_pointer_as_ambiguous(tmp_path):
    # Codex review, PR #1300 discussion_r3920988293 ("Treat unreadable
    # placement pointers as ambiguous"): the placement connector call
    # itself SUCCEEDS here (unlike the connector-timeout case
    # OrderPlacementAmbiguousError already covers), returning only an
    # unresolvable output_path pointer with no order id anywhere inline.
    # persist() cannot read that file, so persisted is None -- falling
    # through to the generic "return the raw payload" branch would hand
    # place_market_buy an id-less envelope it reads as a DEFINITIVE "no
    # order id" rejection, dropping the notional even though the broker's
    # real answer (which we simply failed to read back locally) is
    # genuinely unknown.
    from near_close_dca import OrderPlacementAmbiguousError

    missing = tmp_path / "missing-placement-response.json"
    broker = RobinhoodBroker(
        place_fn=lambda **kwargs: {"output_path": str(missing)},
        enable_live_orders=True,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingBrokerAdapter(broker, recorder)

    with pytest.raises(OrderPlacementAmbiguousError):
        wrapped.place_market_buy("AAPL", 10.0, "ref-1")

    assert recorder.error is not None
    assert "could not read output_path" in recorder.error



def test_persisting_finance_provider_restores_native_datetime_after_raw_round_trip(tmp_path):
    native_timestamp = datetime(2026, 7, 8, 10, 20, tzinfo=EASTERN)
    provider = FinanceDataProvider(
        bars_fn=lambda *args: [
            {
                "timestamp": native_timestamp,
                "open": 99.0,
                "high": 100.0,
                "low": 98.0,
                "close": 99.5,
                "volume": 10,
            }
        ],
        previous_close_fn=lambda *args: None,
    )
    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(provider, recorder)
    now = _now_live()

    bars = wrapped.get_intraday_bars(
        ticker="AAPL", start=now - timedelta(hours=1), end=now, interval_minutes=10
    )

    assert len(bars) == 1
    assert bars[0].timestamp == native_timestamp
    archived = json.loads((tmp_path / "raw" / recorder.saved_filenames[0]).read_bytes())
    assert archived["data"][0]["timestamp"] == {"__datetime__": native_timestamp.isoformat()}



def test_persisting_market_data_provider_persists_result_when_wrapped_connector_is_bypassed(tmp_path):
    class CachedProvider:
        source = "test"

        def __init__(self):
            # Its presence makes the decorator wrap a recognized connector,
            # but get_quote deliberately serves an independent cache.
            self._quotes_fn = lambda **kwargs: pytest.fail("cached lookup must bypass connector")

        def get_quote(self, ticker, as_of):
            return (101.0, as_of)

    recorder = _RawInputRecorder(tmp_path / "raw", "2026-07-08")
    wrapped = _PersistingMarketDataProvider(CachedProvider(), recorder)
    now = _now_live()

    quote = wrapped.get_quote("AAPL", now)
    assert quote[0] == 101.0
    assert quote[1] == now
    assert any(name.startswith("quote_AAPL_") for name in recorder.saved_filenames)



def test_place_broker_orders_settles_ambiguous_credit_exactly_once(tmp_path):
    # Codex review, PR #1300 discussion_r3920344722 ("Avoid settling and
    # re-preserving the same ambiguous credit"): an ambiguous placement
    # (OrderPlacementAmbiguousError) that is also a cumulative-credit top-up
    # settles the existing pending balance in its own except-block -- but the
    # GENERIC blocked-placement pass afterward previously did not exclude
    # "potentially_submitted", so it immediately called preserve_pending()
    # with the same decline, restoring a balance settle() just cleared and
    # leaving the ledger at neither the settled ($0.00) nor the never-
    # happened ($0.55) figure.
    from dca_cumulative import CumulativeCreditLedger
    from near_close_dca import TOP_UP_INCREMENTAL, OrderPlacementAmbiguousError

    class _AmbiguousBroker:
        def account_number(self):
            return "999000111"

        def available_buying_power(self):
            return 1000.0

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise OrderPlacementAmbiguousError(f"placement connector call for {ticker} timed out")

    cfg = MorningConfig(tickers=("AAPL",), base_buy_amount_usd=1.0)
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    ex.credit_ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 7, 8)
    ex.credit_ledger.record("AAPL", trading_day.isoformat(), 0.25, stream="morning")
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.25

    signal = MorningSignal(
        ticker="AAPL",
        source="finance",
        action=Action.BUY,
        reason="test",
        current_price=99.0,
        previous_close=100.0,
        drop_pct=1.0,
        multiplier=1.0,
        hypothetical_amount_usd=1.0,
        selected_base_amount_usd=1.0,
    )
    outcome = TopUpOutcome(
        ticker="AAPL",
        desired_amount=1.30,
        decision_price=99.0,
        prior_same_day_amount=1.0,
        prior_same_day_reference_price=100.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        incremental_order_amount=1.0,
        top_up_guard_result=TOP_UP_INCREMENTAL,
        top_up_skipped=False,
        effective_base_amount_usd=1.0,
        cumulative_credit_decline_usd=0.30,
    )
    outcomes = ex._place_broker_orders(
        [signal],
        trading_day,
        _AmbiguousBroker(),
        {"AAPL": outcome},
        remaining_daily=180.0,
        remaining_weekly=900.0,
    )
    assert outcomes["AAPL"]["status"] == "potentially_submitted"
    # Settled exactly once by the except-block -- not ALSO preserve_pending()d
    # by the generic blocked-placement pass afterward.
    assert ex.credit_ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0
    events = ex.credit_ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "settled"
    assert events[-1]["submitted_amount_usd"] == 1.0



def test_seed_ledger_carries_forward_an_already_seeded_potentially_submitted_row(tmp_path):
    """Codex review, PR #1300 discussion_r3920642630 ("Carry ambiguous
    orders through normal retries"): _seed_ledger_from_prior_runs's
    already-seeded branch loads today's OWN scheduled-path submitted/filled
    rows into self._prior_same_day_candidate_rows so a same-day retry
    carries them forward into the artifact it's about to overwrite. Only
    recognizing the literal "submitted"/"filled" strings dropped an
    ambiguous "potentially_submitted" row from that carry-forward, even
    though a normal (non-blocked) retry with no new submission for that
    ticker would otherwise silently rewrite run_state.json/
    submitted_orders.csv without it.
    """
    pf_root = _seed_preflight(tmp_path / "pf", ("AAPL",), trading_date=date(2026, 6, 30))
    cfg = MorningConfig(
        tickers=("AAPL",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "live"),
        preflight_run_state_root=str(pf_root),
        cap_seed_roots=(),
        approved_test_dates=frozenset({date(2026, 6, 30)}),
    )
    ex = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    now = datetime.combine(date(2026, 6, 30), time(10, 30), tzinfo=EASTERN)

    scheduled_dir = ex.run_state_dir(now)
    scheduled_dir.mkdir(parents=True, exist_ok=True)
    (scheduled_dir / "run_state.json").write_text(
        json.dumps(
            {
                "run_id": "already-seeded-run",
                "trading_day": "2026-06-30",
                "buy_candidates": [
                    {
                        "ticker": "AAPL",
                        "status": "potentially_submitted",
                        "order_id": None,
                        "planned_amount_usd": 5.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # Force the already-seeded branch: this run_id's spend is already in
    # self.ledger from an earlier call this same process made, so only the
    # carry-forward loading matters here, not the cap-seeding total.
    ex._seeded_prior_run_ids.add("already-seeded-run")

    ex._seed_ledger_from_prior_runs(now)

    assert len(ex._prior_same_day_candidate_rows) == 1
    assert ex._prior_same_day_candidate_rows[0]["ticker"] == "AAPL"
    assert ex._prior_same_day_candidate_rows[0]["status"] == "potentially_submitted"



def test_startup_failure_counts_a_potentially_submitted_in_flight_order(tmp_path):
    # Codex review, PR #1300 discussion_r3920546920 ("Count ambiguous
    # orders in crash-recovery artifacts"): if an ambiguous placement is
    # recorded and any LATER step raises before the normal artifact write
    # completes, _write_startup_failure_artifacts's own submitted count
    # must still recognize "potentially_submitted" alongside "submitted" --
    # otherwise it retained the row but reported orders_submitted_count=0
    # and not_submitted_error, contradicting the row it just preserved.
    ex = _live_executor(tmp_path, ("AAA",))
    ex._in_flight_broker_outcomes = {
        "AAA": {
            "status": "potentially_submitted",
            "order_id": None,
            "submitted_amount_usd": 5.0,
            "skip_reason": "placement ambiguous: timed out",
        }
    }
    ex._in_flight_signals_by_ticker = {}

    ex._write_startup_failure_artifacts(_now_live(), RuntimeError("later boom"))

    run_dir = ex.run_state_dir(_now_live())
    run_state = json.loads((run_dir / "run_state.json").read_bytes())
    assert run_state["orders_submitted_count"] == 1
    assert run_state["buy_candidates"][0]["status"] == "potentially_submitted"



def test_submitted_keys_counts_potentially_submitted_outcomes():
    # Codex review, PR #1300 discussion_r3920344709 ("Count ambiguous morning
    # placements as in-flight"): _submitted_keys feeds
    # _apply_no_order_classification's orders_submitted_count -- excluding
    # "potentially_submitted" (an ambiguous placement connector failure) let
    # a run whose only outcome is this status be recorded as
    # orders_submitted_count=0/completed_no_orders, contradicting the
    # possibly-live order.
    from morning_dca import _submitted_keys

    outcomes = {"AAA": {"status": "potentially_submitted", "order_id": None}}
    assert _submitted_keys(outcomes, []) == {"ticker:AAA"}

    # Also recognized when carried forward as a buy_candidates row (a
    # same-slot retry's preserved evidence, not this call's own outcomes).
    buy_candidates = [{"ticker": "BBB", "status": "potentially_submitted", "order_id": None}]
    assert _submitted_keys({}, buy_candidates) == {"ticker:BBB"}

    # Codex review, PR #1300 discussion_r3920709301 ("Retain pending orders
    # in retry aggregates"): "pending"/"unconfirmed" (a reconciliation pass
    # can demote "submitted" to either between retries) must be recognized
    # too, not just "potentially_submitted".
    pending_outcomes = {"DDD": {"status": "pending", "order_id": "o-1"}}
    assert _submitted_keys(pending_outcomes, []) == {"o-1"}
    unconfirmed_rows = [{"ticker": "EEE", "status": "unconfirmed", "order_id": None}]
    assert _submitted_keys({}, unconfirmed_rows) == {"ticker:EEE"}





def test_same_stream_order_exists_asks_for_every_entry_points_family(tmp_path):
    """2026-09-07 diagnostic C:F5, executor side: the check must name the
    run_dca.run_slot() engine's graded-dca-{stream}-... and dca_runner's
    dca-runner-{slot}-... ref ids alongside its own {stream}-dca-... pair."""
    from scripts.idempotency import same_stream_ref_id_prefixes

    asked = []

    class _Recorder:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            asked.append(tuple(ref_id_prefixes))
            return {"id": "ORD-1", "ref_id": "graded-dca-morning-2026-07-08-AAPL"}

    found = _same_stream_order_exists(_Recorder(), "AAPL", date(2026, 7, 8), "morning")
    assert found == {"id": "ORD-1", "ref_id": "graded-dca-morning-2026-07-08-AAPL"}
    assert asked == [same_stream_ref_id_prefixes("morning", date(2026, 7, 8), "AAPL")]
    assert "graded-dca-morning-2026-07-08-AAPL" in asked[0]
    assert "dca-runner-1030-2026-07-08-AAPL" in asked[0]
    assert "morning-dca-manual-2026-07-08-AAPL" not in asked[0]
