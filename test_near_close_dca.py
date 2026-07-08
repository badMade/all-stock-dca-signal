"""Self-tests for the graded DCA sizing ladder and slippage/coverage reporting.

Runnable with either ``python test_near_close_dca.py`` or ``pytest``. Boundary
percentages are converted to fractions before calling graded_buy_multiplier, which
expects a fraction of the previous close (2.0% down == 0.02).
"""

import json
import os
import tempfile
from datetime import date, datetime, time, timedelta

from near_close_dca import (
    ACTIVE_TICKERS,
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
    reconcile_signals,
    run_and_report,
    run_daily_compact,
    select_budget_tier,
    write_run_artifacts,
)

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


def test_base_amount_is_three_dollars():
    assert StrategyConfig().base_buy_amount_usd == 3.0


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
    rh = _FakeData(SourceName.ROBINHOOD, 98.0, 100.0, quote=(98.0, now))
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
    assert usage["order_reviews_attempted"] == 3
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


def test_load_prior_same_day_reads_morning_manual_allocation(tmp_path):
    # No orders.csv at all (legacy/older artifact layout) -- the amount is still
    # read from run_state.json, but reference_price is None rather than falling
    # back to target_price/decision_bar_close: that's the decision bar's price at
    # signal time, not a proven execution price, so using it as a stand-in risks
    # comparing the afternoon quote against a price nothing actually traded at.
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


def test_load_prior_same_day_prefers_orders_csv_fill_price(tmp_path):
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
    # The actual fill price wins over the recorded signal-time target price.
    assert allocations["AAPL"].reference_price == 97.5


def test_load_prior_same_day_unconfirmed_submitted_order_has_no_reference_price(tmp_path):
    # status="submitted" in run_state.json only means an order_id was obtained,
    # not that it filled; when orders.csv doesn't confirm a fill (still
    # "submitted"), we can't tell whether real capital moved. Dropping the
    # ticker entirely would risk a double-buy if it actually filled but was
    # never refreshed, so it stays as prior allocation context with the known
    # planned amount but no reference price -- which routes the afternoon guard
    # to its conservative missing_prior_price / manual-review $0 path rather
    # than either a blind top-up or a blind full re-buy.
    root = os.path.join(tmp_path, "cron_tracking", "ce16ba6b")
    _write_prior_run(
        root,
        "manual_runs/0900",
        _manual_run_state([
            {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 99.0},
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


# --- Integration: top-up guard through run_daily_compact ----------------------


def test_run_daily_compact_top_up_records_incremental(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    # 100 -> 98 is a 2% drop -> 2x -> $6 desired. Morning bought $3 at 99.0; the
    # afternoon quote is 98.0 (< 99.0) so only the $3 difference is bought.
    prior = {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=99.0, run_id="manual_am", source="manual")}

    run_daily_compact(
        now, config, state, fin, rh, broker,
        artifacts_dir=artifacts_dir, prior_same_day_allocations=prior,
    )

    with open(os.path.join(artifacts_dir, "run_state.json"), encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    aapl = by_ticker["AAPL"]
    assert aapl["top_up_guard_result"] == TOP_UP_INCREMENTAL
    assert aapl["incremental_order_amount"] == 3.0
    assert aapl["afternoon_desired_amount"] == 6.0
    assert aapl["prior_same_day_amount"] == 3.0
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
    assert by_ticker["NVDA"]["incremental_order_amount"] == 6.0
    assert "graded-dca-topup" not in by_ticker["NVDA"]["order_id"]
    assert "graded-dca-2026-06-29-NVDA" in by_ticker["NVDA"]["order_id"]


def test_run_daily_compact_fully_offset_still_writes_valid_state(tmp_path):
    artifacts_dir = os.path.join(tmp_path, "runs", "2026-06-29-offset")
    now, config, state, broker, fin, rh = _compact_setup(tmp_path)
    # Every ticker fully offset: prior price 97.0 is below the 98.0 afternoon quote
    # (price not below prior) so each candidate tops up $0, yet a valid scheduled
    # run_state must still be written.
    prior = {
        t: PriorSameDayAllocation(t, amount_usd=3.0, reference_price=97.0, run_id="manual_am", source="manual")
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
    assert run_state["budget"]["day_notional_usd"] == 0.0
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
    prior = {"AAPL": PriorSameDayAllocation("AAPL", amount_usd=3.0, reference_price=99.0, run_id="manual_am", source="manual")}

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
    assert by_ticker["AAPL"]["incremental_order_amount"] == 3.0
    # NVDA has NO loaded prior allocation, so the broker's same-day-order check
    # must still apply -- the guard bypass is scoped per-ticker, not blanket.
    assert by_ticker["NVDA"]["status"] == "skipped"
    assert by_ticker["NVDA"]["skip_reason"] == "same-day broker order exists"


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


if __name__ == "__main__":
    _run_all()
