"""Unit tests for dca_console_tickers.py (universe breakdown and data freshness)."""

from datetime import date
from pathlib import Path

from dca_console_tickers import _inspect_csv, build_tickers_snapshot


def test_inspect_csv_missing(tmp_path: Path):
    nonexistent = tmp_path / "NONEXISTENT.csv"
    res = _inspect_csv(nonexistent, date(2026, 10, 2))
    assert res["exists"] is False
    assert res["freshness"] == "missing"
    assert res["last_date"] is None


def test_inspect_csv_up_to_date(tmp_path: Path):
    csv_file = tmp_path / "AAPL.csv"
    csv_file.write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n"
        "2026-10-01,300.0,305.0,299.0,302.0,302.0,10000,0,0\n"
        "2026-10-02,302.0,306.0,301.0,304.0,304.0,12000,0,0\n"
    )
    res = _inspect_csv(csv_file, date(2026, 10, 3))
    assert res["exists"] is True
    assert res["last_date"] == "2026-10-02"
    assert res["freshness"] == "up_to_date"
    assert res["total_bars"] == 2


def test_inspect_csv_stale(tmp_path: Path):
    csv_file = tmp_path / "STALE.csv"
    csv_file.write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n"
        "2026-08-01,100.0,105.0,99.0,102.0,102.0,5000,0,0\n"
    )
    res = _inspect_csv(csv_file, date(2026, 10, 3))
    assert res["exists"] is True
    assert res["last_date"] == "2026-08-01"
    assert res["freshness"] == "stale"


def test_build_tickers_snapshot_fallback_when_empty(tmp_path: Path):
    snapshot = build_tickers_snapshot(tmp_path, tmp_path / "empty_config", today=date(2026, 10, 3))
    assert isinstance(snapshot, dict)
    assert "universe" in snapshot
    assert "archive" in snapshot
    assert "summary" in snapshot
    assert "items" in snapshot
    assert len(snapshot["warnings"]) > 0
