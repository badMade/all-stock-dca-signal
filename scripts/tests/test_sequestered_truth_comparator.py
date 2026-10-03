"""Tests for scripts/sequestered_truth_comparator.py."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.sequestered_truth_comparator import (
    DailyRecord,
    DataIntegrityError,
    SequesteredComparator,
    export_daily_ledger_csv,
    load_predictions_from_csv,
    main,
    render_markdown_report,
)


def test_daily_record_validation() -> None:
    """DailyRecord rejects invalid types, booleans, negative values, and non-finite floats."""
    # Valid
    rec = DailyRecord(date="2026-05-14", ticker="AAPL", actual_close=150.0, pred_close=152.0)
    assert rec.ticker == "AAPL"

    # Boolean as float rejected
    with pytest.raises(DataIntegrityError, match="cannot be a boolean"):
        DailyRecord(date="2026-05-14", ticker="AAPL", actual_close=True, pred_close=150.0)  # type: ignore

    # NaN rejected
    with pytest.raises(DataIntegrityError, match="must be a finite number"):
        DailyRecord(date="2026-05-14", ticker="AAPL", actual_close=float("nan"), pred_close=150.0)

    # Infinity rejected
    with pytest.raises(DataIntegrityError, match="must be a finite number"):
        DailyRecord(date="2026-05-14", ticker="AAPL", actual_close=float("inf"), pred_close=150.0)

    # Negative price rejected
    with pytest.raises(DataIntegrityError, match="must be non-negative"):
        DailyRecord(date="2026-05-14", ticker="AAPL", actual_close=-10.0, pred_close=150.0)


def test_evaluate_ticker_perfect_prediction() -> None:
    """When predictions exactly match actuals, MAE is 0 and directional hit rate is 100%."""
    actuals = {
        "2026-05-14": {"open": 100.0, "high": 105.0, "low": 99.0, "close": 100.0},
        "2026-05-15": {"open": 101.0, "high": 107.0, "low": 100.0, "close": 105.0},  # +5%
        "2026-05-18": {"open": 104.0, "high": 106.0, "low": 101.0, "close": 102.0},  # -2.85%
        "2026-05-19": {"open": 102.0, "high": 109.0, "low": 102.0, "close": 108.0},  # +5.88%
    }

    preds = [
        {"date": "2026-05-15", "ticker": "XYZ", "pred_close": 105.0, "pred_close_p10": 102.0, "pred_close_p90": 108.0},
        {"date": "2026-05-18", "ticker": "XYZ", "pred_close": 102.0, "pred_close_p10": 99.0, "pred_close_p90": 105.0},
        {"date": "2026-05-19", "ticker": "XYZ", "pred_close": 108.0, "pred_close_p10": 105.0, "pred_close_p90": 110.0},
    ]

    comparator = SequesteredComparator()
    metrics, aligned = comparator.evaluate_ticker_series("XYZ", preds, actuals)

    assert len(aligned) == 3
    assert metrics.close_mae == 0.0
    assert metrics.close_rmse == 0.0
    assert metrics.close_mape_pct == 0.0
    assert metrics.directional_accuracy_pct == 100.0
    assert metrics.directional_hits == 3
    assert metrics.band_coverage_pct == 100.0
    assert metrics.band_hits == 3
    assert metrics.cumulative_return_drift_pct == 0.0


def test_evaluate_ticker_inverse_direction() -> None:
    """When predictions move in the exact opposite direction, directional hit rate is 0%."""
    actuals = {
        "2026-05-14": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        "2026-05-15": {"open": 101.0, "high": 110.0, "low": 100.0, "close": 110.0},  # Actual went UP
        "2026-05-18": {"open": 109.0, "high": 110.0, "low": 95.0, "close": 95.0},  # Actual went DOWN
    }

    preds = [
        {"date": "2026-05-15", "ticker": "XYZ", "pred_close": 90.0},  # Predicted DOWN
        {"date": "2026-05-18", "ticker": "XYZ", "pred_close": 120.0},  # Predicted UP
    ]

    comparator = SequesteredComparator()
    metrics, aligned = comparator.evaluate_ticker_series("XYZ", preds, actuals)

    assert len(aligned) == 2
    assert metrics.directional_accuracy_pct == 0.0
    assert metrics.directional_hits == 0
    assert metrics.directional_opportunities == 2
    assert metrics.close_mae == 22.5  # (|90-110| + |120-95|) / 2 = (20 + 25) / 2 = 22.5


def test_reject_conflicting_duplicate_predictions() -> None:
    """Duplicate predictions with different prices for the same day raise DataIntegrityError."""
    actuals = {
        "2026-05-15": {"open": 100.0, "high": 105.0, "low": 99.0, "close": 100.0},
    }
    preds = [
        {"date": "2026-05-15", "ticker": "XYZ", "pred_close": 100.0},
        {"date": "2026-05-15", "ticker": "XYZ", "pred_close": 105.0},  # Conflict!
    ]

    comparator = SequesteredComparator()
    with pytest.raises(DataIntegrityError, match="Duplicate conflicting predictions"):
        comparator.evaluate_ticker_series("XYZ", preds, actuals)


def test_csv_loading_and_multi_ticker_evaluation(tmp_path: Path) -> None:
    """Test full load from CSV and multi-ticker aggregation."""
    # Write synthetic ticker history CSV
    ticker_dir = tmp_path / "ticker_data"
    ticker_dir.mkdir()

    aapl_csv = ticker_dir / "AAPL.csv"
    with aapl_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"])
        writer.writerow(["2026-05-14", "180.0", "182.0", "179.0", "180.0", "180.0", "1000"])
        writer.writerow(["2026-05-15", "181.0", "185.0", "180.0", "184.0", "184.0", "1200"])
        writer.writerow(["2026-05-18", "183.0", "184.0", "181.0", "182.0", "182.0", "1100"])

    msft_csv = ticker_dir / "MSFT.csv"
    with msft_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"])
        writer.writerow(["2026-05-14", "400.0", "405.0", "399.0", "400.0", "400.0", "1000"])
        writer.writerow(["2026-05-15", "402.0", "410.0", "401.0", "408.0", "408.0", "1200"])
        writer.writerow(["2026-05-18", "407.0", "412.0", "406.0", "411.0", "411.0", "1100"])

    # Write prediction CSV
    pred_csv = tmp_path / "preds.csv"
    with pred_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "ticker", "pred_close", "pred_close_p10", "pred_close_p90"])
        writer.writerow(["2026-05-15", "AAPL", "183.0", "180.0", "186.0"])
        writer.writerow(["2026-05-18", "AAPL", "181.0", "178.0", "184.0"])
        writer.writerow(["2026-05-15", "MSFT", "406.0", "400.0", "412.0"])
        writer.writerow(["2026-05-18", "MSFT", "410.0", "405.0", "415.0"])

    comparator = SequesteredComparator(ground_truth_dir=ticker_dir)
    preds = load_predictions_from_csv(pred_csv)
    summary, records = comparator.evaluate_all(preds)

    assert summary.total_days == 2
    assert summary.tickers_evaluated == ["AAPL", "MSFT"]
    assert len(records) == 4

    # Generate Markdown and export CSV
    report_md = tmp_path / "report.md"
    md_content = render_markdown_report(summary, records)
    report_md.write_text(md_content, encoding="utf-8")
    assert "Sequestered Truth Comparator: 90-Day Evaluation Report" in md_content
    assert "AAPL" in md_content
    assert "MSFT" in md_content

    ledger_csv = tmp_path / "ledger.csv"
    export_daily_ledger_csv(records, ledger_csv)
    assert ledger_csv.is_file()

    # CLI main execution
    cli_json = tmp_path / "summary.json"
    cli_args = [
        "--predictions",
        str(pred_csv),
        "--history-dir",
        str(ticker_dir),
        "--json-out",
        str(cli_json),
    ]
    exit_code = main(cli_args)
    assert exit_code == 0
    assert cli_json.is_file()
    data = json.loads(cli_json.read_bytes())
    assert data["total_days"] == 2
    assert "AAPL" in data["ticker_metrics"]
    assert "MSFT" in data["ticker_metrics"]
