"""Self-tests for the graded DCA sizing ladder and slippage/coverage reporting.

Runnable with either ``python test_near_close_dca.py`` or ``pytest``. Boundary
percentages are converted to fractions before calling graded_buy_multiplier, which
expects a fraction of the previous close (2.0% down == 0.02).
"""

import json
import os
import tempfile
from datetime import date, datetime, time, timedelta
from typing import List, Optional

from near_close_dca import (
    ACTIVE_TICKERS,
    EASTERN,
    EXCLUDED_TICKERS,
    Action,
    Bar,
    DryRunBroker,
    OrderFill,
    ReconciledDecision,
    SourceName,
    StrategyConfig,
    StrategyState,
    build_execution_record,
    build_report,
    classify_execution_quality,
    classify_missing_fill,
    evaluate_source_signal,
    generate_slippage_report,
    graded_buy_multiplier,
    reconcile_signals,
    run_and_report,
    write_run_state,
    RunResult,
    MISSING_FILL_BUDGET_CAPPED,
    MISSING_FILL_BROKER_BLOCKED,
    MISSING_FILL_DATA_ERROR,
    MISSING_FILL_EXECUTION_GAP,
    MISSING_FILL_FAILED_TRIGGER,
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


def test_base_amount_is_one_dollar():
    assert StrategyConfig().base_buy_amount_usd == 1.0


def test_budget_caps():
    config = StrategyConfig()
    assert config.daily_base_cap_usd == 60.0
    assert config.reserve_cap_usd == 420.0
    assert config.hard_daily_max_usd == 120.0
    assert config.weekly_notional_cap_usd == 600.0


def test_ticker_universe():
    assert "IBM" in ACTIVE_TICKERS
    assert "IRDM" in ACTIVE_TICKERS
    assert "GLW" in ACTIVE_TICKERS
    assert "IRDMI" not in ACTIVE_TICKERS
    assert "GLWR" not in ACTIVE_TICKERS
    assert "BYDDY" in EXCLUDED_TICKERS
    assert "BYDDY" not in ACTIVE_TICKERS


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
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = str(tmp_path) if tmp_path is not None else tmpdir
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


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All self-tests passed.")


if __name__ == "__main__":
    _run_all()
