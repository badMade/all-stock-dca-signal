"""Self-tests for the graded DCA sizing ladder and slippage/coverage reporting.

Runnable with either ``python test_near_close_dca.py`` or ``pytest``. Boundary
percentages are converted to fractions before calling graded_buy_multiplier, which
expects a fraction of the previous close (2.0% down == 0.02).
"""

import csv
import json
import os
import tempfile
from datetime import date, datetime, time, timedelta

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
    TOP_UP_MISSING_PRIOR_PRICE,
    TOP_UP_NO_INCREMENTAL,
    TOP_UP_NO_PRIOR_RUN,
    TOP_UP_PRICE_NOT_BELOW,
    USAGE_PROXY_FIELDS,
    Action,
    Bar,
    DryRunBroker,
    PriorSameDayAllocation,
    ReconciledDecision,
    RunResult,
    SourceName,
    SourceSignal,
    StrategyConfig,
    StrategyState,
    _earlier_selected_base,
    _iso_or_none,
    _process_buy_candidate,
    allocation_sort_key,
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
    (0.09, 0.0),   # below the 0.1% minimum -> no buy
    (0.1, 1.0),    # 1x tier begins
    (0.999, 1.0),  # still 1x just under 1.0%
    (1.0, 1.5),    # 1.5x tier begins
    (1.999, 1.5),  # still 1.5x just under 2.0%
    (2.0, 2.0),    # 2x tier begins
    (2.999, 2.0),  # still 2x just under 3.0%
    (3.0, 2.5),    # 2.5x tier begins
    (3.999, 2.5),  # still 2.5x just under 4.0%
    (4.0, 3.0),    # 3x tier begins
    (4.999, 3.0),  # still 3x just under 5.0%
    (5.0, 3.5),    # 3.5x tier begins
    (5.999, 3.5),  # still 3.5x just under 6.0%
    (6.0, 4.0),    # 4x tier begins
    (6.999, 4.0),  # still 4x just under 7.0%
    (7.0, 4.5),    # 4.5x tier begins
    (7.999, 4.5),  # still 4.5x just under 8.0%
    (8.0, 5.0),    # 5x tier begins
    (12.0, 5.0),   # no upper cutoff, still 5x
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
    assert (normal.name, normal.daily_cap_usd, normal.weekly_cap_usd) == ("normal", 180.0, 900.0)
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
        ts = datetime.combine(as_of.date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
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
    def __init__(self, source, close_price, prev_close, quote=None, bid_ask=(97.0, 99.0)):
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
    data = _FakeData(SourceName.FINANCE, close_price=98.0, prev_close=100.0, quote=(97.5, quote_time))
    signal = evaluate_source_signal("AAPL", now, StrategyConfig(), data, interval_minutes=15)
    # target (quote) is distinct from the completed decision-bar close.
    assert signal.quote_price == 97.5
    assert signal.decision_bar_close == 98.0
    assert signal.quote_time == quote_time


def test_target_price_falls_back_to_bar_close_without_quote():
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeData(SourceName.FINANCE, close_price=98.0, prev_close=100.0, quote=None)
    signal = evaluate_source_signal("AAPL", now, StrategyConfig(), data, interval_minutes=15)
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
        ts = datetime.combine(as_of.date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
        # open/high sit at/above the close; low is the distinct prior-session low.
        return Bar(ts, self._prev, self._prev, self._prev_low, self._prev, 1000)


def test_first_allocation_below_close_only_uses_one_dollar_base():
    # Decision price below the prior close (100) but not below the prior low (95)
    # -> $1 base scale. Drop is 2% -> 2x multiplier -> $1 * 2 = $2 suggested.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(SourceName.FINANCE, close_price=98.0, prev_close=100.0, prev_low=95.0)
    signal = evaluate_source_signal("AAPL", now, StrategyConfig(), data, interval_minutes=15)
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
    data = _FakeDataWithLow(SourceName.FINANCE, close_price=94.0, prev_close=100.0, prev_low=95.0)
    signal = evaluate_source_signal("AAPL", now, StrategyConfig(), data, interval_minutes=15)
    assert signal.action == Action.BUY
    assert signal.previous_low == 95.0
    assert signal.base_selection_reason == BASE_BELOW_CLOSE_AND_LOW
    assert signal.selected_base_amount_usd == 2.0
    assert signal.buy_multiplier == 4.0
    assert signal.suggested_amount_usd == 2.0 * 4.0


def test_first_allocation_above_close_is_no_buy():
    # Decision price above the prior close -> no buy, $0 base, zeroed amount.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(SourceName.FINANCE, close_price=101.0, prev_close=100.0, prev_low=95.0)
    signal = evaluate_source_signal("AAPL", now, StrategyConfig(), data, interval_minutes=15)
    assert signal.action != Action.BUY
    assert signal.selected_base_amount_usd == 0.0
    assert signal.suggested_amount_usd == 0.0


def test_first_allocation_missing_prior_low_treated_as_below_close_only():
    # A non-positive/missing prior low is treated conservatively as not-below-low,
    # so the $1 (below-close-only) scale is selected even on a deep drop.
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FakeDataWithLow(SourceName.FINANCE, close_price=94.0, prev_close=100.0, prev_low=0.0)
    signal = evaluate_source_signal("AAPL", now, StrategyConfig(), data, interval_minutes=15)
    assert signal.action == Action.BUY
    assert signal.base_selection_reason == BASE_BELOW_CLOSE_ONLY
    assert signal.selected_base_amount_usd == 1.0


def test_select_first_allocation_base_helper_branches():
    config = StrategyConfig()
    assert select_first_allocation_base_usd(94.0, 100.0, 95.0, config) == (2.0, BASE_BELOW_CLOSE_AND_LOW)
    assert select_first_allocation_base_usd(98.0, 100.0, 95.0, config) == (1.0, BASE_BELOW_CLOSE_ONLY)
    assert select_first_allocation_base_usd(101.0, 100.0, 95.0, config) == (0.0, BASE_NOT_BELOW_CLOSE)
    assert select_first_allocation_base_usd(100.0, 100.0, 95.0, config) == (0.0, BASE_NOT_BELOW_CLOSE)
    # Missing prior low -> conservative below-close-only ($1).
    assert select_first_allocation_base_usd(94.0, 100.0, None, config) == (1.0, BASE_BELOW_CLOSE_ONLY)
    # Per-order ceiling holds: deepest drop (5x) on the $2 scale tops out at $10.
    assert 2.0 * 5.0 == config.max_per_order_usd


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
        "AAPL": PriorAllocation(ticker="AAPL", prior_usd=1.0, reference_price=99.0, sources=["manual_csv"]),
    }
    merge_manual_top_up_context(prior_same_day_allocations, manual_context)
    merged = prior_same_day_allocations["AAPL"]
    assert merged.amount_usd == 3.0
    assert merged.selected_base_amount_usd is None
    assert merged.selected_base_amount_usd_at is None


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
    assert decision.selected_base_amount_usd == 2.0  # finance's base, not Robinhood's $1


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
    assert decision.selected_base_amount_usd * decision.buy_multiplier == decision.suggested_amount_usd


def test_earlier_selected_base_picks_by_timestamp_not_argument_order():
    # Codex review ("Select the base from the chronologically first allocation"):
    # the later (15:05) allocation is passed as `existing` (the position that
    # previously always won); the earlier (09:45) allocation must still win.
    later_as_existing = _earlier_selected_base(
        2.0, "2026-06-29T15:05:00-04:00",  # existing: later, would previously win
        1.0, "2026-06-29T09:45:00-04:00",  # alloc: earlier
    )
    assert later_as_existing == (1.0, "2026-06-29T09:45:00-04:00")
    # Same two allocations, swapped argument positions -> same (earlier) answer.
    earlier_as_existing = _earlier_selected_base(
        1.0, "2026-06-29T09:45:00-04:00",
        2.0, "2026-06-29T15:05:00-04:00",
    )
    assert earlier_as_existing == (1.0, "2026-06-29T09:45:00-04:00")
    # Missing/unparseable timestamps fall back to preferring `existing` (the
    # pre-chronological-comparison default) rather than guessing at ordering.
    assert _earlier_selected_base(2.0, None, 1.0, "2026-06-29T09:45:00-04:00") == (2.0, None)
    assert _earlier_selected_base(None, None, 1.0, "2026-06-29T09:45:00-04:00") == (1.0, "2026-06-29T09:45:00-04:00")


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
                None if rh_drop is None
                else SimpleNamespace(drop_vs_previous_close_pct=rh_drop)
            ),
        )

    def order(cs):
        return [c.ticker for c in sorted(cs, key=allocation_sort_key)]

    # Equal multiplier and drop -> ticker alphabetical breaks the tie (A->Z),
    # not the input/universe order.
    assert order([cand("MSFT", 2.0, 3.0), cand("AAPL", 2.0, 3.0), cand("GOOG", 2.0, 3.0)]) == [
        "AAPL", "GOOG", "MSFT",
    ]

    # Higher multiplier dominates both drop and alphabetical.
    assert order([cand("AAPL", 1.0, 9.0), cand("ZZZ", 3.0, 0.5), cand("MSFT", 2.0, 5.0)]) == [
        "ZZZ", "MSFT", "AAPL",
    ]

    # Same multiplier -> larger drop first, then ticker.
    assert order([cand("BBB", 2.0, 1.0), cand("AAA", 2.0, 4.0), cand("CCC", 2.0, 4.0)]) == [
        "AAA", "CCC", "BBB",
    ]

    # Drop is the max across sources: a bigger Robinhood drop outranks finance-only.
    assert order([cand("FN", 2.0, 3.0), cand("RH", 2.0, 1.0, rh_drop=6.0)]) == ["RH", "FN"]


def _no_data_signal(source, provider_error):
    return SourceSignal(
        ticker="AAPL",
        source=source,
        action=Action.NO_DATA,
        reason="provider error: boom" if provider_error else "missing completed bar or previous close",
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
    assert abs(record.slippage_pct - 1.0) < 1e-9   # 1 / 100 * 100
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
    return ReconciledDecision(ticker=ticker, action=action, reason="x", finance_signal=None, robinhood_signal=None)



def test_missing_fill_loop_reason_none_decision():
    assert classify_missing_fill(None, MISSING_FILL_BUDGET_CAPPED) == MISSING_FILL_BUDGET_CAPPED



def test_missing_fill_none_decision_no_loop_reason():
    assert classify_missing_fill(None, None) == MISSING_FILL_DATA_ERROR



def test_missing_fill_classification():
    assert classify_missing_fill(_decision("A", Action.NO_DATA), None) == MISSING_FILL_DATA_ERROR
    assert classify_missing_fill(_decision("A", Action.SKIP), None) == MISSING_FILL_FAILED_TRIGGER
    assert classify_missing_fill(_decision("A", Action.NO_ACTION), None) == MISSING_FILL_FAILED_TRIGGER
    assert classify_missing_fill(_decision("A", Action.MANUAL_REVIEW), None) == MISSING_FILL_FAILED_TRIGGER
    # loop reason takes precedence over the reconciled action.
    assert classify_missing_fill(_decision("A", Action.BUY), MISSING_FILL_BUDGET_CAPPED) == MISSING_FILL_BUDGET_CAPPED
    assert classify_missing_fill(_decision("A", Action.BUY), MISSING_FILL_BROKER_BLOCKED) == MISSING_FILL_BROKER_BLOCKED
    assert classify_missing_fill(_decision("A", Action.BUY), MISSING_FILL_EXECUTION_GAP) == MISSING_FILL_EXECUTION_GAP


def test_build_report_metrics_and_ranking():
    records = [
        build_execution_record(ticker="A", target_price=100.0, target_price_source="finance:signal_quote",
                               decision_bar_close=100.0, notional_usd=10.0, avg_fill_price=101.0, quantity_filled=1.0),
        build_execution_record(ticker="B", target_price=100.0, target_price_source="finance:signal_quote",
                               decision_bar_close=100.0, notional_usd=10.0, avg_fill_price=100.2, quantity_filled=1.0),
    ]
    run_result = RunResult(
        trading_day=date(2026, 6, 29),
        decisions=[_decision("C", Action.NO_DATA), _decision("D", Action.SKIP)],
        execution_records=records,
        loop_block_reasons={},
        active_universe=["A", "B", "C", "D"],
    )
    report = build_report(run_result, "manual", datetime(2026, 6, 29, 16, 0, tzinfo=EASTERN))
    # A has larger abs slippage (1.0%) than B (0.2%), so A ranks first.
    assert [r.ticker for r in report.ranked_slippage] == ["A", "B"]
    assert report.top_slippage_tickers == ["A", "B"]
    assert report.metrics["filled_order_count"] == 2
    assert report.metrics["missing_fill_count"] == 2
    assert report.missing_fills["C"] == MISSING_FILL_DATA_ERROR
    assert report.missing_fills["D"] == MISSING_FILL_FAILED_TRIGGER
    assert report.metrics["missing_fill_reasons"][MISSING_FILL_DATA_ERROR] == 1
    for key in ["mean_abs_slippage_pct", "median_abs_slippage_pct", "p90_abs_slippage_pct", "net_slippage_cost"]:
        assert key in report.metrics


# --- Artifact / state-file fields --------------------------------------------

def test_generate_report_and_state_artifacts(tmp_path=None):
    tmpdir_obj = tempfile.TemporaryDirectory()
    workdir = str(tmp_path) if tmp_path is not None else tmpdir_obj.name
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(dry_run=True)
    state = StrategyState()
    # reference prices near the decision close so the dry-run broker simulates fills.
    broker = DryRunBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers},
                          simulated_slippage_pct=0.2)
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))

    result = run_and_report(
        now, config, state, fin, rh, broker,
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
    assert payload["artifacts"]["slippage_report_path"] == result["slippage_report_path"]
    assert payload["artifacts"]["orders_csv_path"] == result["orders_csv_path"]
    metrics = payload["summary_metrics"]
    for key in [
        "filled_order_count", "total_filled_notional", "net_slippage_cost",
        "mean_abs_slippage_pct", "median_abs_slippage_pct", "p90_abs_slippage_pct",
        "top_slippage_tickers", "missing_fill_count", "missing_fill_reasons",
    ]:
        assert key in metrics, f"missing metric {key}"
    # dry-run simulated fills should have produced at least one filled order.
    assert metrics["filled_order_count"] >= 1

    # CSV carries the durable per-order slippage fields.
    csv_text = open(result["orders_csv_path"], encoding="utf-8").read()
    header = csv_text.splitlines()[0]
    for column in ["target_price", "slippage_pct", "slippage_cost", "latency_seconds", "execution_quality"]:
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
    broker = DryRunBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    # Tight bid/ask (0.2% spread, well under the default 1% breaker) so these
    # compact-mode tests -- which exercise usage-proxy logging, kill-switch
    # accounting, confirmation-required handling, and top-up recording, not
    # spread behavior itself -- still reach a submitted/dry_run outcome under
    # the scheduled-run fail-closed-on-missing-spread breaker.
    rh = _FakeDataWithBidAsk(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1))
    return now, config, state, broker, fin, rh


def test_usage_proxy_logged_in_run_state(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir,
                      notifications_sent=1)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    assert usage["state_file_bytes"] == os.path.getsize(os.path.join(artifacts_dir, "run_state.json"))
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

    run_daily_compact(now, config, state, raising, rh, broker, artifacts_dir=artifacts_dir)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    finance_rows = [row for row in run_state["signal_rows"] if row["source"] == "finance"]
    assert finance_rows and all(row["provider_error"] is True for row in finance_rows)


def test_finance_ticker_not_found_degrades_to_robinhood_only_buy(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, _fin, rh = _compact_setup(tmp_path)
    not_found = _NotFoundData(SourceName.FINANCE)

    run_daily_compact(now, config, state, not_found, rh, broker, artifacts_dir=artifacts_dir)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        run_state = json.load(handle)
    usage = run_state["usage_proxy"]

    # A permanent "ticker not found" error on one source (e.g. GFUZ unresolved on the
    # finance connector) must not count as a provider error or force manual review the
    # way a genuine transient failure does (see test_usage_proxy_counts_provider_errors
    # above) -- the documented Robinhood-only BUY path still proceeds.
    assert usage["errors_count"] == 0
    assert usage["manual_recovery_required"] is False
    assert usage["orders_placed_count"] == 3
    finance_rows = [row for row in run_state["signal_rows"] if row["source"] == "finance"]
    assert finance_rows and all(row["provider_error"] is False for row in finance_rows)
    assert all("not found" in row["reason"] for row in finance_rows)


def test_run_daily_compact_writes_structured_only(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    result = run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    assert result["compact"] is True
    for key in ["run_state_path", "signals_path", "buy_candidates_path", "orders_csv_path"]:
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

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        run_state = json.load(handle)
    rc = run_state["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["manual_run"] is False
    assert validate_classification(rc).ok


def test_run_daily_compact_manual_stamps_manual_run_classification(tmp_path):
    from scripts.run_classification import validate_classification

    artifacts_dir = os.path.join(tmp_path, "runs", "manual_2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=False)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    broker1 = DryRunBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers})
    fin1 = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now1))
    # Tight bid/ask so the scheduled-run fail-closed-on-missing-spread breaker
    # doesn't block this kill-switch scenario (spread behavior is not under test).
    rh1 = _FakeDataWithBidAsk(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now1), bid_ask=(97.9, 98.1))
    run_daily_compact(now1, config, state1, fin1, rh1, broker1, artifacts_dir=day1_dir)

    with open(os.path.join(day1_dir, "run_state.json"), encoding="utf-8") as handle:
        day1_state = json.load(handle)
    day1_deployed = sum(
        c["planned_amount_usd"] for c in day1_state["buy_candidates"] if c["status"] == "submitted"
    )
    assert day1_deployed > 0.4 * config.monthly_cash_allocation_usd  # sanity: threshold is crossed

    # Day 2 (within the trailing 7 days): the kill-switch must engage from day 1's
    # history alone, capping every buy multiplier to 1x.
    day2_dir = os.path.join(history_root, "2026-07-02")
    now2 = datetime(2026, 7, 2, 15, 45, tzinfo=EASTERN)
    state2 = StrategyState()
    broker2 = DryRunBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers})
    fin2 = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now2))
    rh2 = _FakeDataWithBidAsk(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now2), bid_ask=(97.9, 98.1))
    run_daily_compact(
        now2, config, state2, fin2, rh2, broker2,
        artifacts_dir=day2_dir, deployment_history_root=history_root,
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
    assert day2_state["risk_guard"]["deployed_last_7d_usd"] == round(day1_deployed + day2_deployed, 2)


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
                "run_id": f"scheduled_{day.strftime('%Y%m%d')}_1520ET",
                "triggered_at_utc": f"{day.isoformat()}T19:45:00Z",
                "state_schema_version": "run-classification-v1",
            },
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": deployed_usd}
            ],
        }
        with open(os.path.join(run_dir, "run_state.json"), "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)

    history_root = os.path.join(str(tmp_path), "runs")
    as_of = date(2026, 7, 21)
    _write_scheduled_state(as_of - timedelta(days=3), 30.0)     # in window -> counts
    _write_scheduled_state(as_of - timedelta(days=6), 12.0)     # window edge -> counts
    _write_scheduled_state(as_of - timedelta(days=7), 999.0)    # first day past window
    _write_scheduled_state(as_of - timedelta(days=40), 999.0)   # long-stale history

    config = StrategyConfig(monthly_cash_allocation_usd=3600.0)
    guarded = _apply_deployment_history(config, as_of, history_root)

    # Only the two in-window deployments (30.0 + 12.0) contribute; the day-7 and
    # day-40 artifacts are correctly excluded even though they are valid,
    # capital-deploying scheduled runs.
    assert guarded.deployed_last_7d_usd == 42.0


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
    broker = DryRunBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeDataWithBidAsk(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.9, 98.1))

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    broker = DryRunBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))

    run_daily_compact(
        now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=False,
    )

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAA"]["planned_amount_usd"] == 4.0
    assert by_ticker["BBB"]["planned_amount_usd"] == 4.0  # NOT capped to the $2 base
    assert run_state["risk_guard"]["kill_switch_engaged"] is False


def test_run_daily_compact_date_gated_short_circuits(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-07-01")
    _now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    gated_now = datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN)  # not an approved date

    result = run_daily_compact(gated_now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    assert result["blocked"] is True
    assert os.path.exists(result["run_state_path"])
    with open(result["run_state_path"], encoding="utf-8") as handle:
        assert json.load(handle)["status"] == "blocked"
    # No paths to CSVs that were never written, and no stray artifacts on disk.
    for key in ["signals_path", "buy_candidates_path", "orders_csv_path"]:
        assert key not in result
    assert set(os.listdir(artifacts_dir)) == {"run_state.json"}


def test_run_daily_compact_holiday_gated_short_circuits(tmp_path):
    """An approved date that also lands on a full NYSE closure (e.g. an extended
    trading_window.json that widens approved_test_dates with no holiday
    subtraction of its own) must still report blocked=True and never advertise
    signals/candidates/orders CSV paths that run_once never wrote."""
    labor_day = date(2026, 9, 7)
    artifacts_dir = os.path.join(tmp_path, "runs", str(labor_day))
    _now, _config, state, broker, fin, rh = _compact_setup(tmp_path)
    holiday_now = datetime(2026, 9, 7, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=_config.tickers,
        approved_test_dates=frozenset({labor_day}),
    )

    result = run_daily_compact(holiday_now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    assert result["blocked"] is True
    with open(result["run_state_path"], encoding="utf-8") as handle:
        assert json.load(handle)["status"] == "blocked"
    for key in ["signals_path", "buy_candidates_path", "orders_csv_path"]:
        assert key not in result
    assert set(os.listdir(artifacts_dir)) == {"run_state.json"}


def test_run_daily_compact_date_gate_does_not_clobber_prior_artifacts(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "reused")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)
    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        original = json.load(handle)
    assert "usage_proxy" in original

    gated_now = datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN)  # not an approved date
    result = run_daily_compact(gated_now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    assert result["blocked"] is True
    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        preserved = json.load(handle)
    # The blocked stub must never overwrite a previously recorded run.
    assert preserved == original


# --- run_classification wiring on the live path (task ce16ba6b state guarantee) ---


def test_build_run_artifacts_stamps_scheduled_classification(tmp_path):
    from scripts.run_classification import validate_classification

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=True)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=False)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=True)

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
    assert validate_run_artifacts(d, require_submitted_orders=True, require_summary=True)


def test_manual_compact_does_not_write_scheduled_contract_files(tmp_path):
    # The manual path is not the scheduled contract: it must not stamp the
    # scheduled-only evidence field nor write the scheduled contract artifacts.
    from pathlib import Path

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "manual-am")
    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=False)

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
            {"ticker": "A", "planned_amount_usd": 2.0, "status": "dry_run",
             "order_id": "DRY-RUN-SIM-A-2.00-x"},
            {"ticker": "B", "planned_amount_usd": 3.0, "status": "dry_run",
             "order_id": "DRY-RUN-SIM-B-3.00-x"},
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
            "run_origin": "scheduled", "manual_run": False, "stream": "afternoon",
            "valid_scheduled_evidence": True,
        },
        "buy_candidates": [
            {"ticker": "A", "planned_amount_usd": 2.0, "order_id": "o1", "status": "submitted"},
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
    assert persisted["artifact_contract_failure"]["failure"] == "artifact_contract_failure"
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
        lambda artifacts, artifacts_dir: ArtifactContractResult(False, ["forced contract failure"]),
    )

    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    classification_root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    run_daily_compact(
        now, config, state, fin, rh, broker,
        artifacts_dir=artifacts_dir, scheduled=True,
        classification_state_root=classification_root,
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
        now, config, state, fin, rh, broker,
        artifacts_dir=artifacts_dir, scheduled=True,
        classification_state_root=classification_root,
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
        now, config, state, fin, rh, broker,
        artifacts_dir=artifacts_dir, scheduled=False,
        classification_state_root=classification_root,
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
    broker = _ConfirmationRequiredBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers})
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    rh = _FakeDataWithBidAsk(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(97.0, 99.0))
    artifacts_dir = os.path.join(tmp_path, "runs", "wide-spread")

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=True)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=True)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
        SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now), bid_ask=(float("nan"), 99.0)
    )
    artifacts_dir = os.path.join(tmp_path, "runs", "nan-spread")

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=True)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=False)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    rh = _FakeDataWithRaisingBidAsk(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = os.path.join(tmp_path, "runs", "spread-error")

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir, scheduled=True)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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

    run_daily_compact(now, config, state, fin, rh, broker, artifacts_dir=artifacts_dir)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    written_csv_bytes = os.path.getsize(os.path.join(out, "signals.csv")) + os.path.getsize(
        os.path.join(out, "buy_candidates.csv")
    )
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
    prior = PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=99.0, run_id="manual_am", source="manual")
    decision = _buy_decision("AAPL", 6.0, target_price=98.0)
    outcome = evaluate_top_up_guard(decision, prior, StrategyConfig())
    assert outcome.top_up_guard_result == TOP_UP_INCREMENTAL
    assert outcome.incremental_order_amount == 3.0
    assert outcome.skipped is False
    assert outcome.prior_same_day_amount == 3.0
    assert outcome.prior_same_day_reference_price == 99.0
    assert outcome.afternoon_decision_price == 98.0


def test_top_up_guard_price_not_below_prior_buys_zero():
    prior = PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=98.0, run_id="manual_am", source="manual")
    # Equal price -> not strictly below -> $0.
    equal = evaluate_top_up_guard(_buy_decision("AAPL", 6.0, target_price=98.0), prior, StrategyConfig())
    assert equal.top_up_guard_result == TOP_UP_PRICE_NOT_BELOW
    assert equal.incremental_order_amount == 0.0
    assert equal.skipped is True
    # Higher price -> also $0.
    higher = evaluate_top_up_guard(_buy_decision("AAPL", 6.0, target_price=98.5), prior, StrategyConfig())
    assert higher.top_up_guard_result == TOP_UP_PRICE_NOT_BELOW
    assert higher.incremental_order_amount == 0.0
    assert higher.skipped is True


def test_top_up_guard_prior_exceeds_afternoon_desired_buys_zero():
    # Morning bought $6; afternoon only wants $3, even into a lower price -> $0.
    prior = PriorSameDayAllocation("AAPL", amount_usd=6.0, reference_price=99.0, run_id="manual_am", source="manual")
    outcome = evaluate_top_up_guard(_buy_decision("AAPL", 3.0, target_price=98.0), prior, StrategyConfig())
    assert outcome.top_up_guard_result == TOP_UP_NO_INCREMENTAL
    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True


def test_top_up_guard_missing_prior_price_is_conservative_manual_review():
    # Prior amount known but no comparable reference price -> conservative $0 + manual review.
    prior = PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=None, run_id="manual_am", source="manual")
    outcome = evaluate_top_up_guard(_buy_decision("AAPL", 6.0, target_price=98.0), prior, StrategyConfig())
    assert outcome.top_up_guard_result == TOP_UP_MISSING_PRIOR_PRICE
    assert outcome.incremental_order_amount == 0.0
    assert outcome.skipped is True
    assert outcome.manual_review is True


def test_top_up_guard_missing_afternoon_price_is_conservative():
    # Known prior price but no afternoon price (no target, no signals) -> conservative $0.
    prior = PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=99.0, run_id="manual_am", source="manual")
    outcome = evaluate_top_up_guard(_buy_decision("AAPL", 6.0, target_price=0.0), prior, StrategyConfig())
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
    assert outcome.afternoon_desired_amount == 4.0  # 1.0 (preserved base) * 4x, not 2.0 * 4x
    assert outcome.top_up_guard_result == TOP_UP_INCREMENTAL
    assert outcome.incremental_order_amount == 2.0  # 4.0 - 2.0, not 8.0 - 2.0 == 6.0
    # Codex review ("Persist the preserved base on top-up candidates"): the
    # guard must expose the PRESERVED $1 base it actually used, not this run's
    # own freshly recomputed $2 base, so a candidate-row artifact stays
    # consistent with the amount actually calculated.
    assert outcome.effective_base_amount_usd == 1.0


def test_top_up_guard_falls_back_to_current_amount_when_prior_base_unknown():
    # An older prior-allocation artifact recorded no selected_base_amount_usd at
    # all -- must fall back to the pre-existing behavior (this run's own suggested
    # amount) rather than treat the unknown preserved base as $0.
    prior = PriorSameDayAllocation("AAPL", amount_usd=2.0, reference_price=98.0, run_id="manual_am", source="manual")
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
    assert outcome.afternoon_desired_amount == 8.0
    assert outcome.incremental_order_amount == 6.0
    # Falls back to this run's own base ($2) since no prior base was on record.
    assert outcome.effective_base_amount_usd == 2.0


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
                handle.write(f"{row['ticker']},{row['status']},{row['avg_fill_price']}\n")
    return run_dir


def _manual_run_state(candidates, generated_at="2026-06-29T10:00:00-04:00", run_origin="manual"):
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


def test_load_prior_same_day_reads_morning_manual_allocation(tmp_path):
    # No orders.csv at all (legacy/older artifact layout). A submitted row still
    # contributes prior-allocation amount, but without a confirmed fill its logged
    # target_price must not become a known reference price.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state([
            {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 99.0},
        ]),
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
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 2.0, "selected_base_amount_usd": 1.0},
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
            "run_classification": {"run_origin": "scheduled", "stream": "morning", "run_id": "morning_0945"},
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 1.0},  # no base field at all
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
            "run_classification": {"run_origin": "scheduled", "stream": "morning", "run_id": "morning_1030"},
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 2.0, "selected_base_amount_usd": 2.0},
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
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 2.0, "selected_base_amount_usd": 2.0},
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "97.0"}],
    )
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 1.0, "selected_base_amount_usd": 1.0},
            ],
            generated_at="2026-06-29T09:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].selected_base_amount_usd == 1.0  # 09:00's base, not 11:00's


def test_load_prior_same_day_allocation_unconfirmed_base_not_trusted(tmp_path):
    # An unconfirmed "submitted" row (no orders.csv fill) has neither a known
    # reference price nor a trusted base -- same conservative gate as reference_price.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state([
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 3.0,
                "target_price": 99.0,
                "selected_base_amount_usd": 1.0,
            },
        ]),
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].selected_base_amount_usd is None


def test_load_prior_same_day_submitted_with_logged_price_and_no_fill_has_unknown_reference(tmp_path):
    # A still-submitted order with no confirmed fill row may later be rejected or
    # canceled; do not use its logged target/decision price as a known reference.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state([
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 3.0,
                "target_price": 99.0,
                "decision_bar_close": 99.2,
            },
        ]),
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
        _manual_run_state([
            {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0},
        ]),
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
        _manual_run_state([
            {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0, "target_price": 99.0},
        ]),
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
        _manual_run_state([
            {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0},
        ]),
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
            _manual_run_state([
                {"ticker": "AMZN", "status": "filled", "planned_amount_usd": 3.0},
            ]),
            handle,
        )
    with open(os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8") as handle:
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
            _manual_run_state([
                {"ticker": "AMZN", "status": "filled", "planned_amount_usd": 6.0},
            ]),
            handle,
        )
    with open(os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8") as handle:
        handle.write("ticker,amount_usd,avg_fill_price\nAMZN,4.25,242.1099\n")

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    assert allocations["AMZN"].amount_usd == 4.25
    assert allocations["AMZN"].reference_price == 242.1099
    assert allocations["AMZN"].source == "manual"


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


def test_load_prior_same_day_multi_merges_roots_sums_amounts_lowest_reference(tmp_path):
    # The afternoon guard scans multiple roots (its own + morning + midday). When
    # a ticker appears in more than one root, amounts sum and the lowest known
    # reference price wins, so the afternoon must beat the best earlier entry.
    morning_root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        morning_root,
        "20260629_1030ET",
        _manual_run_state([
            {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0, "target_price": 99.0},
        ]),
    )
    midday_root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        midday_root,
        "20260629_1230ET",
        _manual_run_state([
            {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 4.5, "target_price": 97.0},
        ]),
    )
    allocations = load_prior_same_day_allocations_multi(
        (morning_root, midday_root), date(2026, 6, 29)
    )
    assert allocations["AAPL"].amount_usd == 7.5
    assert allocations["AAPL"].reference_price == 97.0


def test_load_prior_same_day_multi_missing_reference_poisons_to_none(tmp_path):
    # If any contributing run lacks a reference price, the merged reference is
    # unknown -- the guard can't safely compare, so it routes to the conservative
    # manual-review path rather than trusting a partial reference.
    morning_root = os.path.join(tmp_path, "cron_tracking", "morning_dca")
    _write_prior_run(
        morning_root,
        "20260629_1030ET",
        _manual_run_state([
            {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0, "target_price": 99.0},
        ]),
    )
    midday_root = os.path.join(tmp_path, "cron_tracking", "midday_dca")
    _write_prior_run(
        midday_root,
        "20260629_1230ET",
        _manual_run_state([
            {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 4.5},
        ]),
        orders_rows=[{"ticker": "AAPL", "status": "submitted", "avg_fill_price": ""}],
    )
    allocations = load_prior_same_day_allocations_multi(
        (morning_root, midday_root), date(2026, 6, 29)
    )
    assert allocations["AAPL"].amount_usd == 7.5
    assert allocations["AAPL"].reference_price is None


def test_load_prior_same_day_multi_picks_chronologically_first_base_regardless_of_root_order(tmp_path):
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
            "run_classification": {"run_origin": "scheduled", "stream": "morning", "run_id": "morning_0945"},
            "buy_candidates": [
                {"ticker": "AAPL", "status": "filled", "planned_amount_usd": 1.0, "selected_base_amount_usd": 1.0},
            ],
        },
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )

    allocations = load_prior_same_day_allocations_multi(
        (afternoon_root, morning_root), date(2026, 6, 29), boundary_et=time(15, 20)
    )

    assert allocations["AAPL"].amount_usd == 3.0
    assert allocations["AAPL"].selected_base_amount_usd == 1.0


def test_load_prior_same_day_unconfirmed_submitted_order_has_no_reference_price(tmp_path):
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
        _manual_run_state([
            {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0},
        ]),
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
        _manual_run_state([
            {"ticker": "AAPL", "status": "dry_run", "planned_amount_usd": 3.0, "target_price": 99.0},
        ]),
    )
    assert load_prior_same_day_allocations(root, date(2026, 6, 29)) == {}


def test_load_prior_same_day_excludes_scheduled_and_after_boundary(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    # Scheduled run -> never prior context.
    _write_prior_run(
        root,
        "scheduled_runs/20260629_1520ET",
        _manual_run_state(
            [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 99.0}],
            run_origin="scheduled",
        ),
    )
    # Manual run but after the 2:30 PM ET boundary -> excluded.
    _write_prior_run(
        root,
        "manual_runs/1500",
        _manual_run_state(
            [{"ticker": "NVDA", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 99.0}],
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
    assert "AAPL" in allocations, "morning scheduled run must appear as prior allocation context"
    assert allocations["AAPL"].amount_usd == 3.0


def test_load_prior_same_day_aggregates_amount_and_takes_min_price(tmp_path):
    # Reference prices only ever come from confirmed orders.csv fills (never
    # target_price/decision_bar_close -- see test_load_prior_same_day_reads_
    # morning_manual_allocation), so both runs here confirm a real fill price.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state(
            [{"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0, "target_price": 99.0}],
            generated_at="2026-06-29T09:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "99.0"}],
    )
    _write_prior_run(
        root,
        "manual_runs/1000",
        _manual_run_state(
            [{"ticker": "AAPL", "status": "filled", "planned_amount_usd": 4.5, "target_price": 98.0}],
            generated_at="2026-06-29T10:00:00-04:00",
        ),
        orders_rows=[{"ticker": "AAPL", "status": "filled", "avg_fill_price": "98.0"}],
    )
    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert allocations["AAPL"].amount_usd == 7.5
    # The afternoon run must beat the best (lowest) earlier entry price.
    assert allocations["AAPL"].reference_price == 98.0


def test_load_prior_same_day_ignores_other_trading_days(tmp_path):
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    state = _manual_run_state(
        [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 99.0}]
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
            _manual_run_state([{"ticker": "AMZN", "status": "filled", "planned_amount_usd": 3.0}]),
            handle,
        )
    with open(os.path.join(run_dir, "filled_orders.csv"), "w", encoding="utf-8") as handle:
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
        _manual_run_state([{"ticker": "AAPL", "status": "filled", "planned_amount_usd": 3.0}]),
    )
    orders_csv = os.path.join(root, "manual_runs", "0900", "orders.csv")
    with open(orders_csv, "w", encoding="utf-8") as handle:
        handle.write("ticker,status,avg_fill_price,quantity_filled\nAAPL,filled,,0.05\n")

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))

    # No crash, and no fabricated reference price for the priceless fill row.
    assert allocations["AAPL"].reference_price is None


def test_run_daily_compact_checks_per_order_cap_before_broker_duplicate_lookup(tmp_path):
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

    run_daily_compact(now, config, state, fin, rh, tracking_broker, artifacts_dir=artifacts_dir)

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
        _manual_run_state([
            {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 99.0},
        ]),
    )

    allocations = load_prior_same_day_allocations(root, date(2026, 6, 29))
    assert set(allocations) == {"AAPL"}


# --- Integration: top-up guard through run_daily_compact ----------------------


def test_run_daily_compact_top_up_records_incremental(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    # 100 -> 98 is a 2% drop -> 2x -> $4 desired. Morning bought $2 at 99.0; the
    # afternoon quote is 98.0 (< 99.0) so only the $2 difference is bought.
    prior = {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=2.0, reference_price=99.0, run_id="manual_am", source="manual")}

    run_daily_compact(
        now, config, state, fin, rh, broker,
        artifacts_dir=artifacts_dir, prior_same_day_allocations=prior,
    )

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
        t: PriorSameDayAllocation(t, amount_usd=2.0, reference_price=97.0, run_id="manual_am", source="manual")
        for t in config.tickers
    }

    result = run_daily_compact(
        now, config, state, fin, rh, broker,
        artifacts_dir=artifacts_dir, prior_same_day_allocations=prior,
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
        assert candidate["top_up_guard_result"] == TOP_UP_PRICE_NOT_BELOW
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

    blocking = _BlockingBroker(cash=500.0, reference_prices={t: 98.0 for t in config.tickers})
    prior = {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=2.0, reference_price=99.0, run_id="manual_am", source="manual")}

    run_daily_compact(
        now, config, state, fin, rh, blocking,
        artifacts_dir=artifacts_dir, prior_same_day_allocations=prior,
    )

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
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
    from near_close_dca import TOP_UP_INCREMENTAL, TopUpGuardOutcome, build_run_artifacts

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
        now, config, state, [decision],
        candidate_outcomes={"AAPL": {"order_id": "x", "status": "dry_run"}},
        top_up_outcomes={"AAPL": guard},
    )
    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert candidates["AAPL"]["planned_amount_usd"] == 3.0, (
        "planned_amount_usd must equal incremental_order_amount, not the full desired amount"
    )
    assert candidates["AAPL"]["afternoon_desired_amount_usd"] == 6.0, (
        "original desired amount must be preserved in afternoon_desired_amount_usd"
    )


def test_build_run_artifacts_records_guards_effective_base_not_current_run_base(tmp_path):
    # Codex review ("Persist the preserved base on top-up candidates"): when the
    # guard reused a preserved prior-allocation base, the candidate row must
    # record THAT base, not this run's own freshly recomputed
    # decision.selected_base_amount_usd.
    from near_close_dca import TOP_UP_INCREMENTAL, TopUpGuardOutcome, build_run_artifacts

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
        now, config, state, [decision],
        candidate_outcomes={"AAPL": {"order_id": "x", "status": "dry_run"}},
        top_up_outcomes={"AAPL": guard},
    )
    candidates = {c["ticker"]: c for c in artifacts["buy_candidates"]}
    assert candidates["AAPL"]["selected_base_amount_usd"] == 1.0


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
            "AAPL", amount_usd=200.0, reference_price=99.0, run_id="manual_am", source="manual"
        )
    }
    broker = DryRunBroker(cash=5000.0, reference_prices={"AAPL": 98.0})
    fin = _FakeData(SourceName.FINANCE, 98.0, 100.0, quote=(98.0, now))
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
    artifacts_dir = str(tmp_path / "runs" / "2026-06-29-cap")

    import json as _json
    result = run_daily_compact(
        now, config, state, fin, rh, broker,
        artifacts_dir=artifacts_dir,
        prior_same_day_allocations=prior,
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
            fh, fieldnames=["ticker", "status", "avg_fill_price", "quantity_filled", "order_id"]
        )
        writer.writeheader()
        writer.writerow({
            "ticker": "AAPL", "status": "filled",
            "avg_fill_price": "99.0", "quantity_filled": "0.0303", "order_id": "ord1",
        })

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
    assert "AAPL" in allocations, (
        "full-report run_state without buy_candidates must produce an AAPL allocation via orders_csv_path"
    )
    alloc = allocations["AAPL"]
    assert abs(alloc.reference_price - 99.0) < 0.001
    assert abs(alloc.amount_usd - 99.0 * 0.0303) < 0.001


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
    assert iso_week_key(date(2023, 12, 31)) == (2023, 52)  # Sunday, last day of 2023 ISO week 52
    assert iso_week_key(date(2024, 1, 1)) == (2024, 1)    # Monday, first day of 2024 ISO week 1

    # Leap year (2024)
    assert iso_week_key(date(2024, 2, 29)) == (2024, 9)   # Thursday, Leap day

    # A year where Jan 1st is in the previous year's week 52/53
    # Jan 1, 2023 was a Sunday. ISO weeks start on Monday.
    # Therefore, Jan 1 2023 belongs to the last ISO week of 2022.
    assert iso_week_key(date(2023, 1, 1)) == (2022, 52)
    assert iso_week_key(date(2023, 1, 2)) == (2023, 1)    # Monday, starts week 1 of 2023

    # 53-week year boundary (e.g., 2020)
    assert iso_week_key(date(2020, 12, 31)) == (2020, 53) # Thursday, week 53
    assert iso_week_key(date(2021, 1, 3)) == (2020, 53)   # Sunday, last day of week 53
    assert iso_week_key(date(2021, 1, 4)) == (2021, 1)    # Monday, starts week 1 of 2021


def test_approved_window_status_counts_remaining_days():
    from near_close_dca import approved_window_status

    approved = frozenset({date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)})
    in_window, window_end, remaining = approved_window_status(approved, date(2026, 8, 25))
    assert in_window is True
    assert window_end == date(2026, 8, 26)
    assert remaining == 2  # the 25th and 26th

    in_window, window_end, remaining = approved_window_status(approved, date(2026, 9, 1))
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
        allowed = _check_execution_gates(date(2026, 8, 31), config, DryRunBroker(), str(tmp_path))
    assert allowed is False
    assert any("hard-stopped" in record.getMessage() for record in caplog.records)
    # The blocked stub artifact is still written as before.
    assert (tmp_path / "run_state.json").exists()


def test_execution_gate_blocks_nyse_holiday_even_if_config_approves_it(tmp_path, caplog):
    """A config-driven window (e.g. an extended trading_window.json) can widen
    approved_test_dates to any weekday range with no holiday subtraction of its
    own; the gate must still block full NYSE closures like Labor Day and
    Thanksgiving via the independently computed calendar."""
    from near_close_dca import _check_execution_gates

    for holiday in (date(2026, 9, 7), date(2026, 11, 26)):  # Labor Day, Thanksgiving
        config = StrategyConfig(approved_test_dates=frozenset({holiday}))
        with caplog.at_level("WARNING", logger="near_close_dca"):
            allowed = _check_execution_gates(holiday, config, DryRunBroker(), str(tmp_path / str(holiday)))
        assert allowed is False
        assert any(
            "NYSE holiday gate blocked execution" in record.getMessage() for record in caplog.records
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
    orders_csv.write_text(good + f"MSFT,filled,{oversized},0.01,oid-2\n", encoding="utf-8")

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


if __name__ == "__main__":
    _run_all()
