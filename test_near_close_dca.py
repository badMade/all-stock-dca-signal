"""Self-tests for the graded DCA sizing ladder and slippage/coverage reporting.

Runnable with either ``python test_near_close_dca.py`` or ``pytest``. Boundary
percentages are converted to fractions before calling graded_buy_multiplier, which
expects a fraction of the previous close (2.0% down == 0.02).
"""

import csv
import json
import os
import pathlib
import tempfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from conftest import FIXTURE_ACCOUNT_NUMBER
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
    Action,
    AlpacaBroker,
    Bar,
    BrokerName,
    CompactRunOptions,
    DryRunBroker,
    OrderFill,
    PriorSameDayAllocation,
    ReconciledDecision,
    RunResult,
    SourceName,
    SourceSignal,
    StrategyConfig,
    StrategyState,
    _check_execution_gates,
    _earlier_selected_base,
    _graded_dca_ref_id_prefix,
    _iso_or_none,
    _process_buy_candidate,
    _same_stream_order_exists,
    _seed_prior_allocations,
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
    write_run_artifacts,
)
from scripts.run_classification import PriorAllocation

# (drop percent from prior close, expected multiplier).
LADDER_BOUNDARY_CASES = [
    (0.09, 0.0),  # below the 0.1% minimum -> no buy
    (0.1, 1.0),  # 1x tier begins
    (0.999, 1.0),  # still 1x just under 1.0%
    (1.0, 1.5),  # 1.5x tier begins
    (1.999, 1.5),  # still 1.5x just under 2.0%
    (2.0, 2.0),  # 2x tier begins
    (2.999, 2.0),  # still 2x just under 3.0%
    (3.0, 2.5),  # 2.5x tier begins
    (3.999, 2.5),  # still 2.5x just under 4.0%
    (4.0, 3.0),  # 3x tier begins
    (4.999, 3.0),  # still 3x just under 5.0%
    (5.0, 3.5),  # 3.5x tier begins
    (5.999, 3.5),  # still 3.5x just under 6.0%
    (6.0, 4.0),  # 4x tier begins
    (6.999, 4.0),  # still 4x just under 7.0%
    (7.0, 4.5),  # 4.5x tier begins
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


def test_ticker_universe():
    assert "IBM" in ACTIVE_TICKERS
    assert "TXN" in ACTIVE_TICKERS
    assert "RKLB" in ACTIVE_TICKERS
    assert "GFUZ" in ACTIVE_TICKERS
    assert "IRDM" in ACTIVE_TICKERS
    assert "GLW" in ACTIVE_TICKERS
    assert "IRDMI" not in ACTIVE_TICKERS
    assert "GLWR" not in ACTIVE_TICKERS
    assert "BYDDY" in EXCLUDED_TICKERS
    assert "BYDDY" not in ACTIVE_TICKERS
    for excluded in ("VCX", "TRLV"):
        assert excluded in EXCLUDED_TICKERS
        assert excluded not in ACTIVE_TICKERS


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
    # -> $1 base scale. Drop is 2% -> 2x multiplier -> $1 * 2 = $2 suggested.
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
    assert signal.buy_multiplier == 2.0
    assert signal.suggested_amount_usd == 1.0 * 2.0


def test_first_allocation_below_close_and_low_uses_two_dollar_base():
    # Decision price below both the prior close (100) and the prior low (95)
    # -> $2 base scale. Drop is 6% -> 4x multiplier -> $2 * 4 = $8 suggested.
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
    assert signal.buy_multiplier == 4.0
    assert signal.suggested_amount_usd == 2.0 * 4.0


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
    with open(result["run_state_path"], encoding="utf-8") as handle:
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
        assert by_ticker[ticker]["incremental_order_amount"] == 4.0


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
    merge_manual_top_up_context(prior_same_day_allocations, manual_context)
    merged = prior_same_day_allocations["AAPL"]
    assert merged.amount_usd == 3.0
    assert merged.selected_base_amount_usd is None
    assert merged.selected_base_amount_usd_at is None


def test_merge_manual_top_up_context_retains_base_when_manual_fill_is_later():
    # Codex review discussion_r3653976740 ("Preserve the known base when a
    # later manual fill is merged"): an earlier scheduled allocation resolved
    # a $1 base (below close only) at 10:30. A LATER (14:00) bare-CSV manual
    # fill, whose PriorAllocation.reference_ts now identifies its own time,
    # must not erase that known base -- it cannot itself be the true first
    # allocation since it happened after the base was already established.
    # Before the fix this unconditionally poisoned the base to None, so a
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
    merge_manual_top_up_context(prior_same_day_allocations, manual_context)
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
    merge_manual_top_up_context(prior_same_day_allocations, manual_context)
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
    merge_manual_top_up_context(prior_same_day_allocations, manual_context)
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
    merge_manual_top_up_context(prior_same_day_allocations, manual_context)
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
    merge_manual_top_up_context(prior_same_day_allocations, manual_context)
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
        config,
        state,
        broker,
        robinhood_data=None,
        now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 6, 29),
        guard_active=False,
        prior_same_day_allocations={},
        # Trivially over the kill-switch threshold (fraction * $10 allocation).
        running_deployed_7d=100.0,
        scheduled=False,
        run_result=None,
    )
    assert candidate_outcome["submitted_amount_usd"] == 1.0  # capped at $1, not $2


def test_kill_switch_caps_guarded_top_up_at_preserved_base():
    # Codex review ("Cap guarded top-ups using the preserved base"): an
    # earlier same-day allocation fixed AAPL's base at $1 (below close only).
    # A deeper afternoon price now crosses the prior low too, so THIS run's
    # own selected_base_amount_usd recomputes to $2 -- but the top-up guard
    # must still use the $1 preserved base, and so must the kill-switch cap.
    # Before the fix, the cap used decision.selected_base_amount_usd ($2)
    # instead, so a 5x signal's $4 guard delta got min'd against $2 instead of
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
        config,
        state,
        broker,
        robinhood_data=None,
        now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 6, 29),
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        # Trivially over the kill-switch threshold (fraction * $10 allocation).
        running_deployed_7d=100.0,
        scheduled=False,
        run_result=None,
    )
    # The guard's own $4 delta (($1 * 5x) - $1 already bought) must be capped
    # at the preserved $1 base, not this run's fresh $2 base.
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
        config,
        state1,
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        state2,
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=run_result,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
    )
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=starting_deployed_7d,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    placed_amount = broker.placed["AAPL"]
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=starting_deployed_7d,
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=starting_deployed_7d,
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    placed_amount = broker.placed["AAPL"]
    starting_deployed_7d = 50.0
    _, _, returned_deployed_7d, _, _ = _process_buy_candidate(
        decision,
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=starting_deployed_7d,
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
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
        config,
        fresh_state,
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        shared_state,
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
    )
    placed_amount = broker.placed["AAPL"]
    assert shared_state.buys_today("AAPL", trading_day) == 1
    assert shared_state.day_notional(trading_day) == pytest.approx(placed_amount)

    _process_buy_candidate(
        decision,
        config,
        shared_state,
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=narrow_spread_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
    )
    # Attempt 2 (retry): the spread has since widened past the gate's limit.
    # A wide/failing spread on the RETRY must not surface as this row's
    # verdict -- the original placement is what was actually authorized.
    wide_spread_data = _FakeDataWithBidAsk(
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(90.0, 110.0)
    )
    outcome2, _, _, _, _ = _process_buy_candidate(
        decision,
        config,
        StrategyState(),
        broker,
        robinhood_data=wide_spread_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
    class _NoIdDupBroker:
        def account_number(self):
            return FIXTURE_ACCOUNT_NUMBER

        def available_buying_power(self):
            return 1000.0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

        def same_day_strategy_order_exists_with_ref_id_prefix(
            self, ticker, trading_day, ref_id_prefixes
        ):
            return {"dollar_amount": 5.0}  # no id / order_id

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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
    )
    assert outcome["status"] == "blocked"
    assert outcome.get("order_id") is None
    assert errors_count_inc == 1


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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=None,
        now=now,
        trading_day=date(2026, 6, 29),
        guard_active=False,
        prior_same_day_allocations={},
        running_deployed_7d=0.0,
        scheduled=False,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=False,
        prior_same_day_allocations={},
        running_deployed_7d=0.0,
        scheduled=False,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
    )
    assert scheduled_outcome.get("duplicate_preserved") is not True, (
        "the manual fill's ref_id must not be mistaken for this scheduled "
        "run's own earlier attempt"
    )
    assert scheduled_outcome["status"] == "submitted"
    assert (
        len(broker._orders) == 2
    ), "the scheduled run must place its own real incremental order"


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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=date(2026, 6, 29),
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=None,
        now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 6, 29),
        guard_active=False,
        prior_same_day_allocations={},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
        stream="midday",
    )

    assert outcome["status"] == "submitted"
    assert outcome.get("duplicate_preserved") is not True
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
        stream="midday",
    )

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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=trading_day,
        guard_active=True,
        prior_same_day_allocations={"AAPL": prior},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
        stream="midday",
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
        _same_stream_order_exists(_LegacyBroker(), "AAPL", date(2026, 6, 29))


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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=date(2026, 6, 29),
        guard_active=False,
        prior_same_day_allocations={},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=date(2026, 6, 29),
        guard_active=False,
        prior_same_day_allocations={},
        running_deployed_7d=0.0,
        scheduled=True,
        run_result=None,
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
        config,
        StrategyState(),
        broker,
        robinhood_data=robinhood_data,
        now=now,
        trading_day=date(2026, 6, 29),
        guard_active=False,
        prior_same_day_allocations={},
        running_deployed_7d=0.0,
        scheduled=False,
        run_result=None,
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
    assert report.metrics["missing_fill_reasons"][MISSING_FILL_DATA_ERROR] == 1
    for key in [
        "mean_abs_slippage_pct",
        "median_abs_slippage_pct",
        "p90_abs_slippage_pct",
        "net_slippage_cost",
    ]:
        assert key in report.metrics


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

    assert os.path.exists(result["slippage_report_path"])
    assert os.path.exists(result["orders_csv_path"])
    assert os.path.exists(result["state_path"])

    report_text = open(result["slippage_report_path"], encoding="utf-8").read()
    assert "DCA Manual Run Slippage and Coverage Report" in report_text

    with open(result["state_path"], encoding="utf-8") as handle:
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
    csv_text = open(result["orders_csv_path"], encoding="utf-8").read()
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
    # $4 (2x the $2 base) with a $7.50 monthly allocation (40% threshold = $3)
    # mean the FIRST submitted order alone already crosses the threshold. The
    # SECOND candidate in the same run must then be capped to the $2 base
    # instead of keeping its uncapped 2x amount -- otherwise a volatile day
    # could overshoot the guard well beyond the base amount before the guard
    # is even visible to tomorrow's run.
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAA", "BBB"),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        monthly_cash_allocation_usd=7.5,
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
    # its full 2x amount, which alone crosses the $3 threshold.
    assert by_ticker["AAA"]["planned_amount_usd"] == 4.0
    assert by_ticker["AAA"]["buy_multiplier"] == 2.0
    # BBB, priced after the threshold is crossed, is capped to the $2 base --
    # even though its ladder multiplier (still reported for audit purposes)
    # remains 2.0, showing the cap is a dollar-amount guard, not a multiplier
    # rewrite.
    assert by_ticker["BBB"]["planned_amount_usd"] == 2.0
    assert by_ticker["BBB"]["buy_multiplier"] == 2.0


def test_manual_run_submissions_do_not_advance_in_batch_burn(tmp_path):
    # Same setup as the scheduled in-batch test above (AAA/BBB, $7.50 monthly
    # allocation), but scheduled=False: a manual run's own submissions must
    # never advance the scheduled-live 7-day figure the kill-switch reads, so
    # neither candidate is capped even though AAA's amount alone would cross
    # the $3 threshold if counted.
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-manual")
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAA", "BBB"),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        monthly_cash_allocation_usd=7.5,
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
    assert by_ticker["AAA"]["planned_amount_usd"] == 4.0
    assert by_ticker["BBB"]["planned_amount_usd"] == 4.0  # NOT capped to the $2 base
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

    run_state = json.loads((d / "run_state.json").read_text())
    summary = json.loads((d / "run_summary.json").read_text())
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

    run_state = json.loads(Path(artifacts_dir, "run_state.json").read_text())
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

    persisted = json.loads((d / "run_state.json").read_text())
    assert persisted["status"] == "blocked"
    assert (
        persisted["artifact_contract_failure"]["failure"] == "artifact_contract_failure"
    )
    assert persisted["run_classification"]["valid_scheduled_evidence"] is False
    # both direct-discovery consumers now refuse the rejected run
    assert derive_valid_scheduled_evidence(persisted) is False
    assert load_json_files([d / "run_state.json"]) == []


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
    assert not latest_scheduled_run_path(classification_root).exists()


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

    pointer = latest_scheduled_run_path(classification_root)
    assert pointer.exists()
    ptr = json.loads(pointer.read_text())
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

    assert not latest_scheduled_run_path(classification_root).exists()


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
    payload = json.loads(pointer.read_text(encoding="utf-8"))
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


def _afternoon_merge(preserved, placed, exc=None, tmp_path=None):
    """Drive _write_afternoon_failure_artifacts over a preserved state."""
    import near_close_dca as _n

    if exc is None:
        exc = RuntimeError("boom")

    out = pathlib.Path(tmp_path) / "slot"
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_state.json").write_text(json.dumps(preserved), encoding="utf-8")
    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = placed
    try:
        _n._write_afternoon_failure_artifacts(out, _now_et(), exc)
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)
    return out, json.loads((out / "run_state.json").read_text(encoding="utf-8"))


def _now_et():
    return datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)


def _preserved_state():
    return {
        "run_id": "earlier",
        "trading_day": "2026-06-29",
        "status": "completed",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
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

    summary = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
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
        max_per_order_usd=15.0,
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
    assert candidate["planned_amount_usd"] == 20.0
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
    # moved, so the ticker must carry no prior allocation at all.
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

    # Rejected with zero execution: no capital moved, so no prior allocation.
    assert "EVR" not in allocations
    # The genuine fill in the same CSV is unaffected.
    assert allocations["SPCX"].amount_usd == 3.50
    assert allocations["SPCX"].reference_price == 109.9899


def test_terminal_non_fill_states_match_the_canonical_broker_set():
    # Codex review: "Treat voided and expired broker orders as terminal". The
    # set was hand-copied into two modules and BOTH copies omitted 'voided' and
    # 'expired', so an order that could no longer fill was carried as live
    # unknown context and forced its ticker onto the $0 manual-review path.
    # Both readers now source the canonical set, so a future addition to the
    # broker normalizer reaches them without a third edit.
    import sys

    from near_close_dca import _TERMINAL_NON_FILL_STATES
    from scripts.broker_reconciliation import _TERMINAL_ORDER_STATES

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
    from run_classification import _TERMINAL_NON_FILL_STATES as rc_terminal

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
        ),
    )
    allocations = load_prior_same_day_allocations_multi(
        (morning_root, midday_root), date(2026, 6, 29)
    )
    assert allocations["AAPL"].amount_usd == 7.5
    assert allocations["AAPL"].reference_price == 99.0


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
            ]
        ),
    )
    midday_root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        midday_root,
        "20260629_1230ET",
        _manual_run_state(
            [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 4.5},
            ]
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
        max_per_order_usd=15.0,
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
    # Codex/Gemini review on PR #53: Path.read_text() can raise
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
    # 100 -> 98 is a 2% drop -> 2x -> $4 desired. Morning bought $2 at 99.0; the
    # afternoon quote is 98.0 (< 99.0) so only the $2 difference is bought.
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
    assert aapl["incremental_order_amount"] == 2.0
    assert aapl["afternoon_desired_amount"] == 4.0
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
    assert by_ticker["NVDA"]["incremental_order_amount"] == 4.0
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
    # Fix 2: prior amounts are seeded into the ledger before the cap check,
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
    assert by_ticker["AAPL"]["incremental_order_amount"] == 2.0
    # NVDA has NO loaded prior allocation, so the broker's same-day-order check
    # must still apply -- the guard bypass is scoped per-ticker, not blanket.
    assert by_ticker["NVDA"]["status"] == "skipped"
    assert by_ticker["NVDA"]["skip_reason"] == "same-day broker order exists"


# --- Fix 1: planned_amount_usd reflects the guarded incremental amount --------


def test_build_run_artifacts_uses_guarded_amount_as_planned_notional(tmp_path):
    """build_run_artifacts must set planned_amount_usd = incremental_order_amount
    (not the full afternoon desired amount) when a top-up guard outcome is present.
    The original desired amount must be preserved in afternoon_desired_amount_usd."""
    from near_close_dca import (
        TOP_UP_INCREMENTAL,
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
        top_up_outcomes={"AAPL": guard},
    )
    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert (
        candidates["AAPL"]["planned_amount_usd"] == 3.0
    ), "planned_amount_usd must equal incremental_order_amount, not the full desired amount"
    assert (
        candidates["AAPL"]["afternoon_desired_amount_usd"] == 6.0
    ), "original desired amount must be preserved in afternoon_desired_amount_usd"


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
        top_up_outcomes={"AAPL": guard},
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
        top_up_outcomes={"AAPL": guard},
    )
    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert candidates["AAPL"]["selected_base_amount_usd"] == 1.0
    assert candidates["AAPL"]["decision_bar_close"] == 93.0


# --- Fix 2: prior same-day allocations count against the daily/weekly cap ----


def test_prior_allocations_count_against_cap(tmp_path):
    """When the guard is active, prior same-day amounts must be seeded into the
    budget ledger so that prior + incremental spend together cannot exceed the cap.

    Setup: 1 ticker (AAPL), base=$190, 2% drop -> 2x -> suggested=$380 each,
    which triggers stress tier ($360/day). Prior=$200 at $99; afternoon=$98 (below
    prior -> incremental = $380 - $200 = $180).

    Without Fix 2: remaining = $360 (no seeding). $180 < $360 -> cap passes (BUG).
    With    Fix 2: remaining = $360 - $200 = $160. $180 > $160 -> cap blocks (correct).
    """
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAPL",),
        approved_test_dates=frozenset({date(2026, 6, 29)}),
        base_buy_amount_usd=190.0,  # 190 * 2x = $380 > $360 stress cap
    )
    state = StrategyState()
    prior = {
        "AAPL": PriorSameDayAllocation(
            "AAPL",
            amount_usd=200.0,
            reference_price=99.0,
            run_id="manual_am",
            source="manual",
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
    # (Without Fix 2, remaining = $360 and $180 < $360 -> would slip through.)
    aapl = candidates["AAPL"]
    assert aapl["status"] in ("blocked", "skipped"), (
        f"AAPL should be blocked by cap (prior + incremental > cap); "
        f"got status={aapl['status']!r}, skip_reason={aapl.get('skip_reason')!r}"
    )


# --- Fix 3: load_prior_same_day_allocations reads full-report run_state ------


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
    # POSIX permission bits. Either environment would let read_text() succeed, so a
    # regression that removed the OSError handler would still pass silently.
    # Creating a directory with the matching filename raises IsADirectoryError (an
    # OSError subclass) from read_text() deterministically on every platform and
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
        (_Path(__file__).parent / "schemas" / "run_state.schema.json").read_text(
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

    summary = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    assert "stale" not in summary
    assert summary["run_classification"] is not None
    assert (out / "signals.csv").read_text(encoding="utf-8").strip() != ""
    assert "stale" not in (out / "submitted_orders.csv").read_text(encoding="utf-8")
    # The repaired set validates, so the handler no longer denies an artifact set
    # it could have fixed.
    assert validate_run_artifacts(out, require_summary=True)
    run_state = json.loads((out / "run_state.json").read_text(encoding="utf-8"))
    assert "artifact_contract_failure" not in run_state


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
    run_state = json.loads((out / "run_state.json").read_text(encoding="utf-8"))
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
        cwd=_Path(__file__).resolve().parent,
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


class _InjectedBroker:
    def __init__(self, account: str) -> None:
        self._account = account

    def account_number(self) -> str:
        return self._account


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
        max_per_order_usd=15.0,
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
    assert by_ticker["AAPL"]["incremental_order_amount"] == 2.0


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
