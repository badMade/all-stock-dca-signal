"""Tests for scripts/run_90day_prediction_experiment.py."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.run_90day_prediction_experiment import (
    generate_walkforward_baseline_predictions,
    get_trading_days,
    main,
)


def _create_sample_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"])
        for r in rows:
            writer.writerow(r)


def test_get_trading_days_and_baseline_generation(tmp_path: Path) -> None:
    ticker_dir = tmp_path / "tickers"
    ticker_dir.mkdir()

    spy_rows = [
        ["2026-05-14", "500.0", "505.0", "499.0", "502.0", "502.0", "1000"],
        ["2026-05-15", "503.0", "507.0", "501.0", "506.0", "506.0", "1200"],
        ["2026-05-18", "505.0", "508.0", "504.0", "507.0", "507.0", "1100"],
        ["2026-05-19", "508.0", "512.0", "507.0", "510.0", "510.0", "1300"],
    ]
    _create_sample_csv(ticker_dir / "SPY.csv", spy_rows)

    dates = get_trading_days(ticker_dir, "SPY")
    assert dates == ["2026-05-14", "2026-05-15", "2026-05-18", "2026-05-19"]

    out_csv = tmp_path / "preds.csv"
    preds = generate_walkforward_baseline_predictions(
        tickers=["SPY"],
        trading_days=dates,
        history_dir=ticker_dir,
        output_path=out_csv,
    )

    # 4 days total, so 3 prediction days
    assert len(preds) == 3
    assert preds[0]["date"] == "2026-05-15"
    # Predicted close for 2026-05-15 should be 2026-05-14's close: 502.0
    assert preds[0]["pred_close"] == 502.0
    # Predicted close for 2026-05-18 should be 2026-05-15's close: 506.0
    assert preds[1]["pred_close"] == 506.0
    assert out_csv.is_file()


def test_main_experiment_orchestration(tmp_path: Path) -> None:
    ticker_dir = tmp_path / "tickers"
    ticker_dir.mkdir()

    spy_rows = [
        ["2026-05-14", "500.0", "505.0", "499.0", "502.0", "502.0", "1000"],
        ["2026-05-15", "503.0", "507.0", "501.0", "506.0", "506.0", "1200"],
        ["2026-05-18", "505.0", "508.0", "504.0", "507.0", "507.0", "1100"],
        ["2026-05-19", "508.0", "512.0", "507.0", "510.0", "510.0", "1300"],
    ]
    _create_sample_csv(ticker_dir / "SPY.csv", spy_rows)

    exp_dir = tmp_path / "experiment"
    cli_args = [
        "--tickers", "SPY",
        "--days", "3",
        "--history-dir", str(ticker_dir),
        "--experiment-dir", str(exp_dir),
        "--model", "persistence",
    ]

    ret = main(cli_args)
    assert ret == 0

    results_dir = exp_dir / "sequestered_results"
    assert (results_dir / "comparison_report.md").is_file()
    assert (results_dir / "comparison_metrics.json").is_file()
    assert (results_dir / "daily_comparison_ledger.csv").is_file()

    data = json.loads((results_dir / "comparison_metrics.json").read_text(encoding="utf-8"))
    assert "SPY" in data["ticker_metrics"]
    assert data["ticker_metrics"]["SPY"]["days_count"] == 3
