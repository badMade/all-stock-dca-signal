"""Enforcement of the dated ``no_buy`` window across the entry points (buy-time veto).

Written test-first, from docs/adr-no-buy-window.md and the operator's 2026-11-20 request, not from the code:
the seven tax-loss tickers are not bought through 2026-12-21 and buying resumes on its own on 2026-12-22.

The design point these tests pin (the first attempt, which folded blocked names into ``excluded_tickers`` and
removed them from the run-time ticker list, broke 33 tests: the backup planner's stamped universe hash and symbol
count, the committed Perplexity prompts, the payload publisher): the UNIVERSE DOES NOT CHANGE. A blocked ticker is
still in ``tickers`` and is still scored, and its BUY is vetoed at the point each entry point turns a decision into
an order. The pure rule is covered in ``test_no_buy_window.py``.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import date, datetime

import pytest
from test_morning_dca import FixedDropProvider, _now

import dca_config
import dca_runner
from dca_config import get_config_dir, load_strategy_config, load_tickers
from dca_runner import BudgetTier
from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor
from near_close_dca import (
    EASTERN,
    Action,
    DryRunBroker,
    ProcessCandidateContext,
    ReconciledDecision,
    SourceName,
    StrategyConfig,
    StrategyState,
    _process_buy_candidate,
)

HARVEST = {"DFTX", "RGTI", "LITE", "CBRS", "VCX", "INTC", "SNDK"}
PERMANENT_EXCLUDED = {"QBUT", "KVUE"}
IN_WINDOW = date(2026, 11, 23)  # the first trading day after the sales
LAST_BLOCKED_DAY = date(2026, 12, 21)
FIRST_FREE_DAY = date(2026, 12, 22)


# --- the shared loader: surfaces the set, leaves the universe alone ---------------------------


def test_loader_surfaces_the_blocked_set_inside_the_window():
    assert load_tickers(get_config_dir(), today=IN_WINDOW)["no_buy_tickers"] == frozenset(HARVEST)


def test_loader_leaves_the_universe_and_the_excluded_set_untouched():
    inside = load_tickers(get_config_dir(), today=IN_WINDOW)
    after = load_tickers(get_config_dir(), today=FIRST_FREE_DAY)
    assert inside["tickers"] == after["tickers"], "the universe (and so its stamped hash and count) must not change"
    assert inside["excluded_tickers"] == after["excluded_tickers"] == frozenset(PERMANENT_EXCLUDED)
    assert HARVEST <= set(inside["tickers"])


def test_loader_blocks_through_the_last_day_and_resumes_after_it():
    assert load_tickers(get_config_dir(), today=LAST_BLOCKED_DAY)["no_buy_tickers"] == frozenset(HARVEST)
    assert "no_buy_tickers" not in load_tickers(get_config_dir(), today=FIRST_FREE_DAY)


def test_loader_blocks_nothing_before_the_sale_date():
    assert "no_buy_tickers" not in load_tickers(get_config_dir(), today=date(2026, 11, 19))


def test_loader_default_clock_is_read_through_the_today_seam(monkeypatch):
    monkeypatch.setattr(dca_config, "_today_et", lambda: IN_WINDOW)
    assert load_tickers(get_config_dir())["no_buy_tickers"] == frozenset(HARVEST)
    monkeypatch.setattr(dca_config, "_today_et", lambda: FIRST_FREE_DAY)
    assert "no_buy_tickers" not in load_tickers(get_config_dir())


def test_strategy_config_uses_the_runs_own_clock_not_the_hosts():
    inside = load_strategy_config(get_config_dir(), now=datetime(2026, 11, 23, 15, 30))
    assert inside.no_buy_tickers == frozenset(HARVEST)
    assert HARVEST <= set(inside.tickers) and not (HARVEST & set(inside.excluded_tickers))
    after = load_strategy_config(get_config_dir(), now=datetime(2026, 12, 22, 15, 30))
    assert after.no_buy_tickers == frozenset()
    assert tuple(after.tickers) == tuple(inside.tickers)


def test_a_bare_strategy_config_blocks_nothing():
    assert StrategyConfig().no_buy_tickers == frozenset()
    assert MorningConfig().no_buy_tickers == frozenset()


def test_bad_no_buy_block_fails_the_whole_load(tmp_path):
    shutil.copytree(get_config_dir(), tmp_path / "config")
    path = tmp_path / "config" / "tickers.json"
    cfg = json.loads(path.read_text())
    cfg["no_buy"]["DFTX"]["until"] = "not-a-date"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="no_buy"):
        load_tickers(tmp_path / "config", today=IN_WINDOW)


# --- every stream config carries the set ----------------------------------------------------


@pytest.mark.parametrize(
    ("module_name", "loader_name"),
    [
        ("morning_dca", "load_morning_config"),
        ("market_open_dca", "load_market_open_config"),
        ("midday_dca", "load_midday_config"),
        ("intraday_dca", "load_intraday_config"),
    ],
)
def test_stream_configs_carry_the_blocked_set_and_keep_the_universe(monkeypatch, module_name, loader_name):
    import importlib

    module = importlib.import_module(module_name)
    loader = getattr(module, loader_name)
    kwargs = {"mode": "live"} if module_name == "morning_dca" else {}
    monkeypatch.setattr(dca_config, "_today_et", lambda: FIRST_FREE_DAY)
    free = loader(get_config_dir(), **kwargs)
    monkeypatch.setattr(dca_config, "_today_et", lambda: IN_WINDOW)
    config = loader(get_config_dir(), **kwargs)
    assert config.no_buy_tickers == frozenset(HARVEST)
    assert tuple(config.tickers) == tuple(free.tickers)
    assert config.excluded_tickers == free.excluded_tickers


# --- near_close_dca (the 3:30 PM path): the BUY is vetoed, nothing is built ----------------


def _near_close_result(config: StrategyConfig, ticker: str) -> tuple:
    decision = ReconciledDecision(
        ticker=ticker,
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0,
        suggested_amount_usd=2.0,
        selected_base_amount_usd=2.0,
        target_price=98.0,
    )
    broker = DryRunBroker(cash=500.0, reference_prices={ticker: 98.0})
    return _process_buy_candidate(
        decision,
        ctx=ProcessCandidateContext(
            config=config,
            state=StrategyState(),
            broker=broker,
            robinhood_data=None,
            now=datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN),
            trading_day=date(2026, 6, 29),
            guard_active=False,
            prior_same_day_allocations={},
            scheduled=False,
            run_result=None,
        ),
        running_deployed_7d=0.0,
    )


def _near_close_outcome(config: StrategyConfig, ticker: str) -> dict:
    return _near_close_result(config, ticker)[0]


def test_near_close_vetoes_a_blocked_buy_without_building_an_order():
    outcome, top_up, running, errors, reviews = _near_close_result(
        StrategyConfig(no_buy_tickers=frozenset({"AAPL"})), "AAPL"
    )
    assert outcome == {"status": "skipped", "order_id": None, "skip_reason": "no_buy_window"}
    assert (top_up, running, errors, reviews) == (None, 0.0, 0, 0)  # no order review, no spend, no error


def test_near_close_still_buys_an_unblocked_ticker_in_the_same_config():
    outcome = _near_close_outcome(StrategyConfig(no_buy_tickers=frozenset({"AAPL"})), "NVDA")
    assert outcome["status"] in {"submitted", "dry_run"}


def test_near_close_buys_normally_when_nothing_is_blocked():
    assert _near_close_outcome(StrategyConfig(), "AAPL")["status"] in {"submitted", "dry_run"}


# --- morning_dca and its three subclasses (and scripts/stream_candidates, which calls it) ----


def _morning_signal(no_buy: frozenset, ticker: str = "AAPL"):
    cfg = MorningConfig(tickers=("AAPL", "NVDA"), no_buy_tickers=no_buy)
    executor = MorningDCAExecutor(cfg, mode=MORNING_LIVE_MODE)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"AAPL": 0.05, "NVDA": 0.05})
    return executor.evaluate_signal(ticker, _now(), data)


def test_morning_scores_a_blocked_ticker_but_withdraws_its_buy():
    free = _morning_signal(frozenset())
    assert free.action == Action.BUY and free.multiplier > 0  # the control: this drop is a BUY
    blocked = _morning_signal(frozenset({"AAPL"}))
    assert blocked.action == Action.NO_ACTION
    assert blocked.multiplier == 0.0 and blocked.hypothetical_amount_usd == 0.0
    assert "no_buy_window" in blocked.reason and "no_buy_window" in blocked.warnings
    # Tracking is kept: the row still carries the prices and the drop the ladder saw.
    assert blocked.current_price == free.current_price and blocked.drop_pct == free.drop_pct


def test_morning_leaves_other_tickers_alone():
    assert _morning_signal(frozenset({"AAPL"}), "NVDA").action == Action.BUY


def test_morning_buy_candidates_exclude_a_blocked_ticker():
    signals = [_morning_signal(frozenset({"AAPL"}), "AAPL"), _morning_signal(frozenset({"AAPL"}), "NVDA")]
    assert [s.ticker for s in MorningDCAExecutor._buy_candidates(signals)] == ["NVDA"]


# --- dca_runner reads tickers.json itself ---------------------------------------------------


def _plan(on: date, *tickers: str):
    return dca_runner.plan_orders(
        candidates=[dca_runner.Candidate(t, 2.0) for t in tickers],
        snapshot=dca_runner.budget_snapshot([], on),
        tier=BudgetTier("normal", 180.0, 900.0),
    )


def test_runner_refuses_a_blocked_ticker_inside_the_window_and_places_the_rest():
    plan = _plan(IN_WINDOW, "DFTX", "AAPL")
    assert [c.ticker for c in plan.to_place] == ["AAPL"]
    assert ["DFTX", dca_runner.REFUSED_NO_BUY_WINDOW] in [list(x) for x in plan.refused]


def test_runner_allows_the_ticker_again_on_the_first_free_day():
    assert [c.ticker for c in _plan(FIRST_FREE_DAY, "DFTX").to_place] == ["DFTX"]


def test_runner_refuses_every_harvest_ticker_inside_the_window():
    plan = _plan(IN_WINDOW, *sorted(HARVEST))
    assert plan.to_place == []
    assert {t for t, reason in plan.refused if reason == dca_runner.REFUSED_NO_BUY_WINDOW} == HARVEST


def test_runner_blocked_tickers_do_not_count_toward_tier_selection():
    import unittest.mock as mock

    candidates = [dca_runner.Candidate("DFTX", 5.0), dca_runner.Candidate("AAPL", 5.0)]
    with mock.patch.object(dca_runner, "_no_buy_blocked", return_value=frozenset({"DFTX"})):
        valid, total, _count = dca_runner._tier_selection_inputs(candidates)
    assert [c.ticker for c in valid] == ["AAPL"]
    assert total == 5.0


def test_runner_helper_reads_the_block_at_call_time():
    assert dca_runner._no_buy_blocked(IN_WINDOW) == frozenset(HARVEST)
    assert dca_runner._no_buy_blocked(FIRST_FREE_DAY) == frozenset()


# --- the plan builders and validators refuse a blocked symbol like an excluded one -----------


def test_plan_validator_refuses_a_blocked_symbol():
    import dca_order_plan

    config = dataclasses.replace(StrategyConfig(tickers=("AAA", "BBB")), no_buy_tickers=frozenset({"AAA"}))
    excluded = set(config.excluded_tickers) | set(config.no_buy_tickers)
    errors = dca_order_plan._order_errors(
        {k: ("AAA" if k == "symbol" else 2.0) for k in sorted(dca_order_plan._ORDER_KEYS)},
        slot="0935",
        trading_day=date(2026, 11, 23),
        active=set(config.tickers),
        excluded=excluded,
        max_per_order_usd=10.0,
    )
    assert any(e.code == "excluded_symbol" for e in errors)
