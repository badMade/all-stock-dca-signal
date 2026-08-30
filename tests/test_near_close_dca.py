"""Self-tests for the graded DCA sizing ladder and slippage/coverage reporting.

Runnable with either ``python test_near_close_dca.py`` or ``pytest``. Boundary
percentages are converted to fractions before calling graded_buy_multiplier, which
expects a fraction of the previous close (2.0% down == 0.02).
"""

import csv
import json
import logging
import os
import pathlib
import tempfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, cast

import pytest

from conftest import FIXTURE_ACCOUNT_NUMBER
from dca_cumulative import CumulativeCreditLedger
from near_close_dca import (
    ACTIVE_TICKERS,
    BASE_BELOW_CLOSE_AND_LOW,
    BASE_BELOW_CLOSE_ONLY,
    BASE_NOT_BELOW_CLOSE,
    BUDGET_TIERS,
    EASTERN,
    EXCLUDED_TICKERS,
    MISSING_FILL_BROKER_BLOCKED,
    MISSING_FILL_BUDGET_CAPPED,
    MISSING_FILL_DATA_ERROR,
    MISSING_FILL_EXECUTION_GAP,
    MISSING_FILL_FAILED_TRIGGER,
    TOP_UP_INCREMENTAL,
    TOP_UP_MISSING_PRIOR_BASE,
    TOP_UP_MISSING_PRIOR_PRICE,
    TOP_UP_NO_PRIOR_RUN,
    TOP_UP_NOT_LOWER_OR_NO_DELTA,
    USAGE_PROXY_FIELDS,
    WHOLE_SHARE_TICKERS,
    Action,
    AlpacaBroker,
    AlpacaConfig,
    Bar,
    BrokerName,
    CompactRunOptions,
    DryRunBroker,
    OrderFill,
    PriorSameDayAllocation,
    ProcessCandidateContext,
    ReconciledDecision,
    RunResult,
    SourceName,
    SourceSignal,
    StrategyConfig,
    StrategyState,
    _check_execution_gates,
    _earlier_selected_base,
    _evaluate_all_signals,
    _graded_dca_ref_id_prefix,
    _inverse_martingale_reconciled_price,
    _iso_or_none,
    _process_buy_candidate,
    _process_buy_candidates_batch,
    _same_stream_order_exists,
    _seed_prior_allocations,
    _select_reconciled_signal_rows,
    account_pinning_failure,
    allocation_sort_key,
    build_broker,
    build_execution_record,
    build_report,
    build_usage_proxy,
    classify_execution_quality,
    classify_missing_fill,
    evaluate_source_signal,
    evaluate_top_up_guard,
    graded_buy_multiplier,
    load_prior_same_day_allocations,
    load_prior_same_day_allocations_multi,
    merge_manual_top_up_context,
    reconcile_signals,
    run_and_report,
    run_daily_compact,
    select_budget_tier,
    select_first_allocation_base_usd,
    within_caps,
    write_run_artifacts,
)
from scripts.run_classification import PriorAllocation

# (drop percent from prior close, expected multiplier).
LADDER_BOUNDARY_CASES = [
    (0.09, 0.0),  # below the 0.1% minimum -> no buy
    (0.1, 1.0),  # 1x tier begins
    (1.999, 1.0),  # still 1x just under 2.0%
    (2.0, 1.5),  # 1.5x tier begins
    (3.999, 1.5),  # still 1.5x just under 4.0%
    (4.0, 2.0),  # 2x tier begins
    (4.999, 2.0),  # still 2x just under 5.0%
    (5.0, 2.5),  # 2.5x tier begins
    (5.999, 2.5),  # still 2.5x just under 6.0%
    (6.0, 3.0),  # 3x tier begins
    (6.499, 3.0),  # still 3x just under 6.5%
    (6.5, 3.5),  # 3.5x tier begins
    (6.999, 3.5),  # still 3.5x just under 7.0%
    (7.0, 4.0),  # 4x tier begins
    (7.499, 4.0),  # still 4x just under 7.5%
    (7.5, 4.5),  # 4.5x tier begins
    (7.999, 4.5),  # still 4.5x just under 8.0%
    (8.0, 5.0),  # 5x tier begins
    (12.0, 5.0),  # no upper cutoff, still 5x
]


def test_ladder_boundaries():
    config = StrategyConfig()
    for drop_pct, expected in LADDER_BOUNDARY_CASES:
        drop_fraction = drop_pct / 100.0
        actual = graded_buy_multiplier(drop_fraction, config)
        assert actual == expected, f"drop {drop_pct}% -> {actual}, expected {expected}"


def test_raw_ladder_multiplier_ignores_the_ml_nudge_and_caps():
    """Regression test for "Preserve the raw ladder tier for selloff-breadth
    counting". raw_ladder_multiplier() must return the ladder's OWN tier
    match, unaffected by the ml-forecaster sizing nudge (or the kill-switch
    cap) that graded_buy_multiplier() applies on top of it -- a 5.0% drop
    is a 2.5x ladder tier regardless of any per-ticker ML factor.
    """
    from near_close_dca import raw_ladder_multiplier

    config = StrategyConfig(ml_sizing_adjustment={"AAPL": 1.2})
    drop_fraction = 5.0 / 100.0  # the 2.5x ladder tier
    assert raw_ladder_multiplier(drop_fraction, config, "AAPL") == 2.5
    # graded_buy_multiplier applies the SAME ticker's ML factor on top --
    # 2.5 * 1.2 = 3.0, crossing the default high_multiplier_threshold (3.0)
    # that raw_ladder_multiplier's own 2.5 never reaches.
    assert graded_buy_multiplier(drop_fraction, config, "AAPL") == pytest.approx(3.0)


def test_base_amount_is_two_dollars():
    assert StrategyConfig().base_buy_amount_usd == 2.0


def test_weekly_rollover_reserve_cap():
    assert StrategyConfig().weekly_rollover_reserve_cap_usd == 900.0


def test_budget_tier_selection():
    # Normal when both planned spend and 3x+ signal count are low.
    normal = select_budget_tier(0.0, 0)
    assert (normal.name, normal.daily_cap_usd, normal.weekly_cap_usd) == (
        "normal",
        180.0,
        900.0,
    )
    assert select_budget_tier(129.0, 5).name == "normal"

    # Either dimension can escalate the tier; the higher wins.
    assert select_budget_tier(130.0, 0).name == "elevated"
    assert select_budget_tier(0.0, 6).name == "elevated"
    assert select_budget_tier(214.0, 0).name == "high_volatility"
    assert select_budget_tier(0.0, 11).name == "high_volatility"
    assert select_budget_tier(273.0, 0).name == "stress"
    assert select_budget_tier(0.0, 18).name == "stress"

    # Highest triggered tier wins across dimensions.
    assert select_budget_tier(130.0, 18).name == "stress"

    stress = BUDGET_TIERS[-1]
    assert (stress.daily_cap_usd, stress.weekly_cap_usd) == (360.0, 1800.0)
    assert stress.uses_rollover_reserve is True


def test_weekly_rollover_reserve_rolls_at_week_boundary():
    config = StrategyConfig()
    state = StrategyState()
    state.selected_tier = BUDGET_TIERS[-1]  # stress tier (uses rollover reserve)

    # Week 1: Monday and Tuesday. Spending within the week must NOT create a
    # rollover reserve for the same week -- the reserve reflects the PRIOR week.
    monday = date(2026, 7, 6)
    tuesday = date(2026, 7, 7)
    state.record_buy("AAA", monday, 100.0)
    state.finalize_daily_budget(monday, config)
    # On Tuesday (same week) the reserve is still 0: Monday's spend did not
    # leak into the current-week rollover.
    state.allowed_spend_today(tuesday, config)
    assert state.weekly_rollover_reserve_usd == 0.0
    state.record_buy("BBB", tuesday, 200.0)
    state.finalize_daily_budget(tuesday, config)

    # Week 2: Monday. Now the reserve rolls forward from week 1's unused Normal
    # capacity: $900 normal cap - $300 spent = $600 (under the $900 reserve cap).
    next_monday = date(2026, 7, 13)
    state.allowed_spend_today(next_monday, config)
    assert state.weekly_rollover_reserve_usd == 600.0
    # And it stays constant for the rest of the week.
    state.record_buy("CCC", next_monday, 50.0)
    state.finalize_daily_budget(next_monday, config)
    state.allowed_spend_today(date(2026, 7, 14), config)
    assert state.weekly_rollover_reserve_usd == 600.0


def test_record_buy_tracks_the_daily_order_count():
    """StrategyState.day_order_count is a plain per-day tally, separate from
    per-ticker buys_today, that record_buy bumps alongside the dollar
    counters -- the mechanism within_caps()'s order-count cap reads."""
    state = StrategyState()
    trading_day = date(2026, 7, 6)
    assert state.day_order_count(trading_day) == 0
    state.record_buy("AAPL", trading_day, 2.0)
    assert state.day_order_count(trading_day) == 1
    # A second, same-ticker top-up buy still consumes a second slot.
    state.record_buy("AAPL", trading_day, 1.0)
    assert state.day_order_count(trading_day) == 2
    state.record_buy("IBM", trading_day, 2.0)
    assert state.day_order_count(trading_day) == 3
    # A different day is tracked independently.
    assert state.day_order_count(date(2026, 7, 7)) == 0


def test_within_caps_enforces_the_daily_order_count_cap():
    """within_caps() is the one gate every scheduled slot (run_dca.run_slot /
    run_pipeline -> near_close_dca) and manual run actually places orders
    through, so config.max_orders_per_day has to be enforced here rather
    than only in dca_runner.plan_orders(), which the live path never calls
    (Codex review: "Apply the order-count cap to every live placement
    path")."""
    trading_day = date(2026, 7, 6)
    config = StrategyConfig(max_orders_per_day=2)
    state = StrategyState()
    # Below the cap: two orders fit.
    assert within_caps(1.0, state, trading_day, config) is True
    state.record_buy("AAPL", trading_day, 1.0)
    assert within_caps(1.0, state, trading_day, config) is True
    state.record_buy("IBM", trading_day, 1.0)
    # The cap is now reached; a third order must be refused even though
    # dollar headroom remains.
    assert within_caps(1.0, state, trading_day, config) is False


def test_within_caps_is_uncapped_by_default():
    """A directly constructed StrategyConfig (tests, demo mode) has
    max_orders_per_day=None, matching pre-cap behavior exactly."""
    trading_day = date(2026, 7, 6)
    config = StrategyConfig()
    state = StrategyState()
    for _ in range(50):
        state.record_buy("AAPL", trading_day, 0.01)
    assert within_caps(0.01, state, trading_day, config) is True


def test_ticker_universe():
    assert "IBM" in ACTIVE_TICKERS
    assert "TXN" in ACTIVE_TICKERS
    assert "RKLB" in ACTIVE_TICKERS
    # Added 2026-08-04. Whitelisted without a broker instrument search, so the
    # only in-repo guarantee is membership; run-time preflight gates the rest.
    assert "VT" in ACTIVE_TICKERS
    assert "GFUZ" in ACTIVE_TICKERS
    assert "IRDM" in ACTIVE_TICKERS
    assert "GLW" in ACTIVE_TICKERS
    assert "IRDMI" not in ACTIVE_TICKERS
    assert "GLWR" not in ACTIVE_TICKERS
    # Nothing is categorically excluded (no buying mechanism at all) today.
    assert EXCLUDED_TICKERS == frozenset()
    # BYDDY is not tradable for fractional/notional buys on Robinhood, so it
    # lives in its own WHOLE_SHARE_TICKERS category (see
    # config/tickers.json's exclusion_reasons/whole_share and
    # docs/whole_share_accumulation.md) instead of ACTIVE_TICKERS or
    # EXCLUDED_TICKERS.
    # RVII joined BYDDY in that category 2026-08-24 (user-requested): it is
    # likewise not purchasable fractionally, so it is still ladder-evaluated
    # and still bought, just through the whole-share mechanism.
    assert WHOLE_SHARE_TICKERS == frozenset({"BYDDY", "RVII"})
    for whole_share in ("BYDDY", "RVII"):
        assert whole_share not in ACTIVE_TICKERS
        assert whole_share not in EXCLUDED_TICKERS
    # RVI, VCX, and TRLV were excluded 2026-08-05, then re-added 2026-08-16 at
    # the owner's explicit request (see config/tickers.json notes).
    for reinstated in ("RVI", "VCX", "TRLV"):
        assert reinstated in ACTIVE_TICKERS
        assert reinstated not in EXCLUDED_TICKERS
    assert "QBUT" in ACTIVE_TICKERS


# --- Target price selection --------------------------------------------------


class _FakeData:
    """Minimal provider producing a controlled drop and an optional signal quote."""

    def __init__(self, source, close_price, prev_close, quote=None):
        self.source = source
        self._close = close_price
        self._prev = prev_close
        self._quote = quote

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(
            as_of.date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        return Bar(ts, self._prev, self._prev, self._prev, self._prev, 1000)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        # Declining series: bars in the last 30 minutes settle at the decision close,
        # earlier bars sit higher so the 60-minute trend reads as down.
        bars = []
        cursor = start
        late_threshold = end - timedelta(minutes=30)
        while cursor <= end:
            price = self._close if cursor >= late_threshold else self._close + 2.0
            bars.append(Bar(cursor, price, price, price, price, 100))
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_quote(self, ticker, as_of):
        return self._quote  # (price, time) or None


class _FakeDataWithBidAsk(_FakeData):
    def __init__(
        self, source, close_price, prev_close, quote=None, bid_ask=(97.0, 99.0)
    ):
        super().__init__(source, close_price, prev_close, quote=quote)
        self._bid_ask = bid_ask

    def get_bid_ask(self, ticker, as_of):
        return self._bid_ask


class _FakeDataWithRaisingBidAsk(_FakeData):
    """Simulates a provider whose get_bid_ask raises (e.g. inactive/halted quote or missing fields)."""

    def get_bid_ask(self, ticker, as_of):
        raise ValueError(f"no usable bid/ask in {ticker} quote")


def test_target_price_defaults_to_signal_quote():
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    quote_time = datetime(2026, 6, 29, 15, 44, tzinfo=EASTERN)
    data = _FakeData(
        SourceName.FINANCE, close_price=98.0, prev_close=100.0, quote=(97.5, quote_time)
    )
    signal = evaluate_source_signal(
        "AAPL", now, StrategyConfig(), data, interval_minutes=15
    )
    # target (quote) is distinct from the completed decision-bar close.
    assert signal.quote_price == 97.5
    assert signal.decision_bar_close == 98.0
    assert signal.quote_time == quote_time


def test_target_price_falls_back_to_bar_close_without_quote():
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeData(SourceName.FINANCE, close_price=98.0, prev_close=100.0, quote=None)
    signal = evaluate_source_signal(
        "AAPL", now, StrategyConfig(), data, interval_minutes=15
    )
    assert signal.quote_price == 98.0
    assert signal.decision_bar_close == 98.0


class _FakeDataWithLow(_FakeData):
    """Provider whose previous regular-session bar carries a distinct low.

    The base ``_FakeData`` sets the prior bar's low equal to the prior close, so it
    cannot distinguish "below prior close only" from "below prior close and low". This
    variant lets a test place the decision price on either side of the prior low.
    """

    def __init__(self, source, close_price, prev_close, prev_low, quote=None):
        super().__init__(source, close_price, prev_close, quote=quote)
        self._prev_low = prev_low

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(
            as_of.date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        # open/high sit at/above the close; low is the distinct prior-session low.
        return Bar(ts, self._prev, self._prev, self._prev_low, self._prev, 1000)


def test_first_allocation_below_close_only_uses_one_dollar_base():
    # Decision price below the prior close (100) but not below the prior low (95)
    # -> $1 base scale. Drop is 2% -> 1.5x multiplier -> $1 * 1.5 = $1.50 suggested.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(
        SourceName.FINANCE, close_price=98.0, prev_close=100.0, prev_low=95.0
    )
    signal = evaluate_source_signal(
        "AAPL", now, StrategyConfig(), data, interval_minutes=15
    )
    assert signal.action == Action.BUY
    assert signal.previous_low == 95.0
    assert signal.base_selection_reason == BASE_BELOW_CLOSE_ONLY
    assert signal.selected_base_amount_usd == 1.0
    assert signal.buy_multiplier == 1.5
    assert signal.suggested_amount_usd == 1.0 * 1.5


def test_first_allocation_below_close_and_low_uses_two_dollar_base():
    # Decision price below both the prior close (100) and the prior low (95)
    # -> $2 base scale. Drop is 6% -> 3x multiplier -> $2 * 3 = $6 suggested.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(
        SourceName.FINANCE, close_price=94.0, prev_close=100.0, prev_low=95.0
    )
    signal = evaluate_source_signal(
        "AAPL", now, StrategyConfig(), data, interval_minutes=15
    )
    assert signal.action == Action.BUY
    assert signal.previous_low == 95.0
    assert signal.base_selection_reason == BASE_BELOW_CLOSE_AND_LOW
    assert signal.selected_base_amount_usd == 2.0
    assert signal.buy_multiplier == 3.0
    assert signal.suggested_amount_usd == 2.0 * 3.0


def test_calibrated_first_tier_overrides_global_minimum_drop():
    # Codex review ("Let calibrated first tiers override the global floor"):
    # AAAU-style calibration with a first tier below the shared 0.1% minimum
    # (config/per_ticker_sizing.json). A 0.05% drop clears the calibrated
    # 0.01% first tier (buy_multiplier=1x) but is still below the global
    # 0.1% floor -- flooring it at the global minimum would silently veto a
    # buy the checked-in per-symbol model already approved.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        per_ticker_sizing={"AAAU": {"tiers": ((0.0001, 1.0), (0.02, 1.5))}}
    )
    data = _FakeData(SourceName.FINANCE, close_price=99.95, prev_close=100.0)
    signal = evaluate_source_signal("AAAU", now, config, data, interval_minutes=15)
    assert signal.action == Action.BUY
    assert signal.buy_multiplier == 1.0


def test_uncalibrated_ticker_still_floored_at_global_minimum_drop():
    # Control: the SAME 0.05% drop for an uncalibrated symbol (no
    # per_ticker_sizing entry) must still be blocked by the global 0.1%
    # minimum -- only an explicit calibration overrides the floor.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        per_ticker_sizing={"AAAU": {"tiers": ((0.0001, 1.0), (0.02, 1.5))}}
    )
    data = _FakeData(SourceName.FINANCE, close_price=99.95, prev_close=100.0)
    signal = evaluate_source_signal("AAPL", now, config, data, interval_minutes=15)
    assert signal.action == Action.NO_ACTION
    assert signal.reason == "below prior close but below 0.1% minimum drop"


def test_first_allocation_above_close_is_no_buy():
    # Decision price above the prior close -> no buy, $0 base, zeroed amount.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(
        SourceName.FINANCE, close_price=101.0, prev_close=100.0, prev_low=95.0
    )
    signal = evaluate_source_signal(
        "AAPL", now, StrategyConfig(), data, interval_minutes=15
    )
    assert signal.action != Action.BUY
    assert signal.selected_base_amount_usd == 0.0
    assert signal.suggested_amount_usd == 0.0


def test_first_allocation_missing_prior_low_treated_as_below_close_only():
    # A non-positive/missing prior low is treated conservatively as not-below-low,
    # so the $1 (below-close-only) scale is selected even on a deep drop.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(
        SourceName.FINANCE, close_price=94.0, prev_close=100.0, prev_low=0.0
    )
    signal = evaluate_source_signal(
        "AAPL", now, StrategyConfig(), data, interval_minutes=15
    )
    assert signal.action == Action.BUY
    assert signal.base_selection_reason == BASE_BELOW_CLOSE_ONLY
    assert signal.selected_base_amount_usd == 1.0


def test_first_allocation_zero_multiplier_normalizes_reason_to_no_buy():
    # Canonical rule item 6: "not lower than close, OR multiplier is 0" -> no buy,
    # reason not_below_prior_close_no_buy. Decision price IS below the prior close
    # (100) and not below the prior low (95), so select_first_allocation_base_usd
    # alone would label this below_prior_close_only -- but the drop (0.05%) is
    # below the first ladder tier's 0.1% threshold, so buy_multiplier is 0 and the
    # ticker must not carry a first-buy sizing reason for what the rule says was a
    # no-buy.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(
        SourceName.FINANCE, close_price=99.95, prev_close=100.0, prev_low=95.0
    )
    signal = evaluate_source_signal(
        "AAPL", now, StrategyConfig(), data, interval_minutes=15
    )
    assert signal.action != Action.BUY
    assert signal.buy_multiplier == 0.0
    assert signal.base_selection_reason == BASE_NOT_BELOW_CLOSE
    assert signal.selected_base_amount_usd == 0.0
    assert signal.suggested_amount_usd == 0.0


def test_select_first_allocation_base_helper_branches():
    config = StrategyConfig()
    assert select_first_allocation_base_usd(94.0, 100.0, 95.0, config) == (
        2.0,
        BASE_BELOW_CLOSE_AND_LOW,
    )
    assert select_first_allocation_base_usd(98.0, 100.0, 95.0, config) == (
        1.0,
        BASE_BELOW_CLOSE_ONLY,
    )
    assert select_first_allocation_base_usd(101.0, 100.0, 95.0, config) == (
        0.0,
        BASE_NOT_BELOW_CLOSE,
    )
    assert select_first_allocation_base_usd(100.0, 100.0, 95.0, config) == (
        0.0,
        BASE_NOT_BELOW_CLOSE,
    )
    # Missing prior low -> conservative below-close-only ($1).
    assert select_first_allocation_base_usd(94.0, 100.0, None, config) == (
        1.0,
        BASE_BELOW_CLOSE_ONLY,
    )
    # Per-order ceiling holds: deepest drop (5x) on the $2 scale tops out at $10.
    assert 2.0 * 5.0 == config.max_per_order_usd


def test_first_buy_ticker_uses_close_and_low_even_when_other_tickers_have_prior_same_day_buys():
    # Canonical rule items 1-2, 7-8: the per-ticker first-buy sizing is independent.
    # AAA has no prior same-day buy, so it must still be sized as a first buy
    # against yesterday's close (100) and low (95) regardless of any other
    # ticker's same-day activity.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig()

    aaa_data = _FakeDataWithLow(
        SourceName.FINANCE, close_price=94.0, prev_close=100.0, prev_low=95.0
    )
    aaa_signal = evaluate_source_signal(
        "AAA", now, config, aaa_data, interval_minutes=15
    )
    assert aaa_signal.action == Action.BUY
    assert aaa_signal.base_selection_reason == BASE_BELOW_CLOSE_AND_LOW
    assert aaa_signal.selected_base_amount_usd == 2.0


def test_run_daily_compact_prior_allocation_scoped_to_its_own_ticker(tmp_path):
    # Canonical rule items 7-8: same-day buys in one ticker must never suppress
    # first-allocation evaluation for another ticker. Exercised at the real
    # orchestration entrypoint (run_daily_compact), not evaluate_top_up_guard in
    # isolation, with a prior-allocation mapping that actually contains AAPL's
    # context alongside NVDA/MSFT -- proving the scheduled runner itself never
    # lets AAPL's presence in that mapping gate NVDA/MSFT.
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-ticker-scope")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=3.0,
            reference_price=99.0,
            run_id="am",
            source="manual",
            selected_base_amount_usd=2.0,
        ),
    }

    result = run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    assert result["blocked"] is False
    with open(cast(Any, result["run_state_path"]), encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}

    # AAPL has its own prior ($3 at ref 99.0); the afternoon price (98.0) is
    # below that reference, so it tops up only the incremental difference.
    assert by_ticker["AAPL"]["top_up_guard_result"] == TOP_UP_INCREMENTAL
    assert by_ticker["AAPL"]["incremental_order_amount"] == 1.0

    # NVDA and MSFT have no prior allocation of their own -- AAPL's presence in
    # the SAME prior_same_day_allocations mapping must never suppress their
    # first allocation, which must be the full suggested amount.
    for ticker in ("NVDA", "MSFT"):
        assert by_ticker[ticker]["top_up_guard_result"] == TOP_UP_NO_PRIOR_RUN
        assert by_ticker[ticker]["incremental_order_amount"] == 3.0


def test_budget_tier_is_selected_from_post_topup_spend_not_pre_topup_desired(tmp_path):
    """Tier selection must reflect what the batch will actually try to spend,
    not each candidate's full first-allocation desired amount.

    A single ticker with a $150 desired amount (5x the $30 base) but a $140
    same-day prior allocation only needs a $10 incremental top-up. Selecting
    the tier from the pre-topup $150 would pick Elevated ($240/$1,200,
    TIER_SPEND_THRESHOLDS_USD's first boundary is $129); selecting it from the
    actionable $10 correctly keeps Normal ($180/$900) -- authorizing real
    daily-cap headroom the actionable plan never needed is exactly the
    excess-spend risk this pins against (Codex review: "Select the tier
    after applying top-up deltas").
    """
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-tier-post-topup")
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        base_buy_amount_usd=30.0,
        base_buy_amount_below_close_usd=30.0,
        max_per_order_usd=200.0,
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 90.0})
    # 10% drop vs. the prior close -> the ladder's 5x tier -> $30 base * 5 = $150 desired.
    fin = _FakeData(SourceName.FINANCE, 90.0, 100.0, quote=(90.0, now))
    rh = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 90.0, 100.0, quote=(90.0, now), bid_ask=(89.9, 90.1)
    )
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=140.0,
            reference_price=95.0,  # above today's ~90.0 decision price -> qualifies for top-up
            run_id="am",
            source="scheduled",
            selected_base_amount_usd=30.0,
        ),
    }

    result = run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir,
            prior_same_day_allocations=prior_same_day_allocations,
        ),
    )

    assert result["blocked"] is False
    with open(cast(Any, result["run_state_path"]), encoding="utf-8") as handle:
        run_state = json.load(handle)
    [candidate] = run_state["buy_candidates"]
    assert candidate["top_up_guard_result"] == TOP_UP_INCREMENTAL
    assert candidate["incremental_order_amount"] == pytest.approx(10.0, abs=0.01)
    assert run_state["budget"]["budget_tier"] == "normal"
    assert run_state["budget"]["daily_cap_usd"] == pytest.approx(180.0)


def test_reconciled_decision_carries_target_fields():
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    qt = datetime(2026, 6, 29, 15, 44, tzinfo=EASTERN)
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(97.5, qt))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(97.6, qt))
    fs = evaluate_source_signal("AAPL", now, StrategyConfig(), fin, 15)
    rs = evaluate_source_signal("AAPL", now, StrategyConfig(), rh, 5)
    decision = reconcile_signals("AAPL", fs, rs)
    assert decision.action == Action.BUY
    # finance drives the target when both agree.
    assert decision.target_price == 97.5
    assert decision.target_price_source == "finance:signal_quote"
    assert decision.decision_bar_close == 98.0


def test_merge_manual_top_up_context_poisons_base_to_unknown():
    # Codex review ("Keep the base unknown after merging an earlier manual
    # fill"): PriorAllocation (from a bare-CSV-only manual fill) carries no
    # timestamp, so merging it with an existing allocation that already
    # resolved a base cannot establish whether the manual fill actually came
    # first -- the merged base must be poisoned to unknown rather than
    # silently keep the existing (possibly later) base.
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=97.0,
            run_id="run1",
            source="scheduled",
            selected_base_amount_usd=2.0,
            selected_base_amount_usd_at="2026-06-29T10:30:00-04:00",
        ),
    }
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL", prior_usd=1.0, reference_price=99.0, sources=["manual_csv"]
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    merged = prior_same_day_allocations["AAPL"]
    assert merged.amount_usd == 3.0
    assert merged.selected_base_amount_usd is None
    assert merged.selected_base_amount_usd_at is None


def test_merge_manual_top_up_context_new_allocation_uses_attempt_count_not_sources():
    """Codex review: "Count each fallback CSV order as an attempt". A single
    bare manual/recovery CSV can contain multiple distinct orders for the
    same ticker -- PriorAllocation.sources records only ONE entry per
    contributing RUN, not per order, so len(sources) undercounted whenever a
    fallback-only run placed more than one real order for a ticker.
    attempt_count is populated per-row by scripts.run_classification's
    _prior_from_rows/_merge_prior and must be used directly instead."""
    prior_same_day_allocations = {}
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL",
            prior_usd=5.0,
            reference_price=99.0,
            sources=["manual_csv"],
            attempt_count=3,
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    assert prior_same_day_allocations["AAPL"].attempt_count == 3


def test_merge_manual_top_up_context_merge_path_sums_attempt_count_not_sources():
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=97.0,
            run_id="run1",
            source="scheduled",
            attempt_count=1,
        ),
    }
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL",
            prior_usd=5.0,
            reference_price=99.0,
            sources=["manual_csv"],
            attempt_count=3,
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    assert prior_same_day_allocations["AAPL"].attempt_count == 4


def test_merge_manual_top_up_context_retains_base_when_manual_fill_is_later():
    # Codex review discussion_r3653976740 ("Preserve the known base when a
    # later manual fill is merged"): an earlier scheduled allocation resolved
    # a $1 base (below close only) at 10:30. A LATER (14:00) bare-CSV manual
    # fill, whose PriorAllocation.reference_ts now identifies its own time,
    # must not erase that known base -- it cannot itself be the true first
    # allocation since it happened after the base was already established.
    # If the base is poisoned to None, a
    # subsequent lower-price run would recompute its own (possibly $2) base,
    # doubling the full-day target before subtracting prior spend.
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=1.0,
            reference_price=97.0,
            run_id="run1",
            source="scheduled",
            selected_base_amount_usd=1.0,
            selected_base_amount_usd_at="2026-06-29T10:30:00-04:00",
        ),
    }
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL",
            prior_usd=1.0,
            reference_price=99.0,
            sources=["manual_csv"],
            reference_ts=14 * 3600,
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    merged = prior_same_day_allocations["AAPL"]
    assert merged.amount_usd == 2.0
    assert merged.selected_base_amount_usd == 1.0
    assert merged.selected_base_amount_usd_at == "2026-06-29T10:30:00-04:00"


def test_merge_manual_top_up_context_poisons_base_when_manual_fill_may_be_earlier():
    # The inverse of the above: a bare-CSV manual fill at 09:00, EARLIER than
    # the existing allocation's base-establishing run at 10:30, could itself
    # be the true first same-day allocation -- its own base is unknown from a
    # bare CSV, so the merged base must still be poisoned rather than keep
    # `existing`'s (possibly wrong, non-first) base.
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=1.0,
            reference_price=97.0,
            run_id="run1",
            source="scheduled",
            selected_base_amount_usd=1.0,
            selected_base_amount_usd_at="2026-06-29T10:30:00-04:00",
        ),
    }
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL",
            prior_usd=1.0,
            reference_price=99.0,
            sources=["manual_csv"],
            reference_ts=9 * 3600,
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    merged = prior_same_day_allocations["AAPL"]
    assert merged.selected_base_amount_usd is None
    assert merged.selected_base_amount_usd_at is None


def test_merge_manual_top_up_context_uses_most_recent_reference_not_merge_order():
    # Codex review ("Preserve the manual-context timestamp when merging
    # references"): a bare-CSV manual fill merged last must not automatically
    # win the reference price merely by being merged last. Here the core
    # loader's 12:30 scheduled fill ($90) is more recent than the manual
    # loader's 09:00 fill ($100), so the merged reference must be the more
    # recent $90 -- a 15:20 decision price of $95 must be blocked against $90,
    # not incorrectly permitted against a stale $100. The winning (existing)
    # side's own identity must travel with it too.
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=90.0,
            run_id="run1",
            source="scheduled",
            reference_ts=datetime(2026, 6, 29, 12, 30, tzinfo=EASTERN),
        ),
    }
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL",
            prior_usd=1.0,
            reference_price=100.0,
            sources=["manual_csv"],
            reference_ts=9 * 3600,
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    merged = prior_same_day_allocations["AAPL"]
    assert merged.reference_price == 90.0
    assert merged.run_id == "run1"
    assert merged.source == "scheduled"


def test_merge_manual_top_up_context_manual_wins_when_actually_more_recent():
    # The inverse of the above: a manual fill genuinely later in the day
    # (15:05) than the core loader's own reference (10:30) must win, and its
    # OWN identity must travel with the winning price -- pairing a manual
    # reference with the scheduled run's run_id/source would point
    # reconciliation at the wrong artifact (Codex review: "Keep the winning
    # manual reference's identity").
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=95.0,
            run_id="run1",
            source="scheduled",
            reference_ts=datetime(2026, 6, 29, 10, 30, tzinfo=EASTERN),
        ),
    }
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL",
            prior_usd=1.0,
            reference_price=93.0,
            sources=["manual_csv"],
            reference_ts=15 * 3600 + 5 * 60,
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    merged = prior_same_day_allocations["AAPL"]
    assert merged.reference_price == 93.0
    assert merged.run_id == "manual_csv"
    assert merged.source == "manual_top_up_context"


def test_merge_manual_top_up_context_sub_minute_precision():
    # Codex review ("Retain sub-minute generated-at ordering"): two artifacts
    # in the same ET minute at different seconds must not tie. The core
    # loader's 12:30:45 fill is more recent than the manual loader's
    # 12:30:05 fill despite sharing the same minute, so the merged reference
    # must be the core loader's, not fall through to merge-order.
    prior_same_day_allocations = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=90.0,
            run_id="run1",
            source="scheduled",
            reference_ts=datetime(2026, 6, 29, 12, 30, 45, tzinfo=EASTERN),
        ),
    }
    manual_context = {
        "AAPL": PriorAllocation(
            ticker="AAPL",
            prior_usd=1.0,
            reference_price=100.0,
            sources=["manual_csv"],
            reference_ts=12 * 3600 + 30 * 60 + 5,
        ),
    }
    merge_manual_top_up_context(prior_same_day_allocations, cast(Any, manual_context))
    merged = prior_same_day_allocations["AAPL"]
    assert merged.reference_price == 90.0


def test_reconcile_signals_agree_preserves_base_from_winning_signal():
    # Codex review ("Preserve the base that produced the reconciled amount"): when
    # both sources agree on BUY but disagree on both base and multiplier, the
    # reconciled base must come from whichever signal actually produced the
    # (min) amount -- not an independent min() over the two bases, which could
    # pick a base from the signal that did NOT determine the amount.
    finance = SourceSignal(
        ticker="AAPL",
        source=SourceName.FINANCE,
        action=Action.BUY,
        reason="test",
        buy_multiplier=1.0,
        suggested_amount_usd=2.0,  # $2 base * 1x
        selected_base_amount_usd=2.0,
    )
    robinhood = SourceSignal(
        ticker="AAPL",
        source=SourceName.ROBINHOOD,
        action=Action.BUY,
        reason="test",
        buy_multiplier=2.5,
        suggested_amount_usd=2.5,  # $1 base * 2.5x
        selected_base_amount_usd=1.0,
    )
    decision = reconcile_signals("AAPL", finance, robinhood)
    assert decision.suggested_amount_usd == 2.0  # finance's smaller amount wins
    assert (
        decision.selected_base_amount_usd == 2.0
    )  # finance's base, not Robinhood's $1


def test_reconcile_signals_agree_pairs_multiplier_with_winning_signal():
    # Codex review ("Keep the winning amount's multiplier paired with its
    # base"): the inverse-ordering case where the smaller amount comes from the
    # signal with the LARGER multiplier. Finance $2*2.5x=$5, Robinhood
    # $1*3.5x=$3.50 -> amount is Robinhood's $3.50, so both its base ($1) AND
    # its multiplier (3.5x) must be used together, not finance's mismatched 2.5x.
    finance = SourceSignal(
        ticker="AAPL",
        source=SourceName.FINANCE,
        action=Action.BUY,
        reason="test",
        buy_multiplier=2.5,
        suggested_amount_usd=5.0,
        selected_base_amount_usd=2.0,
    )
    robinhood = SourceSignal(
        ticker="AAPL",
        source=SourceName.ROBINHOOD,
        action=Action.BUY,
        reason="test",
        buy_multiplier=3.5,
        suggested_amount_usd=3.5,
        selected_base_amount_usd=1.0,
    )
    decision = reconcile_signals("AAPL", finance, robinhood)
    assert decision.suggested_amount_usd == 3.5
    assert decision.selected_base_amount_usd == 1.0
    assert decision.buy_multiplier == 3.5  # Robinhood's, paired with its own $1 base
    assert (
        decision.selected_base_amount_usd * decision.buy_multiplier
        == decision.suggested_amount_usd
    )


def _im_signal(source, *, decision_price=100.0, previous_close=100.0):
    return SourceSignal(
        ticker="AAPL",
        source=source,
        action=Action.NO_ACTION,
        reason="test",
        decision_price=decision_price,
        previous_close=previous_close,
    )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_inverse_martingale_reconciled_price_rejects_non_finite_finance_decision_price(bad_value):
    """Regression test for "Reject non-finite reconciled prices".

    Every comparison against NaN is false, and "inf > tolerance * inf" is
    also false -- without an explicit finiteness check, a non-finite
    decision_price from either source would sail past both the positivity
    check and the cross-source tolerance check below it. An infinite
    decision_price in particular would make drop_metric go to -inf in
    evaluate_inverse_martingale_step, trivially satisfying ANY active
    cycle's recovery check and firing a real synthetic order.
    """
    finance = _im_signal(SourceName.FINANCE, decision_price=bad_value)
    robinhood = _im_signal(SourceName.ROBINHOOD, decision_price=100.0)
    decision = ReconciledDecision(
        ticker="AAPL", action=Action.NO_ACTION, reason="test",
        finance_signal=finance, robinhood_signal=robinhood,
    )
    assert _inverse_martingale_reconciled_price(decision) is None


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_inverse_martingale_reconciled_price_rejects_non_finite_previous_close(bad_value):
    finance = _im_signal(SourceName.FINANCE, previous_close=bad_value)
    robinhood = _im_signal(SourceName.ROBINHOOD, previous_close=100.0)
    decision = ReconciledDecision(
        ticker="AAPL", action=Action.NO_ACTION, reason="test",
        finance_signal=finance, robinhood_signal=robinhood,
    )
    assert _inverse_martingale_reconciled_price(decision) is None


def test_inverse_martingale_reconciled_price_accepts_agreeing_finite_signals():
    finance = _im_signal(SourceName.FINANCE, decision_price=99.0, previous_close=100.0)
    robinhood = _im_signal(SourceName.ROBINHOOD, decision_price=99.1, previous_close=100.0)
    decision = ReconciledDecision(
        ticker="AAPL", action=Action.NO_ACTION, reason="test",
        finance_signal=finance, robinhood_signal=robinhood,
    )
    result = _inverse_martingale_reconciled_price(decision)
    assert result is not None
    decision_price, previous_close = result
    assert decision_price == pytest.approx(99.0)
    assert previous_close == pytest.approx(100.0)


def test_earlier_selected_base_picks_by_timestamp_not_argument_order():
    # Codex review ("Select the base from the chronologically first allocation"):
    # the later (15:05) allocation is passed as `existing` (the position that
    # previously always won); the earlier (09:45) allocation must still win.
    later_as_existing = _earlier_selected_base(
        2.0,
        "2026-06-29T15:05:00-04:00",  # existing: later, would previously win
        1.0,
        "2026-06-29T09:45:00-04:00",  # alloc: earlier
    )
    assert later_as_existing == (1.0, "2026-06-29T09:45:00-04:00")
    # Same two allocations, swapped argument positions -> same (earlier) answer.
    earlier_as_existing = _earlier_selected_base(
        1.0,
        "2026-06-29T09:45:00-04:00",
        2.0,
        "2026-06-29T15:05:00-04:00",
    )
    assert earlier_as_existing == (1.0, "2026-06-29T09:45:00-04:00")
    # Missing/unparseable timestamps fall back to preferring `existing` (the
    # pre-chronological-comparison default) rather than guessing at ordering.
    assert _earlier_selected_base(2.0, None, 1.0, "2026-06-29T09:45:00-04:00") == (
        2.0,
        None,
    )
    assert _earlier_selected_base(None, None, 1.0, "2026-06-29T09:45:00-04:00") == (
        1.0,
        "2026-06-29T09:45:00-04:00",
    )


def test_kill_switch_caps_order_at_candidates_own_selected_base():
    # Codex review ("Cap the kill switch at the selected base"): a below-close-
    # only candidate's own selected base is $1, not the fixed
    # config.base_buy_amount_usd ($2). Once the kill switch engages, capping at
    # the fixed $2 would let this candidate submit double its 1x risk-capped
    # amount.
    config = StrategyConfig(monthly_cash_allocation_usd=10.0)
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=5.0,
        suggested_amount_usd=5.0,  # $1 base * 5x, not yet kill-switch-capped
        selected_base_amount_usd=1.0,
        target_price=98.0,
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    candidate_outcome, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=date(2026, 6, 29),
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=False,
            run_result=None,
        ),
        running_deployed_7d=100.0,
    )
    assert candidate_outcome["submitted_amount_usd"] == 1.0  # capped at $1, not $2


def test_kill_switch_caps_guarded_top_up_at_preserved_base():
    # Codex review ("Cap guarded top-ups using the preserved base"): an
    # earlier same-day allocation fixed AAPL's base at $1 (below close only).
    # A deeper afternoon price now crosses the prior low too, so THIS run's
    # own selected_base_amount_usd recomputes to $2 -- but the top-up guard
    # must still use the $1 preserved base, and so must the kill-switch cap.
    # If the cap uses decision.selected_base_amount_usd ($2)
    # instead, a 5x signal's $4 guard delta gets min'd against $2 instead of
    # the correct $1 ceiling, letting a guarded top-up submit double the
    # reduced-scale risk the kill switch is meant to enforce for this ticker.
    config = StrategyConfig(monthly_cash_allocation_usd=10.0)
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=5.0,
        suggested_amount_usd=10.0,  # this run's own $2 base * 5x
        selected_base_amount_usd=2.0,
        target_price=98.0,
    )
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=date(2026, 6, 29),
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
        ),
        running_deployed_7d=100.0,
    )
    # The guard's own $4 delta (($1 * 5x) - $1 already bought) must be capped
    # at the preserved $1 base, not this run's fresh $2 base.
    assert top_up_outcome is not None
    assert top_up_outcome.incremental_order_amount == 4.0
    assert candidate_outcome["submitted_amount_usd"] == 1.0


class _RefIdTrackingBroker:
    """Broker double that remembers placed ref_ids, so the same-stream duplicate
    check (same_day_strategy_order_exists_with_ref_id_prefix) reflects real prior
    placements instead of a fixture that always returns nothing.
    """

    def __init__(self, cash: float = 1000.0):
        self._cash = cash
        self.placed: dict = {}  # ticker -> notional (last placement)
        self._orders: list = []  # (ticker, ref_id, notional, order_id)
        self._fills: dict = {}  # order_id -> OrderFill

    def get_order_fill(self, order_id):
        return self._fills.get(order_id)

    def account_number(self):
        return FIXTURE_ACCOUNT_NUMBER

    def available_buying_power(self):
        return self._cash

    def same_day_strategy_order_exists(self, ticker, trading_day):
        return any(t == ticker for t, _rid, _amt, _oid in self._orders)

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker, trading_day, ref_id_prefixes
    ):
        for t, rid, amt, order_id in self._orders:
            if t == ticker and rid in ref_id_prefixes:
                return {"id": order_id, "dollar_amount": amt}
        return None

    supports_concurrent_fill_lookup: bool = False

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id):
        for t, rid, amt, order_id in self._orders:
            if t == ticker and rid == ref_id:
                return {"id": order_id, "dollar_amount": amt}
        return None

    def same_day_ticker_notional_usd(self, ticker, trading_day):
        return sum(amt for t, _rid, amt, _oid in self._orders if t == ticker)

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        order_id = f"ORDER-{ticker}-{len(self._orders) + 1}"
        self.placed[ticker] = notional_usd
        self._orders.append((ticker, ref_id, notional_usd, order_id))
        return order_id

    def same_day_order_with_ref_id_prefix_any_state(
        self, ticker, trading_day, ref_id_prefixes
    ):
        return self.same_day_strategy_order_exists_with_ref_id_prefix(
            ticker, trading_day, ref_id_prefixes
        )

    def is_tradable_for_whole_share_buy(self, ticker):
        return True

    def review_whole_share_buy(self, ticker, quantity):
        return True, "ok"

    def place_whole_share_buy(self, ticker, quantity, ref_id):
        order_id = f"ORDER-WS-{ticker}-{len(self._orders) + 1}"
        self._orders.append((ticker, ref_id, quantity, order_id))
        return order_id

    def get_raw_placement_responses(self):
        return []


def _graded_decision(
    ticker="AAPL",
    buy_multiplier=5.0,
    suggested_amount_usd=10.0,
    selected_base_amount_usd=2.0,
    target_price=98.0,
):
    return ReconciledDecision(
        ticker=ticker,
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=buy_multiplier,
        suggested_amount_usd=suggested_amount_usd,
        selected_base_amount_usd=selected_base_amount_usd,
        target_price=target_price,
    )


def test_guarded_top_up_retry_does_not_place_a_duplicate_order():
    # A retry of the SCHEDULED afternoon run for a ticker with an earlier
    # same-day (morning/midday) allocation must detect ITS OWN already-placed
    # top-up order rather than recompute and place a second one. Before the
    # fix, ticker_guarded candidates skipped the broker's same-day-order check
    # entirely (with no narrower same-stream check to replace it), so a retry
    # would double-buy.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    # Attempt 1: places the guarded top-up for real.
    state1 = StrategyState()
    outcome1, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state1,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome1["status"] == "submitted"
    assert broker.placed["AAPL"] > 0
    placed_amount = broker.placed["AAPL"]
    assert len(broker._orders) == 1

    # Give the broker a real fill for the already-placed order, as a live
    # broker would report on a retry that queries it fresh.
    placed_order_id = broker._orders[0][3]
    broker._fills[placed_order_id] = OrderFill(
        avg_fill_price=98.0,
        quantity_filled=placed_amount / 98.0,
        order_submitted_at=now,
        order_filled_at=now,
    )

    # Attempt 2 (a retry with a fresh, unseeded StrategyState -- exactly what a
    # fresh process restart looks like -- and the SAME prior_same_day_allocations,
    # since load_prior_same_day_allocations deliberately excludes this stream's
    # own earlier same-day runs): must find the same-stream order already on the
    # broker and must NOT place a second one.
    state2 = StrategyState()
    run_result = RunResult(
        trading_day=trading_day, decisions=[decision], active_universe=["AAPL"]
    )
    outcome2, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state2,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=run_result,
        ),
        running_deployed_7d=0.0,
    )
    assert len(broker._orders) == 1, "retry must not place a second broker order"
    assert outcome2["status"] == "submitted"
    assert outcome2["duplicate_preserved"] is True
    assert outcome2["order_id"] == broker._orders[0][3]
    assert outcome2["submitted_amount_usd"] == pytest.approx(placed_amount)
    # Codex/Copilot review: a preserved duplicate order must still be logged
    # into execution_records (so it appears in the slippage/coverage report)
    # and must NOT be misclassified as a budget-capped missing fill -- the
    # order was genuinely submitted (and here, filled).
    assert len(run_result.execution_records) == 1
    record = run_result.execution_records[0]
    assert record.ticker == "AAPL"
    assert record.order_id == placed_order_id
    assert record.status == "filled"
    assert "AAPL" not in run_result.loop_block_reasons


def test_same_stream_duplicate_does_not_double_log_an_already_recorded_order():
    """Regression test for "Deduplicate recovered execution records by order
    ID". When the same-stream duplicate check finds a broker order that is
    ALREADY present in run_result.execution_records (e.g. the crash-lease
    recovery-publish loop already appended a record for this exact order_id
    earlier in the same run), it must not log it a second time --
    _merge_execution_csv does not deduplicate within its own incoming batch,
    so two records with the same order_id would double-count one execution
    in orders.csv/filled counts/filled notional.
    """
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    state1 = StrategyState()
    outcome1, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state1,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome1["status"] == "submitted"
    placed_order_id = broker._orders[0][3]
    broker._fills[placed_order_id] = OrderFill(
        avg_fill_price=98.0,
        quantity_filled=1.0 / 98.0,
        order_submitted_at=now,
        order_filled_at=now,
    )

    state2 = StrategyState()
    run_result = RunResult(
        trading_day=trading_day, decisions=[decision], active_universe=["AAPL"]
    )
    # Simulates the recovery-publish loop having already appended an
    # execution record for this exact order earlier in the same run.
    run_result.execution_records.append(
        build_execution_record(
            ticker="AAPL",
            target_price=0.0,
            target_price_source="",
            decision_bar_close=0.0,
            notional_usd=1.0,
            action_reason="recovered_inverse_martingale_lease",
            order_id=placed_order_id,
            force_filled=True,
        )
    )
    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state2,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=run_result,
        ),
        running_deployed_7d=0.0,
    )

    matching = [r for r in run_result.execution_records if r.order_id == placed_order_id]
    assert len(matching) == 1, "the order must not be logged a second time"
    assert matching[0].action_reason == "recovered_inverse_martingale_lease"


def _write_scheduled_afternoon_artifact(history_root, trading_day, buy_candidates):
    """Write a minimal valid scheduled-afternoon run_state.json for
    ``trading_day`` under ``history_root``, for tests exercising
    deployment-history scans keyed off exact ticker/day/stream matches."""
    from scripts.run_classification import (
        CRON_ID,
        SCHEDULED_FOR_ET,
        SCHEDULED_RUN_TYPE,
        SCHEDULED_WINDOW_END_ET,
        SCHEDULED_WINDOW_START_ET,
    )

    run_dir = os.path.join(str(history_root), trading_day.isoformat())
    os.makedirs(run_dir, exist_ok=True)
    state = {
        "trading_day": trading_day.isoformat(),
        "status": "ok",
        "generated_at": f"{trading_day.isoformat()}T15:45:00",
        "run_classification": {
            "run_origin": "scheduled",
            "run_type": SCHEDULED_RUN_TYPE,
            "cron_id": CRON_ID,
            "scheduled_for_et": SCHEDULED_FOR_ET,
            "scheduled_window_start_et": SCHEDULED_WINDOW_START_ET,
            "scheduled_window_end_et": SCHEDULED_WINDOW_END_ET,
            "manual_run": False,
            "run_id": f"scheduled_{trading_day.strftime('%Y%m%d')}_1530ET",
            "triggered_at_utc": f"{trading_day.isoformat()}T19:45:00Z",
            "state_schema_version": "run-classification-v1",
        },
        "buy_candidates": buy_candidates,
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def test_guarded_top_up_retry_does_not_double_count_deployed_7d():
    # Codex review on PR #231 ("Do not add a preserved duplicate to
    # deployment again"): on the production path, _apply_deployment_history
    # has already summed a duplicate-preserved order into
    # config.deployed_last_7d_usd (and so into running_deployed_7d's starting
    # value) from THIS trading day's own already-written artifact before this
    # retry's batch even started. _process_buy_candidate must not add the
    # preserved amount to running_deployed_7d a second time.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=starting_deployed_7d,
    )
    assert returned_deployed_7d == starting_deployed_7d, (
        "a duplicate-preserved outcome places no new capital and must not "
        "increment running_deployed_7d"
    )


def test_guarded_top_up_retry_counts_recovered_order_when_not_yet_on_disk(tmp_path):
    # Codex review on PR #231 ("Count broker-only preserved orders in rolling
    # deployment"): when the prior attempt was interrupted after the broker
    # accepted the order but before it could write its own run_state.json,
    # _apply_deployment_history has nothing on disk to sum -- the recovered
    # order must be added to running_deployed_7d so later candidates in this
    # retry's batch (and the kill-switch audit) see the real deployed total.
    # An empty deployment_history_root (no run_state.json for this ticker/day
    # anywhere on disk) is exactly that "nothing counted this yet" state.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    history_root = os.path.join(str(tmp_path), "history")
    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            deployment_history_root=history_root,
        ),
        running_deployed_7d=0.0,
    )
    placed_amount = broker.placed["AAPL"]
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            deployment_history_root=history_root,
        ),
        running_deployed_7d=starting_deployed_7d,
    )
    assert returned_deployed_7d == pytest.approx(
        starting_deployed_7d + placed_amount
    ), (
        "a preserved duplicate not yet reflected in any written artifact must "
        "still be added to running_deployed_7d"
    )


def test_guarded_top_up_retry_does_not_recount_when_ticker_already_on_disk(tmp_path):
    # Codex review on PR #231 ("Verify the recovered order is actually in
    # deployment history"): date membership alone is not enough -- a
    # scheduled-afternoon artifact for today that DOES contain this exact
    # ticker's submitted order must still suppress the running_deployed_7d
    # increment (it's already summed into config.deployed_last_7d_usd via
    # _apply_deployment_history), even though this call also passes a
    # non-zero starting running_deployed_7d.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    history_root = os.path.join(str(tmp_path), "history")
    _write_scheduled_afternoon_artifact(
        history_root,
        trading_day,
        [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 1.0}],
    )
    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            deployment_history_root=history_root,
        ),
        running_deployed_7d=0.0,
    )
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            deployment_history_root=history_root,
        ),
        running_deployed_7d=starting_deployed_7d,
    )
    assert returned_deployed_7d == starting_deployed_7d


def test_guarded_top_up_retry_counts_ticker_when_only_a_different_ticker_is_on_disk(
    tmp_path,
):
    # Codex review on PR #231 ("Verify the recovered order is actually in
    # deployment history"): a same-day scheduled-afternoon artifact that
    # exists on disk but records a DIFFERENT ticker (e.g. an earlier
    # invocation that placed one ticker then crashed before a later
    # invocation's placement for THIS ticker was ever written) must not be
    # mistaken for proof that THIS ticker's order was already counted --
    # checking "any artifact for today" instead of this ticker specifically
    # would silently under-count deployment.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    history_root = os.path.join(str(tmp_path), "history")
    _write_scheduled_afternoon_artifact(
        history_root,
        trading_day,
        [{"ticker": "MSFT", "status": "submitted", "planned_amount_usd": 4.0}],
    )
    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            deployment_history_root=history_root,
        ),
        running_deployed_7d=0.0,
    )
    placed_amount = broker.placed["AAPL"]
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            deployment_history_root=history_root,
        ),
        running_deployed_7d=starting_deployed_7d,
    )
    assert returned_deployed_7d == pytest.approx(starting_deployed_7d + placed_amount)


def test_guarded_top_up_retry_records_recovered_buy_despite_seeded_prior_context():
    # Codex review on PR #231 ("Record the recovered buy separately from
    # seeded prior context"): on every real guarded run, _seed_prior_allocations
    # calls state.record_buy() for this ticker's morning/midday allocation
    # BEFORE _process_buy_candidate runs, so buys_today(...) is already
    # nonzero even in a freshly restarted process whose OWN afternoon order
    # was never booked into this StrategyState. Gating the duplicate-preserved
    # branch's state.record_buy on buys_today()==0 (as a prior fix did) would
    # then wrongly skip recording a genuinely new-to-this-state recovered
    # order, omitting it from day/week spend.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    trading_day = date(2026, 6, 29)
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    # A fresh, never-before-used StrategyState -- exactly what a process
    # restart looks like -- seeded with the morning allocation the same way
    # _run_once_impl seeds it before ever reaching _process_buy_candidate.
    fresh_state = StrategyState()
    _seed_prior_allocations(
        fresh_state,
        trading_day,
        True,
        {"AAPL": prior},
        one_order_per_ticker_per_day=False,
    )
    assert (
        fresh_state.buys_today("AAPL", trading_day) == 1
    ), "seeding must land the morning allocation"
    assert fresh_state.day_notional(trading_day) == pytest.approx(1.0)

    # The broker already has this stream's own afternoon order from an
    # earlier (now-dead) process instance -- simulated directly, since this
    # fresh process never placed it itself.
    broker = _RefIdTrackingBroker()
    broker._orders.append(
        (
            "AAPL",
            f"graded-dca-topup-{trading_day.isoformat()}-AAPL",
            3.0,
            "ORDER-AAPL-recovered",
        )
    )

    outcome, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=fresh_state,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["duplicate_preserved"] is True
    assert outcome["submitted_amount_usd"] == pytest.approx(3.0)
    # The recovered order must be booked ON TOP of the seeded morning
    # allocation, not skipped because buys_today was already nonzero.
    assert fresh_state.buys_today("AAPL", trading_day) == 2
    assert fresh_state.day_notional(trading_day) == pytest.approx(1.0 + 3.0)


def test_guarded_top_up_retry_does_not_rebook_when_state_already_recorded_it():
    # Codex review on PR #231 ("Avoid rebooking duplicates in reused strategy
    # state"): when a retry reuses the SAME StrategyState instance the first
    # attempt already booked this order through (rather than a fresh
    # process-restart StrategyState), the duplicate-preserved branch must not
    # call state.record_buy a second time -- that would inflate today's/this
    # week's spend and buy count for this ticker.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    shared_state = StrategyState()
    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=shared_state,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    placed_amount = broker.placed["AAPL"]
    assert shared_state.buys_today("AAPL", trading_day) == 1
    assert shared_state.day_notional(trading_day) == pytest.approx(placed_amount)

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=shared_state,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert shared_state.buys_today("AAPL", trading_day) == 1, (
        "a duplicate-preserved outcome must not rebook a buy already recorded "
        "in this same StrategyState instance"
    )
    assert shared_state.day_notional(trading_day) == pytest.approx(placed_amount), (
        "rebooking would double-count this order's notional in the same "
        "StrategyState instance's day spend"
    )


def test_guarded_top_up_retry_records_recovered_spread_status_without_a_verdict():
    # Codex review on PR #231 ("Retain the original spread verdict for
    # recovered orders"): a duplicate-preserved outcome must not carry a
    # fresh retry-time spread_ok/spread_not_ok/spread_unavailable verdict --
    # the spread can move between the original placement and this retry, and
    # an unfavorable fresh reading would relabel an already-authorized,
    # genuinely submitted order as having failed or never reached the safety
    # gate. It must record spread_status="not_evaluated" with no spread_ok
    # verdict instead -- the nearest value inside config/scheduled_tasks.json's
    # declared row_level_statuses enum that commits to no pass/fail claim
    # (Codex review, PR #231: "Keep recovered spread evidence within the
    # declared contract" -- a bespoke "duplicate_recovered" status was tried
    # first and rejected for falling outside that enum).
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    # Attempt 1: a narrow spread that actually authorizes the placement.
    narrow_spread_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=narrow_spread_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    # Attempt 2 (retry): the spread has since widened past the gate's limit.
    # A wide/failing spread on the RETRY must not surface as this row's
    # verdict -- the original placement is what was actually authorized.
    wide_spread_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(90.0, 110.0)
    )
    outcome2, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=wide_spread_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome2["duplicate_preserved"] is True
    assert outcome2["spread_status"] == "not_evaluated"
    assert outcome2["spread_ok"] is None
    assert outcome2["spread_pct"] is None


def test_guarded_top_up_retry_blocks_when_broker_returns_no_order_id():
    # Codex review on PR #231 ("Require an order ID before marking legacy
    # duplicates submitted"): a matched duplicate with no id/order_id field
    # (a legacy boolean-style adapter, or a matched order dict missing both)
    # must not be published as status="submitted" with a null order_id --
    # that row would be untraceable in submitted_orders.csv and
    # reconciliation could never match it to a broker fill.
    class _NoIdDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {"dollar_amount": 5.0}  # no id / order_id

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {"dollar_amount": 5.0}

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError(
                "must not place a fresh order when a same-stream duplicate is found"
            )

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _NoIdDupBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    outcome, _, _, errors_count_inc, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "blocked"
    assert outcome.get("order_id") is None
    assert errors_count_inc == 1


def test_duplicate_with_no_amount_fields_is_never_booked_as_a_zero_buy():
    # Codex review ("Preserve recovered notional when the retry delta is
    # zero"): the duplicate-recovery fallback was changed to
    # min(order_amount, top_up_outcome.incremental_order_amount). A retry whose
    # freshly recomputed top-up is now skipped entirely carries
    # incremental_order_amount == 0, so the min() booked an order the broker had
    # already accepted as a $0 buy -- record_buy(), running_deployed_7d and the
    # persisted candidate all omitting its real deployment, leaving later
    # candidates uncapped against a cap this order should have consumed. That is
    # the very "nonsensical $0 buy" the surrounding comment exists to prevent.
    class _AmountlessDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            # Identifies the order but carries neither dollar_amount nor
            # notional, nor any quantity/price to recover a fill from.
            return {"id": "DUP-NO-AMOUNT"}

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {"id": "DUP-NO-AMOUNT"}

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("must not place a fresh order for a known duplicate")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    # target_price ABOVE prior.reference_price -> the fresh top-up guard
    # computes a zero increment, which is exactly the case that produced $0.
    decision = _graded_decision(target_price=100.0)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    state = StrategyState()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=_AmountlessDupBroker(),
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )

    assert state.day_notional(trading_day) > 0.0, (
        "an order the broker already accepted must never be booked as $0 -- a "
        "zero retry delta is not evidence that nothing was deployed"
    )


def test_duplicate_with_no_amount_fields_is_never_shrunk_to_the_retry_delta():
    # Codex review ("Avoid shrinking amountless duplicates to the retry
    # delta"): the same fallback, in the OPPOSITE direction to the zero-delta
    # case above. Here the retry's freshly recomputed top-up is positive but
    # SMALLER than the order this run would otherwise place -- prior
    # allocation context that was not visible when the original order went in
    # has since appeared, so the guard now only wants the difference.
    # min(order_amount, incremental) booked the already-placed order at that
    # smaller delta, understating StrategyState deployment, the rolling
    # kill-switch total and the persisted candidate notional, and leaving
    # later candidates budget the duplicate order had already consumed.
    class _AmountlessDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            # Neither dollar_amount/notional nor quantity/price: this path
            # genuinely cannot price the duplicate from anything the broker said.
            return {"id": "DUP-NO-AMOUNT"}

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {"id": "DUP-NO-AMOUNT"}

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("must not place a fresh order for a known duplicate")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    # target_price BELOW prior.reference_price -> a real incremental top-up.
    decision = _graded_decision(target_price=98.0)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    # Pinned so the assertion below is about the min() and not about whatever
    # the ladder happens to compute: the guard wants $4 more, the candidate's
    # own order_amount is the $10 suggested_amount_usd.
    incremental = evaluate_top_up_guard(decision, prior, config).incremental_order_amount
    assert 0 < incremental < decision.suggested_amount_usd

    state = StrategyState()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=_AmountlessDupBroker(),
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )

    assert state.day_notional(trading_day) == decision.suggested_amount_usd, (
        "an unpriceable duplicate must be booked at the conservative "
        "order_amount, never at today's smaller retry delta -- over-recording "
        "caps later candidates too tightly, which is the safe direction; "
        "under-recording hands them budget the duplicate already spent"
    )


def test_duplicate_with_no_amount_fields_recovers_the_real_fill(tmp_path):
    # The preferred recovery, mirroring the immediate-recovery path's own
    # "Book the actual fill notional after immediate recovery": when a SETTLED
    # matched order carries quantity/average_price, that IS the real
    # deployment and beats any locally recomputed estimate.
    class _FillBearingDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {
                "id": "DUP-FILLED",
                "state": "filled",
                "cumulative_quantity": "3",
                "average_price": "7.00",
            }

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {
                "id": "DUP-FILLED",
                "state": "filled",
                "cumulative_quantity": "3",
                "average_price": "7.00",
            }

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("must not place a fresh order for a known duplicate")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision(target_price=100.0)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    state = StrategyState()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=_FillBearingDupBroker(),
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )

    assert state.day_notional(trading_day) == 21.0, "3 shares at $7.00 is the real deployment"


@pytest.mark.parametrize("live_state", ["partially_filled", "confirmed", "new", ""])
def test_a_live_partial_duplicate_stays_reserved_at_its_full_notional(live_state):
    # Codex review discussion_r3867947161 ("Keep live partial orders reserved
    # at their full notional"). _same_stream_order_exists deliberately matches
    # LIVE orders too, so an adapter can return a partially_filled duplicate
    # with no dollar_amount/notional. Booking cumulative_quantity *
    # average_price there recorded only the EXECUTED slice of an order whose
    # unfilled remainder is still working and can fill at any moment --
    # understating StrategyState and running_deployed_7d by that remainder and
    # handing later candidates in the same batch daily/weekly/kill-switch
    # headroom this order can still consume. Only a SETTLED order has a final
    # executed amount (the rule _recovered_broker_notional applies); anything
    # still live keeps the conservative requested notional.
    class _LivePartialDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def _dup(self):
            # $2 of a $10 order has executed so far; the broker reports no
            # dollar_amount/notional for it.
            order = {"id": "DUP-PARTIAL", "cumulative_quantity": "0.2", "average_price": "10.00"}
            if live_state:
                order["state"] = live_state
            return order

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("must not place a fresh order for a known duplicate")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision(target_price=100.0)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    state = StrategyState()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=_LivePartialDupBroker(),
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )

    assert state.day_notional(trading_day) == decision.suggested_amount_usd, (
        "a live partial's unfilled remainder is still committed: booking only "
        "its executed $2.00 leaves later candidates budget the outstanding "
        "order can still consume"
    )


@pytest.mark.parametrize("terminal_state", ["canceled", "expired"])
def test_a_terminally_canceled_partial_duplicate_books_its_executed_amount(terminal_state):
    # The other side of the same rule: a partial that has stopped moving IS
    # settled -- no further fill can land on it -- so its executed amount is
    # the final truth and over-recording the whole requested notional would
    # cap later candidates against money that was never deployed.
    class _TerminalPartialDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def _dup(self):
            return {
                "id": "DUP-CANCELED",
                "state": terminal_state,
                "cumulative_quantity": "0.2",
                "average_price": "10.00",
            }

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("must not place a fresh order for a known duplicate")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision(target_price=100.0)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    state = StrategyState()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=_TerminalPartialDupBroker(),
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )

    assert state.day_notional(trading_day) == 2.0, "0.2 shares at $10.00 executed and no more can"


@pytest.mark.parametrize("terminal_state", ["canceled", "expired"])
def test_a_terminal_partial_without_a_cumulative_quantity_is_not_valued_at_request_size(
    terminal_state,
):
    # Codex review discussion_r3868141054 ("Avoid treating requested quantity as
    # a terminal partial fill"): on a canceled/expired row ``quantity`` is what
    # was REQUESTED, not what executed, so the cumulative_quantity-or-quantity
    # fallback booked a partially filled cancellation at its full requested size
    # -- overstating daily/weekly deployment and suppressing later candidates
    # that should still fit. Only a FILLED row's plain quantity is its executed
    # amount, exactly as _recovered_broker_notional() scopes the same fallback.
    class _RequestedQuantityOnlyDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def _dup(self):
            # 5 shares REQUESTED at $10; no cumulative_quantity, so nothing here
            # says how much of it actually executed.
            return {
                "id": "DUP-CANCELED-NO-CUMQTY",
                "state": terminal_state,
                "quantity": "5",
                "average_price": "10.00",
            }

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("must not place a fresh order for a known duplicate")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision(target_price=100.0)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    state = StrategyState()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=_RequestedQuantityOnlyDupBroker(),
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )

    booked = state.day_notional(trading_day)
    assert booked != 50.0, (
        "5 REQUESTED shares at $10 is not evidence $50 executed on a "
        f"{terminal_state} order"
    )
    assert booked == decision.suggested_amount_usd, (
        "with no executed quantity to read, the conservative order_amount stands"
    )


def test_a_filled_duplicate_may_still_fall_back_to_plain_quantity():
    # The other half of the scoping: on a FILLED row the plain ``quantity`` IS
    # the executed amount, so a broker that omits cumulative_quantity there is
    # still valued from its real fill rather than an estimate.
    class _FilledQuantityOnlyDupBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=1000.0, account=FIXTURE_ACCOUNT_NUMBER)

        def _dup(self):
            return {
                "id": "DUP-FILLED-NO-CUMQTY",
                "state": "filled",
                "quantity": "3",
                "average_price": "7.00",
            }

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def same_day_order_with_ref_id_prefix_any_state(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return self._dup()

        def is_tradable_for_fractional_buy(self, ticker):
            return True

        def review_market_buy(self, ticker, notional_usd):
            return True, "ok"

        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise AssertionError("must not place a fresh order for a known duplicate")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision(target_price=100.0)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    state = StrategyState()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=_FilledQuantityOnlyDupBroker(),
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )

    assert state.day_notional(trading_day) == 21.0, "3 filled shares at $7.00"


def test_guarded_top_up_retry_finds_duplicate_even_when_fresh_guard_would_skip():
    # Codex review on PR #231, P1 ("Check for an existing order before
    # returning from the top-up guard"): a retry whose freshly recomputed
    # top-up guard evaluates to a zero increment (e.g. the price rose above
    # the prior reference) must still detect and preserve an order this
    # stream already placed on an earlier attempt -- not overwrite the
    # scheduled artifact with a "skipped" row that erases the real order's
    # evidence.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    # Attempt 1: target_price (98.0) below prior.reference_price (99.0) -> a
    # real incremental top-up is computed and placed.
    decision_attempt1 = _graded_decision(target_price=98.0)
    outcome1, _, _, _, _ = _process_buy_candidate(
        decision_attempt1,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome1["status"] == "submitted"
    assert len(broker._orders) == 1

    # Attempt 2 (retry): a fresh decision whose target_price (100.0) is now
    # ABOVE the prior reference price -- evaluate_top_up_guard would compute
    # a $0/skipped incremental on its own. The retry must still find and
    # preserve the real order from attempt 1 instead of reporting "skipped".
    decision_attempt2 = _graded_decision(target_price=100.0)
    outcome2, top_up_outcome2, _, _, _ = _process_buy_candidate(
        decision_attempt2,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert len(broker._orders) == 1, "retry must not place a second broker order"
    assert outcome2["status"] == "submitted", outcome2
    assert outcome2["duplicate_preserved"] is True
    # The fresh top-up guard context is still computed/available for the row
    # (e.g. for reporting), even though its own skip verdict was overridden.
    assert top_up_outcome2 is not None


def test_manual_placement_uses_distinct_ref_id_namespace():
    # Codex review on PR #231, P1 ("Separate manual ref IDs from scheduled
    # retry IDs"): a manual (scheduled=False) placement's ref_id must not
    # collide with a plain scheduled order's ref_id -- otherwise a LATER
    # scheduled run that treats this manual fill as cross-stream prior
    # context would have its same-stream duplicate check match the manual
    # order and wrongly skip a legitimate fresh incremental top-up.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    outcome, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=now,
            trading_day=date(2026, 6, 29),
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=False,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "submitted"
    ref_id = broker._orders[0][1]
    assert (
        "manual" in ref_id
    ), f"manual placement must use a distinct ref_id namespace, got {ref_id!r}"


def test_scheduled_top_up_not_blocked_by_manual_fills_ordinary_ref_id():
    # End-to-end regression for the ref_id-collision bug above: a manual
    # near-close run buys AAPL earlier in the day; a later SCHEDULED run
    # treats that fill as prior context (ticker_guarded=True) and must place
    # its own real incremental top-up, not mistake the manual order's ref_id
    # for its own earlier attempt and skip as a phantom duplicate.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    # Manual run: no prior allocation, so ticker_guarded=False and the ref_id
    # is the manual-namespaced one.
    manual_decision = _graded_decision(
        suggested_amount_usd=3.0, selected_base_amount_usd=1.0, buy_multiplier=3.0
    )
    manual_outcome, _, _, _, _ = _process_buy_candidate(
        manual_decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=False,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert manual_outcome["status"] == "submitted"
    assert len(broker._orders) == 1

    # Scheduled run: the manual fill above is now visible as prior context.
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=3.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    scheduled_decision = _graded_decision(target_price=98.0)
    scheduled_outcome, _, _, _, _ = _process_buy_candidate(
        scheduled_decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert scheduled_outcome.get("duplicate_preserved") is not True, (
        "the manual fill's ref_id must not be mistaken for this scheduled "
        "run's own earlier attempt"
    )
    assert scheduled_outcome["status"] == "submitted"
    assert (
        len(broker._orders) == 2
    ), "the scheduled run must place its own real incremental order"


def test_alpaca_get_order_fill():
    def mock_order_fill_fn(payload):
        assert payload["input"]["orderId"] == "12345"
        assert "isPaperAPI" in payload["input"]
        return {
            "order": {
                "id": "12345",
                "status": "filled",
                "filled_qty": "10",
                "filled_avg_price": "150.0",
                "submitted_at": "2026-07-02T13:30:00Z",
                "filled_at": "2026-07-02T13:30:05Z",
            }
        }

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 10.0
    assert fill.avg_fill_price == 150.0

    # test dryrun handling
    assert broker.get_order_fill("DRYRUN-12345") is None

    # test missing fn
    broker_no_fn = AlpacaBroker()
    assert broker_no_fn.get_order_fill("12345") is None


def test_alpaca_get_order_fill_ignores_partial():
    # If the order is partially_filled, returning an OrderFill causes build_execution_record
    # to consider it fully filled, preventing subsequent refreshes. It must remain None
    # (unfilled) until it's terminally "filled".
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "partially_filled",
                "filled_qty": "5",
                "filled_avg_price": "150.0",
                "submitted_at": "2026-07-02T13:30:00Z",
                "filled_at": "2026-07-02T13:30:05Z",
            }
        }

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_accepts_terminally_canceled_partial_fill():
    # A 'canceled' or 'expired' order can still carry a real, positive
    # filled_qty for shares that executed before the remainder was
    # canceled/expired. Unlike 'partially_filled' (non-terminal, still
    # eligible for refresh), these statuses will never progress further
    # under this order id, so that execution must be recorded now or it
    # becomes a permanent missing-fill gap (Codex review: "Record
    # executions from terminally canceled Alpaca orders"). A resolved
    # 'replaced' chain is covered separately by
    # test_alpaca_get_order_fill_follows_replacement_chain.
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "canceled",
                "filled_qty": "4",
                "filled_avg_price": "150.0",
                "submitted_at": "2026-07-02T13:30:00Z",
                "filled_at": "2026-07-02T13:30:05Z",
            }
        }

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 4.0
    assert fill.avg_fill_price == 150.0

    def mock_order_fill_fn_expired(payload):
        return {
            "order": {
                "id": "12345",
                "status": "expired",
                "filled_qty": "2",
                "filled_avg_price": "148.5",
            }
        }

    broker_expired = AlpacaBroker(order_fill_fn=mock_order_fill_fn_expired)
    fill_expired = broker_expired.get_order_fill("12345")
    assert fill_expired is not None
    assert fill_expired.quantity_filled == 2.0

    # A 'replaced' order with no replaced_by to follow at all does NOT fall
    # back to the frozen partial from the original order: per Alpaca's
    # documented semantics a genuinely replaced order always carries its
    # replacement's id, so a missing one signals an incomplete/malformed
    # response rather than a permanent characteristic. Report no fill and
    # let a later refresh retry (Codex review: "Retry replacement chains
    # without a usable next hop").
    def mock_order_fill_fn_replaced_no_target(payload):
        return {
            "order": {
                "id": "12345",
                "status": "replaced",
                "filled_qty": "3",
                "filled_avg_price": "149.0",
            }
        }

    broker_replaced = AlpacaBroker(order_fill_fn=mock_order_fill_fn_replaced_no_target)
    assert broker_replaced.get_order_fill("12345") is None

    # a canceled order with nothing executed still carries no fill.
    def mock_order_fill_fn_no_fill(payload):
        return {
            "order": {
                "id": "12345",
                "status": "canceled",
                "filled_qty": "0",
                "filled_avg_price": "0",
            }
        }

    broker_no_fill = AlpacaBroker(order_fill_fn=mock_order_fill_fn_no_fill)
    assert broker_no_fill.get_order_fill("12345") is None


def test_alpaca_get_order_fill_accepts_calculated_status_for_day_tif():
    # Per Alpaca's own docs, 'calculated' means "completed for the day
    # (either filled OR done for day), settlement pending" -- it is NOT a
    # synonym for 'filled', so it must be gated the same way as
    # 'done_for_day': a durable terminal signal only for a 'day' TIF order,
    # with the same explicit-zero exemption (a day order can legitimately
    # reach 'calculated' having executed nothing) (Codex review: "Gate
    # calculated status on the order's TIF").
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "calculated",
                "filled_qty": "10",
                "filled_avg_price": "150.0",
            }
        }

    broker = AlpacaBroker(
        config=AlpacaConfig(time_in_force="day"), order_fill_fn=mock_order_fill_fn
    )
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 10.0
    assert fill.avg_fill_price == 150.0

    def mock_order_fill_fn_no_fill(payload):
        return {
            "order": {
                "id": "12345",
                "status": "calculated",
                "filled_qty": "0",
                "filled_avg_price": "0",
            }
        }

    broker_no_fill = AlpacaBroker(
        config=AlpacaConfig(time_in_force="day"), order_fill_fn=mock_order_fill_fn_no_fill
    )
    assert broker_no_fill.get_order_fill("12345") is None


def test_alpaca_get_order_fill_calculated_stays_non_terminal_for_other_tif():
    # For any TIF other than 'day' (e.g. 'gtc'), a 'calculated' response
    # might represent the done_for_day case rather than a true final fill
    # -- Alpaca can resume working the order on a later trading day, so it
    # must stay eligible for a later refresh rather than being accepted
    # as final.
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "calculated",
                "filled_qty": "10",
                "filled_avg_price": "150.0",
            }
        }

    broker = AlpacaBroker(
        config=AlpacaConfig(time_in_force="gtc"), order_fill_fn=mock_order_fill_fn
    )
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_accepts_done_for_day_for_day_tif():
    # 'done_for_day' means Alpaca won't execute the order further today.
    # That's a durable terminal signal ONLY for a 'day' time-in-force order
    # (it expires with the trading day) -- must be accepted like
    # 'canceled'/'expired', including the explicit-zero exemption, since a
    # day order can legitimately end done_for_day having executed nothing
    # at all (Codex review: "Accept done_for_day Alpaca fills").
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "done_for_day",
                "filled_qty": "6",
                "filled_avg_price": "150.0",
            }
        }

    broker = AlpacaBroker(
        config=AlpacaConfig(time_in_force="day"), order_fill_fn=mock_order_fill_fn
    )
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 6.0
    assert fill.avg_fill_price == 150.0

    def mock_order_fill_fn_no_fill(payload):
        return {
            "order": {
                "id": "12345",
                "status": "done_for_day",
                "filled_qty": "0",
                "filled_avg_price": "0",
            }
        }

    broker_no_fill = AlpacaBroker(
        config=AlpacaConfig(time_in_force="day"), order_fill_fn=mock_order_fill_fn_no_fill
    )
    assert broker_no_fill.get_order_fill("12345") is None


def test_alpaca_get_order_fill_done_for_day_stays_non_terminal_for_other_tif():
    # For any TIF other than 'day' (e.g. 'gtc'), Alpaca can resume working
    # the order on a later trading day after done_for_day -- it is NOT a
    # durable terminal signal there, so it must stay eligible for a later
    # refresh rather than being accepted as final.
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "done_for_day",
                "filled_qty": "6",
                "filled_avg_price": "150.0",
            }
        }

    broker = AlpacaBroker(
        config=AlpacaConfig(time_in_force="gtc"), order_fill_fn=mock_order_fill_fn
    )
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_follows_replacement_chain():
    # When the connector CAN resolve a 'replaced' order's replacement, follow
    # it and aggregate: sum the filled quantity across every hop, compute the
    # notional-weighted average price, keep the earliest submitted_at and the
    # latest filled_at. Otherwise the original order's partial fill would be
    # reported as the whole execution, or (if it filled nothing) the real
    # fill on the replacement would be missed entirely (Codex review:
    # "Reconcile the replaced_by order before returning").
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "3",
            "filled_avg_price": "149.0",
            "submitted_at": "2026-07-02T13:30:00Z",
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "filled",
            "filled_qty": "7",
            "filled_avg_price": "151.0",
            "filled_at": "2026-07-02T13:31:00Z",
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 10.0  # 3 (original) + 7 (replacement)
    assert fill.avg_fill_price == pytest.approx((3 * 149.0 + 7 * 151.0) / 10.0)
    assert fill.order_submitted_at == datetime(2026, 7, 2, 13, 30, 0, tzinfo=timezone.utc)
    assert fill.order_filled_at == datetime(2026, 7, 2, 13, 31, 0, tzinfo=timezone.utc)


def test_alpaca_get_order_fill_replacement_chain_still_open_stays_none():
    # If the replacement order is itself still live (not yet terminal), the
    # whole chain isn't resolved yet -- it must report no fill so a later
    # refresh retries, not the partial accumulated so far.
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "3",
            "filled_avg_price": "149.0",
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "partially_filled",
            "filled_qty": "1",
            "filled_avg_price": "151.0",
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_replacement_lookup_failure_returns_none():
    # A 'replaced' order DOES have a replaced_by to follow, but the lookup for
    # that id comes back empty/mismatched (e.g. a stale or transient
    # response). Unlike the no-replaced_by case, this must NOT fall back to
    # the original's partial fill: build_execution_record() would persist it
    # as a completed 'filled' record, and _refresh_fills_from_broker() would
    # then never retry -- permanently omitting whatever the replacement order
    # goes on to execute (Codex review: "Retry unresolved replacement-chain
    # lookups").
    def mock_order_fill_fn(payload):
        # Always answers with the original order, regardless of which id was
        # requested -- so looking up "67890" never resolves.
        return {
            "order": {
                "id": "12345",
                "status": "replaced",
                "filled_qty": "3",
                "filled_avg_price": "149.0",
                "replaced_by": "67890",
            }
        }

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_replacement_terminal_invalid_numerics_returns_none():
    # The chain resolves to a genuinely terminal 'filled' hop, but that
    # hop's own fill numerics are missing/zero -- a malformed or incomplete
    # response for that specific lookup. It must NOT fall back to the
    # earlier hop's valid partial fill as if the chain were fully resolved,
    # or a later real fill on the terminal hop would never be picked up
    # (Codex review: "Retry chains whose terminal fill details are
    # invalid").
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "3",
            "filled_avg_price": "149.0",
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "filled",
            "filled_qty": "0",
            "filled_avg_price": "0",
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_replacement_chain_exceeding_hop_cap_returns_none():
    # A replacement chain deeper than the hop cap must report no fill rather
    # than silently returning whatever partial had been accumulated when the
    # cap was hit -- that partial reflects an unresolved chain, not a
    # genuine terminal state (Codex review: "Retry replacement chains that
    # exceed the lookup cap").
    orders_by_id = {}
    for i in range(15):
        orders_by_id[str(i)] = {
            "id": str(i),
            "status": "replaced",
            "filled_qty": "1",
            "filled_avg_price": "100.0",
            "replaced_by": str(i + 1),
        }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("0") is None


def test_alpaca_get_order_fill_preserves_submitted_at_from_zero_fill_hop():
    # The original order is replaced before executing anything (zero fill),
    # but it still has a real submitted_at that must count as the chain's
    # earliest submission time even though this hop contributed no
    # quantity -- otherwise end-to-end latency is understated using only the
    # replacement's later submission time (Codex review: "Preserve
    # submission times from zero-fill replacement hops").
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "0",
            "filled_avg_price": "0",
            "submitted_at": "2026-07-02T13:30:00Z",
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "filled",
            "filled_qty": "10",
            "filled_avg_price": "150.0",
            "submitted_at": "2026-07-02T13:30:10Z",
            "filled_at": "2026-07-02T13:31:00Z",
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 10.0
    assert fill.order_submitted_at == datetime(2026, 7, 2, 13, 30, 0, tzinfo=timezone.utc)
    assert fill.order_filled_at == datetime(2026, 7, 2, 13, 31, 0, tzinfo=timezone.utc)


def test_alpaca_get_order_fill_replacement_ends_in_unfilled_cancel():
    # The chain resolves to a genuinely terminal 'canceled' hop that
    # legitimately reports a zero fill (the replacement never executed
    # before being canceled). Unlike a malformed 'filled' terminal hop, this
    # IS a real, final answer -- the earlier hop's valid partial must still
    # be returned, not discarded (Codex review: "Preserve earlier fills when
    # a replacement ends unfilled").
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "3",
            "filled_avg_price": "149.0",
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "canceled",
            "filled_qty": "0",
            "filled_avg_price": "0",
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 3.0
    assert fill.avg_fill_price == 149.0


def test_alpaca_get_order_fill_replacement_ends_in_unfilled_cancel_with_null_price():
    # Alpaca can report a canceled/expired unfilled order with
    # filled_avg_price as JSON null rather than "0" or omitted -- order.get(
    # key, 0.0) only substitutes the default when the key is ABSENT, so a
    # present null reaches a bare float(None) and raises, discarding the
    # earlier hop's real partial fill along with it. Must degrade to using
    # the accumulated total, not crash (Codex review: "Accept null prices on
    # unfilled canceled replacements").
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "3",
            "filled_avg_price": "149.0",
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "canceled",
            "filled_qty": "0",
            "filled_avg_price": None,
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 3.0
    assert fill.avg_fill_price == 149.0


def test_alpaca_get_order_fill_retries_replacement_with_unpriced_partial():
    # An INTERMEDIATE 'replaced' hop reports a genuinely positive filled_qty
    # but an unusable filled_avg_price (missing/zero/non-finite). A positive
    # quantity means a real execution happened there; silently dropping it
    # while continuing to follow replaced_by would under-report a fill known
    # to have happened once the chain resolves using only the later hop's
    # data. The whole chain must be treated as unresolved instead (Codex
    # review: "Retry replacements with invalid partial-fill numerics").
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "3",
            "filled_avg_price": "0",  # real qty, unusable price
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "filled",
            "filled_qty": "7",
            "filled_avg_price": "151.0",
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_retries_canceled_replacement_with_invalid_quantity():
    # A terminal 'canceled'/'expired' hop is trusted as a genuine zero fill
    # ONLY when filled_qty is an explicit, finite zero. A missing,
    # non-numeric, non-finite, or negative filled_qty is NOT proof nothing
    # executed -- it's an ambiguous/malformed response -- so it must not be
    # silently treated the same as a resolved zero fill and used to freeze
    # an earlier hop's valid partial as final (Codex review: "Retry
    # canceled replacements with invalid quantities").
    base_replaced = {
        "id": "12345",
        "status": "replaced",
        "filled_qty": "3",
        "filled_avg_price": "149.0",
        "replaced_by": "67890",
    }

    invalid_terminal_cases = [
        {"status": "canceled"},  # filled_qty entirely absent
        {"status": "canceled", "filled_qty": "-1"},
        {"status": "canceled", "filled_qty": "not-a-number"},
        {"status": "canceled", "filled_qty": "NaN"},
        {"status": "expired", "filled_qty": None},
    ]
    for terminal_order in invalid_terminal_cases:
        terminal_order = {"id": "67890", **terminal_order}
        orders_by_id = {"12345": base_replaced, "67890": terminal_order}

        def mock_order_fill_fn(payload, _orders=orders_by_id):
            return {"order": _orders[payload["input"]["orderId"]]}

        broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
        assert broker.get_order_fill("12345") is None, f"expected None for {terminal_order}"


def test_alpaca_get_order_fill_rejects_mixed_timezone_awareness():
    # A naive (offset-less) timestamp mixed with a timezone-aware one from
    # another hop can't be safely subtracted downstream in
    # build_execution_record()'s latency computation (TypeError: can't
    # subtract offset-naive and offset-aware datetimes). The naive one must
    # be treated as unknown rather than passed through, while the otherwise-
    # valid fill quantity/price is still returned.
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "filled",
                "filled_qty": "10",
                "filled_avg_price": "150.0",
                "submitted_at": "2026-07-02T13:30:00",  # naive: no offset/Z
                "filled_at": "2026-07-02T13:31:00Z",  # aware
            }
        }

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 10.0
    assert fill.order_submitted_at is None
    assert fill.order_filled_at == datetime(2026, 7, 2, 13, 31, 0, tzinfo=timezone.utc)


def test_alpaca_get_order_fill_replacement_cycle_returns_none():
    # A cyclic replaced_by chain (A -> B -> A) must never loop forever, and
    # must not fall back to whatever was accumulated before the cycle was
    # detected: a cycle is definitionally invalid Alpaca data (an order
    # can't be replaced by an order that replaced it), so it signals a
    # malformed/incomplete response rather than a resolved chain -- report
    # no fill and let a later refresh retry.
    orders_by_id = {
        "12345": {
            "id": "12345",
            "status": "replaced",
            "filled_qty": "3",
            "filled_avg_price": "149.0",
            "replaced_by": "67890",
        },
        "67890": {
            "id": "67890",
            "status": "replaced",
            "filled_qty": "0",
            "filled_avg_price": "0",
            "replaced_by": "12345",
        },
    }

    def mock_order_fill_fn(payload):
        return {"order": orders_by_id[payload["input"]["orderId"]]}

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("12345") is None


def test_alpaca_get_order_fill_unwraps_data_envelope():
    # An MCP/Pipedream connector response can arrive wrapped in one or more
    # {"data": ...} layers (the same envelope dca_live_adapters._unwrap() and
    # dca_reporting._unwrap_payload() already handle for other adapters).
    # Failing to unwrap it here would report a genuine fill as a missing-fill
    # gap (Codex review: "Unwrap connector data envelopes before parsing
    # fills").
    def mock_order_fill_fn_single_wrap(payload):
        return {
            "data": {
                "order": {
                    "id": "12345",
                    "status": "filled",
                    "filled_qty": "10",
                    "filled_avg_price": "150.0",
                }
            }
        }

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn_single_wrap)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.quantity_filled == 10.0
    assert fill.avg_fill_price == 150.0

    def mock_order_fill_fn_double_wrap(payload):
        return {
            "data": {
                "data": {
                    "orders": [
                        {
                            "id": "12345",
                            "status": "filled",
                            "filled_qty": "5",
                            "filled_avg_price": "100.0",
                        }
                    ]
                }
            }
        }

    broker_double = AlpacaBroker(order_fill_fn=mock_order_fill_fn_double_wrap)
    fill_double = broker_double.get_order_fill("12345")
    assert fill_double is not None
    assert fill_double.quantity_filled == 5.0


def test_alpaca_get_order_fill_rejects_invalid_fill_numerics():
    # A terminal status alone is not enough: filled_qty <= 0 was already
    # guarded, but a missing/zero/negative/non-finite filled_avg_price (or a
    # non-finite filled_qty) must also be rejected, or _log_execution() would
    # mark the order filled with corrupt numerics and the nonblank CSV values
    # would stop _refresh_fills_from_broker() from ever retrying it (Codex
    # review: "Reject non-positive and non-finite Alpaca fill values").
    def _order_with(**overrides):
        base = {
            "id": "12345",
            "status": "filled",
            "filled_qty": "10",
            "filled_avg_price": "150.0",
        }
        base.update(overrides)
        return {"order": base}

    invalid_cases = [
        {"filled_avg_price": "0"},
        {"filled_avg_price": "-150.0"},
        {"filled_avg_price": "NaN"},
        {"filled_avg_price": "Infinity"},
        {"filled_qty": "NaN"},
        {"filled_qty": "Infinity"},
    ]
    for overrides in invalid_cases:
        order = _order_with(**overrides)

        def mock_order_fill_fn(payload, _order=order):
            return _order

        broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
        assert broker.get_order_fill("12345") is None, f"expected None for {overrides}"


def test_alpaca_get_order_fill_missing_timestamps_stay_none():
    # A filled-order response omitting submitted_at/filled_at must leave those
    # fields None rather than backfilling datetime.now(): two back-to-back
    # now() calls would otherwise persist a fabricated near-zero latency as
    # broker evidence (Codex review: "Preserve absent Alpaca fill timestamps
    # as unknown").
    def mock_order_fill_fn(payload):
        return {
            "order": {
                "id": "12345",
                "status": "filled",
                "filled_qty": "10",
                "filled_avg_price": "150.0",
            }
        }

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    fill = broker.get_order_fill("12345")
    assert fill is not None
    assert fill.order_submitted_at is None
    assert fill.order_filled_at is None


def test_alpaca_get_order_fill_malformed_response_returns_none():
    # Parsing must happen inside the same guarded path as the network call: a
    # None/non-dict payload, a missing fill value, or a malformed timestamp
    # must degrade to an unfilled result instead of raising, since
    # _log_execution() calls get_order_fill() without its own exception guard
    # (Codex review: "Restore malformed-response handling inside the
    # exception boundary").
    def mock_order_fill_fn(payload):
        return None

    broker = AlpacaBroker(order_fill_fn=mock_order_fill_fn)
    assert broker.get_order_fill("12345") is None

    # A malformed timestamp degrades just that field to unknown rather than
    # discarding the otherwise-valid fill quantity/price -- the primary
    # purpose of this lookup -- and must not raise (it's caught internally
    # by _parse_alpaca_timestamp, not just the outer exception boundary).
    def mock_order_fill_fn_bad_timestamp(payload):
        return {
            "order": {
                "id": "12345",
                "status": "filled",
                "filled_qty": "10",
                "filled_avg_price": "150.0",
                "submitted_at": "not-a-timestamp",
            }
        }

    broker_bad_ts = AlpacaBroker(order_fill_fn=mock_order_fill_fn_bad_timestamp)
    fill_bad_ts = broker_bad_ts.get_order_fill("12345")
    assert fill_bad_ts is not None
    assert fill_bad_ts.quantity_filled == 10.0
    assert fill_bad_ts.order_submitted_at is None

    def mock_order_fill_fn_bad_qty(payload):
        return {
            "order": {
                "id": "12345",
                "status": "filled",
                "filled_qty": "not-a-number",
                "filled_avg_price": "150.0",
            }
        }

    broker_bad_qty = AlpacaBroker(order_fill_fn=mock_order_fill_fn_bad_qty)
    assert broker_bad_qty.get_order_fill("12345") is None


def test_alpaca_get_order_fill_warns_when_live_orders_enabled_without_fn(caplog):
    # AlpacaBroker.get_order_fill must not fail silently: if live orders are
    # enabled but no order_fill_fn is wired, every execution would otherwise
    # be reported as unfilled with no signal that lookup is unimplemented.
    config = AlpacaConfig(enable_live_orders=True)
    broker = AlpacaBroker(config=config)
    with caplog.at_level(logging.WARNING):
        assert broker.get_order_fill("12345") is None
    assert any("order_fill_fn" in record.message for record in caplog.records)


def test_alpaca_broker_same_stream_check_fails_closed():
    # Codex review on PR #231 ("Fail closed for Alpaca until client IDs are
    # wired"): build_broker() can return AlpacaBroker for a guarded top-up
    # candidate, but place_market_buy never sends a client order id the live
    # connector could match against, so this narrow check can never actually
    # detect a same-stream duplicate. Degrading to "no known duplicate" would
    # silently disable retry protection for a live-enabled AlpacaBroker and
    # let a retry place a second real order. It must raise instead, matching
    # the posture of an adapter that doesn't implement this method at all.
    broker = AlpacaBroker()
    with pytest.raises(RuntimeError):
        broker.same_day_strategy_order_exists_with_ref_id_prefix(
            "AAPL", date(2026, 6, 29), ("graded-dca-topup-2026-06-29-AAPL",)
        )
    # And _same_stream_order_exists (the real call site) must also raise, so a
    # guarded top-up on Alpaca is blocked rather than silently unprotected.
    with pytest.raises(RuntimeError):
        _same_stream_order_exists(broker, "AAPL", date(2026, 6, 29))


def test_guarded_top_up_dry_run_skips_duplicate_check_entirely():
    # Codex review on PR #231 ("Allow guarded Alpaca simulations through the
    # duplicate hook"): the same-stream duplicate check exists only to guard
    # against a REAL duplicate broker order, but it ran unconditionally
    # whenever ticker_guarded was true -- even under config.dry_run, where no
    # real order (duplicate or otherwise) is ever submitted. Combined with
    # AlpacaBroker's now fail-closed narrow check, a scheduled Alpaca DRY RUN
    # with prior same-day context would raise and get blocked instead of
    # producing the expected simulated order. The check must be skipped
    # entirely under dry_run, regardless of which broker is selected.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=True)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )
    # AlpacaBroker's narrow duplicate lookup always raises (fail-closed) --
    # if the dry-run candidate reaches it at all, this test would fail with
    # that RuntimeError instead of exercising the intended simulated path.
    broker = AlpacaBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    outcome, _, _, errors_count_inc, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert errors_count_inc == 0
    assert outcome["status"] == "dry_run"
    assert "duplicate_preserved" not in outcome


def test_guarded_top_up_first_attempt_bypasses_broad_same_day_check():
    # The broad same_day_strategy_order_exists check must stay bypassed for a
    # guarded ticker's FIRST attempt this run, even when the broker reports an
    # (unrelated, cross-stream) same-day order for the ticker -- otherwise a
    # legitimate top-up on top of a morning allocation could never place.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        selected_base_amount_usd=1.0,
    )

    class _CrossStreamBroker(_RefIdTrackingBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True  # e.g. the morning stream's own order, unrelated to this stream

    broker = _CrossStreamBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    outcome, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=date(2026, 6, 29),
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "submitted"
    assert outcome.get("duplicate_preserved") is not True
    assert "AAPL" in broker.placed


def test_non_guarded_candidate_still_blocked_by_broad_same_day_check():
    # A ticker with no prior same-day allocation is not "guarded", so the
    # existing broad broker-level same-day check must still gate it.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0)
    decision = _graded_decision()
    broker = _RefIdTrackingBroker()
    broker._orders.append(("AAPL", "other-stream-ref", 5.0, "ORDER-OTHER"))
    outcome, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=date(2026, 6, 29),
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "skipped"
    assert outcome["skip_reason"] == "same-day broker order exists"
    assert "AAPL" not in broker.placed


def test_graded_dca_ref_id_prefix_scopes_non_afternoon_streams_only():
    # "afternoon" (this module's historical, single-stream default) must keep
    # the exact legacy, unprefixed convention byte-for-byte -- docs/safeguards.md's
    # Gate 4 sole-executor check and the real legacy 10ce362c task's own order
    # history depend on this literal string. Only morning/midday (the
    # seven-slot roster's other two canonical run_slot() streams) get a
    # distinguishing token.
    assert _graded_dca_ref_id_prefix("afternoon") == "graded-dca"
    assert _graded_dca_ref_id_prefix("afternoon", "topup") == "graded-dca-topup"
    assert _graded_dca_ref_id_prefix("afternoon", "manual") == "graded-dca-manual"
    assert _graded_dca_ref_id_prefix("morning") == "graded-dca-morning"
    assert _graded_dca_ref_id_prefix("morning", "topup") == "graded-dca-morning-topup"
    assert _graded_dca_ref_id_prefix("midday") == "graded-dca-midday"
    assert _graded_dca_ref_id_prefix("midday", "topup") == "graded-dca-midday-topup"


def test_same_stream_order_exists_does_not_cross_canonical_slots(monkeypatch):
    # Codex review (P1): "Namespace idempotency keys before enabling cross-slot
    # top-ups". Before the stream parameter existed, a real order the morning
    # slot placed for AAPL (ref_id "graded-dca-2026-06-29-AAPL") would ALSO
    # match the midday slot's own same-stream duplicate lookup, since both
    # canonical slots share this exact near_close_dca.py engine and used the
    # identical unprefixed prefix. That misread a genuine cross-slot top-up as
    # "this run's own retry" and silently skipped placing it.
    broker = _RefIdTrackingBroker()
    trading_day = date(2026, 6, 29)
    morning_ref_id = (
        f"{_graded_dca_ref_id_prefix('morning')}-{trading_day.isoformat()}-AAPL"
    )
    broker._orders.append(("AAPL", morning_ref_id, 1.0, "ORDER-MORNING-AAPL"))

    # The midday slot's own same-stream check must NOT find the morning
    # slot's order -- it belongs to a different stream.
    assert _same_stream_order_exists(broker, "AAPL", trading_day, "midday") is None
    # The morning slot's own retry check DOES find its own earlier order.
    found = _same_stream_order_exists(broker, "AAPL", trading_day, "morning")
    assert found is not None
    assert found["id"] == "ORDER-MORNING-AAPL"


def test_same_stream_duplicate_check_or_assume_exists_fails_closed():
    # Codex review discussion on commit 3d8df75 ("Recover accepted orders
    # before discarding their credit"): the caller (the final-session credit
    # flush's discard decision) uses this to decide whether a destructive
    # discard is safe. An inconclusive broker check must never read as "no
    # duplicate" -- that would silently discard possibly-real ledger evidence
    # of an order the broker check itself failed to confirm one way or the
    # other.
    from near_close_dca import _same_stream_duplicate_check_or_assume_exists

    class _RaisingBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, prefixes):
            raise RuntimeError("broker API unavailable")

    trading_day = date(2026, 6, 29)
    assert (
        _same_stream_duplicate_check_or_assume_exists(
            cast(Any, _RaisingBroker()), "AAPL", trading_day, "afternoon"
        )
        is True
    )

    # A real, conclusive "no duplicate" result is still respected.
    broker = _RefIdTrackingBroker()
    assert (
        _same_stream_duplicate_check_or_assume_exists(broker, "AAPL", trading_day, "afternoon")
        is False
    )


def test_guarded_top_up_across_canonical_slots_places_a_new_order_not_a_duplicate():
    # End-to-end version of the above through _process_buy_candidate: the
    # midday slot guard-topping-up a ticker the morning slot already bought
    # must actually SUBMIT the incremental order, not silently re-confirm the
    # morning slot's order as though it were its own retry.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()  # target_price=98.0, below the $99 prior reference.
    trading_day = date(2026, 6, 29)
    morning_ref_id = (
        f"{_graded_dca_ref_id_prefix('morning')}-{trading_day.isoformat()}-AAPL"
    )
    broker = _RefIdTrackingBroker()
    broker._orders.append(("AAPL", morning_ref_id, 1.0, "ORDER-MORNING-AAPL"))
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        run_id="slot_1030",
        source="scheduled",
        selected_base_amount_usd=2.0,
    )
    now = datetime(2026, 6, 29, 12, 30, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            stream="midday",
        ),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome.get("duplicate_preserved") is not True
    assert top_up_outcome is not None
    assert top_up_outcome.top_up_guard_result == "incremental_top_up"
    # $10 desired (2.0 base * 5.0 multiplier) - $1.0 already bought = $9.0 incremental.
    assert broker.placed["AAPL"] == 9.0
    midday_ref_id = (
        f"{_graded_dca_ref_id_prefix('midday', 'topup')}-{trading_day.isoformat()}-AAPL"
    )
    assert broker._orders[-1][1] == midday_ref_id


def test_guarded_top_up_corrects_an_understated_artifact_prior_against_the_broker():
    # Codex review (P1): "Include broker orders in the delta before disabling
    # the duplicate gate". The artifact-derived prior says only $1.00 was
    # bought (e.g. the artifact write for a second fill failed, or a
    # different-ref-id order isn't reflected in the scanned prior-allocation
    # roots), but the broker's own ground-truth same-day total for this
    # ticker is $9.00. Trusting the artifact figure would compute a $9.00
    # incremental top-up on top of capital already spent; the corrected
    # delta must instead be the true remaining $1.00.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()  # desired = 2.0 base * 5.0 multiplier = $10.00.
    trading_day = date(2026, 6, 29)
    broker = _RefIdTrackingBroker()
    # A real $9.00 same-day order the artifact trail failed to capture, under
    # a ref id our own same-stream check does not recognize as a duplicate.
    broker._orders.append(
        ("AAPL", "some-other-untracked-ref-id", 9.0, "ORDER-UNTRACKED")
    )
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        run_id="slot_1030",
        source="scheduled",
        selected_base_amount_usd=2.0,
    )
    now = datetime(2026, 6, 29, 12, 30, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            stream="midday",
        ),
        running_deployed_7d=0.0,
    )

    assert top_up_outcome is not None
    assert top_up_outcome.top_up_guard_result == "incremental_top_up"
    assert top_up_outcome.incremental_order_amount == 1.0
    assert outcome["status"] == "submitted"
    assert broker.placed["AAPL"] == 1.0


def test_guarded_top_up_fails_closed_when_broker_cannot_verify_same_day_notional():
    # A broker adapter that doesn't implement same_day_ticker_notional_usd at
    # all (e.g. AlpacaBroker) must not silently fall back to trusting the
    # artifact-only prior -- that's exactly the gap this fix closes.
    class _NoNotionalCheckBroker(_RefIdTrackingBroker):
        def same_day_ticker_notional_usd(self, ticker, trading_day):
            raise RuntimeError("notional lookup not implemented")

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    trading_day = date(2026, 6, 29)
    broker = _NoNotionalCheckBroker()
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        run_id="slot_1030",
        source="scheduled",
        selected_base_amount_usd=2.0,
    )
    now = datetime(2026, 6, 29, 12, 30, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    outcome, _, _, errors_count_inc, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            stream="midday",
        ),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "same-day notional check failed" in outcome["skip_reason"]
    assert "AAPL" not in broker.placed
    assert errors_count_inc == 1


def test_same_stream_order_exists_fails_closed_when_broker_unsupported():
    # An adapter that doesn't implement the narrower ref-id check must raise
    # rather than silently behave as "no duplicate" -- a guarded top-up retry
    # would otherwise double-buy on exactly the broker this check exists for.
    class _LegacyBroker:
        pass

    with pytest.raises(RuntimeError):
        _same_stream_order_exists(cast(Any, _LegacyBroker()), "AAPL", date(2026, 6, 29))


def test_insufficient_buying_power_blocks_before_placement():
    # available_buying_power() < order_amount must block the candidate before
    # any broker order is placed, and be distinguishable from a cap/spread block.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    broker = DryRunBroker(cash=0.50)
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    outcome, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=date(2026, 6, 29),
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "blocked"
    assert outcome["skip_reason"] == "insufficient buying power"
    assert outcome["budget_blocked"] is True


def test_not_tradable_for_fractional_buy_blocks_before_placement():
    class _UntradableBroker(DryRunBroker):
        def is_tradable_for_fractional_buy(self, ticker):
            return False

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    broker = _UntradableBroker(cash=1000.0)
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    outcome, _, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=date(2026, 6, 29),
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=True,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "blocked"
    assert outcome["skip_reason"] == "not tradable for fractional buy"
    assert "AAPL" not in broker._fills


def test_broker_review_block_prevents_manual_order_placement():
    # A manual (non-scheduled) run routes through review_market_buy as its
    # human-reviewed safety net; a broker-side rejection must block the order.
    class _ReviewBlockedBroker(DryRunBroker):
        def review_market_buy(self, ticker, notional_usd):
            return False, "flagged for manual review"

    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    decision = _graded_decision()
    broker = _ReviewBlockedBroker(cash=1000.0)
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    outcome, _, _, _, reviews_attempted = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=date(2026, 6, 29),
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=False,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "blocked"
    assert outcome["skip_reason"] == "review blocked: flagged for manual review"
    assert reviews_attempted == 1


def test_approved_dates_cover_window_and_exclude_holidays():
    from near_close_dca import APPROVED_TEST_DATES

    # Full canonical window is covered...
    assert date(2026, 7, 2) in APPROVED_TEST_DATES
    assert date(2026, 8, 28) in APPROVED_TEST_DATES
    # ...weekends are excluded...
    assert date(2026, 7, 4) not in APPROVED_TEST_DATES  # Saturday
    # ...and the observed Independence Day holiday (Fri 2026-07-03) is excluded.
    assert date(2026, 7, 3) not in APPROVED_TEST_DATES


def test_allocation_sort_key_tie_break_is_alphabetical():
    from types import SimpleNamespace

    def cand(ticker, mult, drop, rh_drop=None):
        return SimpleNamespace(
            ticker=ticker,
            buy_multiplier=mult,
            finance_signal=SimpleNamespace(drop_vs_previous_close_pct=drop),
            robinhood_signal=(
                None
                if rh_drop is None
                else SimpleNamespace(drop_vs_previous_close_pct=rh_drop)
            ),
        )

    def order(cs):
        return [c.ticker for c in sorted(cs, key=allocation_sort_key)]

    # Equal multiplier and drop -> ticker alphabetical breaks the tie (A->Z),
    # not the input/universe order.
    assert order(
        [cand("MSFT", 2.0, 3.0), cand("AAPL", 2.0, 3.0), cand("GOOG", 2.0, 3.0)]
    ) == [
        "AAPL",
        "GOOG",
        "MSFT",
    ]

    # Higher multiplier dominates both drop and alphabetical.
    assert order(
        [cand("AAPL", 1.0, 9.0), cand("ZZZ", 3.0, 0.5), cand("MSFT", 2.0, 5.0)]
    ) == [
        "ZZZ",
        "MSFT",
        "AAPL",
    ]

    # Same multiplier -> larger drop first, then ticker.
    assert order(
        [cand("BBB", 2.0, 1.0), cand("AAA", 2.0, 4.0), cand("CCC", 2.0, 4.0)]
    ) == [
        "AAA",
        "CCC",
        "BBB",
    ]

    # Drop is the max across sources: a bigger Robinhood drop outranks finance-only.
    assert order([cand("FN", 2.0, 3.0), cand("RH", 2.0, 1.0, rh_drop=6.0)]) == [
        "RH",
        "FN",
    ]


def _no_data_signal(source, provider_error):
    return SourceSignal(
        ticker="AAPL",
        source=source,
        action=Action.NO_DATA,
        reason=(
            "provider error: boom"
            if provider_error
            else "missing completed bar or previous close"
        ),
        provider_error=provider_error,
    )


def test_provider_error_forces_manual_review_instead_of_auto_buy():
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    rs = evaluate_source_signal("AAPL", now, StrategyConfig(), rh, 5)
    assert rs.action == Action.BUY

    fs = _no_data_signal(SourceName.FINANCE, provider_error=True)
    decision = reconcile_signals("AAPL", fs, rs)
    assert decision.action == Action.MANUAL_REVIEW
    assert "provider error" in decision.reason
    assert decision.suggested_amount_usd == 0.0


def test_plain_no_data_still_allows_robinhood_only_buy():
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    rs = evaluate_source_signal("AAPL", now, StrategyConfig(), rh, 5)

    # Genuine missing data (no exception) keeps the documented Robinhood-only path.
    fs = _no_data_signal(SourceName.FINANCE, provider_error=False)
    decision = reconcile_signals("AAPL", fs, rs)
    assert decision.action == Action.BUY
    assert decision.reason == "Robinhood-only BUY because finance source is unavailable"


# --- Slippage calculations ---------------------------------------------------


def test_slippage_calculations_unfavorable_buy():
    submitted = datetime(2026, 6, 29, 15, 45, 0, tzinfo=EASTERN)
    filled = submitted + timedelta(seconds=3)
    record = build_execution_record(
        ticker="AAPL",
        target_price=100.0,
        target_price_source="finance:signal_quote",
        decision_bar_close=100.5,
        notional_usd=10.0,
        order_submitted_at=submitted,
        order_filled_at=filled,
        avg_fill_price=101.0,
        quantity_filled=2.0,
    )
    assert record.slippage_abs is not None
    assert record.slippage_pct is not None
    assert record.slippage_cost is not None
    assert record.status == "filled"
    assert abs(record.slippage_abs - 1.0) < 1e-9
    assert abs(record.slippage_pct - 1.0) < 1e-9  # 1 / 100 * 100
    assert abs(record.slippage_cost - 2.0) < 1e-9  # 1.0 * 2 shares
    assert record.latency_seconds == 3.0
    assert record.execution_quality == "poor"


def test_slippage_favorable_and_quality_buckets():
    record = build_execution_record(
        ticker="MSFT",
        target_price=200.0,
        target_price_source="finance:signal_quote",
        decision_bar_close=200.0,
        notional_usd=10.0,
        avg_fill_price=199.9,  # filled below target -> favorable (negative)
        quantity_filled=1.0,
    )
    assert record.slippage_abs is not None
    assert record.slippage_pct is not None
    assert record.slippage_abs < 0
    assert abs(record.slippage_pct - (-0.05)) < 1e-9
    assert record.execution_quality == "excellent"
    assert record.latency_seconds is None  # no timestamps supplied


def test_execution_quality_buckets():
    assert classify_execution_quality(None) == "unfilled"
    assert classify_execution_quality(0.05) == "excellent"
    assert classify_execution_quality(-0.2) == "good"
    assert classify_execution_quality(0.4) == "acceptable"
    assert classify_execution_quality(1.5) == "poor"


def test_filled_record_against_a_zero_target_has_no_slippage():
    """Regression test for "Do not price recovered slippage against zero".
    A recovered crash-lease fire has no ReconciledDecision and so passes
    target_price=0.0 (a deliberate sentinel, not a real quote) while still
    supplying a real fill. Computing slippage_abs/slippage_cost against that
    zero priced the fill's entire notional as unfavorable slippage, inflating
    the run's aggregate net_slippage_cost with no real comparison behind it.
    Quantity/price/notional -- and a "filled" status -- must still be
    recorded; only the slippage fields are affected.
    """
    record = build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        avg_fill_price=96.5,
        quantity_filled=0.0725,
    )
    assert record.status == "filled"
    assert record.avg_fill_price == 96.5
    assert record.quantity_filled == 0.0725
    assert record.slippage_abs is None
    assert record.slippage_pct is None
    assert record.slippage_cost is None
    assert record.execution_quality == "unfilled"


def test_force_filled_marks_a_record_filled_without_price_or_quantity():
    """Regression test for "Mark executed-notional recoveries as filled". A
    supported terminal history row can report state="filled" and a positive
    executed_notional with no separate price/quantity breakdown. Without
    force_filled, such a recovery was recorded "submitted" despite being a
    broker-confirmed fill, undercounting filled_order_count/
    total_filled_notional. No price/quantity means no computable slippage
    either way.
    """
    record = build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        force_filled=True,
    )
    assert record.status == "filled"
    assert record.avg_fill_price is None
    assert record.quantity_filled is None
    assert record.slippage_abs is None
    assert record.slippage_cost is None


def test_unfilled_record_has_no_slippage():
    record = build_execution_record(
        ticker="NVDA",
        target_price=50.0,
        target_price_source="finance:signal_quote",
        decision_bar_close=50.0,
        notional_usd=5.0,
        avg_fill_price=None,
        quantity_filled=None,
    )
    assert record.status == "submitted"
    assert record.slippage_pct is None
    assert record.execution_quality == "unfilled"


def test_build_execution_record_tolerates_mixed_timezone_awareness_timestamps():
    """Regression test for "Contain invalid recovery timestamps after the
    fill lookup". A broker adapter's get_order_fill() can return an
    OrderFill whose submission and fill timestamps have mixed timezone
    awareness (one aware, one naive). datetime subtraction raises TypeError
    for that combination even though both values parsed fine individually --
    this runs after a crash-lease recovery already durably cleared the
    lease, so raising here would lose the recovered outcome with no lease
    left to retry it from. Latency must be left unavailable instead.
    """
    record = build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        avg_fill_price=96.5,
        quantity_filled=0.0725,
        order_submitted_at=datetime(2026, 7, 1, 15, 44, 0),
        order_filled_at=datetime(2026, 7, 1, 15, 44, 30, tzinfo=EASTERN),
    )
    assert record.status == "filled"
    assert record.latency_seconds is None


# --- Missing-fill classification ---------------------------------------------


def _decision(ticker, action):
    return ReconciledDecision(
        ticker=ticker,
        action=action,
        reason="x",
        finance_signal=None,
        robinhood_signal=None,
    )


def test_missing_fill_loop_reason_none_decision():
    assert (
        classify_missing_fill(None, MISSING_FILL_BUDGET_CAPPED)
        == MISSING_FILL_BUDGET_CAPPED
    )


def test_missing_fill_none_decision_no_loop_reason():
    assert classify_missing_fill(None, None) == MISSING_FILL_DATA_ERROR


def test_missing_fill_classification():
    assert (
        classify_missing_fill(_decision("A", Action.NO_DATA), None)
        == MISSING_FILL_DATA_ERROR
    )
    assert (
        classify_missing_fill(_decision("A", Action.SKIP), None)
        == MISSING_FILL_FAILED_TRIGGER
    )
    assert (
        classify_missing_fill(_decision("A", Action.NO_ACTION), None)
        == MISSING_FILL_FAILED_TRIGGER
    )
    assert (
        classify_missing_fill(_decision("A", Action.MANUAL_REVIEW), None)
        == MISSING_FILL_FAILED_TRIGGER
    )
    # loop reason takes precedence over the reconciled action.
    assert (
        classify_missing_fill(_decision("A", Action.BUY), MISSING_FILL_BUDGET_CAPPED)
        == MISSING_FILL_BUDGET_CAPPED
    )
    assert (
        classify_missing_fill(_decision("A", Action.BUY), MISSING_FILL_BROKER_BLOCKED)
        == MISSING_FILL_BROKER_BLOCKED
    )
    assert (
        classify_missing_fill(_decision("A", Action.BUY), MISSING_FILL_EXECUTION_GAP)
        == MISSING_FILL_EXECUTION_GAP
    )


def test_build_report_metrics_and_ranking():
    records = [
        build_execution_record(
            ticker="A",
            target_price=100.0,
            target_price_source="finance:signal_quote",
            decision_bar_close=100.0,
            notional_usd=10.0,
            avg_fill_price=101.0,
            quantity_filled=1.0,
        ),
        build_execution_record(
            ticker="B",
            target_price=100.0,
            target_price_source="finance:signal_quote",
            decision_bar_close=100.0,
            notional_usd=10.0,
            avg_fill_price=100.2,
            quantity_filled=1.0,
        ),
    ]
    run_result = RunResult(
        trading_day=date(2026, 6, 29),
        decisions=[_decision("C", Action.NO_DATA), _decision("D", Action.SKIP)],
        execution_records=records,
        loop_block_reasons={},
        active_universe=["A", "B", "C", "D"],
    )
    report = build_report(
        run_result, "manual", datetime(2026, 6, 29, 16, 0, tzinfo=EASTERN)
    )
    # A has larger abs slippage (1.0%) than B (0.2%), so A ranks first.
    assert [r.ticker for r in report.ranked_slippage] == ["A", "B"]
    assert report.top_slippage_tickers == ["A", "B"]
    assert report.metrics["filled_order_count"] == 2
    assert report.metrics["missing_fill_count"] == 2
    assert report.missing_fills["C"] == MISSING_FILL_DATA_ERROR
    assert report.missing_fills["D"] == MISSING_FILL_FAILED_TRIGGER
    assert cast(dict, report.metrics["missing_fill_reasons"])[MISSING_FILL_DATA_ERROR] == 1
    for key in [
        "mean_abs_slippage_pct",
        "median_abs_slippage_pct",
        "p90_abs_slippage_pct",
        "net_slippage_cost",
    ]:
        assert key in report.metrics


def test_build_report_total_filled_notional_falls_back_to_notional_usd():
    """Regression test for "Include executed-notional-only fills in the
    reported notional". A force_filled record confirmed only through
    executed_notional has no avg_fill_price/quantity_filled, so the granular
    product is 0.0 even though the order genuinely settled. total_filled_
    notional must fall back to the record's own notional_usd in that case
    instead of reporting a $0 contribution for a confirmed fill.
    """
    records = [
        build_execution_record(
            ticker="A",
            target_price=100.0,
            target_price_source="finance:signal_quote",
            decision_bar_close=100.0,
            notional_usd=10.0,
            avg_fill_price=101.0,
            quantity_filled=1.0,
        ),
        build_execution_record(
            ticker="B",
            target_price=0.0,
            target_price_source="",
            decision_bar_close=0.0,
            notional_usd=7.0,
            force_filled=True,
        ),
    ]
    run_result = RunResult(
        trading_day=date(2026, 6, 29),
        decisions=[],
        execution_records=records,
        loop_block_reasons={},
        active_universe=["A", "B"],
    )
    report = build_report(
        run_result, "manual", datetime(2026, 6, 29, 16, 0, tzinfo=EASTERN)
    )
    assert report.metrics["filled_order_count"] == 2
    assert report.metrics["total_filled_notional"] == pytest.approx(101.0 * 1.0 + 7.0)


def test_build_report_prefers_authoritative_recovered_notional_over_the_granular_product():
    """Regression test for "Prefer authoritative recovered notional in
    reports". A recovered crash-lease fire's notional_usd is the
    AUTHORITATIVE executed amount (im_recovery.recovered_amount_usd, sourced
    from _recovered_broker_notional's own executed_notional-first rule). It
    must win even when granular price/quantity ALSO happen to be present and
    their product merely disagrees with it due to independent rounding --
    e.g. a $7 executed notional with quantity 2 and a rounded $3 price would
    otherwise report only $6 through the product.
    """
    record = build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        action_reason="recovered_inverse_martingale_lease",
        avg_fill_price=3.0,
        quantity_filled=2.0,
        force_filled=True,
    )
    run_result = RunResult(
        trading_day=date(2026, 6, 29),
        decisions=[],
        execution_records=[record],
        loop_block_reasons={},
        active_universe=["AAPL"],
    )
    report = build_report(
        run_result, "manual", datetime(2026, 6, 29, 16, 0, tzinfo=EASTERN)
    )
    assert report.metrics["total_filled_notional"] == pytest.approx(7.0)


def test_build_report_excludes_unmeasured_fills_from_slippage_ranking():
    """Regression test for "Exclude unmeasured fills from slippage
    rankings". A force_filled record confirmed only through
    executed_notional has slippage_pct=None -- there is nothing to compare
    against a target with. Treating that as 0.0 for ranking purposes let an
    unmeasured recovery occupy a "Top Slippage Names" slot, or dominate the
    ranking entirely on a run with no measurable fills. It must still count
    toward filled_order_count, just not appear in the ranking.
    """
    records = [
        build_execution_record(
            ticker="B",
            target_price=0.0,
            target_price_source="",
            decision_bar_close=0.0,
            notional_usd=7.0,
            force_filled=True,
        ),
    ]
    run_result = RunResult(
        trading_day=date(2026, 6, 29),
        decisions=[],
        execution_records=records,
        loop_block_reasons={},
        active_universe=["B"],
    )
    report = build_report(
        run_result, "manual", datetime(2026, 6, 29, 16, 0, tzinfo=EASTERN)
    )
    assert report.metrics["filled_order_count"] == 1
    assert report.ranked_slippage == []
    assert report.top_slippage_tickers == []


def test_build_report_does_not_let_a_recovery_fill_mask_a_separate_candidate():
    """Regression test for "Do not let recovery fills cover separate
    same-ticker orders". A recovered crash-lease fill for an OLDER,
    unrelated AAPL order and a genuinely missing fill for a DISTINCT current
    AAPL candidate can coexist -- the synthetic recovery bookkeeping exists
    precisely so both can. Matching purely by ticker let the recovery mask
    the current candidate's own missing fill.
    """
    records = [
        build_execution_record(
            ticker="AAPL",
            target_price=0.0,
            target_price_source="",
            decision_bar_close=0.0,
            notional_usd=7.0,
            action_reason="recovered_inverse_martingale_lease",
            force_filled=True,
        ),
    ]
    current_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="graded_dca",
        finance_signal=None,
        robinhood_signal=None,
    )
    run_result = RunResult(
        trading_day=date(2026, 6, 29),
        decisions=[current_decision],
        execution_records=records,
        loop_block_reasons={},
        active_universe=["AAPL"],
    )
    report = build_report(
        run_result, "manual", datetime(2026, 6, 29, 16, 0, tzinfo=EASTERN)
    )
    assert "AAPL" in report.missing_fills, (
        "the current candidate's own decision (reason='graded_dca') is "
        "distinct from the recovery -- it must still report as missing"
    )


# --- Artifact / state-file fields --------------------------------------------


def test_generate_report_and_state_artifacts(tmp_path=None):
    tmpdir_obj = tempfile.TemporaryDirectory()
    workdir = str(tmp_path) if tmp_path is not None else tmpdir_obj.name
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(dry_run=True)
    state = StrategyState()
    # reference prices near the decision close so the dry-run broker simulates fills.
    broker = DryRunBroker(
        cash=500.0,
        reference_prices={t: 98.0 for t in config.tickers},
        simulated_slippage_pct=0.2,
    )
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))

    result = run_and_report(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        output_dir=os.path.join(workdir, "reports"),
        state_path=os.path.join(workdir, "state.json"),
        run_label="manual",
        scheduled=False,
    )

    assert os.path.exists(str(result["slippage_report_path"]))
    assert os.path.exists(str(result["orders_csv_path"]))
    assert os.path.exists(str(result["state_path"]))

    report_text = open(str(result["slippage_report_path"]), encoding="utf-8").read()
    assert "DCA Manual Run Slippage and Coverage Report" in report_text

    with open(str(result["state_path"]), encoding="utf-8") as handle:
        payload = json.load(handle)
    assert (
        payload["artifacts"]["slippage_report_path"] == result["slippage_report_path"]
    )
    assert payload["artifacts"]["orders_csv_path"] == result["orders_csv_path"]
    metrics = payload["summary_metrics"]
    for key in [
        "filled_order_count",
        "total_filled_notional",
        "net_slippage_cost",
        "mean_abs_slippage_pct",
        "median_abs_slippage_pct",
        "p90_abs_slippage_pct",
        "top_slippage_tickers",
        "missing_fill_count",
        "missing_fill_reasons",
    ]:
        assert key in metrics, f"missing metric {key}"
    # dry-run simulated fills should have produced at least one filled order.
    assert metrics["filled_order_count"] >= 1

    # CSV carries the durable per-order slippage fields.
    csv_text = open(str(result["orders_csv_path"]), encoding="utf-8").read()
    header = csv_text.splitlines()[0]
    for column in [
        "target_price",
        "slippage_pct",
        "slippage_cost",
        "latency_seconds",
        "execution_quality",
    ]:
        assert column in header


# --- Usage-proxy logging & compact daily mode --------------------------------


class _RaisingData:
    """Provider whose intraday fetch always raises, to exercise error counting."""

    def __init__(self, source):
        self.source = source

    def get_previous_close_bar(self, ticker, as_of):
        raise RuntimeError("boom")

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        raise RuntimeError("boom")


class _NotFoundData:
    """Provider that always raises a "ticker not found" style error.

    Mirrors a finance connector lookup failure for a Robinhood-only ticker
    such as GFUZ (see data_source_notes in config/tickers.json).
    """

    def __init__(self, source):
        self.source = source

    def get_previous_close_bar(self, ticker, as_of):
        raise LookupError(f"{ticker}: Not Found (404)")

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        raise LookupError(f"{ticker}: Not Found (404)")


def _compact_setup(workdir):
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL", "NVDA", "MSFT"),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    # Tight bid/ask (0.2% spread, well under the default 1% breaker) so these
    # compact-mode tests -- which exercise usage-proxy logging, kill-switch
    # accounting, confirmation-required handling, and top-up recording, not
    # spread behavior itself -- still reach a submitted/dry_run outcome under
    # the scheduled-run fail-closed-on-missing-spread breaker.
    rh = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )
    return now, config, state, broker, fin, rh


def test_usage_proxy_logged_in_run_state(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, notifications_sent=1),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    usage = run_state["usage_proxy"]
    for field in USAGE_PROXY_FIELDS:
        assert field in usage, f"missing usage-proxy field {field}"

    assert usage["tickers_scanned"] == 3
    assert usage["finance_calls_attempted"] == 3
    assert usage["robinhood_calls_attempted"] == 3
    assert usage["signal_rows_written"] == 6
    assert usage["buy_candidates_written"] == 3
    assert usage["order_reviews_attempted"] == 0
    assert usage["orders_placed_count"] == 3
    # signals.csv, buy_candidates.csv, and orders.csv all count toward the proxy.
    assert usage["csv_artifact_count"] == 3
    # The backfill re-serializes until the recorded size matches what is on disk,
    # and the total must cover the state file plus every counted CSV exactly.
    assert usage["state_file_bytes"] == os.path.getsize(
        os.path.join(artifacts_dir, "run_state.json")
    )
    csv_bytes = sum(
        os.path.getsize(os.path.join(artifacts_dir, name))
        for name in ("signals.csv", "buy_candidates.csv", "orders.csv")
    )
    assert usage["total_artifact_bytes"] == usage["state_file_bytes"] + csv_bytes
    assert usage["notifications_sent"] == 1
    assert usage["errors_count"] == 0
    assert usage["manual_recovery_required"] is False
    assert isinstance(usage["cost_control_notes"], list) and usage["cost_control_notes"]


def test_usage_proxy_counts_provider_errors(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, _fin, rh = _compact_setup(tmp_path)
    raising = _RaisingData(SourceName.FINANCE)

    run_daily_compact(
        now,
        config,
        state,
        raising,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    usage = run_state["usage_proxy"]

    # Finance raises once per ticker; run degrades to NO_DATA instead of crashing.
    assert usage["errors_count"] == 3
    assert usage["manual_recovery_required"] is True
    # Provider errors must never fall through to a Robinhood-only auto-buy.
    assert usage["orders_placed_count"] == 0
    assert usage["buy_candidates_written"] == 0
    assert run_state["counts"]["manual_review"] == 3
    assert sorted(run_state["manual_review_tickers"]) == ["AAPL", "MSFT", "NVDA"]
    # The artifact rows carry the provider_error flag, not just the reason text.
    finance_rows = [
        row for row in run_state["signal_rows"] if row["source"] == "finance"
    ]
    assert finance_rows and all(row["provider_error"] is True for row in finance_rows)


def test_finance_ticker_not_found_degrades_to_robinhood_only_buy(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, _fin, rh = _compact_setup(tmp_path)
    not_found = _NotFoundData(SourceName.FINANCE)

    run_daily_compact(
        now,
        config,
        state,
        not_found,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    usage = run_state["usage_proxy"]

    # A permanent "ticker not found" error on one source (e.g. GFUZ unresolved on the
    # finance connector) must not count as a provider error or force manual review the
    # way a genuine transient failure does (see test_usage_proxy_counts_provider_errors
    # above) -- the documented Robinhood-only BUY path still proceeds.
    assert usage["errors_count"] == 0
    assert usage["manual_recovery_required"] is False
    assert usage["orders_placed_count"] == 3
    finance_rows = [
        row for row in run_state["signal_rows"] if row["source"] == "finance"
    ]
    assert finance_rows and all(row["provider_error"] is False for row in finance_rows)
    assert all("not found" in row["reason"] for row in finance_rows)


def test_run_daily_compact_writes_structured_only(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    result = run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    assert result["compact"] is True
    for key in [
        "run_state_path",
        "signals_path",
        "buy_candidates_path",
        "orders_csv_path",
    ]:
        assert os.path.exists(result[key]), f"missing structured artifact: {key}"

    # The compact daily path must not emit the narrative markdown report or any
    # weekly/dashboard markdown — those are Friday/monthly-only.
    md_files = [f for f in os.listdir(artifacts_dir) if f.endswith(".md")]
    assert md_files == [], f"unexpected markdown on daily path: {md_files}"
    files = set(os.listdir(artifacts_dir))
    assert "run_state.json" in files
    assert "orders.csv" in files


def test_run_daily_compact_stamps_scheduled_run_classification(tmp_path):
    from scripts.run_classification import validate_classification

    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    rc = run_state["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["manual_run"] is False
    assert validate_classification(rc).ok


def test_run_daily_compact_manual_stamps_manual_run_classification(tmp_path):
    from scripts.run_classification import validate_classification

    artifacts_dir = os.path.join(tmp_path, "runs", "manual_2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=False),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    rc = run_state["run_classification"]
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert validate_classification(rc).ok


def test_deployment_history_root_engages_kill_switch_on_later_run(tmp_path):
    # Day 1: a scheduled run deploys real capital under a small monthly allocation
    # so its total trivially exceeds the 40% kill-switch threshold.
    history_root = os.path.join(tmp_path, "runs")
    day1_dir = os.path.join(history_root, "2026-06-29")
    # 2026-07-02 (not 2026-07-03, the observed Independence Day) so the new
    # NYSE-holiday execution gate doesn't block day 2 below.
    day1_dates = frozenset({date(2026, 6, 29), date(2026, 7, 2)})

    now1 = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        # dry_run=False so candidates land as status="submitted" (deployed_usd only
        # counts submitted candidates) -- DryRunBroker still simulates the fill.
        dry_run=False,
        tickers=("AAPL", "NVDA", "MSFT"),
        approved_test_dates=day1_dates,
        monthly_cash_allocation_usd=10.0,  # tiny allocation -> trivial to exceed 40%
    )
    state1 = StrategyState()
    broker1 = DryRunBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    fin1 = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now1))
    # Tight bid/ask so the scheduled-run fail-closed-on-missing-spread breaker
    # doesn't block this kill-switch scenario (spread behavior is not under test).
    rh1 = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now1), bid_ask=(97.9, 98.1)
    )
    run_daily_compact(
        now1,
        config,
        state1,
        fin1,
        rh1,
        broker1,
        options=CompactRunOptions(artifacts_dir=day1_dir),
    )

    with open(os.path.join(day1_dir, "run_state.json"), encoding="utf-8") as handle:
        day1_state = json.load(handle)
    day1_deployed = sum(
        c["planned_amount_usd"]
        for c in day1_state["buy_candidates"]
        if c["status"] == "submitted"
    )
    assert (
        day1_deployed > 0.4 * config.monthly_cash_allocation_usd
    )  # sanity: threshold is crossed

    # Day 2 (within the trailing 7 days): the kill-switch must engage from day 1's
    # history alone, capping every buy multiplier to 1x.
    day2_dir = os.path.join(history_root, "2026-07-02")
    now2 = datetime(2026, 7, 2, 15, 45, tzinfo=EASTERN)
    state2 = StrategyState()
    broker2 = DryRunBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    fin2 = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now2))
    rh2 = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now2), bid_ask=(97.9, 98.1)
    )
    run_daily_compact(
        now2,
        config,
        state2,
        fin2,
        rh2,
        broker2,
        options=CompactRunOptions(
            artifacts_dir=day2_dir, deployment_history_root=history_root
        ),
    )

    with open(os.path.join(day2_dir, "run_state.json"), encoding="utf-8") as handle:
        day2_state = json.load(handle)
    assert day2_state["risk_guard"]["kill_switch_engaged"] is True
    submitted = [c for c in day2_state["buy_candidates"] if c["status"] == "submitted"]
    assert submitted  # the guard caps sizing, it doesn't block buying entirely
    assert all(c["buy_multiplier"] == 1.0 for c in submitted)
    day2_deployed = sum(c["planned_amount_usd"] for c in submitted)
    # The reported trailing-7-day figure reflects day 1's history PLUS day 2's
    # own submissions as they were made (see
    # test_kill_switch_caps_later_candidates_within_the_same_batch for the
    # narrower in-batch behavior) -- not just the pre-run historical figure.
    assert day2_state["risk_guard"]["deployed_last_7d_usd"] == round(
        day1_deployed + day2_deployed, 2
    )


def test_apply_deployment_history_only_counts_trailing_7d(tmp_path):
    # Regression guard for the ⚡ Bolt fast-substring window filter in
    # _apply_deployment_history: only run_states whose trading_day is inside the
    # trailing KILL_SWITCH_WINDOW_DAYS ending on the as-of day may contribute to
    # the injected deployed_last_7d_usd. An artifact dated outside that window is
    # skipped before the expensive json.load and must not change the figure,
    # while an in-window artifact must still be parsed and counted.
    from near_close_dca import _apply_deployment_history
    from scripts.run_classification import (
        CRON_ID,
        SCHEDULED_FOR_ET,
        SCHEDULED_RUN_TYPE,
        SCHEDULED_WINDOW_END_ET,
        SCHEDULED_WINDOW_START_ET,
    )

    def _write_scheduled_state(day, deployed_usd):
        run_dir = os.path.join(str(tmp_path), "runs", day.isoformat())
        os.makedirs(run_dir, exist_ok=True)
        state = {
            "trading_day": day.isoformat(),
            "status": "ok",
            # generated_at is after 2:30 PM ET so it passes the afternoon-evidence
            # floor and counts as scheduled-live deployment.
            "generated_at": f"{day.isoformat()}T15:45:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": SCHEDULED_RUN_TYPE,
                "cron_id": CRON_ID,
                "scheduled_for_et": SCHEDULED_FOR_ET,
                "scheduled_window_start_et": SCHEDULED_WINDOW_START_ET,
                "scheduled_window_end_et": SCHEDULED_WINDOW_END_ET,
                "manual_run": False,
                "run_id": f"scheduled_{day.strftime('%Y%m%d')}_1530ET",
                "triggered_at_utc": f"{day.isoformat()}T19:45:00Z",
                "state_schema_version": "run-classification-v1",
            },
            "buy_candidates": [
                {
                    "ticker": "AAPL",
                    "status": "submitted",
                    "planned_amount_usd": deployed_usd,
                }
            ],
        }
        with open(
            os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(state, handle, indent=2)

    history_root = os.path.join(str(tmp_path), "runs")
    as_of = date(2026, 7, 21)
    _write_scheduled_state(as_of - timedelta(days=3), 30.0)  # in window -> counts
    _write_scheduled_state(as_of - timedelta(days=6), 12.0)  # window edge -> counts
    _write_scheduled_state(as_of - timedelta(days=7), 999.0)  # first day past window
    _write_scheduled_state(as_of - timedelta(days=40), 999.0)  # long-stale history

    config = StrategyConfig(monthly_cash_allocation_usd=3600.0)
    guarded = _apply_deployment_history(config, as_of, history_root)

    # Only the two in-window deployments (30.0 + 12.0) contribute; the day-7 and
    # day-40 artifacts are correctly excluded even though they are valid,
    # capital-deploying scheduled runs.
    assert guarded.deployed_last_7d_usd == 42.0


def test_same_day_ticker_deployment_already_recorded(tmp_path):
    # Underpins the deployment_history_root check in _process_buy_candidate's
    # duplicate-preserved branch (Codex review, PR #231: "Count broker-only
    # preserved orders in rolling deployment" and "Verify the recovered order
    # is actually in deployment history"): False exactly when THIS ticker's
    # submitted order hasn't been written to disk for today yet, so a
    # same-stream duplicate found via the broker is real, not-yet-counted
    # capital.
    from near_close_dca import _same_day_ticker_deployment_already_recorded

    history_root = os.path.join(str(tmp_path), "runs")
    trading_day = date(2026, 7, 21)

    # No artifact for trading_day anywhere on disk yet -> not recorded.
    assert (
        _same_day_ticker_deployment_already_recorded("AAPL", trading_day, history_root)
        is False
    )

    # A same-day artifact exists but records a DIFFERENT ticker (e.g. an
    # earlier invocation placed MSFT then crashed before a later invocation's
    # AAPL placement was ever written) -> date membership alone must not be
    # mistaken for proof AAPL's order was counted.
    _write_scheduled_afternoon_artifact(
        history_root,
        trading_day,
        [{"ticker": "MSFT", "status": "submitted", "planned_amount_usd": 5.0}],
    )
    assert (
        _same_day_ticker_deployment_already_recorded("AAPL", trading_day, history_root)
        is False
    )
    # MSFT itself, however, is recorded.
    assert (
        _same_day_ticker_deployment_already_recorded("MSFT", trading_day, history_root)
        is True
    )

    # Now AAPL's own submitted order is on disk for today -> recorded.
    _write_scheduled_afternoon_artifact(
        history_root,
        trading_day,
        [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 5.0}],
    )
    assert (
        _same_day_ticker_deployment_already_recorded("AAPL", trading_day, history_root)
        is True
    )


def test_kill_switch_caps_later_candidates_within_the_same_batch(tmp_path):
    # No prior-day history is needed: two same-day candidates each priced at
    # $3 (1.5x the $2 base on the 2% drop below) with a $5.00 monthly allocation
    # (40% threshold = $2) mean the FIRST submitted order alone already crosses
    # the threshold. The SECOND candidate in the same run must then be capped to
    # the $2 base instead of keeping its uncapped 1.5x amount -- otherwise a
    # volatile day could overshoot the guard well beyond the base amount before
    # the guard is even visible to tomorrow's run.
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAA", "BBB"),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        monthly_cash_allocation_usd=5.0,
    )
    state = StrategyState()
    broker = DryRunBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    submitted = [c for c in run_state["buy_candidates"] if c["status"] == "submitted"]
    assert len(submitted) == 2
    # AAA is filled first (alphabetical tiebreak at equal multiplier/drop) at
    # its full 1.5x amount, which alone crosses the $2 threshold.
    assert by_ticker["AAA"]["planned_amount_usd"] == 3.0
    assert by_ticker["AAA"]["buy_multiplier"] == 1.5
    # BBB, priced after the threshold is crossed, is capped to the $2 base --
    # even though its ladder multiplier (still reported for audit purposes)
    # remains 1.5, showing the cap is a dollar-amount guard, not a multiplier
    # rewrite.
    assert by_ticker["BBB"]["planned_amount_usd"] == 2.0
    assert by_ticker["BBB"]["buy_multiplier"] == 1.5


def test_manual_run_submissions_do_not_advance_in_batch_burn(tmp_path):
    # Same setup as the scheduled in-batch test above (AAA/BBB, $5.00 monthly
    # allocation), but scheduled=False: a manual run's own submissions must
    # never advance the scheduled-live 7-day figure the kill-switch reads, so
    # neither candidate is capped even though AAA's amount alone would cross
    # the $2 threshold if counted.
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-manual")
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAA", "BBB"),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        monthly_cash_allocation_usd=5.0,
    )
    state = StrategyState()
    broker = DryRunBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=False),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAA"]["planned_amount_usd"] == 3.0
    assert by_ticker["BBB"]["planned_amount_usd"] == 3.0  # NOT capped to the $2 base
    assert run_state["risk_guard"]["kill_switch_engaged"] is False


def _assert_csv_header(path):
    """The file exists AND carries a non-empty header row, not a zero-byte stub."""
    with open(path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle), None)
    assert header and any(col.strip() for col in header), f"{path} has no header row"


def test_run_daily_compact_date_gated_short_circuits(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-07-01")
    _now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    gated_now = datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN)  # not an approved date

    result = run_daily_compact(
        gated_now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    assert result["blocked"] is True
    assert os.path.exists(result["run_state_path"])
    with open(result["run_state_path"], encoding="utf-8") as handle:
        assert json.load(handle)["status"] == "blocked"
    # A blocked run still writes the COMPLETE root-level artifact set, but never
    # advertises buy_candidates/orders CSVs that run_once genuinely never wrote.
    for key in ["buy_candidates_path", "orders_csv_path"]:
        assert key not in result
    assert set(os.listdir(artifacts_dir)) == {
        "run_state.json",
        "run_summary.json",
        "signals.csv",
        "submitted_orders.csv",
    }
    _assert_csv_header(os.path.join(artifacts_dir, "submitted_orders.csv"))
    _assert_csv_header(os.path.join(artifacts_dir, "signals.csv"))


def test_run_daily_compact_holiday_gated_short_circuits(tmp_path):
    """An approved date that also lands on a full NYSE closure (e.g. an extended
    trading_window.json that widens approved_test_dates with no holiday
    subtraction of its own) must still report blocked=True, write the complete
    root-level artifact set, and never advertise buy_candidates/orders CSV paths
    that run_once never wrote."""
    labor_day = date(2026, 9, 7)
    artifacts_dir = os.path.join(tmp_path, "runs", str(labor_day))
    _now, _config, state, broker, fin, rh = _compact_setup(tmp_path)
    holiday_now = datetime(2026, 9, 7, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=_config.tickers,
        approved_test_dates=frozenset({labor_day}),
    )

    result = run_daily_compact(
        holiday_now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    assert result["blocked"] is True
    with open(result["run_state_path"], encoding="utf-8") as handle:
        assert json.load(handle)["status"] == "blocked"
    for key in ["buy_candidates_path", "orders_csv_path"]:
        assert key not in result
    assert set(os.listdir(artifacts_dir)) == {
        "run_state.json",
        "run_summary.json",
        "signals.csv",
        "submitted_orders.csv",
    }


def test_blocked_stub_is_contract_valid_but_never_scheduled_evidence(tmp_path):
    """A null-classification stub reads as malformed and fails the contract; one
    that passed as evidence would manufacture an evidence-shaped artifact for a
    day the market was shut. It must be neither (Codex review)."""
    from scripts.same_day_pointer import derive_valid_scheduled_evidence
    from scripts.scheduled_artifacts import validate_run_artifacts

    artifacts_dir = os.path.join(tmp_path, "runs", "2026-07-01")
    _now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    gated_now = datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN)  # not an approved date

    run_daily_compact(
        gated_now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    # Well-formed and attributable...
    assert isinstance(run_state["run_classification"], dict)
    assert run_state["order_submit_status"] == "no_orders_submitted"
    assert run_state["completion_reason"] == "date_or_holiday_gate_blocked"
    assert validate_run_artifacts(artifacts_dir, require_summary=True)
    # ...but never scheduled evidence.
    assert run_state["scheduled_evidence_denied"] is True
    assert derive_valid_scheduled_evidence(run_state) is False
    # And no top-level run_id: no run happened, so nothing keys off one.
    assert "run_id" not in run_state


def test_run_daily_compact_date_gate_does_not_clobber_prior_artifacts(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "reused")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )
    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        original = json.load(handle)
    assert "usage_proxy" in original

    gated_now = datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN)  # not an approved date
    result = run_daily_compact(
        gated_now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    assert result["blocked"] is True
    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        preserved = json.load(handle)
    # The blocked stub must never overwrite a previously recorded run.
    assert preserved == original


# --- run_classification wiring on the live path (task ce16ba6b state guarantee) ---


def test_build_run_artifacts_stamps_scheduled_classification(tmp_path):
    from scripts.run_classification import validate_classification

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    rc = run_state["run_classification"]
    assert validate_classification(rc).ok
    assert rc["run_origin"] == "scheduled"
    assert rc["manual_run"] is False
    # This run is a dry run (config.dry_run=True in _compact_setup), so no real
    # order can have been submitted regardless of buy candidates.
    assert run_state["order_submit_status"] == "dry_run"


def test_build_run_artifacts_stamps_manual_classification(tmp_path):
    from scripts.run_classification import validate_classification

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "manual-am")
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=False),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    rc = run_state["run_classification"]
    assert validate_classification(rc).ok
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True


def test_scheduled_compact_writes_and_validates_artifact_contract(tmp_path):
    # The scheduled 3:20 PM live path must write the full contract set and pass
    # validate_run_artifacts in production (not only in tests): run_state.json,
    # signals.csv, submitted_orders.csv, and a run_summary.json whose
    # run_classification mirrors run_state's.
    from pathlib import Path

    from scripts.scheduled_artifacts import validate_run_artifacts

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    d = Path(artifacts_dir)
    assert (d / "run_state.json").exists()
    assert (d / "signals.csv").exists()
    assert (d / "submitted_orders.csv").exists()
    assert (d / "run_summary.json").exists()

    run_state = json.loads((d / "run_state.json").read_bytes())
    summary = json.loads((d / "run_summary.json").read_bytes())
    # run_summary.run_classification mirrors run_state's exactly
    assert summary["run_classification"] == run_state["run_classification"]
    # the scheduled classification now carries the afternoon-contract fields
    rc = run_state["run_classification"]
    assert rc["trading_day"] == run_state["trading_day"]
    assert rc["same_day_required"] is True
    assert rc["valid_scheduled_evidence"] is True
    # the production contract validator passes on the written dir
    assert validate_run_artifacts(
        d, require_submitted_orders=True, require_summary=True
    )


def test_manual_compact_does_not_write_scheduled_contract_files(tmp_path):
    # The manual path is not the scheduled contract: it must not stamp the
    # scheduled-only evidence field nor write the scheduled contract artifacts.
    from pathlib import Path

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "manual-am")
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=False),
    )

    run_state = json.loads(Path(artifacts_dir, "run_state.json").read_bytes())
    rc = run_state["run_classification"]
    assert rc["run_origin"] == "manual"
    assert "valid_scheduled_evidence" not in rc
    assert not Path(artifacts_dir, "run_summary.json").exists()
    assert not Path(artifacts_dir, "submitted_orders.csv").exists()


def test_scheduled_contract_excludes_dry_run_orders_from_submitted_orders(tmp_path):
    # A scheduled dry run assigns synthetic DRY-RUN-SIM order_ids while keeping
    # status="dry_run"; those simulated orders must NOT land in submitted_orders.csv
    # (the drift detector would otherwise report them as real broker fills).
    import csv as _csv
    from pathlib import Path

    from near_close_dca import _write_and_validate_scheduled_contract

    artifacts = {
        "run_id": "2026-06-29-152000",
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T15:20:00-04:00",
        "broker": "robinhood",
        "dry_run": True,
        "order_submit_status": "dry_run",
        "run_classification": {"run_origin": "scheduled", "manual_run": False},
        "buy_candidates": [
            {
                "ticker": "A",
                "planned_amount_usd": 2.0,
                "status": "dry_run",
                "order_id": "DRY-RUN-SIM-A-2.00-x",
            },
            {
                "ticker": "B",
                "planned_amount_usd": 3.0,
                "status": "dry_run",
                "order_id": "DRY-RUN-SIM-B-3.00-x",
            },
        ],
    }
    d = Path(tmp_path)
    (d / "run_state.json").write_text(json.dumps(artifacts), encoding="utf-8")
    (d / "signals.csv").write_text("ticker\nA\nB\n", encoding="utf-8")

    result = _write_and_validate_scheduled_contract(artifacts, d)
    assert result  # dry_run is exempt from the compute-only check; contract passes
    rows = list(_csv.DictReader((d / "submitted_orders.csv").open()))
    assert rows == []  # header only -- no simulated dry-run orders recorded


def test_contract_failure_persists_invalid_evidence_marker(tmp_path):
    # A contract-rejected scheduled run must be marked invalid IN the run_state, so
    # consumers that discover the file directly (not via the pointer) also exclude
    # it: select_same_day_run_state (derive_valid_scheduled_evidence) and the
    # weekly dashboard's load_json_files (drops status=='blocked').
    from pathlib import Path

    from dca_weekly_dashboard import load_json_files
    from near_close_dca import _write_and_validate_scheduled_contract
    from scripts.same_day_pointer import derive_valid_scheduled_evidence

    artifacts = {
        "run_id": "2026-06-29-152000",
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T15:20:00-04:00",
        "broker": "robinhood",
        "dry_run": False,
        "order_submit_status": "submitted",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
            "valid_scheduled_evidence": True,
        },
        "buy_candidates": [
            {
                "ticker": "A",
                "planned_amount_usd": 2.0,
                "order_id": "o1",
                "status": "submitted",
            },
            # positive planned row left unplaced -> compute-only terminal failure
            {"ticker": "B", "planned_amount_usd": 3.0, "status": "not_submitted"},
        ],
    }
    d = Path(tmp_path)
    (d / "run_state.json").write_text(json.dumps(artifacts), encoding="utf-8")
    (d / "signals.csv").write_text("ticker\nA\nB\n", encoding="utf-8")

    result = _write_and_validate_scheduled_contract(artifacts, d)
    assert not result  # partial unplaced planned order -> contract failure

    persisted = json.loads((d / "run_state.json").read_bytes())
    assert persisted["status"] == "blocked"
    assert (
        persisted["artifact_contract_failure"]["failure"] == "artifact_contract_failure"
    )
    assert persisted["artifact_contract_failure"]["compute_only"] is True
    assert persisted["run_classification"]["valid_scheduled_evidence"] is False
    # The run as a whole is still denied as valid COMPLETION evidence...
    assert derive_valid_scheduled_evidence(persisted) is False
    # ...but "A"'s real submission must not vanish from the weekly dashboard's
    # order/notional metrics just because "B" made the run invalid overall
    # (compute_only describes completion validity, not whether an individual
    # row's placement is trustworthy).
    kept = load_json_files([d / "run_state.json"])
    assert len(kept) == 1
    assert kept[0]["buy_candidates"][0]["ticker"] == "A"


def test_contract_failure_blocks_scheduled_pointer_update(tmp_path, monkeypatch):
    # A run that breached the artifact contract must NOT be recorded as valid
    # scheduled evidence: the latest_scheduled_run pointer update is skipped even
    # though the run_classification itself is well-formed.
    from pathlib import Path

    import near_close_dca
    from scripts.run_classification import latest_scheduled_run_path
    from scripts.scheduled_artifacts import ArtifactContractResult

    monkeypatch.setattr(
        near_close_dca,
        "_write_and_validate_scheduled_contract",
        lambda artifacts, artifacts_dir: ArtifactContractResult(
            False, ["forced contract failure"]
        ),
    )

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    classification_root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir,
            scheduled=True,
            classification_state_root=classification_root,
        ),
    )
    # run_state.json is still written, but the invalid run is not pointed at.
    assert Path(artifacts_dir, "run_state.json").exists()
    assert not latest_scheduled_run_path(Path(classification_root)).exists()


def test_scheduled_run_updates_latest_scheduled_run_pointer(tmp_path):
    from pathlib import Path

    from scripts.run_classification import latest_scheduled_run_path

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    classification_root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir,
            scheduled=True,
            classification_state_root=classification_root,
        ),
    )

    pointer = latest_scheduled_run_path(Path(classification_root))
    assert pointer.exists()
    ptr = json.loads(pointer.read_bytes())
    # Points at the run_state.json this run actually wrote to artifacts_dir, not
    # a second, separately-computed copy under classification_root.
    assert ptr["run_state_path"] == os.path.join(artifacts_dir, "run_state.json")
    assert not (Path(classification_root) / "scheduled_runs").exists()


def test_manual_run_does_not_touch_latest_scheduled_run_pointer(tmp_path):
    from scripts.run_classification import latest_scheduled_run_path

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "manual-am")
    classification_root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir,
            scheduled=False,
            classification_state_root=classification_root,
        ),
    )

    assert not latest_scheduled_run_path(pathlib.Path(classification_root)).exists()


def test_confirmation_required_broker_response_recorded_distinctly(tmp_path):
    from near_close_dca import OrderConfirmationRequiredError

    class _ConfirmationRequiredBroker(DryRunBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise OrderConfirmationRequiredError("broker requires step-up confirmation")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False,
        tickers=config.tickers,
        approved_test_dates=config.approved_test_dates,
    )
    broker = _ConfirmationRequiredBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert run_state["order_submit_status"] == "not_submitted_confirmation_required"
    for candidate in run_state["buy_candidates"]:
        assert candidate["status"] == "not_submitted_confirmation_required"
        assert candidate["order_id"] is None
        # Scheduled runs place directly (no pre-placement review), so reviewed_order is None.
        assert candidate["reviewed_order"] is None
    # Not counted as an error: the broker responded normally, just requiring
    # confirmation, so nothing needs retrying and no alert should fire on this basis.
    assert run_state["usage_proxy"]["errors_count"] == 0


def test_confirmation_required_holds_do_not_consume_the_daily_attempt_cap(tmp_path):
    """Codex review, PR #786 ("Exclude confirmation holds from the daily
    attempt count").

    The batch loop's `max_orders_per_day` funnel charged one attempt for
    ANY outcome carrying `broker_placement_failed=True`, and a confirmation-
    required hold carries that marker too (for the different, legitimate
    reasons `_outcome_records_a_broker_attempt` gives -- preserving it
    through a recovery merge, and counting it toward the broker-health
    breaker). But `OrderConfirmationRequiredError`'s own contract is "no
    order was attempted and nothing needs retrying by this run", and
    `_orders_csv_attempt_counts` (the CROSS-RUN reconstruction from
    artifacts) already excludes this exact status for that reason. Charging
    it here anyway meant a run with several confirmation-required tickers
    and a small `max_orders_per_day` could exhaust the cap on holds that
    never reached a real placement decision, blocking a later, genuinely
    tradable candidate in the SAME run -- while restarting from this same
    day's artifacts would have reconstructed zero attempts and let it
    through.
    """
    from near_close_dca import OrderConfirmationRequiredError

    class _ConfirmationRequiredBroker(DryRunBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise OrderConfirmationRequiredError("broker requires step-up confirmation")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False,
        tickers=config.tickers,
        approved_test_dates=config.approved_test_dates,
        max_orders_per_day=1,
    )
    broker = _ConfirmationRequiredBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    # All three tickers (AAPL, NVDA, MSFT) held for confirmation despite a
    # max_orders_per_day cap of 1 -- none of them consumed a slot.
    assert len(run_state["buy_candidates"]) == 3
    for candidate in run_state["buy_candidates"]:
        assert candidate["status"] == "not_submitted_confirmation_required"
    assert state.day_order_count(date(2026, 6, 29)) == 0


def test_scheduled_no_order_run_records_completed_no_orders_not_a_failure(tmp_path):
    """No ticker qualifies on the ladder, so nothing reaches the spread gate: the
    run completed with no orders and must NOT be blamed on missing spread data
    (Jul 27 2026 1:30 PM ET)."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 100.0})
    # Price at the prior close: a 0% drop earns no ladder multiplier.
    fin = _FakeData(SourceName.FINANCE, 100.0, 100.0, quote=(100.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 100.0, 100.0, quote=(100.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "no-candidates")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert run_state["run_classification_outcome"] == "completed_no_orders"
    assert run_state["completion_reason"] == "no_ladder_candidates"
    # The contract assigns this case 'no_orders_submitted'; the legacy
    # not_submitted_no_candidates status would contradict it and disagree with
    # the morning stream for the same situation.
    assert run_state["order_submit_status"] == "no_orders_submitted"
    assert run_state["error_class"] is None
    assert run_state["candidates_reaching_spread_gate"] == 0
    assert run_state["candidates_blocked_missing_spread"] == 0
    assert run_state["is_trading_failure"] is False
    assert run_state["orders_submitted_count"] == 0


def test_scheduled_run_blocked_only_by_missing_spread_records_missing_spread_data(
    tmp_path,
):
    """Candidates DID reach the gate and every one was failed closed there, so the
    run legitimately carries error_class='missing_spread_data' -- still a safe
    no-order skip, not a trading failure."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    # Plain _FakeData has no get_bid_ask at all, so the spread cannot be proven.
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "spread-blocked")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["spread_ok"] is False
    assert candidate["spread_status"] == "spread_unavailable"
    assert run_state["run_classification_outcome"] == "completed_no_orders"
    assert run_state["error_class"] == "missing_spread_data"
    assert run_state["completion_reason"] is None
    assert run_state["candidates_reaching_spread_gate"] == 1
    assert run_state["candidates_blocked_missing_spread"] == 1
    assert run_state["is_trading_failure"] is False


def test_confirmation_required_status_survives_the_no_order_classification(tmp_path):
    """A concrete status the placement path derived is more specific than
    'no orders' and must not be overwritten, or is_confirmation_blocked breaks."""
    from near_close_dca import OrderConfirmationRequiredError

    class _ConfirmationRequiredBroker(DryRunBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise OrderConfirmationRequiredError("broker requires step-up confirmation")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False,
        tickers=config.tickers,
        approved_test_dates=config.approved_test_dates,
    )
    broker = _ConfirmationRequiredBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "confirmation")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert run_state["order_submit_status"] == "not_submitted_confirmation_required"
    # The outcome fields are still recorded alongside it.
    assert run_state["run_classification_outcome"] == "completed_no_orders"
    assert run_state["orders_submitted_count"] == 0


def test_an_ambiguous_placement_raise_is_reconciled_against_its_ref_id():
    """A raise from place_market_buy does not prove the broker created nothing.

    `RobinhoodBroker.place_market_buy` is string-or-raise and raises a plain
    `RuntimeError` both for a broker refusing the ticker outright -- the
    reported RVII incident, where isolating is the whole point -- and for a
    connector that died while carrying an acceptance back. The exception cannot
    tell those apart (Codex review, PR #786: "Halt on ambiguous fractional
    placement exceptions").

    Halting at the `except` site is not available, and measuring says so rather
    than arguing it: the batch's own last-resort net turns a raise from here
    straight back into an isolated `failed` row, so re-raising changes nothing.
    What IS available is the deterministic per-order `ref_id`.

    Reading the id it finds is NOT the same as resolving the ambiguity, which
    took a second round to see -- hence three verdicts rather than a nullable
    id."""
    import types

    import near_close_dca

    ref = "dca-graded-1530-2026-08-25-AAPL"
    day = date(2026, 8, 25)

    def ctx_with(lookup, *, has_method=True):
        broker = types.SimpleNamespace()
        if has_method:
            broker.same_day_order_with_ref_id_prefix_any_state = lookup
        return types.SimpleNamespace(broker=broker, trading_day=day)

    def verdict(match):
        return near_close_dca._reconcile_ambiguous_fractional_placement(
            ctx_with(lambda t, d, refs, m=match: dict(m) if m else None), "AAPL", ref
        )

    ACCEPTED = near_close_dca._PLACEMENT_ACCEPTED
    NO_ORDER = near_close_dca._PLACEMENT_NO_ORDER
    UNKNOWN = near_close_dca._PLACEMENT_UNKNOWN

    # A working or filled order is a real order this run must account for --
    # the connector-timeout case the finding named.
    assert verdict({"id": "ORD", "state": "queued"}) == (ACCEPTED, "ORD")
    assert verdict({"id": "ORD", "state": "filled", "cumulative_quantity": "3"}) == (ACCEPTED, "ORD")
    assert verdict({"order_id": "ORD2", "state": "queued"}) == (ACCEPTED, "ORD2")

    # "Not terminal" is NOT the same as "working": a partial row carrying a
    # usable id and a missing, empty or unrecognized state was classified
    # accepted and booked for the full requested notional, when an incomplete
    # terminal rejection has exactly that shape (Codex review, PR #786:
    # "Require a recognized state before booking the order"). The recognized
    # live/filled states are an ALLOWLIST now.
    for state in ("new", "confirmed", "unconfirmed", "partially_filled", " QUEUED "):
        assert verdict({"id": "ORD", "state": state}) == (ACCEPTED, "ORD"), state
    for partial in ({"id": "ORD"}, {"id": "ORD", "state": ""},
                    {"id": "ORD", "state": None},
                    {"id": "ORD", "state": "weird_broker_state"},
                    {"id": "ORD", "state": "partially_rejected"}):
        assert verdict(partial) == (UNKNOWN, "ORD"), partial

    # A DECLINED terminal order was never working, so nothing executed. This
    # is the reported RVII refusal's own shape once the broker recorded an id
    # for it, and the first version of this function booked it as a successful
    # submission for the full requested notional (Codex review, PR #786:
    # "Reject terminal history matches instead of booking submissions").
    for state in ("rejected", "failed", "voided", " REJECTED "):
        assert verdict({"id": "ORD", "state": state}) == (NO_ORDER, "ORD"), state
        # ... unless the row ALSO reports a fill. "Rejected means it was never
        # working, so it cannot have executed" is true of a self-consistent
        # broker row and not of the row in front of us:
        # `_recovered_broker_notional` already treats positive execution
        # evidence on every settled state as real settled spend, and
        # discarding it here marked the placement proven-failed while the fill
        # sat outside deployment and cap accounting (Codex review, PR #786:
        # "Inspect fills before declaring declined orders unplaced"). Round
        # 29's rule again: a carve-out no wider than its justification.
        for fill in ({"cumulative_quantity": "3"}, {"executed_notional": "70.08"},
                     {"cumulative_quantity": "oops"}, {"cumulative_quantity": False}):
            assert verdict({"id": "ORD", "state": state, **fill}) == (UNKNOWN, "ORD"), (state, fill)
        # An explicit zero, or no fill fields at all, still proves it -- an
        # absent figure is expected on a declined order rather than suspicious.
        assert verdict(
            {"id": "ORD", "state": state, "executed_notional": "0", "cumulative_quantity": "0"}
        ) == (NO_ORDER, "ORD"), state

    # An id this run cannot TRACK is not an id. `_is_blank_broker_field` covers
    # None/""/whitespace, and every other JSON-valid but unusable shape
    # survived `str(...)` as a nonempty string -- `true` became "True", `{}`
    # became "{}", and 0/-1/NaN became ordinary-looking numeric ids, each
    # recorded as a successful submission carrying a reference no later
    # reconciliation can resolve (Codex review, PR #786: "Keep unusable
    # reconciled IDs untrackable").
    for bad in (True, {}, [], 0, -1, float("nan"), float("inf"), "", "   ", None):
        assert verdict({"id": bad, "state": "queued"}) == (UNKNOWN, None), bad
    # The same rules `dca_runner._is_usable_order_id` applies, so a positive
    # finite number and a nonblank string are still ids.
    for good in ("ORD", " ORD ", 12345, 1.0):
        assert verdict({"id": good, "state": "queued"}) == (ACCEPTED, str(good).strip()), good
    # ... and an unusable `id` falls through to `order_id` rather than
    # short-circuiting the search.
    assert verdict({"id": True, "order_id": "ORD2", "state": "queued"}) == (ACCEPTED, "ORD2")

    # cancelled/canceled/expired mean the order EXISTED and was then killed,
    # so only an EXPLICIT zero rules a partial fill out.
    for state in ("cancelled", "canceled", "expired"):
        assert verdict({"id": "ORD", "state": state, "cumulative_quantity": "0"}) == (
            NO_ORDER, "ORD",
        ), state
    # ... and an absent, blank or unreadable figure leaves it UNKNOWN, never
    # zero. The id is carried so the order stays reconcilable by hand: booking
    # the full requested notional would overstate real spend, booking nothing
    # would lose it.
    for fill in ({"cumulative_quantity": "2"}, {}, {"cumulative_quantity": "  "},
                 {"cumulative_quantity": "oops"},
                 # A BOOLEAN is malformed evidence, not a measurement, and
                 # `float(False)` is `0.0` -- so `cumulative_quantity: false`
                 # read as a proven-unfilled order and let the caller call the
                 # placement a proven failure. Round 33 caught the mirror of
                 # this in `float(True) == 1.0` loading as a $1 ceiling (Codex
                 # review, PR #786: "Reject boolean zero-fill evidence").
                 {"cumulative_quantity": False},
                 {"cumulative_quantity": True}):
        assert verdict({"id": "ORD", "state": "cancelled", **fill}) == (UNKNOWN, "ORD"), fill
    # A genuine numeric zero, in every spelling, still proves it.
    for zero in (0, 0.0, "0", "0.0", " 0 "):
        assert verdict({"id": "ORD", "state": "cancelled", "cumulative_quantity": zero}) == (
            NO_ORDER, "ORD",
        ), zero

    # An explicit zero on any of the OTHER three supported notional aliases
    # -- with NO `cumulative_quantity` field at all -- proves it exactly the
    # same way. This branch used to check `cumulative_quantity` alone, so
    # `{"id": "ORD", "state": "cancelled", "executed_notional": "0"}` found
    # nothing here and fell through to UNKNOWN, even though
    # `_reconciled_order_may_have_executed` (checked just above this branch)
    # already proves no field on the row indicates a possible fill (Codex
    # review, PR #786: "Honor zero execution notionals on cancelled
    # recoveries").
    for state in ("cancelled", "canceled", "expired"):
        for zero_field in ("executed_notional", "executed_dollar_amount", "filled_notional"):
            assert verdict({"id": "ORD", "state": state, zero_field: "0"}) == (
                NO_ORDER, "ORD",
            ), (state, zero_field)
    # Mapping-wrapped money form (`_reconciled_order_may_have_executed`'s own
    # unwrap): proves it identically.
    assert verdict({"id": "ORD", "state": "cancelled", "executed_notional": {"amount": "0"}}) == (
        NO_ORDER, "ORD",
    )
    # Any OTHER field on the same row still positively contradicting the
    # zero-proof halts it to UNKNOWN, exactly as a positive
    # `cumulative_quantity` would -- this is `_reconciled_order_may_have_
    # executed`'s own guard, not a new rule, so it is asserted here only as
    # a boundary check.
    assert verdict(
        {"id": "ORD", "state": "cancelled", "executed_notional": "0", "filled_notional": "5"}
    ) == (UNKNOWN, "ORD")

    # A NEGATIVE figure is malformed evidence, not a measurement of "nothing
    # was bought". `parsed > 0` read `-5` as proof of no fill, so a DECLINED
    # row carrying one returned NO_ORDER and the batch continued with
    # whatever that row really describes outside cap accounting (Codex
    # review, PR #786: "Treat negative fill measurements as ambiguous").
    #
    # Both siblings already read a negative this way, which is what makes
    # this the outlier rather than a new rule -- asserted here so the three
    # cannot drift apart again.
    for fill_field in ("executed_notional", "cumulative_quantity"):
        for negative in (-5, -0.01, "-5", -1e-9):
            assert verdict(
                {"id": "ORD", "state": "rejected", fill_field: negative}
            ) == (UNKNOWN, "ORD"), (fill_field, negative)
            assert verdict(
                {"id": "ORD", "state": "cancelled", fill_field: negative}
            ) == (UNKNOWN, "ORD"), (fill_field, negative)
    # Imported locally: `near_close_dca` does not depend on `dca_runner`, and
    # this is a cross-module drift check rather than a use.
    import dca_runner

    assert (
        dca_runner._combined_fill_evidence([{"cumulative_quantity": -1}])
        == dca_runner._FILL_EVIDENCE_POSITIVE_OR_UNREADABLE
    )
    assert dca_runner.BrokerOrder(
        symbol="AAPL", side="buy", dollar_amount=2.0,
        created_at="2026-08-16T15:45:00Z", state="rejected",
        executed_dollar_amount=-5.0,
    ).counts_toward_budget is True
    # `-0.0` IS zero, and still proves it.
    assert verdict({"id": "ORD", "state": "cancelled", "cumulative_quantity": -0.0}) == (
        NO_ORDER, "ORD",
    )

    # A match history POSITIVELY FOUND but whose id is unusable is ambiguity,
    # not absence -- round 39's rule, broken in new code in the round that
    # cited it (Codex review, PR #786: "Preserve visible matches that lack a
    # usable ID"). Note a whitespace-only id previously passed through as
    # usable.
    for bad in ({"id": "", "state": "queued"}, {"id": "   ", "state": "queued"},
                {"state": "queued"}, {"id": None, "order_id": None, "state": "queued"}):
        assert verdict(bad) == (UNKNOWN, None), bad

    # A clean miss proves nothing: this module documents Robinhood's order
    # history as eventually consistent, so a just-accepted order may simply not
    # be indexed yet (Codex review, PR #786: "Do not treat an immediate history
    # miss as proof of rejection").
    assert verdict(None) == (UNKNOWN, None)

    # And a history that could not be READ is a fourth thing again -- it
    # raises, so the caller can tell "learned nothing" from "found nothing".
    def unprovable(t, d, refs):
        raise RuntimeError("order history could not be proven complete")

    with pytest.raises(RuntimeError):
        near_close_dca._reconcile_ambiguous_fractional_placement(
            ctx_with(unprovable), "AAPL", ref
        )
    with pytest.raises(RuntimeError):
        near_close_dca._reconcile_ambiguous_fractional_placement(
            ctx_with(None, has_method=False), "AAPL", ref
        )

    # The match is on the EXACT reference, so another day's or another
    # stream's order for the same ticker cannot be mistaken for this one.
    ctx = ctx_with(lambda t, d, refs: None if ref in refs else {"id": "OTHER"})
    assert near_close_dca._reconcile_ambiguous_fractional_placement(ctx, "AAPL", ref) == (
        UNKNOWN, None,
    )


def test_an_unresolved_placement_cannot_halt_without_the_adapter_distinction():
    """Why an UNKNOWN verdict isolates rather than stopping the batch.

    The hazard is real: a connector that times out after the broker accepted
    leaves an order live and unindexed, so an immediate history miss returns
    UNKNOWN and the batch keeps placing against arithmetic missing that spend
    (Codex review, PR #786: "Stop the batch while placement remains
    unresolved").

    Halting on UNKNOWN was implemented and measured, and it **re-creates the
    reported incident**. A broker refusing one ticker outright creates no
    order, so a fully working, readable order history truthfully reports no
    match -- the identical UNKNOWN a not-yet-indexed acceptance produces. The
    two are indistinguishable *even after querying history*, because both look
    like absence. Halting there turns every ordinary refusal into
    `GS, EVR: never submitted -- batch halted after RVII rejection`, which is
    the failure this PR exists to remove.

    That is round 22's lesson one level deeper, and it has the same remedy:
    the distinction has to survive the adapter. A `ProvenNoOrderPlacementError`
    raised where the response proves no order lets a refusal be proven without
    consulting history at all, leaving a history miss genuinely ambiguous and
    safe to halt on. Until then UNKNOWN isolates, records
    `placement_outcome_unproven`, and flags the run for a human.

    This test pins the measurement so the halt is not re-attempted without
    seeing it."""
    import datetime
    import types

    import near_close_dca

    # A broker whose history works perfectly and truthfully reports that the
    # refused order does not exist -- the reported RVII shape.
    broker = types.SimpleNamespace(
        same_day_order_with_ref_id_prefix_any_state=lambda t, d, refs: None
    )
    ctx = types.SimpleNamespace(broker=broker, trading_day=datetime.date(2026, 8, 25))

    verdict, order_id = near_close_dca._reconcile_ambiguous_fractional_placement(
        ctx, "RVII", "dca-graded-1530-2026-08-25-RVII"
    )
    assert (verdict, order_id) == (near_close_dca._PLACEMENT_UNKNOWN, None)

    # A not-yet-indexed acceptance produces the byte-identical verdict, which
    # is the whole difficulty.
    assert near_close_dca._reconcile_ambiguous_fractional_placement(
        ctx, "AAPL", "dca-graded-1530-2026-08-25-AAPL"
    ) == (near_close_dca._PLACEMENT_UNKNOWN, None)

    # So the batch loop must NOT carry an unresolved-placement halt: nothing
    # in the module may stop the batch on this verdict while the two cases are
    # indistinguishable.
    import inspect

    batch_source = inspect.getsource(near_close_dca._process_buy_candidates_batch)
    assert "_IN_FLIGHT_UNRESOLVED_PLACEMENTS" not in batch_source
    assert "batch halted because" not in batch_source


def test_attempt_accounting_is_independent_of_reconciliation_readability():
    """History readability classifies broker health, not whether a call occurred.

    The two come apart, and getting it wrong breaks the breaker in one
    direction or the other. A readable history that named no order means this
    placement produced no order this run can see -- the ordinary refusal, and
    exactly what the breaker exists to count, since an outage times out every
    placement and leaves a readable empty history. A history this run could not
    read taught it nothing about broker health, but the placement call still
    consumed a daily attempt and must advance the consecutive call-failure
    breaker.  A separate marker preserves both facts.

    `placement_outcome_proven` is the separate question, and only a terminal
    verdict answers it: a clean miss is not proof, because the history is
    eventually consistent."""
    import inspect

    import near_close_dca

    source = inspect.getsource(near_close_dca._process_buy_candidate)
    assert '"broker_placement_failed": history_readable,' in source
    assert '"broker_placement_attempted": True,' in source
    assert (
        "placement_outcome_proven = history_readable and verdict == _PLACEMENT_NO_ORDER"
        in source
    )
    assert '"placement_outcome_unproven": not placement_outcome_proven,' in source
    # The found-but-unbookable order's id survives into the row, or it becomes
    # unreconcilable by hand.
    assert '"order_id": reconciled_order_id,' in source
    # And the failure reason carries the exception TYPE only -- this string is
    # persisted verbatim into run_state.json and buy_candidates.csv.
    assert 'f"placement failed: {type(exc).__name__}"' in source
    assert 'f"placement outcome unproven: {type(exc).__name__}"' in source

    batch_source = inspect.getsource(near_close_dca._process_buy_candidates_batch)
    assert "raw_broker_call_attempted" in batch_source


def test_afternoon_placement_failures_are_recorded_as_a_trading_failure(tmp_path):
    """Every qualifying placement raised: a genuine failure, not a healthy
    no-order run (Codex review)."""

    class _FailingBroker(DryRunBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise RuntimeError("broker rejected the order")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False,
        tickers=config.tickers,
        approved_test_dates=config.approved_test_dates,
    )
    broker = _FailingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "placement-failed")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert (
        run_state["run_classification_outcome"]
        == "failed_after_connector_native_recovery_attempt"
    )
    assert run_state["error_class"] == "failed_after_connector_native_recovery_attempt"
    assert run_state["is_trading_failure"] is True
    assert run_state["order_submit_status"] == "not_submitted_error"


def test_afternoon_run_summary_carries_the_no_order_outcome_fields(tmp_path):
    """A summary-only consumer must be able to tell a healthy zero-order run from
    a failure (Codex review)."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 100.0})
    fin = _FakeData(SourceName.FINANCE, 100.0, 100.0, quote=(100.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 100.0, 100.0, quote=(100.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "summary-fields")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_summary.json"), encoding="utf-8"
    ) as handle:
        summary = json.load(handle)
    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert summary["run_classification_outcome"] == "completed_no_orders"
    assert summary["completion_reason"] == "no_ladder_candidates"
    assert summary["error_class"] is None
    assert summary["is_trading_failure"] is False
    assert summary["orders_submitted_count"] == 0
    assert summary["run_classification"] == run_state["run_classification"]


def test_blocked_stub_completes_a_partial_legacy_directory(tmp_path):
    """A directory left holding only run_state.json (interrupted or legacy write)
    must be COMPLETED on the next blocked pass, not left partial forever
    (Codex review)."""
    artifacts_dir = pathlib.Path(tmp_path) / "runs" / "partial"
    artifacts_dir.mkdir(parents=True)
    legacy = json.dumps({"trading_day": "2026-07-01", "status": "blocked"})
    (artifacts_dir / "run_state.json").write_text(legacy, encoding="utf-8")

    _now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    gated_now = datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN)  # not an approved date

    run_daily_compact(
        gated_now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=str(artifacts_dir)),
    )

    # The pre-existing run_state is preserved byte for byte...
    assert (artifacts_dir / "run_state.json").read_text(encoding="utf-8") == legacy
    # ...and the missing companions are now written.
    for name in ("run_summary.json", "signals.csv", "submitted_orders.csv"):
        assert (artifacts_dir / name).exists(), f"{name} still missing"
    _assert_csv_header(str(artifacts_dir / "signals.csv"))
    _assert_csv_header(str(artifacts_dir / "submitted_orders.csv"))
    summary = json.loads(
        (artifacts_dir / "run_summary.json").read_text(encoding="utf-8")
    )
    # The summary mirrors what is actually on disk, not the fresh stub.
    assert summary["trading_day"] == "2026-07-01"


def test_provider_errors_are_not_recorded_as_a_healthy_no_order_run(tmp_path):
    """_evaluate_all_signals degrades a raising provider's tickers to NO_DATA and
    bumps errors_count; the zero-order result must not read as a clean ladder
    no-op (Codex review)."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})

    class _RaisingProvider(_FakeData):
        def get_previous_close_bar(self, ticker, as_of):
            raise RuntimeError("provider is down")

    fin = _RaisingProvider(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _RaisingProvider(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "provider-errors")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert run_state["usage_proxy"]["errors_count"] > 0
    assert run_state["run_classification_outcome"] == "completed_no_orders"
    # Not a null error_class: the run was degraded, not a clean ladder no-op.
    assert run_state["error_class"] == "degraded_market_data"


# --- Per-ticker error isolation ----------------------------------------------
# One symbol that blows up mid-evaluation must cost only itself. Before the
# batch-level net, an unhandled raise inside _process_buy_candidate escaped
# _process_buy_candidates_batch and ended the whole run in the failure writer:
# every ticker sorted after the raising one was never evaluated at all.


def _isolation_context(config, state, broker, now, trading_day):
    return ProcessCandidateContext(
        config=config,
        state=state,
        broker=broker,
        robinhood_data=None,
        now=now,
        trading_day=trading_day,
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
    )


def _isolation_buy_decision(ticker, amount=2.0):
    return ReconciledDecision(
        ticker=ticker,
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0,
        suggested_amount_usd=amount,
        selected_base_amount_usd=2.0,
        target_price=98.0,
        decision_bar_close=98.0,
    )


def test_a_raising_candidate_does_not_abort_the_rest_of_the_batch(monkeypatch, caplog):
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL", "MSFT", "NVDA"),
        approved_test_dates=frozenset({trading_day}),
    )
    broker = DryRunBroker(
        cash=500.0, reference_prices={"AAPL": 98.0, "MSFT": 98.0, "NVDA": 98.0}
    )
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)

    original = ncd._process_buy_candidate

    def _boom_for_msft(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "MSFT":
            raise RuntimeError("provider returned a malformed quote")
        return original(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_msft)

    with caplog.at_level(logging.ERROR, logger="near_close_dca"):
        candidate_outcomes, _top_up, _deployed, errors_count, _reviews = (
            _process_buy_candidates_batch(
                buy_candidates=[
                    _isolation_buy_decision("AAPL"),
                    _isolation_buy_decision("MSFT"),
                    _isolation_buy_decision("NVDA"),
                ],
                ctx=ctx,
            )
        )

    # NVDA is sorted AFTER the raising MSFT: before the net it was never
    # evaluated at all, which is the whole point of this test.
    assert set(candidate_outcomes) == {"AAPL", "MSFT", "NVDA"}
    for survivor in ("AAPL", "NVDA"):
        # Whatever the ladder/gates decided for them, they were EVALUATED --
        # they carry a real outcome of their own, not the batch's error row.
        assert candidate_outcomes[survivor]["status"] != "failed"
        assert "candidate_processing_error" not in candidate_outcomes[survivor]

    failed = candidate_outcomes["MSFT"]
    assert failed["status"] == "failed"
    assert failed["order_id"] is None
    # Type only, never the exception's message: skip_reason is persisted
    # verbatim into run_state.json/buy_candidates.csv, and an unexpected
    # broker/provider exception can carry an account id or a token.
    assert failed["skip_reason"] == "candidate processing raised RuntimeError"
    assert "provider returned a malformed quote" not in failed["skip_reason"]
    # The marker that separates an unhandled raise from an ordinary failed
    # placement, both of which are "failed".
    assert failed["candidate_processing_error"] == "candidate processing raised RuntimeError"

    assert errors_count == 1
    assert "MSFT" in caplog.text
    assert "provider returned a malformed quote" in caplog.text


def test_a_raise_after_an_accepted_order_fails_the_run(monkeypatch):
    """A raise AFTER the broker accepted is NOT isolated.

    The order is real, this run's deployment accounting for it is unknown (the
    raise stops _process_buy_candidate returning its updated running total),
    and its execution logging may be incomplete. Reconstructing the deployment
    figure here means mirroring every branch's own condition and keeping the
    copies in step forever -- and two review rounds found that guess wrong from
    both directions. Fail the run instead: no further order is placed against a
    total known to be wrong. The published order still reaches the failure
    artifact via _IN_FLIGHT_CANDIDATE_OUTCOMES, so a later same-day run will
    not re-allocate it."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    config = StrategyConfig(
        dry_run=False, tickers=("AAPL",), approved_test_dates=frozenset({trading_day})
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)

    def _place_then_raise(*, decision, ctx, running_deployed_7d):
        ncd._publish_in_flight_outcome(
            decision.ticker,
            {
                "status": "submitted",
                "order_id": "real-broker-order-1",
                "skip_reason": None,
                "submitted_amount_usd": 2.0,
            },
        )
        raise RuntimeError("fill lookup exploded after placement")

    monkeypatch.setattr(ncd, "_process_buy_candidate", _place_then_raise)

    with pytest.raises(RuntimeError, match="fill lookup exploded after placement"):
        _process_buy_candidates_batch(buy_candidates=[_isolation_buy_decision("AAPL")], ctx=ctx)

    # The real order survives for the failure writer, annotated so post-run
    # triage can see that this ticker's processing did not finish.
    in_flight = ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["AAPL"]
    assert in_flight["status"] == "submitted"
    assert in_flight["order_id"] == "real-broker-order-1"
    assert in_flight["submitted_amount_usd"] == 2.0
    assert in_flight["candidate_processing_error"] == "candidate processing raised RuntimeError"
    assert "fill lookup exploded" not in in_flight["candidate_processing_error"]


def test_the_candidate_processing_error_marker_reaches_the_artifact_rows(tmp_path, monkeypatch):
    """_build_buy_candidates rebuilds each row from an explicit field list, so
    a marker that is not copied never reaches run_state.json/buy_candidates.csv
    -- and in the accepted-order case the row is an ordinary "submitted" with a
    null skip_reason, leaving no ticker-level evidence at all (Codex review,
    PR #786: "Persist the candidate processing error marker")."""
    import near_close_dca as ncd

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    config = replace(config, dry_run=False)
    artifacts_dir = os.path.join(tmp_path, "runs", "marker")

    original = ncd._process_buy_candidate

    def _boom_for_nvda(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "NVDA":
            raise RuntimeError("NVDA blew up")
        return original(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_nvda)

    run_daily_compact(
        now, config, state, fin, rh, broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        rows = {r["ticker"]: r for r in json.load(handle)["buy_candidates"]}
    assert rows["NVDA"]["candidate_processing_error"] == "candidate processing raised RuntimeError"
    # Type only -- the row is persisted verbatim and must carry no exception text.
    assert "NVDA blew up" not in json.dumps(rows["NVDA"])
    # Present on every row (defaulted), or buy_candidates.csv's header -- derived
    # from row 0 alone -- would reject a later row carrying a key row 0 lacks.
    assert all("candidate_processing_error" in r for r in rows.values())
    with open(os.path.join(artifacts_dir, "buy_candidates.csv"), encoding="utf-8") as handle:
        assert "candidate_processing_error" in next(iter(csv.reader(handle)))


def test_a_seeded_recovery_still_gets_a_breaker_skip_and_keeps_its_evidence(monkeypatch):
    """A pre-seeded outcome must not read as "already processed".

    _reconcile_cross_day_whole_share_claims_for_batch runs BEFORE the candidate
    loop and seeds candidate_outcomes for tickers that are also in
    buy_candidates -- which is why the loop reads `seeded_outcome` at all. The
    old membership test (`d.ticker not in candidate_outcomes`) therefore dropped
    a seeded-but-never-reached ticker from the halt record entirely, leaving its
    row showing only the older recovered submission with no sign that today's
    order was never attempted (Codex review, PR #786: "Track processed
    candidates when recording breaker skips").

    Both facts have to survive: the recovery's own evidence, and the breaker
    skip."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("AAPL", "MSFT", "NVDA", "GS", "EVR")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)

    # EVR sorts last and is never reached, but a cross-day whole-share claim
    # for it was recovered before the loop started.
    seeded_recovery = {
        "status": "submitted",
        "order_id": "RECOVERED-EVR",
        "submitted_amount_usd": 23.36,
        "whole_share_cross_day_recovery": True,
    }
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {"EVR": seeded_recovery}
    seen = []

    def _always_fails(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        return ({"status": "failed", "order_id": None, "skip_reason": "broker unavailable",
                 "broker_placement_failed": True},
                None, running_deployed_7d, 1, 0)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _always_fails)

    candidate_outcomes, _t, _d, _e, _r = _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    assert seen == ["AAPL", "MSFT", "NVDA"], "the breaker must stop after the third failure"
    row = candidate_outcomes["EVR"]
    # The breaker skip is recorded even though the ticker was pre-seeded...
    assert row["placement_breaker_halted"] is True
    assert row["today_order_not_attempted"] is True
    assert "consecutive placement failures" in row["breaker_halt_reason"]
    # A tail row was never attempted, so it is not where the breaker tripped.
    assert not row.get("placement_breaker_tripped_here")
    # ... and the recovery's own settled-spend evidence is untouched.
    assert row["order_id"] == "RECOVERED-EVR"
    assert row["status"] == "submitted"
    assert row["submitted_amount_usd"] == 23.36
    # GS was never seeded and gets the ordinary skipped row.
    assert candidate_outcomes["GS"]["status"] == "skipped"
    assert candidate_outcomes["GS"]["placement_breaker_halted"] is True
    # NVDA is the candidate whose failure TRIPPED the breaker, so it carries
    # both markers. Marking only the tail meant a trip on the FINAL candidate
    # left no row anywhere saying the batch stopped -- harmless here, where
    # errors_count_inc covers run health unconditionally, but the same shape
    # let the executor persist a partially submitted run as an ordinary
    # success (Codex review, PR #786: "Mark the triggering outcome when the
    # breaker fires"). Kept symmetric so the next consumer of this marker
    # cannot inherit the executor's version of that bug.
    assert candidate_outcomes["NVDA"]["placement_breaker_halted"] is True
    assert candidate_outcomes["NVDA"]["placement_breaker_tripped_here"] is True
    # ... and it survives into the artifact, like every other annotation here.
    assert ncd._failure_candidate_row(
        "NVDA", candidate_outcomes["NVDA"]
    )["placement_breaker_tripped_here"] is True

    # ... and the annotations must SURVIVE into the artifact. Both builders
    # rebuild rows from explicit field lists, so a key that is only in the
    # in-flight outcome never reaches run_state.json/buy_candidates.csv
    # (Codex review, PR #786: "Serialize breaker annotations on recovered
    # rows") -- the same trap candidate_processing_error hit.
    failure_row = ncd._failure_candidate_row("EVR", candidate_outcomes["EVR"])
    assert failure_row["placement_breaker_halted"] is True
    assert failure_row["today_order_not_attempted"] is True
    assert "consecutive placement failures" in failure_row["breaker_halt_reason"]
    # The recovery's own evidence still rides along in the artifact row too.
    assert failure_row["candidate_processing_error"] is None


def test_a_structurally_valid_but_malformed_signal_is_dropped():
    """`isinstance(signal, SourceSignal)` was not enough. A provider can return
    a REAL SourceSignal whose fields are malformed -- `action=None` passes the
    type check and then makes `_signal_row`'s `signal.action.value` raise while
    the run artifacts are being built, after other candidates have already
    placed. The type was right and the contents were not (Codex review,
    PR #786: "Discard malformed signals after reconciliation fails")."""
    import near_close_dca as ncd

    malformed = ncd.SourceSignal(
        ticker="RVII", source="finance", action=None, reason="x"
    )
    # It really does take the artifact writer down if retained...
    with pytest.raises(AttributeError):
        ncd._signal_row("RVII", "finance", malformed)
    # ... so it is dropped, and the writer's own no-signal row covers it.
    assert ncd._serializable_signal(malformed) is None
    assert ncd._signal_row("RVII", "finance", ncd._serializable_signal(malformed))[
        "action"
    ] == ncd.Action.NO_DATA.value

    # A well-formed signal is kept untouched, and a non-signal still drops.
    intact = ncd.SourceSignal(
        ticker="RVII", source="finance", action=ncd.Action.NO_ACTION, reason="x"
    )
    assert ncd._serializable_signal(intact) is intact
    assert ncd._serializable_signal({"action": "buy"}) is None


def test_serializable_signal_guards_every_field_the_writer_dereferences():
    """The first version of this validator checked only `action`, and a signal
    with a good action and `decision_bar_timestamp="bad"` still crashed
    `_signal_row` on `.isoformat()` (Codex review, PR #786: "Validate every
    signal field used by artifact serialization").

    So rather than listing the fields by hand and hoping the list stays
    current, this walks `_signal_row`'s own source for the dereferences that
    can raise -- `.value` and `_iso_or_none(...)` -- and asserts the validator
    covers each one. A field added to the writer without a guard fails here
    instead of on a live run."""
    import inspect
    import re

    import near_close_dca as ncd

    source = inspect.getsource(ncd._signal_row)
    # Fields passed through _iso_or_none, which calls .isoformat() on anything
    # that is not None.
    timestamp_fields = set(re.findall(r"_iso_or_none\(signal\.(\w+)\)", source))
    assert timestamp_fields, "fixture must find the writer's timestamp fields"
    assert timestamp_fields == set(ncd._SIGNAL_ROW_TIMESTAMP_FIELDS), (
        "_signal_row dereferences a timestamp _serializable_signal does not guard"
    )
    # ... and the .value dereference, which is `action` alone today.
    value_fields = set(re.findall(r"signal\.(\w+)\.value", source))
    assert value_fields == {"action"}, (
        "_signal_row dereferences a .value field _serializable_signal does not guard"
    )

    # Each guarded field really is fatal if retained, and really is dropped.
    for bad_field in sorted(timestamp_fields):
        signal = ncd.SourceSignal(
            ticker="RVII",
            source="finance",
            action=ncd.Action.NO_ACTION,
            reason="x",
            **{bad_field: "bad"},
        )
        with pytest.raises(AttributeError):
            ncd._signal_row("RVII", "finance", signal)
        assert ncd._serializable_signal(signal) is None, bad_field

    # A well-formed signal with real datetimes survives untouched.
    intact = ncd.SourceSignal(
        ticker="RVII",
        source="finance",
        action=ncd.Action.NO_ACTION,
        reason="x",
        decision_bar_timestamp=datetime(2026, 8, 3, 15, 45, tzinfo=EASTERN),
    )
    assert ncd._serializable_signal(intact) is intact

    # The field-by-field checks above are not the whole guard. run_state.json
    # goes through json.dumps, which raises on ANY unserializable value -- not
    # only the ones _signal_row dereferences -- so a field this test's source
    # walk cannot know about is caught by round-tripping the row itself.
    for bad_field in ("reason", "decision_price"):
        for bad_value in (object(), {1, 2}, lambda: None):
            signal_fields = {"reason": "x", bad_field: bad_value}
            unserializable = ncd.SourceSignal(
                ticker="RVII",
                source="finance",
                action=ncd.Action.NO_ACTION,
                **signal_fields,
            )
            # Precondition: the row really does take the writer down.
            with pytest.raises(TypeError):
                json.dumps(ncd._signal_row("RVII", "finance", unserializable))
            assert ncd._serializable_signal(unserializable) is None, bad_field

    # ... and the guard is not stricter than the writer: a NaN float is what
    # json.dumps emits by default, so it must still be RETAINED.
    nan_priced = ncd.SourceSignal(
        ticker="RVII",
        source="finance",
        action=ncd.Action.NO_ACTION,
        reason="x",
        decision_price=float("nan"),
    )
    assert ncd._serializable_signal(nan_priced) is nan_priced


def test_a_recovery_keeps_todays_accumulation_bookkeeping():
    """A recovered order from an earlier claim supplies the row's status and
    spend, but today's ledger contribution is already recorded and is not the
    recovery's to discard. Replacing the row wholesale dropped
    whole_share_contribution_usd, the updated pending balance and any current
    block reason from run_state.json and buy_candidates.csv, leaving the
    persisted accumulator state stale (Codex review, PR #786: "Preserve
    today's accumulation metadata beside a recovery")."""
    import near_close_dca as ncd

    recovered = {
        "status": "submitted",
        "order_id": "RECOVERED-BYDDY",
        "submitted_amount_usd": 10.0,
    }
    today = {
        "status": "skipped",
        "deliberate_skip": True,
        "skip_reason": "whole-share: contribution accumulated, share threshold not yet reached",
        "whole_share_contribution_usd": 2.0,
        "whole_share_pending_usd": 6.0,
    }
    merged = dict(recovered)
    for carried in ncd._TODAY_ACCUMULATION_FIELDS:
        value = merged.get(carried, ncd._MISSING)
        if value is ncd._MISSING or value is None:
            today_value = today.get(carried)
            if today_value is not None:
                merged[carried] = today_value

    # The recovery still owns status, spend and order id...
    assert merged["status"] == "submitted"
    assert merged["order_id"] == "RECOVERED-BYDDY"
    assert merged["submitted_amount_usd"] == 10.0
    # ... and today's bookkeeping rides alongside rather than vanishing.
    assert merged["whole_share_contribution_usd"] == 2.0
    assert merged["whole_share_pending_usd"] == 6.0
    assert "threshold not yet reached" in merged["skip_reason"]

    # Both markers reach the artifacts, like every other annotation here.
    row = ncd._failure_candidate_row("BYDDY", merged)
    assert row["whole_share_contribution_usd"] == 2.0
    assert row["whole_share_pending_usd"] == 6.0


def test_a_merged_recovery_does_not_mask_todays_placement_failure():
    """A recovered fill and a fresh placement that failed today are two
    independent events. Forcing the aggregate status to "submitted" made the
    second invisible to `_apply_no_order_classification`, which counts
    `status == "failed"` rows -- so a run whose today-order was rejected
    recorded as an ordinary submitted run (Codex review, PR #786: "Preserve
    fresh placement failures when merging recoveries")."""
    import near_close_dca as ncd

    merged = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "RECOVERED", "submitted_amount_usd": 23.36},
        {
            "status": "failed",
            "order_id": None,
            "skip_reason": "placement failed: RuntimeError",
            "broker_placement_failed": True,
            "submitted_amount_usd": 0.0,
        },
    )
    # The recovered order really did submit, so the status stands...
    assert merged["status"] == "submitted"
    assert merged["submitted_amount_usd"] == 23.36
    # ... and today's failure rides alongside it rather than being erased.
    assert merged["merged_placement_failed"] is True
    assert "RuntimeError" in merged["merged_failure_reason"]

    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"RVII": merged})
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["error_class"] == "partial_placement_failure"
    # ... and survives into the artifact row.
    assert ncd._failure_candidate_row("RVII", merged)["merged_placement_failed"] is True


def test_a_merged_recovery_does_not_mask_todays_blocked_placement():
    """The sibling of the "failed" case above, and the half that fix missed. A
    whole-share placement whose reconciliation lookup then RAISED is recorded
    "blocked" on purpose -- so the ledger claim stays reserved for a human --
    and "blocked" is not in `_PRIOR_ALLOCATION_ATTEMPT_STATUSES`, so the batch
    loop replaced the row wholesale with the recovered fill and every trace of
    the block went with it (Codex review, PR #786: "Preserve blocked placement
    failures beside recovered fills")."""
    import near_close_dca as ncd

    blocked_today = {
        "status": "blocked",
        "broker_placement_failed": True,
        "order_id": "TERMINAL-ID",
        "skip_reason": (
            "whole-share placement failed: RuntimeError; reconciliation lookup also failed: "
            "TimeoutError; claim left reserved pending manual reconciliation"
        ),
        "submitted_amount_usd": 0.0,
    }

    # The batch loop's own merge trigger: "blocked" alone is not an attempt
    # status, but the broker_placement_failed marker says a real call was made,
    # so the recovered fill must MERGE with this row rather than replace it.
    assert blocked_today["status"] not in ncd._PRIOR_ALLOCATION_ATTEMPT_STATUSES
    assert ncd._outcome_records_a_broker_attempt(blocked_today)
    # The confirmation-required hold is the same shape, for the same reason.
    assert ncd._outcome_records_a_broker_attempt(
        {"status": "not_submitted_confirmation_required", "broker_placement_failed": True}
    )
    # A policy block decided BEFORE any submission still gets replaced: no
    # broker call was ever made, so there is no second event to preserve.
    assert not ncd._outcome_records_a_broker_attempt(
        {"status": "blocked", "deliberate_skip": True, "skip_reason": "budget cap"}
    )
    assert not ncd._outcome_records_a_broker_attempt(
        {"status": "skipped", "deliberate_skip": True}
    )
    # ... and the statuses that always merged still do.
    for attempted in sorted(ncd._PRIOR_ALLOCATION_ATTEMPT_STATUSES):
        assert ncd._outcome_records_a_broker_attempt({"status": attempted}), attempted

    merged = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "RECOVERED", "submitted_amount_usd": 23.36},
        blocked_today,
    )
    # The recovered order really did submit, so the status and its spend stand...
    assert merged["status"] == "submitted"
    assert merged["submitted_amount_usd"] == 23.36
    assert merged["order_id"] == "RECOVERED"
    # ... today's block rides alongside rather than vanishing, on its OWN key:
    # a reconciliation call that raised is a connector error, not a failed
    # order, and the two are classified differently.
    assert merged["merged_placement_blocked"] is True
    assert merged.get("merged_placement_failed") is None
    assert "claim left reserved" in merged["merged_failure_reason"]
    # ... and the terminal order id is still reconcilable.
    assert "TERMINAL-ID" in merged["whole_share_order_ids"]

    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": merged})
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"

    # ... and survives into both artifact builders.
    assert ncd._failure_candidate_row("BYDDY", merged)["merged_placement_blocked"] is True

    # A DELIBERATE block (a budget cap, a duplicate guard) made no broker call
    # and must not be marked -- it never carries broker_placement_failed.
    innocuous = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "RECOVERED", "submitted_amount_usd": 23.36},
        {"status": "blocked", "deliberate_skip": True, "skip_reason": "budget cap"},
    )
    assert innocuous.get("merged_placement_blocked") is None


def test_a_breaker_skipped_fire_does_not_consume_a_retry_attempt():
    """max_fire_attempts is a retry budget for orders the BROKER refused. A
    candidate the consecutive-failure breaker never reached made no broker call
    at all, so counting it abandoned accumulated cycles after a few outage
    mornings -- the same inversion of "deferred, not lost" the dry-run case
    already had a carve-out for (Codex review, PR #786: "Do not count
    breaker-skipped inverse-Martingale fires")."""
    import near_close_dca as ncd

    # The breaker's unattempted tail, and a pre-roll recovery annotated with
    # the same halt: neither ticker's own order was ever tried.
    assert not ncd._fire_attempt_reached_the_broker(
        {"status": "skipped", "deliberate_skip": True, "placement_breaker_halted": True}
    )
    assert not ncd._fire_attempt_reached_the_broker(
        {
            "status": "submitted",
            "order_id": "RECOVERED",
            "placement_breaker_halted": True,
            "today_order_not_attempted": True,
        }
    )
    # The existing dry-run carve-out is unchanged.
    assert not ncd._fire_attempt_reached_the_broker({"status": "dry_run"})

    # The row that TRIPPED the breaker really was attempted and really failed:
    # that is exactly what the retry budget counts.
    assert ncd._fire_attempt_reached_the_broker(
        {
            "status": "failed",
            "broker_placement_failed": True,
            "placement_breaker_halted": True,
            "placement_breaker_tripped_here": True,
        }
    )
    # ... as is an ordinary rejected placement with no breaker involved.
    assert ncd._fire_attempt_reached_the_broker(
        {"status": "failed", "broker_placement_failed": True}
    )
    assert ncd._fire_attempt_reached_the_broker({"status": "submitted"})


def test_every_broker_quantity_parse_survives_an_overflowing_figure():
    """`int(float(...))` raises OverflowError — not ValueError — on an
    infinite or oversized figure, and these parse BROKER-supplied data. One
    such parse sat inside the placement-exception reconciliation path, past
    the `except` that had just discovered a terminal order: the raise escaped
    into the batch loop's last-resort net, which has no idea a terminal order
    was found, wrote an ordinary failed row and kept placing while that
    order's possible fill went unaccounted (Codex review, PR #786: "Preserve
    terminal orders when quantity parsing overflows")."""
    import re
    from pathlib import Path

    import near_close_dca as ncd

    # Precondition: these really are the shapes that raise, and it really is
    # OverflowError rather than the ValueError the handlers already caught.
    for overflowing in ("inf", "1e400", float("inf"), "1" + "0" * 400):
        with pytest.raises(OverflowError):
            int(float(overflowing))
    # NaN is the ValueError case, already handled — kept here so a future
    # narrowing of either handler fails this test.
    with pytest.raises(ValueError):
        int(float("nan"))

    # EVERY such parse in the module is guarded, not only the one the finding
    # named. Read off the source rather than hand-listed, so a parse added
    # later without the guard fails the build instead of a live run.
    lines = Path(ncd.__file__).read_text(encoding="utf-8").splitlines()
    # Real parses only: this module's house style quotes `int(float(...))` in
    # the prose explaining WHY these handlers are wide, and a comment has no
    # handler to find.
    parses = [
        i for i, line in enumerate(lines)
        if re.search(r"int\(float\(", line) and not line.lstrip().startswith("#")
    ]
    # Two of the six parses this test originally found (the ordinary-success
    # and immediate-recovery whole-share settlement branches) were replaced
    # by `_finite_nonnegative_float`, which already rejects NaN/Infinity
    # internally rather than needing a local `except OverflowError` (Codex
    # review, PR #786: "Reject invalid cumulative quantities before
    # settling") -- so the raw parse count this scan finds legitimately
    # dropped from six to four. The four remaining are exactly the ones this
    # test still needs to guard.
    assert len(parses) >= 4, f"fixture must find the broker-quantity parses, got {parses}"
    for i in parses:
        # The handler sits within a couple of lines of the parse in every case.
        window = "\n".join(lines[i:i + 4])
        assert "except (TypeError, ValueError, OverflowError)" in window, (
            "an int(float(...)) over broker data does not catch OverflowError, "
            f"near line {i + 1}: {lines[i].strip()}"
        )


def test_a_merged_recovery_counts_every_order_it_represents():
    """Two unresolved claims for the same ticker recovered in one run become
    ONE row carrying both order ids, both notionals and both share counts —
    but `orders_submitted_count` and `usage_proxy.orders_placed_count` counted
    ROWS, so the top-level activity metrics reported one order while the
    execution log and order artifacts carried two (Codex review, PR #786:
    "Count every order represented by a merged outcome")."""
    import near_close_dca as ncd

    first = {"status": "submitted", "order_id": "ORD-A", "submitted_amount_usd": 23.36,
             "whole_share_quantity": 1}
    second = {"status": "submitted", "order_id": "ORD-B", "submitted_amount_usd": 23.40,
              "whole_share_quantity": 1}
    merged = ncd._merge_whole_share_submission_outcomes(first, second)
    assert merged["merged_submission_count"] == 2
    assert merged["whole_share_order_ids"] == ["ORD-A", "ORD-B"]

    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": merged})
    assert artifacts["orders_submitted_count"] == 2
    proxy = ncd.build_usage_proxy(
        decisions=[], signal_rows=[], buy_candidates=[], candidate_outcomes={"BYDDY": merged}
    )
    assert proxy["orders_placed_count"] == 2

    # An aggregate can itself be an input to another aggregate — the batch
    # loop merges a seeded pre-roll recovery with a mid-call one before
    # merging in today's own submission — so the count has to accumulate.
    third = ncd._merge_whole_share_submission_outcomes(
        merged, {"status": "submitted", "order_id": "ORD-C", "submitted_amount_usd": 10.0}
    )
    assert third["merged_submission_count"] == 3

    # A side that did NOT submit keeps its id reconcilable but is not counted
    # as an order — which is exactly why this is an explicit count rather than
    # len(whole_share_order_ids).
    with_failure = ncd._merge_whole_share_submission_outcomes(
        first,
        {"status": "failed", "order_id": "TERMINAL", "broker_placement_failed": True,
         "submitted_amount_usd": 0.0},
    )
    assert with_failure["merged_submission_count"] == 1
    assert "TERMINAL" in with_failure["whole_share_order_ids"]

    # An ordinary unmerged row counts exactly as it always did.
    plain = ncd.build_usage_proxy(
        decisions=[], signal_rows=[], buy_candidates=[],
        candidate_outcomes={"AAPL": {"status": "submitted", "order_id": "X"}},
    )
    assert plain["orders_placed_count"] == 1


def test_a_merged_confirmation_hold_is_not_a_connector_error():
    """A confirmation-required hold carries `broker_placement_failed` — a real
    broker call was made — so round 28's `merged_placement_blocked` predicate
    ("any non-submitted side with the marker") captured it and reported the run
    as a partial placement failure with `error_class='degraded_market_data'`.
    Everywhere else in this module a confirmation hold is a routine non-error
    outcome (Codex review, PR #786: "Exclude confirmation holds from merged
    connector errors")."""
    import near_close_dca as ncd

    hold = {
        "status": "not_submitted_confirmation_required",
        "broker_placement_failed": True,
        "order_id": None,
        "skip_reason": "submission awaiting confirmation",
    }
    merged = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "RECOVERED", "submitted_amount_usd": 23.36},
        hold,
    )
    # Not a connector error, and not a failed order.
    assert merged.get("merged_placement_blocked") is None
    assert merged.get("merged_placement_failed") is None
    # ... but not silently swallowed either: the merge forces the row's status
    # to "submitted", so the hold rides alongside on a key that classifies
    # nothing.
    assert merged["merged_confirmation_required"] is True
    assert "awaiting confirmation" in merged["merged_confirmation_reason"]

    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": merged})
    assert artifacts.get("is_trading_failure") is None
    assert artifacts.get("error_class") is None
    assert artifacts.get("run_classification_outcome") is None

    # ... and it reaches the artifact rows like every other marker here.
    assert ncd._failure_candidate_row("BYDDY", merged)["merged_confirmation_required"] is True

    # The two cases this narrowing must NOT disturb: round 27's failed
    # placement and round 28's non-deliberate block both still classify.
    still_failed = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "RECOVERED", "submitted_amount_usd": 23.36},
        {"status": "failed", "broker_placement_failed": True, "skip_reason": "RuntimeError"},
    )
    assert still_failed["merged_placement_failed"] is True
    still_blocked = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "RECOVERED", "submitted_amount_usd": 23.36},
        {"status": "blocked", "broker_placement_failed": True, "order_id": "TERMINAL",
         "skip_reason": "claim left reserved pending manual reconciliation"},
    )
    assert still_blocked["merged_placement_blocked"] is True

    # And the hold is still MERGED rather than replaced — it is a real broker
    # attempt, which is a separate question from how it classifies.
    assert ncd._outcome_records_a_broker_attempt(hold)


def test_a_settlement_adjustment_is_not_a_second_submission():
    """`_book_reconciled_whole_share_order` publishes a settlement adjustment
    as `status="submitted"` carrying the SAME order_id as the physical order it
    corrects — and deliberately skips `record_buy()` for exactly that reason,
    as `load_prior_same_day_allocations` skips its attempt count. Round 29's
    submission count read the status alone and reported two orders for one
    (Codex review, PR #786: "Exclude settlement adjustments from submission
    counts")."""
    import near_close_dca as ncd

    real = {"status": "submitted", "order_id": "ORD-A", "submitted_amount_usd": 23.36,
            "whole_share_quantity": 1}
    adjustment = {"status": "submitted", "order_id": "ORD-A", "submitted_amount_usd": 1.14,
                  "whole_share_quantity": 0, "whole_share_settlement_adjustment": True}

    merged = ncd._merge_whole_share_submission_outcomes(real, adjustment)
    assert merged["merged_submission_count"] == 1

    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": merged})
    assert artifacts["orders_submitted_count"] == 1
    assert ncd.build_usage_proxy(
        decisions=[], signal_rows=[], buy_candidates=[], candidate_outcomes={"BYDDY": merged}
    )["orders_placed_count"] == 1

    # The same rule for an adjustment published on its own: it corrects an
    # order some earlier run already counted, so this run placed nothing.
    assert ncd.build_usage_proxy(
        decisions=[], signal_rows=[], buy_candidates=[],
        candidate_outcomes={"BYDDY": adjustment},
    )["orders_placed_count"] == 0

    # Two genuinely distinct orders still count 2 — round 29's case is intact.
    two_real = ncd._merge_whole_share_submission_outcomes(
        real, {"status": "submitted", "order_id": "ORD-B", "submitted_amount_usd": 23.40}
    )
    assert two_real["merged_submission_count"] == 2


def test_a_mixed_aggregate_does_not_claim_to_be_a_settlement_adjustment():
    """`whole_share_settlement_adjustment` is a claim about the WHOLE row —
    "this is a correction to an order some earlier booking already counted" —
    and `merged = dict(new)` asserted it for the combined row whenever the
    adjustment happened to be the newer side. Two readers then got the wrong
    answer: `load_prior_same_day_allocations` skipped the attempt count for an
    aggregate that really does contain a placement, and `_submission_count`
    reported zero for it, so re-merging discarded the real order's own count
    (Codex review, PR #786: "Clear adjustment marker from mixed submission
    aggregates")."""
    import near_close_dca as ncd

    real = {"status": "submitted", "order_id": "ORD-A", "submitted_amount_usd": 23.36,
            "whole_share_quantity": 1}
    adjustment = {"status": "submitted", "order_id": "ORD-A", "submitted_amount_usd": 1.14,
                  "whole_share_quantity": 0, "whole_share_settlement_adjustment": True}

    for label, merged in (
        ("adjustment newer", ncd._merge_whole_share_submission_outcomes(real, adjustment)),
        ("adjustment older", ncd._merge_whole_share_submission_outcomes(adjustment, real)),
    ):
        assert not merged.get("whole_share_settlement_adjustment"), label
        assert merged["merged_submission_count"] == 1, label
        # ... and the aggregate answers for itself consistently, which is what
        # makes re-merging safe.
        assert ncd._submission_count(merged) == 1, label

    # The batch loop routinely merges an already-merged row again. Before the
    # fix the aggregate reported zero and the real order's count vanished.
    mixed = ncd._merge_whole_share_submission_outcomes(real, adjustment)
    again = ncd._merge_whole_share_submission_outcomes(
        mixed, {"status": "submitted", "order_id": "ORD-B", "submitted_amount_usd": 23.40}
    )
    assert again["merged_submission_count"] == 2

    # The one case where the claim is still true of the row as a whole: every
    # submitting side is an adjustment. Marker kept, count zero.
    both = ncd._merge_whole_share_submission_outcomes(
        adjustment, dict(adjustment, order_id="ORD-B")
    )
    assert both["whole_share_settlement_adjustment"] is True
    assert both["merged_submission_count"] == 0
    assert ncd._submission_count(both) == 0

    # A side that made no broker call at all does not make the row a real
    # placement, so the marker stands.
    #
    # This assertion originally used a `status="failed"` side and expected the
    # marker KEPT, on the reasoning that a non-submitting side is not a real
    # placement. That was too coarse and a later round said so: a failed
    # placement IS a real broker attempt that consumed a
    # `max_orders_per_day` slot — which is exactly why "failed" is in
    # `_PRIOR_ALLOCATION_ATTEMPT_STATUSES`, and why
    # `load_prior_same_day_allocations` already counts such a row as an
    # attempt when it stands alone. Merging it with an adjustment must not
    # make it stop counting (Codex review, PR #786: "Clear adjustment marker
    # when merging any real attempt"). The distinction is broker-call versus
    # no-broker-call, not submitted versus not-submitted.
    with_deliberate_skip = ncd._merge_whole_share_submission_outcomes(
        {"status": "skipped", "deliberate_skip": True, "skip_reason": "budget cap"},
        adjustment,
    )
    assert with_deliberate_skip["whole_share_settlement_adjustment"] is True
    # ... while a failed placement clears it, and is counted as the attempt
    # it was.
    with_failure = ncd._merge_whole_share_submission_outcomes(
        {"status": "failed", "skip_reason": "RuntimeError"}, adjustment
    )
    assert not with_failure.get("whole_share_settlement_adjustment")
    assert with_failure["merged_attempt_count"] == 1

    # The inputs are never mutated — the merge builds a new row.
    assert adjustment["whole_share_settlement_adjustment"] is True


def test_aggregate_failure_markers_survive_a_second_merge():
    """`merged = dict(new)` carries NEW's markers and drops EXISTING's. That
    is invisible while `existing` is a plain outcome — the status checks
    rebuild the markers from its status — and wrong the moment `existing` is
    itself an aggregate: its status is already "submitted", so those checks
    see two submitted sides and recreate nothing (Codex review, PR #786:
    "Carry aggregate failure markers through subsequent merges")."""
    import near_close_dca as ncd

    recovery = {"status": "submitted", "order_id": "REC", "submitted_amount_usd": 23.36}
    second_recovery = {"status": "submitted", "order_id": "ORD2", "submitted_amount_usd": 10.0}

    for label, today, marker in (
        ("failed", {"status": "failed", "broker_placement_failed": True,
                    "skip_reason": "RuntimeError"}, "merged_placement_failed"),
        ("blocked", {"status": "blocked", "broker_placement_failed": True, "order_id": "T",
                     "skip_reason": "claim left reserved"}, "merged_placement_blocked"),
        ("hold", {"status": "not_submitted_confirmation_required",
                  "broker_placement_failed": True, "skip_reason": "awaiting confirmation"},
         "merged_confirmation_required"),
    ):
        aggregate = ncd._merge_whole_share_submission_outcomes(recovery, today)
        assert aggregate[marker] is True, label
        # The batch loop merges an already-merged row again routinely. Keep
        # the aggregate metadata regardless of which argument it occupies;
        # the merge contract is symmetric even though scalar fields prefer
        # the newer side.
        for again in (
            ncd._merge_whole_share_submission_outcomes(aggregate, second_recovery),
            ncd._merge_whole_share_submission_outcomes(second_recovery, aggregate),
        ):
            assert again.get(marker) is True, label
            reason_key = (
                "merged_confirmation_reason"
                if marker == "merged_confirmation_required"
                else "merged_failure_reason"
            )
            assert again.get(reason_key), label

    # The classification is what actually went wrong: before the fix the
    # twice-merged row reported no trading failure at all.
    aggregate = ncd._merge_whole_share_submission_outcomes(
        recovery, {"status": "failed", "broker_placement_failed": True, "skip_reason": "RuntimeError"}
    )
    again = ncd._merge_whole_share_submission_outcomes(aggregate, second_recovery)
    assert "RuntimeError" in again["merged_failure_reason"]
    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": again})
    assert artifacts["is_trading_failure"] is True
    assert artifacts["orders_failed_count"] == 1


def test_a_merged_failure_is_counted_even_when_the_breaker_trips():
    """Today's failed placement merged with an older recovery IS what trips
    the breaker, so one row carries `placement_breaker_halted` AND
    `merged_placement_failed`. Chaining the two checks meant the breaker
    branch consumed the row and `failed` never incremented (Codex review,
    PR #786: "Count merged failures even when the breaker trips")."""
    import near_close_dca as ncd

    merged = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "REC", "submitted_amount_usd": 23.36},
        {"status": "failed", "broker_placement_failed": True, "skip_reason": "RuntimeError"},
    )
    merged["placement_breaker_halted"] = True

    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": merged})
    assert artifacts["orders_failed_count"] == 1
    assert artifacts["is_trading_failure"] is True
    assert artifacts["error_class"] == "partial_placement_failure"

    # A three-way merge can carry a failed AND a blocked side; each is its own
    # event, so neither may consume the other's branch either.
    both = ncd._merge_whole_share_submission_outcomes(
        merged, {"status": "blocked", "broker_placement_failed": True, "order_id": "T",
                 "skip_reason": "claim left reserved"},
    )
    assert both["merged_placement_failed"] is True
    assert both["merged_placement_blocked"] is True
    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": both})
    assert artifacts["orders_failed_count"] == 1


def test_a_recovered_zero_fill_attempt_is_merged_not_overwritten():
    """`_book_reconciled_whole_share_order`'s zero-fill branch reports
    `status="failed"` — nothing was purchased — while still describing a REAL
    broker order that consumed a `max_orders_per_day` slot. When one ticker
    has several unresolved claims and the zero-fill one reconciles FIRST, the
    later filled claim overwrote it outright, dropping the attempt and its
    order id from the artifact (Codex review, PR #786: "Merge recovered
    zero-fill attempts before later submissions")."""
    import near_close_dca as ncd

    zero_fill = {"status": "failed", "whole_share_zero_fill_attempt": True,
                 "order_id": "ZERO-ORD", "submitted_amount_usd": 0.0}
    # The publication site's own test, shared with the batch loop so the two
    # cannot drift apart again.
    assert ncd._outcome_is_mergeable_recovery(zero_fill)
    assert ncd._outcome_is_mergeable_recovery({"status": "submitted"})
    # A row that is neither is still overwritten, as before.
    assert not ncd._outcome_is_mergeable_recovery({"status": "failed"})
    assert not ncd._outcome_is_mergeable_recovery({"status": "skipped", "deliberate_skip": True})

    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    published = ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]
    try:
        ncd._publish_or_merge_whole_share_outcome("BYDDY", zero_fill)
        ncd._publish_or_merge_whole_share_outcome("BYDDY", {
            "status": "submitted", "order_id": "FILL-ORD",
            "submitted_amount_usd": 23.36, "whole_share_quantity": 1,
        })
        row = published["BYDDY"]
        # Both broker orders survive, so neither drops out of attempt counting.
        assert row["whole_share_order_ids"] == ["ZERO-ORD", "FILL-ORD"]
        # The filled claim owns the status and the spend...
        assert row["status"] == "submitted"
        assert row["submitted_amount_usd"] == 23.36
        # ... and the zero-fill attempt is still recorded as one. Every
        # consumer reads this marker as an inclusion test, so an aggregate
        # containing a zero-fill attempt genuinely carries it — unlike
        # `whole_share_settlement_adjustment`, which is an exclusion claim and
        # is cleared on a mixed aggregate.
        assert row["whole_share_zero_fill_attempt"] is True
    finally:
        ncd._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)


def test_a_merged_failure_reaches_the_zero_submission_classification():
    """The zero-submission branch re-derived "did a placement fail?" from raw
    statuses, which cannot see a failure the merge markers recorded — a merged
    row's status is forced to "submitted". Pair that with a settlement
    adjustment, which counts ZERO submissions, and the run reaches that branch
    carrying `failed = 1` and was classified `completed_no_orders` with no
    is_trading_failure and no count (Codex review, PR #786: "Include merged
    failures in zero-order classification")."""
    import near_close_dca as ncd

    adjustment = {"status": "submitted", "order_id": "ORD-A", "submitted_amount_usd": 1.14,
                  "whole_share_quantity": 0, "whole_share_settlement_adjustment": True}
    merged = ncd._merge_whole_share_submission_outcomes(
        adjustment,
        {"status": "failed", "broker_placement_failed": True, "skip_reason": "RuntimeError"},
    )
    # The shape that makes this reachable: submitted status, zero submissions,
    # a real failure recorded only in the marker.
    assert merged["status"] == "submitted"
    assert merged["merged_submission_count"] == 0
    assert merged["merged_placement_failed"] is True
    assert ncd._submitted_orders_in_row(merged) == 0

    artifacts = {"buy_candidates": [{"ticker": "BYDDY"}], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": merged})
    assert artifacts["is_trading_failure"] is True
    assert (
        artifacts["run_classification_outcome"]
        == "failed_after_connector_native_recovery_attempt"
    )
    assert artifacts["error_class"] == "failed_after_connector_native_recovery_attempt"
    assert artifacts["orders_failed_count"] == 1

    # The sibling marker takes the same path: a pre-placement or reconciliation
    # call that raised is a placement attempted and not placed, exactly as the
    # SUBMITTED branch has always treated the two together.
    blocked = ncd._merge_whole_share_submission_outcomes(
        adjustment,
        {"status": "blocked", "broker_placement_failed": True, "order_id": "T",
         "skip_reason": "claim left reserved"},
    )
    artifacts = {"buy_candidates": [{"ticker": "BYDDY"}], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(artifacts, {"BYDDY": blocked})
    assert artifacts["is_trading_failure"] is True

    # ... and a run that genuinely placed nothing and failed nothing is still
    # a clean no-order run. Widening this predicate must not make every
    # deliberate skip a trading failure.
    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(
        artifacts, {"AAPL": {"status": "skipped", "deliberate_skip": True}}
    )
    assert artifacts["is_trading_failure"] is False
    assert artifacts["run_classification_outcome"] == "completed_no_orders"
    assert artifacts.get("orders_failed_count") is None


def test_a_merged_row_counts_every_broker_attempt_it_represents():
    """Two questions the merge must answer separately, because they genuinely
    differ: how many orders SUBMITTED, and how many consumed a
    `max_orders_per_day` slot. Two recovered zero-fill attempts submitted
    nothing and took two slots.

    Both defects here understate that shared daily cap, which is what makes
    them P1: an understated cap leaves room for a later run to place an extra
    live order (Codex review, PR #786: "Clear adjustment marker when merging
    any real attempt", "Track every attempt represented by merged
    outcomes")."""
    import near_close_dca as ncd

    zero_fill = {"status": "failed", "whole_share_zero_fill_attempt": True,
                 "order_id": "ZERO", "submitted_amount_usd": 0.0}
    adjustment = {"status": "submitted", "order_id": "ADJ", "submitted_amount_usd": 1.14,
                  "whole_share_quantity": 0, "whole_share_settlement_adjustment": True}
    submitted = {"status": "submitted", "order_id": "SUB", "submitted_amount_usd": 23.36,
                 "whole_share_quantity": 1}
    failed = {"status": "failed", "broker_placement_failed": True, "order_id": "F1",
              "skip_reason": "RuntimeError"}

    # A zero-fill recovery and a rejected placement are each a real order that
    # took a slot, even though neither submitted anything.
    for attempt in (zero_fill, failed, submitted):
        assert ncd._is_distinct_broker_attempt(attempt), attempt["order_id"]
    assert not ncd._is_distinct_broker_attempt(adjustment)

    # An adjustment merged with ANY of them is no longer an adjustment row —
    # in either ordering. Before the fix the row carried both markers, and
    # `load_prior_same_day_allocations` reads the adjustment one.
    for label, attempt in (("zero-fill", zero_fill), ("failed", failed), ("submitted", submitted)):
        for merged in (
            ncd._merge_whole_share_submission_outcomes(attempt, adjustment),
            ncd._merge_whole_share_submission_outcomes(adjustment, attempt),
        ):
            assert not merged.get("whole_share_settlement_adjustment"), label
            assert merged["merged_attempt_count"] == 1, label

    # Two adjustments and nothing else IS still an adjustment row, counting
    # neither an attempt nor a submission.
    both = ncd._merge_whole_share_submission_outcomes(
        adjustment, dict(adjustment, order_id="ADJ2")
    )
    assert both["whole_share_settlement_adjustment"] is True
    assert both["merged_attempt_count"] == 0
    assert both["merged_submission_count"] == 0

    # Attempts and submissions diverge, which is the whole point of counting
    # them separately.
    two_zero = ncd._merge_whole_share_submission_outcomes(
        zero_fill, dict(zero_fill, order_id="ZERO2")
    )
    assert two_zero["merged_attempt_count"] == 2
    assert two_zero["merged_submission_count"] == 0
    assert two_zero["whole_share_order_ids"] == ["ZERO", "ZERO2"]

    mixed = ncd._merge_whole_share_submission_outcomes(submitted, zero_fill)
    assert (mixed["merged_attempt_count"], mixed["merged_submission_count"]) == (2, 1)
    # ... and both accumulate through a re-merge.
    again = ncd._merge_whole_share_submission_outcomes(mixed, dict(submitted, order_id="SUB2"))
    assert (again["merged_attempt_count"], again["merged_submission_count"]) == (3, 2)

    # The consumer reads the recorded total; an unmerged row still counts 1,
    # so nothing outside the merged case changes.
    assert ncd._broker_attempt_count({"status": "submitted"}) == 1
    assert ncd._broker_attempt_count({"status": "skipped", "deliberate_skip": True}) == 1
    assert ncd._broker_attempt_count(two_zero) == 2


def test_merged_attempts_are_bucketed_by_trading_day():
    """`max_orders_per_day` is a PER-DAY limit, so a flat total is the wrong
    shape for it. A prior-day recovered order merged with a fresh order placed
    today gave `merged_attempt_count == 2`, and because the aggregate's claim
    breakdown then spans both days it is not "cross-day only" either — so both
    were charged against today, and one old recovery plus one fresh order could
    block a later valid order (Codex review, PR #786: "Bucket merged attempts
    by their trading day").

    That is the opposite error from the undercount the flat count was added to
    fix, in the same line of code."""
    import near_close_dca as ncd

    today = "2026-08-03"
    prior_day_recovery = {
        "status": "submitted", "order_id": "OLD", "submitted_amount_usd": 23.36,
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-01", "amount_usd": 23.36}],
    }
    fresh_today = {"status": "submitted", "order_id": "NEW", "submitted_amount_usd": 23.40}

    merged = ncd._merge_whole_share_submission_outcomes(prior_day_recovery, fresh_today)
    # The flat total still records both orders...
    assert merged["merged_attempt_count"] == 2
    # ... but only today's is charged to today.
    assert ncd._broker_attempts_on_day(merged, today) == 1

    # The undercount this replaced stays fixed: two SAME-day zero-fill
    # recoveries still consume two of today's slots.
    zero = {"status": "failed", "whole_share_zero_fill_attempt": True,
            "order_id": "Z1", "submitted_amount_usd": 0.0}
    two_zero = ncd._merge_whole_share_submission_outcomes(zero, dict(zero, order_id="Z2"))
    assert ncd._broker_attempts_on_day(two_zero, today) == 2

    # An adjustment-only aggregate charges nothing, and an unmerged row counts
    # exactly 1 as it always did.
    adjustment = {"status": "submitted", "order_id": "A1", "submitted_amount_usd": 1.0,
                  "whole_share_settlement_adjustment": True}
    both_adjustments = ncd._merge_whole_share_submission_outcomes(
        adjustment, dict(adjustment, order_id="A2")
    )
    assert ncd._broker_attempts_on_day(both_adjustments, today) == 0
    assert ncd._broker_attempts_on_day({"status": "submitted"}, today) == 1
    assert ncd._broker_attempts_on_day({"status": "skipped", "deliberate_skip": True}, today) == 1


def test_a_confirmation_hold_is_not_a_merged_attempt():
    """A confirmation hold carries `broker_placement_failed` because a real
    broker call was made — but no order reached the broker, which is why a
    standalone hold is excluded from `_PRIOR_ALLOCATION_ATTEMPT_STATUSES` and
    from `_orders_csv_attempt_counts`. Reading the broad marker without that
    carve-out made a merged hold count as a second daily attempt, prematurely
    consuming `max_orders_per_day` (Codex review, PR #786: "Exclude
    confirmation holds from merged attempt counts")."""
    import near_close_dca as ncd

    hold = {"status": "not_submitted_confirmation_required",
            "broker_placement_failed": True, "skip_reason": "awaiting confirmation"}
    # The standalone exclusion this has to match.
    assert hold["status"] not in ncd._PRIOR_ALLOCATION_ATTEMPT_STATUSES
    assert not ncd._is_distinct_broker_attempt(hold)

    merged = ncd._merge_whole_share_submission_outcomes(
        {"status": "submitted", "order_id": "REC", "submitted_amount_usd": 23.36}, hold
    )
    assert merged["merged_attempt_count"] == 1
    assert ncd._broker_attempts_on_day(merged, "2026-08-03") == 1
    # It is still merged rather than replaced, and still recorded — only its
    # ATTEMPT accounting changes.
    assert merged["merged_confirmation_required"] is True
    assert ncd._outcome_records_a_broker_attempt(hold)


def test_an_afternoon_breaker_halt_is_a_trading_failure(monkeypatch):
    """The afternoon aggregate is an INDEPENDENT implementation of the morning
    executor's contract, so fixing `_apply_no_order_classification` there did
    not reach this one. Both halves of the same gap: a breaker trip contributes
    nothing to `failed` (its tail is a deliberate skip, and the trips can be
    confirmation-required holds), so a halted batch recorded an ordinary
    success -- `submitted` with no is_trading_failure when an order had landed,
    and `completed_no_orders` when none had (Codex review, PR #786: "Classify
    afternoon breaker halts as trading failures")."""
    import near_close_dca as ncd

    halted_tail = {
        "status": "skipped",
        "deliberate_skip": True,
        "placement_breaker_halted": True,
    }

    # ... with an order already submitted.
    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(
        artifacts, {"AAPL": {"status": "submitted"}, "GS": dict(halted_tail)}
    )
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["error_class"] == "partial_placement_failure"

    # ... and with nothing submitted at all, which falls through to the
    # zero-order classifier instead.
    artifacts = {"buy_candidates": [], "signal_rows": [], "usage_proxy": {}}
    ncd._apply_no_order_classification(
        artifacts,
        {
            "GS": {"status": "not_submitted_confirmation_required"},
            "EVR": dict(halted_tail),
        },
    )
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == (
        "failed_after_connector_native_recovery_attempt"
    )


def test_consecutive_failed_placements_halt_the_afternoon_batch(monkeypatch):
    """Isolation is per ticker, not unconditional. An unbroken run of failed
    placements means the broker leg itself is unhealthy, so every further
    candidate is a doomed live call (Codex review, PR #786: "Enforce the streak
    breaker in every live executor")."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("AAPL", "MSFT", "NVDA", "GS", "EVR")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)

    # A leftover outcomes dict from an earlier test would seed this batch and
    # get merged into its rows, masking the failures the breaker counts.
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    seen = []

    def _always_fails(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        # broker_placement_failed is what the breaker counts -- a real,
        # broker-facing placement call that failed, as opposed to a "failed"
        # row from a raise that never reached the broker.
        return ({"status": "failed", "order_id": None, "skip_reason": "broker unavailable",
                 "broker_placement_failed": True},
                None, running_deployed_7d, 1, 0)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _always_fails)

    candidate_outcomes, _t, _d, errors_count, _r = _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    assert len(seen) == 3, "the breaker must stop the batch, not attempt all five"
    # The halt is RECORDED, not just logged: every unattempted ticker gets an
    # explicit skipped row naming the breaker, so run_state.json can say which
    # orders were deliberately left alone and why.
    unattempted = [t for t in tickers if t not in seen]
    assert unattempted, "fixture must leave some candidates unattempted"
    for ticker in unattempted:
        row = candidate_outcomes[ticker]
        assert row["status"] == "skipped"
        assert row["placement_breaker_halted"] is True
        assert row["deliberate_skip"] is True
        assert "consecutive placement failures" in row["skip_reason"]
        assert row["submitted_amount_usd"] == 0.0
    # 3 real failures + 1 for the tripped breaker itself, so
    # manual_recovery_required is set even when a streak of
    # confirmation-required holds contributes nothing to errors_count.
    assert errors_count == 4


def test_a_submission_between_failures_resets_the_afternoon_streak(monkeypatch):
    """Cumulative counting would re-create halt-on-first-rejection on any batch
    with a few independently bad symbols."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("AAPL", "MSFT", "NVDA", "GS", "EVR", "IBM")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)

    # A leftover outcomes dict from an earlier test would seed this batch and
    # get merged into its rows, masking the failures the breaker counts.
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    seen = []

    def _alternating(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        if len(seen) % 2 == 1:
            return ({"status": "failed", "order_id": None, "skip_reason": "rejected",
                     "broker_placement_failed": True},
                    None, running_deployed_7d, 1, 0)
        # broker_placement_succeeded is what resets the streak -- a FRESH
        # placement, as opposed to a duplicate recovery that also reports
        # "submitted" without having placed anything this invocation.
        return ({"status": "submitted", "order_id": f"ORD-{decision.ticker}",
                 "submitted_amount_usd": 2.0, "broker_placement_succeeded": True},
                None, running_deployed_7d, 0, 0)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _alternating)

    candidate_outcomes, _t, _d, _e, _r = _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    assert len(seen) == len(tickers)
    assert sum(o["status"] == "submitted" for o in candidate_outcomes.values()) == 3


def test_a_pre_broker_raise_does_not_trip_the_broker_health_breaker(monkeypatch):
    """The breaker asks "is the broker leg unhealthy?", so it must count only
    real broker-facing placement failures. The batch's own last-resort net also
    assigns status "failed" -- to a candidate that raised BEFORE reaching the
    broker (bad quote arithmetic, a ledger error) -- and three of those would
    otherwise abandon every remaining ticker with no placement having failed at
    all (Codex review, PR #786: "Base the breaker on actual placement
    failures")."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("AAPL", "MSFT", "NVDA", "GS", "EVR")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    seen = []

    def _raises_before_the_broker(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        raise ValueError("malformed quote arithmetic")

    monkeypatch.setattr(ncd, "_process_buy_candidate", _raises_before_the_broker)

    candidate_outcomes, _t, _d, errors_count, _r = _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    assert seen == list(tickers), "every ticker must still be evaluated"
    assert all(o["status"] == "failed" for o in candidate_outcomes.values())
    assert not any(o.get("broker_placement_failed") for o in candidate_outcomes.values())
    assert errors_count == len(tickers)


def test_confirmation_required_placements_count_toward_the_streak(monkeypatch):
    """A broker refusing every direct submission is a real broker-facing
    failure that produced no order -- exactly the systemic condition the
    breaker exists to stop -- even though its status is not "failed" (Codex
    review, PR #786: "Count confirmation-required placement failures")."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("AAPL", "MSFT", "NVDA", "GS", "EVR")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    seen = []

    def _confirmation_required(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        return (
            {
                "status": "not_submitted_confirmation_required",
                "order_id": None,
                "skip_reason": "broker requires post-review confirmation before submission",
                "broker_placement_failed": True,
            },
            None,
            running_deployed_7d,
            0,
            1,
        )

    monkeypatch.setattr(ncd, "_process_buy_candidate", _confirmation_required)

    _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    assert len(seen) == 3, "the breaker must stop the batch after three refusals"
    # A confirmation-required streak contributes nothing to errors_count on its
    # own, so the tripped breaker is what has to set manual_recovery_required.
    _outcomes = ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]
    assert any(o.get("placement_breaker_halted") for o in _outcomes.values())


def test_a_malformed_provider_signal_is_normalized_instead_of_poisoning_artifacts():
    """A provider that RETURNS an unexpected value instead of raising is not
    caught by _safe_signal's try/except, and the value flows all the way to
    _signal_row's `signal.action.value` while BUILDING THE ARTIFACTS -- long
    after other candidates placed, so a raise there costs the whole run its
    artifacts rather than costing this ticker its signal (Codex review,
    PR #786: "Replace malformed signals in the reconciliation fallback")."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(dry_run=True, tickers=("AAPL",))

    class _MalformedProvider(_FakeData):
        pass

    fin = _MalformedProvider(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))

    import near_close_dca as ncd

    original = ncd.evaluate_source_signal

    def _returns_garbage(*, ticker, now, config, data, interval_minutes):
        if data.source == SourceName.FINANCE:
            return {"not": "a signal"}
        return original(
            ticker=ticker, now=now, config=config, data=data, interval_minutes=interval_minutes
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ncd, "evaluate_source_signal", _returns_garbage)
        decisions, errors_count = _evaluate_all_signals(now, config, ["AAPL"], fin, rh)

    assert errors_count == 1
    decision = decisions[0]
    # Normalized at the boundary, so nothing downstream ever sees a non-signal.
    assert isinstance(decision.finance_signal, SourceSignal)
    assert decision.finance_signal.action == Action.NO_DATA
    assert decision.finance_signal.provider_error is True
    # The artifact builder can now serialize it without raising -- the whole
    # point of normalizing rather than merely catching.
    row = ncd._signal_row("AAPL", "finance", decision.finance_signal)
    assert row["action"] == Action.NO_DATA.value


def test_a_duplicate_recovery_does_not_reset_the_streak(monkeypatch):
    """A same-stream duplicate recovery reports status "submitted" while making
    no successful placement call of its own, so resetting on status alone let a
    failure-duplicate-failure-failure sequence during an outage keep resetting
    and never trip (Codex review, PR #786: "Reset the breaker only after a
    fresh placement succeeds")."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("AAPL", "MSFT", "NVDA", "GS", "EVR")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    seen = []

    def _failure_then_duplicate(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        if len(seen) == 2:
            # The duplicate-recovery shape: "submitted", a real order id from an
            # EARLIER attempt, and no fresh placement this invocation.
            return ({"status": "submitted", "order_id": "ORD-EARLIER",
                     "submitted_amount_usd": 2.0, "duplicate_preserved": True},
                    None, running_deployed_7d, 0, 0)
        return ({"status": "failed", "order_id": None, "skip_reason": "broker unavailable",
                 "broker_placement_failed": True},
                None, running_deployed_7d, 1, 0)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _failure_then_duplicate)

    _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    # fail, duplicate (no reset), fail, fail -> tripped on the third real failure.
    assert len(seen) == 4


def test_a_whole_share_placement_failure_counts_toward_the_streak(monkeypatch):
    """A mixed sequence -- one whole-share failure then two fractional ones --
    is still three real broker failures. Leaving the whole-share path unmarked
    meant the threshold was never reached and live calls continued through the
    systemic condition the breaker exists to stop (Codex review, PR #786:
    "Count whole-share failures toward the placement streak")."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("BYDDY", "AAPL", "MSFT", "NVDA", "GS")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    seen = []

    def _mixed_failures(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        if decision.ticker == "BYDDY":
            # A whole-share no-order placement failure, as
            # _process_whole_share_candidate now reports one.
            return ({"status": "failed", "order_id": None,
                     "skip_reason": "whole-share placement failed",
                     "broker_placement_failed": True,
                     "whole_share_quantity": 0},
                    None, running_deployed_7d, 1, 0)
        return ({"status": "failed", "order_id": None, "skip_reason": "broker unavailable",
                 "broker_placement_failed": True},
                None, running_deployed_7d, 1, 0)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _mixed_failures)

    _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    assert len(seen) == 3, "the whole-share failure must count toward the same streak"


def test_every_whole_share_no_order_branch_marks_a_broker_failure():
    """Guards the marking itself: every no-order outcome the whole-share
    placement handler can return must carry broker_placement_failed, and every
    recovered/existing-order outcome must not."""
    import inspect
    import re

    import near_close_dca as ncd

    source = inspect.getsource(ncd._process_whole_share_candidate)
    # Each outcome dict literal, paired with the status it declares.
    for block in re.findall(r'\{[^{}]*"status": "(\w+)",\n(.*?)\n\s*\}', source, re.S):
        status, body = block
        marked = "broker_placement_failed" in body
        if status in {"failed", "not_submitted_confirmation_required"}:
            assert marked, f"{status} outcome is missing broker_placement_failed:\n{body[:200]}"
        if status in {"submitted", "skipped"}:
            assert not marked, f"{status} outcome must not claim a placement failure:\n{body[:200]}"


def test_an_isolated_failure_row_validates_against_the_run_state_schema():
    """_failure_candidate_row runs precisely when a run failed -- when its
    artifact is the only record left -- and run_state.schema.json REQUIRES a
    numeric planned_amount_usd. An outcome without a submitted amount used to
    write null there, making that recovery artifact schema-invalid exactly when
    it matters most (Codex review, PR #786: "Preserve a numeric planned amount
    for isolated failures")."""
    jsonschema = pytest.importorskip("jsonschema")
    import near_close_dca as ncd

    schema = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "schemas" / "run_state.schema.json")
        .read_text(encoding="utf-8")
    )
    row_schema = schema["properties"]["buy_candidates"]["items"]

    # The isolation net's own synthesized outcome...
    isolated = ncd._failure_candidate_row(
        "AAPL",
        {
            "status": "failed",
            "order_id": None,
            "skip_reason": "candidate processing raised RuntimeError",
            "candidate_processing_error": "candidate processing raised RuntimeError",
            "submitted_amount_usd": 0.0,
        },
    )
    isolated.setdefault("buy_multiplier", 0.0)
    jsonschema.validate(isolated, row_schema)
    assert isolated["planned_amount_usd"] == 0.0

    # ...and any outcome that omits the amount entirely, which is the general
    # hole the coercion closes.
    bare = ncd._failure_candidate_row("MSFT", {"status": "failed"})
    bare.setdefault("buy_multiplier", 0.0)
    jsonschema.validate(bare, row_schema)
    assert bare["planned_amount_usd"] == 0.0


def test_a_blocked_candidate_does_not_count_toward_the_streak(monkeypatch):
    """A blocked candidate is a deliberate policy decision made BEFORE any
    broker submission (budget cap, spread gate, duplicate guard) and says
    nothing about broker health."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    tickers = ("AAPL", "MSFT", "NVDA", "GS", "EVR")
    config = StrategyConfig(
        dry_run=False,
        tickers=tickers,
        approved_test_dates=frozenset({trading_day}),
        max_consecutive_placement_failures=3,
    )
    broker = DryRunBroker(cash=500.0, reference_prices=dict.fromkeys(tickers, 98.0))
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)

    # A leftover outcomes dict from an earlier test would seed this batch and
    # get merged into its rows, masking the failures the breaker counts.
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    seen = []

    def _always_blocked(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        return ({"status": "blocked", "skip_reason": "spread too wide", "deliberate_skip": True},
                None, running_deployed_7d, 0, 0)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _always_blocked)

    _process_buy_candidates_batch(
        buy_candidates=[_isolation_buy_decision(t) for t in tickers], ctx=ctx
    )

    assert len(seen) == len(tickers), "policy blocks must never trip the broker-health breaker"


def test_an_accepted_whole_share_order_that_never_published_fails_the_run(monkeypatch):
    """The isolation net decides halt-vs-isolate on whether an outcome was
    published, and the whole-share path does a lot of fallible work after the
    broker accepts but before its first publish -- an order-history lookup,
    int(float(...)) over a broker-supplied quantity, ledger settlement.

    A raise in that window used to reach the net with nothing published, so it
    was isolated: a zero-notional "failed" row for an order that is live at the
    broker, with its real spend missing from deployment, cap, and attempt
    accounting (Codex review, PR #786: "Halt on unpublished whole-share
    acceptances")."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    config = StrategyConfig(
        dry_run=False, tickers=("AAPL",), approved_test_dates=frozenset({trading_day})
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    ncd._IN_FLIGHT_WHOLE_SHARE_ACCEPTANCES.clear()

    def _accept_then_boom(*, decision, ctx, running_deployed_7d):
        # Exactly what _process_whole_share_candidate does: mark at the
        # placement site, then raise in the post-placement work before any
        # outcome has been published.
        ncd._mark_whole_share_acceptance(decision.ticker, "WS-ORD-1", 23.36)
        raise OverflowError("int too large to convert to float")

    monkeypatch.setattr(ncd, "_process_buy_candidate", _accept_then_boom)

    with pytest.raises(OverflowError):
        _process_buy_candidates_batch(
            buy_candidates=[_isolation_buy_decision("AAPL")], ctx=ctx
        )

    # The accepted order still has to reach the failure writer, which reads
    # exactly this dict -- otherwise failing the run would ALSO lose it.
    published = ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["AAPL"]
    assert published["status"] == "submitted"
    assert published["order_id"] == "WS-ORD-1"
    assert published["submitted_amount_usd"] == 23.36
    assert "OverflowError" in published["candidate_processing_error"]
    # Type only, never the exception's message.
    assert "int too large" not in published["candidate_processing_error"]

    # And it survives serialization into the recovery artifact.
    row = ncd._failure_candidate_row("AAPL", published)
    assert row["order_id"] == "WS-ORD-1"
    assert row["planned_amount_usd"] == 23.36


def test_a_pre_broker_raise_is_still_isolated_on_the_whole_share_path(monkeypatch):
    """The guard against over-correcting the finding above: a whole-share
    candidate that raises WITHOUT the broker having accepted anything leaves no
    acceptance marker, so it isolates exactly as every other pre-broker raise
    does. Without this, the fix would quietly restore halt-on-first-failure for
    whole-share tickers."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    config = StrategyConfig(
        dry_run=False, tickers=("AAPL", "MSFT"), approved_test_dates=frozenset({trading_day})
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0, "MSFT": 98.0})
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)
    ncd._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    # A stale marker from an EARLIER candidate must not be read as this one's.
    ncd._IN_FLIGHT_WHOLE_SHARE_ACCEPTANCES["AAPL"] = ("WS-ORD-STALE", 23.36)

    seen = []

    def _boom_before_the_broker(*, decision, ctx, running_deployed_7d):
        seen.append(decision.ticker)
        raise RuntimeError("quote arithmetic failed")

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_before_the_broker)

    candidate_outcomes, _top_up, _deployed, errors_count, _reviews = (
        _process_buy_candidates_batch(
            buy_candidates=[_isolation_buy_decision(t) for t in ("AAPL", "MSFT")], ctx=ctx
        )
    )

    assert seen == ["AAPL", "MSFT"], "the rest of the batch must still be attempted"
    assert candidate_outcomes["AAPL"]["status"] == "failed"
    assert candidate_outcomes["AAPL"]["submitted_amount_usd"] == 0.0
    assert errors_count == 2


def test_a_raising_candidate_does_not_erase_a_seeded_cross_day_recovery(monkeypatch):
    """The seeded pre-roll recovery is a DIFFERENT order from today's failed
    candidate; the existing aggregation must still preserve it."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    config = StrategyConfig(
        dry_run=False, tickers=("AAPL",), approved_test_dates=frozenset({trading_day})
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    ctx = _isolation_context(config, StrategyState(), broker, now, trading_day)

    def _boom(*, decision, ctx, running_deployed_7d):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom)

    candidate_outcomes, _top_up, _deployed, errors_count, _reviews = (
        _process_buy_candidates_batch(
            buy_candidates=[_isolation_buy_decision("AAPL")],
            ctx=ctx,
            initial_candidate_outcomes={
                "AAPL": {
                    "status": "submitted",
                    "order_id": "recovered-from-yesterday",
                    "submitted_amount_usd": 5.0,
                }
            },
        )
    )

    outcome = candidate_outcomes["AAPL"]
    assert outcome["status"] == "submitted"
    assert "recovered-from-yesterday" in (outcome.get("whole_share_order_ids") or [])
    assert outcome["submitted_amount_usd"] == 5.0
    assert errors_count == 1


def test_one_raising_candidate_still_leaves_a_completed_run(tmp_path, monkeypatch):
    """End to end: the run finishes and writes its normal artifact set instead of
    unwinding into the failure writer, with the bad ticker recorded as failed."""
    import near_close_dca as ncd

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    config = replace(config, dry_run=False)
    artifacts_dir = os.path.join(tmp_path, "runs", "isolated-failure")

    original = ncd._process_buy_candidate

    def _boom_for_nvda(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "NVDA":
            raise RuntimeError("NVDA blew up")
        return original(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_nvda)

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        run_state = json.load(handle)

    rows = {row["ticker"]: row for row in run_state["buy_candidates"]}
    assert rows["NVDA"]["status"] == "failed"
    # The other two symbols were still evaluated and still submitted.
    assert rows["AAPL"]["status"] == "submitted"
    assert rows["MSFT"]["status"] == "submitted"
    assert run_state["usage_proxy"]["errors_count"] >= 1
    # A partially failed batch is a real trading failure as well as a partial
    # success -- both facts are recorded, neither is dropped.
    assert run_state["usage_proxy"]["manual_recovery_required"] is True


def test_signal_reconciliation_failure_degrades_only_that_ticker(monkeypatch, caplog):
    """reconcile_signals sits outside _safe_signal's provider try/except; a raise
    there used to abort signal evaluation for every remaining ticker, before any
    placement had happened at all."""
    import near_close_dca as ncd

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(dry_run=True, tickers=("AAPL", "MSFT", "NVDA"))
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))

    original = ncd.reconcile_signals

    def _boom_for_msft(ticker, finance_signal, robinhood_signal, *args, **kwargs):
        if ticker == "MSFT":
            raise ValueError("unexpected signal shape")
        return original(ticker, finance_signal, robinhood_signal, *args, **kwargs)

    monkeypatch.setattr(ncd, "reconcile_signals", _boom_for_msft)

    with caplog.at_level(logging.ERROR, logger="near_close_dca"):
        decisions, errors_count = _evaluate_all_signals(
            now, config, ["AAPL", "MSFT", "NVDA"], fin, rh
        )

    by_ticker = {d.ticker: d for d in decisions}
    assert set(by_ticker) == {"AAPL", "MSFT", "NVDA"}
    # Fail closed: never an auto-buy on a decision that could not be computed.
    assert by_ticker["MSFT"].action == Action.MANUAL_REVIEW
    # Type only, never the exception's message: the reason is persisted into
    # signals.csv/run_state.json (AGENTS.md secrets policy).
    assert by_ticker["MSFT"].reason == "signal reconciliation failed: ValueError"
    assert by_ticker["NVDA"].action != Action.MANUAL_REVIEW
    assert errors_count == 1
    assert "MSFT" in caplog.text


def test_a_provider_error_is_logged_per_ticker(caplog):
    """errors_count alone never says WHICH symbol failed; the degradation is
    otherwise invisible in signals.csv."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(dry_run=True, tickers=("AAPL",))

    class _RaisingProvider(_FakeData):
        def get_previous_close_bar(self, ticker, as_of):
            raise RuntimeError("provider is down")

    fin = _RaisingProvider(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))

    with caplog.at_level(logging.WARNING, logger="near_close_dca"):
        _decisions, errors_count = _evaluate_all_signals(now, config, ["AAPL"], fin, rh)

    assert errors_count == 1
    assert "AAPL" in caplog.text
    assert "provider is down" in caplog.text


def test_empty_universe_still_writes_a_signals_header(tmp_path):
    """No active tickers previously wrote a zero-byte signals.csv, which the new
    header requirement would reject, failing the run and blocking the pointer
    update (Codex review)."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True, tickers=(), approved_test_dates=frozenset({date(2026, 6, 29)})
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "empty-universe")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    _assert_csv_header(os.path.join(artifacts_dir, "signals.csv"))
    _assert_csv_header(os.path.join(artifacts_dir, "buy_candidates.csv"))

    from scripts.scheduled_artifacts import validate_run_artifacts

    assert validate_run_artifacts(artifacts_dir, require_summary=True)


def test_a_manual_afternoon_placement_failure_is_not_a_scheduled_recovery_failure(
    tmp_path,
):
    """run_once(scheduled=False) never runs the connector-native recovery policy,
    so its broker failures must not be attributed to it (Codex review)."""

    class _FailingBroker(DryRunBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            raise RuntimeError("broker rejected the order")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False,
        tickers=config.tickers,
        approved_test_dates=config.approved_test_dates,
    )
    broker = _FailingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "manual-placement-failed")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=False),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert run_state["run_classification_outcome"] == "failed_placement"
    assert run_state["error_class"] == "failed_placement"
    assert run_state["is_trading_failure"] is True


def test_afternoon_partially_submitted_batch_records_the_failed_placement(tmp_path):
    """One order landed and another ticker raised: an ordinary success record
    would hide the failed order (Codex review)."""

    class _PartlyFailingBroker(DryRunBroker):
        def place_market_buy(self, ticker, notional_usd, ref_id):
            if ticker == "NVDA":
                raise RuntimeError("broker rejected NVDA")
            return super().place_market_buy(ticker, notional_usd, ref_id)

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False,
        tickers=config.tickers,
        approved_test_dates=config.approved_test_dates,
    )
    broker = _PartlyFailingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "partial-batch")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    assert run_state["orders_submitted_count"] >= 1
    assert run_state["orders_failed_count"] == 1
    assert run_state["run_classification_outcome"] == "failed_after_partial_submission"
    assert run_state["is_trading_failure"] is True
    # The summary carries it too, so a summary-only consumer sees the failure.
    with open(
        os.path.join(artifacts_dir, "run_summary.json"), encoding="utf-8"
    ) as handle:
        summary = json.load(handle)
    assert summary["is_trading_failure"] is True


def test_a_retry_keeps_an_earlier_attempts_orders_in_the_aggregate(tmp_path):
    """A scheduled retry overwrites the fixed-path artifact; counting only the
    current broker_outcomes would record a zero-order run for a slot that had
    already placed orders (Codex review). Morning-stream behavior."""
    import morning_dca as _m

    outcomes = {"AAA": {"status": "submitted", "order_id": "o-new"}}
    carried = [
        {"ticker": "AAA", "status": "submitted", "order_id": "o-new"},
        {
            "ticker": "BBB",
            "status": "submitted",
            "order_id": "o-earlier",
            "planned_amount_usd": 2.0,
        },
    ]

    # Counted once for AAA (present in both) plus once for the carried-forward BBB.
    assert len(_m._submitted_keys(outcomes, carried)) == 2
    extra = _m._carried_forward_order_rows(outcomes, carried)
    assert [row["ticker"] for row in extra] == ["BBB"]
    assert extra[0]["order_id"] == "o-earlier"
    assert extra[0]["submitted_amount_usd"] == 2.0


def test_afternoon_startup_failure_still_writes_the_complete_artifact_set(tmp_path):
    """Nothing wrapped run_once, so an exception before _finalize_and_write_artifacts
    left the slot with no artifacts at all (Codex review)."""
    from scripts.same_day_pointer import derive_valid_scheduled_evidence
    from scripts.scheduled_artifacts import validate_run_artifacts

    class _ExplodingBroker(DryRunBroker):
        def account_number(self):
            raise RuntimeError("account lookup exploded")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    broker = _ExplodingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "afternoon-startup-failure")

    with pytest.raises(RuntimeError):
        run_daily_compact(
            now,
            config,
            state,
            fin,
            rh,
            broker,
            options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
        )

    for name in (
        "run_state.json",
        "run_summary.json",
        "signals.csv",
        "submitted_orders.csv",
    ):
        assert os.path.exists(os.path.join(artifacts_dir, name)), f"{name} missing"
    assert validate_run_artifacts(artifacts_dir, require_summary=True)

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    assert run_state["status"] == "failed"
    assert run_state["error_class"] == "scheduled_run_startup_failure"
    assert run_state["is_trading_failure"] is True
    assert run_state["orders_submitted_count"] == 0
    # Proof the slot fired, never scheduled evidence.
    assert derive_valid_scheduled_evidence(run_state) is False


def test_an_afternoon_failure_after_placement_preserves_the_order(tmp_path):
    """A later ticker raising must not erase an earlier accepted order, or a
    same-day guard would re-allocate that capital (Codex review)."""

    class _FailAfterFirstBroker(DryRunBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._placed = 0

        def place_market_buy(self, ticker, notional_usd, ref_id):
            if self._placed >= 1:
                raise RuntimeError("connector died mid-batch")
            self._placed += 1
            return super().place_market_buy(ticker, notional_usd, ref_id)

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False,
        tickers=config.tickers,
        approved_test_dates=config.approved_test_dates,
    )
    broker = _FailAfterFirstBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "afternoon-mid-batch")

    # The batch catches per-candidate placement errors, so this completes rather
    # than raising; either way the accepted order must survive in the artifact.
    try:
        run_daily_compact(
            now,
            config,
            state,
            fin,
            rh,
            broker,
            options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
        )
    except RuntimeError:
        pass

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    assert run_state["orders_submitted_count"] >= 1
    submitted = [
        c for c in run_state["buy_candidates"] if c.get("status") == "submitted"
    ]
    assert submitted and submitted[0].get("order_id")


def test_an_evidence_denied_stub_is_not_selected_for_scheduled_reporting(tmp_path):
    """is_scheduled_state must honor the denial marker too, or the reporting
    selector returns a blocked stub as a valid scheduled run (Codex review)."""
    from scripts.run_classification import (
        build_scheduled_classification,
        is_scheduled_state,
    )

    denied = {
        "trading_day": "2026-07-01",
        "status": "blocked",
        "run_classification": build_scheduled_classification("2026-07-01"),
        "scheduled_evidence_denied": True,
    }
    assert is_scheduled_state(denied) is False
    # The same state without the marker is still a normal scheduled state.
    allowed = dict(denied)
    allowed.pop("scheduled_evidence_denied")
    assert is_scheduled_state(allowed) is True


def test_an_accepted_order_survives_a_raise_in_post_placement_work(tmp_path):
    """The batch only records an outcome once _process_buy_candidate RETURNS, so a
    raise in the fill lookup between placement and return would lose a real,
    already-accepted order (Codex review)."""

    class _FillLookupExplodes(DryRunBroker):
        def get_order_fill(self, order_id):
            raise RuntimeError("fill lookup exploded after the order was accepted")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=False, tickers=("AAPL",), approved_test_dates=config.approved_test_dates
    )
    broker = _FillLookupExplodes(cash=500.0, reference_prices={"AAPL": 98.0})
    artifacts_dir = os.path.join(tmp_path, "runs", "post-placement-raise")

    try:
        run_daily_compact(
            now,
            config,
            state,
            fin,
            rh,
            broker,
            options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
        )
    except RuntimeError:
        pass

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    # The accepted order is in the artifact no matter which path wrote it.
    assert run_state["orders_submitted_count"] >= 1
    submitted = [
        c for c in run_state.get("buy_candidates", []) if c.get("status") == "submitted"
    ]
    assert submitted and submitted[0].get("order_id"), run_state.get("buy_candidates")


def test_an_afternoon_failure_refreshes_the_latest_scheduled_pointer(tmp_path):
    """Otherwise latest_scheduled_run.json keeps targeting the previous trading
    day even though today's failed slot has artifacts (Codex review)."""
    from scripts.run_classification import latest_scheduled_run_path

    class _ExplodingBroker(DryRunBroker):
        def account_number(self):
            raise RuntimeError("account lookup exploded")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    broker = _ExplodingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "pointer-on-failure")
    classification_root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")

    with pytest.raises(RuntimeError):
        run_daily_compact(
            now,
            config,
            state,
            fin,
            rh,
            broker,
            options=CompactRunOptions(
                artifacts_dir=artifacts_dir,
                scheduled=True,
                classification_state_root=classification_root,
            ),
        )

    pointer = latest_scheduled_run_path(pathlib.Path(classification_root))
    assert pointer.exists(), "the scheduled pointer was never refreshed"
    payload = json.loads(pointer.read_bytes())
    assert payload["run_state_path"] == os.path.join(artifacts_dir, "run_state.json")
    # The state it points at denies its own evidence, so following the pointer
    # still refuses to count the run.
    assert payload["valid_scheduled_evidence"] is False


def test_an_afternoon_failure_state_carries_the_scheduled_run_id(tmp_path):
    """schemas/run_state.schema.json requires it and build_run_summary reads it
    (Codex review)."""

    class _ExplodingBroker(DryRunBroker):
        def account_number(self):
            raise RuntimeError("boom")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    broker = _ExplodingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "failure-run-id")

    with pytest.raises(RuntimeError):
        run_daily_compact(
            now,
            config,
            state,
            fin,
            rh,
            broker,
            options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
        )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    assert run_state["run_id"], "failure state has no top-level run_id"
    with open(
        os.path.join(artifacts_dir, "run_summary.json"), encoding="utf-8"
    ) as handle:
        summary = json.load(handle)
    assert summary["run_id"] == run_state["run_id"]


def test_an_unreadable_blocked_state_is_replaced_not_wrapped(tmp_path):
    """A truncated or non-object run_state.json must not be left in place with a
    fresh summary and CSVs written around it (Codex review)."""
    artifacts_dir = pathlib.Path(tmp_path) / "runs" / "corrupt"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "run_state.json").write_text("{ truncated", encoding="utf-8")

    _now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    gated_now = datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN)  # not an approved date

    run_daily_compact(
        gated_now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=str(artifacts_dir)),
    )

    run_state = json.loads(
        (artifacts_dir / "run_state.json").read_text(encoding="utf-8")
    )
    assert run_state["status"] == "blocked"
    assert run_state["completion_reason"] == "date_or_holiday_gate_blocked"

    from scripts.scheduled_artifacts import validate_run_artifacts

    assert validate_run_artifacts(str(artifacts_dir), require_summary=True)


def test_a_failing_afternoon_run_with_broken_companions_denies_its_own_evidence(
    tmp_path,
):
    """The failure handler must not publish a valid pointer for a state whose
    four-file set is incomplete or corrupt (Codex review)."""
    from scripts.run_classification import latest_scheduled_run_path

    class _ExplodingBroker(DryRunBroker):
        def account_number(self):
            raise RuntimeError("boom")

    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    broker = _ExplodingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    artifacts_dir = pathlib.Path(tmp_path) / "runs" / "broken-companions"
    artifacts_dir.mkdir(parents=True)
    classification_root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    # A state from an earlier attempt, with a summary that does not mirror it.
    (artifacts_dir / "run_state.json").write_text(
        json.dumps(
            {
                "run_id": "earlier",
                "trading_day": "2026-06-29",
                "run_classification": {
                    "run_origin": "scheduled",
                    "manual_run": False,
                    "stream": "afternoon",
                    "scheduled_for_et": "15:30",
                },
            }
        ),
        encoding="utf-8",
    )
    (artifacts_dir / "run_summary.json").write_text(
        json.dumps(
            {
                "run_id": "stale",
                "run_classification": {
                    "run_origin": "scheduled",
                    "manual_run": False,
                    "stream": "morning",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError):
        run_daily_compact(
            now,
            config,
            state,
            fin,
            rh,
            broker,
            options=CompactRunOptions(
                artifacts_dir=str(artifacts_dir),
                scheduled=True,
                classification_state_root=classification_root,
            ),
        )

    run_state = json.loads(
        (artifacts_dir / "run_state.json").read_text(encoding="utf-8")
    )
    assert (
        run_state["artifact_contract_failure"]["failure"] == "artifact_contract_failure"
    )
    assert run_state["scheduled_evidence_denied"] is True
    payload = json.loads(
        latest_scheduled_run_path(pathlib.Path(classification_root)).read_text(
            encoding="utf-8"
        )
    )
    assert payload["valid_scheduled_evidence"] is False


def test_a_cap_blocked_candidate_never_reaches_the_spread_gate(tmp_path):
    """A run stopped only by caps must not be misreported as missing_spread_data:
    the contract says a cap-blocked candidate carries no spread verdict (Codex
    review)."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    # Exhaust today's remaining budget so within_caps() rejects the candidate.
    state.select_daily_budget(0.0, 0, config)
    state.record_buy("ZZZ", date(2026, 6, 29), state.current_tier().daily_cap_usd)
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    # No bid/ask at all: if the cap check ran AFTER the spread lookup this run
    # would be blamed on the spread.
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "cap-before-spread")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["skip_reason"] == "cap would be exceeded"
    # No spread verdict: it never reached the gate.
    assert candidate["spread_status"] == "not_evaluated"
    assert run_state["candidates_reaching_spread_gate"] == 0
    assert run_state["error_class"] != "missing_spread_data"
    assert run_state["completion_reason"] == "blocked_before_spread_gate"


def test_run_daily_compact_stops_at_the_order_count_cap(tmp_path):
    """End-to-end proof that the roster-wide daily order-count circuit breaker
    (config/budgets.json circuit_breakers.max_orders_per_day) actually binds
    on the real live path -- run_daily_compact is what run_dca.run_slot() /
    run_pipeline() call for every one of the seven scheduled slots, unlike
    dca_runner.plan_orders(), which nothing live invokes (Codex review:
    "Apply the order-count cap to every live placement path"). Ample dollar
    headroom remains; only the count cap should block this candidate."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        max_orders_per_day=1,
    )
    state = StrategyState()
    state.select_daily_budget(0.0, 0, config)
    # One order already booked today -- at the cap, with the daily $ cap
    # nowhere near exhausted.
    state.record_buy("ZZZ", date(2026, 6, 29), 1.0)
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "order-count-cap")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["skip_reason"] == "cap would be exceeded"
    assert run_state["completion_reason"] == "blocked_before_spread_gate"


def _afternoon_merge(preserved, placed, exc=None, tmp_path=None):
    """Drive _write_afternoon_failure_artifacts over a preserved state."""
    import near_close_dca as _n

    if exc is None:
        exc = RuntimeError("boom")
    if tmp_path is None:
        tmp_path = tempfile.mkdtemp()

    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_state.json").write_text(json.dumps(preserved), encoding="utf-8")
    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = placed
    try:
        _n._write_afternoon_failure_artifacts(out, _now_et(), exc)
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)
    return out, json.loads((out / "run_state.json").read_bytes())


def _now_et():
    return datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)


def _preserved_state() -> dict[str, Any]:
    return {
        "run_id": "earlier",
        "trading_day": "2026-06-29",
        "status": "completed",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
            "scheduled_for_et": "15:30",
        },
        "run_classification_outcome": "completed_no_orders",
        "order_submit_status": "submitted",
        "is_trading_failure": False,
        "broker_outcomes": {
            "AAA": {
                "status": "submitted",
                "order_id": "o-earlier",
                "submitted_amount_usd": 2.0,
            }
        },
        "buy_candidates": [
            {"ticker": "AAA", "status": "submitted", "order_id": "o-earlier"}
        ],
        "orders_submitted_count": 1,
    }


def test_a_retrys_second_order_for_the_same_ticker_is_not_dropped(tmp_path):
    """A ticker-keyed merge kept only the earlier order while the count and CSV
    are derived from those outcomes (Codex review)."""
    placed = {
        "AAA": {
            "status": "submitted",
            "order_id": "o-retry",
            "submitted_amount_usd": 1.0,
        }
    }

    out, run_state = _afternoon_merge(_preserved_state(), placed, tmp_path=tmp_path)

    assert run_state["orders_submitted_count"] == 2
    order_ids = {c.get("order_id") for c in run_state["buy_candidates"]}
    assert order_ids == {"o-earlier", "o-retry"}
    with (out / "submitted_orders.csv").open(newline="", encoding="utf-8") as handle:
        csv_ids = {row["order_id"] for row in csv.DictReader(handle)}
    assert csv_ids == {"o-earlier", "o-retry"}


def test_a_seeded_recovery_refreshes_failure_state_derived_counts(tmp_path):
    """Regression test for "Refresh failure-state counts for seeded
    recoveries". When a recovered lease is appended to buy_candidates/
    broker_outcomes on a preserved-state retry, counts.buy_candidates and the
    usage_proxy's buy_candidates_written/orders_placed_count must be
    recomputed too, not just counts.submitted -- otherwise the regenerated
    run_summary.json exposes the prior attempt's stale candidate count.
    """
    preserved = _preserved_state()
    preserved["usage_proxy"] = {
        "csv_artifact_count": 1,
        "state_file_bytes": 1,
        "total_artifact_bytes": 1,
        "buy_candidates_written": 1,
        "orders_placed_count": 1,
    }
    placed = {
        "BBB": {
            "status": "submitted",
            "order_id": "o-retry",
            "submitted_amount_usd": 1.0,
        }
    }

    _out, run_state = _afternoon_merge(preserved, placed, tmp_path=tmp_path)

    assert len(run_state["buy_candidates"]) == 2
    assert run_state["counts"]["buy_candidates"] == 2
    assert run_state["usage_proxy"]["buy_candidates_written"] == 2
    assert run_state["usage_proxy"]["orders_placed_count"] == 2


def test_a_malformed_prior_broker_outcomes_field_is_treated_as_unusable(tmp_path):
    """Regression test for "Reject malformed outcomes before preserving the
    prior state". A same-slot run_state.json that parsed fine and matched
    identity, but whose broker_outcomes field is a non-mapping (a corrupted
    list), must not be treated as usable/preservable: the merge branch
    unconditionally does `dict(merged.get("broker_outcomes") or {})`, which
    raises for a non-empty non-mapping value -- possibly AFTER this
    invocation already placed a real order. Must fall through to full
    synthesis instead of crashing.
    """
    preserved = _preserved_state()
    preserved["broker_outcomes"] = ["not", "a", "mapping"]
    placed = {
        "BBB": {
            "status": "submitted",
            "order_id": "o-retry",
            "submitted_amount_usd": 1.0,
        }
    }

    # Must not raise.
    _out, run_state = _afternoon_merge(preserved, placed, tmp_path=tmp_path)

    # The malformed prior state was replaced wholesale (synthesized), not
    # preserved-and-merged -- the earlier run_id is gone and only the newly
    # placed order survives.
    assert run_state["run_id"] != "earlier"
    order_ids = {c.get("order_id") for c in run_state["buy_candidates"]}
    assert order_ids == {"o-retry"}


def test_a_merged_retry_is_recorded_as_a_post_submission_failure(tmp_path):
    """Retaining the earlier attempt's successful classification would let the
    canonical state claim the retry succeeded (Codex review)."""
    placed = {
        "BBB": {
            "status": "submitted",
            "order_id": "o-retry",
            "submitted_amount_usd": 1.0,
        }
    }

    _out, run_state = _afternoon_merge(_preserved_state(), placed, tmp_path=tmp_path)

    assert run_state["status"] == "failed"
    assert run_state["run_classification_outcome"] == "failed_after_partial_submission"
    assert run_state["error_class"] == "scheduled_run_failed_after_submission"
    assert run_state["is_trading_failure"] is True
    assert run_state["scheduled_evidence_denied"] is True
    assert run_state["run_classification"]["valid_scheduled_evidence"] is False


def test_the_summary_is_regenerated_after_a_merge(tmp_path):
    """A pre-merge summary passes validation while reporting the previous
    attempt's count and outcome (Codex review)."""
    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True)
    (out / "run_summary.json").write_text(
        json.dumps(
            {
                "run_id": "earlier",
                "orders_submitted_count": 1,
                "run_classification_outcome": "completed_no_orders",
            }
        ),
        encoding="utf-8",
    )
    placed = {
        "BBB": {
            "status": "submitted",
            "order_id": "o-retry",
            "submitted_amount_usd": 1.0,
        }
    }

    _out, run_state = _afternoon_merge(_preserved_state(), placed, tmp_path=tmp_path)

    summary = json.loads((out / "run_summary.json").read_bytes())
    assert summary["orders_submitted_count"] == run_state["orders_submitted_count"] == 2
    assert summary["run_classification_outcome"] == "failed_after_partial_submission"


def test_scheduled_afternoon_blocks_wide_spread_before_ordering(tmp_path):
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        max_bid_ask_spread_pct=1.0,
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    # 97 x 99 is a ~2.04% midpoint spread, above the configured 1% hard breaker.
    rh = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.0, 99.0)
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "wide-spread")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert candidate["status"] == "blocked"
    assert "bid/ask spread" in candidate["skip_reason"]
    assert run_state["usage_proxy"]["orders_placed_count"] == 0


def test_scheduled_afternoon_blocks_when_spread_unavailable(tmp_path):
    # Codex review on PR #22: config/scheduled_tasks.json requires spread_ok=true
    # before any scheduled live order. A provider without get_bid_ask support (or
    # one whose quote returns nothing) must fail closed here, not silently bypass
    # the breaker the way it did when spread_pct was simply left as None.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    # Plain _FakeData has no get_bid_ask method at all.
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "missing-spread")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert candidate["status"] == "blocked"
    assert "spread" in candidate["skip_reason"]
    assert run_state["usage_proxy"]["orders_placed_count"] == 0


def test_scheduled_afternoon_blocks_non_finite_bid_ask_quote(tmp_path):
    # Codex review on the PR #42 merge commit: bid <= 0 / ask <= 0 / ask < bid
    # comparisons are all False against NaN, and float('inf') satisfies > 0, so a
    # provider returning a NaN or infinite bid/ask previously slipped past
    # _quote_bid_ask() and produced a NaN spread_pct. `NaN > max_bid_ask_spread_pct`
    # is also False, so the scheduled path would have placed an order against an
    # unusable quote instead of failing closed the way a missing quote already does.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        max_bid_ask_spread_pct=1.0,
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD,
        98.0,
        100.0,
        quote=(98.0, now),
        bid_ask=(float("nan"), 99.0),
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "nan-spread")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert candidate["status"] == "blocked"
    assert "spread" in candidate["skip_reason"]
    assert run_state["usage_proxy"]["orders_placed_count"] == 0


def test_manual_run_not_blocked_when_spread_unavailable(tmp_path):
    # The missing-spread fail-closed breaker is scoped to scheduled runs only:
    # a manual run still has review_market_buy as a human-reviewed safety net,
    # so it must not be blocked on a missing spread alone.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "manual-missing-spread")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=False),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert candidate["status"] != "blocked"


def test_scheduled_afternoon_spread_error_is_caught_and_recorded(tmp_path):
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    # Provider raises on get_bid_ask (e.g. halted or missing bid/ask fields).
    rh = _FakeDataWithRaisingBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now)
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "spread-error")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir, scheduled=True),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    [candidate] = run_state["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert candidate["status"] == "blocked"
    assert "spread quote error" in candidate["skip_reason"]
    assert run_state["usage_proxy"]["orders_placed_count"] == 0
    assert run_state["usage_proxy"]["errors_count"] == 1


def test_run_daily_compact_blocks_orders_above_per_order_ceiling(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-max-order")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=config.approved_test_dates,
        base_buy_amount_usd=10.0,
        # The 2% drop below is 1.5x on the current ladder, so a $10 base plans
        # $15; the ceiling sits below that so the candidate is blocked.
        max_per_order_usd=10.0,
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)

    candidate = run_state["buy_candidates"][0]
    assert candidate["ticker"] == "AAPL"
    assert candidate["planned_amount_usd"] == 15.0
    assert candidate["status"] == "blocked"
    assert candidate["skip_reason"] == "max per-order ceiling exceeded"
    assert run_state["usage_proxy"]["orders_placed_count"] == 0


def test_write_run_artifacts_ignores_stale_orders_csv(tmp_path):
    out = os.path.join(tmp_path, "runs", "reused-day")
    os.makedirs(out)
    with open(os.path.join(out, "orders.csv"), "w", encoding="utf-8") as handle:
        handle.write("ticker\nSTALE\n")

    artifacts = {
        "signal_rows": [{"ticker": "AAPL", "source": "finance"}],
        "buy_candidates": [],
        "usage_proxy": {
            "csv_artifact_count": None,
            "state_file_bytes": None,
            "total_artifact_bytes": None,
        },
    }
    write_run_artifacts(artifacts, out)

    with open(os.path.join(out, "run_state.json"), encoding="utf-8") as handle:
        usage = json.load(handle)["usage_proxy"]
    # A leftover orders.csv from a previous run is not attributed to this run.
    assert usage["csv_artifact_count"] == 2
    state_size = os.path.getsize(os.path.join(out, "run_state.json"))
    assert usage["state_file_bytes"] == state_size
    written_csv_bytes = os.path.getsize(
        os.path.join(out, "signals.csv")
    ) + os.path.getsize(os.path.join(out, "buy_candidates.csv"))
    assert usage["total_artifact_bytes"] == state_size + written_csv_bytes


def test_scheduled_finalize_archives_only_after_the_contract_is_written(tmp_path, monkeypatch):
    # Codex review ("Finalize scheduled artifacts before waiting on archive
    # locks"): a per-ticker archive lock can block for several seconds when
    # another process is writing the same ticker file, but the just-fired-run
    # grace period a monitor uses to distinguish a genuinely incomplete run
    # from one still finishing defaults to only 20 seconds. Archiving must
    # happen strictly AFTER the scheduled contract's required
    # run_summary.json/submitted_orders.csv are written, never before, or
    # ordinary archive contention can make a successfully submitted run
    # appear incomplete and trigger a false escalation.
    import near_close_dca as m

    call_order = []
    monkeypatch.setattr(m, "build_run_artifacts", lambda *a, **k: {"signal_rows": []})
    monkeypatch.setattr(
        m,
        "write_run_artifacts",
        lambda artifacts, artifacts_dir: (
            call_order.append("write_run_artifacts") or {"run_state": "run_state.json"}
        ),
    )
    monkeypatch.setattr(
        m,
        "_write_and_validate_scheduled_contract",
        lambda artifacts, artifacts_dir: (call_order.append("contract") or True),
    )
    monkeypatch.setattr(
        m,
        "_archive_intraday_observations",
        lambda artifacts, out, decisions=(), run_state_path=None: (call_order.append("archive") or None),
    )

    m._finalize_and_write_artifacts(
        now=datetime(2026, 6, 29, 15, 30, tzinfo=EASTERN),
        config=StrategyConfig(),
        state=StrategyState(),
        decisions=[],
        candidate_outcomes={},
        order_reviews_attempted=0,
        errors_count=0,
        notifications_sent=None,
        cost_control_notes=None,
        top_up_outcomes=None,
        scheduled=True,
        artifacts_dir=tmp_path,
        classification_state_root=None,
    )

    assert call_order == ["write_run_artifacts", "contract", "archive"]


def test_manual_finalize_archives_right_after_write_run_artifacts(tmp_path, monkeypatch):
    # A manual path has no scheduled contract step to wait for -- archiving
    # should proceed right after write_run_artifacts, and the contract
    # helper must never be invoked at all.
    import near_close_dca as m

    call_order = []
    monkeypatch.setattr(m, "build_run_artifacts", lambda *a, **k: {"signal_rows": []})
    monkeypatch.setattr(
        m,
        "write_run_artifacts",
        lambda artifacts, artifacts_dir: (
            call_order.append("write_run_artifacts") or {"run_state": "run_state.json"}
        ),
    )
    monkeypatch.setattr(
        m,
        "_write_and_validate_scheduled_contract",
        lambda artifacts, artifacts_dir: (call_order.append("contract") or True),
    )
    monkeypatch.setattr(
        m,
        "_archive_intraday_observations",
        lambda artifacts, out, decisions=(), run_state_path=None: (call_order.append("archive") or None),
    )

    m._finalize_and_write_artifacts(
        now=datetime(2026, 6, 29, 15, 30, tzinfo=EASTERN),
        config=StrategyConfig(),
        state=StrategyState(),
        decisions=[],
        candidate_outcomes={},
        order_reviews_attempted=0,
        errors_count=0,
        notifications_sent=None,
        cost_control_notes=None,
        top_up_outcomes=None,
        scheduled=False,
        artifacts_dir=tmp_path,
        classification_state_root=None,
    )

    assert call_order == ["write_run_artifacts", "archive"]


def test_archive_intraday_observations_persists_failure_to_run_state(tmp_path, monkeypatch):
    # Codex review ("Persist near-close archive failures in run state"):
    # unlike the morning-family path, a failed shared-archive write here
    # previously left no trace once process logs were gone -- the run_state
    # on disk never carried an intraday_archive.error a post-run consumer
    # could detect. Once run_state_path is supplied, an ok:false summary
    # must be annotated onto the artifacts dict AND persisted to that path,
    # without raising or blocking the (already-successful) trading run.
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: None)
    monkeypatch.setattr(
        m,
        "archive_observations_safely",
        lambda observations: {"ok": False, "error": "archive unavailable"},
    )

    run_state_path = tmp_path / "run_state.json"
    run_state_path.write_text(json.dumps({"run_id": "r1", "signal_rows": []}))
    artifacts = {"run_id": "r1", "signal_rows": [], "trading_day": "2026-08-03"}

    result = m._archive_intraday_observations(
        artifacts, tmp_path, run_state_path=run_state_path
    )

    assert result is None
    assert cast(dict, artifacts["intraday_archive"])["ok"] is False
    assert cast(dict, artifacts["intraday_archive"])["error"] == "archive unavailable"
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted["intraday_archive"]["ok"] is False
    assert persisted["intraday_archive"]["error"] == "archive unavailable"


def test_archive_intraday_observations_persists_success_summary_too(tmp_path, monkeypatch):
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: "snap.json")
    monkeypatch.setattr(
        m, "archive_observations_safely", lambda observations: {"ok": True}
    )

    run_state_path = tmp_path / "run_state.json"
    run_state_path.write_text(json.dumps({"run_id": "r1", "signal_rows": []}))
    artifacts = {"run_id": "r1", "signal_rows": [], "trading_day": "2026-08-03"}

    result = m._archive_intraday_observations(
        artifacts, tmp_path, run_state_path=run_state_path
    )

    assert result == "snap.json"
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted["intraday_archive"]["ok"] is True
    assert persisted["intraday_archive"]["run_snapshot"] == "snap.json"


def test_archive_intraday_observations_preserves_contract_failure_markers(
    tmp_path, monkeypatch
):
    # Codex review ("Preserve contract-failure markers when annotating run
    # state"): _write_and_validate_scheduled_contract amends the ON-DISK
    # run_state.json in place (status='blocked', artifact_contract_failure)
    # WITHOUT touching the in-memory artifacts dict this function receives.
    # Serializing that now-stale in-memory copy over the file would silently
    # erase the rejection every downstream consumer (select_same_day_run_state,
    # load_scheduled_run_states) relies on.
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: None)
    monkeypatch.setattr(
        m, "archive_observations_safely", lambda observations: {"ok": True}
    )

    run_state_path = tmp_path / "run_state.json"
    # The on-disk file already carries the contract-failure amendment that
    # _write_and_validate_scheduled_contract would have written; the
    # in-memory artifacts dict below deliberately does NOT have it, exactly
    # as it would look after that earlier write bypassed the caller's copy.
    run_state_path.write_text(
        json.dumps(
            {
                "run_id": "r1",
                "signal_rows": [],
                "status": "blocked",
                "artifact_contract_failure": {"reason": "scheduled-run artifact contract failed"},
                "run_classification": {"valid_scheduled_evidence": False},
            }
        )
    )
    artifacts = {
        "run_id": "r1",
        "signal_rows": [],
        "trading_day": "2026-08-03",
        "status": "ok",
        "run_classification": {"valid_scheduled_evidence": True},
    }

    m._archive_intraday_observations(artifacts, tmp_path, run_state_path=run_state_path)

    persisted = json.loads(run_state_path.read_bytes())
    assert persisted["status"] == "blocked"
    assert persisted["artifact_contract_failure"]["reason"] == "scheduled-run artifact contract failed"
    assert persisted["run_classification"]["valid_scheduled_evidence"] is False
    assert persisted["intraday_archive"]["ok"] is True


def test_archive_intraday_observations_resettles_usage_bytes(tmp_path, monkeypatch):
    # Codex review ("Recalculate usage bytes after expanding run state"):
    # write_run_artifacts() already settled state_file_bytes/
    # total_artifact_bytes against the serialized size BEFORE this
    # annotation grows the file with the intraday_archive object; leaving
    # them at their pre-annotation values understates
    # dca_cost_tracker.py's artifact-I/O cost proxy.
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: None)
    monkeypatch.setattr(
        m, "archive_observations_safely", lambda observations: {"ok": True}
    )

    (tmp_path / "signals.csv").write_text("ticker\nAAPL\n")
    run_state_path = tmp_path / "run_state.json"
    base_state = {
        "run_id": "r1",
        "signal_rows": [],
        "usage_proxy": {"csv_artifact_count": 1, "state_file_bytes": 1, "total_artifact_bytes": 1},
    }
    run_state_path.write_text(json.dumps(base_state))
    artifacts = dict(base_state, trading_day="2026-08-03")

    m._archive_intraday_observations(artifacts, tmp_path, run_state_path=run_state_path)

    persisted = json.loads(run_state_path.read_bytes())
    usage = persisted["usage_proxy"]
    state_bytes = len(json.dumps(persisted, indent=2, sort_keys=True).encode("utf-8"))
    csv_bytes = (tmp_path / "signals.csv").stat().st_size
    assert usage["state_file_bytes"] == state_bytes
    assert usage["total_artifact_bytes"] == state_bytes + csv_bytes


def test_archive_intraday_observations_without_run_state_path_does_not_persist(
    tmp_path, monkeypatch
):
    # Backward compatible: omitting run_state_path (the default) must not
    # attempt to write anywhere, matching every existing caller that has no
    # durable run_state to annotate.
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: None)
    monkeypatch.setattr(
        m,
        "archive_observations_safely",
        lambda observations: {"ok": False, "error": "archive unavailable"},
    )
    artifacts: dict[str, Any] = {"run_id": "r1", "signal_rows": [], "trading_day": "2026-08-03"}

    result = m._archive_intraday_observations(artifacts, tmp_path)

    assert result is None
    assert artifacts["intraday_archive"]["ok"] is False
    assert not (tmp_path / "run_state.json").exists()


def test_archive_intraday_observations_does_not_synthesize_missing_run_state(
    tmp_path, monkeypatch
):
    # Codex review ("Never synthesize run_state from in-memory artifacts when
    # the on-disk file is unusable"): if run_state.json is missing entirely --
    # e.g. because contract validation could not attach its failure marker
    # to it -- this function must not fabricate one from the pre-validation
    # in-memory `artifacts` dict. Doing so would create on-disk evidence that
    # LOOKS like a valid scheduled run even though it never actually passed
    # contract validation, defeating the fail-closed contract.
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: None)
    monkeypatch.setattr(
        m, "archive_observations_safely", lambda observations: {"ok": True}
    )

    run_state_path = tmp_path / "run_state.json"
    assert not run_state_path.exists()
    artifacts: dict[str, Any] = {"run_id": "r1", "signal_rows": [], "trading_day": "2026-08-03"}

    result = m._archive_intraday_observations(
        artifacts, tmp_path, run_state_path=run_state_path
    )

    assert result is None
    # The in-memory copy is still annotated for this call's own return value...
    assert artifacts["intraday_archive"]["ok"] is True
    # ...but nothing was ever written to disk in its place.
    assert not run_state_path.exists()


def test_archive_intraday_observations_does_not_clobber_unreadable_run_state(
    tmp_path, monkeypatch
):
    # Same fail-closed rule as above, but for a run_state.json that exists
    # on disk yet cannot be parsed (malformed JSON) -- also must be left
    # untouched rather than overwritten with the in-memory artifacts.
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: None)
    monkeypatch.setattr(
        m, "archive_observations_safely", lambda observations: {"ok": True}
    )

    run_state_path = tmp_path / "run_state.json"
    run_state_path.write_text("not valid json{{{")
    artifacts = {"run_id": "r1", "signal_rows": [], "trading_day": "2026-08-03"}

    result = m._archive_intraday_observations(
        artifacts, tmp_path, run_state_path=run_state_path
    )

    assert result is None
    assert run_state_path.read_text() == "not valid json{{{"


def test_archive_intraday_observations_skips_write_on_concurrent_run_state_change(
    tmp_path, monkeypatch
):
    # Codex review ("Guard the archive annotation's read-modify-write against
    # a concurrent retry"): a read-modify-write of run_state.json with no
    # coordination against a concurrent retry writing the same file can lose
    # that retry's newer, completed state. Simulate the race by mutating the
    # file (advancing both mtime and size) from inside the read hook this
    # test monkeypatches in, mimicking a concurrent writer finishing between
    # this function's own read and its later write.
    import near_close_dca as m

    monkeypatch.setattr(m, "write_run_snapshot", lambda out, observations: None)
    monkeypatch.setattr(
        m, "archive_observations_safely", lambda observations: {"ok": True}
    )

    run_state_path = tmp_path / "run_state.json"
    original = {"run_id": "r1", "signal_rows": [], "status": "ok"}
    run_state_path.write_text(json.dumps(original))

    real_read_bytes = pathlib.Path.read_bytes

    def racing_read_bytes(self):
        data = real_read_bytes(self)
        if self == run_state_path:
            # A "concurrent retry" finishes and rewrites the file right after
            # this function's own read, changing both mtime and size.
            self.write_text(json.dumps({"run_id": "r1", "signal_rows": [], "status": "retry-completed"}))
        return data

    monkeypatch.setattr(pathlib.Path, "read_bytes", racing_read_bytes)

    artifacts = {"run_id": "r1", "signal_rows": [], "trading_day": "2026-08-03"}
    result = m._archive_intraday_observations(
        artifacts, tmp_path, run_state_path=run_state_path
    )

    assert result is None
    # The concurrent retry's newer state must survive untouched -- not be
    # overwritten by this invocation's stale read-then-write.
    persisted = json.loads(run_state_path.read_bytes())
    assert persisted["status"] == "retry-completed"
    assert "intraday_archive" not in persisted


def test_select_reconciled_signal_rows_picks_the_buy_winning_provider(tmp_path):
    # Codex review ("Archive one reconciled near-close observation per
    # ticker"): at 15:30 signal_rows carries two rows per ticker (Finance
    # then Robinhood), and the scheduled natural key
    # (trading_day, stream, slot, ticker) has no source component, so
    # archiving both unfiltered would collapse to whichever sorts last --
    # Robinhood here -- even when Finance is the one that actually produced
    # the reconciled BUY.
    finance_signal = SourceSignal(
        ticker="AAPL", source=SourceName.FINANCE, action=Action.BUY, reason="ok",
        decision_price=98.0, quote_price=98.0,
    )
    robinhood_signal = SourceSignal(
        ticker="AAPL", source=SourceName.ROBINHOOD, action=Action.BUY, reason="ok",
        decision_price=97.0, quote_price=97.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="finance and Robinhood agree on BUY",
        finance_signal=finance_signal,
        robinhood_signal=robinhood_signal,
        target_price=98.0,
        target_price_source="finance:signal_quote",
    )
    signal_rows = [
        {"ticker": "AAPL", "source": "finance", "decision_bar_close": 98.0},
        {"ticker": "AAPL", "source": "robinhood", "decision_bar_close": 97.0},
    ]

    selected = _select_reconciled_signal_rows(signal_rows, [decision])

    assert len(selected) == 1
    row = selected[0]
    assert row["ticker"] == "AAPL"
    assert row["source"] == "finance"
    assert row["decision_bar_close"] == 98.0
    # Codex review ("Archive the near-close target quote"): the row must
    # also carry the RECONCILED execution target, not just the raw
    # decision_bar_close, so the archived price matches what placement
    # actually used.
    assert row["current_price"] == 98.0
    assert row["price_source"] == "finance:signal_quote"


def test_select_reconciled_signal_rows_overlays_the_target_quote_time():
    quote_time = datetime(2026, 6, 29, 15, 29, 50, tzinfo=EASTERN)
    finance_signal = SourceSignal(
        ticker="AAPL", source=SourceName.FINANCE, action=Action.BUY, reason="ok",
        decision_price=98.0, quote_price=98.5, quote_time=quote_time,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="Robinhood-only BUY because finance source is unavailable",
        finance_signal=finance_signal,
        robinhood_signal=None,
        target_price=98.5,
        target_price_source="finance:signal_quote",
        target_quote_time=quote_time,
    )
    signal_rows = [{"ticker": "AAPL", "source": "finance", "decision_bar_close": 98.0}]

    selected = _select_reconciled_signal_rows(signal_rows, [decision])

    assert selected[0]["current_price"] == 98.5
    assert selected[0]["price_as_of"] == quote_time


def test_select_reconciled_signal_rows_does_not_mutate_the_original_signal_row():
    # The same signal_rows list is also written to signals.csv by
    # write_run_artifacts -- overlaying archive-only fields must never leak
    # into that unrelated artifact.
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="ok",
        finance_signal=None,
        robinhood_signal=None,
        target_price=98.0,
        target_price_source="finance:signal_quote",
    )
    original_row = {"ticker": "AAPL", "source": "finance", "decision_bar_close": 98.0}
    signal_rows = [original_row]

    _select_reconciled_signal_rows(signal_rows, [decision])

    assert "current_price" not in original_row
    assert "price_source" not in original_row


def test_select_reconciled_signal_rows_prefers_finance_for_non_buy_decisions():
    # Neither provider "wins" a non-BUY decision (target_price_source stays
    # empty), so this system's Finance-primary policy is used as the
    # tie-break, falling back to Robinhood only when Finance has no signal.
    decision_both = ReconciledDecision(
        ticker="AAPL",
        action=Action.NO_ACTION,
        reason="mixed or flat conditions",
        finance_signal=SourceSignal(
            ticker="AAPL", source=SourceName.FINANCE, action=Action.NO_ACTION, reason="ok"
        ),
        robinhood_signal=SourceSignal(
            ticker="AAPL", source=SourceName.ROBINHOOD, action=Action.NO_ACTION, reason="ok"
        ),
    )
    decision_finance_only = ReconciledDecision(
        ticker="MSFT",
        action=Action.NO_ACTION,
        reason="mixed or flat conditions",
        finance_signal=None,
        robinhood_signal=SourceSignal(
            ticker="MSFT", source=SourceName.ROBINHOOD, action=Action.NO_ACTION, reason="ok"
        ),
    )
    signal_rows = [
        {"ticker": "AAPL", "source": "finance"},
        {"ticker": "AAPL", "source": "robinhood"},
        {"ticker": "MSFT", "source": "finance"},
        {"ticker": "MSFT", "source": "robinhood"},
    ]

    selected = _select_reconciled_signal_rows(signal_rows, [decision_both, decision_finance_only])

    by_ticker = {row["ticker"]: row["source"] for row in selected}
    assert by_ticker == {"AAPL": "finance", "MSFT": "robinhood"}


def test_provider_error_manual_review_classified_as_data_error():
    fs = _no_data_signal(SourceName.FINANCE, provider_error=True)
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.MANUAL_REVIEW,
        reason="provider error on finance; do not auto-buy",
        finance_signal=fs,
        robinhood_signal=None,
    )
    assert classify_missing_fill(decision, None) == MISSING_FILL_DATA_ERROR


def test_build_usage_proxy_defaults_and_overrides():
    decisions = [
        _decision("A", Action.BUY),
        _decision("B", Action.MANUAL_REVIEW),
    ]
    signal_rows = [{}, {}, {}, {}]
    buy_candidates = [{"ticker": "A"}]
    outcomes = {"A": {"status": "dry_run", "order_id": "X"}}

    usage = build_usage_proxy(
        decisions=decisions,
        signal_rows=signal_rows,
        buy_candidates=buy_candidates,
        candidate_outcomes=outcomes,
    )
    # calls default to tickers scanned; reviews default to candidate count.
    assert usage["tickers_scanned"] == 2
    assert usage["finance_calls_attempted"] == 2
    assert usage["order_reviews_attempted"] == 1
    assert usage["orders_placed_count"] == 1
    # a MANUAL_REVIEW decision flags manual recovery even with zero errors.
    assert usage["manual_recovery_required"] is True

    proxy = build_usage_proxy(
        decisions=[_decision("A", Action.BUY), _decision("B", Action.BUY)],
        signal_rows=[{}, {}, {}],
        buy_candidates=[{"a": 1}],
        candidate_outcomes={},
        order_reviews_attempted=5,
        finance_calls_attempted=4,
        robinhood_calls_attempted=2,
        errors_count=1,
        notifications_sent=3,
        cost_control_notes=["Note 1"],
    )
    assert proxy["finance_calls_attempted"] == 4
    assert proxy["robinhood_calls_attempted"] == 2
    assert proxy["order_reviews_attempted"] == 5
    assert proxy["errors_count"] == 1
    assert proxy["notifications_sent"] == 3
    assert proxy["cost_control_notes"] == ["Note 1"]


# --- Same-day prior-run offset / top-up guard --------------------------------


def _buy_decision(ticker, suggested_amount_usd, target_price):
    """A minimal BUY ReconciledDecision for exercising the top-up guard directly."""
    return ReconciledDecision(
        ticker=ticker,
        action=Action.BUY,
        reason="test buy candidate",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=suggested_amount_usd,
        target_price=target_price,
    )


def test_top_up_guard_no_prior_run_allocates_full_amount():
    decision = _buy_decision("AAPL", 6.0, target_price=98.0)
    outcome = evaluate_top_up_guard(decision, None, StrategyConfig())
    assert outcome.top_up_guard_result == TOP_UP_NO_PRIOR_RUN
    assert outcome.incremental_order_amount == 6.0
    assert outcome.skipped is False
    assert outcome.manual_review is False


def test_top_up_guard_discards_sub_threshold_pending_credit_with_no_prior_allocation():
    # Codex review ("Flush pending credits even without prior context"): a
    # ticker whose only earlier same-day activity was a sub-dollar decline
    # that never itself crossed the broker's $1 minimum has NO
    # prior_same_day_allocations entry at all -- that dict only tracks
    # actual orders, not pending credits. prior is therefore None here, same
    # as for a ticker with no same-day activity whatsoever. Before this fix,
    # a non-BUY final decision for that ticker fell through to the plain
    # "no prior" outcome (desired=0, since there's no BUY signal to size)
    # without ever considering existing_pending_credit_usd, permanently
    # stranding the credit since near_close_dca is always the final session.
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.NO_ACTION,
        reason="price recovered above previous close",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=0.0,
        suggested_amount_usd=0.0,
        target_price=100.0,
    )

    outcome = evaluate_top_up_guard(
        decision, None, StrategyConfig(), existing_pending_credit_usd=0.40
    )

    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True
    assert outcome.cumulative_credit_decline_usd is None


def test_top_up_guard_lower_price_buys_incremental_difference():
    # Morning bought $3 at 99.0; afternoon wants $6 and the price is below 99.0.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=3.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = _buy_decision("AAPL", 6.0, target_price=98.0)
    outcome = evaluate_top_up_guard(decision, prior, StrategyConfig())
    assert outcome.top_up_guard_result == TOP_UP_INCREMENTAL
    assert outcome.incremental_order_amount == 3.0
    assert outcome.skipped is False
    assert outcome.prior_same_day_amount == 3.0
    assert outcome.prior_same_day_reference_price == 99.0
    assert outcome.afternoon_decision_price == 98.0


def test_top_up_guard_price_not_below_prior_buys_zero():
    # Canonical rule item 12: not-strictly-lower and zero-delta both carry the
    # single unified same_day_topup_not_lower_or_no_delta reason.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=3.0,
        reference_price=98.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    # Equal price -> not strictly below -> $0.
    equal = evaluate_top_up_guard(
        _buy_decision("AAPL", 6.0, target_price=98.0), prior, StrategyConfig()
    )
    assert equal.top_up_guard_result == TOP_UP_NOT_LOWER_OR_NO_DELTA
    assert equal.incremental_order_amount == 0.0
    assert equal.skipped is True
    # Higher price -> also $0.
    higher = evaluate_top_up_guard(
        _buy_decision("AAPL", 6.0, target_price=98.5), prior, StrategyConfig()
    )
    assert higher.top_up_guard_result == TOP_UP_NOT_LOWER_OR_NO_DELTA
    assert higher.incremental_order_amount == 0.0
    assert higher.skipped is True


def test_top_up_guard_prior_exceeds_afternoon_desired_buys_zero():
    # Morning bought $6; afternoon only wants $3, even into a lower price -> $0.
    # Canonical rule item 12: zero-delta also carries the unified reason.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.5,
    )
    outcome = evaluate_top_up_guard(
        _buy_decision("AAPL", 3.0, target_price=98.0), prior, StrategyConfig()
    )
    assert outcome.top_up_guard_result == TOP_UP_NOT_LOWER_OR_NO_DELTA
    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True


def test_top_up_guard_flushes_a_qualifying_credit_despite_a_negative_ladder_delta():
    # Codex review discussion on commit 55e175d ("Flush qualifying credits
    # despite a negative ladder delta"): the SAME prior-exceeds-desired
    # early return above must not silently strand a qualifying (>= $0.50)
    # broker-minimum remainder just because THIS ticker's own ladder target
    # has nothing further to contribute -- the remainder is unrelated money
    # owed from an earlier blocked purchase, not more of this ticker's own
    # ladder allocation, and the final session must still flush it.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.5,
    )
    outcome = evaluate_top_up_guard(
        _buy_decision("AAPL", 3.0, target_price=98.0),
        prior,
        StrategyConfig(),
        existing_pending_credit_usd=0.75,
    )
    assert outcome.top_up_guard_result == TOP_UP_INCREMENTAL
    assert outcome.incremental_order_amount == 1.0
    assert outcome.skipped is False
    assert outcome.discarded_pending_credit_usd is None


def test_top_up_guard_discards_a_sub_threshold_credit_despite_a_negative_ladder_delta():
    # The discard counterpart: a remainder below $0.50 in the same
    # prior-exceeds-desired state must be durably discarded, not left
    # pending with nothing left to ever resolve it.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.5,
    )
    outcome = evaluate_top_up_guard(
        _buy_decision("AAPL", 3.0, target_price=98.0),
        prior,
        StrategyConfig(),
        existing_pending_credit_usd=0.30,
    )
    assert outcome.top_up_guard_result == TOP_UP_NOT_LOWER_OR_NO_DELTA
    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True
    assert outcome.discarded_pending_credit_usd == 0.30


def test_top_up_guard_final_session_discards_sub_threshold_decline():
    # Codex review ("wire CumulativeCreditLedger into a real execution path"):
    # the day's ladder target is already fully met (ladder_incremental == 0),
    # but the price is $0.30 lower than the prior run's reference -- below
    # cumulative_top_up_min_drop_usd (0.50), so neither the ladder nor the
    # price-step floor alone would order anything. near_close_dca is always
    # the scheduled 3:30 PM final session, so dca_cumulative's contract
    # requires flushing that positive residual decline up to a $1 order
    # instead of silently dropping it.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    outcome = evaluate_top_up_guard(
        _buy_decision("AAPL", 6.0, target_price=98.70), prior, StrategyConfig()
    )
    assert outcome.top_up_guard_result == TOP_UP_NOT_LOWER_OR_NO_DELTA
    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True
    # Codex review ("Defer ledger updates until the submitted amount is
    # known"): evaluate_top_up_guard is a PURE computation with no ledger
    # I/O -- it only reports which decline drove the credit-derived
    # incremental (cumulative_credit_decline_usd) so the caller can settle
    # the durable ledger later, once the resulting order is confirmed
    # submitted, not here before any cap/spread/placement gate has run.
    assert outcome.cumulative_credit_decline_usd is None


def test_top_up_guard_ordinary_ladder_increment_has_no_cumulative_credit_marker():
    # A $3 ladder-derived top-up doesn't touch the cumulative credit
    # mechanism at all, so cumulative_credit_decline_usd must stay None --
    # only the genuinely credit-derived sub-dollar path sets it.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=3.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    outcome = evaluate_top_up_guard(
        _buy_decision("AAPL", 6.0, target_price=98.0), prior, StrategyConfig()
    )
    assert outcome.top_up_guard_result == TOP_UP_INCREMENTAL
    assert outcome.incremental_order_amount == 3.0
    assert outcome.cumulative_credit_decline_usd is None


def test_cumulative_credit_settles_ledger_only_after_confirmed_submission(tmp_path):
    # Codex review ("Defer ledger updates until the submitted amount is
    # known"): the authoritative call site (_process_buy_candidate) must
    # settle the durable CumulativeCreditLedger only once the credit-derived
    # order is actually confirmed submitted, using the real submitted
    # notional -- not during evaluate_top_up_guard's earlier, pre-placement
    # computation, which runs before the cap/spread/buying-power/broker
    # gates that could still block or shrink the order.
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.40,
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.40})
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert top_up_outcome is not None
    assert top_up_outcome.cumulative_credit_decline_usd == 1.0
    assert candidate_outcome["status"] == "submitted"
    assert candidate_outcome["submitted_amount_usd"] == 1.0
    # Codex review discussion on commit bb3d494 ("Require a carried
    # remainder before marking aggregation"): this is a FRESH $0.60 decline
    # with no prior broker-minimum remainder (the ledger is empty going in)
    # -- an ordinary final-session top-up, not an aggregation of anything a
    # broker-minimum block earlier carried forward. The aggregate audit
    # fields must not be set for it.
    assert candidate_outcome.get("aggregate_minimum_topup_applied") is not True
    events = ledger.events(trading_day.isoformat())
    assert len(events) == 1
    assert events[0]["ticker"] == "AAPL"
    assert events[0]["decision"] == "settled"
    assert events[0]["submitted_amount_usd"] == 1.0


def test_cumulative_credit_discards_a_combined_total_still_below_fifty_cents(tmp_path):
    # Codex review discussion on commit 0af4768 ("Clear combined credits
    # that remain below fifty cents"): _select_final_session_credit_flush_
    # tickers defers a ticker whose own decision this run has a structural
    # chance of combining with its existing remainder -- but that combined
    # total can still land under the final-session discard threshold (e.g.
    # a $0.10 existing remainder plus a genuine but tiny $0.05 further
    # decline). evaluate_top_up_guard's own final-session apply_cumulative_
    # credit call already correctly decides to discard that combined total,
    # but (being a pure computation with no ledger I/O) never actually
    # clears the ledger's minimum-order-provenance pool on its own --
    # _process_buy_candidate must durably discard it using
    # discarded_pending_credit_usd, or the balance is left stranded pending
    # forever with nothing left this final session to ever revisit it.
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)
    ledger.record("AAPL", trading_day.isoformat(), 0.10, stream="morning")

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.95,  # a genuine but tiny further decline of $0.05
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.95})

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert top_up_outcome is not None
    assert top_up_outcome.cumulative_credit_decline_usd is None
    assert top_up_outcome.discarded_pending_credit_usd == 0.10
    assert candidate_outcome["status"] == "skipped"
    # The ledger must actually be cleared -- not left stranded -- since
    # nothing later this final session will ever revisit it.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {}
    events = ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "discarded"


def test_ordinary_ladder_order_folds_in_a_qualifying_existing_remainder(tmp_path):
    # Codex review discussion on commit 58a536a ("Resolve pending credit on
    # existing buy candidates"): a ticker whose own ladder increment already
    # qualifies for an ordinary >= $1 order on its own (the `else` branch,
    # ladder_incremental >= 1.0) previously never looked at
    # existing_pending_credit_usd at all -- a minimum-order remainder
    # sitting alongside a genuine ladder increment was placed as an ordinary
    # order while the remainder was left completely untouched: neither
    # combined into the order nor flushed/discarded, stranded for the rest
    # of the day since nothing else this final session will ever revisit
    # it. A remainder that itself already qualifies (>= $0.50, per the same
    # final-session threshold the aggregate rule uses everywhere else) must
    # be folded into the order that's happening anyway.
    #
    # Seeded via the same rebase-override mechanism used elsewhere in this
    # file's ledger tests: a single record() call at/above $0.50 rounds up
    # to an order immediately rather than sitting pending, so a genuinely
    # pending >= $0.50 balance can only arise from a rebased retry that gets
    # overridden back to "stays fully pending" (see
    # test_record_rebase_preserves_minimum_order_provenance_after_threshold_crossing
    # in test_dca_cumulative.py for the underlying mechanism).
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="s0")
    stale_baseline = ledger.pending_usd("AAPL", trading_day.isoformat())
    ledger.record("AAPL", trading_day.isoformat(), 0.10, stream="s-concurrent")
    ledger.record(
        "AAPL",
        trading_day.isoformat(),
        0.10,
        idempotency_key="slot-b",
        pending_baseline_usd=stale_baseline,
        stream="s-b",
    )
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.50}

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.0,  # a clear further decline below the $99 reference
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    # $5.00 ladder increment (desired 6.00, reusing the preserved $3 base at
    # buy_multiplier 2.0 -- prior 1.00) plus the qualifying $0.50 remainder
    # folded in.
    assert top_up_outcome is not None
    assert top_up_outcome.incremental_order_amount == 5.50
    assert candidate_outcome["status"] == "submitted"
    assert candidate_outcome["submitted_amount_usd"] == 5.50
    assert candidate_outcome["aggregate_minimum_topup_applied"] is True
    assert candidate_outcome["aggregate_minimum_topup_remainder_usd"] == 0.50
    # The ledger must actually be cleared, not left stranded.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {}
    assert ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0


def test_ordinary_ladder_order_discards_a_sub_threshold_existing_remainder(tmp_path):
    # Codex review discussion on commit e385a54 ("Discard sub-threshold
    # credit before folding ladder orders"): the manifest's own $0.50
    # discard threshold ("less than $0.50, forget it for that trading day")
    # applies to the remainder ITSELF first, unconditionally -- not just for
    # a standalone remainder-only order. A $0.30 remainder must be
    # discarded, not folded into an unrelated ladder order that would
    # happen regardless of the remainder's existence.
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.0,  # a clear further decline below the $99 reference
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    # The plain $5.00 ladder increment, with NO remainder folded in.
    assert top_up_outcome is not None
    assert top_up_outcome.incremental_order_amount == 5.0
    assert top_up_outcome.discarded_pending_credit_usd == 0.30
    assert candidate_outcome["status"] == "submitted"
    assert candidate_outcome["submitted_amount_usd"] == 5.0
    assert candidate_outcome.get("aggregate_minimum_topup_applied") is not True
    # The ledger must still be cleared -- the remainder was discarded, not
    # left stranded.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {}


def test_process_buy_candidate_ignores_pending_credit_outside_the_afternoon_stream(
    tmp_path,
):
    # Codex review discussion on commit f46f29c ("Restrict per-candidate
    # credit resolution to the afternoon"): guard_active is shared by every
    # scheduled stream with prior-allocation context, not just the 3:30 PM
    # final pass -- run_dca.run_slot() routes 1030ET (stream="morning") and
    # 1230ET (stream="midday") through this exact same _process_buy_candidate
    # with guard_active True. Without a stream check, an ordinary BUY
    # candidate on one of those earlier streams would read this ticker's
    # pending remainder and apply evaluate_top_up_guard's final-session
    # (apply_cumulative_credit(..., final_session=True)) fold/discard/round
    # semantics hours before the actual final session -- discarding or
    # folding a remainder that is supposed to accumulate untouched until
    # 3:30 PM. A midday BUY must leave the ledger and the audit fields alone.
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=1.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.0,  # a clear further decline below the $99 reference
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    now = datetime(2026, 6, 29, 12, 30, tzinfo=EASTERN)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1)
    )

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            credit_ledger=ledger,
            stream="midday",
        ),
        running_deployed_7d=0.0,
    )

    # The plain $5.00 ladder increment -- no fold, no discard, no rounding.
    assert top_up_outcome is not None
    assert top_up_outcome.incremental_order_amount == 5.0
    assert top_up_outcome.discarded_pending_credit_usd is None
    assert candidate_outcome["status"] == "submitted"
    assert candidate_outcome["submitted_amount_usd"] == 5.0
    assert candidate_outcome.get("aggregate_minimum_topup_applied") is not True
    # The remainder must still be sitting pending for the real 3:30 PM pass.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.30}


def test_cumulative_credit_discards_small_pending_balance_with_no_new_decline(tmp_path):
    # Codex review ("Accumulate credits instead of resetting pending state"):
    # a $0.20 credit was already pending for this ticker from earlier in the
    # day. This afternoon's own decision price has recovered back to the
    # prior run's exact reference price (no NEW decline at all), so the
    # combined total stays below the final-session discard threshold. This
    # is always the final same-day session, so the $0.20 must be durably
    # discarded here rather than left "pending" forever with nothing left
    # to ever revisit it (Codex review discussion on commit 55e175d:
    # "Recheck survivor balances that shrink below the threshold" --
    # generalized here to every flush-only zero-order path, not just the
    # concurrency-shrinkage case that comment described).
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)
    ledger.record("AAPL", trading_day.isoformat(), 0.20, stream="morning")

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=99.0,  # recovered back to the prior reference -- no new decline
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 99.0})

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert top_up_outcome is not None
    assert top_up_outcome.top_up_guard_result == TOP_UP_NOT_LOWER_OR_NO_DELTA
    assert top_up_outcome.cumulative_credit_decline_usd is None
    assert top_up_outcome.discarded_pending_credit_usd == 0.20
    assert candidate_outcome["status"] == "skipped"
    events = ledger.events(trading_day.isoformat())
    assert events[-1]["decision"] == "discarded"
    assert ledger.pending_usd("AAPL", trading_day.isoformat()) == 0.0


def test_cumulative_credit_ledger_untouched_when_order_is_blocked(tmp_path):
    # The same credit-derived candidate, but blocked by the per-order
    # ceiling before it ever reaches the broker -- the ledger must stay
    # completely untouched (no premature flush of a credit for an order
    # that was never actually placed), leaving the credit available for a
    # later attempt.
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.40,
    )
    config = StrategyConfig(dry_run=False, max_per_order_usd=0.50)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.40})
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert top_up_outcome is not None
    assert top_up_outcome.cumulative_credit_decline_usd == 1.0
    assert candidate_outcome["status"] == "blocked"
    events = ledger.events(trading_day.isoformat())
    assert events == []


def test_final_session_credit_flush_defers_discard_when_a_decline_can_combine_it():
    # Codex review discussion_r3793071614 ("Aggregate current credit before
    # discarding the remainder"): a ticker with a sub-$0.50 broker-minimum
    # remainder AND a real chance this run's own decline could combine with
    # it must NOT be discarded before _process_buy_candidate/
    # evaluate_top_up_guard gets that chance (e.g. pending $0.40 + a fresh
    # $0.20 decline = $0.60, which should place a $1 order, not be silently
    # dropped). That chance exists only when the ticker has a known prior
    # same-day allocation with a usable base/reference price AND this run's
    # own decision price is strictly below that reference price -- the exact
    # gate evaluate_top_up_guard itself checks before its combination branch.
    from near_close_dca import _select_final_session_credit_flush_tickers

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=3.0,
        reference_price=100.0,
        run_id="morning",
        source="scheduled",
        selected_base_amount_usd=3.0,
    )
    combinable_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0,
        suggested_amount_usd=3.0,
        selected_base_amount_usd=3.0,
        target_price=99.50,  # strictly below the prior's $100.00 reference
    )
    recovered_decision = ReconciledDecision(
        ticker="MSFT",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        target_price=101.0,  # at/above its own prior reference -- no combination
    )
    discarded = []
    survivors = _select_final_session_credit_flush_tickers(
        {"AAPL": 0.40, "MSFT": 0.30, "DELISTED": 0.30},
        {"AAPL": combinable_decision, "MSFT": recovered_decision},
        {
            "AAPL": prior,
            "MSFT": PriorSameDayAllocation(
                ticker="MSFT",
                amount_usd=2.0,
                reference_price=100.0,
                run_id="morning",
                source="scheduled",
                selected_base_amount_usd=2.0,
            ),
        },
        discard=discarded.append,
    )

    # AAPL's own decline this run could still combine with its remainder --
    # it must survive to be routed through the normal candidate flow, which
    # alone can correctly evaluate that combination.
    assert survivors == ["AAPL"]
    # MSFT has a decision and a prior allocation, but its price recovered
    # (not below the reference), so evaluate_top_up_guard could only take a
    # flush-only path that never actually clears the ledger -- discarding it
    # here is the only place this remainder is ever resolved. DELISTED has no
    # decision this run at all, same reasoning.
    assert set(discarded) == {"MSFT", "DELISTED"}


def test_final_session_credit_flush_still_survives_at_or_above_the_minimum():
    # A remainder already at or above $0.50 must never be discarded here
    # either, decision or not -- unchanged baseline behavior.
    from near_close_dca import _select_final_session_credit_flush_tickers

    discarded = []
    survivors = _select_final_session_credit_flush_tickers(
        {"AAPL": 0.75},
        {},
        {},
        discard=discarded.append,
    )

    assert survivors == ["AAPL"]
    assert discarded == []


class _MismatchRefusingLedger:
    """CumulativeCreditLedger double with the real refusal contract.

    ``discard_minimum_order_pending`` refuses on ANY mismatch between the
    caller's observation and the amount under its own lock, and refuses again
    when the current balance qualifies for an order -- the two distinct
    reasons the real ledger returns False.
    """

    QUALIFYING_THRESHOLD = 0.50

    def __init__(self, balances):
        self.balances = dict(balances)
        self.discard_calls = []

    def minimum_order_pending_tickers(self, trading_day):
        return dict(self.balances)

    def discard_minimum_order_pending(self, ticker, trading_day, observed_amount_usd=None):
        self.discard_calls.append((ticker, observed_amount_usd))
        current = float(self.balances.get(ticker, 0.0))
        if current <= 0:
            return True
        if observed_amount_usd is not None and round(float(observed_amount_usd), 2) != round(current, 2):
            return False
        if current >= self.QUALIFYING_THRESHOLD:
            return False
        self.balances.pop(ticker, None)
        return True


def test_discard_retry_clears_a_concurrently_changed_sub_threshold_balance():
    # Codex review, P2 ("Retry sub-threshold changes in the flush selector"):
    # the ledger refuses on any stale observation, not only a
    # threshold-crossing one. The retry re-reads and tries once more with the
    # refreshed amount, so the still-sub-threshold balance is written off.
    from near_close_dca import _discard_minimum_order_pending_with_retry

    ledger = _MismatchRefusingLedger({"ZZZ": 0.30})

    assert _discard_minimum_order_pending_with_retry(ledger, "ZZZ", "2026-06-29", 0.20) is True
    assert ledger.discard_calls == [("ZZZ", 0.20), ("ZZZ", 0.30)]
    assert ledger.balances == {}


def test_discard_retry_still_refuses_a_concurrently_qualified_balance():
    # The other refusal reason must survive the retry untouched: a balance
    # that grew past the threshold is real money and belongs in an order, so
    # False still means "now qualifies" and the caller routes it into an
    # evaluation. Only ONE discard attempt is made -- the refreshed amount
    # matches nothing stale, so there is nothing to retry.
    from near_close_dca import _discard_minimum_order_pending_with_retry

    ledger = _MismatchRefusingLedger({"ZZZ": 0.60})

    assert _discard_minimum_order_pending_with_retry(ledger, "ZZZ", "2026-06-29", 0.60) is False
    assert ledger.discard_calls == [("ZZZ", 0.60)]
    assert ledger.balances == {"ZZZ": 0.60}


def test_final_session_flush_discards_a_changed_balance_with_no_decision():
    # The reviewer's exact scenario: an excluded/un-enrolled ticker has no
    # decision this run, so the selector's refusal branch cannot route it into
    # a candidate evaluation. Before the retry, a concurrent update that moved
    # the balance without carrying it over the threshold refused the discard
    # and the credit was left pending after the day's LAST pass. With the
    # retry wired into the callback, the balance is written off instead.
    from near_close_dca import (
        _discard_minimum_order_pending_with_retry,
        _select_final_session_credit_flush_tickers,
    )

    ledger = _MismatchRefusingLedger({"ZZZ": 0.30})
    observed = {"ZZZ": 0.20}  # this run's pre-lock scan, now stale

    survivors = _select_final_session_credit_flush_tickers(
        observed,
        {},  # no decision for ZZZ -- nothing to route it through
        {},
        discard=lambda ticker: _discard_minimum_order_pending_with_retry(
            ledger, ticker, "2026-06-29", observed.get(ticker, 0.0)
        ),
    )

    assert survivors == []
    assert ledger.balances == {}


def test_final_session_credit_flush_discard_failure_is_logged_not_raised():
    from near_close_dca import _select_final_session_credit_flush_tickers

    def _raising_discard(ticker):
        raise OSError("ledger is locked")

    survivors = _select_final_session_credit_flush_tickers(
        {"DELISTED": 0.20},
        {},
        {},
        discard=_raising_discard,
    )

    # A discard failure must not propagate and must not resurrect the
    # ticker as a survivor either -- it was already decided un-flushable.
    assert survivors == []


def test_final_session_credit_flush_routes_a_recoverable_duplicate_to_survivors():
    # Codex review discussion on commit 3d8df75 ("Recover accepted orders
    # before discarding their credit"): an earlier attempt this same stream
    # may have already placed a real broker order combining this remainder
    # with a fresh decline (e.g. $0.30 pending + a $0.30 decline -> a real $1
    # order), then crashed before settle()/artifact persistence ran -- the
    # ledger still shows the pre-order $0.30 as pending. If THIS retry's own
    # decision no longer looks combinable (price recovered to NO_ACTION),
    # discarding would erase the only ledger evidence before
    # _process_buy_candidate's own duplicate lookup ever runs. A duplicate
    # order must route the ticker to survivors instead, never to discard.
    from near_close_dca import Action, ReconciledDecision, _select_final_session_credit_flush_tickers

    recovered_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        target_price=101.0,
    )
    discarded = []
    survivors = _select_final_session_credit_flush_tickers(
        {"AAPL": 0.30},
        {"AAPL": recovered_decision},
        {},
        discard=discarded.append,
        duplicate_order_exists=lambda ticker: ticker == "AAPL",
    )

    assert survivors == ["AAPL"]
    assert discarded == []


def test_final_session_credit_flush_leaves_an_unroutable_duplicate_pending():
    # Same scenario as above, but this run has no decision at all for the
    # ticker (e.g. it dropped out of the universe) -- there is no way to
    # route it through _process_buy_candidate's recovery, so it must be left
    # alone rather than discarded (destroying possibly-real ledger evidence)
    # or wrongly added as an unroutable survivor.
    from near_close_dca import _select_final_session_credit_flush_tickers

    discarded = []
    survivors = _select_final_session_credit_flush_tickers(
        {"DELISTED": 0.30},
        {},
        {},
        discard=discarded.append,
        duplicate_order_exists=lambda ticker: True,
    )

    assert survivors == []
    assert discarded == []


def test_final_session_credit_flush_discards_a_structurally_combinable_but_unsafe_decision():
    # Codex review discussion on commit f2df714 ("Discard unsafe sub-
    # threshold remainders"): a ticker can be structurally combinable (known
    # prior, usable base/reference, this run's price strictly below the
    # reference) while its own decision this run is MANUAL_REVIEW or
    # NO_DATA -- a provider error or Finance/Robinhood disagreement, not a
    # genuine recovered/flat price. _run_once_impl's flush-injection loop
    # rejects any such decision via _is_flush_safe_decision before it ever
    # reaches evaluate_top_up_guard, so deferring the discard here for a
    # combination that can never happen would strand the remainder pending
    # instead of forgetting it at the final session.
    from near_close_dca import _select_final_session_credit_flush_tickers

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=3.0,
        reference_price=100.0,
        run_id="morning",
        source="scheduled",
        selected_base_amount_usd=3.0,
    )
    unsafe_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.MANUAL_REVIEW,
        reason="finance/robinhood disagreement",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0,
        suggested_amount_usd=3.0,
        selected_base_amount_usd=3.0,
        target_price=99.50,  # strictly below the prior's $100.00 reference
    )
    discarded = []
    survivors = _select_final_session_credit_flush_tickers(
        {"AAPL": 0.40},
        {"AAPL": unsafe_decision},
        {"AAPL": prior},
        discard=discarded.append,
    )

    assert survivors == []
    assert discarded == ["AAPL"]


def test_flush_safe_decision_rejects_manual_review_and_no_data():
    # Codex review discussion on commit bb3d494 ("Keep failed-confidence
    # decisions out of flush candidates"): MANUAL_REVIEW/NO_DATA mean this
    # run's OWN signal for the ticker hit a provider error, a Finance/
    # Robinhood disagreement, or missing data -- not a recovered/flat price
    # safe to place a real order from.
    from near_close_dca import _is_flush_safe_decision

    for unsafe_action in (Action.MANUAL_REVIEW, Action.NO_DATA):
        decision = ReconciledDecision(
            ticker="AAPL",
            action=unsafe_action,
            reason="test",
            finance_signal=None,
            robinhood_signal=None,
        )
        assert _is_flush_safe_decision(decision) is False

    for safe_action in (Action.BUY, Action.NO_ACTION, Action.SKIP):
        decision = ReconciledDecision(
            ticker="AAPL",
            action=safe_action,
            reason="test",
            finance_signal=None,
            robinhood_signal=None,
        )
        assert _is_flush_safe_decision(decision) is True


def test_final_session_credit_flush_is_restricted_to_the_afternoon_stream(
    tmp_path, monkeypatch
):
    # Codex review discussion on commit 8720cec ("Restrict the final credit
    # flush to the afternoon stream"): guard_active alone does not mean
    # "this is the 15:30 run" -- run_dca.run_slot() routes 1030ET
    # (stream="morning") and 1230ET (stream="midday") through this same
    # _run_once_impl with guard_active true (they use the ordinary top-up
    # guard too). Without a stream check, the FINAL-SESSION-only discard/
    # flush policy fired hours early, discarding a 9:35 remainder at 10:30
    # instead of letting it accumulate through 3:30 as designed.
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = CumulativeCreditLedger(root=tmp_path / "cron_tracking" / "cumulative_dca_credits")
    trading_day = date(2026, 6, 29)
    ledger.record("AAPL", trading_day.isoformat(), 0.20, stream="market_open")
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.20}

    now = datetime(2026, 6, 29, 12, 30, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 100.0})
    # Flat/recovered price -- AAPL's own decision this run is NOT a BUY, so
    # it would only ever reach the credit-ledger flush/discard path, never
    # the ordinary buy_candidates list.
    fin = _FakeData(SourceName.FINANCE, 100.0, 100.0, quote=(100.0, now))
    rh = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 100.0, 100.0, quote=(100.0, now), bid_ask=(99.99, 100.01)
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "midday")

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir,
            scheduled=True,
            stream="midday",
            prior_same_day_allocations={},
        ),
    )

    # The 12:30 midday run must NOT have touched the ledger -- the $0.20
    # remainder is only for the 3:30 final pass to resolve.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.20}


def test_duplicate_recovery_settles_the_aggregate_credit_ledger(tmp_path):
    # Codex review discussion on commit 8720cec ("Reconcile aggregate
    # credits on duplicate recovery"): if the broker accepts an aggregate
    # order and the process crashes before settle() runs, a retry that
    # finds the same-stream order already placed takes the
    # duplicate_preserved early-return path -- which, before this fix,
    # skipped both settle() and the aggregate audit fields entirely, even
    # though this retry's OWN evaluate_top_up_guard call (computed just
    # above the duplicate check, using the still-pending ledger balance)
    # shows the aggregate rule applied to that exact order.
    #
    # Attempt 1 places the order and settles normally; the ledger balance is
    # then restored to simulate a crash landing between broker acceptance
    # and settle() on a hypothetical earlier process. Attempt 2 (the retry)
    # must find the duplicate and use IT to durably clear the ledger.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.40,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    robinhood_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.40, 100.0, quote=(98.40, now), bid_ask=(98.3, 98.5)
    )
    ledger = CumulativeCreditLedger(root=tmp_path)
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    # Attempt 1: places the real aggregate order and settles normally.
    state1 = StrategyState()
    outcome1, top_up_outcome1, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state1,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )
    assert outcome1["status"] == "submitted"
    assert outcome1["aggregate_minimum_topup_applied"] is True
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {}

    # Simulate the crash: restore the ledger to the still-pending state a
    # process that placed the order but never reached settle() would leave.
    # That means BOTH halves -- the credit is pending again AND no settlement
    # was ever recorded for the order. Restoring the balance alone is not
    # enough: attempt 1 really did run settle(), so it left the order's id
    # behind in ``settled_order_ids``. A process that crashed BEFORE settle()
    # would have written no such marker, and leaving one here would make the
    # retry's own settle() take the idempotent no-op path and never clear the
    # balance -- testing the idempotency guard instead of the duplicate-recovery
    # settlement this regression is about (Codex review: "Make cumulative-credit
    # settlement idempotent by order ID"). Drop the marker so the ledger state
    # genuinely matches the modelled crash point.
    ledger_state_path = tmp_path / trading_day.isoformat() / "ledger.json"
    ledger_state = json.loads(ledger_state_path.read_text())
    ledger_state.pop("settled_order_ids", None)
    ledger_state_path.write_text(json.dumps(ledger_state, indent=2, sort_keys=True) + "\n")
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    # Attempt 2 (the retry): finds the same-stream order and must recover
    # it, durably settling the ledger this time.
    state2 = StrategyState()
    outcome2, top_up_outcome2, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state2,
            broker=broker,
            robinhood_data=robinhood_data,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )
    assert len(broker._orders) == 1, "retry must not place a second broker order"
    assert outcome2["duplicate_preserved"] is True
    assert outcome2["aggregate_minimum_topup_applied"] is True
    assert outcome2["aggregate_minimum_topup_remainder_usd"] == 0.30
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {}


def test_kill_switch_blocks_a_credit_only_flush_instead_of_a_zero_dollar_order(tmp_path):
    # Codex review discussion on commit db6ef02 ("Keep recovered-price
    # flushes above the kill-switch floor"): a credit-only flush candidate
    # (no prior allocation, non-BUY decision whose selected_base_amount_usd
    # is 0 -- exactly the ReconciledDecision shape the "no prior" flush
    # branch reuses) has no positive base for the kill-switch cap to fall
    # back to. Without an explicit check, the clamp brings order_amount to
    # exactly $0 and nothing downstream rejects that before place_market_buy,
    # so the broker would receive a genuine $0 order while settle() clears
    # nothing (pending_after = pending_before - 0), silently stranding the
    # qualifying credit as though the final pass had resolved it. The
    # candidate must be blocked instead, leaving the ledger balance
    # untouched.
    config = StrategyConfig(monthly_cash_allocation_usd=10.0, dry_run=False)
    recovered_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        target_price=101.0,
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 101.0})
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)
    # A single record() call at/above $0.50 rounds up to an order immediately
    # regardless of final_session (apply_cumulative_credit's own threshold
    # logic), so a genuinely PENDING >= $0.50 balance can only arise from a
    # rebased retry that gets overridden back to "stays fully pending" --
    # same seeding technique used elsewhere in this file (see
    # test_record_rebase_preserves_minimum_order_provenance_after_threshold_
    # crossing in test_dca_cumulative.py for the underlying mechanism).
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="s0")
    stale_baseline = ledger.pending_usd("AAPL", trading_day.isoformat())
    ledger.record("AAPL", trading_day.isoformat(), 0.10, stream="s-concurrent")
    ledger.record(
        "AAPL",
        trading_day.isoformat(),
        0.20,
        idempotency_key="slot-b",
        pending_baseline_usd=stale_baseline,
        stream="s-b",
    )
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.60}

    candidate_outcome, _, _, _, _ = _process_buy_candidate(
        recovered_decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={},  # no prior for AAPL at all
            scheduled=True,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=100.0,  # engages the kill switch
    )

    assert candidate_outcome["status"] == "blocked"
    assert candidate_outcome.get("order_id") is None
    # The qualifying credit must still be sitting pending -- not silently
    # settled by a phantom $0 order, and not discarded either.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.60}


def test_process_buy_candidate_recovers_a_duplicate_with_no_prior_allocation(tmp_path):
    # Codex review discussion on commit f0b4177 ("Recover duplicates for
    # prior-less flush survivors"): the same-stream duplicate lookup was
    # gated on ticker_guarded (requires a PriorSameDayAllocation entry for
    # this exact ticker), so a credit-flush survivor whose prior-allocation
    # artifact is unavailable on retry (e.g. an aggregate order was accepted
    # before a crash, and the artifact that would have recorded it never got
    # written) skipped the duplicate lookup entirely -- an unsafe decision
    # was blocked without ever getting a chance to recover the accepted
    # order. The lookup must also run whenever this ticker carries a pending
    # ledger credit, independent of whether prior context loaded.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    unsafe_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.MANUAL_REVIEW,
        reason="finance/robinhood disagreement",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=0.0,
        selected_base_amount_usd=3.0,
        target_price=98.40,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    ref_id = f"{_graded_dca_ref_id_prefix('afternoon')}-{trading_day.isoformat()}-AAPL"
    broker._orders.append(("AAPL", ref_id, 1.0, "ORDER-EARLIER-AAPL"))
    ledger = CumulativeCreditLedger(root=tmp_path)
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    outcome, _, _, _, _ = _process_buy_candidate(
        unsafe_decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={},  # no entry for AAPL at all
            scheduled=True,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["duplicate_preserved"] is True
    assert outcome["order_id"] == "ORDER-EARLIER-AAPL"
    assert len(broker._orders) == 1, "must not place a new order for an unreliable decision"


def test_process_buy_candidate_recovers_a_duplicate_despite_an_unsafe_decision(tmp_path):
    # Codex review discussion on commit be6b992 ("Recover duplicates before
    # filtering unsafe flush signals"): a ticker can reach this function via
    # the final-session credit flush's duplicate-recovery survivor path
    # specifically BECAUSE its own decision this run is unreliable
    # (MANUAL_REVIEW/NO_DATA) -- the injection loop no longer filters those
    # out itself (that would drop the ticker before the duplicate check below
    # ever runs). A confirmed same-stream duplicate must still be recovered
    # regardless of the decision's own confidence.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    unsafe_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.MANUAL_REVIEW,
        reason="finance/robinhood disagreement",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=0.0,
        selected_base_amount_usd=3.0,
        target_price=98.40,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    ref_id = f"{_graded_dca_ref_id_prefix('afternoon')}-{trading_day.isoformat()}-AAPL"
    broker._orders.append(("AAPL", ref_id, 1.0, "ORDER-EARLIER-AAPL"))
    ledger = CumulativeCreditLedger(root=tmp_path)
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    outcome, _, _, _, _ = _process_buy_candidate(
        unsafe_decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["duplicate_preserved"] is True
    assert outcome["order_id"] == "ORDER-EARLIER-AAPL"
    assert len(broker._orders) == 1, "must not place a new order for an unreliable decision"


def test_process_buy_candidate_blocks_an_unsafe_decision_with_no_recoverable_duplicate(tmp_path):
    # Same setup as above, but no earlier broker order exists for this
    # ticker: an unreliable decision must never fall through to placing a
    # genuinely NEW order just because it was injected via the flush path.
    config = StrategyConfig(monthly_cash_allocation_usd=100.0, dry_run=False)
    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    unsafe_decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.MANUAL_REVIEW,
        reason="finance/robinhood disagreement",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=0.0,
        selected_base_amount_usd=3.0,
        target_price=98.40,
    )
    broker = _RefIdTrackingBroker()
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    trading_day = date(2026, 6, 29)
    ledger = CumulativeCreditLedger(root=tmp_path)
    ledger.record("AAPL", trading_day.isoformat(), 0.30, stream="morning")

    outcome, _, _, _, _ = _process_buy_candidate(
        unsafe_decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=now,
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=True,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert broker._orders == []
    # The remainder is left untouched -- not durably resolved either way --
    # since this function never got to make a real decision about it.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.30}


def test_dry_run_never_discards_the_live_credit_ledger(tmp_path):
    # Codex review discussion on commit e385a54 ("Keep dry runs from
    # discarding the live credit ledger"): CumulativeCreditLedger() always
    # resolves the SAME default root regardless of config.dry_run, and a
    # --demo/local dry-run invocation sharing that root with real scheduled
    # runs (the common case) must never mutate it. This exercises
    # _process_buy_candidate's own discard guard directly -- a scenario that
    # would otherwise durably discard a $0.10 remainder must leave the
    # ledger completely untouched under dry_run=True.
    ledger = CumulativeCreditLedger(root=tmp_path)
    trading_day = date(2026, 6, 29)
    ledger.record("AAPL", trading_day.isoformat(), 0.10, stream="morning")

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.95,  # a genuine but tiny further decline of $0.05
    )
    config = StrategyConfig(dry_run=True)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.95})

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=ledger,
        ),
        running_deployed_7d=0.0,
    )

    assert top_up_outcome is not None
    assert top_up_outcome.discarded_pending_credit_usd == 0.10
    assert candidate_outcome["status"] == "skipped"
    # The ledger balance and its event log must be exactly as seeded --
    # no discard event, no cleared balance.
    assert ledger.minimum_order_pending_tickers(trading_day.isoformat()) == {"AAPL": 0.10}
    events = ledger.events(trading_day.isoformat())
    assert len(events) == 1
    assert events[0]["decision"] == "pending"


def test_final_session_credit_flush_routes_a_concurrently_enlarged_balance():
    # Codex review discussion on commit bb3d494 ("Route concurrently
    # enlarged credits into the final pass"): when discard() reports False
    # (a concurrent update raised the ledger's balance to or above the
    # discard threshold since this scan), the ticker must not just survive
    # discard -- it must be routed to a candidate evaluation this run so the
    # now-qualifying balance can actually be ordered, not left stranded
    # after the day's last pass just because it wasn't deferred up front.
    from near_close_dca import _select_final_session_credit_flush_tickers

    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
    )
    survivors = _select_final_session_credit_flush_tickers(
        {"AAPL": 0.20},
        {"AAPL": decision},
        {},
        discard=lambda ticker: False,
    )

    assert survivors == ["AAPL"]


def test_final_session_credit_flush_checks_the_ladder_delta_before_deferring():
    # Codex review discussion on commit bb3d494 ("Check the remaining
    # ladder delta before deferring discards"): a lower afternoon price
    # alone does not guarantee evaluate_top_up_guard reaches its
    # combination branch -- it also requires desired - prior.amount_usd
    # >= 0. A prior/manual allocation already at or above today's ladder
    # target hits evaluate_top_up_guard's TOP_UP_NOT_LOWER_OR_NO_DELTA
    # early return instead, which never touches existing_pending_credit_usd.
    # Deferring in that state strands the remainder with nothing left to
    # ever discard or combine it.
    from near_close_dca import _select_final_session_credit_flush_tickers

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=5.0,  # already well above today's ladder target below
        reference_price=100.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0,
        suggested_amount_usd=1.0,
        selected_base_amount_usd=1.0,
        target_price=99.0,  # strictly below the $100 reference
    )
    discarded = []
    survivors = _select_final_session_credit_flush_tickers(
        {"AAPL": 0.30},
        {"AAPL": decision},
        {"AAPL": prior},
        discard=discarded.append,
    )

    # No combination is structurally possible (desired 1.00 - prior 5.00 is
    # negative), so the remainder must be discarded immediately, not
    # deferred to a candidate evaluation that can never resolve it.
    assert survivors == []
    assert discarded == ["AAPL"]


class _ConcurrentlyEnlargedLedger:
    """CumulativeCreditLedger double simulating a concurrent update landing
    between the pending-balance read and the discard call: the FIRST
    minimum_order_pending_tickers() read returns a small, sub-threshold
    balance; discard_minimum_order_pending() refuses (returns False); and
    every SUBSEQUENT minimum_order_pending_tickers() read reflects the
    larger, now-qualifying balance a concurrent stream contributed.
    """

    def __init__(self, initial, enlarged):
        self._initial = initial
        self._enlarged = enlarged
        self._reads = 0
        self.settled = []
        self.discard_calls = []

    def minimum_order_pending_tickers(self, trading_day):
        self._reads += 1
        return dict(self._initial if self._reads == 1 else self._enlarged)

    # ``observed_amount_usd`` mirrors the real ledger's signature: the caller
    # hands back the balance it read BEFORE the lock so the ledger can refuse a
    # discard whose premise no longer holds. The double must accept it -- a
    # double stuck on the old two-argument signature would raise TypeError,
    # which _process_buy_candidate's broad handler reads as "discarded", and the
    # race this regression exists to exercise would silently stop being tested.
    def discard_minimum_order_pending(self, ticker, trading_day, observed_amount_usd=None):
        self.discard_calls.append((ticker, observed_amount_usd))
        return False

    def settle(
        self,
        ticker,
        trading_day,
        submitted_amount_usd,
        *,
        stream="unknown",
        recorded_at=None,
        order_id=None,
    ):
        self.settled.append((ticker, submitted_amount_usd))


class _ConcurrentlySubThresholdLedger:
    """CumulativeCreditLedger double: a concurrent update moves the balance
    WITHOUT carrying it over the order threshold.

    The FIRST minimum_order_pending_tickers() read returns the caller's
    pre-lock observation; every subsequent read returns the new balance.
    discard_minimum_order_pending() mirrors the real ledger's contract: it
    refuses on ANY mismatch between the caller's observation and the amount
    under its own lock, not only on a threshold-crossing one.
    """

    def __init__(self, initial, updated):
        self._initial = dict(initial)
        self.balances = dict(updated)
        self._reads = 0
        self.discard_calls = []

    def minimum_order_pending_tickers(self, trading_day):
        self._reads += 1
        return dict(self._initial if self._reads == 1 else self.balances)

    def discard_minimum_order_pending(self, ticker, trading_day, observed_amount_usd=None):
        self.discard_calls.append((ticker, observed_amount_usd))
        current = float(self.balances.get(ticker, 0.0))
        if observed_amount_usd is not None and round(float(observed_amount_usd), 2) != round(current, 2):
            return False
        self.balances.pop(ticker, None)
        return True

    def settle(
        self,
        ticker,
        trading_day,
        submitted_amount_usd,
        *,
        stream="unknown",
        recorded_at=None,
        order_id=None,
    ):
        raise AssertionError("a sub-threshold remainder must not place an order")


def test_process_buy_candidate_retries_a_still_sub_threshold_discard():
    # Codex review, P2 ("Retry discards after sub-threshold balance changes"):
    # discard_minimum_order_pending() refuses on ANY mismatch between the
    # caller's pre-lock observation and the amount under its own lock -- not
    # only when the balance crossed the order threshold. A concurrent update
    # from $0.10 to $0.20 therefore refuses, the caller re-reads and
    # re-evaluates, and the re-evaluation comes back a discard AGAIN. The
    # "a refusal means it now qualifies" reasoning does not cover that, so
    # without a retry the balance is left pending after the day's LAST pass
    # with nothing left to resolve it. The retry carries the refreshed
    # observation, which matches the locked amount and clears.
    ledger = _ConcurrentlySubThresholdLedger(
        initial={"AAPL": 0.10}, updated={"AAPL": 0.20}
    )
    trading_day = date(2026, 6, 29)

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        # A tiny further decline: $0.20 + $0.05 is still short of $0.50, so the
        # re-evaluation is another discard, not an order.
        target_price=98.95,
    )
    config = StrategyConfig(dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.95})

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=cast(Any, ledger),
        ),
        running_deployed_7d=0.0,
    )

    # Still a discard verdict, not an order.
    assert top_up_outcome is not None
    assert top_up_outcome.discarded_pending_credit_usd is not None
    assert candidate_outcome["status"] != "submitted"
    # Two attempts: the stale observation, then the refreshed one.
    assert ledger.discard_calls == [("AAPL", 0.10), ("AAPL", 0.20)]
    # The final pass leaves nothing pending -- the remainder is written off,
    # not stranded past the day's last chance to resolve it.
    assert ledger.balances == {}


def test_process_buy_candidate_re_evaluates_a_concurrently_enlarged_discard(tmp_path):
    # Codex review discussion on commit e385a54 ("Route a concurrently
    # qualified discard into evaluation"): the first read here sees $0.10,
    # this call's own tiny decline combines to a sub-$0.50 discard decision,
    # but discard_minimum_order_pending() reports the balance grew to $0.60
    # in the meantime (a concurrent stream's contribution). Re-reading and
    # re-evaluating with the fresh $0.60 must let the now-qualifying order
    # actually place, rather than leaving the candidate at the stale
    # "skipped" verdict computed against the smaller balance.
    ledger = _ConcurrentlyEnlargedLedger(
        initial={"AAPL": 0.10}, enlarged={"AAPL": 0.60}
    )
    trading_day = date(2026, 6, 29)

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.95,  # a genuine but tiny further decline of $0.05
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.95})

    candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=state,
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=trading_day,
            guard_active=True,
            prior_same_day_allocations={"AAPL": prior},
            scheduled=False,
            run_result=None,
            credit_ledger=cast(Any, ledger),
        ),
        running_deployed_7d=0.0,
    )

    # $0.60 (enlarged) + $0.05 decline crosses the $0.50 threshold and
    # rounds up to the broker's $1.00 minimum -- a real order, not a skip.
    assert top_up_outcome is not None
    assert top_up_outcome.discarded_pending_credit_usd is None
    assert top_up_outcome.cumulative_credit_decline_usd is not None
    assert candidate_outcome["status"] == "submitted"
    assert candidate_outcome["submitted_amount_usd"] == 1.0
    assert ledger.settled == [("AAPL", 1.0)]
    # The discard attempt carried the caller's own pre-lock reading ($0.10),
    # which is exactly what lets the real ledger detect that the locked balance
    # has moved on and refuse the discard.
    assert ledger.discard_calls == [("AAPL", 0.10)]


class _RaisingSettleLedger:
    """CumulativeCreditLedger double whose settle() always raises."""

    def settle(
        self,
        ticker,
        trading_day,
        submitted_amount_usd,
        *,
        stream="unknown",
        recorded_at=None,
        order_id=None,
    ):
        raise OSError("ledger is locked")

    def minimum_order_pending_tickers(self, trading_day):
        return {}


def test_credit_ledger_settlement_failure_does_not_lose_the_placed_order():
    # Codex review, P1 ("Publish accepted orders before settling the credit
    # ledger"): if the broker accepts a credit-derived order but settle()
    # raises because the ledger is corrupt, locked, or unwritable, the
    # already-placed order must still be published to the in-flight-outcome
    # tracker (so a failure artifact built from it never omits a real
    # accepted order) and _process_buy_candidate must still return
    # normally instead of propagating the ledger failure.
    import near_close_dca as _n

    prior = PriorSameDayAllocation(
        ticker="AAPL",
        amount_usd=6.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=3.0,
        target_price=98.40,
    )
    config = StrategyConfig(dry_run=False)
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.40})
    trading_day = date(2026, 6, 29)

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        candidate_outcome, top_up_outcome, _, _, _ = _process_buy_candidate(
            decision,
            ctx=ProcessCandidateContext(
                config=config,
                state=state,
                broker=broker,
                robinhood_data=None,
                now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
                trading_day=trading_day,
                guard_active=True,
                prior_same_day_allocations={"AAPL": prior},
                scheduled=False,
                run_result=None,
                credit_ledger=cast(Any, _RaisingSettleLedger()),
            ),
            running_deployed_7d=0.0,
        )
        published = dict(_n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"])
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    assert top_up_outcome is not None
    assert top_up_outcome.cumulative_credit_decline_usd == 1.0
    assert candidate_outcome["status"] == "submitted"
    assert candidate_outcome["submitted_amount_usd"] == 1.0
    assert published.get("AAPL", {}).get("status") == "submitted"


def test_top_up_guard_missing_prior_price_is_conservative_manual_review():
    # Prior amount known but no comparable reference price -> conservative $0 + manual review.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=3.0,
        reference_price=None,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    outcome = evaluate_top_up_guard(
        _buy_decision("AAPL", 6.0, target_price=98.0), prior, StrategyConfig()
    )
    assert outcome.top_up_guard_result == TOP_UP_MISSING_PRIOR_PRICE
    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True
    assert outcome.manual_review is True


def test_top_up_guard_missing_afternoon_price_is_conservative():
    # Known prior price but no afternoon price (no target, no signals) -> conservative $0.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=3.0,
        reference_price=99.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=3.0,
    )
    outcome = evaluate_top_up_guard(
        _buy_decision("AAPL", 6.0, target_price=0.0), prior, StrategyConfig()
    )
    assert outcome.top_up_guard_result == TOP_UP_MISSING_PRIOR_PRICE
    assert outcome.incremental_order_amount == 0.0
    assert outcome.manual_review is True


def test_top_up_guard_reuses_first_allocation_base_for_later_top_up():
    # Codex review ("Preserve the first allocation's base for same-day top-ups"):
    # prior close $100, prior low $95. A morning run bought using the $1 base (its
    # decision price was below close but not below the low) for $2 total (1.0 base
    # * 2x). The afternoon run's OWN decision price ($94) is now below the low too,
    # so its own freshly-selected base would be $2 -- but the guard must keep using
    # the first allocation's $1 base with the afternoon run's own 4x multiplier, not
    # silently upgrade the whole day's target to the $2 scale.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=2.0,
        reference_price=98.0,
        run_id="manual_am",
        source="manual",
        selected_base_amount_usd=1.0,
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test buy candidate",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=4.0,
        # Recomputed (incorrectly, from this run's own $2 base) as 2.0 * 4.0.
        suggested_amount_usd=8.0,
        selected_base_amount_usd=2.0,
        target_price=94.0,
    )
    outcome = evaluate_top_up_guard(decision, prior, StrategyConfig())
    assert (
        outcome.afternoon_desired_amount == 4.0
    )  # 1.0 (preserved base) * 4x, not 2.0 * 4x
    assert outcome.top_up_guard_result == TOP_UP_INCREMENTAL
    assert outcome.incremental_order_amount == 2.0  # 4.0 - 2.0, not 8.0 - 2.0 == 6.0
    # Codex review ("Persist the preserved base on top-up candidates"): the
    # guard must expose the PRESERVED $1 base it actually used, not this run's
    # own freshly recomputed $2 base, so a candidate-row artifact stays
    # consistent with the amount actually calculated.
    assert outcome.effective_base_amount_usd == 1.0


def test_top_up_guard_fails_closed_when_prior_base_unknown():
    # An older prior-allocation artifact recorded no selected_base_amount_usd at
    # all -- silently recomputing the full-day target from this run's own base
    # would risk over/understating the incremental order (Codex review: "Fail
    # closed when the first allocation base is unknown"), so the guard must
    # conservatively buy $0 and flag manual review instead, exactly like a
    # missing reference price.
    prior = PriorSameDayAllocation(
        "AAPL",
        amount_usd=2.0,
        reference_price=98.0,
        run_id="manual_am",
        source="manual",
    )
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test buy candidate",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=2.0,
        suggested_amount_usd=8.0,
        selected_base_amount_usd=2.0,
        target_price=94.0,
    )
    outcome = evaluate_top_up_guard(decision, prior, StrategyConfig())
    assert outcome.top_up_guard_result == TOP_UP_MISSING_PRIOR_BASE
    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True
    assert outcome.manual_review is True


# --- load_prior_same_day_allocations -----------------------------------------


def _write_prior_run(root, subdir, run_state, orders_rows=None):
    run_dir = os.path.join(root, subdir)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(run_state, handle)
    if orders_rows is not None:
        with open(os.path.join(run_dir, "orders.csv"), "w", encoding="utf-8") as handle:
            handle.write("ticker,status,avg_fill_price\n")
            for row in orders_rows:
                handle.write(
                    f"{row['ticker']},{row['status']},{row['avg_fill_price']}\n"
                )
    return run_dir


def _stamp_mtime_on_trading_day(path, trading_day):
    """Set path's mtime to noon Eastern on trading_day (see _mtime_matches_trading_day)."""
    stamp = datetime.combine(trading_day, time(12, 0), tzinfo=EASTERN).timestamp()
    os.utime(path, (stamp, stamp))


def _manual_run_state(
    candidates, generated_at="2026-06-29T10:00:00-04:00", run_origin="manual"
):
    return {
        "trading_day": "2026-06-29",
        "generated_at": generated_at,
        "run_classification": {"run_origin": run_origin, "run_id": f"{run_origin}_am"},
        "buy_candidates": candidates,
    }


def test_load_prior_same_day_includes_scheduled_morning_live_allocation(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "20260629_0945ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:45:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "morning_dca_live",
                "stream": "morning",
                "run_id": "morning_live_20260629_0945ET",
            },
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0},
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "98.25"}],
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].reference_price == 98.25
    assert allocations["AAPL"].source == "scheduled"


def test_load_prior_same_day_counts_a_failed_attempt_with_zero_amount(tmp_path):
    """Codex review: "Count every prior buy attempt toward the daily limit".
    A candidate whose placement raised (status="failed", near_close_dca's own
    _process_buy_candidate exception path) obtained no order_id and moved no
    capital, but the attempt was genuinely made against the broker and still
    consumes one of the shared max_orders_per_day slots. Previously "failed"
    wasn't even in _PRIOR_ALLOCATION_STATUSES, so this row was invisible to
    load_prior_same_day_allocations entirely."""
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "20260629_1030ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T10:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "morning_dca_live",
                "stream": "morning",
                "run_id": "morning_live_20260629_1030ET",
            },
            "buy_candidates": [
                {
                    "ticker": "AAPL",
                    "status": "failed",
                    "planned_amount_usd": 3.0,
                    "skip_reason": "placement failed: broker timeout",
                },
            ],
        },
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 0.0
    assert allocations["AAPL"].attempt_count == 1
    assert allocations["AAPL"].reference_price is None


def test_load_prior_same_day_counts_order_merged_with_settlement_adjustment(tmp_path):
    """A row-wide adjustment marker must not hide a physical order's attempt."""
    import near_close_dca as ncd

    real = {
        "ticker": "RVII",
        "status": "submitted",
        "order_id": "ORD-A",
        "planned_amount_usd": 23.36,
        "submitted_amount_usd": 23.36,
        "whole_share_quantity": 1,
    }
    adjustment = {
        "ticker": "RVII",
        "status": "submitted",
        "order_id": "ORD-A",
        "planned_amount_usd": 1.14,
        "submitted_amount_usd": 1.14,
        "whole_share_quantity": 0,
        "whole_share_settlement_adjustment": True,
    }
    merged = ncd._merge_whole_share_submission_outcomes(real, adjustment)

    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "20260629_1030ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T10:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "morning_dca_live",
                "stream": "morning",
                "run_id": "morning_live_20260629_1030ET",
            },
            "buy_candidates": [dict(merged, ticker="RVII", planned_amount_usd=23.36)],
        },
    )

    allocation = load_prior_same_day_allocations(root, date(2026, 6, 29))["RVII"]

    assert not merged.get("whole_share_settlement_adjustment")
    assert merged["merged_submission_count"] == 1
    assert allocation.attempt_count == 1


def test_load_prior_same_day_excludes_a_cross_day_whole_share_recovery_from_attempt_count(tmp_path):
    """Codex review ("Keep recovered attempts on their original claim
    dates"): a whole-share candidate row whose ENTIRE
    whole_share_claim_breakdown points at a trading day strictly BEFORE
    this artifact's own trading_day is a cross-day RECOVERY of an order
    that was actually attempted (and already counted toward
    max_orders_per_day) on that earlier day, not a new attempt made today.
    Counting it again here would seed a later same-day retry's own attempt
    count as if the historical order were a fresh same-day attempt,
    potentially blocking a legitimate new order today.
    """
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        root,
        "20260629_1230ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T12:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "midday_dca_live",
                "stream": "midday",
                "run_id": "midday_live_20260629_1230ET",
            },
            "buy_candidates": [
                {
                    "ticker": "BYDDY",
                    "status": "submitted",
                    "planned_amount_usd": 10.0,
                    "whole_share_claim_breakdown": [
                        {"trading_day": "2026-06-27", "amount_usd": 10.0},
                    ],
                },
            ],
        },
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["BYDDY"].attempt_count == 0


def test_load_prior_same_day_excludes_a_cross_day_zero_fill_recovery_from_attempt_count(tmp_path):
    """Codex review ("Keep recovered zero-fill attempts on the claim
    day"): a confirmed zero-fill recovery (see
    _book_reconciled_whole_share_order's own zero_fill_attempt branch)
    used to record the claim's original trading day only inside the
    free-text skip_reason, with no whole_share_claim_breakdown at all --
    the SAME cross-day exclusion this file already covers for a real
    settled recovery (see the sibling test just above) evaluated False for
    a zero-fill row, since the exclusion check reads ONLY the
    machine-readable breakdown, never skip_reason text. A cross-day
    zero-fill attempt was therefore double counted: once via
    ctx.state.record_buy(..., claim_trading_day_date, 0.0) at recovery
    time, and again here when a LATER run reloaded the artifact.
    """
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        root,
        "20260629_1230ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T12:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "midday_dca_live",
                "stream": "midday",
                "run_id": "midday_live_20260629_1230ET",
            },
            "buy_candidates": [
                {
                    "ticker": "BYDDY",
                    "status": "failed",
                    "planned_amount_usd": 0.0,
                    "skip_reason": (
                        "whole-share: confirmed zero-fill terminal order reconciled cross-day "
                        "(trading_day=2026-06-27)"
                    ),
                    "whole_share_zero_fill_attempt": True,
                    "whole_share_claim_breakdown": [
                        {"trading_day": "2026-06-27", "amount_usd": 0.0},
                    ],
                },
            ],
        },
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["BYDDY"].attempt_count == 0


def test_load_prior_same_day_counts_a_whole_share_row_whose_breakdown_includes_today(tmp_path):
    """Companion to the cross-day-exclusion test above: a whole-share row
    whose breakdown includes TODAY's own trading day (e.g. a recovered
    older claim aggregated with today's own fresh contribution) still
    counts as a real attempt made today.
    """
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        root,
        "20260629_1230ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T12:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "midday_dca_live",
                "stream": "midday",
                "run_id": "midday_live_20260629_1230ET",
            },
            "buy_candidates": [
                {
                    "ticker": "BYDDY",
                    "status": "submitted",
                    "planned_amount_usd": 20.0,
                    "whole_share_claim_breakdown": [
                        {"trading_day": "2026-06-27", "amount_usd": 10.0},
                        {"trading_day": "2026-06-29", "amount_usd": 10.0},
                    ],
                },
            ],
        },
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["BYDDY"].attempt_count == 1


def test_load_prior_same_day_omits_a_cross_day_whole_share_recovery_from_todays_amount(tmp_path):
    """Codex review ("Bucket recovered notional by its claim date"): a
    whole-share candidate row whose ENTIRE whole_share_claim_breakdown
    points at an earlier trading day is a cross-day RECOVERY -- its
    dollars already contributed to THAT day's own allocation on the
    earlier run that recorded it. Counting the row's full
    planned_amount_usd again under TODAY's own bucket would double-count
    it, and since _compute_earlier_week_cross_stream_notional() reuses
    this loader to seed the shared weekly cap, that misattributes a prior
    week's already-spent recovery as current-week spend.
    """
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        root,
        "20260629_1230ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T12:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "midday_dca_live",
                "stream": "midday",
                "run_id": "midday_live_20260629_1230ET",
            },
            "buy_candidates": [
                {
                    "ticker": "BYDDY",
                    "status": "submitted",
                    "planned_amount_usd": 10.0,
                    "whole_share_claim_breakdown": [
                        {"trading_day": "2026-06-27", "amount_usd": 10.0},
                    ],
                },
            ],
        },
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert "BYDDY" not in allocations or allocations["BYDDY"].amount_usd == 0.0


def test_load_prior_same_day_splits_a_mixed_whole_share_breakdown_by_claim_date(tmp_path):
    """Companion to the cross-day-omission test above: a whole-share row
    whose breakdown mixes an older recovered claim with today's own fresh
    contribution attributes only TODAY's own portion to today's bucket,
    not the row's full combined amount.
    """
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        root,
        "20260629_1230ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T12:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "midday_dca_live",
                "stream": "midday",
                "run_id": "midday_live_20260629_1230ET",
            },
            "buy_candidates": [
                {
                    "ticker": "BYDDY",
                    "status": "submitted",
                    "planned_amount_usd": 20.0,
                    "whole_share_claim_breakdown": [
                        {"trading_day": "2026-06-27", "amount_usd": 10.0},
                        {"trading_day": "2026-06-29", "amount_usd": 10.0},
                    ],
                },
            ],
        },
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["BYDDY"].amount_usd == pytest.approx(10.0)


def test_load_prior_same_day_preserves_a_fresh_contribution_merged_with_an_older_recovery(tmp_path):
    """Codex review ("Preserve fresh notional in mixed recovery rows"): a
    row can merge an OLDER recovered claim (which DOES carry its own
    whole_share_claim_breakdown entry) with a genuinely FRESH order placed
    TODAY (which does NOT -- only a reconciled recovery ever gets a
    breakdown entry; see _merge_whole_share_submission_outcomes). Summing
    only the breakdown entries matching today would then attribute $0 to
    that fresh order entirely, dropping real submitted capital from
    same-day/weekly-cap reconstruction.
    """
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        root,
        "20260629_1230ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T12:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "midday_dca_live",
                "stream": "midday",
                "run_id": "midday_live_20260629_1230ET",
            },
            "buy_candidates": [
                {
                    "ticker": "BYDDY",
                    "status": "submitted",
                    # $30 total: $10 from the older recovered claim (carries
                    # its own breakdown entry) + $20 from a fresh order
                    # placed today (no breakdown entry of its own).
                    "planned_amount_usd": 30.0,
                    "whole_share_claim_breakdown": [
                        {"trading_day": "2026-06-27", "amount_usd": 10.0},
                    ],
                },
            ],
        },
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # Only the fresh $20 (the row's $30 total minus the $10 already
    # attributed to the older claim's own day) is attributed to today.
    assert allocations["BYDDY"].amount_usd == pytest.approx(20.0)


def test_load_prior_same_day_counts_every_attempt_for_one_ticker(tmp_path):
    """A ticker bought once and re-attempted-and-failed twice in the same
    earlier run is 3 attempts, not 1 -- even though only one of them
    contributed a dollar amount."""
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "20260629_1030ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T10:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "morning_dca_live",
                "stream": "morning",
                "run_id": "morning_live_20260629_1030ET",
            },
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0},
                {"ticker": "AAPL", "status": "failed", "planned_amount_usd": 2.0},
                {"ticker": "AAPL", "status": "failed", "planned_amount_usd": 1.0},
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "98.25"}],
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].attempt_count == 3


def test_load_prior_same_day_csv_only_reconstruction_does_not_crash_when_merged_with_a_candidate_run(
    tmp_path,
):
    """Codex review: "Initialize attempt counts for CSV-reconstructed
    orders". The no-embedded-candidates branch (a recovery run reconstructed
    solely from its recorded orders CSV) used to create its aggregated entry
    without an "attempt_count" key. Since aggregated.setdefault() returns
    whatever entry already exists for a ticker, a LATER-scanned run's
    embedded-candidate branch reusing that same entry via
    entry["attempt_count"] += 1 would raise KeyError and abort the whole
    reconstruction -- not just undercount, but crash -- when the earlier
    CSV-only run is scanned first (by generated_at) and both runs cover the
    same ticker."""
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")

    # Run 1 (scanned first: earlier generated_at): CSV-only reconstruction,
    # no embedded buy_candidates.
    csv_only_dir = os.path.join(root, "20260629_1230ET_recovery_1230ET")
    os.makedirs(csv_only_dir, exist_ok=True)
    csv_only_state = _recovery_run_state([], generated_at="2026-06-29T12:30:00-04:00")
    csv_only_state["artifacts"] = {"orders_csv_path": "filled_orders.csv"}
    with open(
        os.path.join(csv_only_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(csv_only_state, handle)
    with open(
        os.path.join(csv_only_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,3.50,filled,109.989900,0.031821\n"
        )

    # Run 2 (scanned second: later generated_at): embedded candidate for the
    # SAME ticker, reusing run 1's aggregated entry.
    candidate_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(candidate_dir, exist_ok=True)
    candidate_state = _recovery_run_state(
        [{"ticker": "SPCX", "status": "filled", "planned_amount_usd": 2.0}],
        generated_at="2026-06-29T13:05:00-04:00",
    )
    with open(
        os.path.join(candidate_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(candidate_state, handle)
    with open(
        os.path.join(candidate_dir, "orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write("ticker,status,avg_fill_price\nSPCX,filled,110.00\n")

    # Must not raise KeyError.
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["SPCX"].amount_usd == pytest.approx(5.50)
    assert allocations["SPCX"].attempt_count == 2


def test_load_prior_same_day_survives_malformed_buy_candidates(tmp_path):
    # Codex review: "Reject malformed candidate collections in recovery states".
    # A scalar buy_candidates raised TypeError out of the whole scan, and a
    # truthy non-list was silently read as "candidates handled" -- skipping the
    # orders-CSV fallback, so the run's real fills went unseen and its ticker
    # could be bought again at 3:30. Both now route into the CSV fallback.
    for malformed in (1, 1.5, "candidates", {"AAPL": 3.0}, True):
        root = os.path.join(
            tmp_path,
            f"cron_tracking_{type(malformed).__name__}_{malformed}",
            "morning_dca",
        )
        _write_prior_run(
            root,
            "20260629_0945ET",
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T09:45:00-04:00",
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "morning_dca_live",
                    "stream": "morning",
                    "run_id": "morning_live_20260629_0945ET",
                },
                "buy_candidates": malformed,
                "artifacts": {"orders_csv_path": "orders.csv"},
            },
        )
        with open(
            os.path.join(root, "20260629_0945ET", "orders.csv"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                "ticker,status,amount_usd,avg_fill_price\nAAPL,filled,3.00,98.25\n"
            )

        # No TypeError, and the sibling CSV's fill is still counted.
        allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

        assert "AAPL" in allocations, malformed
        assert allocations["AAPL"].reference_price == 98.25, malformed


def test_load_prior_same_day_reads_morning_manual_allocation(tmp_path):
    # No orders.csv at all (legacy/older artifact layout). A submitted row still
    # contributes prior-allocation amount, but without a confirmed fill its logged
    # target_price must not become a known reference price.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "submitted",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                },
            ]
        ),
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert set(allocations) == {"AAPL"}
    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].reference_price is None
    assert allocations["AAPL"].source == "manual"


def test_load_prior_same_day_allocation_preserves_selected_base_amount(tmp_path):
    # Codex review ("Preserve the first allocation's base for same-day top-ups"):
    # a later same-day top-up guard must be able to recover the first allocation's
    # selected base from the run_state.json this loader reads.
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "20260629_0945ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:45:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "morning_dca_live",
                "stream": "morning",
                "run_id": "morning_live_20260629_0945ET",
            },
            "buy_candidates": [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 2.0,
                    "selected_base_amount_usd": 1.0,
                },
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "98.25"}],
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].selected_base_amount_usd == 1.0


def test_load_prior_same_day_first_run_locks_base_even_when_unknown(tmp_path):
    # Codex review ("Do not infer a first-allocation base from a later run"): if the
    # true FIRST same-day run predates selected_base_amount_usd (or never resolved
    # one), the aggregate base must stay unknown forever -- a later same-day run's
    # own base (which could itself be a top-up requalified at a different price)
    # must never backfill it.
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "20260629_0945ET",  # sorts (chronologically) before the 1030 run below
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:45:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "stream": "morning",
                "run_id": "morning_0945",
            },
            "buy_candidates": [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 1.0,
                },  # no base field at all
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )
    _write_prior_run(
        root,
        "20260629_1030ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T10:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "stream": "morning",
                "run_id": "morning_1030",
            },
            "buy_candidates": [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 2.0,
                    "selected_base_amount_usd": 2.0,
                },
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "97.0"}],
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].selected_base_amount_usd is None


def test_load_prior_same_day_processes_runs_by_timestamp_not_path_order(tmp_path):
    # Codex review ("Sort first-allocation records by timestamp"): within a
    # single root, "manual_recovery/1100" sorts lexically BEFORE
    # "manual_runs/0900" even though 09:00 happened first, so the aggregator
    # must not lock the base from whichever subtree happens to sort first.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_recovery/1100",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T11:00:00-04:00",
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "same_day_top_up_context_only": True,
                "valid_scheduled_evidence": False,
                "run_id": "recovery_1100",
            },
            "buy_candidates": [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 2.0,
                    "selected_base_amount_usd": 2.0,
                },
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "97.0"}],
    )
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 1.0,
                    "selected_base_amount_usd": 1.0,
                },
            ],
            generated_at="2026-06-29T09:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 3.0
    assert (
        allocations["AAPL"].selected_base_amount_usd == 1.0
    )  # 09:00's base, not 11:00's


def test_load_prior_same_day_allocation_unconfirmed_base_not_trusted(tmp_path):
    # An unconfirmed "submitted" row (no orders.csv fill) has neither a known
    # reference price nor a trusted base -- same conservative gate as reference_price.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "submitted",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                    "selected_base_amount_usd": 1.0,
                },
            ]
        ),
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].selected_base_amount_usd is None


def test_load_prior_same_day_submitted_with_logged_price_and_no_fill_has_unknown_reference(
    tmp_path,
):
    # A still-submitted order with no confirmed fill row may later be rejected or
    # canceled; do not use its logged target/decision price as a known reference.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "submitted",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                    "decision_bar_close": 99.2,
                },
            ]
        ),
        orders_rows=[{"ticker": "AAPL", "status": "submitted", "avg_fill_price": ""}],
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].reference_price is None


def test_load_prior_same_day_no_logged_price_has_no_reference(tmp_path):
    # No logged target_price/decision_price and no confirmed fill -> reference is
    # unknown (None), routing the guard to its conservative missing-price path.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0},
            ]
        ),
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].reference_price is None


def _manual_recovery_run_state(candidates, **classification_overrides):
    classification = {
        "run_origin": "manual",
        "manual_run": True,
        "same_day_top_up_context_only": True,
        "valid_scheduled_evidence": False,
        "run_id": "manual_recovery_am",
    }
    classification.update(classification_overrides)
    return {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T10:48:00-04:00",
        "run_classification": classification,
        "buy_candidates": candidates,
    }


def test_load_prior_same_day_includes_correctly_classified_manual_recovery(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "manual_recovery/20260629_1048ET",
        _manual_recovery_run_state(
            [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0}]
        ),
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert set(allocations) == {"AAPL"}
    assert allocations["AAPL"].amount_usd == 3.0


def test_load_prior_same_day_excludes_misclassified_manual_recovery(tmp_path):
    # manual_missed_run_recovery requires all four classification fields; a
    # recovery artifact missing/failing one of them must not reduce or block
    # a later buy (config/morning_dca.json: manual_missed_run_recovery).
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "manual_recovery/20260629_1048ET",
        _manual_recovery_run_state(
            [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0}],
            valid_scheduled_evidence=True,
        ),
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations == {}


def test_load_prior_same_day_excludes_manual_recovery_missing_classification(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root,
        "manual_recovery/20260629_1048ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T10:48:00-04:00",
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0}
            ],
        },
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations == {}


def test_load_prior_same_day_prefers_logged_target_over_fill_price(tmp_path):
    # Reference-price preference: the logged target_price wins over the confirmed
    # average_fill_price when both are present.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                },
            ]
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "97.5"}],
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].reference_price == 99.0


def test_load_prior_same_day_falls_back_to_fill_price_without_logged_price(tmp_path):
    # No logged target_price/decision_price -> use the confirmed average_fill_price.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0},
            ]
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "97.5"}],
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].reference_price == 97.5


def test_load_prior_same_day_reads_manual_override_filled_orders_csv(tmp_path):
    # A manual override rerun records confirmed fills in filled_orders.csv (not
    # orders.csv). Without a logged price the guard falls back to that fill price,
    # so a manual override still contributes prior-allocation context.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "manual_reruns", "1505")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(
            _manual_run_state(
                [
                    {"ticker": "AMZN", "status": "filled", "planned_amount_usd": 3.0},
                ]
            ),
            handle,
        )
    with open(
        os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write("ticker,amount_usd,avg_fill_price\nAMZN,2.5,242.1099\n")
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AMZN"].amount_usd == 2.5
    assert allocations["AMZN"].reference_price == 242.1099
    assert allocations["AMZN"].source == "manual"


def test_load_prior_same_day_reads_documented_manual_filled_orders_shape(tmp_path):
    # Documented manual override fills can omit orders.csv-only fields such as
    # status and quantity_filled, while recording amount_usd and avg_fill_price.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "manual_reruns", "1505")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(
            _manual_run_state(
                [
                    {"ticker": "AMZN", "status": "filled", "planned_amount_usd": 6.0},
                ]
            ),
            handle,
        )
    with open(
        os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write("ticker,amount_usd,avg_fill_price\nAMZN,4.25,242.1099\n")

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AMZN"].amount_usd == 4.25
    assert allocations["AMZN"].reference_price == 242.1099
    assert allocations["AMZN"].source == "manual"


def _recovery_run_state(candidates, generated_at="2026-06-29T13:05:00-04:00"):
    """A scheduled-recovery run_state, the Jul 27 2026 12:30 recovery shape."""
    return {
        "trading_day": "2026-06-29",
        "generated_at": generated_at,
        "run_classification": {
            "run_origin": "scheduled_recovery",
            "run_type": "midday_dca_live_recovery",
            "stream": "midday_recovery",
            "manual_run": False,
            "trading_day": "2026-06-29",
            "run_id": "midday_recovery_20260629_1305ET",
            "state_schema_version": "midday-live-v2-recovery",
            "valid_scheduled_evidence": False,
            "valid_recovery_context": True,
            "same_day_top_up_context_only": True,
        },
        "buy_candidates": candidates,
    }


def test_orders_csv_fills_reads_recovery_fill_column_shape(tmp_path):
    # Codex review: "Teach the final-run loader the recovery fill shape". A
    # recovery artifact persists the broker's own column names
    # (average_fill_price / quantity) rather than the codebase-standard
    # avg_fill_price / quantity_filled. _orders_csv_fills recognized only the
    # latter, so every such row parsed to no price AND no quantity.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "20260629_1230ET_recovery_1305ET"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,3.50,filled,109.989900,0.031821\n"
        "GFUZ,10.00,filled,6.249900,1.600025\n",
        encoding="utf-8",
    )

    fills = _orders_csv_fills(run_dir)

    assert fills["SPCX"]["avg_fill_price"] == 109.9899
    assert fills["SPCX"]["quantity_filled"] == 0.031821
    assert fills["SPCX"]["amount_usd"] == 3.50
    assert fills["GFUZ"]["avg_fill_price"] == 6.2499
    assert fills["GFUZ"]["quantity_filled"] == 1.600025


def test_orders_csv_fills_honors_recovery_state_column(tmp_path):
    # Codex review: "Honor the recovery row state before counting fills". The
    # broker-native recovery shape records the outcome in `state`, not `status`.
    # A statusless row in filled_orders.csv hit the is_manual_filled_orders
    # heuristic and was counted as deployed capital regardless of state, so a
    # rejected or canceled order reduced -- or entirely suppressed -- that
    # ticker's later 3:30 allocation for capital that never moved.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "20260629_1230ET_recovery_1305ET"
    run_dir.mkdir(parents=True)
    # Revised for "Preserve executed partial fills in recovery CSVs": the gate is
    # on EXECUTED QUANTITY, not the state name. A non-'filled' row that executed
    # nothing is dropped; one that executed real shares is kept, because
    # discarding deployed capital lets the later run re-buy the full amount.
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,3.50,filled,109.989900,0.031821\n"
        "EVR,1.00,rejected,250.000000,0\n"
        "DXYZ,1.00,canceled,22.601700,\n"
        "GS,1.00,,1041.181500,-\n",  # blank state, nothing executed
        encoding="utf-8",
    )

    fills = _orders_csv_fills(run_dir)

    assert "SPCX" in fills
    for ticker in ("EVR", "DXYZ", "GS"):
        assert ticker not in fills, ticker


def test_orders_csv_fills_keeps_executed_partial_and_canceled_rows(tmp_path):
    # Codex review (P1): "Preserve executed partial fills in recovery CSVs".
    # An exact state=='filled' gate discarded a partially_filled row and a
    # canceled-after-partial-execution row, both of which are real deployed
    # capital -- scripts/broker_reconciliation.py already counts them as prior
    # exposure precisely so a later FIRST_ALLOCATION cannot re-buy the full
    # amount on top of shares already held.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "20260727_1430ET_recovery_1432ET"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\n"
        # amount_usd is the REQUESTED $4.00; only half the quantity executed.
        "FLKR,4.00,partially_filled,50.000000,0.040000\n"
        "AMZN,1.00,canceled,200.000000,0.002500\n",
        encoding="utf-8",
    )

    fills = _orders_csv_fills(run_dir)

    assert set(fills) == {"FLKR", "AMZN"}
    # The executed cash is price * executed quantity, NOT the requested
    # amount_usd -- using the request would overstate prior allocation and
    # suppress a legitimate top-up.
    assert abs(fills["FLKR"]["amount_usd"] - 2.00) < 1e-9
    assert fills["FLKR"]["avg_fill_price"] == 50.0
    assert fills["FLKR"]["quantity_filled"] == 0.04
    assert abs(fills["AMZN"]["amount_usd"] - 0.50) < 1e-9


def test_submitted_notionals_drop_terminal_non_fill_states(tmp_path):
    # Codex review: "Apply the recovery state gate to submitted notionals". A
    # recovery row can carry BOTH status='submitted' and a broker outcome in
    # `state`. The fill reader drops a rejected row, but this reader still added
    # its notional as unknown-price prior allocation, pushing that ticker onto
    # the conservative $0 manual-review path for an order known not to have
    # filled.
    from near_close_dca import _orders_csv_submitted_notionals

    csv_path = tmp_path / "submitted_orders.csv"
    csv_path.write_text(
        "ticker,status,state,notional_usd,quantity\n"
        "SPCX,submitted,rejected,3.50,0\n"
        "DXYZ,submitted,canceled,1.00,\n"
        "GS,submitted,cancelled,1.00,-\n"
        "FLKR,submitted,failed,4.00,0\n"
        # Still live or already done -- both must keep counting.
        "AMZN,submitted,pending,1.00,\n"
        "GFUZ,submitted,queued,10.00,\n"
        "EVR,submitted,filled,1.00,0.002968\n"
        # Terminal BUT executed: real deployed capital, must keep counting
        # (Codex review: "Preserve executed partial fills in recovery CSVs").
        "TRIN,submitted,canceled,2.00,0.010000\n",
        encoding="utf-8",
    )

    notionals = _orders_csv_submitted_notionals(csv_path)

    # Revised for "Require explicit zero before dropping submitted notionals":
    # only SPCX and FLKR state a definitive numeric zero. DXYZ (blank) and GS
    # ('-') are terminal with an UNRESOLVED quantity -- the artifact does not
    # establish whether they partially executed, and dropping them removes
    # their notional entirely, letting a later run re-buy the full amount.
    assert set(notionals) == {"AMZN", "GFUZ", "EVR", "TRIN", "DXYZ", "GS"}
    for dropped in ("SPCX", "FLKR"):
        assert dropped not in notionals, dropped


def test_submitted_notionals_keep_statusless_live_recovery_rows(tmp_path):
    # Codex review: "Retain statusless nonterminal recovery rows". A
    # broker-native recovery CSV may omit `status` entirely and report its
    # outcome only in `state` -- a shape _orders_csv_fills already treats as
    # authoritative. Requiring status=='submitted' dropped a still-LIVE row, so
    # a candidate-less recovery contributed no prior allocation and a later run
    # could submit the full amount while that order was outstanding.
    from near_close_dca import _orders_csv_submitted_notionals

    csv_path = tmp_path / "submitted_orders.csv"
    csv_path.write_text(
        "ticker,state,notional_usd,quantity\n"
        "AMZN,pending,1.00,\n"
        "GFUZ,queued,10.00,\n"
        # Terminal with an explicit zero: no capital moved, still dropped.
        "SPCX,rejected,3.50,0\n"
        # _orders_csv_fills owns a filled row and reports a real price, so this
        # unresolved-context reader must not also claim it.
        "EVR,filled,1.00,0.002968\n",
        encoding="utf-8",
    )

    notionals = _orders_csv_submitted_notionals(csv_path)

    assert set(notionals) == {"AMZN", "GFUZ"}


def test_submitted_notionals_keep_rows_without_a_state_column(tmp_path):
    # The gate is scoped to terminal non-fill states, so an ordinary
    # submitted_orders.csv with no `state` column is untouched. Dropping an
    # unknown-outcome row would UNDERCOUNT prior allocation and risk re-buying.
    from near_close_dca import _orders_csv_submitted_notionals

    csv_path = tmp_path / "submitted_orders.csv"
    csv_path.write_text(
        "ticker,status,notional_usd\nAAPL,submitted,5.00\n", encoding="utf-8"
    )

    assert _orders_csv_submitted_notionals(csv_path) == {"AAPL": 5.00}


def test_orders_csv_fills_accepts_broker_state_filled_over_submitted_status(tmp_path):
    # Codex review: "Let the broker state confirm filled recovery rows".
    # status='submitted' with state='filled' was dropped by the status gate, so
    # only _orders_csv_submitted_notionals saw it -- as an unknown-price
    # notional, forcing the ticker to the conservative $0 manual-review path
    # despite a real fill price and quantity being right there in the row.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "20260727_1430ET_recovery_1432ET"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,status,state,amount_usd,average_fill_price,quantity\n"
        "EVR,submitted,filled,1.00,336.916900,0.002968\n",
        encoding="utf-8",
    )

    fills = _orders_csv_fills(run_dir)

    assert fills["EVR"]["avg_fill_price"] == 336.9169
    assert fills["EVR"]["quantity_filled"] == 0.002968
    # A full fill keeps its recorded amount -- the partial recompute must not
    # fire for a row the broker confirmed as filled.
    assert fills["EVR"]["amount_usd"] == 1.00


def test_orders_csv_fills_accepts_submitted_partial_executions(tmp_path):
    # Codex review: "Let submitted partial executions reach the fill reader".
    # The previous round taught the status gate about state='filled' but not
    # about a row still carrying status='submitted' while the broker reports
    # partially_filled (or canceled after executing). Such a row was dropped
    # here while _orders_csv_submitted_notionals kept the FULL requested
    # notional, so a $10 request with $2 executed counted as $10 deployed and
    # suppressed the remaining $8 top-up.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "20260727_1430ET_recovery_1432ET"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,status,state,amount_usd,average_fill_price,quantity\n"
        "GFUZ,submitted,partially_filled,10.00,10.000000,0.200000\n"
        "AMZN,submitted,canceled,1.00,200.000000,0.002500\n"
        # Nothing executed: still dropped.
        "EVR,submitted,rejected,1.00,250.000000,0\n",
        encoding="utf-8",
    )

    fills = _orders_csv_fills(run_dir)

    assert set(fills) == {"GFUZ", "AMZN"}
    # Executed price * quantity wins over the requested amount.
    assert abs(fills["GFUZ"]["amount_usd"] - 2.00) < 1e-9
    assert abs(fills["AMZN"]["amount_usd"] - 0.50) < 1e-9


def test_orders_csv_fills_without_state_column_is_unchanged(tmp_path):
    # The state gate applies only when the column is PRESENT. A manual-override
    # filled_orders.csv carrying neither status nor state must keep parsing as
    # confirmed, exactly as before.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "20260629_1530ET"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,avg_fill_price,quantity_filled\nAAPL,5.00,100.0,0.05\n",
        encoding="utf-8",
    )

    assert _orders_csv_fills(run_dir)["AAPL"]["amount_usd"] == 5.00


def test_orders_csv_fills_prefers_canonical_column_over_recovery_alias(tmp_path):
    # An artifact carrying BOTH spellings must resolve on the codebase-standard
    # column, so adding the aliases cannot change how existing artifacts parse.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,status,avg_fill_price,average_fill_price,quantity_filled,quantity\n"
        "SPCX,3.50,filled,100.0,999.0,0.5,42.0\n",
        encoding="utf-8",
    )

    fills = _orders_csv_fills(run_dir)

    assert fills["SPCX"]["avg_fill_price"] == 100.0
    assert fills["SPCX"]["quantity_filled"] == 0.5


def test_orders_csv_fills_falls_back_when_canonical_column_is_blank(tmp_path):
    # Both spellings present but the canonical one empty: the alias must still
    # resolve rather than the blank being read as "no value".
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,status,avg_fill_price,average_fill_price,quantity_filled,quantity\n"
        "SPCX,3.50,filled,,109.9899,,0.031821\n",
        encoding="utf-8",
    )

    fills = _orders_csv_fills(run_dir)

    assert fills["SPCX"]["avg_fill_price"] == 109.9899
    assert fills["SPCX"]["quantity_filled"] == 0.031821


def test_load_prior_same_day_reads_midday_recovery_artifact(tmp_path):
    # End-to-end for the Jul 27 2026 12:30 recovery pass: the artifact sits in a
    # dated sibling of the scheduled slot dir, carries the recovery
    # classification, and records fills in the recovery column shape. All three
    # had to work for the 3:30 final buy to see the $20.50 already spent --
    # otherwise those tickers look untouched and get re-allocated in full.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(
            _recovery_run_state(
                [
                    {"ticker": "SPCX", "status": "filled", "planned_amount_usd": 3.5},
                ]
            ),
            handle,
        )
    with open(
        os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,3.50,filled,109.989900,0.031821\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["SPCX"].amount_usd == 3.50
    assert allocations["SPCX"].reference_price == 109.9899


def test_load_prior_same_day_follows_recorded_csv_for_candidate_backed_recovery(
    tmp_path,
):
    # Codex review: "Follow the recorded CSV for candidate-backed recoveries".
    # A recovery can write artifacts/run_state.json beside a ROOT-level CSV
    # under a CUSTOM name and record it as ../executions.csv, while ALSO
    # carrying non-empty buy_candidates. Three separate paths then miss it:
    # the fills lookup beside the state finds nothing, the recorded-path branch
    # is gated on candidates being EMPTY, and the fallback loader reports the
    # run covered because the candidates are there. The fixed-name root retry
    # does not save it either -- _orders_csv_fills() knows only
    # filled_orders.csv and orders.csv, not executions.csv.
    #
    # The candidate's PLANNED $3.50 would then stand in for an authoritative
    # PARTIAL $1.20 fill, overstating deployed capital and suppressing the
    # remaining 3:30 top-up.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    state_dir = os.path.join(run_dir, "artifacts")
    os.makedirs(state_dir, exist_ok=True)
    state = _recovery_run_state(
        [
            {"ticker": "SPCX", "status": "submitted", "planned_amount_usd": 3.5},
        ]
    )
    state["artifacts"] = {"orders_csv_path": "../executions.csv"}
    with open(
        os.path.join(state_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(state, handle)
    with open(os.path.join(run_dir, "executions.csv"), "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,1.20,partially_filled,109.989900,0.010910\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert abs(allocations["SPCX"].amount_usd - 1.20) < 1e-9
    # Revised for "Keep partial executions from confirming a reference price":
    # the executed $1.20 counts as deployed capital, but a partial's price does
    # NOT settle the reference -- the unexecuted remainder may still reprice or
    # be cancelled, so a later top-up must not be authorized against it. The
    # earlier version of this test asserted 109.9899 here, encoding that bug.
    assert allocations["SPCX"].reference_price is None


def test_load_prior_same_day_survives_malformed_recorded_csv_path(tmp_path):
    # Codex review: "Validate recorded CSV path types before constructing Path".
    # The recovery classification schema does not constrain the nested artifacts
    # block, so orders_csv_path can hold any JSON shape. Path([]) raises
    # TypeError, and that escaped load_prior_same_day_allocations() and aborted
    # the WHOLE same-day scan -- taking the afternoon run with it -- instead of
    # treating one artifact as unreadable. A GOOD sibling artifact must still
    # load, which is what proves the scan was not aborted.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    bad_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(bad_dir, exist_ok=True)
    bad_state = _recovery_run_state(
        [
            {"ticker": "SPCX", "status": "filled", "planned_amount_usd": 3.5},
        ]
    )
    bad_state["artifacts"] = {"orders_csv_path": []}
    with open(os.path.join(bad_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(bad_state, handle)

    good_dir = os.path.join(root, "manual_reruns", "1505")
    os.makedirs(good_dir, exist_ok=True)
    with open(
        os.path.join(good_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(
            _manual_run_state(
                [
                    {"ticker": "AMZN", "status": "filled", "planned_amount_usd": 3.0},
                ]
            ),
            handle,
        )
    with open(
        os.path.join(good_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write("ticker,amount_usd,avg_fill_price\nAMZN,2.5,242.1099\n")

    skipped = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skipped
    )

    # The scan completed rather than raising, and the good artifact loaded.
    assert allocations["AMZN"].amount_usd == 2.5
    # The malformed artifact is surfaced as unreadable, not silently ignored.
    assert any("20260629_1230ET_recovery_1305ET" in str(p) for p in skipped)


def test_load_prior_same_day_honors_terminal_rejection_for_candidate_recovery(tmp_path):
    # Codex review: "Honor terminal rejections for candidate-backed recoveries".
    # The recovery's recorded CSV says the order ended REJECTED having executed
    # nothing. _orders_csv_fills correctly returns no fill, but the candidate
    # loop then kept the full planned amount with an unknown reference price --
    # routing a ticker whose order is KNOWN not to have filled into the $0
    # manual-review guard and suppressing a legitimate later top-up. No capital
    # moved, so the ticker's amount_usd must stay $0 (updated for Codex review:
    # "Count every prior buy attempt toward the daily limit" -- the ticker now
    # DOES appear, with amount_usd=0.0, so its rejected attempt still counts
    # toward the shared max_orders_per_day cap when seeded; only the DOLLAR
    # contract from the original finding is unchanged).
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(run_dir, exist_ok=True)
    state = _recovery_run_state(
        [
            {
                "ticker": "EVR",
                "status": "submitted",
                "order_id": "ORD-REJECTED",
                "planned_amount_usd": 5.0,
            },
            {
                "ticker": "SPCX",
                "status": "submitted",
                "order_id": "ORD-FILLED",
                "planned_amount_usd": 3.5,
            },
        ]
    )
    state["artifacts"] = {"orders_csv_path": "filled_orders.csv"}
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    with open(
        os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "EVR,ORD-REJECTED,5.00,rejected,,0\n"
            "SPCX,ORD-FILLED,3.50,filled,109.989900,0.031821\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # Rejected with zero execution: no capital moved, so no dollar prior
    # allocation, but the attempt itself is still tracked.
    assert allocations["EVR"].amount_usd == 0.0
    assert allocations["EVR"].attempt_count == 1
    # The genuine fill in the same CSV is unaffected.
    assert allocations["SPCX"].amount_usd == 3.50
    assert allocations["SPCX"].reference_price == 109.9899


def test_idless_ambiguous_candidate_ignores_ticker_level_terminal_rejection(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(run_dir, exist_ok=True)
    state = _recovery_run_state([{
        "ticker": "EVR",
        "status": "failed",
        "ambiguous_placement_exception": True,
        "planned_amount_usd": 5.0,
    }])
    state["artifacts"] = {"orders_csv_path": "filled_orders.csv"}
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    with open(os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "EVR,UNRELATED-REJECTED,0,rejected,,0\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["EVR"].amount_usd == 5.0


def test_terminal_non_fill_states_match_the_canonical_broker_set():
    # Codex review: "Treat voided and expired broker orders as terminal". The
    # set was hand-copied into two modules and BOTH copies omitted 'voided' and
    # 'expired', so an order that could no longer fill was carried as live
    # unknown context and forced its ticker onto the $0 manual-review path.
    # Both readers now source the canonical set, so a future addition to the
    # broker normalizer reaches them without a third edit.

    from near_close_dca import _TERMINAL_NON_FILL_STATES
    from scripts.broker_reconciliation import _TERMINAL_ORDER_STATES
    from scripts.run_classification import _TERMINAL_NON_FILL_STATES as rc_terminal

    assert _TERMINAL_NON_FILL_STATES == frozenset(_TERMINAL_ORDER_STATES)
    assert rc_terminal == frozenset(_TERMINAL_ORDER_STATES)
    for state in ("voided", "expired"):
        assert state in _TERMINAL_NON_FILL_STATES
        assert state in rc_terminal


def test_terminal_collector_refuses_truncated_rows(tmp_path):
    # Codex review: "Reject truncated terminal rows before excluding
    # candidates". csv.DictReader fills omitted TRAILING cells with None rather
    # than raising, so a canceled row read mid-write -- before its positive
    # cumulative_quantity was flushed -- looked like a zero-execution terminal
    # outcome. Acting on that would delete a candidate whose order really did
    # partially fill, letting the later run re-buy its full amount.
    from near_close_dca import _orders_csv_terminal_non_fills

    csv_path = tmp_path / "filled_orders.csv"
    csv_path.write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,cumulative_quantity\n"
        "EVR,ORD-COMPLETE,5.00,rejected,,0\n"
        # Mid-write: the trailing columns were never flushed.
        "SPCX,ORD-TRUNCATED,3.50,canceled\n",
        encoding="utf-8",
    )

    order_ids, tickers = _orders_csv_terminal_non_fills(csv_path)

    # The complete rejection is authoritative; the truncated row is refused.
    assert order_ids == {"ORD-COMPLETE"}
    assert "ORD-TRUNCATED" not in order_ids
    assert tickers == set()


def test_load_prior_same_day_merges_recorded_fills_with_sibling_fills(tmp_path):
    # Codex review: "Merge recorded recovery fills even when sibling fills
    # exist". Gating the recorded CSV on `fills` being EMPTY meant a single fill
    # beside run_state.json made the whole recorded CSV invisible, so any other
    # ticker it confirmed fell back to its candidate's full planned amount --
    # here a $3.00 executed partial of a $10.00 candidate counting as $10.00.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    state_dir = os.path.join(run_dir, "artifacts")
    os.makedirs(state_dir, exist_ok=True)
    state = _recovery_run_state(
        [
            {"ticker": "AMZN", "status": "filled", "planned_amount_usd": 1.0},
            {"ticker": "GFUZ", "status": "submitted", "planned_amount_usd": 10.0},
        ]
    )
    state["artifacts"] = {"orders_csv_path": "../executions.csv"}
    with open(
        os.path.join(state_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(state, handle)
    # A fill sits BESIDE run_state.json, so `fills` is already truthy.
    with open(
        os.path.join(state_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "AMZN,1.00,filled,231.429900,0.004320\n"
        )
    # The recorded in-root CSV confirms a DIFFERENT ticker.
    with open(os.path.join(run_dir, "executions.csv"), "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,10.00,partially_filled,6.249900,0.480000\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # The sibling fill is unchanged -- merging must never overwrite it.
    assert abs(allocations["AMZN"].amount_usd - 1.00) < 1e-9
    assert allocations["AMZN"].reference_price == 231.4299
    # And the recorded CSV's ticker is sized by EXECUTED capital, not the $10
    # planned amount its candidate carries.
    assert abs(allocations["GFUZ"].amount_usd - 3.00) < 1e-9
    # Partial: notional counts, reference price stays unresolved.
    assert allocations["GFUZ"].reference_price is None


def test_terminal_collector_requires_an_explicit_zero(tmp_path):
    # Codex review: "Require an explicit zero before excluding terminal
    # candidates". A COMPLETE row with a malformed executed-quantity value says
    # "unknown", not "nothing filled". Reading it as a definitive zero removes a
    # candidate whose order may really have partially executed, and the later
    # run then buys its full amount again.
    from near_close_dca import _orders_csv_terminal_non_fills

    csv_path = tmp_path / "filled_orders.csv"
    csv_path.write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,cumulative_quantity\n"
        "EVR,ORD-ZERO,5.00,canceled,,0\n"
        "SPCX,ORD-GARBAGE,3.50,canceled,,oops\n"
        "GS,ORD-BLANK,1.00,canceled,,\n",
        encoding="utf-8",
    )

    order_ids, _tickers = _orders_csv_terminal_non_fills(csv_path)

    # Only the explicit numeric zero is definitive evidence of no execution.
    assert order_ids == {"ORD-ZERO"}


def test_load_prior_same_day_prefers_a_final_fill_over_a_stale_partial(tmp_path):
    # Codex review: "Reconcile overlapping ticker fills instead of keeping the
    # first". A stale partially_filled snapshot beside run_state.json and the
    # recorded CSV's final filled state describe the SAME order. setdefault kept
    # the partial, preserving its smaller notional and unresolved reference
    # price -- an unnecessary manual-review stop for an order that completed.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    state_dir = os.path.join(run_dir, "artifacts")
    os.makedirs(state_dir, exist_ok=True)
    state = _recovery_run_state(
        [
            {
                "ticker": "GFUZ",
                "status": "submitted",
                "order_id": "ORD-GFUZ",
                "planned_amount_usd": 10.0,
            },
        ]
    )
    state["artifacts"] = {"orders_csv_path": "../executions.csv"}
    with open(
        os.path.join(state_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(state, handle)
    # Stale snapshot beside the state: only part of the order had executed.
    with open(
        os.path.join(state_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,ORD-GFUZ,10.00,partially_filled,6.249900,0.480000\n"
        )
    # The recorded CSV holds the same order's FINAL state.
    with open(os.path.join(run_dir, "executions.csv"), "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,ORD-GFUZ,10.00,filled,6.249900,1.600025\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # The completed fill wins: full notional, and a settled reference price.
    assert abs(allocations["GFUZ"].amount_usd - 10.00) < 1e-9
    assert allocations["GFUZ"].reference_price == 6.2499
    # And never summed -- the same order appearing twice is one allocation.
    assert allocations["GFUZ"].amount_usd < 20.0


def test_prior_run_is_scheduled_rejects_a_rerun_named_like_an_intraday_stream():
    # Codex review: "Validate ordinary intraday origins before loading context".
    # 'intraday_1430_rerun' starts with 'intraday' but ends in '_rerun', so it
    # missed the '_recovery' branch and was admitted here with NO validation --
    # even though run_origin='manual_live_recovery_rerun' is exactly the shape
    # every manifest in this PR records as rejected by is_top_up_context_state().
    from near_close_dca import _prior_run_is_scheduled

    rerun = {
        "trading_day": "2026-06-29",
        "run_classification": {
            "stream": "intraday_1430_rerun",
            "run_origin": "manual_live_recovery_rerun",
            "manual_run": False,
            "trading_day": "2026-06-29",
        },
    }
    # True == "treated as a scheduled run", i.e. NOT loaded as prior context.
    assert _prior_run_is_scheduled(rerun) is True

    # An ordinary scheduled intraday run is still eligible prior context.
    for stream in ("intraday", "intraday_1130", "intraday_1430"):
        ordinary = {
            "trading_day": "2026-06-29",
            "run_classification": {
                "stream": stream,
                "run_origin": "scheduled",
                "manual_run": False,
                "trading_day": "2026-06-29",
            },
        }
        assert _prior_run_is_scheduled(ordinary) is False, stream

    # A manual_run flag wins over the stream name, as everywhere else.
    manual = {
        "trading_day": "2026-06-29",
        "run_classification": {
            "stream": "intraday_1130",
            "run_origin": "scheduled",
            "manual_run": True,
            "trading_day": "2026-06-29",
        },
    }
    assert _prior_run_is_scheduled(manual) is True


def test_submitted_notionals_read_the_recovery_amount_column(tmp_path):
    # Codex review: "Read amount_usd for unresolved recovery submissions". The
    # broker-native recovery shape records amount_usd, which _orders_csv_fills
    # already accepts. Reading only notional_usd meant a statusless live row
    # passed the gate and then contributed NOTHING, so a later run could submit
    # the full allocation while that order was still outstanding.
    from near_close_dca import _orders_csv_submitted_notionals

    csv_path = tmp_path / "submitted_orders.csv"
    csv_path.write_text(
        "ticker,state,amount_usd\n" "AMZN,pending,1.00\n" "GFUZ,queued,10.00\n",
        encoding="utf-8",
    )
    notionals = _orders_csv_submitted_notionals(csv_path)
    assert abs(notionals["AMZN"] - 1.00) < 1e-9
    assert abs(notionals["GFUZ"] - 10.00) < 1e-9

    # Canonical column wins when an artifact carries both.
    both = tmp_path / "orders.csv"
    both.write_text(
        "ticker,status,notional_usd,amount_usd\nEVR,submitted,3.00,99.00\n",
        encoding="utf-8",
    )
    assert abs(_orders_csv_submitted_notionals(both)["EVR"] - 3.00) < 1e-9


def test_load_prior_same_day_sums_two_distinct_orders_for_one_ticker(tmp_path):
    # Codex review: "Merge recovery fills by order ID before aggregating
    # tickers". Two DISTINCT orders for one ticker across sibling and recorded
    # CSVs are two real capital movements. Keying the merge only by ticker kept
    # the first and discarded the second, understating deployed capital and
    # letting the candidate loop apply one order's fill to every candidate for
    # that ticker -- permitting an excessive top-up.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    state_dir = os.path.join(run_dir, "artifacts")
    os.makedirs(state_dir, exist_ok=True)
    state = _recovery_run_state(
        [
            {
                "ticker": "GFUZ",
                "status": "filled",
                "order_id": "ORD-A",
                "planned_amount_usd": 4.0,
            },
        ]
    )
    state["artifacts"] = {"orders_csv_path": "../executions.csv"}
    with open(
        os.path.join(state_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(state, handle)
    with open(
        os.path.join(state_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,ORD-A,4.00,filled,4.000000,1.000000\n"
        )
    with open(os.path.join(run_dir, "executions.csv"), "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,ORD-B,6.00,filled,2.000000,3.000000\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["GFUZ"].amount_usd is not None
    assert allocations["GFUZ"].reference_price is not None
    # Both orders count: $4.00 + $6.00.
    assert abs(allocations["GFUZ"].amount_usd - 10.00) < 1e-9
    # Quantity-weighted average of 4.00 x 1 and 2.00 x 3 = 10/4 = 2.50.
    assert abs(allocations["GFUZ"].reference_price - 2.50) < 1e-9


def test_load_prior_same_day_does_not_sum_the_same_order_twice(tmp_path):
    # The other half of the same rule: one order appearing in two sibling CSVs
    # is ONE order. Summing there would double-count real capital, which is the
    # direction that spends money twice.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    state_dir = os.path.join(run_dir, "artifacts")
    os.makedirs(state_dir, exist_ok=True)
    state = _recovery_run_state(
        [
            {
                "ticker": "GFUZ",
                "status": "filled",
                "order_id": "ORD-A",
                "planned_amount_usd": 4.0,
            },
        ]
    )
    state["artifacts"] = {"orders_csv_path": "../executions.csv"}
    with open(
        os.path.join(state_dir, "run_state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(state, handle)
    with open(
        os.path.join(state_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,ORD-A,4.00,partially_filled,4.000000,1.000000\n"
        )
    with open(os.path.join(run_dir, "executions.csv"), "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,ORD-A,4.00,filled,4.000000,1.000000\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert abs(allocations["GFUZ"].amount_usd - 4.00) < 1e-9
    # The completed snapshot won the tie, so the price is settled.
    assert allocations["GFUZ"].reference_price == 4.0


def _recovery_classification(stream="midday_recovery", **overrides):
    # stream/run_type/state_schema_version must correspond, so derive the two
    # dependent values from the stream rather than only swapping the name.
    base = stream[: -len("_recovery")]
    rc = dict(_recovery_run_state([])["run_classification"])
    rc["stream"] = stream
    rc["run_type"] = f"{base}_dca_live_recovery"
    rc["state_schema_version"] = f"{base.replace('_', '-')}-live-v2-recovery"
    rc.update(overrides)
    return rc


def test_prior_run_is_scheduled_treats_recovery_streams_as_prior_context():
    # A recovery stream for an earlier slot is eligible prior context, but only
    # when it satisfies the full scheduled-recovery contract.
    from near_close_dca import _prior_run_is_scheduled

    for stream in ("midday_recovery", "morning_recovery", "market_open_recovery"):
        # The top-level trading_day is REQUIRED on a recovery state (Codex
        # review: "Require a top-level trading day on recovery states"),
        # matching the day filter load_prior_same_day_allocations applies.
        state = {
            "trading_day": "2026-06-29",
            "run_classification": _recovery_classification(stream),
        }
        assert _prior_run_is_scheduled(state) is False, stream
        # Without it the artifact is ignored (True = treated as scheduled), the
        # safe direction and the one the allocation loader already takes.
        assert (
            _prior_run_is_scheduled(
                {"run_classification": _recovery_classification(stream)}
            )
            is True
        ), stream

    afternoon = {
        "run_classification": {"stream": "afternoon", "run_origin": "scheduled"}
    }
    assert _prior_run_is_scheduled(afternoon) is True


def test_prior_run_is_scheduled_gates_recovery_streams_on_the_contract():
    # Codex review: "Gate recovery streams before loading their fills". A
    # corrupted or misclassified artifact that merely NAMES a recovery stream
    # must not be read as prior context -- it could otherwise wrongly reduce or
    # block the 3:30 allocation. True here means "treated as scheduled", i.e.
    # ignored, which is the safe direction.
    from near_close_dca import _prior_run_is_scheduled

    # The bare shape: a recovery stream name and nothing else.
    assert (
        _prior_run_is_scheduled(
            {
                "run_classification": {
                    "stream": "midday_recovery",
                    "run_origin": "scheduled",
                }
            }
        )
        is True
    )

    for override in (
        {"valid_recovery_context": None},
        {"valid_scheduled_evidence": True},
        {"same_day_top_up_context_only": False},
        {"manual_run": True},
        {"run_origin": "scheduled"},
        {"recovery_rerun": True},
    ):
        state = {"run_classification": _recovery_classification(**override)}
        assert _prior_run_is_scheduled(state) is True, override


def test_prior_run_is_scheduled_excludes_afternoon_recovery():
    # An afternoon recovery is the afternoon run's own repair, never prior
    # context for it -- even when perfectly well-formed.
    from near_close_dca import _prior_run_is_scheduled

    state = {"run_classification": _recovery_classification("afternoon_recovery")}

    assert _prior_run_is_scheduled(state) is True


def test_recovery_orders_csv_path_outside_run_dir_is_ignored(tmp_path):
    # Codex review: "Constrain recovery fill paths to their run root". Recovery
    # artifacts became eligible prior context, and a copied one can carry a
    # stale ABSOLUTE orders_csv_path naming the run it was copied from.
    # Following it verbatim loads an unrelated CSV's fills as this pass's
    # recovery fills, which can reduce or block the later 3:30 buy.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(run_dir, exist_ok=True)

    # An unrelated CSV OUTSIDE the recovery run directory.
    outside_dir = os.path.join(tmp_path, "elsewhere", "other_run")
    os.makedirs(outside_dir, exist_ok=True)
    outside_csv = os.path.join(outside_dir, "filled_orders.csv")
    with open(outside_csv, "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "AAPL,99.00,filled,100.0,0.99\n"
        )

    state = _recovery_run_state([])  # no buy_candidates -> the CSV branch
    state["artifacts"] = {"orders_csv_path": outside_csv}
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # The unrelated ticker must not appear as recovery context.
    assert "AAPL" not in allocations


def test_recovery_orders_csv_path_inside_run_dir_is_loaded(tmp_path):
    # The containment check must not break the legitimate case: a recorded path
    # that does resolve inside the recovery run directory is still followed.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(run_dir, exist_ok=True)
    inside_csv = os.path.join(run_dir, "dca_recovery_20260629_orders.csv")
    with open(inside_csv, "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,3.50,filled,109.9899,0.031821\n"
        )

    state = _recovery_run_state([])
    state["artifacts"] = {"orders_csv_path": inside_csv}
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["SPCX"].amount_usd == 3.50
    assert allocations["SPCX"].reference_price == 109.9899


def test_market_open_bare_submitted_orders_csv_is_read_with_no_candidates_or_recorded_path(
    tmp_path,
):
    """Codex review: "Read canonical CSVs during same-day cap seeding". The
    market-open/intraday streams have no in-repo Python entry point at all --
    their Claude-executor prompts write only run_state.json (no embedded
    buy_candidates, no artifacts.orders_csv_path) plus a bare sibling
    submitted_orders.csv using the canonical submitted_amount_usd column.
    Without a fallback to that bare sibling, a valid same-day fill recorded
    only there contributed $0 to the shared same-day cap ledger."""
    root = os.path.join(tmp_path, "cron_tracking", "market_open_dca")
    run_dir = os.path.join(root, "20260629_0935ET")
    os.makedirs(run_dir, exist_ok=True)

    state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T09:35:00-04:00",
        "run_classification": {
            "run_origin": "scheduled",
            "run_type": "market_open_dca_live",
            "stream": "market_open",
            "manual_run": False,
            "trading_day": "2026-06-29",
            "run_id": "market_open_live_20260629_0935ET",
        },
        "buy_candidates": [],
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    with open(
        os.path.join(run_dir, "submitted_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,status,submitted_amount_usd,avg_fill_price,quantity_filled\n"
            "AAAU,ORD-1,filled,2.00,50.00,0.04\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAAU"].amount_usd == pytest.approx(2.00)
    assert allocations["AAAU"].reference_price == 50.00


def test_market_open_bare_submitted_orders_csv_unresolved_submitted_row(tmp_path):
    """The submitted-but-not-yet-filled case for the same bare-sibling
    fallback: a still-live order contributes its amount as prior allocation
    with an UNKNOWN reference price (the conservative path), same as the
    recorded-CSV branch already does."""
    root = os.path.join(tmp_path, "cron_tracking", "market_open_dca")
    run_dir = os.path.join(root, "20260629_0935ET")
    os.makedirs(run_dir, exist_ok=True)

    state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T09:35:00-04:00",
        "run_classification": {
            "run_origin": "scheduled",
            "run_type": "market_open_dca_live",
            "stream": "market_open",
            "manual_run": False,
            "trading_day": "2026-06-29",
            "run_id": "market_open_live_20260629_0935ET",
        },
        "buy_candidates": [],
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    with open(
        os.path.join(run_dir, "submitted_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,status,submitted_amount_usd,avg_fill_price,quantity_filled\n"
            "AAAU,ORD-1,submitted,2.00,,\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAAU"].amount_usd == pytest.approx(2.00)
    assert allocations["AAAU"].reference_price is None


def test_market_open_bare_submitted_orders_csv_reserves_pending_and_unconfirmed_rows(
    tmp_path,
):
    """Codex review: "Reserve pending orders loaded from the canonical CSV".
    A canonical submitted_orders.csv row can carry an explicit status of
    "pending" or "unconfirmed" (scripts.scheduled_artifacts.
    ORDER_ATTEMPT_STATUSES), not just "submitted". The old status check
    accepted only status == "submitted" exactly, falling back to the
    statusless-live path only when the status column was entirely empty --
    so a row with an explicit "pending"/"unconfirmed" status was silently
    dropped, reconstructing that attempt as $0 exposure and letting the same
    ticker be bought again while the real order was still outstanding."""
    root = os.path.join(tmp_path, "cron_tracking", "market_open_dca")
    run_dir = os.path.join(root, "20260629_0935ET")
    os.makedirs(run_dir, exist_ok=True)

    state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T09:35:00-04:00",
        "run_classification": {
            "run_origin": "scheduled",
            "run_type": "market_open_dca_live",
            "stream": "market_open",
            "manual_run": False,
            "trading_day": "2026-06-29",
            "run_id": "market_open_live_20260629_0935ET",
        },
        "buy_candidates": [],
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    with open(
        os.path.join(run_dir, "submitted_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,status,submitted_amount_usd,avg_fill_price,quantity_filled\n"
            "AAAU,ORD-1,pending,2.00,,\n"
            "IEMG,ORD-2,unconfirmed,3.50,,\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAAU"].amount_usd == pytest.approx(2.00)
    assert allocations["AAAU"].reference_price is None
    assert allocations["IEMG"].amount_usd == pytest.approx(3.50)
    assert allocations["IEMG"].reference_price is None


def test_recovery_external_csv_path_falls_back_to_the_local_sibling(tmp_path):
    # Codex review: "Fall back to the recovery run's local CSV". Rejecting the
    # external path outright turned a wrong allocation into NO allocation --
    # the same money outcome, since the ticker gets bought again. A copied
    # recovery state with a stale absolute path AND a valid local
    # filled_orders.csv must load the local one.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
    os.makedirs(run_dir, exist_ok=True)

    outside_dir = os.path.join(tmp_path, "elsewhere", "other_run")
    os.makedirs(outside_dir, exist_ok=True)
    outside_csv = os.path.join(outside_dir, "filled_orders.csv")
    with open(outside_csv, "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "AAPL,99.00,filled,100.0,0.99\n"
        )
    # The SAME basename, locally, with this run's real fills.
    with open(
        os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,3.50,filled,109.9899,0.031821\n"
        )

    state = _recovery_run_state([])
    state["artifacts"] = {"orders_csv_path": outside_csv}
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # The local sibling is loaded...
    assert allocations["SPCX"].amount_usd == 3.50
    assert allocations["SPCX"].reference_price == 109.9899
    # ...and the unrelated external CSV still contributes nothing.
    assert "AAPL" not in allocations


def test_prior_run_is_scheduled_gates_on_recovery_origin_not_stream_name():
    # Codex review (P1): "Gate every scheduled-recovery origin before loading
    # fills". Keying the recovery branch off the stream NAME left every
    # malformed-stream shape unvalidated -- and 'intraday_bogus' was even
    # accepted as eligible by the startswith('intraday') arm. True here means
    # "treated as scheduled", i.e. ignored, which is the safe direction.
    from near_close_dca import _prior_run_is_scheduled

    for stream in ("intraday_bogus", None, 123, "", "midday", [], {}):
        state = {
            "run_classification": {"run_origin": "scheduled_recovery", "stream": stream}
        }
        assert _prior_run_is_scheduled(state) is True, stream

    # A conforming recovery artifact is still eligible prior context. It needs
    # its top-level trading_day, which the contract now requires (Codex review:
    # "Require a top-level trading day on recovery states").
    assert (
        _prior_run_is_scheduled(
            {
                "trading_day": "2026-06-29",
                "run_classification": _recovery_classification("midday_recovery"),
            }
        )
        is False
    )


def test_orders_csv_fills_rejects_non_finite_amount_and_price(tmp_path):
    # Gemini review on PR #31: _orders_csv_fills validated amount_usd/avg_fill_price
    # with only `value > 0`, which excludes NaN (NaN > 0 is False) but NOT
    # Infinity (float('inf') > 0 is True). A malformed/corrupted manual
    # filled_orders.csv row with an infinite amount_usd or avg_fill_price would
    # otherwise poison the same-day top-up guard with an unbounded prior
    # allocation or reference price.
    from near_close_dca import _orders_csv_fills

    run_dir = tmp_path / "manual_reruns" / "1505"
    run_dir.mkdir(parents=True)
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,avg_fill_price,quantity_filled\n"
        "AMZN,inf,242.1099,\n"
        "GOOG,4.5,Infinity,\n"
        "GOOGL,nan,242.1099,\n"
        "MU,4.5,242.1099,inf\n",
        encoding="utf-8",
    )
    fills = _orders_csv_fills(run_dir)
    assert fills["AMZN"]["amount_usd"] is None
    assert fills["GOOG"]["avg_fill_price"] is None
    assert fills["GOOGL"]["amount_usd"] is None
    assert fills["MU"]["quantity_filled"] is None


def test_load_prior_same_day_multi_merges_roots_sums_amounts_most_recent_reference(
    tmp_path,
):
    # The afternoon guard scans multiple roots (its own + morning + midday). When
    # a ticker appears in more than one root, amounts sum and the ticker's MOST
    # RECENT known reference price wins (by generated_at) -- NOT the lowest. Here
    # the later midday run has the HIGHER price, and it must win to prove the
    # resolution is recency-based, not lowest-based.
    morning_root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        morning_root,
        "20260629_1030ET",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 3.0,
                    "target_price": 97.0,
                }
            ],
            generated_at="2026-06-29T10:30:00-04:00",
            # Distinct run_origin per root so each contributes a distinct
            # run_id ("manual_morning_am" vs "manual_midday_am") -- these are
            # two genuinely separate runs, and a shared default run_id here
            # would be wrongly recognized as a mirrored duplicate by the
            # dedup added for "Deduplicate mirrored artifacts before pooling
            # spend".
            run_origin="manual_morning",
        ),
    )
    midday_root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        midday_root,
        "20260629_1230ET",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 4.5,
                    "target_price": 99.0,
                }
            ],
            generated_at="2026-06-29T12:30:00-04:00",
            run_origin="manual_midday",
        ),
    )
    allocations = load_prior_same_day_allocations_multi(
        (morning_root, midday_root), date(2026, 6, 29)
    )
    assert allocations["AAPL"].amount_usd == 7.5
    assert allocations["AAPL"].reference_price == 99.0


def test_load_prior_same_day_multi_deduplicates_a_mirrored_manual_override(tmp_path):
    """Codex review: "Deduplicate mirrored artifacts before pooling spend".
    The exact documented scenario: a manual override backfilled under both
    midday_dca/manual_reruns AND ce16ba6b/manual_runs is the SAME run,
    mirrored -- not two independent contributions. If each
    root's scan finds and sums the same run's dollars,
    load_prior_same_day_allocations_multi() records twice the real spend
    (existing.amount_usd + alloc.amount_usd unconditionally)."""
    same_run_id = "midday_manual_override_20260629_1345ET"
    mirrored_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T13:45:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": same_run_id},
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "filled",
                "planned_amount_usd": 5.0,
                "target_price": 98.0,
            },
        ],
    }
    root_a = os.path.join(tmp_path, "cron_tracking", "midday_dca", "manual_reruns")
    _write_prior_run(root_a, "20260629_1345ET", mirrored_state)
    root_b = os.path.join(tmp_path, "cron_tracking", "ce16ba6b", "manual_runs")
    _write_prior_run(root_b, "20260629_1345ET", mirrored_state)

    allocations = load_prior_same_day_allocations_multi(
        (
            os.path.join(tmp_path, "cron_tracking", "midday_dca"),
            os.path.join(tmp_path, "cron_tracking", "ce16ba6b"),
        ),
        date(2026, 6, 29),
        include_scheduled_afternoon=True,
    )

    # The SAME $5.00 order, mirrored under two roots, must count once.
    assert allocations["AAPL"].amount_usd == pytest.approx(5.0)
    assert allocations["AAPL"].attempt_count == 1


def test_load_prior_same_day_multi_deduplicates_a_mirrored_zero_notional_attempt(tmp_path):
    """Codex review: "Deduplicate mirrored zero-notional attempts". The same
    documented mirrored-run scenario as the dollar-dedup test above, but for a
    REJECTED order that contributed no dollars. contributing_run_ids is only
    populated once a row contributes a positive dollar amount, so a mirrored
    zero-notional attempt's run_id never lands in it and the dollar-dedup
    check can never recognize the duplicate -- before this fix, the additive
    branch counted the SAME broker attempt twice against attempt_count."""
    same_run_id = "midday_manual_override_20260629_1345ET"
    mirrored_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T13:45:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": same_run_id},
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "failed",
                "planned_amount_usd": 5.0,
                "skip_reason": "placement failed: broker timeout",
            },
        ],
    }
    root_a = os.path.join(tmp_path, "cron_tracking", "midday_dca", "manual_reruns")
    _write_prior_run(root_a, "20260629_1345ET", mirrored_state)
    root_b = os.path.join(tmp_path, "cron_tracking", "ce16ba6b", "manual_runs")
    _write_prior_run(root_b, "20260629_1345ET", mirrored_state)

    allocations = load_prior_same_day_allocations_multi(
        (
            os.path.join(tmp_path, "cron_tracking", "midday_dca"),
            os.path.join(tmp_path, "cron_tracking", "ce16ba6b"),
        ),
        date(2026, 6, 29),
        include_scheduled_afternoon=True,
    )

    # The SAME rejected attempt, mirrored under two roots, must count once.
    assert allocations["AAPL"].amount_usd == 0.0
    assert allocations["AAPL"].attempt_count == 1


def test_load_prior_same_day_multi_partial_overlap_falls_back_to_summing(tmp_path):
    """A root whose contributing run_ids for a ticker are only PARTIALLY
    already-seen (one shared/mirrored run plus one genuinely new one) can't
    be cleanly split into "new dollars" vs "duplicate dollars" without a
    per-run_id amount breakdown -- falls through to the existing additive
    merge, the conservative direction (over-restricts headroom rather than
    risking silently dropping a genuine contribution)."""
    root_a = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        root_a,
        "20260629_1030ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T10:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "morning_dca_live",
                "stream": "morning",
                "run_id": "shared_run_A",
            },
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 2.0},
            ],
        },
    )
    root_b = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    # Root B's own directory mirrors root A's run (same run_id "shared_run_A",
    # e.g. backfilled for audit) PLUS a genuinely separate, later midday run
    # ("run_B") for the same ticker.
    _write_prior_run(
        root_b,
        "20260629_1030ET_mirror",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T10:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "morning_dca_live",
                "stream": "morning",
                "run_id": "shared_run_A",
            },
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 2.0},
            ],
        },
    )
    _write_prior_run(
        root_b,
        "20260629_1230ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T12:30:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "run_type": "midday_dca_live",
                "stream": "midday",
                "run_id": "run_B",
            },
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0},
            ],
        },
    )

    allocations = load_prior_same_day_allocations_multi(
        (root_a, root_b), date(2026, 6, 29)
    )

    # root_b's own per-root total is already 2.0 (mirrored) + 3.0 (genuine) =
    # 5.0; only a FULL subset match against root_a's {shared_run_A} would
    # skip re-adding it, but root_b's set {shared_run_A, run_B} is not a
    # subset of root_a's {shared_run_A} -- so this sums root_a's 2.0 with
    # root_b's full 5.0, landing on 7.0, not the true 5.0. This is the
    # documented conservative fallback, not a silent drop.
    assert allocations["AAPL"].amount_usd == pytest.approx(7.0)


def test_load_prior_same_day_multi_missing_reference_poisons_to_none(tmp_path):
    # If any contributing run lacks a reference price, the merged reference is
    # unknown -- the guard can't safely compare, so it routes to the conservative
    # manual-review path rather than trusting a partial reference.
    morning_root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        morning_root,
        "20260629_1030ET",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                },
            ],
            # Distinct run_origin per root -- these are two separate runs;
            # see the matching note in
            # test_load_prior_same_day_multi_merges_roots_sums_amounts_most_recent_reference.
            run_origin="manual_morning",
        ),
    )
    midday_root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        midday_root,
        "20260629_1230ET",
        _manual_run_state(
            [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 4.5},
            ],
            run_origin="manual_midday",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "submitted", "avg_fill_price": ""}],
    )
    allocations = load_prior_same_day_allocations_multi(
        (morning_root, midday_root), date(2026, 6, 29)
    )
    assert allocations["AAPL"].amount_usd == 7.5
    assert allocations["AAPL"].reference_price is None
    assert allocations["AAPL"].reference_ts is None


def test_load_prior_same_day_multi_picks_chronologically_first_base_regardless_of_root_order(
    tmp_path,
):
    # Codex review ("Select the base from the chronologically first allocation"):
    # a 09:45 morning $1-base buy is the true first same-day allocation; a 15:05
    # manual $2-base buy under the afternoon root came later. The afternoon root
    # is passed FIRST here (matching run_dca.py's own scan order), but the
    # morning allocation's base must still win because it happened first.
    afternoon_root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        afternoon_root,
        "manual_runs/1505",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 2.0,
                    "selected_base_amount_usd": 2.0,
                    "target_price": 94.0,
                },
            ],
            generated_at="2026-06-29T15:05:00-04:00",
        ),
    )
    morning_root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        morning_root,
        "20260629_0945ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:45:00-04:00",
            "run_classification": {
                "run_origin": "scheduled",
                "stream": "morning",
                "run_id": "morning_0945",
            },
            "buy_candidates": [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 1.0,
                    "selected_base_amount_usd": 1.0,
                },
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )

    allocations = load_prior_same_day_allocations_multi(
        (afternoon_root, morning_root), date(2026, 6, 29), boundary_et=time(15, 20)
    )

    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].selected_base_amount_usd == 1.0


def test_load_prior_same_day_unconfirmed_submitted_order_has_no_reference_price(
    tmp_path,
):
    # status="submitted" in run_state.json only means an order_id was obtained,
    # not that it filled; when orders.csv doesn't confirm a fill (still
    # "submitted"), we can't tell whether real capital moved. Dropping the
    # ticker entirely would risk a double-buy if it actually filled but was
    # never refreshed, so it stays as prior allocation context with the known
    # planned amount but no reference price -- which routes the afternoon guard
    # to its conservative missing_prior_price / manual-review $0 path rather
    # than either a blind top-up or a blind full re-buy. With no logged
    # target_price/decision_price and no confirmed fill, no reference resolves.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0},
            ]
        ),
        orders_rows=[{"ticker": "AAPL", "status": "submitted", "avg_fill_price": ""}],
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].reference_price is None


def test_load_prior_same_day_excludes_dry_run_candidates(tmp_path):
    # A dry-run smoke test never places a real order (its own skip_reason says
    # so); it must never reduce the scheduled run's real buy.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "dry_run",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                },
            ]
        ),
    )
    assert load_prior_same_day_allocations(root, date(2026, 6, 29)) == {}


def test_load_prior_same_day_excludes_scheduled_and_after_boundary(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    # Scheduled run -> never prior context.
    _write_prior_run(
        root,
        "scheduled_runs/20260629_1520ET",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "submitted",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                }
            ],
            run_origin="scheduled",
        ),
    )
    # Manual run but after the 2:30 PM ET boundary -> excluded.
    _write_prior_run(
        root,
        "manual_runs/1500",
        _manual_run_state(
            [
                {
                    "ticker": "NVDA",
                    "status": "submitted",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                }
            ],
            generated_at="2026-06-29T15:00:00-04:00",
        ),
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations == {}


def test_load_prior_same_day_includes_morning_scheduled_run(tmp_path):
    """Morning scheduled runs (stream='morning') must be included as prior context."""
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    morning_root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    # Morning run with run_origin='scheduled' and stream='morning' — must be included.
    _write_prior_run(
        morning_root,
        "20260629_0945ET",
        {
            "trading_day": "2026-06-29",
            "generated_at": "2026-06-29T09:45:00-04:00",
            "stream": "morning",
            "run_classification": {
                "run_origin": "scheduled",
                "stream": "morning",
                "run_type": "morning_dca_live",
                "valid_afternoon_evidence": False,
                "manual_run": False,
                "run_id": "morning_live_20260629_0945ET",
            },
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0}
            ],
        },
    )
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), additional_state_roots=[morning_root]
    )
    assert (
        "AAPL" in allocations
    ), "morning scheduled run must appear as prior allocation context"
    assert allocations["AAPL"].amount_usd == 3.0


def test_load_prior_same_day_aggregates_amount_and_takes_most_recent_price(tmp_path):
    # Reference prices only ever come from confirmed orders.csv fills (never
    # target_price/decision_bar_close -- see test_load_prior_same_day_reads_
    # morning_manual_allocation), so both runs here confirm a real fill price.
    # Amounts sum and the ticker's MOST RECENT reference wins (by generated_at),
    # NOT the lowest: the later 10:00 run has the HIGHER price and must win.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 3.0,
                    "target_price": 98.0,
                }
            ],
            generated_at="2026-06-29T09:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "98.0"}],
    )
    _write_prior_run(
        root,
        "manual_runs/1000",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "filled",
                    "planned_amount_usd": 4.5,
                    "target_price": 99.0,
                }
            ],
            generated_at="2026-06-29T10:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].amount_usd == 7.5
    # The afternoon run must beat the ticker's most recent earlier entry price.
    assert allocations["AAPL"].reference_price == 99.0


def test_load_prior_same_day_unknown_reference_clears_reference_timestamp(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [{"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0}],
            generated_at="2026-06-29T09:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "98.0"}],
    )
    _write_prior_run(
        root,
        "manual_runs/1000",
        _manual_run_state(
            [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 4.5}],
            generated_at="2026-06-29T10:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "submitted", "avg_fill_price": ""}],
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 7.5
    assert allocations["AAPL"].reference_price is None
    assert allocations["AAPL"].reference_ts is None


def test_load_prior_same_day_ignores_other_trading_days(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    state = _manual_run_state(
        [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 3.0,
                "target_price": 99.0,
            }
        ]
    )
    state["trading_day"] = "2026-06-26"
    _write_prior_run(root, "manual_runs/0900", state)
    assert load_prior_same_day_allocations(root, date(2026, 6, 29)) == {}


# --- PR #22 bot-review regression coverage (batch: top-up guard) --------------


def test_load_prior_same_day_preserves_amount_only_fill_as_unknown_price(tmp_path):
    # Codex review #88: a CSV-only prior allocation row can have amount_usd but
    # no usable avg_fill_price (e.g. a manual override fill logged without a
    # captured price). The row's amount must still count as prior-allocation
    # capital, with an unknown (None) reference price -- not be dropped entirely.
    root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    run_dir = os.path.join(root, "manual_reruns", "1505")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(
            _manual_run_state(
                [{"ticker": "AMZN", "status": "filled", "planned_amount_usd": 3.0}]
            ),
            handle,
        )
    with open(
        os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write("ticker,amount_usd,avg_fill_price\nAMZN,2.5,\n")

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AMZN"].amount_usd == 2.5
    assert allocations["AMZN"].reference_price is None


def test_load_prior_same_day_quantity_only_fill_without_price_does_not_crash(tmp_path):
    # Codex review #90 (paired with #88): a filled row with a quantity but no
    # usable price and no amount_usd carries no way to derive a dollar amount.
    # It must be dropped safely, never raise (e.g. None * quantity), and never
    # fabricate a reference price.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [{"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0}]
        ),
    )
    orders_csv = os.path.join(root, "manual_runs", "0900", "orders.csv")
    with open(orders_csv, "w", encoding="utf-8") as handle:
        handle.write(
            "ticker,status,avg_fill_price,quantity_filled\nAAPL,filled,,0.05\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # No crash, and no fabricated reference price for the priceless fill row.
    assert allocations["AAPL"].reference_price is None


def test_run_daily_compact_checks_per_order_cap_before_broker_duplicate_lookup(
    tmp_path,
):
    # Codex review #83: the hard per-order cap must be checked before the
    # broker's same-day-duplicate lookup, not after -- an over-cap candidate
    # should never need a same_day_strategy_order_exists() broker round-trip.
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-cap-before-dup")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=config.approved_test_dates,
        base_buy_amount_usd=10.0,
        # The 2% drop below is 1.5x on the current ladder, so a $10 base plans
        # $15; the ceiling sits below that so the candidate is blocked.
        max_per_order_usd=10.0,
    )

    class _TrackingBroker(DryRunBroker):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.duplicate_lookup_calls = []

        def same_day_strategy_order_exists(self, ticker, trading_day):
            self.duplicate_lookup_calls.append(ticker)
            return super().same_day_strategy_order_exists(ticker, trading_day)

    tracking_broker = _TrackingBroker(cash=500.0, reference_prices={"AAPL": 98.0})

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        tracking_broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    candidate = run_state["buy_candidates"][0]
    assert candidate["status"] == "blocked"
    assert candidate["skip_reason"] == "max per-order ceiling exceeded"
    assert tracking_broker.duplicate_lookup_calls == []


def test_load_prior_same_day_final_override_can_see_earlier_scheduled_run(tmp_path):
    # Codex review #96: a final manual buy override invoked after the day's
    # already-completed scheduled afternoon run must still see that run's
    # allocation as prior context, or it risks double-buying the same ticker.
    # Default guard behavior (used by the scheduled run itself) is unchanged --
    # only an explicit opt-in surfaces the earlier scheduled run.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "scheduled_runs/20260629_1520ET",
        _manual_run_state(
            [{"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0}],
            generated_at="2026-06-29T15:20:00-04:00",
            run_origin="scheduled",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )

    # Default behavior is unchanged: the scheduled run is never prior context.
    default_allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert default_allocations == {}

    # A final override running later the same day must see it.
    override_allocations = load_prior_same_day_allocations(
        root,
        date(2026, 6, 29),
        boundary_et=time(15, 35),
        include_scheduled_afternoon=True,
    )
    assert override_allocations["AAPL"].amount_usd == 3.0
    assert override_allocations["AAPL"].reference_price == 99.0


def test_load_prior_same_day_matches_json_escaped_trading_day(tmp_path):
    # Codex review on PR #53: the fast substring pre-check ("if trading_day_iso
    # not in raw_text: continue") must not skip a same-day artifact just because
    # its trading_day string happens to be spelled with JSON unicode escapes
    # (e.g. "2026-06-29") instead of literal hyphens -- json.loads
    # resolves that to the identical date string, but the naive substring check
    # would miss it and silently drop this prior allocation, letting the
    # afternoon top-up guard place a duplicate order on top of a fill it never
    # saw.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    run_dir = os.path.join(root, "manual_runs", "0900")
    os.makedirs(run_dir, exist_ok=True)
    # Both date-bearing fields are escaped so no literal "2026-06-29" substring
    # appears anywhere in the file -- otherwise the (unescaped) generated_at
    # field alone would satisfy the fast-path check and mask the bug.
    raw = (
        '{"trading_day": "2026\\u002d06\\u002d29", '
        '"generated_at": "2026\\u002d06\\u002d29T10:00:00-04:00", '
        '"run_classification": {"run_origin": "manual", "run_id": "manual_am"}, '
        '"buy_candidates": [{"ticker": "AAPL", "status": "submitted", '
        '"planned_amount_usd": 3.0, "target_price": 99.0}]}'
    )
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        handle.write(raw)

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert set(allocations) == {"AAPL"}
    assert allocations["AAPL"].amount_usd == 3.0


def test_load_prior_same_day_skips_non_utf8_artifact_without_crashing(tmp_path):
    # Codex/Gemini review on PR #53: Path.read_bytes() can raise
    # UnicodeDecodeError (a ValueError subclass) on a truncated/corrupt
    # historical artifact. The fast-path refactor narrowed the except clause
    # around the read itself to OSError only, so a single bad file would crash
    # the whole same-day scan (and the scheduled run) instead of being skipped
    # like the pre-refactor code did.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    bad_dir = os.path.join(root, "manual_runs", "0800")
    os.makedirs(bad_dir, exist_ok=True)
    with open(os.path.join(bad_dir, "run_state.json"), "wb") as handle:
        handle.write(b"\xff\xfe not valid utf-8 \x80\x81")

    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {
                    "ticker": "AAPL",
                    "status": "submitted",
                    "planned_amount_usd": 3.0,
                    "target_price": 99.0,
                },
            ]
        ),
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert set(allocations) == {"AAPL"}


# --- Integration: top-up guard through run_daily_compact ----------------------


def test_run_daily_compact_top_up_records_incremental(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    # 100 -> 98 is a 2% drop -> 1.5x -> $3 desired. Morning bought $2 at 99.0; the
    # afternoon quote is 98.0 (< 99.0) so only the $1 difference is bought.
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=99.0,
            run_id="manual_am",
            source="manual",
            selected_base_amount_usd=2.0,
        ),
    }

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    aapl = by_ticker["AAPL"]
    assert aapl["top_up_guard_result"] == TOP_UP_INCREMENTAL
    assert aapl["incremental_order_amount"] == 1.0
    assert aapl["afternoon_desired_amount"] == 3.0
    assert aapl["prior_same_day_amount"] == 2.0
    assert aapl["prior_same_day_reference_price"] == 99.0
    assert aapl["prior_same_day_run_id"] == "manual_am"
    assert aapl["status"] == "dry_run"
    # AAPL's placement used a "-topup-" ref_id, distinct from the plain
    # graded-dca-<day>-<ticker> formula the morning run used for the same
    # ticker/day -- otherwise a broker treating ref_id as an idempotency key
    # could resolve this afternoon placement to the earlier morning order
    # instead of buying the incremental amount.
    assert "graded-dca-topup-2026-06-29-AAPL" in aapl["order_id"]
    # A ticker with no prior allocation still buys its full amount, using the
    # plain (non-topup) ref_id formula.
    assert by_ticker["NVDA"]["top_up_guard_result"] == TOP_UP_NO_PRIOR_RUN
    assert by_ticker["NVDA"]["incremental_order_amount"] == 3.0
    assert "graded-dca-topup" not in by_ticker["NVDA"]["order_id"]
    assert "graded-dca-2026-06-29-NVDA" in by_ticker["NVDA"]["order_id"]


def test_run_daily_compact_fully_offset_still_writes_valid_state(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-offset")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    # Every ticker fully offset: prior price 97.0 is below the 98.0 afternoon quote
    # (price not below prior) so each candidate tops up $0, yet a valid scheduled
    # run_state must still be written.
    prior = {
        t: PriorSameDayAllocation(
            t,
            amount_usd=2.0,
            reference_price=97.0,
            run_id="manual_am",
            source="manual",
            selected_base_amount_usd=2.0,
        )
        for t in config.tickers
    }

    result = run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    assert result["blocked"] is False
    assert os.path.exists(result["run_state_path"])
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    # Prior amounts are seeded into the ledger before the cap check,
    # so day_notional reflects the 3 tickers x $2 prior spend even though the
    # afternoon incremental is $0 for all (price not below prior).
    assert run_state["budget"]["day_notional_usd"] == 6.0
    for candidate in run_state["buy_candidates"]:
        assert candidate["top_up_guard_result"] == TOP_UP_NOT_LOWER_OR_NO_DELTA
        assert candidate["incremental_order_amount"] == 0.0
        assert candidate["top_up_skipped"] is True


def test_run_daily_compact_manual_run_does_not_block_scheduled(tmp_path):
    # With the guard active a same-day broker order must NOT block the scheduled run;
    # a normal (guard-less) run would be blocked by same_day_strategy_order_exists.
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-noblock")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    class _BlockingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

    blocking = _BlockingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=99.0,
            run_id="manual_am",
            source="manual",
            selected_base_amount_usd=2.0,
        ),
    }

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        blocking,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Guard bypassed the same-day broker block and still topped up AAPL, which
    # has a loaded prior allocation.
    assert by_ticker["AAPL"]["status"] == "dry_run"
    assert by_ticker["AAPL"]["incremental_order_amount"] == 1.0
    # NVDA has NO loaded prior allocation, so the broker's same-day-order check
    # must still apply -- the guard bypass is scoped per-ticker, not blanket.
    assert by_ticker["NVDA"]["status"] == "skipped"
    assert by_ticker["NVDA"]["skip_reason"] == "same-day broker order exists"


# --- planned_amount_usd reflects the guarded incremental amount ---------------


def test_build_run_artifacts_uses_guarded_amount_as_planned_notional(tmp_path):
    """build_run_artifacts must set planned_amount_usd = incremental_order_amount
    (not the full afternoon desired amount) when a top-up guard outcome is present.
    The original desired amount must be preserved in afternoon_desired_amount_usd."""
    from near_close_dca import (
        TOP_UP_INCREMENTAL,
        ArtifactRunContext,
        TopUpGuardOutcome,
        build_run_artifacts,
    )

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    decision = _buy_decision("AAPL", suggested_amount_usd=6.0, target_price=98.0)
    # Guard says the incremental order is only $3 (prior $3 already spent today).
    guard = TopUpGuardOutcome(
        ticker="AAPL",
        top_up_guard_result=TOP_UP_INCREMENTAL,
        incremental_order_amount=3.0,
        afternoon_desired_amount=6.0,
        afternoon_decision_price=98.0,
        prior_same_day_amount=3.0,
        prior_same_day_reference_price=99.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        skipped=False,
        manual_review=False,
    )
    artifacts = build_run_artifacts(
        now,
        config,
        state,
        [decision],
        candidate_outcomes={"AAPL": {"order_id": "x", "status": "dry_run"}},
        run_context=ArtifactRunContext(top_up_outcomes={"AAPL": guard}),
    )
    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert (
        candidates["AAPL"]["planned_amount_usd"] == 3.0
    ), "planned_amount_usd must equal incremental_order_amount, not the full desired amount"
    assert (
        candidates["AAPL"]["afternoon_desired_amount_usd"] == 6.0
    ), "original desired amount must be preserved in afternoon_desired_amount_usd"


def test_build_buy_candidates_copies_aggregate_topup_fields_into_every_row(tmp_path):
    # Codex review discussion on commit 58a536a ("Copy aggregate audit fields
    # into artifact rows"): _process_buy_candidate sets aggregate_minimum_
    # topup_* on candidate_outcome, but _build_buy_candidates rebuilds each
    # artifact row from an explicit field list and previously never copied
    # them, so they never reached run_state.json/buy_candidates.csv. Also
    # guards against a write-time crash: _write_csv derives buy_candidates.csv's
    # header from row 0 alone, so if only a LATER row carried these keys
    # (the realistic case -- the aggregate rule applies to at most one
    # ticker in a run with several ordinary candidates), csv.DictWriter would
    # raise on the mismatched columns. The fields must be present, defaulted,
    # on EVERY row.
    from near_close_dca import _build_buy_candidates

    decisions = [
        ReconciledDecision(
            ticker="AAA",
            action=Action.BUY,
            reason="ordinary buy",
            finance_signal=None,
            robinhood_signal=None,
            buy_multiplier=1.0,
            suggested_amount_usd=2.0,
            selected_base_amount_usd=2.0,
            target_price=97.0,
        ),
        ReconciledDecision(
            ticker="ZZZ",
            action=Action.BUY,
            reason="aggregate top-up buy",
            finance_signal=None,
            robinhood_signal=None,
            buy_multiplier=1.0,
            suggested_amount_usd=1.0,
            selected_base_amount_usd=1.0,
            target_price=98.5,
        ),
    ]
    candidate_outcomes = {
        # "AAA" (row 0) carries none of the four aggregate-topup keys --
        # the realistic shape for a ticker the rule never touched.
        "AAA": {"order_id": "AAA-1", "status": "dry_run", "submitted_amount_usd": 2.0},
        "ZZZ": {
            "order_id": "ZZZ-1",
            "status": "dry_run",
            "submitted_amount_usd": 1.0,
            "aggregate_minimum_topup_applied": True,
            "aggregate_minimum_topup_remainder_usd": 0.40,
            "aggregate_minimum_topup_order_usd": 1.0,
            "aggregate_minimum_topup_reason": "aggregate_rounded_up_to_minimum",
        },
    }

    rows = _build_buy_candidates(decisions, candidate_outcomes, top_up_outcomes=None)

    # Must not raise, and both rows must carry every one of the four keys so
    # a downstream _write_csv call sees uniform columns regardless of order.
    csv_path = tmp_path / "buy_candidates.csv"
    _write_csv_for_test = __import__("near_close_dca")._write_csv
    _write_csv_for_test(csv_path, rows)

    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAA"]["aggregate_minimum_topup_applied"] is False
    assert by_ticker["AAA"]["aggregate_minimum_topup_remainder_usd"] is None
    assert by_ticker["ZZZ"]["aggregate_minimum_topup_applied"] is True
    assert by_ticker["ZZZ"]["aggregate_minimum_topup_remainder_usd"] == 0.40
    assert by_ticker["ZZZ"]["aggregate_minimum_topup_order_usd"] == 1.0
    assert by_ticker["ZZZ"]["aggregate_minimum_topup_reason"] == "aggregate_rounded_up_to_minimum"

    with open(csv_path, encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
    assert "aggregate_minimum_topup_applied" in header
    assert "aggregate_minimum_topup_remainder_usd" in header


def test_build_run_artifacts_includes_a_submitted_pending_credit_flush(tmp_path):
    # Codex review ("Include pending-credit flush orders in artifacts"): a
    # ticker appended to buy_candidates solely because it carries a pending
    # same-day credit has a non-BUY decision.action (its own signal recovered
    # or is flat) even when _process_buy_candidate went on to submit a real
    # rounded flush order for it. _build_buy_candidates must not drop that
    # real order just because the underlying decision isn't a BUY, or the
    # run's own audit (buy_candidates.csv, counts, order_submit_status)
    # would show no evidence of an order that was actually placed.
    from near_close_dca import (
        TOP_UP_INCREMENTAL,
        ArtifactRunContext,
        TopUpGuardOutcome,
        build_run_artifacts,
    )

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    # AAPL's own final-session signal recovered above the previous close --
    # NOT a fresh BUY -- but it still carries an earlier pending credit that
    # the final session's flush contract requires closing out.
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.NO_ACTION,
        reason="above prior close and 60-minute trend up",
        finance_signal=None,
        robinhood_signal=None,
    )
    guard = TopUpGuardOutcome(
        ticker="AAPL",
        top_up_guard_result=TOP_UP_INCREMENTAL,
        incremental_order_amount=1.0,
        afternoon_desired_amount=0.0,
        afternoon_decision_price=98.0,
        prior_same_day_amount=0.0,
        prior_same_day_reference_price=None,
        prior_same_day_run_id=None,
        prior_same_day_source=None,
        skipped=False,
        manual_review=False,
    )
    artifacts = build_run_artifacts(
        now,
        config,
        state,
        [decision],
        candidate_outcomes={
            "AAPL": {"order_id": "FLUSH-1", "status": "dry_run", "submitted_amount_usd": 1.0}
        },
        run_context=ArtifactRunContext(top_up_outcomes={"AAPL": guard}),
    )

    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert "AAPL" in candidates, "the submitted flush order is missing from buy_candidates"
    assert candidates["AAPL"]["order_id"] == "FLUSH-1"
    assert candidates["AAPL"]["status"] == "dry_run"
    assert candidates["AAPL"]["planned_amount_usd"] == 1.0
    assert candidates["AAPL"]["is_pending_credit_flush"] is True
    assert artifacts["counts"]["buy_candidates"] == 1


def test_build_run_artifacts_counts_selloff_breadth_from_the_raw_ladder_tier(tmp_path):
    """Regression test for "Preserve the raw ladder tier for selloff-breadth
    counting". The high_multiplier_threshold count that escalates the day's
    budget tier (select_budget_tier) must be judged on each ticker's RAW
    ladder tier match, not on buy_multiplier -- which already has the
    ml-forecaster sizing nudge folded in. Six tickers here each have a raw
    ladder tier of 2.5x (below the 3.0x threshold) that an ML factor of 1.2
    nudged up to a FINAL buy_multiplier of 3.0x (at/above the threshold).
    If the count used buy_multiplier, six such tickers would independently
    escalate the budget tier from "normal" straight to "elevated" purely
    from per-ticker ML forecasts -- not from an actual broad market
    selloff. Using ladder_multiplier instead, the tier must stay "normal".
    """
    from near_close_dca import ArtifactRunContext, build_run_artifacts

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    tickers = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
    config = StrategyConfig(
        dry_run=True,
        tickers=tickers,
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    decisions = [
        ReconciledDecision(
            ticker=ticker,
            action=Action.BUY,
            reason="test buy candidate",
            finance_signal=None,
            robinhood_signal=None,
            buy_multiplier=3.0,  # ML-nudged final multiplier -- AT the threshold
            ladder_multiplier=2.5,  # raw ladder tier -- BELOW the threshold
            suggested_amount_usd=5.0,
            target_price=98.0,
        )
        for ticker in tickers
    ]
    candidate_outcomes = {
        ticker: {"order_id": f"{ticker}-1", "status": "dry_run", "submitted_amount_usd": 5.0}
        for ticker in tickers
    }
    artifacts = build_run_artifacts(
        now, config, state, decisions,
        candidate_outcomes=candidate_outcomes,
        run_context=ArtifactRunContext(),
    )
    assert len(artifacts["buy_candidates"]) == 6
    assert state.selected_tier is not None
    assert state.selected_tier.name == "normal", (
        "6 tickers nudged across the threshold by ML alone must not escalate the budget tier"
    )


def test_build_run_artifacts_still_drops_a_non_buy_decision_with_no_order(tmp_path):
    # The opposite of the flush case: an ordinary non-BUY ticker with no
    # placed order must still be excluded from buy_candidates, unchanged.
    from near_close_dca import ArtifactRunContext, build_run_artifacts

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.NO_ACTION,
        reason="mixed or flat conditions",
        finance_signal=None,
        robinhood_signal=None,
    )
    artifacts = build_run_artifacts(
        now, config, state, [decision],
        candidate_outcomes={},
        run_context=ArtifactRunContext(top_up_outcomes=None),
    )
    assert artifacts["buy_candidates"] == []


def test_build_run_artifacts_keeps_preserved_amount_for_duplicate(tmp_path):
    # Codex review on PR #231 ("Keep the broker notional when rebuilding
    # duplicate artifacts"): a same-stream duplicate-preserved outcome carries
    # the REAL broker order's amount from an earlier attempt this run. If this
    # retry's freshly recomputed guard.incremental_order_amount is smaller
    # (e.g. new prior-allocation context shrank it), planned_amount_usd must
    # still be the real preserved amount, not clamped down to the stale guard
    # figure -- the order actually deployed the larger amount.
    from near_close_dca import (
        TOP_UP_INCREMENTAL,
        ArtifactRunContext,
        TopUpGuardOutcome,
        build_run_artifacts,
    )

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    decision = _buy_decision("AAPL", suggested_amount_usd=6.0, target_price=98.0)
    # This retry's own freshly recomputed guard says only $2 incremental --
    # smaller than the $4 that was actually preserved/submitted.
    guard = TopUpGuardOutcome(
        ticker="AAPL",
        top_up_guard_result=TOP_UP_INCREMENTAL,
        incremental_order_amount=2.0,
        afternoon_desired_amount=6.0,
        afternoon_decision_price=98.0,
        prior_same_day_amount=4.0,
        prior_same_day_reference_price=99.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        skipped=False,
        manual_review=False,
    )
    artifacts = build_run_artifacts(
        now,
        config,
        state,
        [decision],
        candidate_outcomes={
            "AAPL": {
                "order_id": "ORDER-1",
                "status": "submitted",
                "submitted_amount_usd": 4.0,
                "duplicate_preserved": True,
            }
        },
        run_context=ArtifactRunContext(top_up_outcomes={"AAPL": guard}),
    )
    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert candidates["AAPL"]["planned_amount_usd"] == 4.0, (
        "planned_amount_usd must retain the preserved broker amount, not be "
        "clamped to this retry's freshly recomputed incremental_order_amount"
    )


def test_build_run_artifacts_records_guards_effective_base_not_current_run_base(
    tmp_path,
):
    # Codex review ("Persist the preserved base on top-up candidates"): when the
    # guard reused a preserved prior-allocation base, the candidate row must
    # record THAT base, not this run's own freshly recomputed
    # decision.selected_base_amount_usd.
    from near_close_dca import (
        TOP_UP_INCREMENTAL,
        ArtifactRunContext,
        TopUpGuardOutcome,
        build_run_artifacts,
    )

    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
    )
    state = StrategyState()
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test buy candidate",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=4.0,
        suggested_amount_usd=6.0,
        selected_base_amount_usd=2.0,  # this run's OWN freshly recomputed base
        target_price=94.0,
        decision_bar_close=93.0,
    )
    guard = TopUpGuardOutcome(
        ticker="AAPL",
        top_up_guard_result=TOP_UP_INCREMENTAL,
        incremental_order_amount=2.0,
        afternoon_desired_amount=4.0,
        afternoon_decision_price=94.0,
        prior_same_day_amount=2.0,
        prior_same_day_reference_price=98.0,
        prior_same_day_run_id="manual_am",
        prior_same_day_source="manual",
        skipped=False,
        manual_review=False,
        effective_base_amount_usd=1.0,  # the PRESERVED base actually used
    )
    artifacts = build_run_artifacts(
        now,
        config,
        state,
        [decision],
        candidate_outcomes={"AAPL": {"order_id": "x", "status": "dry_run"}},
        run_context=ArtifactRunContext(top_up_outcomes={"AAPL": guard}),
    )
    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert candidates["AAPL"]["selected_base_amount_usd"] == 1.0
    assert candidates["AAPL"]["decision_bar_close"] == 93.0


# --- prior same-day allocations count against the daily/weekly cap -------------


def test_prior_allocations_count_against_cap(tmp_path):
    """When the guard is active, prior same-day amounts must be seeded into the
    budget ledger so that prior + incremental spend together cannot exceed the cap.

    Setup: 1 ticker (AAPL), base=$190, 2% drop -> 2x -> suggested=$380 each,
    which triggers stress tier ($360/day). Prior=$200 at $99; afternoon=$98 (below
    prior -> incremental = $380 - $200 = $180).

    Without prior-allocation seeding: remaining = $360 (no seeding). $180 < $360 -> cap passes (REGRESSION).
    With    prior-allocation seeding: remaining = $360 - $200 = $160. $180 > $160 -> cap blocks (correct).
    """
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        base_buy_amount_usd=190.0,  # 190 * 2x = $380 > $360 stress cap
        max_per_order_usd=500.0,
    )
    state = StrategyState()
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=200.0,
            reference_price=99.0,
            run_id="manual_am",
            source="manual",
            selected_base_amount_usd=190.0,
        )
    }
    broker = DryRunBroker(cash=5000.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = str(tmp_path / "runs" / "2026-06-29-cap")

    import json as _json

    result = run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = _json.load(handle)

    candidates = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # $200 prior seeded -> remaining = $360 - $200 = $160.
    # Guard: $380 desired - $200 prior = $180 incremental.
    # $180 > $160 remaining -> must be blocked.
    # (Without prior-allocation seeding, remaining = $360 and $180 < $360 -> would slip through.)
    aapl = candidates["AAPL"]
    assert aapl["status"] in ("blocked", "skipped"), (
        f"AAPL should be blocked by cap (prior + incremental > cap); "
        f"got status={aapl['status']!r}, skip_reason={aapl.get('skip_reason')!r}"
    )


# --- load_prior_same_day_allocations reads full-report run_state ---------------


def test_load_prior_same_day_ignores_unreadable_or_invalid_json(tmp_path):
    from datetime import date

    root = tmp_path / "state_root"
    root.mkdir()

    # 1. Invalid JSON
    bad_json = root / "run_state_bad.json"
    bad_json.write_text("{this is not json}", encoding="utf-8")

    # 2. Unreadable JSON (OSError path). gemini-code-assist / chatgpt-codex-connector
    # review on PR #58: os.chmod(path, 0) does not reliably trigger an OSError --
    # it's a no-op for the root user (the sandbox/CI containers this suite runs in
    # commonly run as root) and Windows only supports a read-only attribute, not
    # POSIX permission bits. Either environment would let read_bytes() succeed, so a
    # regression that removed the OSError handler would still pass silently.
    # Creating a directory with the matching filename raises IsADirectoryError (an
    # OSError subclass) from read_bytes() deterministically on every platform and
    # user, including root.
    unreadable_json = root / "run_state_unreadable.json"
    unreadable_json.mkdir()

    # Should gracefully ignore both ValueError and OSError and continue
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations == {}


def test_load_prior_same_day_skip_sink_records_scan_failures(tmp_path):
    # Codex review ("Detect artifacts skipped during the initial context
    # scan"): both the OSError (unreadable) and ValueError (invalid JSON)
    # skip paths above silently drop the file and contribute no ticker to the
    # returned mapping -- a caller cannot tell "no prior run" apart from "a
    # same-day artifact exists but this scan couldn't read it" just by
    # inspecting the mapping. skip_sink is the caller's way to see the latter.
    root = tmp_path / "state_root"
    root.mkdir()

    unreadable_json = root / "run_state_unreadable.json"
    unreadable_json.mkdir()
    _stamp_mtime_on_trading_day(unreadable_json, date(2026, 6, 29))

    # Matches the trading-day fast-path substring check (so it isn't filtered
    # out before ever reaching json.loads) but is truncated mid-flush, like a
    # sibling stream's writer caught mid-write.
    partial_json = root / "run_state_partial.json"
    partial_json.write_text(
        '{"trading_day": "2026-06-29", "buy_candidates": [', encoding="utf-8"
    )
    _stamp_mtime_on_trading_day(partial_json, date(2026, 6, 29))

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )

    assert allocations == {}
    assert set(skip_sink) == {unreadable_json, partial_json}


def test_load_prior_same_day_skip_sink_records_truncated_prefix_missing_day(tmp_path):
    # Codex review ("Report truncation before applying the date fast path"):
    # a write_text() writer can expose an empty file, or a prefix that hasn't
    # reached the "trading_day" field yet, while a sibling stream's write is
    # still in flight. Neither ever matches the trading_day_iso substring, so
    # without a completeness check they would be treated as "belongs to
    # another day" by the fast path and skipped BEFORE ever reaching the
    # json.loads() parse-failure handler that records skip_sink.
    root = tmp_path / "state_root"
    root.mkdir()

    empty_json = root / "run_state_empty.json"
    empty_json.write_text("", encoding="utf-8")
    _stamp_mtime_on_trading_day(empty_json, date(2026, 6, 29))

    truncated_prefix_json = root / "run_state_prefix.json"
    truncated_prefix_json.write_text('{"trading', encoding="utf-8")
    _stamp_mtime_on_trading_day(truncated_prefix_json, date(2026, 6, 29))

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )

    assert allocations == {}
    assert set(skip_sink) == {empty_json, truncated_prefix_json}


def test_load_prior_same_day_skip_sink_records_undecodable_bytes(tmp_path):
    # The loader reads bytes and hands them straight to json.loads (see
    # .jules/bolt.md 2026-08-03), so a same-day artifact containing invalid
    # UTF-8 no longer fails at read time the way read_text(encoding="utf-8")
    # made it fail -- it fails at the parse instead, where json.loads raises
    # UnicodeDecodeError (a ValueError subclass). Both handlers record
    # skip_sink, so the observable outcome must be unchanged: a garbled
    # same-day file is still reported as an incomplete scan rather than
    # silently dropped or escaping as an uncaught exception.
    root = tmp_path / "state_root"
    root.mkdir()

    undecodable_json = root / "run_state_undecodable.json"
    undecodable_json.write_bytes(b'{"trading_day": "2026-06-29", "x": "\xff\xfe"}')
    _stamp_mtime_on_trading_day(undecodable_json, date(2026, 6, 29))

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )

    assert allocations == {}
    assert skip_sink == [undecodable_json]


def test_load_prior_same_day_skip_sink_records_nested_brace_truncation(tmp_path):
    # Codex review ("Parse JSON before declaring a brace-terminated prefix
    # complete"): a truncated write can end at the closing brace of a
    # NESTED object (e.g. a "artifacts" sub-dict written before the
    # top-level "trading_day" field, per sorted-key serialization) while the
    # outer object is still open. Such a prefix ends with "}" -- the old
    # "looks complete" heuristic would have wrongly trusted it -- but it is
    # not valid JSON and doesn't yet contain "trading_day" either.
    root = tmp_path / "state_root"
    root.mkdir()

    nested_brace_prefix = root / "run_state_nested.json"
    nested_brace_prefix.write_text(
        '{"artifacts": {"orders_csv_path": "orders.csv"}', encoding="utf-8"
    )
    _stamp_mtime_on_trading_day(nested_brace_prefix, date(2026, 6, 29))

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )

    assert allocations == {}
    assert skip_sink == [nested_brace_prefix]


def test_load_prior_same_day_skip_sink_excludes_old_historical_parse_failure(tmp_path):
    # Codex review ("Exclude historical parse failures from reload
    # completeness"): a permanently corrupted historical artifact from a
    # long-past trading day -- accumulated anywhere under the recursively
    # (rglob) scanned root -- must not poison skip_sink forever. Its own
    # "trading_day" field can't be inspected (that's exactly what failed to
    # parse), so this must be scoped by something else: mtime. A file whose
    # mtime is NOT on the target trading_day is excluded from skip_sink even
    # though it genuinely failed to parse.
    root = tmp_path / "state_root"
    root.mkdir()

    old_corrupt_json = root / "run_state_old_corrupt.json"
    old_corrupt_json.write_text("{this is not valid json at all", encoding="utf-8")
    _stamp_mtime_on_trading_day(old_corrupt_json, date(2025, 1, 15))  # long-past day

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )

    assert allocations == {}
    assert skip_sink == []


def test_mtime_matches_trading_day_is_on_or_after_not_strict_equality(tmp_path):
    """Codex review: "Fail closed on later-modified prior-day artifacts".
    An earlier-weekday artifact (trading_day Monday) being backfilled or
    repaired on a LATER day (mtime Tuesday) is exactly as unverifiable as a
    same-day race, but its mtime can never equal Monday -- a strict
    equality check silently excluded that failure from skip_sink,
    understating real earlier-week spend. A file genuinely older than
    trading_day (real historical garbage) must still be excluded."""
    from near_close_dca import _mtime_matches_trading_day

    monday = date(2026, 6, 29)
    path = tmp_path / "run_state.json"
    path.write_text("{", encoding="utf-8")

    _stamp_mtime_on_trading_day(path, monday)
    assert _mtime_matches_trading_day(path, monday) is True

    # Rewritten/backfilled the NEXT day -- still plausibly relevant to Monday.
    _stamp_mtime_on_trading_day(path, date(2026, 6, 30))
    assert _mtime_matches_trading_day(path, monday) is True

    # Genuinely predates the trading day -- real historical garbage, excluded.
    _stamp_mtime_on_trading_day(path, date(2026, 6, 28))
    assert _mtime_matches_trading_day(path, monday) is False


def test_load_prior_same_day_skip_sink_records_later_day_rewrite_of_earlier_artifact(
    tmp_path,
):
    """End-to-end wiring for the >= fix: an earlier-weekday (Monday) corrupt
    artifact that was rewritten/backfilled on a LATER day (mtime Tuesday)
    must still be recorded in skip_sink when scanned as Monday's context --
    a strict mtime-equality check would have silently dropped it, since a
    file can never have an mtime equal to a day earlier than when it was
    last touched."""
    root = tmp_path / "state_root"
    root.mkdir()

    monday = date(2026, 6, 29)
    tuesday = date(2026, 6, 30)

    rewritten_json = root / "run_state_monday.json"
    rewritten_json.write_text("{this is not valid json at all", encoding="utf-8")
    _stamp_mtime_on_trading_day(rewritten_json, tuesday)

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(root, monday, skip_sink=skip_sink)

    assert allocations == {}
    assert skip_sink == [rewritten_json]


def test_load_prior_same_day_reads_full_report_run_state_via_orders_csv(tmp_path):
    """load_prior_same_day_allocations must handle full-report run_states that store
    'artifacts.orders_csv_path' instead of a top-level 'buy_candidates' list.
    The run must be a manual/non-scheduled run generated before the 2:30 PM ET
    boundary to be eligible as prior allocation context."""
    import csv as _csv
    import json as _json

    root = str(tmp_path / "state_root")
    run_dir = os.path.join(root, "2026-06-29-full-report")
    os.makedirs(run_dir, exist_ok=True)

    # Write an orders.csv with a filled AAPL row.
    orders_csv_path = os.path.join(run_dir, "orders.csv")
    with open(orders_csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(
            fh,
            fieldnames=[
                "ticker",
                "status",
                "avg_fill_price",
                "quantity_filled",
                "order_id",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "AAPL",
                "status": "filled",
                "avg_fill_price": "99.0",
                "quantity_filled": "0.0303",
                "order_id": "ord1",
            }
        )

    # Write a full-report run_state without buy_candidates but with artifacts block.
    # Use run_origin="manual" and a morning time so it passes the boundary/schedule filters.
    run_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T10:00:00-04:00",  # 10 AM ET: before 2:30 PM boundary
        "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
        "artifacts": {
            "orders_csv_path": orders_csv_path,
        },
        # Deliberately omit "buy_candidates" to simulate a full-report run_state.
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as fh:
        _json.dump(run_state, fh)

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert (
        "AAPL" in allocations
    ), "full-report run_state without buy_candidates must produce an AAPL allocation via orders_csv_path"
    alloc = allocations["AAPL"]
    assert alloc.reference_price is not None
    assert alloc.amount_usd is not None
    assert abs(alloc.reference_price - 99.0) < 0.001
    assert abs(alloc.amount_usd - 99.0 * 0.0303) < 0.001


def test_load_prior_same_day_orders_csv_unknown_reference_clears_reference_timestamp(
    tmp_path,
):
    import csv as _csv
    import json as _json

    root = str(tmp_path / "state_root")
    known_run_dir = os.path.join(root, "2026-06-29-known")
    unknown_run_dir = os.path.join(root, "2026-06-29-unknown")
    os.makedirs(known_run_dir, exist_ok=True)
    os.makedirs(unknown_run_dir, exist_ok=True)

    known_orders_csv_path = os.path.join(known_run_dir, "orders.csv")
    with open(known_orders_csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(
            fh, fieldnames=["ticker", "status", "avg_fill_price", "quantity_filled"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "AAPL",
                "status": "filled",
                "avg_fill_price": "99.0",
                "quantity_filled": "0.0303",
            }
        )

    unknown_orders_csv_path = os.path.join(unknown_run_dir, "orders.csv")
    with open(unknown_orders_csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(fh, fieldnames=["ticker", "status", "notional_usd"])
        writer.writeheader()
        writer.writerow(
            {"ticker": "AAPL", "status": "submitted", "notional_usd": "4.5"}
        )

    with open(
        os.path.join(known_run_dir, "run_state.json"), "w", encoding="utf-8"
    ) as fh:
        _json.dump(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T10:00:00-04:00",
                "run_classification": {
                    "run_origin": "manual",
                    "run_id": "manual_known",
                },
                "artifacts": {"orders_csv_path": known_orders_csv_path},
            },
            fh,
        )
    with open(
        os.path.join(unknown_run_dir, "run_state.json"), "w", encoding="utf-8"
    ) as fh:
        _json.dump(
            {
                "trading_day": "2026-06-29",
                "generated_at": "2026-06-29T10:30:00-04:00",
                "run_classification": {
                    "run_origin": "manual",
                    "run_id": "manual_unknown",
                },
                "artifacts": {"orders_csv_path": unknown_orders_csv_path},
            },
            fh,
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 7.5
    assert allocations["AAPL"].reference_price is None
    assert allocations["AAPL"].reference_ts is None


def test_load_prior_same_day_skip_sink_records_missing_referenced_orders_csv(tmp_path):
    # Codex review ("Include referenced CSV failures in scan completeness"):
    # a full-report run_state.json parses fine and RECORDS an
    # artifacts.orders_csv_path, but the CSV file itself is absent (deleted,
    # relocated, or still being written by run_and_report). Before this fix,
    # skip_sink only tracked run_state*.json read/parse failures, so this
    # run_state was reported as scanned cleanly even though its ONLY fill
    # evidence (the referenced CSV) is completely invisible. A ticker whose
    # only same-day fill lives in that missing CSV would then be silently
    # absent from both the reload trigger and the returned allocations.
    import json as _json

    root = str(tmp_path / "state_root")
    run_dir = os.path.join(root, "2026-06-29-full-report")
    os.makedirs(run_dir, exist_ok=True)

    # Deliberately do NOT write orders.csv -- the run_state references it,
    # but it isn't there.
    missing_orders_csv_path = os.path.join(run_dir, "orders.csv")

    run_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T10:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
        "artifacts": {
            "orders_csv_path": missing_orders_csv_path,
        },
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as fh:
        _json.dump(run_state, fh)

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )
    assert "AAPL" not in allocations
    assert skip_sink, (
        "a full-report run_state referencing a missing orders CSV must be "
        "recorded in skip_sink so scan_had_failures reflects the incomplete scan"
    )


def test_load_prior_same_day_skip_sink_ignores_stale_path_with_basename_sibling(
    tmp_path,
):
    # Codex review ("Do not flag stale CSV paths after fallback recovery"):
    # the run_state records a STALE ABSOLUTE artifacts.orders_csv_path (as
    # if from a relocated deployment) that no longer exists, but a
    # basename-matched sibling CSV lives right beside the run_state in its
    # own directory. scripts.run_classification.load_same_day_top_up_context's
    # own basename/glob fallback (_referenced_orders_csvs) already recovers
    # this run's fills via that sibling unconditionally, so flagging a scan
    # failure here would be a permanent false positive -- a stale absolute
    # path never becomes valid, so scan_had_failures would never clear,
    # poisoning an already-correctly-recovered allocation on every future
    # scan.
    import json as _json

    root = str(tmp_path / "state_root")
    run_dir = os.path.join(root, "2026-06-29-full-report")
    os.makedirs(run_dir, exist_ok=True)

    # The basename-matched sibling DOES exist in the run's own directory --
    # only the recorded (stale, absolute, from-elsewhere) path is missing.
    sibling_orders_csv = os.path.join(run_dir, "orders.csv")
    with open(sibling_orders_csv, "w", newline="", encoding="utf-8") as fh:
        fh.write("ticker,status,order_id,avg_fill_price,quantity_filled\n")
        fh.write("AAPL,filled,ord1,99.0,0.0303\n")

    stale_orders_csv_path = "/old/relocated/deployment/orders.csv"

    run_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T10:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
        "artifacts": {
            "orders_csv_path": stale_orders_csv_path,
        },
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as fh:
        _json.dump(run_state, fh)

    skip_sink: list = []
    load_prior_same_day_allocations(root, date(2026, 6, 29), skip_sink=skip_sink)
    assert not skip_sink, (
        "a stale recorded orders CSV path must NOT be flagged as a scan "
        "failure when a basename-matched sibling exists for the manual "
        "top-up context loader's own fallback to recover"
    )


def test_load_prior_same_day_skip_sink_ignores_stale_path_with_glob_sibling(tmp_path):
    # Codex review ("Recognize every supported sibling fallback before
    # flagging failure"): the run_state records a STALE ABSOLUTE
    # artifacts.orders_csv_path whose basename is "orders.csv", but the real
    # sibling CSV in this run's own directory is named "submitted_orders.csv"
    # -- a DIFFERENT basename than the recorded path, so the exact-basename
    # check alone doesn't find it. scripts.run_classification's
    # _referenced_orders_csvs also globs "*orders.csv" in the run directory
    # and WOULD recover this sibling, so flagging a scan failure here is
    # still a permanent false positive -- the exact-basename check covers
    # only one of that helper's two supported fallback mechanisms.
    import json as _json

    root = str(tmp_path / "state_root")
    run_dir = os.path.join(root, "2026-06-29-full-report")
    os.makedirs(run_dir, exist_ok=True)

    # A differently-named sibling that only the *orders.csv glob (not an
    # exact basename match of the recorded path) would find.
    glob_sibling_csv = os.path.join(run_dir, "submitted_orders.csv")
    with open(glob_sibling_csv, "w", newline="", encoding="utf-8") as fh:
        fh.write("ticker,status,order_id,avg_fill_price,quantity_filled\n")
        fh.write("AAPL,filled,ord1,99.0,0.0303\n")

    stale_orders_csv_path = "/old/relocated/deployment/orders.csv"

    run_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T10:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
        "artifacts": {
            "orders_csv_path": stale_orders_csv_path,
        },
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as fh:
        _json.dump(run_state, fh)

    skip_sink: list = []
    load_prior_same_day_allocations(root, date(2026, 6, 29), skip_sink=skip_sink)
    assert not skip_sink, (
        "a stale recorded orders CSV path must NOT be flagged as a scan "
        "failure when a *orders.csv glob sibling exists, even if its "
        "basename doesn't match the recorded path"
    )


def test_load_prior_same_day_skip_sink_records_truncated_orders_csv_row(tmp_path):
    # Codex review ("Detect silently truncated CSV reads"): the orders CSV
    # EXISTS (unlike the test above) but was observed mid-rewrite -- its
    # final row is cut off with only "AAPL,filled" written, missing the
    # order_id/avg_fill_price/quantity_filled columns entirely.
    # csv.DictReader does NOT raise for a short row (it fills the missing
    # trailing columns with None via its `restval`), so the earlier
    # except-clause skip_sink fix never fires here -- the row simply looks
    # like "no usable price or amount" and gets silently dropped. If this is
    # the ticker's ONLY same-day fill, the scan reports itself complete
    # while missing it entirely.
    import json as _json

    root = str(tmp_path / "state_root")
    run_dir = os.path.join(root, "2026-06-29-full-report")
    os.makedirs(run_dir, exist_ok=True)

    orders_csv_path = os.path.join(run_dir, "orders.csv")
    with open(orders_csv_path, "w", newline="", encoding="utf-8") as fh:
        fh.write("ticker,status,order_id,avg_fill_price,quantity_filled\n")
        fh.write(
            "AAPL,filled"
        )  # truncated mid-write -- no trailing columns, no newline

    run_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T10:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
        "artifacts": {
            "orders_csv_path": orders_csv_path,
        },
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as fh:
        _json.dump(run_state, fh)

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )
    assert "AAPL" not in allocations
    assert skip_sink, (
        "a truncated final row in a referenced orders CSV must be recorded "
        "in skip_sink so scan_had_failures reflects the incomplete scan"
    )


def test_load_prior_same_day_csv_only_counts_every_order_row_for_one_ticker(tmp_path):
    """Codex review ("Count each CSV order attempt before aggregating by
    ticker"): the no-embedded-candidates CSV-only reconstruction path (e.g. a
    market-open/intraday run with no in-repo executor) must count ONE
    attempt per qualifying CSV row, not one per ticker after
    _orders_csv_fills/_orders_csv_submitted_notionals collapse multiple
    distinct orders for the same ticker into a single aggregated record.
    Two real orders for AAAU this run -> 2 attempts, even though only the
    filled one contributes a dollar amount."""
    root = os.path.join(tmp_path, "cron_tracking", "market_open_dca")
    run_dir = os.path.join(root, "20260629_0935ET")
    os.makedirs(run_dir, exist_ok=True)

    state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T09:35:00-04:00",
        "run_classification": {
            "run_origin": "scheduled",
            "run_type": "market_open_dca_live",
            "stream": "market_open",
            "manual_run": False,
            "trading_day": "2026-06-29",
            "run_id": "market_open_live_20260629_0935ET",
        },
        "buy_candidates": [],
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    with open(
        os.path.join(run_dir, "submitted_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,status,submitted_amount_usd,avg_fill_price,quantity_filled\n"
            "AAAU,ORD-1,filled,2.00,50.00,0.04\n"
            "AAAU,ORD-2,canceled,0,,\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAAU"].amount_usd == pytest.approx(2.00)
    assert allocations["AAAU"].attempt_count == 2


def test_load_prior_same_day_csv_only_counts_a_terminal_rejected_only_ticker(tmp_path):
    """A ticker whose only CSV row this run is a terminal rejection (no fill,
    no submitted notional -- never enters _csv_tickers at all) must still be
    counted as an attempt with $0, so the shared max_orders_per_day slot it
    consumed isn't invisible to the next run's guard."""
    root = os.path.join(tmp_path, "cron_tracking", "market_open_dca")
    run_dir = os.path.join(root, "20260629_0935ET")
    os.makedirs(run_dir, exist_ok=True)

    state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T09:35:00-04:00",
        "run_classification": {
            "run_origin": "scheduled",
            "run_type": "market_open_dca_live",
            "stream": "market_open",
            "manual_run": False,
            "trading_day": "2026-06-29",
            "run_id": "market_open_live_20260629_0935ET",
        },
        "buy_candidates": [],
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    with open(
        os.path.join(run_dir, "submitted_orders.csv"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            "ticker,order_id,status,submitted_amount_usd,avg_fill_price,quantity_filled\n"
            "MSFT,ORD-9,rejected,0,,\n"
        )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["MSFT"].amount_usd == 0.0
    assert allocations["MSFT"].attempt_count == 1


def test_load_prior_same_day_skip_sink_records_empty_orders_csv_snapshot(tmp_path):
    # Codex review ("Treat empty CSV snapshots as incomplete"): the orders
    # CSV was observed in the instant right after write_execution_csv()'s
    # truncating open(path, "w") but before even the header row was
    # flushed -- a completely empty (0-byte) file. The prior fix's
    # `if rows` check is bypassed entirely here (rows == [] with NO
    # fieldnames at all), so it's indistinguishable from a legitimately
    # empty (header-only, zero-orders) day unless fieldnames itself is
    # inspected: write_execution_csv() unconditionally calls
    # writer.writeheader(), so a COMPLETE write always has a header even
    # with zero data rows -- fieldnames is None ONLY when the file was
    # observed before that header was ever written.
    import json as _json

    root = str(tmp_path / "state_root")
    run_dir = os.path.join(root, "2026-06-29-full-report")
    os.makedirs(run_dir, exist_ok=True)

    orders_csv_path = os.path.join(run_dir, "orders.csv")
    with open(orders_csv_path, "w", newline="", encoding="utf-8"):
        pass  # completely empty -- truncated but nothing flushed yet

    run_state = {
        "trading_day": "2026-06-29",
        "generated_at": "2026-06-29T10:00:00-04:00",
        "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
        "artifacts": {
            "orders_csv_path": orders_csv_path,
        },
    }
    with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as fh:
        _json.dump(run_state, fh)

    skip_sink: list = []
    allocations = load_prior_same_day_allocations(
        root, date(2026, 6, 29), skip_sink=skip_sink
    )
    assert "AAPL" not in allocations
    assert skip_sink, (
        "a completely empty referenced orders CSV (no header at all) must be "
        "recorded in skip_sink so scan_had_failures reflects the incomplete scan"
    )


def _run_all():
    import inspect
    from pathlib import Path

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                # tmp_path-style tests get a throwaway directory outside pytest.
                with tempfile.TemporaryDirectory() as tmp:
                    fn(Path(tmp))
            else:
                fn()
            print(f"PASS {name}")
    print("All self-tests passed.")


def test_prior_run_before_boundary_malformed_iso_format():
    from near_close_dca import _prior_run_before_boundary

    run_state = {"generated_at": "invalid-date-format"}
    boundary_et = time(14, 30)
    assert _prior_run_before_boundary(run_state, boundary_et) is True


def test_iso_or_none_handles_datetime_and_none():
    from datetime import timezone

    # Verify None is passed through
    assert _iso_or_none(None) is None

    # Verify UTC timezone-aware datetime works
    dt_utc = datetime(2023, 10, 27, 14, 30, 0, tzinfo=timezone.utc)
    assert _iso_or_none(dt_utc) == "2023-10-27T14:30:00+00:00"

    # Verify Eastern timezone-aware datetime works (EDT offset is -04:00)
    dt_eastern = datetime(2023, 10, 27, 14, 30, 0, tzinfo=EASTERN)
    assert _iso_or_none(dt_eastern) == "2023-10-27T14:30:00-04:00"

    # Verify naive datetime works
    dt_naive = datetime(2023, 10, 27, 14, 30, 0)
    assert _iso_or_none(dt_naive) == "2023-10-27T14:30:00"


def test_iso_or_none():
    from near_close_dca import _iso_or_none

    assert _iso_or_none(None) is None

    dt = datetime(2023, 1, 1, 12, 0, tzinfo=EASTERN)
    assert _iso_or_none(dt) == "2023-01-01T12:00:00-05:00"


def test_iso_week_key():
    from near_close_dca import iso_week_key

    # Regular mid-week date
    assert iso_week_key(date(2023, 10, 18)) == (2023, 42)  # Wednesday

    # Year boundaries
    assert iso_week_key(date(2023, 12, 31)) == (
        2023,
        52,
    )  # Sunday, last day of 2023 ISO week 52
    assert iso_week_key(date(2024, 1, 1)) == (
        2024,
        1,
    )  # Monday, first day of 2024 ISO week 1

    # Leap year (2024)
    assert iso_week_key(date(2024, 2, 29)) == (2024, 9)  # Thursday, Leap day

    # A year where Jan 1st is in the previous year's week 52/53
    # Jan 1, 2023 was a Sunday. ISO weeks start on Monday.
    # Therefore, Jan 1 2023 belongs to the last ISO week of 2022.
    assert iso_week_key(date(2023, 1, 1)) == (2022, 52)
    assert iso_week_key(date(2023, 1, 2)) == (2023, 1)  # Monday, starts week 1 of 2023

    # 53-week year boundary (e.g., 2020)
    assert iso_week_key(date(2020, 12, 31)) == (2020, 53)  # Thursday, week 53
    assert iso_week_key(date(2021, 1, 3)) == (2020, 53)  # Sunday, last day of week 53
    assert iso_week_key(date(2021, 1, 4)) == (2021, 1)  # Monday, starts week 1 of 2021


def test_approved_window_status_counts_remaining_days():
    from near_close_dca import approved_window_status

    approved = frozenset({date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)})
    in_window, window_end, remaining = approved_window_status(
        approved, date(2026, 8, 25)
    )
    assert in_window is True
    assert window_end == date(2026, 8, 26)
    assert remaining == 2  # the 25th and 26th

    in_window, window_end, remaining = approved_window_status(
        approved, date(2026, 9, 1)
    )
    assert in_window is False
    assert remaining == 0

    assert approved_window_status(frozenset(), date(2026, 8, 25)) == (False, None, 0)


def test_warn_if_window_expiring_warns_near_end(caplog):
    from near_close_dca import warn_if_window_expiring

    approved = frozenset({date(2026, 8, 27), date(2026, 8, 28)})
    with caplog.at_level("WARNING", logger="near_close_dca"):
        warn_if_window_expiring(approved, date(2026, 8, 27))
    assert any(
        "approved trading window ends 2026-08-28" in record.getMessage()
        for record in caplog.records
    )


def test_warn_if_window_expiring_errors_after_end(caplog):
    from near_close_dca import warn_if_window_expiring

    approved = frozenset({date(2026, 8, 28)})
    with caplog.at_level("ERROR", logger="near_close_dca"):
        warn_if_window_expiring(approved, date(2026, 8, 31))
    assert any(
        "approved trading window ended 2026-08-28" in record.getMessage()
        for record in caplog.records
    )


def test_warn_if_window_expiring_quiet_far_from_end(caplog):
    from near_close_dca import _weekday_range, warn_if_window_expiring

    approved = frozenset(_weekday_range(date(2026, 7, 1), date(2026, 12, 31)))
    with caplog.at_level("WARNING", logger="near_close_dca"):
        warn_if_window_expiring(approved, date(2026, 7, 23))
    assert caplog.records == []


def test_execution_gate_blocked_after_window_end_logs_error(tmp_path, caplog):
    from near_close_dca import _check_execution_gates

    config = StrategyConfig(approved_test_dates=frozenset({date(2026, 8, 28)}))
    with caplog.at_level("ERROR", logger="near_close_dca"):
        allowed = _check_execution_gates(
            date(2026, 8, 31), config, DryRunBroker(), str(tmp_path)
        )
    assert allowed is False
    assert any("hard-stopped" in record.getMessage() for record in caplog.records)
    # The blocked stub artifact is still written as before.
    assert (tmp_path / "run_state.json").exists()


def test_execution_gate_blocks_nyse_holiday_even_if_config_approves_it(
    tmp_path, caplog
):
    """A config-driven window (e.g. an extended trading_window.json) can widen
    approved_test_dates to any weekday range with no holiday subtraction of its
    own; the gate must still block full NYSE closures like Labor Day and
    Thanksgiving via the independently computed calendar."""
    from near_close_dca import _check_execution_gates

    for holiday in (date(2026, 9, 7), date(2026, 11, 26)):  # Labor Day, Thanksgiving
        config = StrategyConfig(approved_test_dates=frozenset({holiday}))
        with caplog.at_level("WARNING", logger="near_close_dca"):
            allowed = _check_execution_gates(
                holiday, config, DryRunBroker(), str(tmp_path / str(holiday))
            )
        assert allowed is False
        assert any(
            "NYSE holiday gate blocked execution" in record.getMessage()
            for record in caplog.records
        )
        caplog.clear()


def test_execution_gate_allows_non_holiday_weekday_in_approved_window(tmp_path):
    from near_close_dca import _check_execution_gates

    trading_day = date(2026, 9, 8)  # Tuesday after Labor Day; not a holiday.
    config = StrategyConfig(approved_test_dates=frozenset({trading_day}))
    allowed = _check_execution_gates(trading_day, config, DryRunBroker(), str(tmp_path))
    assert allowed is True


def test_orders_csv_fills_logs_and_keeps_partial_rows_on_corrupt_csv(tmp_path, caplog):
    """A corrupt orders.csv must be loud (logged) and keep rows parsed before the failure."""
    from near_close_dca import _orders_csv_fills

    orders_csv = tmp_path / "orders.csv"
    good = (
        "ticker,status,avg_fill_price,quantity_filled,order_id\n"
        "AAPL,filled,100.0,0.02,oid-1\n"
    )
    # An oversized field raises csv.Error (field larger than field limit) on the
    # row AFTER the valid AAPL row, so parsing fails mid-iteration.
    oversized = "x" * (csv.field_size_limit() + 1)
    orders_csv.write_text(
        good + f"MSFT,filled,{oversized},0.01,oid-2\n", encoding="utf-8"
    )

    with caplog.at_level("ERROR", logger="near_close_dca"):
        fills = _orders_csv_fills(tmp_path)

    assert "AAPL" in fills
    assert fills["AAPL"]["order_id"] == "oid-1"
    assert any("failed to parse" in record.message for record in caplog.records)


def test_orders_csv_fills_logs_on_undecodable_csv(tmp_path, caplog):
    from near_close_dca import _orders_csv_fills

    orders_csv = tmp_path / "orders.csv"
    orders_csv.write_bytes(b"\xff\xfe not utf-8 \xff")

    with caplog.at_level("ERROR", logger="near_close_dca"):
        fills = _orders_csv_fills(tmp_path)

    assert fills == {}
    assert any("failed to parse" in record.message for record in caplog.records)


def test_orders_csv_submitted_notionals_logs_on_corrupt_csv(tmp_path, caplog):
    from near_close_dca import _orders_csv_submitted_notionals

    orders_csv = tmp_path / "orders.csv"
    orders_csv.write_bytes(b"\xff\xfe not utf-8 \xff")

    with caplog.at_level("ERROR", logger="near_close_dca"):
        notionals = _orders_csv_submitted_notionals(orders_csv)

    assert notionals == {}
    assert any("failed to parse" in record.message for record in caplog.records)


def test_blocked_stub_refreshes_the_latest_scheduled_pointer(tmp_path):
    """The calendar gate returns before the normal finalizer, so it must run the
    validation and pointer refresh itself: a pointer-based watchdog has to see
    today's slot resolved as blocked rather than still targeting the previous
    trading day (Codex review)."""
    from near_close_dca import _write_blocked_run_stub
    from scripts.run_classification import latest_scheduled_run_path

    artifacts_dir = pathlib.Path(tmp_path) / "runs" / "2026-07-01"
    state_root = pathlib.Path(tmp_path) / "classification"

    _write_blocked_run_stub(date(2026, 7, 1), artifacts_dir, state_root)

    pointer = json.loads(
        latest_scheduled_run_path(state_root).read_text(encoding="utf-8")
    )
    assert pointer["trading_day"] == "2026-07-01"
    assert pointer["run_state_path"] == str(artifacts_dir / "run_state.json")
    # A market-closed day proves the slot fired but is never scheduled evidence.
    assert pointer["valid_scheduled_evidence"] is False
    assert pointer["pointer_status"] == "pending"


def test_blocked_stub_withholds_the_pointer_when_the_contract_fails(tmp_path, caplog):
    """Failing closed means the directory is not advertised. The preserved
    run_state is never rewritten -- including a legacy one this helper leaves
    byte for byte -- so the pointer is what gates on validation."""
    from near_close_dca import _write_blocked_run_stub
    from scripts.run_classification import latest_scheduled_run_path

    artifacts_dir = pathlib.Path(tmp_path) / "runs" / "corrupt"
    artifacts_dir.mkdir(parents=True)
    legacy = json.dumps({"trading_day": "2026-07-01", "status": "blocked"})
    (artifacts_dir / "run_state.json").write_text(legacy, encoding="utf-8")
    state_root = pathlib.Path(tmp_path) / "classification"

    with caplog.at_level("ERROR", logger="near_close_dca"):
        _write_blocked_run_stub(date(2026, 7, 1), artifacts_dir, state_root)

    # No run_classification on the preserved legacy state, so the contract fails.
    assert not latest_scheduled_run_path(state_root).exists()
    assert (artifacts_dir / "run_state.json").read_text(encoding="utf-8") == legacy
    assert any(
        "not refreshing the latest scheduled pointer" in r.message
        for r in caplog.records
    )


def test_placement_failure_outranks_a_confirmation_hold(tmp_path):
    """A zero-submission batch where one order awaits confirmation and another
    placement RAISED is a placement failure, not an ordinary confirmation block:
    is_confirmation_blocked() must not claim the run (Codex review)."""
    from near_close_dca import (
        _apply_no_order_classification,
        _derive_order_submit_status,
    )
    from scripts.run_classification import is_confirmation_blocked

    config = StrategyConfig(dry_run=False, tickers=("AAPL", "MSFT"))
    buy_candidates = [
        {"ticker": "AAPL", "status": "not_submitted_confirmation_required"},
        {"ticker": "MSFT", "status": "failed"},
    ]

    assert (
        _derive_order_submit_status(config, buy_candidates, 0) == "not_submitted_error"
    )

    artifacts = {
        "buy_candidates": buy_candidates,
        "order_submit_status": _derive_order_submit_status(config, buy_candidates, 0),
    }
    outcomes = {
        "AAPL": {"status": "not_submitted_confirmation_required"},
        "MSFT": {"status": "failed"},
    }
    _apply_no_order_classification(artifacts, outcomes)

    assert artifacts["order_submit_status"] == "not_submitted_error"
    assert artifacts["is_trading_failure"] is True
    assert is_confirmation_blocked(artifacts) is False


def test_confirmation_hold_survives_when_no_placement_failed(tmp_path):
    """The failure precedence must not swallow a genuine confirmation block."""
    from near_close_dca import (
        _apply_no_order_classification,
        _derive_order_submit_status,
    )

    config = StrategyConfig(dry_run=False, tickers=("AAPL",))
    buy_candidates = [
        {"ticker": "AAPL", "status": "not_submitted_confirmation_required"}
    ]

    assert (
        _derive_order_submit_status(config, buy_candidates, 0)
        == "not_submitted_confirmation_required"
    )

    artifacts = {
        "buy_candidates": buy_candidates,
        "order_submit_status": "not_submitted_confirmation_required",
    }
    _apply_no_order_classification(
        artifacts, {"AAPL": {"status": "not_submitted_confirmation_required"}}
    )

    assert artifacts["order_submit_status"] == "not_submitted_confirmation_required"


if __name__ == "__main__":
    _run_all()


def test_a_failure_synthesized_candidate_row_satisfies_the_schema():
    """schemas/run_state.schema.json requires ticker, buy_multiplier,
    planned_amount_usd, and status on every candidate row. validate_run_artifacts
    does not run schema validation, so an omission produced an artifact that read
    as a complete failure record while schema-based reporting rejected it (Codex
    review). One builder serves both failure paths so a field cannot land on one
    and miss the other."""
    import json as _json
    from pathlib import Path as _Path

    from near_close_dca import _failure_candidate_row

    row = _failure_candidate_row(
        "AAA",
        {
            "status": "submitted",
            "order_id": "o-1",
            "submitted_amount_usd": 2.0,
            "buy_multiplier": 1.5,
            "skip_reason": None,
        },
    )

    assert row["buy_multiplier"] == 1.5
    schema = _json.loads(
        (_Path(__file__).parent.parent / "schemas" / "run_state.schema.json").read_text(
            encoding="utf-8"
        )
    )
    item_schema = schema["properties"]["buy_candidates"]["items"]
    # The required-field check is the defect this test was written for, so it
    # runs before the full schema pass below.
    for field in item_schema["required"]:
        assert field in row, field
    # Full schema validation, matching how the other schema tests in this repo
    # are gated (importorskip: a real SKIP, never a false PASS).
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(row, item_schema)


def test_a_failure_synthesized_candidate_row_preserves_whole_share_fields():
    """Codex review ("Preserve whole-share fields in failure candidate
    rows"): when an accepted whole-share order is followed by an exception
    while a LATER candidate is processed, the failure writer rebuilds
    run_state.json's canonical buy_candidates through _failure_candidate_row,
    which must carry the whole_share_pending_usd/whole_share_contribution_usd/
    whole_share_quantity fields _build_buy_candidates's own success path sets
    -- otherwise downstream reporting silently loses the whole-share
    settlement details specifically on failed-after-submission runs.
    """
    from near_close_dca import _failure_candidate_row

    row = _failure_candidate_row(
        "BYDDY",
        {
            "status": "submitted",
            "order_id": "o-1",
            "submitted_amount_usd": 10.0,
            "buy_multiplier": 1.0,
            "skip_reason": None,
            "whole_share_pending_usd": 5.0,
            "whole_share_contribution_usd": 15.0,
            "whole_share_quantity": 1,
            "whole_share_settlement_reconciliation_needed": True,
            "whole_share_settlement_shortfall_usd": 8.0,
        },
    )

    assert row["whole_share_pending_usd"] == 5.0
    assert row["whole_share_contribution_usd"] == 15.0
    assert row["whole_share_quantity"] == 1
    # Codex review ("Preserve clamped shortfalls when settlement fails"):
    # the failure-path row builder must carry this pair through too.
    assert row["whole_share_settlement_reconciliation_needed"] is True
    assert row["whole_share_settlement_shortfall_usd"] == 8.0


def test_a_failure_synthesized_candidate_row_preserves_claim_metadata():
    """Codex review ("Preserve claim metadata in failure candidate rows"):
    this failure-path rebuild is a SEPARATE artifact-construction path from
    _build_buy_candidates's normal one. If a cross-day fill was recovered
    and published (carrying whole_share_claim_breakdown/
    whole_share_order_ids) and a LATER candidate then raises, the failure
    writer rebuilds run_state.json's buy_candidates through
    _failure_candidate_row -- which must carry those two fields too, or
    dca_risk.deployed_usd_by_claim_date() attributes the recovered amount
    to THIS run's own trading_day (extending its kill-switch window) and
    the additional broker order id(s) are lost from the artifact entirely.
    """
    from near_close_dca import _failure_candidate_row

    row = _failure_candidate_row(
        "BYDDY",
        {
            "status": "submitted",
            "order_id": "o-2",
            "submitted_amount_usd": 20.0,
            "buy_multiplier": 1.0,
            "skip_reason": None,
            "whole_share_order_ids": ["o-1", "o-2"],
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-01", "amount_usd": 10.0}],
        },
    )

    assert row["whole_share_order_ids"] == ["o-1", "o-2"]
    assert row["whole_share_claim_breakdown"] == [{"trading_day": "2026-08-01", "amount_usd": 10.0}]


def test_build_buy_candidates_emits_a_row_for_a_whole_share_recovery_with_no_decision():
    """Codex review ("Persist recovered outcomes for orphaned whole-share
    tickers"): the orphaned-ledger discovery pass (see
    WholeShareAccumulationLedger.discover_symbols()) can reconcile a
    ticker no longer in config.whole_share_tickers, which therefore never
    gets evaluated into `decisions` at all. Without an explicit fallback,
    such an outcome is invisible to BOTH the main decision-keyed loop
    (nothing in `decisions` to key off) and the inverse-Martingale
    recovery loop (a different marker) -- the reconciled fill would never
    reach buy_candidates.csv/run_state.json, and because the ledger claim
    is now resolved, no LATER run could ever rediscover the spend either.
    """
    from near_close_dca import _build_buy_candidates

    decisions = []  # BYDDY has no decision -- it isn't enrolled anywhere.
    candidate_outcomes = {
        "BYDDY": {
            "status": "submitted",
            "order_id": "RH-ORPHANED-RECOVERY-1",
            "submitted_amount_usd": 10.0,
            "whole_share_quantity": 1,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        },
    }

    rows = _build_buy_candidates(decisions, candidate_outcomes, top_up_outcomes=None)

    assert len(rows) == 1
    assert rows[0]["ticker"] == "BYDDY"
    assert rows[0]["reason"] == "recovered_whole_share_claim"
    assert rows[0]["status"] == "submitted"
    assert rows[0]["order_id"] == "RH-ORPHANED-RECOVERY-1"
    assert rows[0]["planned_amount_usd"] == pytest.approx(10.0)
    assert rows[0]["whole_share_claim_breakdown"] == [{"trading_day": "2026-08-15", "amount_usd": 10.0}]


def test_build_buy_candidates_emits_a_row_for_an_orphaned_zero_fill_recovery():
    """Codex review ("Persist orphaned zero-fill recoveries"): a confirmed
    zero-fill recovery for a ticker removed from config.whole_share_tickers
    (see _book_reconciled_whole_share_order's own zero_fill_attempt
    branch) publishes "status": "failed" with a whole_share_
    zero_fill_attempt marker instead of "submitted" -- nothing was
    purchased, but it's still a REAL broker attempt, already counted via
    ctx.state.record_buy(..., 0.0). The orphaned-ticker fallback used to
    require "submitted" plus one of three submission-shaped fields, so
    this outcome fell through both checks and was never persisted -- the
    ledger claim was already released, so no LATER run could rediscover
    the attempt either, permanently undercounting max_orders_per_day.
    """
    from near_close_dca import _build_buy_candidates

    decisions = []  # BYDDY has no decision -- it isn't enrolled anywhere.
    candidate_outcomes = {
        "BYDDY": {
            "status": "failed",
            "order_id": "RH-ORPHANED-ZERO-FILL-1",
            "skip_reason": "whole-share: confirmed zero-fill terminal order reconciled cross-day (trading_day=2026-08-15)",
            "whole_share_zero_fill_attempt": True,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 0.0}],
        },
    }

    rows = _build_buy_candidates(decisions, candidate_outcomes, top_up_outcomes=None)

    assert len(rows) == 1
    assert rows[0]["ticker"] == "BYDDY"
    assert rows[0]["reason"] == "recovered_whole_share_claim"
    assert rows[0]["status"] == "failed"
    assert rows[0]["order_id"] == "RH-ORPHANED-ZERO-FILL-1"
    assert rows[0]["planned_amount_usd"] == pytest.approx(0.0)
    assert rows[0]["whole_share_claim_breakdown"] == [{"trading_day": "2026-08-15", "amount_usd": 0.0}]


def test_build_buy_candidates_keeps_recovery_only_rows_on_one_schema(tmp_path):
    """Codex review ("Keep recovery-only CSV rows on one schema"): when a
    run has NO decisions at all but recovers both an inverse-Martingale
    lease AND an orphaned whole-share claim, the inverse recovery row
    (dict-iteration order can put it first) previously carried no
    whole_share_* keys at all, while the whole-share recovery row carried
    eight extra ones. _write_csv derives fieldnames solely from row 0 and
    uses the default extrasaction="raise", so writing buy_candidates.csv
    would raise ValueError the moment the OTHER row's extra keys showed
    up. Both recovery-only row shapes must carry the identical column set.
    """
    from near_close_dca import _build_buy_candidates, _write_csv

    decisions = []  # Neither ticker has a decision -- both are recoveries only.
    candidate_outcomes = {
        "AAPL#imart-recovery": {
            "status": "submitted",
            "order_id": "RH-IMART-1",
            "submitted_amount_usd": 5.0,
            "recovered_inverse_martingale_lease": True,
            "ticker": "AAPL",
        },
        "BYDDY": {
            "status": "submitted",
            "order_id": "RH-ORPHANED-RECOVERY-2",
            "submitted_amount_usd": 10.0,
            "whole_share_quantity": 1,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        },
    }

    rows = _build_buy_candidates(decisions, candidate_outcomes, top_up_outcomes=None)

    assert len(rows) == 2
    assert set(rows[0].keys()) == set(rows[1].keys())
    # Must not raise -- the real assertion this test exists for.
    _write_csv(tmp_path / "buy_candidates.csv", rows)


def test_a_failure_candidate_row_prefers_the_outcomes_own_ticker():
    """Regression test for "Preserve real tickers in recovery failure
    artifacts". A recovered inverse-Martingale lease is published into
    candidate_outcomes under a synthetic "<ticker>#imart-recovery" key (see
    the lease-recovery publication block in _run_once_impl) so it cannot
    collide with an ordinary same-run candidate for the same ticker, and
    carries its real ticker INSIDE the outcome instead. If a later step
    raises before the run finishes, the failure writer reads the same
    in-flight dict and must not write that synthetic key into buy_candidates
    as though it were a real symbol.
    """
    from near_close_dca import _failure_candidate_row

    row = _failure_candidate_row(
        "AAPL#imart-recovery",
        {
            "status": "submitted",
            "order_id": "o-1",
            "submitted_amount_usd": 5.0,
            "recovered_inverse_martingale_lease": True,
            "ticker": "AAPL",
        },
    )

    assert row["ticker"] == "AAPL"

    # An ordinary outcome carries no "ticker" field of its own -- the dict
    # key IS the real ticker -- so the fallback must still return it.
    ordinary_row = _failure_candidate_row("MSFT", {"status": "submitted", "order_id": "o-2"})
    assert ordinary_row["ticker"] == "MSFT"


def test_failure_candidate_row_carries_the_aggregate_topup_audit_fields():
    # Codex review discussion on commit 6e896c5 ("Propagate aggregate fields
    # into failure candidate rows"): _write_afternoon_failure_artifacts
    # reconstructs run_state.json entries through this same builder when a
    # later operation raises after a real order (potentially an aggregate
    # minimum top-up) was already submitted. Before this fix, all four
    # aggregate_minimum_topup_* fields were dropped from a failure-path row
    # even though the submitted order actually applied the rule.
    from near_close_dca import _failure_candidate_row

    row = _failure_candidate_row(
        "AAPL",
        {
            "status": "submitted",
            "order_id": "o-1",
            "submitted_amount_usd": 5.50,
            "buy_multiplier": 2.0,
            "aggregate_minimum_topup_applied": True,
            "aggregate_minimum_topup_remainder_usd": 0.50,
            "aggregate_minimum_topup_order_usd": 5.50,
            "aggregate_minimum_topup_reason": "aggregate_at_or_above_minimum",
        },
    )

    assert row["aggregate_minimum_topup_applied"] is True
    assert row["aggregate_minimum_topup_remainder_usd"] == 0.50
    assert row["aggregate_minimum_topup_order_usd"] == 5.50
    assert row["aggregate_minimum_topup_reason"] == "aggregate_at_or_above_minimum"

    # An ordinary outcome with no aggregate fields at all still gets the
    # unconditionally-defaulted keys -- same _write_csv row-0-header
    # crash-prevention rationale as _build_buy_candidates's success rows.
    ordinary_row = _failure_candidate_row("MSFT", {"status": "submitted", "order_id": "o-2"})
    assert ordinary_row["aggregate_minimum_topup_applied"] is False
    assert ordinary_row["aggregate_minimum_topup_remainder_usd"] is None
    assert ordinary_row["aggregate_minimum_topup_order_usd"] is None
    assert ordinary_row["aggregate_minimum_topup_reason"] is None


def test_a_placement_outcome_carries_the_multiplier_for_the_failure_path():
    """The failure handlers synthesize candidate rows from outcomes alone, so the
    real multiplier has to travel on the outcome to survive a raise."""
    import inspect

    import near_close_dca

    src = inspect.getsource(near_close_dca._process_buy_candidate)
    submitted_block = src.split('"status": "submitted"')[1].split("}")[0]
    assert "buy_multiplier" in submitted_block


def test_replacing_an_unreadable_afternoon_state_regenerates_its_companions(tmp_path):
    """A synthesized state describes THIS failure, so a companion left in a
    reused directory belongs to an earlier attempt. state_was_mutated is only set
    by the merge path, so replacing an unreadable run_state.json preserved a
    stale summary whose classification does not match the new state, failing a
    contract this handler could have repaired (Codex review)."""
    import near_close_dca as _n
    from scripts.scheduled_artifacts import validate_run_artifacts

    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)
    # Unreadable state plus a full set of an earlier attempt's leftovers.
    (out / "run_state.json").write_text("{not json", encoding="utf-8")
    (out / "run_summary.json").write_text(
        json.dumps({"run_classification": None, "stale": True}), encoding="utf-8"
    )
    (out / "signals.csv").write_text("", encoding="utf-8")
    (out / "submitted_orders.csv").write_text("stale,garbage\n", encoding="utf-8")

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        _n._write_afternoon_failure_artifacts(out, _now_et(), RuntimeError("boom"))
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    summary = json.loads((out / "run_summary.json").read_bytes())
    assert "stale" not in summary
    assert summary["run_classification"] is not None
    assert (out / "signals.csv").read_text(encoding="utf-8").strip() != ""
    assert "stale" not in (out / "submitted_orders.csv").read_text(encoding="utf-8")
    # The repaired set validates, so the handler no longer denies an artifact set
    # it could have fixed.
    assert validate_run_artifacts(out, require_summary=True)
    run_state = json.loads((out / "run_state.json").read_bytes())
    assert "artifact_contract_failure" not in run_state


def test_afternoon_failure_artifacts_carry_a_non_afternoon_slots_own_identity(tmp_path):
    """A 10:30/12:30 run's runtime failure must stamp ITS OWN classification and
    correlation id, not the afternoon default -- the raw-persistence and
    calendar-gate failure paths already do this; the generic exception handler
    in run_once() must too (Codex review: "Forward slot identity into
    exception artifacts")."""
    import near_close_dca as _n

    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)

    overrides = {
        "stream": "morning",
        "cron_id": "42173154",
        "supersedes_cron_id": None,
        "scheduled_for_et": "10:30",
        "scheduled_window_start_et": "10:25",
        "scheduled_window_end_et": "10:35",
        "run_id": "scheduled_20260629_1030ET",
    }

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        _n._write_afternoon_failure_artifacts(
            out,
            _now_et(),
            RuntimeError("boom"),
            classification_overrides=overrides,
            correlation_id="dca-2026-06-29-morning-1030et",
        )
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    run_state = json.loads((out / "run_state.json").read_bytes())
    assert run_state["run_id"] == "scheduled_20260629_1030ET"
    assert run_state["run_classification"]["stream"] == "morning"
    assert run_state["run_classification"]["cron_id"] == "42173154"
    assert run_state["correlation_id"] == "dca-2026-06-29-morning-1030et"


def test_the_afternoon_failure_set_is_validated_without_a_pointer_root(tmp_path):
    """The contract failure is stamped INTO the state, which needs no pointer
    root, so gating validation on classification_state_root left a broken
    companion set advertised unchecked (Codex review)."""
    import near_close_dca as _n

    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_state.json").write_text("{not json", encoding="utf-8")

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        _n._write_afternoon_failure_artifacts(out, _now_et(), RuntimeError("boom"))
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    # Companions were regenerated, so the set is valid and nothing is stamped.
    run_state = json.loads((out / "run_state.json").read_bytes())
    assert "artifact_contract_failure" not in run_state

    # Break a companion after the fact and re-enter: validation still runs with
    # no classification_state_root, and the breach is recorded in the state.
    (out / "signals.csv").unlink()
    (out / "run_state.json").write_text("{not json again", encoding="utf-8")
    (out / "submitted_orders.csv").write_text("stale,garbage\n", encoding="utf-8")
    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        _n._write_afternoon_failure_artifacts(out, _now_et(), RuntimeError("boom"))
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)
    assert (out / "signals.csv").exists()


def test_an_afternoon_connector_failure_beside_a_submission_is_not_clean():
    """Sibling of the morning fix, flagged in the same finding: a pre-placement
    broker check that raised is a non-deliberate block, and the submitted branch
    ignored those entirely (Codex review)."""
    from near_close_dca import _apply_no_order_classification

    artifacts = {"signal_rows": []}
    outcomes = {
        "AAA": {"status": "submitted", "order_id": "o-1"},
        "BBB": {"status": "blocked", "skip_reason": "spread quote failed: boom"},
    }

    _apply_no_order_classification(artifacts, outcomes, scheduled=True)

    assert artifacts["orders_submitted_count"] == 1
    assert artifacts["is_trading_failure"] is True
    assert artifacts["run_classification_outcome"] == "failed_after_partial_submission"
    assert artifacts["error_class"] == "degraded_market_data"


def test_an_afternoon_deliberate_skip_beside_a_submission_stays_clean():
    """A cap or ceiling block carries deliberate_skip=True and is not a failure."""
    from near_close_dca import _apply_no_order_classification

    artifacts = {"signal_rows": []}
    outcomes = {
        "AAA": {"status": "submitted", "order_id": "o-1"},
        "BBB": {
            "status": "blocked",
            "skip_reason": "cap exceeded",
            "deliberate_skip": True,
        },
    }

    _apply_no_order_classification(artifacts, outcomes, scheduled=True)

    assert artifacts["orders_submitted_count"] == 1
    assert "is_trading_failure" not in artifacts


def test_completing_a_preserved_state_keeps_the_rich_signal_columns(tmp_path):
    """Pinning fieldnames=("ticker",) truncated a PRESERVED state's signal_rows,
    dropping auto_buy_signal and the amounts the drift detector reads. The writer
    already derives fieldnames from the rows and falls back to ticker when there
    are none (Copilot review)."""
    import near_close_dca as _n

    preserved = _preserved_state()
    preserved["signal_rows"] = [
        {"ticker": "AAA", "auto_buy_signal": True, "suggested_amount_usd": 2.0},
    ]
    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_state.json").write_text(json.dumps(preserved), encoding="utf-8")

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        _n._write_afternoon_failure_artifacts(out, _now_et(), RuntimeError("boom"))
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    header = (out / "signals.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "ticker" in header
    assert "auto_buy_signal" in header
    assert "suggested_amount_usd" in header


def test_contract_failure_stamp_preserves_the_backfilled_usage_proxy(tmp_path):
    """Regression test for "Preserve usage backfill when stamping contract
    failures". A preserved run_state.json's usage_proxy predates the merge of
    a freshly-raised execution record into orders.csv; _backfill_orders_csv_usage
    recomputes csv_artifact_count/total_artifact_bytes and writes them straight
    to disk. If the artifact-contract check then fails (a stale run_summary.json
    mismatches the state), the branch that stamps artifact_contract_failure must
    not resurrect the STALE in-memory usage_proxy over that fresh backfill.
    """
    import near_close_dca as _n

    preserved = _preserved_state()
    preserved["usage_proxy"] = {
        "csv_artifact_count": 0,
        "state_file_bytes": 0,
        "total_artifact_bytes": 0,
    }
    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_state.json").write_text(json.dumps(preserved), encoding="utf-8")
    # A stale summary from a different stream so validate_run_artifacts fails
    # and the contract-failure branch fires.
    (out / "run_summary.json").write_text(
        json.dumps(
            {
                "run_id": "stale",
                "run_classification": {
                    "run_origin": "scheduled",
                    "manual_run": False,
                    "stream": "morning",
                },
            }
        ),
        encoding="utf-8",
    )

    record = build_execution_record(
        ticker="AAA",
        target_price=100.0,
        target_price_source="finance:signal_quote",
        decision_bar_close=100.0,
        notional_usd=2.0,
        avg_fill_price=100.0,
        quantity_filled=0.02,
        order_id="o-earlier",
    )

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        _n._write_afternoon_failure_artifacts(
            out, _now_et(), RuntimeError("boom"), execution_records=[record]
        )
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    run_state = json.loads((out / "run_state.json").read_bytes())
    # The contract failure did fire (proving this test exercises the branch
    # under test), yet the usage_proxy backfilled from disk survived it.
    assert run_state["artifact_contract_failure"]["failure"] == "artifact_contract_failure"
    assert run_state["usage_proxy"]["csv_artifact_count"] > 0
    assert run_state["usage_proxy"]["total_artifact_bytes"] > 0


def test_a_synthesized_state_with_no_rows_still_gets_a_ticker_header(tmp_path):
    """The fallback the removed explicit fieldnames used to provide."""
    import near_close_dca as _n

    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        _n._write_afternoon_failure_artifacts(out, _now_et(), RuntimeError("boom"))
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    assert (out / "signals.csv").read_text(encoding="utf-8").splitlines()[0] == "ticker"


# --- Account pinning rejects malformed values --------------------------------
# resolve_broker_account validates what the config LOADER parses, but the
# loaders' graceful-fallback paths (config absent or unparseable) and any direct
# MorningConfig/StrategyConfig construction bypass it. This shared gate is on
# every live path, so the shape check belongs here too (Codex review, PR #219).


@pytest.mark.parametrize(
    "bad",
    ["acct-123", "12345", "1234567890123", " 123456789 ", "read_only", "12a456789"],
)
def test_account_pinning_rejects_malformed_accounts(bad):
    from near_close_dca import account_pinning_failure

    reason = account_pinning_failure(bad)
    assert reason is not None, f"{bad!r} was accepted for live execution"
    assert "not a valid Robinhood account number" in reason


@pytest.mark.parametrize("good", ["123456", "123456789", "123456789012"])
def test_account_pinning_accepts_well_formed_accounts(good):
    from near_close_dca import account_pinning_failure

    assert account_pinning_failure(good) is None


def test_a_malformed_account_from_the_fallback_path_cannot_go_live(tmp_path):
    """End-to-end, in a fresh interpreter: mirrors how MorningDCAExecutor
    actually invokes the config-missing fallback for a scheduled live run
    (allow_unpinned_account=True -- see _load_default_config).

    The loader's own fallback used to build target_account_number straight
    from the (possibly stale, import-time-bound) environment with no
    validation at all, so the runtime account_pinning_failure() gate was the
    ONLY thing standing between a malformed account and a live run. The
    fallback now resolves and validates the account itself too (Codex
    review, PR #219: "Resolve the account when falling back to defaults"),
    so a malformed value is caught at load time -- target_account_number is
    left None rather than passing "acct-123" through -- and this test now
    confirms the SAME malformed input is still blocked at the runtime gate
    as well, i.e. two independent layers instead of relying on the pinning
    gate alone.
    """
    import os as _os
    import subprocess as _subprocess
    import sys as _sys
    from pathlib import Path as _Path

    probe = (
        "from morning_dca import load_morning_config;"
        "from near_close_dca import account_pinning_failure;"
        f"cfg = load_morning_config(config_dir={str(tmp_path)!r}, mode='live', "
        "allow_unpinned_account=True);"
        "print(repr(cfg.target_account_number));"
        "print(account_pinning_failure(cfg.target_account_number) is not None)"
    )
    env = dict(_os.environ, ROBINHOOD_AGENTIC_ACCOUNT_NUMBER="acct-123")
    out = _subprocess.run(
        [_sys.executable, "-c", probe],
        cwd=_Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    configured, blocked = out.stdout.strip().splitlines()[-2:]
    assert (
        configured == "None"
    ), f"malformed account should be caught at load time, not passed through: {out.stdout}"
    assert (
        blocked == "True"
    ), f"malformed account was accepted for live execution: {out.stdout}"


# --- The digit shape is Robinhood's, not every broker's ----------------------
# Applying it unconditionally at the shared pinning gate rejected a normal
# Alpaca identifier before execution reached the ALPACA branch, so the
# documented broker-agnostic live adapter could not be built at all
# (Codex review, PR #219).


def test_an_alpaca_account_is_not_held_to_the_robinhood_digit_shape():
    config = StrategyConfig(
        dry_run=False, broker=BrokerName.ALPACA, target_account_number="PA3ABCDEF123"
    )
    assert account_pinning_failure(config.target_account_number, config.broker) is None
    assert isinstance(build_broker(config), AlpacaBroker)


@pytest.mark.parametrize("broker", [BrokerName.ALPACA, BrokerName.ROBINHOOD, None])
@pytest.mark.parametrize(
    "unusable", [None, "", "UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"]
)
def test_no_backend_may_trade_an_account_nobody_named(broker, unusable):
    """The nonempty/placeholder half of the gate stays broker-agnostic."""
    assert account_pinning_failure(unusable, broker)


def test_robinhood_still_requires_the_digit_shape():
    assert account_pinning_failure("PA3ABCDEF123", BrokerName.ROBINHOOD)
    assert (
        account_pinning_failure("PA3ABCDEF123") is not None
    ), "default must stay Robinhood"


# --- The execution gate fails closed on an injected broker -------------------
# build_broker refuses to construct an unpinned live adapter, but callers may
# inject a BrokerAdapter into run_once directly and bypass the factory. The gate
# only compared broker.account_number() to the configured value, and two empty
# strings compare equal -- so an unpinned live run reached placement
# (Codex review, PR #219).


class _InjectedBroker(DryRunBroker):
    def __init__(self, account: str) -> None:
        super().__init__(account=account)


def _an_approved_day(config):
    return (
        sorted(config.approved_test_dates)[0]
        if config.approved_test_dates
        else date(2026, 7, 6)
    )


@pytest.mark.parametrize(
    "account", ["", "UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "acct-1", "12345"]
)
def test_an_injected_broker_cannot_trade_an_unpinned_live_account(account):
    config = StrategyConfig(dry_run=False, target_account_number=account)
    with pytest.raises(RuntimeError, match="Refusing to trade live"):
        _check_execution_gates(
            _an_approved_day(config), config, _InjectedBroker(account), None
        )


def test_a_properly_pinned_injected_broker_still_passes():
    config = StrategyConfig(dry_run=False, target_account_number=FIXTURE_ACCOUNT_NUMBER)
    assert _check_execution_gates(
        _an_approved_day(config), config, _InjectedBroker(FIXTURE_ACCOUNT_NUMBER), None
    )


def test_a_dry_run_is_unaffected_by_the_pinning_gate():
    config = StrategyConfig(dry_run=True, target_account_number="")
    assert _check_execution_gates(
        _an_approved_day(config), config, _InjectedBroker(""), None
    )


# --- one_order_per_ticker_per_day: closing the ticker_guarded bypass ---------
#
# config/budgets.json order_rules asserts one_order_per_ticker_per_day: true.
# Nothing read it. _process_buy_candidate gates duplicates twice --
# state.buys_today (:5296) and broker.same_day_strategy_order_exists (:5315) --
# and both are skipped when `ticker_guarded` is set, i.e. whenever a prior
# same-day allocation was loaded. That bypass is deliberate for the legacy
# manual-morning -> scheduled-afternoon top-up, and is kept by default.
#
# On the seven-slot roster it is the hazard: with the guard's state root absent
# (see test_dca_paths) or a slot missing from the prior-allocation chain, the
# same ticker can be bought at successive slots at RISING prices. The new flag
# lets a roster slot demand "first slot wins" without changing the legacy path.


def test_one_order_per_ticker_per_day_defaults_off_to_preserve_the_top_up_path():
    """Default False so test_run_daily_compact_manual_run_does_not_block_scheduled
    and the documented manual->afternoon top-up keep working unchanged."""
    assert StrategyConfig().one_order_per_ticker_per_day is False


def test_flag_makes_the_broker_same_day_gate_beat_a_loaded_prior_allocation(tmp_path):
    """With the flag on, a prior allocation no longer buys a second time."""
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-perday")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    config = replace(config, one_order_per_ticker_per_day=True)

    class _BlockingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

    blocking = _BlockingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=99.0,
            run_id="slot_1030",
            source="scheduled",
            selected_base_amount_usd=2.0,
        ),
    }

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        blocking,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    # Same setup as test_run_daily_compact_manual_run_does_not_block_scheduled,
    # which asserts AAPL is topped up for $2. The flag inverts exactly that.
    assert by_ticker["AAPL"]["status"] == "skipped"
    assert by_ticker["AAPL"]["skip_reason"] == "same-day broker order exists"
    assert by_ticker["AAPL"].get("incremental_order_amount", 0.0) == 0.0


def test_flag_checks_the_hard_ceiling_before_its_own_broker_duplicate_lookup(tmp_path):
    """Codex review discussion_r3679633773: 'Check the hard ceiling before the
    broker duplicate lookup'. one_order_per_ticker_per_day's own broad
    same_day_strategy_order_exists() check must not run for a candidate that
    is already over the hard per-order ceiling -- that candidate should never
    need a broker round-trip, matching the existing cap-before-duplicate
    contract for the legacy (flag-off) path.
    """
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-perday-cap-before-dup")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    config = replace(
        config,
        one_order_per_ticker_per_day=True,
        tickers=("AAPL",),
        base_buy_amount_usd=10.0,
        # The 2% drop below is 1.5x on the current ladder, so a $10 base plans
        # $15; the ceiling sits below that so the candidate is blocked.
        max_per_order_usd=10.0,
    )

    class _TrackingBroker(DryRunBroker):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.duplicate_lookup_calls = []

        def same_day_strategy_order_exists(self, ticker, trading_day):
            self.duplicate_lookup_calls.append(ticker)
            return super().same_day_strategy_order_exists(ticker, trading_day)

    tracking_broker = _TrackingBroker(cash=500.0, reference_prices={"AAPL": 98.0})

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        tracking_broker,
        options=CompactRunOptions(artifacts_dir=artifacts_dir),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    candidate = run_state["buy_candidates"][0]
    assert candidate["status"] == "blocked"
    assert candidate["skip_reason"] == "max per-order ceiling exceeded"
    assert tracking_broker.duplicate_lookup_calls == []


def test_flag_makes_local_buys_today_gate_beat_a_loaded_prior_allocation(tmp_path):
    """The :5296 gate is bypassed by ticker_guarded too; the flag closes both."""
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-perday-local")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    config = replace(config, one_order_per_ticker_per_day=True)

    trading_day = now.astimezone(EASTERN).date()
    state.record_buy("AAPL", trading_day, 2.0)
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=99.0,
            run_id="slot_1030",
            source="scheduled",
            selected_base_amount_usd=2.0,
        ),
    }

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["status"] == "skipped"
    assert by_ticker["AAPL"]["skip_reason"] == "already bought today"


def test_flag_off_leaves_the_documented_top_up_bypass_intact(tmp_path):
    """Anti-entropy pin: the legacy path must be untouched when the flag is off."""
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-legacy")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    assert config.one_order_per_ticker_per_day is False

    class _BlockingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

    blocking = _BlockingBroker(
        cash=500.0, reference_prices={t: 98.0 for t in config.tickers}
    )
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=2.0,
            reference_price=99.0,
            run_id="manual_am",
            source="manual",
            selected_base_amount_usd=2.0,
        ),
    }

    run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        blocking,
        options=CompactRunOptions(
            artifacts_dir=artifacts_dir, prior_same_day_allocations=prior
        ),
    )

    with open(
        os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8"
    ) as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["status"] == "dry_run"
    assert by_ticker["AAPL"]["incremental_order_amount"] == 1.0


def test_seed_prior_allocations_still_seeds_notional_when_the_flag_is_on():
    """Codex review discussion_r3679391265: 'Preserve prior allocations in cap
    bookkeeping'. Before this fix, one_order_per_ticker_per_day=True made
    _seed_prior_allocations an early-return no-op, so an earlier roster slot's
    spend never counted against day_notional()/week_notional() -- a later
    slot's OTHER (different-ticker) candidates would then see the full daily/
    weekly cap as if nothing had been spent yet, letting the roster's
    successive slots exceed the configured absolute $ caps even though the
    broker-level duplicate check still blocked re-buying the SAME ticker.
    """
    state = StrategyState()
    trading_day = date(2026, 6, 29)
    prior = {
        "AAPL": PriorSameDayAllocation(
            ticker="AAPL",
            amount_usd=2.0,
            reference_price=99.0,
            run_id="slot_1030",
            source="scheduled",
            selected_base_amount_usd=2.0,
        ),
    }

    _seed_prior_allocations(
        state,
        trading_day,
        True,
        prior,
        one_order_per_ticker_per_day=True,
    )

    assert state.buys_today("AAPL", trading_day) == 1
    assert state.day_notional(trading_day) == pytest.approx(2.0)
    assert state.week_notional(trading_day) == pytest.approx(2.0)


def test_seed_prior_allocations_tops_up_a_retry_that_discovers_more_evidence():
    """Codex review: "Reconcile increases to already-seeded ticker
    allocations". The original 15:30 run seeds AAPL at $2/1 attempt. A
    manual AAPL fill is then written elsewhere before a retry re-scans
    artifacts, raising the aggregate to $4/2 attempts. The old
    buys_today() == 0 gate treated the first seed as proof the ticker was
    already fully accounted for and skipped it on the retry entirely,
    losing both the extra $2 and the extra attempt."""
    state = StrategyState()
    trading_day = date(2026, 6, 29)

    _seed_prior_allocations(
        state,
        trading_day,
        True,
        {
            "AAPL": PriorSameDayAllocation(
                ticker="AAPL", amount_usd=2.0, reference_price=99.0, attempt_count=1
            ),
        },
        one_order_per_ticker_per_day=False,
    )
    assert state.buys_today("AAPL", trading_day) == 1
    assert state.day_notional(trading_day) == pytest.approx(2.0)

    # Retry: a fresh scan discovers a new manual fill for the same ticker.
    _seed_prior_allocations(
        state,
        trading_day,
        True,
        {
            "AAPL": PriorSameDayAllocation(
                ticker="AAPL", amount_usd=4.0, reference_price=99.0, attempt_count=2
            ),
        },
        one_order_per_ticker_per_day=False,
    )
    assert state.buys_today("AAPL", trading_day) == 2, (
        "the retry's newly-discovered attempt must be topped up, not skipped"
    )
    assert state.day_notional(trading_day) == pytest.approx(4.0), (
        "the retry's newly-discovered $2 must be topped up, not skipped"
    )

    # A third pass with no new evidence must be a no-op, not a re-add.
    _seed_prior_allocations(
        state,
        trading_day,
        True,
        {
            "AAPL": PriorSameDayAllocation(
                ticker="AAPL", amount_usd=4.0, reference_price=99.0, attempt_count=2
            ),
        },
        one_order_per_ticker_per_day=False,
    )
    assert state.buys_today("AAPL", trading_day) == 2
    assert state.day_notional(trading_day) == pytest.approx(4.0)


def test_reconcile_prior_allocation_tops_up_notional_when_attempt_count_is_unchanged():
    """Codex review: "Reconcile notional when the attempt count is unchanged".
    A previously seeded order's executed notional can grow (a partial fill
    confirms more shares) across two scans with NO new attempt at all --
    attempt_delta stays 0 while the dollar delta is genuinely positive. The
    old early return (gated on attempt_delta alone) skipped that dollar
    delta entirely, leaving the daily/weekly ledgers understated."""
    state = StrategyState()
    trading_day = date(2026, 6, 29)

    state.reconcile_prior_allocation("AAPL", trading_day, amount_usd=1.0, attempt_count=1)
    assert state.buys_today("AAPL", trading_day) == 1
    assert state.day_notional(trading_day) == pytest.approx(1.0)

    # Same attempt count, but the fill's confirmed notional grew from $1 to $2.
    state.reconcile_prior_allocation("AAPL", trading_day, amount_usd=2.0, attempt_count=1)
    assert state.buys_today("AAPL", trading_day) == 1, "no new attempt was discovered"
    assert state.day_notional(trading_day) == pytest.approx(2.0), (
        "the extra $1 of confirmed notional must be reconciled even with no new attempt"
    )


def test_reconcile_prior_allocation_persists_baseline_on_exact_match():
    """Codex review: "Record no-op prior-allocation baselines". A ticker's
    first-ever reconcile_prior_allocation call can exactly match an existing
    buys_today() baseline (e.g. a real order recorded by this stream in a
    prior process invocation) -- both deltas compute to zero on that first
    call. The old early return skipped persisting the tracked dicts in that
    case, so a LATER retry that discovers the aggregate has genuinely grown
    recomputed its own baseline from that later call's own (larger)
    amount_usd, silently assuming the whole total was already credited and
    permanently losing the dollar delta between the two scans."""
    state = StrategyState()
    trading_day = date(2026, 6, 29)

    # A real $2/1-attempt order already exists in durable state (as if
    # recorded by this stream in an earlier process invocation).
    state.record_buy("AAPL", trading_day, 2.0)
    assert state.buys_today("AAPL", trading_day) == 1
    assert state.day_notional(trading_day) == pytest.approx(2.0)

    # First reconcile call exactly matches the existing baseline -- both
    # deltas are zero, but the baseline must still be persisted.
    state.reconcile_prior_allocation("AAPL", trading_day, amount_usd=2.0, attempt_count=1)
    assert state.buys_today("AAPL", trading_day) == 1
    assert state.day_notional(trading_day) == pytest.approx(2.0)

    # A retry discovers the aggregate genuinely grew to $4/2 attempts.
    state.reconcile_prior_allocation("AAPL", trading_day, amount_usd=4.0, attempt_count=2)
    assert state.buys_today("AAPL", trading_day) == 2, "the new attempt must be counted"
    assert state.day_notional(trading_day) == pytest.approx(4.0), (
        "the full $2 delta since the last reconcile call must be credited, "
        "not silently dropped because the first call's exact match was never persisted"
    )


def test_seed_prior_allocations_skips_a_ticker_this_stream_already_bought_for_real():
    """A ticker THIS stream has already booked its own real order for
    (own_orders_recorded) must never be topped up from the separate seeded-
    prior-context tracker -- record_buy already accounts for it, and doing
    so too would double-count it."""
    state = StrategyState()
    trading_day = date(2026, 6, 29)
    state.record_buy("AAPL", trading_day, 5.0)
    state.own_orders_recorded.add(("AAPL", trading_day))

    _seed_prior_allocations(
        state,
        trading_day,
        True,
        {
            "AAPL": PriorSameDayAllocation(
                ticker="AAPL", amount_usd=8.0, reference_price=99.0, attempt_count=2
            ),
        },
        one_order_per_ticker_per_day=False,
    )

    assert state.buys_today("AAPL", trading_day) == 1
    assert state.day_notional(trading_day) == pytest.approx(5.0)


def test_seed_prior_allocations_is_a_noop_when_guard_inactive_regardless_of_the_flag():
    """guard_active still gates seeding either way: an unscheduled/manual run
    (or a scheduled run with no prior_same_day_allocations loaded at all) must
    not seed bookkeeping from a stale caller-supplied dict."""
    state = StrategyState()
    trading_day = date(2026, 6, 29)
    prior = {
        "AAPL": PriorSameDayAllocation(
            ticker="AAPL",
            amount_usd=2.0,
            reference_price=99.0,
            run_id="slot_1030",
            source="scheduled",
            selected_base_amount_usd=2.0,
        ),
    }

    _seed_prior_allocations(
        state, trading_day, False, prior, one_order_per_ticker_per_day=True
    )

    assert state.buys_today("AAPL", trading_day) == 0
    assert state.day_notional(trading_day) == pytest.approx(0.0)


def test_seed_prior_allocations_preserves_the_real_attempt_count():
    """Codex review: "Count every prior buy attempt toward the daily limit".
    Earlier slots made 8 attempts total across 3 tickers, several of which
    were rejected/canceled (amount_usd == 0 for that ticker's aggregate, or
    partially offset by one filled attempt). record_buy's fixed +1-per-
    ticker used to collapse this into 3 seeded attempts, letting within_caps
    authorize up to 5 MORE real orders even though the shared
    max_orders_per_day: 8 circuit breaker should already be exhausted."""
    state = StrategyState()
    trading_day = date(2026, 6, 29)
    prior = {
        # 5 attempts, all rejected -- zero capital moved, but still 5 of the
        # shared cap's 8 slots.
        "AAAU": PriorSameDayAllocation(
            ticker="AAAU", amount_usd=0.0, attempt_count=5
        ),
        # 2 attempts, only the second filled.
        "TRIN": PriorSameDayAllocation(
            ticker="TRIN", amount_usd=3.0, reference_price=50.0, attempt_count=2
        ),
        # 1 attempt, filled.
        "EVR": PriorSameDayAllocation(
            ticker="EVR", amount_usd=2.0, reference_price=25.0, attempt_count=1
        ),
    }

    _seed_prior_allocations(
        state,
        trading_day,
        True,
        prior,
        one_order_per_ticker_per_day=False,
    )

    assert state.day_order_count(trading_day) == 8
    assert state.day_notional(trading_day) == pytest.approx(5.0)
    config = StrategyConfig(max_orders_per_day=8)
    # The shared cap is already exhausted by the reconstructed attempts --
    # even a trivially small, otherwise-affordable order must be refused.
    assert within_caps(1.0, state, trading_day, config) is False


def test_bump_week_notional_only_touches_only_the_weekly_bucket():
    """Codex review: "Include prior-week sibling spend in the afternoon
    ledger". Earlier-weekday cross-stream spend must count against the
    shared weekly cap without being attributed to any specific ticker's
    same-day top-up reference (day_notional, buys_today, and
    day_order_count must all stay untouched)."""
    state = StrategyState()
    trading_day = date(2026, 6, 30)  # Tuesday

    state.bump_week_notional_only(trading_day, 150.0)

    assert state.week_notional(trading_day) == pytest.approx(150.0)
    assert state.day_notional(trading_day) == 0.0
    assert state.day_order_count(trading_day) == 0
    assert state.buys_today("AAPL", trading_day) == 0

    # Additive across multiple calls (e.g. two earlier weekdays scanned
    # separately), not a last-write-wins overwrite.
    state.bump_week_notional_only(trading_day, 25.0)
    assert state.week_notional(trading_day) == pytest.approx(175.0)


def test_force_week_notional_to_worst_case_ceiling_fails_closed():
    """Codex review: "Include prior-week sibling spend in the afternoon
    ledger". An earlier-weekday scan that could not verify every artifact
    it found must not let the run place against an unproven, possibly
    understated weekly total -- force the bucket to the worst-case (Stress)
    tier's ceiling, matching morning_dca.py's own fail-closed policy for
    the same failure mode."""
    state = StrategyState()
    trading_day = date(2026, 6, 30)
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)

    state.force_week_notional_to_worst_case_ceiling(trading_day)

    assert state.week_notional(trading_day) == worst_case_cap

    # A direct max(), not additive: calling it again (e.g. a second failed
    # scan) must not inflate the bucket past the ceiling.
    state.force_week_notional_to_worst_case_ceiling(trading_day)
    assert state.week_notional(trading_day) == worst_case_cap

    # And it must never LOWER an already-larger real total.
    state2 = StrategyState()
    state2.bump_week_notional_only(trading_day, worst_case_cap + 500.0)
    state2.force_week_notional_to_worst_case_ceiling(trading_day)
    assert state2.week_notional(trading_day) == pytest.approx(worst_case_cap + 500.0)


def test_reconcile_earlier_week_cross_stream_notional_does_not_double_count_own_history():
    """Codex review: "Reconcile earlier-week spend instead of adding it".
    This StrategyState is persistent across invocations -- an earlier day's
    OWN record_buy() calls already updated submitted_notional_by_week for
    that week. run_dca._compute_earlier_week_cross_stream_notional's total
    is re-derived from on-disk artifacts on EVERY invocation (it includes
    this stream's own earlier-day spend, since its all_roots includes the
    afternoon root) and does not shrink, so reconciling it must not simply
    add it on top of spend this state already knows about from when it
    actually happened."""
    state = StrategyState()
    trading_day = date(2026, 6, 30)  # Tuesday
    # Monday's own record_buy already put $100 into this week's bucket --
    # exactly the kind of spend a fresh cross-stream scan would re-discover.
    state.record_buy("AAPL", date(2026, 6, 29), 100.0)

    # A scan re-derived from artifacts (Monday's own $100 -- already in the
    # bucket above -- plus $50 of genuine cross-stream sibling spend) reports
    # $150 total. Only the genuinely NEW $50 sibling portion must be added on
    # top of the $100 already present, landing on $150, not $250.
    state.reconcile_earlier_week_cross_stream_notional(trading_day, 150.0)
    assert state.week_notional(trading_day) == pytest.approx(150.0)

    # A later invocation this SAME week re-derives the SAME $150 total
    # (nothing new happened) -- must be a no-op, not another +$150.
    state.reconcile_earlier_week_cross_stream_notional(trading_day, 150.0)
    assert state.week_notional(trading_day) == pytest.approx(150.0)

    # A genuinely NEW cross-stream fill raises the re-derived total to
    # $180 -- only the $30 delta should be added.
    state.reconcile_earlier_week_cross_stream_notional(trading_day, 180.0)
    assert state.week_notional(trading_day) == pytest.approx(180.0)


def test_reconcile_earlier_week_cross_stream_notional_preserves_same_day_spend_layered_after_it():
    """A same-day retry that already recorded some of TODAY's own spend (via
    record_buy, layered on top of the last reconciliation) must keep that
    spend when a later reconcile call folds in a newly-discovered earlier-
    week sibling fill -- a bare max(current, total_usd) would silently drop
    it, since total_usd only ever covers EARLIER days, never today."""
    state = StrategyState()
    trading_day = date(2026, 6, 30)  # Tuesday
    state.record_buy("AAPL", date(2026, 6, 29), 100.0)  # Monday's own spend

    # First reconciliation this week: Monday's own $100 plus $50 sibling.
    state.reconcile_earlier_week_cross_stream_notional(trading_day, 150.0)
    assert state.week_notional(trading_day) == pytest.approx(150.0)

    # Today's own $20 buy lands on top (a normal part of today's run).
    state.record_buy("MSFT", trading_day, 20.0)
    assert state.week_notional(trading_day) == pytest.approx(170.0)

    # A retry re-scans and discovers a NEW $30 Monday sibling fill that
    # wasn't visible before -- total_usd rises to $180. Reconciling this
    # must add only the new $30 on top of the current $170 (which includes
    # today's own $20), landing on $200 -- not silently drop today's $20 by
    # taking max($170, $180) == $180.
    state.reconcile_earlier_week_cross_stream_notional(trading_day, 180.0)
    assert state.week_notional(trading_day) == pytest.approx(200.0)


def test_reconcile_earlier_week_cross_stream_notional_clears_prior_poisoning():
    """A transient earlier-week scan failure poisons the bucket to the
    worst-case ceiling; once a LATER invocation's scan succeeds, reconciling
    the real total must replace the synthetic ceiling, not add on top of
    it."""
    state = StrategyState()
    trading_day = date(2026, 6, 30)
    worst_case_cap = max(tier.weekly_cap_usd for tier in BUDGET_TIERS)

    state.force_week_notional_to_worst_case_ceiling(trading_day)
    assert state.week_notional(trading_day) == worst_case_cap

    # The scan recovers with a real, modest $75 total.
    state.reconcile_earlier_week_cross_stream_notional(trading_day, 75.0)
    assert state.week_notional(trading_day) == pytest.approx(75.0)


def test_reconcile_earlier_week_cross_stream_notional_excludes_same_day_seed_from_baseline():
    """Codex review: "Exclude today's allocations from the prior-week
    baseline". _run_once_impl snapshots week_notional() BEFORE calling
    _seed_prior_allocations (which folds TODAY's own same-day cross-stream
    context into the very same weekly bucket) and passes that snapshot as
    pre_same_day_seed_week_notional. Without it, the first reconciliation
    this week would mistake today's own just-seeded same-day spend for
    already-accounted earlier-week spend, silently dropping the real
    earlier-week delta."""
    state = StrategyState()
    trading_day = date(2026, 6, 30)  # Tuesday

    # Nothing earlier this week yet -- the snapshot taken before today's own
    # same-day seeding runs is $0.
    pre_same_day_seed_week_notional = state.week_notional(trading_day)
    assert pre_same_day_seed_week_notional == pytest.approx(0.0)

    # Today's own same-day cross-stream context gets seeded first (as
    # _run_once_impl does), landing $40 in the shared weekly bucket.
    state.reconcile_prior_allocation("AAPL", trading_day, amount_usd=40.0, attempt_count=1)
    assert state.week_notional(trading_day) == pytest.approx(40.0)

    # A fresh earlier-week cross-stream scan reports $30 of genuine Monday
    # sibling spend. Reconciling against the correct pre-seed baseline ($0)
    # must add the full $30 on top of today's $40, landing on $70 -- not
    # mistake today's own $40 same-day seed for already-accounted earlier-
    # week spend and silently drop the $30 delta.
    state.reconcile_earlier_week_cross_stream_notional(
        trading_day, 30.0, pre_same_day_seed_week_notional=pre_same_day_seed_week_notional
    )
    assert state.week_notional(trading_day) == pytest.approx(70.0)


def test_reconcile_earlier_week_cross_stream_notional_persists_baseline_on_exact_match():
    """Codex review: "Persist an unchanged earlier-week reconciliation
    baseline". A first-ever reconciliation this ISO week can exactly match
    its pre_same_day_seed_week_notional baseline -- incremental computes to
    zero. The old code only recorded earlier_week_cross_stream_seeded_usd
    inside the `incremental > 0` branch, so that exact match left the key
    unset. A LATER retry then re-entered the "first reconciliation this
    week" branch again and re-baselined against a snapshot that had since
    grown from today's own same-day spend, silently under-crediting the
    real earlier-week delta discovered by the retry."""
    state = StrategyState()
    monday = date(2026, 6, 29)
    tuesday = date(2026, 6, 30)

    # Monday's own spend, already in the durable weekly bucket.
    state.record_buy("MSFT", monday, 100.0)
    assert state.week_notional(tuesday) == pytest.approx(100.0)

    # First reconciliation this week exactly matches the existing bucket --
    # incremental is zero, but the baseline must still be persisted.
    pre_same_day_seed_week_notional = state.week_notional(tuesday)
    state.reconcile_earlier_week_cross_stream_notional(
        tuesday, 100.0, pre_same_day_seed_week_notional=pre_same_day_seed_week_notional
    )
    assert state.week_notional(tuesday) == pytest.approx(100.0)

    # Tuesday's own $10 order lands on top (a normal part of today's run).
    state.record_buy("IBM", tuesday, 10.0)
    assert state.week_notional(tuesday) == pytest.approx(110.0)

    # A retry discovers $50 of genuine NEW Monday sibling spend the first
    # scan missed (total_usd rises from $100 to $150). Reconciling against
    # the correctly persisted $100 baseline must add the full $50 on top of
    # the current $110, landing on $160 -- not re-baseline against the
    # current (Tuesday-inflated) bucket and silently drop $10 of it.
    state.reconcile_earlier_week_cross_stream_notional(
        tuesday, 150.0, pre_same_day_seed_week_notional=state.week_notional(tuesday)
    )
    assert state.week_notional(tuesday) == pytest.approx(160.0)


def test_mixed_day_aggregate_still_charges_todays_own_attempt(tmp_path):
    """A prior-day recovery merged with a fresh order placed TODAY must charge
    today exactly one attempt -- not zero.

    ``_attempt_side_days`` records the fresh side under the UNDATED bucket (it
    has no claim breakdown of its own to date it by), so the aggregate's
    ``whole_share_claim_breakdown`` names only the OLD date. The loader's
    breakdown-based ``_is_cross_day_recovery_only`` guard therefore fired and
    skipped the bucket-aware count entirely, charging 0 for a real order placed
    today and leaving room for a later run to exceed ``max_orders_per_day``
    (Codex review, PR #786: "Count today's attempt in mixed-day aggregate
    rows").

    Round 35's failure mode on a new pair: an outer guard re-deriving what the
    inner helper now computes properly."""
    import json

    from near_close_dca import (
        _broker_attempts_on_day,
        _merge_whole_share_submission_outcomes,
        load_prior_same_day_allocations,
    )

    prior_day = "2026-06-27"
    today = date(2026, 6, 29)

    recovery = {
        "ticker": "RVII", "status": "submitted", "order_id": "OLD",
        "whole_share_claim_breakdown": [{"trading_day": prior_day, "amount_usd": 23.0}],
    }
    fresh_today = {"ticker": "RVII", "status": "submitted", "order_id": "NEW"}
    merged = _merge_whole_share_submission_outcomes(recovery, fresh_today)

    # The buckets themselves were already right -- both sides recorded, the
    # prior day dated and today's undated.
    assert merged["merged_attempt_days"] == {prior_day: 1, "": 1}
    assert merged["merged_attempt_count"] == 2
    assert _broker_attempts_on_day(merged, today.isoformat()) == 1

    run_dir = tmp_path / "morning_dca" / "20260629_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps({
        "run_id": "r1",
        "generated_at_et": "2026-06-29T09:35:00-04:00",
        "trading_day": today.isoformat(),
        "stream": "morning",
        "buy_candidates": [dict(merged, planned_amount_usd=23.36,
                                amount_usd=23.36, target_price=23.36)],
    }))

    prior = load_prior_same_day_allocations(tmp_path, today)
    # ... and now the LOADER agrees with the helper, where it used to say 0.
    assert prior["RVII"].attempt_count == 1


def test_a_pure_cross_day_recovery_still_charges_today_nothing(tmp_path):
    """The other direction of the same fix: the carve-out must stay no wider
    than its justification (round 29's rule).

    A row whose every attempt belongs to an earlier day still charges today
    zero -- the bucket count answers that on its own, so honouring the buckets
    does not resurrect the double-count the breakdown guard was added for."""
    import json

    from near_close_dca import load_prior_same_day_allocations

    prior_day = "2026-06-27"
    today = date(2026, 6, 29)
    row = {
        "ticker": "RVII", "status": "submitted", "order_id": "OLD",
        "planned_amount_usd": 23.36, "amount_usd": 23.36, "target_price": 23.36,
        "whole_share_claim_breakdown": [{"trading_day": prior_day, "amount_usd": 23.0}],
        "merged_attempt_days": {prior_day: 1},
        "merged_attempt_count": 1,
    }
    run_dir = tmp_path / "morning_dca" / "20260629_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps({
        "run_id": "r1", "generated_at_et": "2026-06-29T09:35:00-04:00",
        "trading_day": today.isoformat(), "stream": "morning",
        "buy_candidates": [row],
    }))

    prior = load_prior_same_day_allocations(tmp_path, today)
    assert prior["RVII"].attempt_count == 0


def test_every_candidate_row_builder_persists_merged_attempt_buckets():
    """``merged_attempt_days``/``merged_attempt_count`` have to reach the
    ARTIFACT, not just the intermediate outcome dict.

    ``load_prior_same_day_allocations`` reads the persisted row, so two
    same-day zero-fill recoveries merged as 2 attempts reloaded as 1 and left
    room for an extra live order beyond ``max_orders_per_day`` (Codex review,
    PR #786: "Persist merged attempt buckets into candidate artifacts").

    Round 31's rule applied to a NEW field rather than a new reader: every row
    builder that rebuilds from an explicit field list has to learn it, or the
    field silently stops existing on whichever path built that row. All four
    are checked here, and unconditionally -- ``_write_csv`` derives
    buy_candidates.csv's header from row 0 alone."""
    from near_close_dca import (
        _broker_attempts_on_day,
        _build_buy_candidates,
        _failure_candidate_row,
        _merge_whole_share_submission_outcomes,
    )

    zero_fill = {
        "ticker": "RVII", "status": "failed", "order_id": "Z1",
        "whole_share_zero_fill_attempt": True, "broker_placement_failed": True,
    }
    second = dict(zero_fill, order_id="Z2")
    merged = _merge_whole_share_submission_outcomes(zero_fill, second)
    assert merged["merged_attempt_count"] == 2

    outcome = dict(
        merged, status="submitted", submitted_amount_usd=23.36,
        whole_share_quantity=1,
        whole_share_claim_breakdown=[{"trading_day": "2026-06-29", "amount_usd": 23.36}],
    )

    rows = _build_buy_candidates([], {"RVII": outcome}, top_up_outcomes=None)
    assert rows[0]["merged_attempt_days"] == merged["merged_attempt_days"]
    assert _broker_attempts_on_day(rows[0], "2026-06-29") == 2

    failure_row = _failure_candidate_row("RVII", outcome)
    assert _broker_attempts_on_day(failure_row, "2026-06-29") == 2

    # Unconditionally present, defaulted, on a decision-keyed row that never
    # merged at all -- the CSV-header contract every sibling field here already
    # follows, since _write_csv derives the header from row 0 alone.
    plain_decision = ReconciledDecision(
        ticker="AAA", action=Action.BUY, reason="ladder buy",
        finance_signal=None, robinhood_signal=None, buy_multiplier=1.0,
        suggested_amount_usd=2.0, selected_base_amount_usd=2.0, target_price=97.0,
    )
    plain = _build_buy_candidates(
        [plain_decision],
        {"AAA": {"order_id": "AAA-1", "status": "dry_run",
                 "submitted_amount_usd": 2.0}},
        top_up_outcomes=None,
    )
    assert "merged_attempt_days" in plain[0]
    assert "merged_attempt_count" in plain[0]
    assert plain[0]["merged_attempt_days"] is None
    # ... and an unmerged row still counts exactly 1, as it always did.
    assert _broker_attempts_on_day(plain[0], "2026-06-29") == 1

    # Row 0 (never merged) and a later merged row must agree on their key sets,
    # or csv.DictWriter raises on the second row.
    assert set(plain[0]) >= {"merged_attempt_days", "merged_attempt_count"}
    assert set(rows[0]) >= {"merged_attempt_days", "merged_attempt_count"}


def test_a_malformed_but_well_typed_signal_is_degraded_on_the_normal_path():
    """``_serializable_signal`` has to run where signals normally arrive.

    It was applied only in the reconciliation-exception fallback, so a provider
    returning a REAL ``SourceSignal`` with a valid action and an unserializable
    emitted field (``reason=object()``) passed the ``isinstance`` check,
    survived ``reconcile_signals`` untouched, and reached ``json.dumps`` while
    writing ``run_state.json`` -- after other tickers' orders had been placed
    (Codex review, PR #786: "Validate normal provider signals before retaining
    them").

    The guard existed and was correct; the path a signal normally arrives on
    simply never consulted it."""
    import json as _json

    from near_close_dca import Action, SourceName, SourceSignal, _serializable_signal, _signal_row

    bad = SourceSignal(
        source=SourceName.FINANCE, ticker="AAA", action=Action.BUY, reason=object()
    )
    # The trap: well-typed, valid action, and fatal to the writer.
    assert isinstance(bad, SourceSignal)
    assert _serializable_signal(bad) is None
    with pytest.raises(TypeError):
        _json.dumps(_signal_row("AAA", "finance", bad))

    # A well-formed signal is untouched by the same guard.
    good = SourceSignal(
        source=SourceName.FINANCE, ticker="AAA", action=Action.BUY, reason="ladder buy"
    )
    assert _serializable_signal(good) is good
    _json.dumps(_signal_row("AAA", "finance", good))


def test_terminal_evidence_applies_per_represented_order_not_per_row(tmp_path):
    """One aggregate id must not invalidate every fill the row represents.

    An aggregate row's scalar ``order_id`` names only ONE of the physical
    orders it stands for, and which one is an accident of the merge: when both
    sides submitted, ``_merge_whole_share_submission_outcomes`` leaves NEW's id
    there. So an older recovery that really filled, merged with a fresh order
    that was accepted and then cancelled with an explicit zero, put the
    CANCELLED id in the scalar field -- and the terminal-id check dropped the
    whole candidate, erasing the older order's real spend from same-day cap and
    top-up reconstruction (Codex review, PR #786: "Preserve other fills when a
    merged order later terminates").

    The merge cannot fix this at its end -- whether NEW later turns out
    terminal is not knowable when the sides are combined -- so the evidence is
    applied where it is read."""
    header = "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"

    def _load(row, csv_body):
        root = os.path.join(tmp_path, str(abs(hash(csv_body + str(row)))), "midday_dca")
        run_dir = os.path.join(root, "20260629_1230ET_recovery_1305ET")
        os.makedirs(run_dir, exist_ok=True)
        state = _recovery_run_state([row])
        state["artifacts"] = {"orders_csv_path": "filled_orders.csv"}
        with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as h:
            json.dump(state, h)
        with open(os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8") as h:
            h.write(header + csv_body)
        return load_prior_same_day_allocations(root, date(2026, 6, 29)).get("RVII")

    aggregate = {
        "ticker": "RVII", "status": "submitted", "order_id": "NEW",
        "whole_share_order_ids": ["OLD", "NEW"], "planned_amount_usd": 23.36,
    }
    mixed = "RVII,OLD,23.00,filled,23.000000,1\nRVII,NEW,23.36,cancelled,,0\n"

    # The finding: OLD really filled, and its $23.00 must survive.
    got = _load(aggregate, mixed)
    assert got is not None
    assert got.amount_usd == 23.00
    assert got.reference_price == 23.00

    # Identical evidence, identical orders -- only which id the merge happened
    # to leave in the scalar field differs. The two must agree.
    control = _load(dict(aggregate, order_id="OLD"), mixed)
    assert control.amount_usd == got.amount_usd

    # The carve-out is no WIDER than its justification: when EVERY represented
    # order is terminal the row still contributes no dollars, and a plain row
    # is unchanged because it represents exactly its own scalar id.
    all_dead = _load(
        aggregate, "RVII,OLD,23.36,rejected,,0\nRVII,NEW,23.36,cancelled,,0\n"
    )
    assert all_dead.amount_usd == 0.0
    plain_dead = _load(
        {"ticker": "RVII", "status": "submitted", "order_id": "NEW",
         "planned_amount_usd": 23.36},
        "RVII,NEW,23.36,cancelled,,0\n",
    )
    assert plain_dead.amount_usd == 0.0


def test_a_zero_fill_recovery_merges_whatever_order_it_arrives_in():
    """Which claim reconciles first is arbitrary; the artifact must not depend
    on it.

    The zero-fill branch published through the plain publisher and skipped
    entirely when another claim for the same ticker had already settled as
    ``"submitted"``, on the grounds that ``record_buy`` counts the attempt
    anyway. That is true of THIS run's in-memory state and false of the next
    one: ``load_prior_same_day_allocations`` rebuilds the cap from the
    ARTIFACT, so the suppressed zero-fill took its ``merged_attempt_days``,
    ``merged_attempt_count`` and order id with it, and one slot was charged
    for two real broker attempts (Codex review, PR #786: "Merge zero-fill
    recoveries regardless of arrival order").

    Round 38's lesson at a second publication site: a value that never reaches
    the artifact is a value the next run does not have."""
    from near_close_dca import (
        _broker_attempts_on_day,
        _merge_whole_share_submission_outcomes,
        _outcome_is_mergeable_recovery,
    )

    filled = {
        "ticker": "RVII", "status": "submitted", "order_id": "FILLED",
        "submitted_amount_usd": 23.0, "whole_share_quantity": 1,
        "whole_share_claim_breakdown": [{"trading_day": "2026-06-27", "amount_usd": 23.0}],
    }
    zero = {
        "ticker": "RVII", "status": "failed", "order_id": "ZERO",
        "whole_share_zero_fill_attempt": True, "broker_placement_failed": True,
        "whole_share_claim_breakdown": [{"trading_day": "2026-06-28", "amount_usd": 0.0}],
    }

    # The publisher already knew how to do this -- round 34 taught
    # _outcome_is_mergeable_recovery to count a marked zero-fill attempt --
    # so the branch only had to stop suppressing and start using it.
    assert _outcome_is_mergeable_recovery(filled) is True

    for merged, expected_ids in (
        (_merge_whole_share_submission_outcomes(filled, zero), ["FILLED", "ZERO"]),
        (_merge_whole_share_submission_outcomes(zero, filled), ["ZERO", "FILLED"]),
    ):
        # Both attempts survive, each on its own claim day, regardless of
        # which broker reconciliation result arrived first.
        assert merged["merged_attempt_count"] == 2
        assert merged["merged_attempt_days"] == {"2026-06-27": 1, "2026-06-28": 1}
        assert merged["whole_share_order_ids"] == expected_ids
        # ... and the settled claim's own purchase is not misreported as
        # anything else, which is exactly what the old suppression was
        # protecting.
        assert merged["status"] == "submitted"
        assert merged["submitted_amount_usd"] == 23.0
        assert merged["whole_share_zero_fill_attempt"] is True
        assert _broker_attempts_on_day(merged, "2026-06-27") == 1
        assert _broker_attempts_on_day(merged, "2026-06-28") == 1


def test_the_zero_fill_branch_no_longer_suppresses_its_own_publication():
    """Walks the source, because the defect was a publication that never
    happened rather than a wrong value.

    A returned outcome cannot show that a branch declined to publish, so the
    guard is pinned where it lived: the zero-fill branch must reach the
    MERGING publisher, and must not carry the old status test that skipped it
    outright."""
    import inspect

    import near_close_dca

    source = inspect.getsource(near_close_dca._book_reconciled_whole_share_order)
    zero_fill_branch = source.split('if reconciled.get("zero_fill_attempt")')[1]
    zero_fill_branch = zero_fill_branch.split('if reconciled.get("live_order_found")')[0]

    assert "_publish_or_merge_whole_share_outcome" in zero_fill_branch
    # The suppression this finding removed, in the shape it had.
    assert '.get("status") != "submitted"' not in zero_fill_branch


def test_share_eligibility_and_the_placement_notional_agree_on_cents():
    """The ceiling cannot be crossed by two rounding rules disagreeing.

    Eligibility used ``round(price * 100)`` while the placement path uses
    ``round(quantity * price, 2)``. At ``price=50.005`` the first is 5000 --
    one share reads as fitting under a $50.00 ceiling -- and the second is
    $50.01, over it (Codex review, PR #786: "Use consistent cent rounding for
    the whole-share allowance")."""
    from near_close_dca import _whole_share_max_shares_by_ceiling

    # The reported disagreement, from both sides.
    assert round(50.005 * 100) == 5000  # the trap: reads as exactly $50.00
    assert round(1 * 50.005, 2) == 50.01  # what placement would really charge
    assert _whole_share_max_shares_by_ceiling(50.0, 50.005) == 0

    # The invariant, stated directly: whatever count this returns, the
    # notional the placement path computes for it is within the ceiling.
    for ceiling, price in [
        (50.0, 50.005), (10.0, 10.00), (10.0, 10.01), (50.0, 23.36),
        (10.0, 23.36), (10.0, 0.10), (50.0, 25.00), (50.0, 16.665),
    ]:
        shares = _whole_share_max_shares_by_ceiling(ceiling, price)
        assert shares >= 0
        if shares:
            assert round(shares * price, 2) <= ceiling, (ceiling, price, shares)

    # ... and the exact-cent case round 26 added this helper for is unchanged:
    # 10.0 // 0.10 is 99.0 in raw float arithmetic, and must still be 100.
    assert _whole_share_max_shares_by_ceiling(10.0, 0.10) == 100


def test_a_failed_broker_call_consumes_a_daily_order_slot():
    """Codex review, PR #786 ("Count failed broker calls toward the daily
    attempt cap").

    `config/budgets.json` states the contract — "a cancelled or rejected buy
    still consumes one [slot] — the conservative direction" — and
    `seed_prior_allocation` already honours it when a LATER run reconstructs
    today from artifacts. Only the success path charged a slot in-run, so the
    in-run counter and the cross-run reconstruction disagreed about the same
    events, with the in-run one permissive.

    Measured before the fix: with a cap of 3 and an alternating
    failure/success sequence, SIX broker calls go out before `within_caps`
    blocks the seventh — and the alternation keeps the consecutive-failure
    breaker permanently reset, so neither limit stops it.
    """
    import near_close_dca

    day = date(2026, 8, 25)
    config = StrategyConfig(max_orders_per_day=3)

    def _attempts_possible(charge_failures):
        state = StrategyState()
        attempts = 0
        for index in range(12):
            if not near_close_dca.within_caps(1.0, state, day, config):
                break
            attempts += 1
            # A DISTINCT ticker per candidate, as a real batch loop has: the
            # charge is gated per (ticker, trading_day), exactly like the
            # success path's own `own_orders_recorded` check.
            ticker = f"TCK{index}"
            if index % 2 == 0:
                if charge_failures:
                    key = (ticker, day)
                    if key not in state.own_orders_recorded:
                        state.record_buy(ticker, day, 0.0)
                        state.own_orders_recorded.add(key)
            else:
                state.record_buy(ticker, day, 1.0)
        return attempts, state

    # The defect, kept as an explicit measurement rather than a claim.
    uncharged, _ = _attempts_possible(charge_failures=False)
    assert uncharged == 6

    # ...and a REPEATED call for the SAME ticker is charged too. An in-process
    # retry reusing the same `StrategyState` calls the broker again, and the
    # first version gated the charge on `own_orders_recorded` — a set that
    # answers "has artifact reconstruction accounted for this ticker?", not
    # "has this broker call been counted?". Measured: eight calls for one
    # ticker against a cap of 3 left the counter at 1 (Codex review, PR #786:
    # "Count repeated broker calls when state is reused").
    repeated = StrategyState()
    repeated_calls = 0
    for _ in range(8):
        if not near_close_dca.within_caps(1.0, repeated, day, config):
            break
        repeated_calls += 1
        repeated.record_buy("AAPL", day, 0.0)
        repeated.own_orders_recorded.add(("AAPL", day))
    assert repeated_calls == config.max_orders_per_day
    assert repeated.day_order_count(day) == config.max_orders_per_day
    # Zero notional throughout — a repeated FAILED call moves only the attempt
    # counter. (The success path keeps its guard for exactly this reason:
    # re-booking a duplicate recovery would inflate real spend.)
    assert repeated.day_notional(day) == pytest.approx(0.0)
    # The seeding dedup still holds, which is what that set is actually for.
    assert ("AAPL", day) in repeated.own_orders_recorded

    # Charged, the cap holds: three broker-facing attempts, then blocked.
    charged, state = _attempts_possible(charge_failures=True)
    assert charged == config.max_orders_per_day
    assert state.day_order_count(day) == config.max_orders_per_day
    # ZERO notional for the failures — nothing was bought, only the attempt
    # counters move. Two of the three charged attempts here were successes.
    assert state.day_notional(day) == pytest.approx(1.0)

    # And the batch loop is what charges it, at the one place that already
    # means "this candidate's own placement reached the broker and produced no
    # order" — not at each of the twelve sites that set the marker.
    import inspect

    source = inspect.getsource(near_close_dca._process_buy_candidates_batch)
    charge_block = source.split("if raw_broker_call_attempted:", 1)[1].split(
        "if raw_broker_placement_failed:", 1
    )[0]
    assert "record_buy(decision.ticker, ctx.trading_day, 0.0)" in charge_block
    assert "own_orders_recorded" in charge_block
    # ...and the charge is NOT gated on that set — only the seeding dedup is.
    assert "if _attempt_key not in ctx.state.own_orders_recorded" not in charge_block

    # ...and it is keyed on "did a broker call happen?", NOT on the broker-HEALTH
    # marker. Those are different questions, and the health marker is
    # deliberately withheld when the order history could not be read — so
    # charging the cap off it meant an order-history outage during placement
    # errors sent the whole batch out with the counter still at zero (Codex
    # review, PR #786: "Count attempts when reconciliation history is
    # unreadable"). The breaker keeps the health marker; only the cap moved.
    assert "raw_broker_call_attempted" in source
    breaker_block = source.split("if raw_broker_placement_failed:", 1)[1]
    assert "consecutive_placement_failures += 1" in breaker_block.split("\n\n", 1)[0]
    assert "record_buy" not in breaker_block.split("consecutive_placement_failures += 1", 1)[0]

    # The one producer that sets them apart: the placement `except` sets
    # `broker_placement_failed` to whether the history was READABLE, and
    # `broker_call_attempted` unconditionally — reaching that block at all
    # means `place_market_buy` was called.
    candidate_source = inspect.getsource(near_close_dca._process_buy_candidate)
    assert '"broker_placement_failed": history_readable,' in candidate_source
    assert '"broker_call_attempted": True,' in candidate_source


def test_an_unreadable_order_history_still_charges_the_daily_attempt_cap():
    """Codex review, PR #786 ("Count attempts when reconciliation history is
    unreadable").

    Round 55 charged the cap off `broker_placement_failed` — the broker-HEALTH
    marker, which the placement `except` sets to `history_readable` on purpose,
    since an unreachable order history says nothing about whether the broker is
    refusing placements. So when `place_market_buy` raised AND the history
    lookup raised too, a real broker call was made and NEITHER the daily
    attempt counter nor the breaker advanced.

    Measured before the fix: an order-history outage sent all ten candidates
    out against a cap of 3, counter still at 0.
    """
    import near_close_dca

    day = date(2026, 8, 25)
    config = StrategyConfig(max_orders_per_day=3)

    def _attempts_possible(charge_on_broker_call):
        state = StrategyState()
        attempts = 0
        for index in range(12):
            if not near_close_dca.within_caps(1.0, state, day, config):
                break
            attempts += 1
            ticker = f"TCK{index}"
            # place_market_buy WAS called and raised; the history lookup also
            # raised, so history_readable is False and the health marker is too.
            raw_broker_placement_failed = False
            raw_broker_call_attempted = True or raw_broker_placement_failed
            if charge_on_broker_call and raw_broker_call_attempted:
                key = (ticker, day)
                if key not in state.own_orders_recorded:
                    state.record_buy(ticker, day, 0.0)
                    state.own_orders_recorded.add(key)
        return attempts

    # The defect, kept as an explicit measurement.
    assert _attempts_possible(charge_on_broker_call=False) == 12
    # Charged on the broker call itself, the cap holds.
    assert _attempts_possible(charge_on_broker_call=True) == config.max_orders_per_day


def test_seeding_does_not_double_charge_a_failure_this_run_already_counted():
    """The pairing with `own_orders_recorded` is what keeps the new charge from
    being counted twice.

    `_seed_prior_allocations` reconstructs today from artifacts, and the failed
    attempt charged in-run is about to write exactly such a row. That pass is
    gated on `own_orders_recorded`, so a ticker this run already charged is not
    topped up again — the same guard the success path and the
    duplicate-recovery path rely on.
    """
    import near_close_dca

    day = date(2026, 8, 25)
    state = StrategyState()
    # What the batch loop now does for a failed broker call.
    state.record_buy("AAPL", day, 0.0)
    state.own_orders_recorded.add(("AAPL", day))
    assert state.day_order_count(day) == 1

    near_close_dca._seed_prior_allocations(
        state,
        day,
        True,
        {"AAPL": near_close_dca.PriorSameDayAllocation(
            ticker="AAPL", amount_usd=0.0, attempt_count=1,
        )},
        one_order_per_ticker_per_day=True,
    )
    assert state.day_order_count(day) == 1, "the artifact row was charged twice"


def test_a_recovered_fill_above_the_approved_amount_is_not_booked_as_the_request():
    """Codex review, PR #786 ("Validate recovered execution amounts before
    booking").

    On an accepted verdict the caller books the REQUESTED amount, so a
    recovered history row reporting MORE spend than the caps approved was
    accepted and then under-booked: `{"id": "ORD", "state": "filled",
    "executed_notional": 100}` for a requested $2 recorded $2 and left $98
    outside deployment, buying-power and every later candidate's cap check.

    Same rule as the placement classifier's executed-notional ceiling and its
    supplied-but-unreadable test, reaching the reconciliation path — which
    predates both.
    """
    import near_close_dca

    class _Ctx:
        trading_day = date(2026, 8, 25)

        class broker:
            row = None

            @staticmethod
            def same_day_order_with_ref_id_prefix_any_state(ticker, day, refs):
                return _Ctx.broker.row

    def verdict(row, approved=2.0):
        _Ctx.broker.row = row
        return near_close_dca._reconcile_ambiguous_fractional_placement(
            _Ctx, "AAPL", "REF-AAPL", approved
        )

    UNKNOWN = near_close_dca._PLACEMENT_UNKNOWN
    ACCEPTED = near_close_dca._PLACEMENT_ACCEPTED

    for field in ("executed_notional", "executed_dollar_amount", "filled_notional"):
        # Above what the caps approved — unknown, and the id is CARRIED so the
        # order stays reconcilable by hand rather than being written off.
        for value in (100, 2.02):
            assert verdict({"id": "ORD", "state": "filled", field: value}) == (
                UNKNOWN, "ORD",
            ), (field, value)
        # Supplied but unreadable, or negative: cannot establish the spend.
        for value in (True, "unknown", float("nan"), float("inf"), {}, [], -5):
            assert verdict({"id": "ORD", "state": "filled", field: value}) == (
                UNKNOWN, "ORD",
            ), (field, value)
        # At or below it: an ordinary recovered fill.
        for value in (2.0, 1.5, 0, 2.0005):
            assert verdict({"id": "ORD", "state": "filled", field: value}) == (
                ACCEPTED, "ORD",
            ), (field, value)

    # ABSENT is not evidence of anything — a `queued` order has not executed,
    # and halting on its silence would refuse every ordinary recovery.
    assert verdict({"id": "ORD", "state": "queued"}) == (ACCEPTED, "ORD")
    assert verdict({"id": "ORD", "state": "filled"}) == (ACCEPTED, "ORD")

    # With no approved amount to compare against, the check cannot run and the
    # verdict is unchanged — the caller always passes one.
    assert verdict(
        {"id": "ORD", "state": "filled", "executed_notional": 100}, None
    ) == (ACCEPTED, "ORD")


def test_a_mapping_shaped_execution_figure_is_a_number_not_an_unreadable_value():
    """A Robinhood money value is a scalar OR an ``{"amount": ...}`` mapping.

    `_recovered_execution_exceeds_approved` restated the executed-notional
    ceiling instead of reusing this module's own money reader, and lost the
    mapping form with it: measured, `executed_notional={"amount": "1.50"}`
    against an approved $2 — a perfectly ordinary fill, well inside the cap —
    returned True and turned an ordinary recovery into UNKNOWN. That is the
    OVER-halt direction this PR exists to prevent, and `_finite_nonnegative_float`
    had already been fixed for this exact shape once (Codex review: "Parse
    mapping-shaped executed notionals") before it was reintroduced one module
    over by restating rather than reusing (Codex review, PR #786: "Parse
    supported mapping-shaped execution notionals").

    The absent-versus-supplied distinction round 57 drew has to survive the
    change: `None` back from the parser still means SUPPLIED AND UNREADABLE,
    because an absent key returns before it is ever called.
    """
    import near_close_dca

    exceeds = near_close_dca._recovered_execution_exceeds_approved

    for field in ("executed_notional", "executed_dollar_amount", "filled_notional"):
        # The mapping form is a NUMBER. Inside the cap, so nothing is wrong.
        for value in ({"amount": "1.50"}, {"amount": 1.5}, {"amount": "2.00"}):
            assert exceeds({field: value}, 2.0) is False, (field, value)
        # ...and it is read as one when it is ABOVE the cap, too — the old code
        # also said True here, but for the wrong reason (unreadable, not
        # oversized), which is a different verdict the moment anyone reads why.
        assert exceeds({field: {"amount": "100.00"}}, 2.0) is True, field
        # A mapping carrying no amount at all is unreadable, not zero.
        assert exceeds({field: {}}, 2.0) is True, field
        assert exceeds({field: {"nope": 1}}, 2.0) is True, field
        # Round 57's distinction, unchanged by the new parser.
        for value in (True, "unknown", float("nan"), float("inf"), -5, [], 10**1000):
            assert exceeds({field: value}, 2.0) is True, (field, value)
        assert exceeds({}, 2.0) is False, field

    # The parser this now delegates to is the one that documents the shape.
    assert near_close_dca._finite_nonnegative_float({"amount": "1.50"}) == 1.5

    # SWEPT, not patched: the sibling helper deciding "may this have executed?"
    # read the same shape through a bare `float()` and raised out of it, so
    # `{"amount": "0"}` — an EXPLICIT zero, the one figure that rules execution
    # out — was read as unreadable and an ordinary declined order came back
    # UNKNOWN instead of NO_ORDER.
    executed = near_close_dca._reconciled_order_may_have_executed
    for field in ("executed_notional", "executed_dollar_amount", "filled_notional", "cumulative_quantity"):
        assert executed({field: {"amount": "0"}}) is False, field
        assert executed({field: {"amount": 0}}) is False, field
        assert executed({field: {"amount": "5"}}) is True, field
        # A container carrying no readable amount is unreadable, not zero.
        assert executed({field: {}}) is True, field
        assert executed({field: {"amount": None}}) is True, field
        assert executed({field: {"amount": "unknown"}}) is True, field


def test_reconciled_execution_check_inspects_every_terminal_notional_alias():
    """Codex review, PR #786 ("Inspect every terminal execution-notional
    alias").

    `_reconciled_order_may_have_executed` only checked `executed_notional`
    and `cumulative_quantity`, while its sibling
    `_recovered_execution_exceeds_approved` already recognizes
    `dca_runner._EXECUTED_NOTIONAL_KEYS`'s other two spellings --
    `executed_dollar_amount` and `filled_notional`. A placement that raises
    whose exact-reference history row is `{"id": "ORD", "state": "rejected",
    "filled_notional": 5}` found nothing here, read as NO execution, and
    `_reconcile_ambiguous_fractional_placement` returned `no_order` --
    letting the run continue while a real $5 of reported spend never
    reached deployment, buying-power, or cap accounting.
    """
    import near_close_dca

    executed = near_close_dca._reconciled_order_may_have_executed
    for field in ("executed_dollar_amount", "filled_notional"):
        assert executed({"id": "ORD", "state": "rejected", field: 5}) is True, field
        assert executed({"id": "ORD", "state": "rejected", field: 0}) is False, field
        assert executed({"id": "ORD", "state": "rejected", field: "unknown"}) is True, field
    # No execution-notional or quantity field present at all: not evidence
    # of anything, matching the existing "supplies none at all" contract.
    assert executed({"id": "ORD", "state": "rejected"}) is False


def test_a_recovered_working_order_is_checked_against_the_amount_it_was_placed_for():
    """"Is there a real order?" and "for how much?" are different questions,
    and a WORKING row can only answer the second one from its REQUESTED amount.

    Round 58 brought the executed-notional ceiling to this path, which settles
    a `filled` row. It does nothing for a `queued` one: nothing has executed
    yet, so there is no executed figure to read, while `dollar_amount` states
    the size of the live order outright. Measured before this check, an
    exact-reference row `{"id": "ORD", "state": "queued", "dollar_amount": 100}`
    recovered against an approved $2 returned ACCEPTED and the caller booked
    the REQUESTED $2, leaving a live $100 order outside deployment,
    buying-power and every later candidate's cap check (Codex review, PR #786:
    "Validate requested notional in reconciled live orders").

    EQUALITY here, not the ceiling the executed figure gets: a partial fill
    below the request is ordinary, but the amount an order was PLACED FOR is
    either the amount the caps approved or it is a different order.
    """
    import near_close_dca

    class _Ctx:
        trading_day = date(2026, 8, 25)

        class broker:
            row = None

            @staticmethod
            def same_day_order_with_ref_id_prefix_any_state(ticker, day, refs):
                return _Ctx.broker.row

    def verdict(row, approved=2.0):
        _Ctx.broker.row = row
        return near_close_dca._reconcile_ambiguous_fractional_placement(
            _Ctx, "AAPL", "REF-AAPL", approved
        )

    UNKNOWN = near_close_dca._PLACEMENT_UNKNOWN
    ACCEPTED = near_close_dca._PLACEMENT_ACCEPTED

    for field in ("dollar_amount", "notional"):
        for state in ("queued", "new", "confirmed", "partially_filled", "filled"):
            # Placed for more than the caps approved — the id is CARRIED, so a
            # human can reconcile the live order rather than have it written off.
            assert verdict({"id": "ORD", "state": state, field: 100}) == (
                UNKNOWN, "ORD",
            ), (field, state)
            # Placed for LESS is a mismatch too: this run placed exactly the
            # approved amount, so a different figure is a different order.
            assert verdict({"id": "ORD", "state": state, field: 1.0}) == (
                UNKNOWN, "ORD",
            ), (field, state)
            # Supplied but unreadable cannot establish agreement.
            for value in (True, "unknown", float("nan"), {}, [], -5):
                assert verdict({"id": "ORD", "state": state, field: value}) == (
                    UNKNOWN, "ORD",
                ), (field, state, value)
            # The approved amount, however the broker spells it.
            for value in (2.0, "2.00", 2.0005, {"amount": "2.00"}):
                assert verdict({"id": "ORD", "state": state, field: value}) == (
                    ACCEPTED, "ORD",
                ), (field, state, value)

    # ABSENT is not a mismatch, for the same reason it is not a fill: silence
    # about the amount is ordinary, and halting on it would refuse every
    # recovery whose history row omits the field.
    assert verdict({"id": "ORD", "state": "queued"}) == (ACCEPTED, "ORD")

    # Both halves are asked. A row that agrees on what it was placed for and
    # reports an oversized FILL is still unaccounted spend...
    assert verdict(
        {"id": "ORD", "state": "filled", "dollar_amount": 2.0, "executed_notional": 100}
    ) == (UNKNOWN, "ORD")
    # ...and one that agrees on both is an ordinary recovered fill.
    assert verdict(
        {
            "id": "ORD",
            "state": "filled",
            "dollar_amount": 2.0,
            "executed_notional": {"amount": "1.50"},
        }
    ) == (ACCEPTED, "ORD")

    # With no approved amount there is nothing to compare against.
    assert verdict({"id": "ORD", "state": "queued", "dollar_amount": 100}, None) == (
        ACCEPTED, "ORD",
    )


def test_a_failed_broker_call_is_never_marked_as_an_order_this_stream_booked():
    """`own_orders_recorded` means "this stream booked a REAL order for this
    ticker today". A broker call that produced no order is not one.

    Round 58 stopped GATING the attempt charge on that set but still ADDED to
    it "for the seeding dedup" — the same borrowed-predicate error one step
    smaller. Two live readers take the set at its word, and both were wrong for
    a failure:

    * `_seed_prior_allocations` skips any ticker in it, so a later scan finding
      the day's aggregate has GROWN — another stream bought the same ticker —
      folded in neither the extra dollars nor the extra attempt, and the shared
      caps stayed stale (Codex review, PR #786: "Keep failed-attempt markers
      out of allocation deduplication").
    * the duplicate-recovery site gates `record_buy(dup_amount)` on it, so a
      real recovered fill for a ticker whose earlier call failed never had its
      dollars booked at all.

    The dedup the marker was meant to buy is already bought, by a mechanism
    that MEASURES rather than guesses: `record_buy` makes `buys_today()`
    positive, and `reconcile_prior_allocation` takes that exact ticker-specific
    count as its attempt baseline and adds only the newly-discovered delta.
    """
    import near_close_dca

    day = date(2026, 8, 25)

    def alloc(amount, attempts):
        return near_close_dca.PriorSameDayAllocation(
            ticker="AAPL", amount_usd=amount, attempt_count=attempts
        )

    def seed(state, allocations):
        near_close_dca._seed_prior_allocations(
            state, day, True, allocations, one_order_per_ticker_per_day=True
        )

    def run(mark_the_failure):
        state = near_close_dca.StrategyState()
        seed(state, {"AAPL": alloc(2.0, 1)})     # an earlier slot's $2 attempt
        state.record_buy("AAPL", day, 0.0)       # this stream's failed call
        if mark_the_failure:
            state.own_orders_recorded.add(("AAPL", day))
        seed(state, {"AAPL": alloc(5.0, 2)})     # a later scan: the day grew
        return state

    # The defect: the marker froze the ticker's reconstruction where it stood.
    frozen = run(True)
    live = run(False)
    assert frozen.buys_today("AAPL", day) == 2
    assert live.buys_today("AAPL", day) == 3, (
        "a later scan discovering another stream's order must still fold in, "
        "which is what reconcile_prior_allocation exists to do"
    )
    assert (
        live.seeded_prior_context_attempts_by_ticker_day.get("AAPL", {}).get(day) == 2
    )

    # ...and the dedup survives WITHOUT the marker: re-scanning this failure's
    # own artifact row contributes nothing, however many times it is scanned.
    state = near_close_dca.StrategyState()
    state.record_buy("AAPL", day, 0.0)
    before = state.buys_today("AAPL", day)
    seed(state, {"AAPL": alloc(0.0, 1)})
    seed(state, {"AAPL": alloc(0.0, 1)})
    assert state.buys_today("AAPL", day) == before == 1

    # The other reader, at the duplicate-recovery site: a real recovered fill
    # for a ticker whose earlier call failed must still book its dollars.
    state = near_close_dca.StrategyState()
    state.record_buy("AAPL", day, 0.0)
    assert ("AAPL", day) not in state.own_orders_recorded
    if ("AAPL", day) not in state.own_orders_recorded:
        state.record_buy("AAPL", day, 2.0)
    assert state.day_notional(day) == 2.0

    # And the source no longer carries the add, so it cannot creep back.
    import inspect

    source = inspect.getsource(near_close_dca._process_buy_candidates_batch)
    charge = source.split("raw_broker_call_attempted:")[1].split("if raw_broker_placement_failed")[0]
    assert "record_buy(decision.ticker, ctx.trading_day, 0.0)" in charge
    assert "own_orders_recorded.add" not in charge, (
        "a failed broker call must not be marked as an order this stream booked"
    )


def test_an_oversized_reconciled_order_id_does_not_disable_the_breaker():
    """`math.isfinite()` converts to a float first, so a syntactically valid
    but astronomically large JSON integer (`10**1000`) raises `OverflowError`
    rather than returning a verdict — and `_is_usable_reconciled_order_id` sat
    inside a function this module never wraps in a try/except.

    Measured before the fix: the raise propagated out of
    `_reconcile_ambiguous_fractional_placement` entirely and was caught by its
    caller's "the history could not be read" handler, which sets
    `history_readable = False` and therefore withholds `broker_placement_failed`
    (that marker is deliberately keyed on READABILITY, not on what the history
    proved). So a broker returning this exact shape on every retry advanced
    only the daily-attempt cap and never tripped
    `max_consecutive_placement_failures` at all (Codex review, PR #786: "Guard
    oversized reconciled order IDs").
    """
    import near_close_dca

    for value in (10**1000, -(10**1000), float("nan"), float("inf")):
        assert near_close_dca._is_usable_reconciled_order_id(value) is False, value

    # Ordinary ids are unaffected.
    assert near_close_dca._is_usable_reconciled_order_id(12345) is True
    assert near_close_dca._is_usable_reconciled_order_id("ORD-1") is True

    # End to end: the oversized id must not raise out of the reconciliation
    # helper, and the row must come back UNKNOWN (visible but untrackable) —
    # not raise, which is what let the caller mislabel the history unreadable.
    from datetime import date

    class _Ctx:
        trading_day = date(2026, 8, 27)

        class broker:
            row = None

            @staticmethod
            def same_day_order_with_ref_id_prefix_any_state(ticker, day, refs):
                return _Ctx.broker.row

    _Ctx.broker.row = {"id": 10**1000, "state": "rejected", "cumulative_quantity": 0}
    verdict, order_id = near_close_dca._reconcile_ambiguous_fractional_placement(
        _Ctx, "AAPL", "REF-AAPL", 2.0
    )
    assert verdict == near_close_dca._PLACEMENT_UNKNOWN
    assert order_id is None


def test_fractional_reconciliation_scans_sibling_run_directories(tmp_path):
    """A fresh scheduled slot settles unresolved evidence in an older sibling."""
    import near_close_dca

    root = tmp_path / "scheduled_runs"
    prior = root / "20260827_1530ET"
    current = root / "20260828_1530ET"
    prior.mkdir(parents=True)
    current.mkdir()
    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    (prior / "run_state.json").write_text(json.dumps({
        "trading_day": now.date().isoformat(),
        "broker_outcomes": {"AAPL": {
            "ticker": "AAPL", "status": "failed",
            "placement_outcome_unproven": True,
            "placement_ref_id": "afternoon-AAPL-sibling",
            "submitted_amount_usd": 2.0,
        }},
    }), encoding="utf-8")

    class Broker(DryRunBroker):
        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, refs):
            return {"id": "ORD-SIBLING", "state": "queued", "dollar_amount": "2.00"}

    ctx = near_close_dca.RunContext(
        now=now,
        config=StrategyConfig(dry_run=False, approved_test_dates=config.approved_test_dates),
        state=state, finance_data=fin, robinhood_data=rh, broker=Broker(cash=500.0),
        artifacts_dir=current, deployment_history_root=root,
    )
    near_close_dca._reconcile_prior_unproven_fractional_placements(ctx)

    saved = json.loads((prior / "run_state.json").read_text(encoding="utf-8"))
    assert saved["broker_outcomes"]["AAPL"]["placement_outcome_unproven"] is False
    assert state.day_notional(now.date()) == 2.0


def test_fractional_reconciliation_booking_id_survives_state_reload(tmp_path):
    """A proven artifact's booking intent is replayed exactly once after restart."""
    import dca_runner
    import near_close_dca

    artifacts = tmp_path / "run"
    artifacts.mkdir()
    now, config, state, _broker, fin, rh = _compact_setup(tmp_path)
    (artifacts / "run_state.json").write_text(json.dumps({
        "trading_day": now.date().isoformat(),
        "broker_outcomes": {"AAPL": {
            "ticker": "AAPL", "status": "submitted",
            "placement_outcome_unproven": False,
            "placement_ref_id": "afternoon-AAPL-booking",
            "order_id": "ORD-BOOKING", "reconciliation_booking_pending": True,
            "reconciled_notional_usd": 2.0,
        }},
    }), encoding="utf-8")
    state_path = tmp_path / "strategy-state.json"
    ctx = near_close_dca.RunContext(
        now=now, config=StrategyConfig(dry_run=False, approved_test_dates=config.approved_test_dates),
        state=state, finance_data=fin, robinhood_data=rh, broker=DryRunBroker(cash=500.0),
        artifacts_dir=artifacts,
    )
    near_close_dca._reconcile_prior_unproven_fractional_placements(ctx)
    dca_runner.save_strategy_state(state, state_path)
    reloaded = dca_runner.load_strategy_state(state_path)
    near_close_dca._reconcile_prior_unproven_fractional_placements(replace(ctx, state=reloaded))
    assert reloaded.day_notional(now.date()) == 2.0
