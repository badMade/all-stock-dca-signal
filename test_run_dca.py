"""Tests for the orchestrator + CLI (run_dca)."""

from __future__ import annotations

from datetime import datetime, time

from dca_config import load_strategy_config
from near_close_dca import EASTERN, SourceName, StrategyConfig, StrategyState, build_broker
from run_dca import DemoDataProvider, _fills_from_orders_csv, main, run_pipeline


def _now(day=(2026, 7, 6)):
    return datetime.combine(datetime(*day).date(), time(15, 45), tzinfo=EASTERN)


def _demo_config():
    # Approve the demo trading day regardless of the bundled window.
    return load_strategy_config(approved_test_dates=frozenset({_now().date()}))


def test_demo_provider_produces_downtrend_bars():
    prov = DemoDataProvider(SourceName.FINANCE)
    start = datetime.combine(datetime(2026, 7, 6).date(), time(9, 30), tzinfo=EASTERN)
    bars = prov.get_intraday_bars("NVDA", start, _now(), 5)
    assert len(bars) > 1
    assert bars[-1].close < bars[0].close  # trend down


def test_run_pipeline_writes_artifacts_and_reports(tmp_path):
    config = _demo_config()
    finance = DemoDataProvider(SourceName.FINANCE)
    robinhood = DemoDataProvider(SourceName.ROBINHOOD)
    broker = build_broker(config)
    result = run_pipeline(
        _now(), config, StrategyState(), finance, robinhood, broker,
        artifacts_dir=tmp_path, generate_full_reports=True,
    )
    assert result["blocked"] is False
    for key in ("run_state_path", "signals_path", "buy_candidates_path", "orders_csv_path"):
        assert (tmp_path / result[key].split("/")[-1]).exists()
    assert "reports" in result and "notion_sync" in result
    assert (tmp_path / "latest_run_summary.json").exists()
    assert (tmp_path / "slippage_and_coverage_report.md").exists()


def test_run_pipeline_blocked_day_skips_reports(tmp_path):
    # A day outside the approved set trips the date gate and writes only the stub.
    config = load_strategy_config(approved_test_dates=frozenset())
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=tmp_path, generate_full_reports=True,
    )
    assert result["blocked"] is True
    assert "reports" not in result
    assert not (tmp_path / "latest_run_summary.json").exists()


def test_fills_from_orders_csv(tmp_path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,avg_fill_price,quantity_filled\n"
        "AAPL,filled,O1,100.0,0.5\n"
        "NVDA,submitted,O2,,\n",
        encoding="utf-8",
    )
    fills = _fills_from_orders_csv(csv_path)
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["order_id"] == "O1"


def test_execution_targets_from_orders_csv(tmp_path):
    from run_dca import _execution_targets_from_orders_csv

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id,target_price\n"
        "AAPL,filled,O1,97.50\n"
        "NVDA,submitted,O2,\n"
        "MU,submitted,O3,-\n",
        encoding="utf-8",
    )
    targets = _execution_targets_from_orders_csv(csv_path)
    assert targets["AAPL"] == 97.50
    assert targets["NVDA"] is None
    assert targets["MU"] is None


def test_fills_from_missing_csv_is_empty(tmp_path):
    assert _fills_from_orders_csv(tmp_path / "nope.csv") == []


def test_cli_demo_exit_code(tmp_path):
    rc = main(["--demo", "--now", "2026-07-06T15:45", "--artifacts-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "run_state.json").exists()


def test_cli_refuses_without_demo(tmp_path):
    rc = main(["--now", "2026-07-06T15:45", "--artifacts-dir", str(tmp_path)])
    assert rc == 2


def test_cli_refuses_when_dry_run_false(tmp_path, monkeypatch):
    import run_dca

    monkeypatch.setattr(run_dca, "load_strategy_config", lambda *a, **k: StrategyConfig(dry_run=False))
    rc = run_dca.main(["--demo", "--artifacts-dir", str(tmp_path)])
    assert rc == 2
