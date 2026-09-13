"""Tests for Notion sync payload builder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dca_notion_sync import (
    _fmt,
    build_compact_sync_markdown,
    build_notion_sync_payload,
    load_latest_run_summary,
    write_compact_sync,
)


def test_fmt():
    assert _fmt(None) == "n/a"
    assert _fmt(10) == "10"
    assert _fmt(10.5, " USD") == "10.5 USD"
    assert _fmt(0) == "0"
    assert _fmt("test") == "test"


def test_build_compact_sync_markdown_empty():
    summary = {}
    md = build_compact_sync_markdown(summary)
    assert "## Latest Run — Compact Sync" in md
    assert "**Run** `n/a`" in md
    assert "Buy candidates | n/a" in md
    assert "Fills / coverage | n/a / n/a" in md


def test_build_compact_sync_markdown_populated():
    summary = {
        "run_id": "r-123",
        "trading_day": "2023-01-01",
        "broker": "ALPACA",
        "dry_run": False,
        "coverage": {
            "buy_candidates": 5,
            "fills": 4,
            "coverage_pct": 80.0,
            "missing_fills": 1,
        },
        "execution_quality": {
            "avg_slippage_pct": 0.05,
            "max_slippage_pct": 0.1,
            "target_price_missing": 0,
            "worst_ticker": "AAPL",
        },
        "budget": {
            "budget_tier": "HIGH",
            "day_notional_usd": 1000.5,
            "planned_notional_usd": 1000.0,
            "weekly_rollover_reserve_usd": 500.0,
            "week_notional_usd": 3000.0,
            "weekly_cap_usd": 5000.0,
        },
        "manual_review_tickers": ["TSLA", "MSFT"],
    }
    md = build_compact_sync_markdown(summary)

    assert "## Latest Run — Compact Sync" in md
    assert "**Run** `r-123` · 2023-01-01 · broker: ALPACA · dry_run: False" in md
    assert "| Buy candidates | 5 |" in md
    assert "| Fills / coverage | 4 / 80.0% |" in md
    assert "| Missing fills | 1 |" in md
    assert "| Avg slippage | 0.05% |" in md
    assert "| Max slippage | 0.1% |" in md
    assert "| target_price_missing | 0 |" in md
    assert "| Worst ticker | AAPL |" in md
    assert "| Budget tier | HIGH |" in md
    assert "| Spent today | 1000.5 USD |" in md
    assert "| Planned notional | 1000.0 USD |" in md
    assert "| Weekly rollover reserve | 500.0 USD |" in md
    assert "| Week notional / cap | 3000.0 USD / 5000.0 USD |" in md
    assert "**Manual review:** TSLA, MSFT" in md


def test_build_notion_sync_payload():
    summary = {
        "run_id": "r-123",
        "trading_day": "2023-01-01",
        "coverage": {"buy_candidates": 2},
    }
    payload = build_notion_sync_payload(summary, page_id="page-123")
    assert payload["title"] == "Latest Run — Compact Sync"
    assert payload["page_id"] == "page-123"
    assert payload["trading_day"] == "2023-01-01"
    assert payload["run_id"] == "r-123"
    assert "## Latest Run" in payload["markdown"]
    assert payload["metrics"]["coverage"]["buy_candidates"] == 2
    assert payload["metrics"]["execution_quality"] == {}
    assert payload["metrics"]["budget"] == {}


def test_write_and_load_compact_sync(tmp_path):
    summary = {
        "run_id": "r-123",
        "trading_day": "2023-01-01",
    }
    paths = write_compact_sync(summary, tmp_path)

    md_path = Path(paths["markdown"])
    payload_path = Path(paths["payload"])

    assert md_path.exists()
    assert payload_path.exists()

    assert "## Latest Run" in md_path.read_text()


def test_write_compact_sync_string_path(tmp_path):
    summary = {
        "run_id": "r-456",
        "trading_day": "2023-01-02",
        "coverage": {"buy_candidates": 3},
    }
    # Pass a string path rather than a Path object
    str_path = str(tmp_path)
    paths = write_compact_sync(summary, str_path)

    md_path = Path(paths["markdown"])
    payload_path = Path(paths["payload"])

    assert md_path.exists()
    assert payload_path.exists()

    assert "## Latest Run" in md_path.read_text()

    # Verify the JSON payload content
    payload_data = json.loads(payload_path.read_bytes())
    assert payload_data["run_id"] == "r-456"
    assert payload_data["trading_day"] == "2023-01-02"
    assert payload_data["metrics"]["coverage"]["buy_candidates"] == 3
    assert "## Latest Run" in payload_data["markdown"]


def test_write_compact_sync_nested_dir(tmp_path):
    summary = {
        "run_id": "r-789",
        "trading_day": "2023-01-03",
    }
    # Use a nested directory that does not exist yet to test parents=True
    nested_dir = tmp_path / "deep" / "nested" / "dir"
    paths = write_compact_sync(summary, nested_dir)

    md_path = Path(paths["markdown"])
    payload_path = Path(paths["payload"])

    assert md_path.exists()
    assert payload_path.exists()
    assert md_path.parent == nested_dir
    assert payload_path.parent == nested_dir


def test_load_latest_run_summary_success(tmp_path):
    summary_path = tmp_path / "latest_run_summary.json"
    summary_path.write_text('{"run_id": "r-123", "trading_day": "2023-01-01"}')

    # test with Path object
    loaded_summary = load_latest_run_summary(summary_path)
    assert loaded_summary["run_id"] == "r-123"
    assert loaded_summary["trading_day"] == "2023-01-01"

    # test with string path
    loaded_summary_str = load_latest_run_summary(str(summary_path))
    assert loaded_summary_str["run_id"] == "r-123"
    assert loaded_summary_str["trading_day"] == "2023-01-01"


def test_load_latest_run_summary_not_found(tmp_path):
    summary_path = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        load_latest_run_summary(summary_path)


def test_load_latest_run_summary_invalid_json(tmp_path):
    summary_path = tmp_path / "invalid.json"
    summary_path.write_text('{"run_id": "r-123", ')
    with pytest.raises(json.JSONDecodeError):
        load_latest_run_summary(summary_path)


def test_load_latest_run_summary_io_error(tmp_path):
    summary_path = tmp_path / "is_directory"
    summary_path.mkdir()
    with pytest.raises(IsADirectoryError):
        load_latest_run_summary(summary_path)
