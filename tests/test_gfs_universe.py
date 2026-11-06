"""GFS (GlobalFoundries) joins the active DCA universe (2026-10-14, user-requested).

The cross-mirror parity checks live in test_config_invariants.py and
test_perplexity_prompt_mirror.py; this file pins the GFS-specific facts.
"""
import json
from pathlib import Path

import near_close_dca

CONFIG = Path(__file__).resolve().parent.parent / "config"


def _load(name):
    return json.loads((CONFIG / name).read_text())


def test_gfs_is_active_in_constant_and_config():
    assert "GFS" in near_close_dca.ACTIVE_TICKERS
    assert "GFS" not in near_close_dca.EXCLUDED_TICKERS
    assert "GFS" in _load("tickers.json")["active"]


def test_gfs_has_a_calibrated_sizing_entry():
    # Calibrated against the ticker_data archive on 2026-10-20 (it had no history when added).
    sizing = _load("per_ticker_sizing.json")
    assert "GFS" not in sizing["fallback_tickers"]
    entry = sizing["tickers"]["GFS"]
    assert entry["source"] == "ninety_day_adjusted_daily_low_percentiles"
    assert entry["sample_sessions"] >= 40


def test_active_count_is_91_everywhere():
    assert len(_load("tickers.json")["active"]) == 122
    policy = _load("scheduled_tasks.json")["ticker_universe_policy"]
    assert policy["active_count"] == 122
