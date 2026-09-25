"""Tests for scripts/export_octopus_ledger.py."""

from __future__ import annotations

from pathlib import Path

from scripts.export_octopus_ledger import load_scheduled_tickers, main


def test_load_scheduled_tickers() -> None:
    tickers = load_scheduled_tickers()
    assert len(tickers) >= 50
    assert "AAPL" in tickers
    assert "MSFT" in tickers
    assert "NVDA" in tickers


def test_main_cli_missing_dir(tmp_path: Path) -> None:
    missing_dir = tmp_path / "nonexistent"
    ret = main(["--run-dir", str(missing_dir)])
    assert ret == 1
