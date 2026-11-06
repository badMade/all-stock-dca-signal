"""LULU (lululemon athletica inc.) joins the active DCA universe (2026-11-05, user-requested)."""
import json
from pathlib import Path

import near_close_dca

CONFIG = Path(__file__).resolve().parent.parent / "config"


def _load(name):
    return json.loads((CONFIG / name).read_text())


def test_lulu_is_active_in_constant_and_config():
    assert "LULU" in near_close_dca.ACTIVE_TICKERS
    assert "LULU" not in near_close_dca.EXCLUDED_TICKERS
    assert "LULU" in _load("tickers.json")["active"]


def test_lulu_has_a_calibrated_sizing_entry():
    entry = _load("per_ticker_sizing.json")["tickers"]["LULU"]
    assert entry["source"] == "ninety_day_adjusted_daily_low_percentiles"
    assert entry["tiers"]
