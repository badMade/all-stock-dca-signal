"""Tests for scripts/publish_octopus_sizing.py."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dca_config import load_ml_sizing_adjustment
from scripts.publish_octopus_sizing import (
    build_sizing_payload,
    evaluate_tier_c_candidates,
    load_active_universe,
    publish_octopus_sizing,
)


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


def test_build_sizing_payload():
    now = datetime(2026, 9, 21, 15, 30, tzinfo=timezone.utc)
    adjustments = {"AAPL": 1.25, "NVDA": 1.30}
    payload = build_sizing_payload(adjustments, status="active", now=now)
    assert payload["status"] == "active"
    assert payload["bounds"]["min_multiplier"] == 0.8
    assert payload["bounds"]["max_multiplier"] == 1.4
    assert payload["adjustments"] == {"AAPL": 1.25, "NVDA": 1.30}
    assert payload["generated_at"] == now.isoformat()


def test_publish_octopus_sizing_end_to_end(
    tmp_path: Path, sample_forecasts_csv: Path, sample_tickers_json: Path
):
    output_path = tmp_path / "ml_sizing_adjustment.json"
    result = publish_octopus_sizing(
        sample_forecasts_csv,
        sample_tickers_json,
        output_path,
        status="active",
    )
    assert result["status"] == "published"
    assert output_path.exists()

    # Verify that dca_config.load_ml_sizing_adjustment accepts the published output
    loaded = load_ml_sizing_adjustment(tmp_path)
    assert "ml_sizing_adjustment" in loaded
    adj = loaded["ml_sizing_adjustment"]
    assert adj["AAPL"] >= 1.25
    assert adj["NVDA"] >= 1.25
    assert "MSFT" not in adj
