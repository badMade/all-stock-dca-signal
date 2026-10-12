"""Stage 2 / PR A contract: the 12:30 stream is scored from Robinhood, like 09:35, 10:30 and the intraday slots.

Owner direction (2026-10-08): nothing is special about 12:30. It must not need the Finance connector, a yfinance
prefetch, or a per-ticker source exception, and no live prompt may name a stock (the universe lives in
``config/tickers.json``). These tests were written before the change and fail until it lands.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from test_generate_midday_candidates import (
    DAY,
    REAL_CONFIG_DIR,
    _config,
    _only,
    _ticker_payload,
)

import scripts.generate_midday_candidates as gmc
from near_close_dca import SourceName

ROOT = Path(__file__).resolve().parents[2]
LIVE_PROMPT = ROOT / "claude" / "dca-executor-prompts" / "live-mirror" / "1230ET-1230-pm-midday-buying-run.txt"


def _robinhood_payload() -> dict:
    # Robinhood's native cadence: 5-minute bars, the falling-trend shape the 12:30 slot validates.
    return _ticker_payload(early_price=100.0, late_price=96.0, previous_close=100.0, previous_low=99.0, step_minutes=5)


def _run(config, market_data):
    return gmc.generate_midday_candidates(
        trading_day=DAY,
        config=config,
        market_data=market_data,
        deployed_last_7d_usd=0.0,
        config_dir=REAL_CONFIG_DIR,
        check_planner_compatibility=False,
    )


def test_spec_is_robinhood_primary_with_no_per_ticker_exception():
    spec = gmc.MIDDAY_SPEC
    assert spec.source is SourceName.ROBINHOOD
    assert spec.source_interval_minutes is None  # native 5-minute cadence, as every other Robinhood-primary slot
    assert spec.trend_min_bars is None
    assert dict(spec.ticker_source_overrides) == {}


def test_five_minute_robinhood_bars_validate_the_trend_and_buy():
    document = _run(_config(), {"TEST": _robinhood_payload()})
    row = _only(document)
    assert row["trend_validated"] is True
    assert [c["ticker"] for c in document["cands"]] == ["TEST"]


def test_every_ticker_is_scored_alike_with_no_alternate_source():
    # GFUZ used to be routed to Robinhood by name while everything else was Finance; now it is just another row.
    document = _run(_config(tickers=("GFUZ",)), {"GFUZ": _robinhood_payload()})
    assert [c["ticker"] for c in document["cands"]] == ["GFUZ"]


def test_midday_config_keeps_robinhood_primary_and_documents_yfinance_fallback():
    config = json.loads((ROOT / "config" / "midday_dca.json").read_text())
    data_sources = config["raw_input_generation"]["data_sources"].lower()
    assert "robinhood is the primary signal source" in data_sources
    assert "explicitly labeled, prefetched yfinance fallback" in data_sources
    assert "finance connector primary" not in data_sources
    assert "gfuz" not in json.dumps(config).lower()


def test_midday_scheduled_task_source_policy_matches_generator():
    config = json.loads((ROOT / "config" / "scheduled_tasks.json").read_text())
    policy = config["market_data_source_policy"]["slot_overrides"]["1230ET"]
    assert policy["primary_source"] == "robinhood"
    assert policy["accepted_sources"] == ["robinhood", "yahoo_finance_yfinance"]
    data_sources = config["midday_raw_input_and_failure_state_fix_2026_07_20"]["raw_input_generation"][
        "data_sources"
    ].lower()
    assert "robinhood" in data_sources
    assert "explicitly labeled yfinance fallback" in data_sources
    assert "finance connector primary" not in data_sources


def test_live_prompt_fetches_robinhood_bars_with_yfinance_only_as_backup():
    prompt = LIVE_PROMPT.read_text()
    lowered = prompt.lower()
    assert "get_equity_historicals" in prompt
    assert "5minute" in prompt
    # yfinance appears only as the labeled backup block every Robinhood-primary slot carries, never as a requirement.
    assert "finance connector" not in lowered
    assert "sources.finance" not in lowered
    assert '"finance"' not in lowered
    assert "except gfuz" not in lowered


def test_live_prompt_names_no_stock():
    prompt = LIVE_PROMPT.read_text()
    tickers = json.loads((ROOT / "config" / "tickers.json").read_text())
    universe = set(tickers["active"]) | set(tickers["whole_share"]) | set(tickers["excluded"]) | {"GFUZ"}
    # Word-bounded so a ticker-like substring of ordinary prose is not a false hit; the account placeholder and
    # tool names are lowercase or contain underscores, so they never match.
    found = sorted(t for t in universe if re.search(rf"(?<![A-Za-z0-9_]){re.escape(t)}(?![A-Za-z0-9_])", prompt))
    assert found == [], found


def test_the_active_midday_task_entry_is_robinhood_primary_not_finance():
    """Copilot's review of #1643: the live task entry (a6239dae) is what a deployed 12:30 run follows.

    The source policy and the separate 2026-07-20 fix block already said Robinhood, but the task entry itself still told
    the raw-input producer to use the Finance connector, so the deployed path could fetch Finance-shaped inputs.
    """
    config = json.loads((ROOT / "config" / "scheduled_tasks.json").read_text())
    tasks = [t for t in config["tasks"] if t.get("id") == "a6239dae"]
    assert len(tasks) == 1 and tasks[0]["status"] == "active" and tasks[0]["executor"] == "MiddayDCAExecutor"
    data_sources = tasks[0]["raw_input_generation"]["data_sources"].lower()
    assert "robinhood" in data_sources and "explicitly labeled yfinance fallback" in data_sources
    assert "finance connector primary" not in data_sources
    assert "alpaca" not in data_sources  # no second validation source is part of the 12:30 inputs any more
