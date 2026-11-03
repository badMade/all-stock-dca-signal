"""Tests for scripts/publish_octopus_sizing.py."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dca_config import load_ml_sizing_adjustment
from scripts.publish_octopus_sizing import (
    evaluate_tier_c_candidates,
    load_active_universe,
    publish_octopus_sizing,
)
from sizing_suggestion_history import read_history


@pytest.fixture
def sample_tickers_json(tmp_path: Path) -> Path:
    tickers_file = tmp_path / "tickers.json"
    data = {
        "$schema": "../schemas/tickers.schema.json",
        "version": "2026-09-21",
        "active": ["AAPL", "NVDA", "MSFT", "GOOGL"],
        "excluded": [],
        "whole_share": [],
    }
    tickers_file.write_text(json.dumps(data), encoding="utf-8")
    return tickers_file


@pytest.fixture
def sample_forecasts_csv(tmp_path: Path) -> Path:
    csv_file = tmp_path / "sample_forecasts.csv"
    rows = [
        {
            "ticker": "AAPL",
            "model": "octopus",
            "origin_date": "2026-09-18",
            "ref_close": "100.0",
            "close_return": "0.005",
            "band_width": "0.04",
            "pred_close": "100.5",
            "close_p10": "98.0",
            "close_p90": "103.0",  # p90_ret = +3.0% > 1.5% -> Tier C trigger
            "shard": "shard_1",
        },
        {
            "ticker": "NVDA",
            "model": "octopus",
            "origin_date": "2026-09-18",
            "ref_close": "100.0",
            "close_return": "-0.01",
            "band_width": "0.08",
            "pred_close": "99.0",
            "close_p10": "96.0",
            "close_p90": "105.0",  # p90_ret = +5.0% > 1.5% -> Tier C trigger
            "shard": "shard_1",
        },
        {
            "ticker": "MSFT",
            "model": "octopus",
            "origin_date": "2026-09-18",
            "ref_close": "100.0",
            "close_return": "0.001",
            "band_width": "0.02",
            "pred_close": "100.1",
            "close_p10": "99.5",
            "close_p90": "100.8",  # p90_ret = +0.8% < 1.5% -> Not Tier C
            "shard": "shard_1",
        },
        {
            "ticker": "NON_ACTIVE",
            "model": "octopus",
            "origin_date": "2026-09-18",
            "ref_close": "50.0",
            "close_return": "0.02",
            "band_width": "0.05",
            "pred_close": "51.0",
            "close_p10": "49.0",
            "close_p90": "53.0",
            "shard": "shard_1",
        },
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return csv_file


def test_load_active_universe(sample_tickers_json: Path):
    active = load_active_universe(sample_tickers_json)
    assert active == {"AAPL", "NVDA", "MSFT", "GOOGL"}


def test_evaluate_tier_c_candidates(sample_forecasts_csv: Path):
    active = {"AAPL", "NVDA", "MSFT"}
    adjustments, metadata = evaluate_tier_c_candidates(sample_forecasts_csv, active)
    assert "AAPL" in adjustments
    assert "NVDA" in adjustments
    assert "MSFT" not in adjustments
    assert "NON_ACTIVE" not in adjustments
    assert metadata["tier_c_count"] == 2
    assert adjustments["NVDA"] > adjustments["AAPL"]  # Higher p90 gives higher recovery bonus


def test_publish_records_to_history_and_never_writes_live_config(
    tmp_path: Path, sample_forecasts_csv: Path, sample_tickers_json: Path
):
    live_config_dir = tmp_path / "config"
    live_config_dir.mkdir()
    history = tmp_path / "history"
    now = datetime(2026, 9, 21, 15, 30, tzinfo=timezone.utc)
    result = publish_octopus_sizing(
        sample_forecasts_csv, sample_tickers_json, history_root=history, now=now
    )
    assert result["status"] == "published"
    assert Path(result["history_file"]) == history / "octopus" / "2026.jsonl"
    # Nothing that feeds live sizing was created: no ml_sizing_adjustment.json anywhere,
    # and the loader sees an empty (no-op) adjustment.
    assert list(tmp_path.rglob("ml_sizing_adjustment.json")) == []
    assert load_ml_sizing_adjustment(live_config_dir).get("ml_sizing_adjustment", {}) == {}

    (record,) = read_history("octopus", root=history)
    assert set(record["adjustments"]) == {"AAPL", "NVDA"}
    assert record["bounds"] == {"min_multiplier": 0.8, "max_multiplier": 1.4}
    assert record["source"]["origin_date"] == "2026-09-18"
    assert record["source"]["forecasts_csv"] == sample_forecasts_csv.name


def test_repeated_publishes_accumulate_history(
    tmp_path: Path, sample_forecasts_csv: Path, sample_tickers_json: Path
):
    history = tmp_path / "history"
    for day in (21, 22, 23):
        publish_octopus_sizing(
            sample_forecasts_csv,
            sample_tickers_json,
            history_root=history,
            now=datetime(2026, 9, day, 15, 30, tzinfo=timezone.utc),
        )
    assert [r["recorded_at"][:10] for r in read_history("octopus", root=history)] == [
        "2026-09-21",
        "2026-09-22",
        "2026-09-23",
    ]


def test_dry_run_records_nothing(tmp_path: Path, sample_forecasts_csv: Path, sample_tickers_json: Path):
    history = tmp_path / "history"
    result = publish_octopus_sizing(sample_forecasts_csv, sample_tickers_json, history_root=history, dry_run=True)
    assert result["status"] == "dry_run"
    assert not history.exists()


def test_cli_has_no_flag_that_targets_live_config():
    import scripts.publish_octopus_sizing as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "--output" not in source and "atomic_write_text" not in source
