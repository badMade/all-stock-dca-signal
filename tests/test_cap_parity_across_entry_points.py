"""Cap parity across the three entry points.

``CLAUDE.md`` ("Three entry points coexist") and ``PRODUCTION_READINESS.md`` record that the entry points seed
their *spent-so-far* figures from three different sources (broker order history in ``dca_runner`` Gate 6, same-day
run artifacts in the executor classes, ``StrategyState`` plus artifact seeding in ``near_close_dca.within_caps``).
Unifying that seeding changes the live 3:30 PM path and needs its own review and acceptance run, so this file does
NOT touch it. It closes the cheaper half of the same hazard: the **limits** themselves must be one set of numbers.

Every cap an entry point compares spend against has to equal the single source of truth in
``config/budgets.json``. If a constant is edited in one module and not the others, one entry point would silently
enforce a different ceiling than its siblings (the "duplicated logic drifts silently" failure CLAUDE.md warns of).
``tests/test_config_invariants.py`` already pins ``near_close_dca.BUDGET_TIERS`` to the JSON; this file extends
that pin to the per-order cap and to each executor stream's own default daily/weekly ceilings.

Read-only: nothing here constructs a broker, touches the network, or places an order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dca_config
import dca_runner
import intraday_dca
import market_open_dca
import midday_dca
import morning_dca
import near_close_dca

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="module")
def budgets() -> dict:
    return json.loads((CONFIG_DIR / "budgets.json").read_text(encoding="utf-8"))


def _normal_tier(budgets: dict) -> dict:
    return next(t for t in budgets["budget_tiers"] if t["name"] == "normal")


def test_per_order_cap_is_one_number_across_every_entry_point(budgets):
    """The absolute per-order ceiling must match in all three entry points and the JSON they mirror."""
    json_cap = float(budgets["circuit_breakers"]["max_per_order_usd"])
    caps = {
        "budgets.json circuit_breakers.max_per_order_usd": json_cap,
        "near_close_dca.StrategyConfig (run_slot / 3:30 PM)": near_close_dca.StrategyConfig().max_per_order_usd,
        "morning_dca.MorningConfig (executor streams)": morning_dca.MorningConfig().max_per_order_usd,
        "dca_runner.MAX_PER_ORDER_USD (gate runner)": dca_runner.MAX_PER_ORDER_USD,
        "morning_dca.load_max_per_order_usd(config/)": morning_dca.load_max_per_order_usd(CONFIG_DIR),
    }
    assert {round(float(v) * 100) for v in caps.values()} == {round(json_cap * 100)}, caps


def test_normal_tier_caps_match_budgets_json(budgets):
    """The shared Normal-tier ceiling every earlier stream draws from is the JSON's normal tier."""
    tier = _normal_tier(budgets)
    first = near_close_dca.BUDGET_TIERS[0]
    assert (first.daily_cap_usd, first.weekly_cap_usd) == (tier["daily_cap_usd"], tier["weekly_cap_usd"])
    assert near_close_dca.NORMAL_WEEKLY_CAP_USD == tier["weekly_cap_usd"]


@pytest.mark.parametrize(
    ("stream", "daily", "weekly"),
    [
        ("morning", morning_dca.DEFAULT_MORNING_DAILY_CAP_USD, morning_dca.DEFAULT_MORNING_WEEKLY_CAP_USD),
        ("market_open", market_open_dca.DEFAULT_MARKET_OPEN_DAILY_CAP_USD, market_open_dca.DEFAULT_MARKET_OPEN_WEEKLY_CAP_USD),
        ("midday", midday_dca.DEFAULT_MIDDAY_DAILY_CAP_USD, midday_dca.DEFAULT_MIDDAY_WEEKLY_CAP_USD),
        ("intraday", intraday_dca.DEFAULT_INTRADAY_DAILY_CAP_USD, intraday_dca.DEFAULT_INTRADAY_WEEKLY_CAP_USD),
    ],
)
def test_each_executor_stream_default_caps_equal_the_shared_normal_tier(stream, daily, weekly, budgets):
    """Every executor stream's default ceilings must equal the one shared Normal tier (pooled-cap design)."""
    tier = _normal_tier(budgets)
    assert daily == tier["daily_cap_usd"], f"{stream} daily default drifted from budgets.json"
    assert weekly == tier["weekly_cap_usd"], f"{stream} weekly default drifted from budgets.json"


@pytest.mark.parametrize(
    "config_cls",
    [
        morning_dca.MorningConfig,
        market_open_dca.MarketOpenConfig,
        midday_dca.MiddayConfig,
        intraday_dca.IntradayConfig,
    ],
)
def test_each_executor_config_instance_carries_the_shared_caps(config_cls, budgets):
    tier = _normal_tier(budgets)
    cfg = config_cls()
    assert cfg.morning_daily_cap_usd == tier["daily_cap_usd"]
    assert cfg.morning_weekly_cap_usd == tier["weekly_cap_usd"]
    assert round(cfg.max_per_order_usd * 100) == round(float(budgets["circuit_breakers"]["max_per_order_usd"]) * 100)


def test_the_unseeded_order_count_breaker_is_uncapped_by_design_everywhere():
    """A directly constructed config (tests, bare StrategyConfig) has ``max_orders_per_day=None``; only the loaders
    overlay the JSON value. Pinned so changing that default is a deliberate, reviewed decision on every path."""
    assert near_close_dca.StrategyConfig().max_orders_per_day is None
    assert morning_dca.MorningConfig().max_orders_per_day is None


def test_loaders_overlay_the_same_order_count_breaker_from_budgets_json(budgets):
    """Both loaders must read ``circuit_breakers.max_orders_per_day`` from the one JSON value."""
    expected = int(budgets["circuit_breakers"]["max_orders_per_day"])
    morning = morning_dca.load_morning_config(
        config_dir=str(CONFIG_DIR), mode=morning_dca.MORNING_LIVE_MODE, allow_unpinned_account=True
    )
    afternoon = dca_config.load_strategy_config(CONFIG_DIR)
    assert morning.max_orders_per_day == expected
    assert afternoon.max_orders_per_day == expected
