"""Enforcement of the dated ``no_buy`` window across the entry points (slice 2).

Written test-first, from docs/adr-no-buy-window.md and the operator's 2026-11-20 request, not from the code:
the seven tax-loss tickers are not bought through 2026-12-21, stay in ``active`` (still tracked), and buying
resumes on its own on 2026-12-22. The pure rule is covered in ``test_no_buy_window.py``; this file covers where
the rule is applied: the shared loader (near_close_dca, morning_dca and its subclasses, and
generate_buy_candidates all read ``excluded_tickers`` from it) and ``dca_runner``, which reads tickers.json
itself.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

import dca_config
import dca_runner
from dca_config import get_config_dir, load_strategy_config, load_tickers
from dca_runner import BudgetTier

HARVEST = {"DFTX", "RGTI", "LITE", "CBRS", "VCX", "INTC", "SNDK"}
PERMANENT_EXCLUDED = {"QBUT", "KVUE"}
IN_WINDOW = date(2026, 11, 23)  # the first trading day after the sales
LAST_BLOCKED_DAY = date(2026, 12, 21)
FIRST_FREE_DAY = date(2026, 12, 22)


# --- the shared loader ---------------------------------------------------------------------


def test_loader_adds_blocked_tickers_to_the_excluded_set_inside_the_window():
    loaded = load_tickers(get_config_dir(), today=IN_WINDOW)
    assert loaded["excluded_tickers"] == frozenset(PERMANENT_EXCLUDED | HARVEST)


def test_loader_moves_blocked_tickers_out_of_the_runtime_tuple_but_not_out_of_the_file():
    # The resolved StrategyConfig requires active/excluded to be disjoint, so inside the window a blocked
    # ticker is in excluded_tickers and NOT in the runtime tickers tuple. The file's own "active" list still
    # carries it, which is what keeps it in the history fetch, sizing calibration and universe records.
    import json

    loaded = load_tickers(get_config_dir(), today=IN_WINDOW)
    assert not (HARVEST & set(loaded["tickers"]))
    assert HARVEST <= set(json.loads((get_config_dir() / "tickers.json").read_text())["active"])
    # Everything else keeps its place and order.
    assert len(loaded["tickers"]) == len(load_tickers(get_config_dir(), today=FIRST_FREE_DAY)["tickers"]) - len(HARVEST)


def test_loader_returns_blocked_tickers_to_the_runtime_tuple_on_the_first_free_day():
    assert HARVEST <= set(load_tickers(get_config_dir(), today=FIRST_FREE_DAY)["tickers"])


def test_loader_blocks_through_the_last_day_and_resumes_after_it():
    assert HARVEST <= load_tickers(get_config_dir(), today=LAST_BLOCKED_DAY)["excluded_tickers"]
    assert load_tickers(get_config_dir(), today=FIRST_FREE_DAY)["excluded_tickers"] == frozenset(PERMANENT_EXCLUDED)


def test_loader_blocks_nothing_before_the_sale_date():
    assert load_tickers(get_config_dir(), today=date(2026, 11, 19))["excluded_tickers"] == frozenset(
        PERMANENT_EXCLUDED
    )


def test_loader_default_clock_is_read_through_the_today_seam(monkeypatch):
    monkeypatch.setattr(dca_config, "_today_et", lambda: IN_WINDOW)
    assert HARVEST <= load_tickers(get_config_dir())["excluded_tickers"]
    monkeypatch.setattr(dca_config, "_today_et", lambda: FIRST_FREE_DAY)
    assert load_tickers(get_config_dir())["excluded_tickers"] == frozenset(PERMANENT_EXCLUDED)


def test_strategy_config_uses_the_runs_own_clock_not_the_hosts():
    inside = load_strategy_config(get_config_dir(), now=datetime(2026, 11, 23, 15, 30))
    assert HARVEST <= set(inside.excluded_tickers)
    assert not (HARVEST & set(inside.tickers))
    after = load_strategy_config(get_config_dir(), now=datetime(2026, 12, 22, 15, 30))
    assert not (HARVEST & set(after.excluded_tickers))
    assert HARVEST <= set(after.tickers)


def test_bad_no_buy_block_fails_the_whole_load(tmp_path):
    import json
    import shutil

    shutil.copytree(get_config_dir(), tmp_path / "config")
    path = tmp_path / "config" / "tickers.json"
    cfg = json.loads(path.read_text())
    cfg["no_buy"]["DFTX"]["until"] = "not-a-date"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="no_buy"):
        load_tickers(tmp_path / "config", today=IN_WINDOW)


def test_no_buy_without_an_excluded_list_fails_closed_instead_of_dropping_the_defaults(tmp_path):
    import json

    (tmp_path / "tickers.json").write_text(
        json.dumps(
            {
                "active": ["AAA"],
                "no_buy": {"AAA": {"blocked_on": "2026-11-20", "until": "2026-12-21", "reason": "synthetic"}},
            }
        )
    )
    with pytest.raises(ValueError, match="no_buy"):
        load_tickers(tmp_path, today=IN_WINDOW)


# --- every executor class gets it through the same loader ---------------------------------


@pytest.mark.parametrize(
    ("module_name", "loader_name"),
    [
        ("morning_dca", "load_morning_config"),
        ("market_open_dca", "load_market_open_config"),
        ("midday_dca", "load_midday_config"),
        ("intraday_dca", "load_intraday_config"),
    ],
)
def test_stream_configs_exclude_blocked_tickers_inside_the_window(monkeypatch, module_name, loader_name):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setattr(dca_config, "_today_et", lambda: IN_WINDOW)
    loader = getattr(module, loader_name)
    kwargs = {"mode": "live"} if module_name == "morning_dca" else {}
    config = loader(get_config_dir(), **kwargs)
    assert HARVEST <= set(config.excluded_tickers)
    assert not (HARVEST & set(config.tickers))


# --- dca_runner reads tickers.json itself -----------------------------------------------------


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
    plan = _plan(FIRST_FREE_DAY, "DFTX")
    assert [c.ticker for c in plan.to_place] == ["DFTX"]


def test_runner_refuses_every_harvest_ticker_inside_the_window():
    plan = _plan(IN_WINDOW, *sorted(HARVEST))
    assert plan.to_place == []
    assert {t for t, reason in plan.refused if reason == dca_runner.REFUSED_NO_BUY_WINDOW} == HARVEST


def test_runner_blocked_tickers_do_not_count_toward_tier_selection():
    candidates = [dca_runner.Candidate("DFTX", 5.0), dca_runner.Candidate("AAPL", 5.0)]
    import unittest.mock as mock

    with mock.patch.object(dca_runner, "_no_buy_blocked", return_value=frozenset({"DFTX"})):
        valid, total, _count = dca_runner._tier_selection_inputs(candidates)
    assert [c.ticker for c in valid] == ["AAPL"]
    assert total == 5.0


def test_runner_execute_plan_helper_reads_the_block_at_call_time(monkeypatch):
    # The helper must not be frozen at import (unlike ACTIVE_TICKERS): an early release or a window
    # expiring must take effect on the next call without a restart.
    assert dca_runner._no_buy_blocked(IN_WINDOW) == frozenset(HARVEST)
    assert dca_runner._no_buy_blocked(FIRST_FREE_DAY) == frozenset()
