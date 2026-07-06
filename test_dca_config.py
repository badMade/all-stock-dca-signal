"""Tests for the config loader (dca_config)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from dca_config import (
    load_approved_dates,
    load_budgets,
    load_sizing_tiers,
    load_strategy_config,
    load_tickers,
)
from near_close_dca import StrategyConfig


def _write(dirpath, name, payload):
    (dirpath / name).write_text(json.dumps(payload), encoding="utf-8")


def test_bundled_config_matches_hardcoded_defaults():
    """The committed config/*.json must reproduce the hardcoded StrategyConfig defaults
    (except the approved window, which is intentionally externalized)."""
    loaded = load_strategy_config()
    default = StrategyConfig()
    assert tuple(loaded.tickers) == tuple(default.tickers)
    assert loaded.excluded_tickers == default.excluded_tickers
    assert loaded.sizing_tiers == default.sizing_tiers
    assert loaded.base_buy_amount_usd == default.base_buy_amount_usd
    assert loaded.daily_base_cap_usd == default.daily_base_cap_usd
    assert loaded.reserve_cap_usd == default.reserve_cap_usd
    assert loaded.hard_daily_max_usd == default.hard_daily_max_usd
    assert loaded.weekly_notional_cap_usd == default.weekly_notional_cap_usd


def test_missing_dir_returns_base_defaults(tmp_path):
    loaded = load_strategy_config(tmp_path / "does-not-exist")
    assert tuple(loaded.tickers) == tuple(StrategyConfig().tickers)
    assert loaded.approved_test_dates == StrategyConfig().approved_test_dates


def test_loading_never_enables_live_trading():
    """dry_run/broker are never sourced from JSON."""
    loaded = load_strategy_config()
    assert loaded.dry_run is True
    assert loaded.broker == StrategyConfig().broker


def test_explicit_overrides_win_over_json():
    loaded = load_strategy_config(dry_run=False, base_buy_amount_usd=5.0)
    assert loaded.dry_run is False
    assert loaded.base_buy_amount_usd == 5.0


def test_unknown_field_rejected():
    with pytest.raises(ValueError, match="unknown StrategyConfig fields"):
        load_strategy_config(not_a_real_field=1)


def test_trading_window_expands_to_weekdays(tmp_path):
    _write(tmp_path, "trading_window.json", {"start_date": "2026-07-02", "end_date": "2026-07-08", "weekdays_only": True})
    approved = load_approved_dates(tmp_path)["approved_test_dates"]
    assert date(2026, 7, 6) in approved      # Monday
    assert date(2026, 7, 3) in approved      # Friday
    assert date(2026, 7, 4) not in approved  # Saturday
    assert date(2026, 7, 5) not in approved  # Sunday
    assert date(2026, 7, 8) in approved      # Wednesday (inclusive end)


def test_trading_window_all_days_when_not_weekdays_only(tmp_path):
    _write(tmp_path, "trading_window.json", {"start_date": "2026-07-03", "end_date": "2026-07-05", "weekdays_only": False})
    approved = load_approved_dates(tmp_path)["approved_test_dates"]
    assert {date(2026, 7, 3), date(2026, 7, 4), date(2026, 7, 5)} <= approved


def test_trading_window_bad_range_raises(tmp_path):
    _write(tmp_path, "trading_window.json", {"start_date": "2026-07-10", "end_date": "2026-07-01"})
    with pytest.raises(ValueError, match="precedes start_date"):
        load_approved_dates(tmp_path)


def test_sizing_tiers_converted_from_percent(tmp_path):
    _write(tmp_path, "sizing_ladder.json", {
        "base_amount_usd": 2.0,
        "tiers": [
            {"min_drop_pct": 1.0, "max_drop_pct": 2.0, "multiplier": 1.5},
            {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 1.0},
        ],
    })
    out = load_sizing_tiers(tmp_path)
    assert out["sizing_tiers"] == ((0.001, 1.0), (0.01, 1.5))  # sorted, percent->fraction
    assert out["minimum_drop_vs_previous_close"] == pytest.approx(0.001)
    assert out["base_buy_amount_usd"] == 2.0


def test_sizing_tiers_malformed_raises(tmp_path):
    _write(tmp_path, "sizing_ladder.json", {"tiers": [{"min_drop_pct": 1.0}]})
    with pytest.raises(ValueError, match="missing min_drop_pct/multiplier"):
        load_sizing_tiers(tmp_path)


def test_budgets_alias_kept_in_lockstep(tmp_path):
    _write(tmp_path, "budgets.json", {"daily_base_cap_usd": 75.0})
    out = load_budgets(tmp_path)
    assert out["daily_base_cap_usd"] == 75.0
    assert out["daily_notional_cap_usd"] == 75.0


def test_tickers_loaded(tmp_path):
    _write(tmp_path, "tickers.json", {"active": ["AAA", "BBB"], "excluded": ["CCC"]})
    out = load_tickers(tmp_path)
    assert out["tickers"] == ("AAA", "BBB")
    assert out["excluded_tickers"] == frozenset({"CCC"})


def test_malformed_json_raises(tmp_path):
    (tmp_path / "tickers.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not parse"):
        load_tickers(tmp_path)
