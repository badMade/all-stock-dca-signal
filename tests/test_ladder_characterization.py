"""Characterization (golden-master) tests for the live sizing functions in ``near_close_dca``.

The plan-first backup executor needs the ladder and base-selection arithmetic in a small,
dependency-free module (``dca_ladder_core``) so it can run in the Perplexity sandbox. Moving that
arithmetic out of the live 3:30 PM path is a refactor, and a refactor must not change a single
result. ``tests/fixtures/ladder_golden.json`` freezes what ``raw_ladder_multiplier``,
``graded_buy_multiplier`` and ``select_first_allocation_base_usd`` returned BEFORE the extraction,
across every active ticker's per-ticker calibration, the global ladder, an ML nudge, an engaged
kill switch, and an inverse-Martingale-suppressed ticker.

These tests pass today and must keep passing unchanged afterwards. They do not import the new
module, so they stay green before and after. Never regenerate the fixture to make a failure go away.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import dca_config
import near_close_dca

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "ladder_golden.json").read_bytes())


@pytest.fixture(scope="module")
def configs():
    live = dca_config.load_strategy_config(dca_config.get_config_dir())
    return {
        "live": live,
        "global": dataclasses.replace(live, per_ticker_sizing={}),
        "ml": dataclasses.replace(live, ml_sizing_adjustment={"VT": 1.2, "MSFT": 0.8, "AAPL": 1.0}),
        "kill_switch": dataclasses.replace(live, deployed_last_7d_usd=1e9),
        "inverse_martingale": dataclasses.replace(live, inverse_martingale={"VT": {"enabled": True}}),
    }


def test_the_fixture_covers_every_active_ticker_and_every_scenario(configs):
    assert set(GOLDEN["multipliers"]) == set(configs)
    assert set(GOLDEN["multipliers"]["live"]) == set(configs["live"].tickers)
    assert len(GOLDEN["drops"]) >= 25 and len(GOLDEN["bases"]) >= 40


@pytest.mark.parametrize("scenario", sorted(GOLDEN["multipliers"]))
def test_raw_and_graded_multipliers_match_the_frozen_outputs(configs, scenario):
    config = configs[scenario]
    for ticker, expected in GOLDEN["multipliers"][scenario].items():
        raw = [near_close_dca.raw_ladder_multiplier(d, config, ticker) for d in GOLDEN["drops"]]
        graded = [near_close_dca.graded_buy_multiplier(d, config, ticker) for d in GOLDEN["drops"]]
        assert raw == expected["raw"], (scenario, ticker, "raw")
        assert graded == expected["graded"], (scenario, ticker, "graded")


def test_base_selection_matches_the_frozen_outputs(configs):
    for case in GOLDEN["bases"]:
        base, reason = near_close_dca.select_first_allocation_base_usd(
            case["decision"], case["close"], case["low"], configs[case["config"]], case["ticker"]
        )
        assert [base, reason] == [case["base"], case["reason"]], case


def test_the_scenarios_really_exercise_each_branch(configs):
    """Guards the fixture itself: if a scenario stopped differing from the live one it would prove nothing."""
    live = GOLDEN["multipliers"]["live"]["VT"]["graded"]
    assert GOLDEN["multipliers"]["kill_switch"]["VT"]["graded"] != live
    assert max(GOLDEN["multipliers"]["kill_switch"]["VT"]["graded"]) == 1.0
    assert set(GOLDEN["multipliers"]["inverse_martingale"]["VT"]["graded"]) == {0.0}
    assert GOLDEN["multipliers"]["ml"]["VT"]["graded"] != live
    assert max(live) > 1.0 and 0.0 in live
