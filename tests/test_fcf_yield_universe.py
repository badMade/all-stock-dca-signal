"""22 free-cash-flow-yield tickers join the active DCA universe (2026-10-19, user-requested).

The cross-mirror parity checks live in test_config_invariants.py and
test_perplexity_prompt_mirror.py; this file pins the facts specific to this addition.
"""
import json
from pathlib import Path

import near_close_dca

CONFIG = Path(__file__).resolve().parent.parent / "config"
NEW = (
    "TSN RPRX CVS SJM KR CAH HCA HRL PFGC SYY HSY USFD MRK RMD DGX CHE ABT MDLZ MCK CHD CL"
).split()


# KVUE was part of the 2026-10-19 request; it was excluded 2026-11-02 (not-found on every live run), see
# test_kvue_is_excluded below.


def _load(name):
    return json.loads((CONFIG / name).read_text())


def test_all_new_tickers_are_active_in_constant_and_config():
    config_active = _load("tickers.json")["active"]
    for ticker in NEW:
        if ticker == "KVUE":
            # Moved to excluded 2026-11-02 (Robinhood inactive instrument) -- see test_kvue_exclusion.py.
            continue
        assert ticker in near_close_dca.ACTIVE_TICKERS, ticker
        assert ticker not in near_close_dca.EXCLUDED_TICKERS, ticker
        assert ticker in config_active, ticker


def test_abbv_was_not_duplicated():
    # ABBV was in the request but has been active since 2026-08-02.
    assert near_close_dca.ACTIVE_TICKERS.count("ABBV") == 1


def test_new_tickers_have_calibrated_sizing_entries():
    # Calibrated against the ticker_data archive on 2026-10-20 (a stopgap from Yahoo history
    # preceded it): every one clears MINIMUM_HISTORY_SESSIONS, so none is a fallback ticker.
    sizing = _load("per_ticker_sizing.json")
    for ticker in NEW:
        assert ticker not in sizing["fallback_tickers"], ticker
        entry = sizing["tickers"][ticker]
        assert entry["source"] == "ninety_day_adjusted_daily_low_percentiles", ticker
        assert entry["sample_sessions"] >= 40, ticker
        assert entry["interpolation"] == "linear", ticker


def test_new_tickers_are_in_the_history_fetch_list():
    listed = set((CONFIG.parent / "tickers.txt").read_text().split())
    assert set(NEW) <= listed


def test_kvue_is_excluded_not_active():
    tickers = _load("tickers.json")
    assert "KVUE" not in tickers["active"] and "KVUE" in tickers["excluded"]
    assert "KVUE" not in near_close_dca.ACTIVE_TICKERS and "KVUE" in near_close_dca.EXCLUDED_TICKERS
