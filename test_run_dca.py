"""Tests for the orchestrator + CLI (run_dca)."""

from __future__ import annotations

from datetime import datetime, time

from dca_config import load_strategy_config
from near_close_dca import EASTERN, DryRunBroker, SourceName, StrategyConfig, StrategyState, build_broker
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


def test_run_pipeline_scheduled_run_auto_loads_prior_allocations(tmp_path):
    import json

    # Seed a manual same-day allocation under the cron task root's manual_runs/,
    # mirroring the real production layout from config/scheduled_tasks.json:
    # <task_root>/scheduled_runs/YYYYMMDD_1520ET/ (artifacts_dir) as a sibling of
    # <task_root>/manual_runs/<timestamp>/. This is nested two levels below the
    # task root, unlike a flat "scheduled" directory -- a naive
    # artifacts_dir.parent would land on scheduled_runs/ itself and never see
    # manual_runs/, so the auto-derived root must walk up past scheduled_runs/.
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    manual_dir = task_root / "manual_runs" / "0900"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": _now().date().isoformat(),
            "generated_at": f"{_now().date().isoformat()}T09:00:00-04:00",
            "run_classification": {"run_origin": "manual", "run_id": "manual_am"},
            # A very low reference price guarantees the afternoon price is never
            # below it, so this ticker's guard result proves the allocation was
            # loaded (top_up_guard_result != "no_prior_run") without depending
            # on the demo provider's exact drop-derived price.
            "buy_candidates": [
                {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 3.0, "target_price": 0.01},
            ],
        }),
        encoding="utf-8",
    )

    config = _demo_config()
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
    )
    assert result["blocked"] is False
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    by_ticker = {c["ticker"]: c for c in run_state["buy_candidates"]}
    assert by_ticker["AAPL"]["top_up_guard_result"] != "no_prior_run"
    assert not (task_root / "latest_run_summary.json").exists()


def test_default_prior_allocation_state_root_walks_past_scheduled_runs():
    from pathlib import Path

    from run_dca import _default_prior_allocation_state_root

    task_root = Path("/cron_tracking/ce16ba6b")
    artifacts_dir = task_root / "scheduled_runs" / "20260706_1520ET"
    assert _default_prior_allocation_state_root(artifacts_dir) == task_root


def test_default_prior_allocation_state_root_falls_back_to_parent():
    from pathlib import Path

    from run_dca import _default_prior_allocation_state_root

    # No scheduled_runs/ segment in the path (e.g. a flat test/demo layout) ->
    # fall back to the immediate parent.
    artifacts_dir = Path("/tmp/some_root/scheduled")
    assert _default_prior_allocation_state_root(artifacts_dir) == Path("/tmp/some_root")


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


def test_refresh_fills_from_broker(tmp_path):
    from near_close_dca import OrderFill
    from run_dca import _refresh_fills_from_broker

    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "ticker,status,order_id\n"
        "AAPL,submitted,O1\n"
        "NVDA,filled,O2\n",
        encoding="utf-8",
    )

    class _Broker:
        def get_order_fill(self, order_id):
            # A fill has since settled for the previously-submitted order.
            if order_id == "O1":
                return OrderFill(avg_fill_price=100.0, quantity_filled=0.5)
            return None

    fills = _refresh_fills_from_broker(csv_path, _Broker(), [])
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["fill_price"] == 100.0


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


def test_run_pipeline_persists_raw_inputs_before_compute(tmp_path):
    import json

    config = _demo_config()
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
    )
    assert result["blocked"] is False
    raw_file = scheduled_dir / "raw" / "quotes_batch1.json"
    assert raw_file.exists()
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["raw_input_dir"] == str(scheduled_dir / "raw")
    assert run_state["raw_inputs_saved"] == ["quotes_batch1.json"]


def test_run_pipeline_raw_persistence_failure_writes_error_state(tmp_path):
    import json

    class ExplodingPayload:
        pass

    config = _demo_config()
    task_root = tmp_path / "cron_tracking" / "ce16ba6b"
    scheduled_dir = task_root / "scheduled_runs" / "20260706_1520ET"
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result
    assert not (scheduled_dir / "orders.csv").exists()
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_persistence_error"]
    assert run_state["raw_inputs_saved"] == []
    assert run_state["buy_candidates"] == []


def test_run_pipeline_raw_persistence_failure_clears_stale_compact_artifacts(tmp_path):
    import json

    class ExplodingPayload:
        pass

    config = _demo_config()
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    scheduled_dir.mkdir(parents=True)
    # Seed stale compact + derived-report artifacts from a prior successful attempt.
    stale_files = (
        "orders.csv", "signals.csv", "buy_candidates.csv",
        "latest_run_summary.json", "slippage_and_coverage_report.md",
        "slippage_ranked.csv", "missing_fills_report.csv",
        "notion_latest_run_compact.md", "notion_latest_run_payload.json",
    )
    for stale in stale_files:
        (scheduled_dir / stale).write_text("stale", encoding="utf-8")

    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        build_broker(config), artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": ExplodingPayload()},
    )
    assert "raw_persistence_error" in result
    for stale in stale_files:
        assert not (scheduled_dir / stale).exists(), f"{stale} was not removed"
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["broker"] == "robinhood"


def test_run_pipeline_live_run_missing_raw_inputs_writes_error_state(tmp_path):
    import json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    # The raw persistence error fires before any broker call, so a DryRunBroker is fine here.
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
        raw_connector_inputs=None,
    )
    assert "raw_persistence_error" in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_inputs_saved"] == []


def test_run_pipeline_live_run_empty_raw_inputs_writes_error_state(tmp_path):
    import json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
        raw_connector_inputs={},
    )
    assert "raw_persistence_error" in result
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_inputs_saved"] == []


def test_run_pipeline_live_run_partial_raw_inputs_writes_error_state(tmp_path):
    import json

    config = load_strategy_config(
        approved_test_dates=frozenset({_now().date()}), dry_run=False
    )
    scheduled_dir = tmp_path / "scheduled_runs" / "20260706_1520ET"
    quotes_file = tmp_path / "quotes.json"
    quotes_file.write_text('{"symbol": "AAPL", "price": 150.0}', encoding="utf-8")
    broker = DryRunBroker(account=config.target_account_number)
    result = run_pipeline(
        _now(), config, StrategyState(),
        DemoDataProvider(SourceName.FINANCE), DemoDataProvider(SourceName.ROBINHOOD),
        broker, artifacts_dir=scheduled_dir,
        raw_connector_inputs={"quotes_batch1.json": {"output_path": str(quotes_file)}},
        required_raw_filenames=frozenset({"quotes_batch1.json", "historicals_batch1.json"}),
    )
    assert "raw_persistence_error" in result
    assert "historicals_batch1.json" in result["raw_persistence_error"]
    with open(result["run_state_path"], encoding="utf-8") as handle:
        run_state = json.load(handle)
    assert run_state["order_submit_status"] == "not_submitted_error"
    assert run_state["raw_inputs_saved"] == []
