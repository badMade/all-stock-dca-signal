"""KVUE (Kenvue) moves from active to excluded (2026-11-02).

Robinhood reported KVUE as an inactive instrument (get_equity_quotes returned
{"inactive_instruments": ["KVUE"]}) when the 9:35 payload publisher refused on 2026-11-02, so
there is no mechanism to buy it -- handled the same way as QBUT. The cross-mirror parity checks
live in test_config_invariants.py; this file pins the KVUE-specific facts.
"""
import json
from pathlib import Path

import near_close_dca

CONFIG = Path(__file__).resolve().parent.parent / "config"


def _load(name):
    return json.loads((CONFIG / name).read_text())


def test_kvue_is_excluded_in_config():
    tickers = _load("tickers.json")
    assert "KVUE" in tickers["excluded"]
    assert "KVUE" not in tickers["active"]
    assert tickers["exclusion_reasons"]["KVUE"]


def test_kvue_is_excluded_in_constants():
    assert "KVUE" in near_close_dca.EXCLUDED_TICKERS
    assert "KVUE" not in near_close_dca.ACTIVE_TICKERS


def test_kvue_excluded_in_scheduled_tasks_mirror():
    universe = _load("scheduled_tasks.json")["canonical_first_buy_and_topup_rule"]["active_universe"]
    assert "KVUE" in universe["excluded"]
    assert "KVUE" not in universe["active"]


def test_active_count_is_90_everywhere():
    assert len(_load("tickers.json")["active"]) == 122
    assert _load("scheduled_tasks.json")["ticker_universe_policy"]["active_count"] == 122
