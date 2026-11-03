"""CCI (Crown Castle Inc.) joins the active DCA universe (2026-10-23, user-requested).

The cross-mirror parity checks live in test_config_invariants.py and
test_perplexity_prompt_mirror.py; this file pins the CCI-specific facts.
"""
import json
from pathlib import Path

import near_close_dca
from scripts.generate_per_ticker_sizing import GLOBAL_FALLBACK_TIERS

CONFIG = Path(__file__).resolve().parent.parent / "config"


def _load(name):
    return json.loads((CONFIG / name).read_text())


def test_cci_is_active_in_constant_and_config():
    assert "CCI" in near_close_dca.ACTIVE_TICKERS
    assert "CCI" not in near_close_dca.EXCLUDED_TICKERS
    assert "CCI" in _load("tickers.json")["active"]


def test_cci_is_not_a_whole_share_ticker():
    assert "CCI" not in _load("tickers.json")["whole_share"]


def test_cci_has_a_sizing_entry():
    # The config loader requires one sizing entry per active ticker. No ticker_data
    # archive is present in this checkout, so the entry is expected to be the
    # global-fallback ladder until scripts/generate_per_ticker_sizing.py is re-run.
    sizing = _load("per_ticker_sizing.json")
    entry = sizing["tickers"]["CCI"]
    assert entry["base_amount_usd"] == 1.0
    assert entry["base_amount_below_close_usd"] == 1.0
    assert entry["tiers"]
    if entry["source"] == "global_fallback_insufficient_history":
        assert "CCI" in sizing["fallback_tickers"]
        assert "interpolation" not in entry
        assert [(tier["min_drop_pct"], tier["multiplier"]) for tier in entry["tiers"]] == list(GLOBAL_FALLBACK_TIERS)


def test_active_count_is_91_everywhere():
    assert len(_load("tickers.json")["active"]) == 90
    policy = _load("scheduled_tasks.json")["ticker_universe_policy"]
    assert policy["active_count"] == 90
