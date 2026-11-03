"""Tests for the config loader (dca_config)."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from dca_config import (
    DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES,
    MissingConfigEnvError,
    _read_json,
    expand_config_env,
    get_config_dir,
    load_approved_dates,
    load_backtest_sizing_tiers,
    load_budgets,
    load_inverse_martingale_config,
    load_ml_sizing_adjustment,
    load_near_close_overrides,
    load_per_ticker_sizing,
    load_sizing_tiers,
    load_strategy_config,
    load_tickers,
)
from near_close_dca import (
    EASTERN,
    WHOLE_SHARE_TICKERS,
    Action,
    Bar,
    SourceName,
    StrategyConfig,
    evaluate_source_signal,
    graded_buy_multiplier,
    select_first_allocation_base_usd,
)


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
    assert loaded.weekly_rollover_reserve_cap_usd == default.weekly_rollover_reserve_cap_usd


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


def test_trading_window_optional_decision_time(tmp_path):
    from datetime import time

    _write(tmp_path, "trading_window.json", {"start_date": "2026-07-02", "end_date": "2026-07-08", "decision_time_et": "15:30"})
    assert load_approved_dates(tmp_path)["decision_time_et"] == time(15, 30)
    # Absent -> not set, so the StrategyConfig default (15:45) is kept.
    _write(tmp_path, "trading_window.json", {"start_date": "2026-07-02", "end_date": "2026-07-08"})
    assert "decision_time_et" not in load_approved_dates(tmp_path)


def test_trading_window_bad_decision_time_raises(tmp_path):
    _write(tmp_path, "trading_window.json", {"start_date": "2026-07-02", "end_date": "2026-07-08", "decision_time_et": "3:30pm"})
    with pytest.raises(ValueError, match="decision_time_et must be"):
        load_approved_dates(tmp_path)


def test_sizing_tiers_converted_from_percent(tmp_path):
    _write(tmp_path, "sizing_ladder.json", {
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "tiers": [
            {"min_drop_pct": 1.0, "max_drop_pct": 2.0, "multiplier": 1.5},
            {"min_drop_pct": 0.1, "max_drop_pct": 1.0, "multiplier": 1.0},
        ],
    })
    out = load_sizing_tiers(tmp_path)
    assert out["sizing_tiers"] == ((0.001, 1.0), (0.01, 1.5))  # sorted, percent->fraction
    assert out["minimum_drop_vs_previous_close"] == pytest.approx(0.001)
    assert out["base_buy_amount_usd"] == 2.0
    assert out["base_buy_amount_below_close_usd"] == 1.0


def test_sizing_tiers_malformed_raises(tmp_path):
    _write(tmp_path, "sizing_ladder.json", {"tiers": [{"min_drop_pct": 1.0}]})
    with pytest.raises(ValueError, match="missing min_drop_pct/multiplier"):
        load_sizing_tiers(tmp_path)


def test_per_ticker_sizing_has_precise_ranges_and_one_dollar_floor(tmp_path):
    _write(tmp_path, "per_ticker_sizing.json", {
        "status": "active",
        "tickers": {
            "LOW": {
                "base_amount_usd": 0.50,
                "base_amount_below_close_usd": 0.25,
                "tiers": [
                    {"min_drop_pct": 0.1234, "multiplier": 1.0},
                    {"min_drop_pct": 1.9876, "multiplier": 5.0},
                ],
            }
        },
    })
    model = load_per_ticker_sizing(tmp_path)["per_ticker_sizing"]["LOW"]
    config = StrategyConfig(per_ticker_sizing={"LOW": model})
    assert model["tiers"][0][0] == pytest.approx(0.001234)
    assert graded_buy_multiplier(0.001234, config, "LOW") == 1.0
    assert select_first_allocation_base_usd(99.0, 100.0, None, config, "LOW")[0] == 1.0


def test_load_per_ticker_sizing_loads_minimum_order_usd(tmp_path):
    _write(tmp_path, "per_ticker_sizing.json", {
        "status": "active",
        "minimum_order_usd": 2.50,
        "tickers": {
            "LOW": {
                "base_amount_usd": 1.0,
                "tiers": [{"min_drop_pct": 0.1, "multiplier": 1.0}],
            }
        },
    })
    res = load_per_ticker_sizing(tmp_path)
    assert res["minimum_order_usd"] == 2.50
    config = load_strategy_config(tmp_path)
    assert config.minimum_order_usd == 2.50


def test_load_per_ticker_sizing_rejects_invalid_minimum_order_usd(tmp_path):
    for invalid in [0, -1.0, True, False]:
        _write(tmp_path, "per_ticker_sizing.json", {
            "status": "active",
            "minimum_order_usd": invalid,
            "tickers": {},
        })
        with pytest.raises(ValueError):
            load_per_ticker_sizing(tmp_path)


def test_load_sizing_tiers_rejects_invalid_minimum_order_usd(tmp_path):
    for invalid in [0, -1.0, True, False]:
        _write(tmp_path, "sizing_ladder.json", {
            "status": "active",
            "base_amount_usd": 2.0,
            "minimum_order_usd": invalid,
            "tiers": [{"min_drop_pct": 0.1, "multiplier": 1.0}],
        })
        with pytest.raises(ValueError):
            load_sizing_tiers(tmp_path)


def test_inverse_martingale_missing_file_is_a_no_op(tmp_path):
    assert load_inverse_martingale_config(tmp_path) == {}


def test_inverse_martingale_inactive_status_is_a_no_op(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {"status": "inactive", "tickers": {}})
    assert load_inverse_martingale_config(tmp_path) == {}


def test_inverse_martingale_disabled_ticker_entry_is_dropped(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "enabled_default": False,
        "tickers": {
            "AAPL": {
                "enabled": False,
                "basis": "pct",
                "tiers": [{"drop_pct": 1.0, "units": 1.0}],
                "recovery_offset_pct": 1.0,
            }
        },
    })
    out = load_inverse_martingale_config(tmp_path)
    assert out["inverse_martingale"] == {}


def test_inverse_martingale_case_folded_duplicate_symbol_raises_even_when_the_duplicate_is_disabled(tmp_path):
    """Regression test for "Reject case-folded duplicate enrollment
    symbols". An enabled "AAPL" and a disabled "aapl" are both individually
    schema-valid, but normalized[] is keyed by the UPPERCASED symbol -- the
    disabled entry, meant to suppress the ticker, would otherwise be
    silently dropped by the enabled-filter BEFORE ever colliding with the
    enabled entry, leaving the ticker enrolled against one configuration
    entry's explicit intent. This must raise, not silently prefer whichever
    entry the enabled-filter didn't skip.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "enabled_default": False,
        "tickers": {
            "AAPL": {
                "enabled": True,
                "basis": "pct",
                "tiers": [{"drop_pct": 1.0, "units": 1.0}],
                "recovery_offset_pct": 1.0,
            },
            "aapl": {
                "enabled": False,
                "basis": "pct",
                "tiers": [{"drop_pct": 1.0, "units": 1.0}],
                "recovery_offset_pct": 1.0,
            },
        },
    })
    with pytest.raises(ValueError, match="duplicate"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_case_folded_duplicate_symbol_raises_when_both_are_enabled(tmp_path):
    """Two ENABLED case-variant entries would otherwise silently overwrite
    one another in `normalized` with no error and no way to know which
    basis/tier/recovery configuration actually won."""
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "enabled_default": False,
        "tickers": {
            "AAPL": {
                "enabled": True,
                "basis": "pct",
                "tiers": [{"drop_pct": 1.0, "units": 1.0}],
                "recovery_offset_pct": 1.0,
            },
            "Aapl": {
                "enabled": True,
                "basis": "stddev",
                "tiers": [{"sigma_multiple": 1.0, "units": 1.0}],
                "recovery_offset_sigma": 0.5,
                "stddev_window_days": 20,
            },
        },
    })
    with pytest.raises(ValueError, match="duplicate"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_enabled_default_true_enrolls_a_ticker_omitting_enabled(tmp_path):
    """Regression test for "Honor the configured default enrollment switch".

    A ticker entry that omits its own `enabled` field must fall back to the
    top-level `enabled_default`, not an unconditional False -- previously
    `enabled_default: true` had NO effect anywhere at runtime.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "enabled_default": True,
        "tickers": {
            "AAPL": {
                # deliberately omits "enabled" -- must fall back to enabled_default
                "basis": "pct",
                "tiers": [{"drop_pct": 1.0, "units": 1.0}],
                "recovery_offset_pct": 1.0,
            }
        },
    })
    out = load_inverse_martingale_config(tmp_path)
    assert "AAPL" in out["inverse_martingale"]
    assert out["inverse_martingale_enabled_default"] is True


def test_inverse_martingale_explicit_enabled_false_overrides_a_true_default(tmp_path):
    """A per-ticker `enabled: false` must still opt that ticker out even when
    enabled_default is true -- the default is only a FALLBACK for an omitted
    field, not a floor."""
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "enabled_default": True,
        "tickers": {
            "AAPL": {
                "enabled": False,
                "basis": "pct",
                "tiers": [{"drop_pct": 1.0, "units": 1.0}],
                "recovery_offset_pct": 1.0,
            }
        },
    })
    out = load_inverse_martingale_config(tmp_path)
    assert out["inverse_martingale"] == {}


def test_inverse_martingale_enabled_default_false_still_requires_explicit_opt_in(tmp_path):
    """The pre-existing default behavior: with enabled_default left at its
    own False default, a ticker omitting `enabled` stays un-enrolled."""
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {
            "AAPL": {
                "basis": "pct",
                "tiers": [{"drop_pct": 1.0, "units": 1.0}],
                "recovery_offset_pct": 1.0,
            }
        },
    })
    out = load_inverse_martingale_config(tmp_path)
    assert out["inverse_martingale"] == {}


def test_inverse_martingale_non_bool_enabled_default_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "enabled_default": "true", "tickers": {},
    })
    with pytest.raises(ValueError, match="enabled_default must be a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_pct_basis_round_trips(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "enabled_default": False,
        "unit_usd": 2.0,
        "max_cycle_days": 10,
        "max_fire_attempts": 3,
        "tickers": {
            "aapl": {
                "enabled": True,
                "basis": "pct",
                "tiers": [
                    {"drop_pct": 2.0, "units": 2.0},
                    {"drop_pct": 1.0, "units": 1.0},
                ],
                "recovery_offset_pct": 1.0,
            }
        },
    })
    out = load_inverse_martingale_config(tmp_path)
    assert out["inverse_martingale_unit_usd"] == 2.0
    assert out["inverse_martingale_max_cycle_days"] == 10
    assert out["inverse_martingale_max_fire_attempts"] == 3
    model = out["inverse_martingale"]["AAPL"]
    assert model["basis"] == "pct"
    assert model["tiers"] == ((1.0, 1.0), (2.0, 2.0))
    assert model["recovery_offset"] == 1.0


def test_inverse_martingale_duplicate_tier_threshold_raises(tmp_path):
    """Regression test for "Reject duplicate tier thresholds at load time"
    (Copilot review). evaluate_inverse_martingale_step's tier-crossing loop
    credits a tier when `threshold not in crossed_before`, and
    crossed_before is only updated after the WHOLE loop finishes evaluating
    this run's tiers -- so two configured tiers sharing the identical
    threshold both read as "not yet crossed" in the same evaluation and
    both get credited, double-accumulating for what is really one dip
    level.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {
            "AAPL": {
                "enabled": True,
                "basis": "pct",
                "tiers": [
                    {"drop_pct": 1.0, "units": 1.0},
                    {"drop_pct": 1.0, "units": 2.0},  # duplicate threshold, different units
                ],
                "recovery_offset_pct": 1.0,
            }
        },
    })
    with pytest.raises(ValueError, match="duplicate"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_stddev_basis_requires_window(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {
            "MSFT": {
                "enabled": True,
                "basis": "stddev",
                "tiers": [{"sigma_multiple": 1.0, "units": 1.0}],
                "recovery_offset_sigma": 0.5,
                # deliberately omits stddev_window_days
            }
        },
    })
    with pytest.raises(ValueError, match="stddev fields missing"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_missing_basis_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {"enabled": True, "tiers": [{"drop_pct": 1.0, "units": 1.0}]}},
    })
    with pytest.raises(ValueError, match="basis missing"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_nonpositive_tier_threshold_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 0.0, "units": 1.0}],
            "recovery_offset_pct": 1.0,
        }},
    })
    with pytest.raises(ValueError, match="drop_pct must be positive"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_negative_units_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": -1.0}],
            "recovery_offset_pct": 1.0,
        }},
    })
    with pytest.raises(ValueError, match="units must be positive"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_nonpositive_recovery_offset_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": 1.0}],
            "recovery_offset_pct": 0.0,
        }},
    })
    with pytest.raises(ValueError, match="recovery_offset_pct must be positive"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_stddev_window_must_exceed_one(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "stddev",
            "tiers": [{"sigma_multiple": 1.0, "units": 1.0}],
            "recovery_offset_sigma": 0.5, "stddev_window_days": 1,
        }},
    })
    with pytest.raises(ValueError, match="stddev_window_days must be > 1"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_nonpositive_global_unit_usd_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "unit_usd": -1.0, "tickers": {},
    })
    with pytest.raises(ValueError, match="unit_usd must be positive"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_nonpositive_max_cycle_days_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_cycle_days": 0, "tickers": {},
    })
    with pytest.raises(ValueError, match="max_cycle_days must be positive"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_nonpositive_max_fire_attempts_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_fire_attempts": 0, "tickers": {},
    })
    with pytest.raises(ValueError, match="max_fire_attempts must be positive"):
        load_inverse_martingale_config(tmp_path)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_inverse_martingale_non_finite_tier_threshold_raises(tmp_path, bad_value):
    """Regression test for "Reject non-finite inverse-Martingale values".

    json.dumps/json.loads round-trip NaN/Infinity/-Infinity by default (a
    non-standard but Python-json-accepted extension), and a NaN or infinite
    threshold compares false against "<= 0" -- the positivity check alone
    would silently accept it.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": bad_value, "units": 1.0}],
            "recovery_offset_pct": 1.0,
        }},
    })
    with pytest.raises(ValueError, match="drop_pct must be positive and finite"):
        load_inverse_martingale_config(tmp_path)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_inverse_martingale_non_finite_tier_units_raises(tmp_path, bad_value):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": bad_value}],
            "recovery_offset_pct": 1.0,
        }},
    })
    with pytest.raises(ValueError, match="units must be positive and finite"):
        load_inverse_martingale_config(tmp_path)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_inverse_martingale_non_finite_recovery_offset_raises(tmp_path, bad_value):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": 1.0}],
            "recovery_offset_pct": bad_value,
        }},
    })
    with pytest.raises(ValueError, match="recovery_offset_pct must be positive and finite"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_non_finite_stddev_window_days_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "stddev",
            "tiers": [{"sigma_multiple": 1.0, "units": 1.0}],
            "recovery_offset_sigma": 0.5, "stddev_window_days": float("nan"),
        }},
    })
    with pytest.raises(ValueError, match="stddev_window_days must be a finite whole number"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_fractional_stddev_window_days_raises(tmp_path):
    """Regression test for "Reject fractional integer limits before truncating".

    2.9 would silently truncate to 2 via int() -- a smaller, unintended
    window -- rather than being refused as schema-invalid.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "stddev",
            "tiers": [{"sigma_multiple": 1.0, "units": 1.0}],
            "recovery_offset_sigma": 0.5, "stddev_window_days": 2.9,
        }},
    })
    with pytest.raises(ValueError, match="stddev_window_days must be a finite whole number"):
        load_inverse_martingale_config(tmp_path)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_inverse_martingale_non_finite_global_unit_usd_raises(tmp_path, bad_value):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "unit_usd": bad_value, "tickers": {},
    })
    with pytest.raises(ValueError, match="unit_usd must be positive and finite"):
        load_inverse_martingale_config(tmp_path)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_inverse_martingale_non_finite_max_cycle_days_raises(tmp_path, bad_value):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_cycle_days": bad_value, "tickers": {},
    })
    with pytest.raises(ValueError, match="max_cycle_days must be positive and finite"):
        load_inverse_martingale_config(tmp_path)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_inverse_martingale_non_finite_max_fire_attempts_raises(tmp_path, bad_value):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_fire_attempts": bad_value, "tickers": {},
    })
    with pytest.raises(ValueError, match="max_fire_attempts must be positive and finite"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_tier_threshold_raises(tmp_path):
    """Regression test for "Reject booleans in inverse-Martingale numeric
    fields". bool is a subclass of int in Python -- float(True) == 1.0 --
    so without an explicit bool exclusion, "drop_pct": true would silently
    become a real 1.0 (100%) threshold instead of being rejected as the
    schema-invalid input it is.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": True, "units": 1.0}],
            "recovery_offset_pct": 1.0,
        }},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_tier_units_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": True}],
            "recovery_offset_pct": 1.0,
        }},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_recovery_offset_pct_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": 1.0}],
            "recovery_offset_pct": True,
        }},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_recovery_offset_sigma_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "stddev",
            "tiers": [{"sigma_multiple": 1.0, "units": 1.0}],
            "recovery_offset_sigma": True, "stddev_window_days": 20,
        }},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_stddev_window_days_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "stddev",
            "tiers": [{"sigma_multiple": 1.0, "units": 1.0}],
            "recovery_offset_sigma": 0.5, "stddev_window_days": True,
        }},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_global_unit_usd_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "unit_usd": True, "tickers": {},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_max_cycle_days_raises(tmp_path):
    """A boolean is especially dangerous here: True coerces to 1.0, a real
    (if extreme) 1-day abandonment cutoff that would otherwise load
    successfully instead of being rejected as schema-invalid."""
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_cycle_days": True, "tickers": {},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_boolean_max_fire_attempts_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_fire_attempts": True, "tickers": {},
    })
    with pytest.raises(ValueError, match="must be a number, not a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_fractional_max_cycle_days_raises(tmp_path):
    """Regression test for "Reject fractional integer limits before truncating".

    0.5 passes the ">0" positivity check but truncates to 0 via int() --
    silently defeating the positivity check it just passed and letting an
    enrolled ticker's cycle abandon itself after effectively zero days.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_cycle_days": 0.5, "tickers": {},
    })
    with pytest.raises(ValueError, match="max_cycle_days must be positive and finite, and a whole number"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_fractional_max_fire_attempts_raises(tmp_path):
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active", "max_fire_attempts": 0.5, "tickers": {},
    })
    with pytest.raises(ValueError, match="max_fire_attempts must be positive and finite, and a whole number"):
        load_inverse_martingale_config(tmp_path)


def test_inverse_martingale_non_bool_enabled_raises(tmp_path):
    """Regression test for "Require a boolean before enabling the accumulator".

    The schema-invalid string "false" is truthy in Python -- without an
    explicit isinstance(..., bool) check this would silently ENABLE the
    accumulator against the operator's obvious intent.
    """
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": "false", "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": 1.0}],
            "recovery_offset_pct": 1.0,
        }},
    })
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        load_inverse_martingale_config(tmp_path)


def test_load_strategy_config_preserves_whole_share_defaults_when_config_dir_is_absent(tmp_path):
    """Codex review ("Preserve whole-share defaults when config is absent"):
    the documented installed-wheel/missing-config-dir fallback (see the
    "config dir not found" warning) constructs load_strategy_config's own
    default `base` when the caller passes none. tickers/excluded_tickers
    survive that fallback via StrategyConfig()'s own real ACTIVE_TICKERS/
    EXCLUDED_TICKERS class defaults, but whole_share_tickers deliberately
    defaults to an EMPTY frozenset on the class itself (to avoid silently
    evaluating BYDDY against every bare ad hoc StrategyConfig() test
    construction) -- so without this fix, BYDDY silently stopped being
    evaluated/accumulated the moment config/tickers.json was unavailable,
    unlike the active/excluded universes.
    """
    missing_dir = tmp_path / "does-not-exist"
    assert not missing_dir.exists()

    config = load_strategy_config(missing_dir)

    assert config.whole_share_tickers == frozenset(WHOLE_SHARE_TICKERS)
    assert tuple(config.tickers) == tuple(StrategyConfig().tickers)
    assert config.excluded_tickers == StrategyConfig().excluded_tickers


def test_load_strategy_config_omitted_whole_share_key_falls_back_to_the_default(tmp_path):
    """Companion to the missing-directory case above: an EXISTING config
    directory whose tickers.json simply omits the optional "whole_share"
    key (valid per schema) must ALSO fall back to the real
    WHOLE_SHARE_TICKERS default, exactly like an absent "excluded" key
    already falls back to the real EXCLUDED_TICKERS default -- not silently
    evaluating an empty whole-share universe just because this one
    optional key wasn't declared.
    """
    _write(tmp_path, "tickers.json", {"active": ["AAPL"]})

    config = load_strategy_config(tmp_path)

    assert config.whole_share_tickers == frozenset(WHOLE_SHARE_TICKERS)


def test_load_strategy_config_whole_share_default_honors_an_explicit_exclusion(tmp_path):
    """Codex review ("Honor explicit exclusions when applying whole-share
    defaults"): a supported legacy/alternate config that explicitly
    excludes BYDDY -- a safer do-not-buy state -- without ever mentioning
    "whole_share" at all must NOT have the implicit WHOLE_SHARE_TICKERS
    fallback inject BYDDY into whole_share_tickers anyway: that would
    overlap the explicit excluded_tickers and fail the disjointness
    validation at startup for a config that never asked for BYDDY to be
    evaluated in ANY category.
    """
    _write(tmp_path, "tickers.json", {"active": ["AAPL"], "excluded": ["BYDDY"]})

    config = load_strategy_config(tmp_path)

    assert "BYDDY" not in config.whole_share_tickers
    assert "BYDDY" in config.excluded_tickers
    # Any other canonical whole-share ticker (none exist today besides
    # BYDDY, but the fallback logic itself must not blanket-empty the set)
    # is still exactly WHOLE_SHARE_TICKERS minus the excluded symbols.
    assert config.whole_share_tickers == frozenset(WHOLE_SHARE_TICKERS) - {"BYDDY"}


def test_load_strategy_config_whole_share_default_honors_an_explicit_override_exclusion(tmp_path):
    """Codex review ("Apply exclusion overrides before deriving whole-share
    defaults"): a caller-supplied `excluded_tickers=` OVERRIDE -- the
    highest-precedence layer, applied via `**overrides` -- must also be
    honored by the implicit whole-share default, not just a JSON-file-level
    "excluded" entry. Deriving the default from the lower-precedence
    merged/base layers alone injected BYDDY into whole_share_tickers
    anyway, which the later `merged.update(overrides)` call cannot undo --
    tripping the disjointness validation for a caller explicitly trying to
    exclude BYDDY via the documented override.
    """
    _write(tmp_path, "tickers.json", {"active": ["AAPL"]})

    config = load_strategy_config(tmp_path, excluded_tickers=frozenset({"BYDDY"}))

    assert "BYDDY" not in config.whole_share_tickers
    assert "BYDDY" in config.excluded_tickers
    assert config.whole_share_tickers == frozenset(WHOLE_SHARE_TICKERS) - {"BYDDY"}


def test_load_strategy_config_explicit_empty_override_clears_a_config_file_exclusion(tmp_path):
    """Codex review, PR #631 discussion_r3914702406 ("Honor an explicit
    empty exclusion override"): the whole-share default's precedence chain
    used to pick a layer by TRUTHINESS (`overrides.get(...) or merged.get(...)
    or base.excluded_tickers or frozenset()`), so a caller explicitly passing
    `excluded_tickers=frozenset()` -- "no exclusions, clear whatever the
    config file said" -- was indistinguishable from not passing the override
    at all: an empty frozenset is falsy, so the chain fell through to the
    tickers.json-level "excluded": ["BYDDY"] underneath it, and BYDDY stayed
    missing from the resolved whole_share_tickers/buying universe despite the
    caller's explicit instruction to clear it.
    """
    _write(tmp_path, "tickers.json", {"active": ["AAPL"], "excluded": ["BYDDY"]})

    config = load_strategy_config(tmp_path, excluded_tickers=frozenset())

    assert "BYDDY" not in config.excluded_tickers
    assert "BYDDY" in config.whole_share_tickers
    assert config.whole_share_tickers == frozenset(WHOLE_SHARE_TICKERS)


def test_load_strategy_config_an_explicit_base_is_not_overridden(tmp_path):
    """A caller passing an explicit `base` (e.g. a test isolating a small ad
    hoc universe) must be unaffected by the missing-config-dir fallback
    default -- only the IMPLICIT default (no `base` passed at all) should
    fill in WHOLE_SHARE_TICKERS.
    """
    missing_dir = tmp_path / "does-not-exist"
    explicit_base = StrategyConfig(whole_share_tickers=frozenset())

    config = load_strategy_config(missing_dir, base=explicit_base)

    assert config.whole_share_tickers == frozenset()


def test_load_strategy_config_leaves_inverse_martingale_off_by_default(tmp_path):
    config = load_strategy_config(tmp_path)
    assert config.inverse_martingale_enabled_default is False
    assert config.inverse_martingale == {}


def test_load_strategy_config_rejects_an_overlap_hidden_by_a_partial_tickers_json(tmp_path):
    """Codex review ("Validate the final overlaid ticker partition"):
    load_tickers's own pairwise-disjointness check only sees the categories a
    PARTIAL tickers.json explicitly provides -- a config defining only
    "whole_share" has an empty "tickers"/"excluded" set of its own, so
    load_tickers sees no overlap. But load_strategy_config merges that
    partial result onto `base` (default StrategyConfig()), whose default
    `tickers` field (ACTIVE_TICKERS) already contains "AAPL" -- the merge
    never touches `tickers` since load_tickers didn't return that key at
    all. The FINAL resolved StrategyConfig then has "AAPL" in both
    `tickers` and `whole_share_tickers`, which load_tickers's own check,
    scoped to one file's declared categories, never catches.
    """
    _write(tmp_path, "tickers.json", {"whole_share": ["AAPL"]})

    with pytest.raises(ValueError, match="pairwise disjoint"):
        load_strategy_config(tmp_path)


def test_load_strategy_config_rejects_a_case_folded_duplicate_supplied_via_override(tmp_path):
    """Codex review ("Reject case-folded duplicates after applying
    overlays"): a case-folded duplicate supplied directly through an
    explicit override bypasses load_tickers/tickers.json entirely, so its
    own within-category duplicate check never runs against it. Both
    spellings survive as distinct frozenset entries; normalizing to check
    overlaps must not silently collapse them without detecting the
    duplicate first.
    """
    with pytest.raises(ValueError, match="case-folded duplicate"):
        load_strategy_config(tmp_path, whole_share_tickers=frozenset({"BYDDY", "byddy"}))


def test_load_strategy_config_rejects_a_lowercase_whole_share_symbol_supplied_via_override(tmp_path):
    """Codex review ("Validate final whole-share symbols after overrides"):
    load_tickers's _WHOLE_SHARE_SYMBOL_PATTERN check only runs against
    tickers.json's own "whole_share" list -- a symbol supplied directly
    through the documented `whole_share_tickers=` override bypasses that
    loader entirely. A lowercase symbol collides with nothing and survives
    every disjointness/duplicate check, but near_close_dca's signal lookup
    and RobinhoodDataProvider compare the broker's canonical UPPERCASE
    response verbatim, so it would silently never match and never
    accumulate. The final overlaid collection must be validated too.
    """
    with pytest.raises(ValueError, match="invalid ticker symbol"):
        load_strategy_config(tmp_path, whole_share_tickers=frozenset({"byddy"}))


def test_ml_sizing_adjustment_missing_file_is_a_no_op(tmp_path):
    assert load_ml_sizing_adjustment(tmp_path) == {}


def test_ml_sizing_adjustment_inactive_status_is_a_no_op(tmp_path):
    _write(tmp_path, "ml_sizing_adjustment.json", {"status": "inactive"})
    assert load_ml_sizing_adjustment(tmp_path) == {}


def _fresh_ml_payload(**overrides):
    payload = {
        "status": "active",
        "generated_at": "2026-08-10T00:00:00+00:00",
        "max_staleness_hours": 240,
        "bounds": {"min_multiplier": 0.8, "max_multiplier": 1.2},
        "adjustments": {"AAPL": 1.05},
    }
    payload.update(overrides)
    return payload


def test_ml_sizing_adjustment_valid_fresh_file_applies(tmp_path):
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload())
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    out = load_ml_sizing_adjustment(tmp_path, now=now)
    assert out == {"ml_sizing_adjustment": {"AAPL": 1.05}}


def test_ml_sizing_adjustment_naive_injected_now_still_applies_fresh_file(tmp_path):
    """Regression test for "Normalize the injected staleness clock".

    A naive `now` (no tzinfo) -- e.g. from a replay/test caller using a
    plain datetime(...) -- must be normalized the same way generated_at
    already is, not raise TypeError subtracting an aware datetime from a
    naive one.
    """
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload())
    naive_now = datetime(2026, 8, 10, 12)  # no tzinfo
    out = load_ml_sizing_adjustment(tmp_path, now=naive_now)
    assert out == {"ml_sizing_adjustment": {"AAPL": 1.05}}


def test_ml_sizing_adjustment_naive_injected_now_still_detects_staleness(tmp_path):
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload())
    naive_now = datetime(2026, 8, 10, 0) + timedelta(hours=241)  # no tzinfo, past the 240h budget
    assert load_ml_sizing_adjustment(tmp_path, now=naive_now) == {}


def test_load_strategy_config_threads_now_into_ml_sizing_staleness(tmp_path):
    """Regression test for "Validate ML staleness against the run timestamp".

    load_strategy_config()'s own `now=` parameter must reach
    load_ml_sizing_adjustment(), not just that function called directly --
    otherwise a replay caller has no way to make load_strategy_config()
    itself judge the published file's freshness against anything but the
    real wall clock.
    """
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload())

    fresh_now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)  # within the 240h budget
    config = load_strategy_config(tmp_path, now=fresh_now)
    assert config.ml_sizing_adjustment == {"AAPL": 1.05}

    stale_now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc) + timedelta(hours=241)
    config = load_strategy_config(tmp_path, now=stale_now)
    assert config.ml_sizing_adjustment == {}

    # A replay before the publication must reject it as future data. Checking
    # this through the aggregate loader (rather than only the leaf loader)
    # protects the actual run_dca configuration path from look-ahead bias.
    replay_before_publish = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    config = load_strategy_config(tmp_path, now=replay_before_publish)
    assert config.ml_sizing_adjustment == {}


def test_ml_sizing_adjustment_stale_file_is_a_no_op(tmp_path):
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload())
    now = datetime(2026, 8, 10, 0, tzinfo=timezone.utc) + timedelta(hours=241)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_future_generated_at_is_a_no_op(tmp_path):
    # A clock skew or corrupted timestamp in the future must not be trusted.
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload())
    now = datetime(2026, 8, 9, 0, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_out_of_bounds_value_rejects_whole_file(tmp_path):
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(
        adjustments={"AAPL": 1.05, "MSFT": 5.0},
    ))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_case_folded_duplicate_symbol_rejects_whole_file(tmp_path):
    """Regression test for "Reject case-folded duplicate ML adjustment
    symbols". Both "AAPL" and "aapl" individually pass every check, but
    normalized[] is keyed by the UPPERCASED symbol -- without a duplicate
    check, whichever one this loop reaches last would silently win, with
    JSON insertion order (not something an operator can reliably reason
    about) deciding which live sizing multiplier actually applies.
    """
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(
        adjustments={"AAPL": 1.05, "aapl": 0.95},
    ))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_boolean_value_rejects_whole_file(tmp_path):
    """bool is a subclass of int in Python -- isinstance(True, (int, float))
    is True -- so without an explicit bool exclusion, a malformed
    "adjustments": {"AAPL": true} would silently coerce to float(True) ==
    1.0, itself always inside [min_multiplier, max_multiplier] (min <= 1.0
    <= max is enforced separately), and pass through as a real, live 1.0
    sizing adjustment instead of being rejected as invalid input (Antigravity
    review: "Boolean Values Converted to Multipliers")."""
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(
        adjustments={"AAPL": True},
    ))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_oversized_integer_rejects_whole_file(tmp_path):
    """Regression test for "Reject oversized JSON integers before float
    conversion". json.loads() parses an arbitrarily large JSON integer into
    a Python int with no size limit of its own -- a 400-digit adjustment
    value passes the real-non-bool-int type check but overflows float()'s
    own finite range, raising an uncaught OverflowError. Since this loader
    runs inside every load_strategy_config() call, that would abort live
    strategy startup entirely instead of returning the documented
    fail-closed {} for a malformed publish.
    """
    huge_int = 10**400
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(
        adjustments={"AAPL": huge_int},
    ))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


@pytest.mark.parametrize("bad_bounds", [
    {"min_multiplier": True, "max_multiplier": 1.2},
    {"min_multiplier": 0.8, "max_multiplier": True},
])
def test_ml_sizing_adjustment_boolean_bound_rejects_whole_file(tmp_path, bad_bounds):
    """Regression test for "Reject booleans in ML multiplier bounds". A
    hand-edited or malformed publication with "min_multiplier": true would
    otherwise silently coerce to float(True) == 1.0 -- itself always a
    valid bound (min <= 1.0 <= max is enforced separately) -- applying a
    live sizing adjustment from a payload that is actually schema-invalid
    instead of the documented fail-closed no-op.
    """
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(
        bounds=bad_bounds, adjustments={"AAPL": 1.05},
    ))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


@pytest.mark.parametrize("bad_bounds", [
    {"min_multiplier": 10**400, "max_multiplier": 1.2},
    {"min_multiplier": 0.8, "max_multiplier": 10**400},
])
def test_ml_sizing_adjustment_oversized_bound_rejects_whole_file(tmp_path, bad_bounds):
    """Regression test for "Catch overflow while parsing multiplier bounds".

    A 400-digit JSON integer in bounds.min_multiplier/max_multiplier passes
    _reject_bool_numeric()'s real-non-bool-numeric check but overflows
    float()'s own finite range there, raising OverflowError -- distinct from
    the already-caught (KeyError, TypeError, ValueError). Since this loader
    runs inside every load_strategy_config() call, an uncaught OverflowError
    here aborted strategy startup entirely instead of the documented
    fail-closed {} no-op for this optional publication.
    """
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(
        bounds=bad_bounds, adjustments={"AAPL": 1.05},
    ))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_malformed_generated_at_is_a_no_op(tmp_path):
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(generated_at="not-a-date"))
    assert load_ml_sizing_adjustment(tmp_path) == {}


@pytest.mark.parametrize("naive_generated_at", ["2026-08-10", "2026-08-10T00:00:00"])
def test_ml_sizing_adjustment_date_only_or_timezone_less_generated_at_is_a_no_op(tmp_path, naive_generated_at):
    """Regression test for "Reject date-only publication timestamps".
    schemas/ml_sizing_adjustment.schema.json declares generated_at as
    "format": "date-time" -- RFC 3339 requires a timezone offset, but
    datetime.fromisoformat() is more permissive and happily parses a
    date-only string or a timezone-less timestamp as NAIVE. Previously that
    naive value was silently assumed to be UTC instead of refused as the
    schema-invalid publication it actually is -- schema validation itself
    never runs against an alternate DCA_CONFIG_DIR this loader might read
    from.
    """
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(generated_at=naive_generated_at))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_malformed_json_is_a_no_op_not_a_raise(tmp_path):
    """Regression test for "Catch malformed ML publication JSON". Unlike
    load_inverse_martingale_config (which deliberately RAISES on a
    malformed file), a truncated/invalid-JSON ml_sizing_adjustment.json
    must fold into the same documented {} no-op as every other malformed-
    payload case -- this loader runs unconditionally inside
    load_strategy_config(), so an uncaught ValueError here would abort
    startup for every scheduled run over one optional, ancillary
    sizing-nudge publish, not merely disable the nudge itself.
    """
    (tmp_path / "ml_sizing_adjustment.json").write_text("{not valid json", encoding="utf-8")
    assert load_ml_sizing_adjustment(tmp_path) == {}


def test_ml_sizing_adjustment_non_object_json_is_a_no_op_not_a_raise(tmp_path):
    (tmp_path / "ml_sizing_adjustment.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_ml_sizing_adjustment(tmp_path) == {}


def test_ml_sizing_adjustment_missing_bounds_is_a_no_op(tmp_path):
    payload = _fresh_ml_payload()
    del payload["bounds"]
    _write(tmp_path, "ml_sizing_adjustment.json", payload)
    assert load_ml_sizing_adjustment(tmp_path) == {}


@pytest.mark.parametrize("bad_staleness", [float("nan"), float("inf"), float("-inf"), 0, -5])
def test_ml_sizing_adjustment_non_finite_or_nonpositive_staleness_is_a_no_op(tmp_path, bad_staleness):
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(max_staleness_hours=bad_staleness))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_unrepresentably_large_staleness_is_a_no_op(tmp_path):
    """Regression test for "Bound staleness values before constructing a
    timedelta". A value like 1e308 is finite (passes math.isfinite) but not
    REPRESENTABLE as a timedelta (internally bounded to roughly
    roughly +/-2.7 million years) -- timedelta(hours=1e308) raises OverflowError.
    Uncaught, that would abort this ENTIRE load_strategy_config() call
    instead of the documented fail-closed {} no-op -- and since
    max_staleness_hours is read from a PERSISTED file, one malformed
    publish would break every subsequent load until manually fixed.
    """
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(max_staleness_hours=1e308))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_rejects_bounds_wider_than_the_schema_hard_limit(tmp_path):
    # 0.01..100 is internally consistent (min <= 1.0 <= max) but far outside
    # the schema's real 0.5..1.5 ceiling -- must not be trusted just because
    # the file declares it.
    _write(tmp_path, "ml_sizing_adjustment.json", _fresh_ml_payload(
        bounds={"min_multiplier": 0.01, "max_multiplier": 100.0},
        adjustments={"AAPL": 1.05},
    ))
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    assert load_ml_sizing_adjustment(tmp_path, now=now) == {}


def test_ml_sizing_adjustment_no_op_leaves_graded_multiplier_unchanged():
    config_without = StrategyConfig()
    config_with_empty = StrategyConfig(ml_sizing_adjustment={})
    assert (
        graded_buy_multiplier(0.02, config_without, "AAPL")
        == graded_buy_multiplier(0.02, config_with_empty, "AAPL")
    )


def test_ml_sizing_adjustment_applies_only_to_configured_ticker():
    config = StrategyConfig(ml_sizing_adjustment={"AAPL": 1.1})
    base = StrategyConfig()
    assert graded_buy_multiplier(0.02, config, "AAPL") == pytest.approx(
        graded_buy_multiplier(0.02, base, "AAPL") * 1.1
    )
    assert graded_buy_multiplier(0.02, config, "MSFT") == graded_buy_multiplier(0.02, base, "MSFT")


class _FixedSignalData:
    """Minimal MarketDataProvider fake: constant prior close and a
    caller-supplied decision price, with a declining 60-minute trend so
    evaluate_source_signal's trend filter is satisfied."""

    def __init__(self, prev_close: float, decision_price: float):
        self._prev = prev_close
        self._price = decision_price
        self.source = SourceName.FINANCE

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(as_of.date() - timedelta(days=1), datetime.min.time().replace(hour=16), tzinfo=EASTERN)
        return Bar(ts, self._prev, self._prev, self._prev, self._prev, 1000)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        bars = []
        cursor = start
        late_threshold = end - timedelta(minutes=30)
        while cursor <= end:
            price = self._price if cursor >= late_threshold else self._price + 2.0
            bars.append(Bar(cursor, price, price, price, price, 100))
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_quote(self, ticker, as_of):
        return (self._price, as_of)


def test_ml_adjustment_cannot_push_a_real_buy_below_the_broker_minimum():
    """A low ml_sizing_adjustment factor (schema allows down to 0.5) combined
    with the base 1x tier must never produce a sub-dollar suggested amount
    for a genuine buy -- brokers reject sub-dollar notional orders."""
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FixedSignalData(prev_close=100.0, decision_price=99.7)  # -0.3% -> 1x tier
    config = StrategyConfig(ml_sizing_adjustment={"AAPL": 0.5})
    signal = evaluate_source_signal("AAPL", now, config, data, 15)
    assert signal.action == Action.BUY
    assert signal.buy_multiplier == pytest.approx(0.5)  # the ML factor is still visible on the multiplier
    assert signal.suggested_amount_usd >= 1.0  # but the final dollar amount is floored


def test_ml_adjustment_does_not_floor_a_genuine_no_buy():
    now = datetime(2026, 6, 29, 15, 45, tzinfo=EASTERN)
    data = _FixedSignalData(prev_close=100.0, decision_price=100.5)  # above prior close -> no buy
    config = StrategyConfig(ml_sizing_adjustment={"AAPL": 0.5})
    signal = evaluate_source_signal("AAPL", now, config, data, 15)
    assert signal.action != Action.BUY
    assert signal.suggested_amount_usd == 0.0


def test_budgets_maps_surviving_fields_only(tmp_path):
    # Daily/weekly caps live in the BUDGET_TIERS ladder, not StrategyConfig; only the
    # base amount, rollover-reserve cap, and hard per-order ceiling are overlaid.
    # The budget_tiers block is ignored.
    _write(tmp_path, "budgets.json", {
        "base_amount_usd": 2.0,
        "base_amount_below_close_usd": 1.0,
        "weekly_rollover_reserve_cap_usd": 900.0,
        "budget_tiers": [{"name": "normal", "daily_cap_usd": 180.0}],
        # max_consecutive_placement_failures is REQUIRED whenever budgets.json
        # exists (Codex review, PR #786: "Reject an omitted breaker in the
        # strategy loader") -- an existing file omitting it is refused rather
        # than silently disabling the breaker, so every synthetic budgets.json
        # fixture in this module supplies it even when the test itself is
        # about a different field entirely.
        "circuit_breakers": {"max_per_order_usd": 10.0, "max_consecutive_placement_failures": 3},
    })
    out = load_budgets(tmp_path)
    assert out == {
        "base_buy_amount_usd": 2.0,
        "base_buy_amount_below_close_usd": 1.0,
        "weekly_rollover_reserve_cap_usd": 900.0,
        "max_per_order_usd": 10.0,
        "max_consecutive_placement_failures": 3,
        # This fixture's circuit_breakers never sets whole_share_max_per_order_usd,
        # so load_budgets stamps the "not opted in" marker load_strategy_config
        # later pops and resolves against the effective ordinary ceiling (Codex
        # review, PR #786: "Require an explicit whole-share ceiling in existing
        # budgets") -- load_budgets's own raw output, read directly here rather
        # than through load_strategy_config, still carries it.
        "_whole_share_ceiling_not_opted_in": True,
    }
    # ... and the collapse itself still holds, asserted where it now happens.
    assert load_strategy_config(tmp_path).whole_share_max_per_order_usd == 10.0


def test_budgets_overlays_max_orders_per_day(tmp_path):
    """The roster-wide daily order-count circuit breaker
    (near_close_dca.StrategyConfig.max_orders_per_day, enforced by
    within_caps()) is overlaid the same way max_per_order_usd is, so a real
    config load picks it up without every caller wiring it explicitly (Codex
    review: "Apply the order-count cap to every live placement path")."""
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {"max_orders_per_day": 8, "max_consecutive_placement_failures": 3},
    })
    out = load_budgets(tmp_path)
    assert out == {
        "max_orders_per_day": 8,
        "max_consecutive_placement_failures": 3,
        # See test_budgets_maps_surviving_fields_only's own comment: this
        # fixture doesn't opt into whole_share_max_per_order_usd either, so
        # load_budgets's raw output still carries the marker.
        "_whole_share_ceiling_not_opted_in": True,
    }
    config = load_strategy_config(tmp_path)
    assert config.max_orders_per_day == 8
    assert config.whole_share_max_per_order_usd == StrategyConfig().max_per_order_usd


@pytest.mark.parametrize("bad_value", [0, -1, True, 3.5, "8", None])
def test_max_orders_per_day_rejects_non_positive_int(tmp_path, bad_value):
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {"max_orders_per_day": bad_value, "max_consecutive_placement_failures": 3},
    })
    with pytest.raises(ValueError, match="max_orders_per_day must be a positive integer"):
        load_budgets(tmp_path)


def test_max_orders_per_day_defaults_to_uncapped_when_absent(tmp_path):
    """A config directory with no circuit_breakers.max_orders_per_day key must
    leave StrategyConfig's own None (uncapped) default untouched, matching
    every other optional overlay in this module."""
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {"max_per_order_usd": 10.0, "max_consecutive_placement_failures": 3},
    })
    out = load_budgets(tmp_path)
    assert "max_orders_per_day" not in out
    config = load_strategy_config(tmp_path)
    assert config.max_orders_per_day is None


def test_budgets_does_not_overlay_one_order_per_ticker_per_day(tmp_path):
    """The bundled config/budgets.json asserts order_rules.one_order_per_ticker_per_day:
    true for the seven-slot roster's own bookkeeping, but StrategyConfig defaults it to
    False specifically to preserve the legacy manual-morning -> scheduled-afternoon
    lower-price top-up path. run_dca.run_slot() is the one caller allowed to turn it on,
    via an explicit config override -- load_strategy_config() must never pick it up from
    JSON, or every legacy load_strategy_config() caller (including the afternoon live
    stream, see docs/live_wiring.md) would have its top-up path silently blocked by the
    roster-only policy (Codex review: "Keep first-wins policy out of legacy config
    loads")."""
    _write(tmp_path, "budgets.json", {
        "order_rules": {"one_order_per_ticker_per_day": True},
        "circuit_breakers": {"max_consecutive_placement_failures": 3},
    })
    out = load_budgets(tmp_path)
    assert "one_order_per_ticker_per_day" not in out
    config = load_strategy_config(tmp_path)
    assert config.one_order_per_ticker_per_day is False


def test_tickers_loaded(tmp_path):
    _write(tmp_path, "tickers.json", {"active": ["AAA", "BBB"], "excluded": ["CCC"]})
    out = load_tickers(tmp_path)
    assert out["tickers"] == ("AAA", "BBB")
    assert out["excluded_tickers"] == frozenset({"CCC"})


def test_malformed_json_raises(tmp_path):
    (tmp_path / "tickers.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not parse"):
        load_tickers(tmp_path)

def test_read_json_missing_file(tmp_path):
    assert _read_json(tmp_path / "missing.json") is None

def test_read_json_oserror(tmp_path):
    # Simulate OSError by trying to read a directory instead of a file
    with pytest.raises(ValueError, match="could not parse config file"):
        _read_json(tmp_path)

def test_read_json_not_a_dict(tmp_path):
    file_path = tmp_path / "not_dict.json"
    file_path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON object, got list"):
        _read_json(file_path)

def test_read_json_valid(tmp_path):
    file_path = tmp_path / "valid.json"
    file_path.write_text('{"key": "value"}', encoding="utf-8")
    assert _read_json(file_path) == {"key": "value"}

def test_read_json_rejects_duplicate_top_level_keys(tmp_path):
    file_path = tmp_path / "dup.json"
    file_path.write_text('{"AAPL": {"enabled": true}, "AAPL": {"enabled": false}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key 'AAPL'"):
        _read_json(file_path)

def test_read_json_rejects_duplicate_nested_keys(tmp_path):
    file_path = tmp_path / "dup_nested.json"
    file_path.write_text(
        '{"tickers": {"AAPL": {"enabled": true, "enabled": false}}}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate key 'enabled'"):
        _read_json(file_path)


def test_load_near_close_overrides_missing_file(tmp_path):
    assert load_near_close_overrides(tmp_path) == {}

def test_load_near_close_overrides_filters_unknown_and_excluded(tmp_path):
    _write(
        tmp_path,
        "near_close_dca.json",
        {
            "base_buy_amount_usd": 5.0,
            "dry_run": False,
            "broker": "test",
            "unknown_field": "ignore"
        }
    )
    assert load_near_close_overrides(tmp_path) == {"base_buy_amount_usd": 5.0}


def test_load_near_close_overrides_excludes_sizing_validated_fields(tmp_path):
    """Regression test for "Prevent generic overrides from bypassing sizing validation".

    inverse_martingale/ml_sizing_adjustment (and their scalar siblings) each
    have their own dedicated, validated loader; letting near_close_dca.json
    set them directly would let an unchecked value silently replace an
    already-validated one, since load_near_close_overrides() is merged AFTER
    both dedicated loaders in load_strategy_config().
    """
    _write(
        tmp_path,
        "near_close_dca.json",
        {
            "base_buy_amount_usd": 5.0,
            "inverse_martingale_enabled_default": True,
            "inverse_martingale_unit_usd": 999.0,
            "inverse_martingale_max_cycle_days": 1,
            "inverse_martingale_max_fire_attempts": 1,
            "inverse_martingale": {"AAPL": {"enabled": True}},
            "ml_sizing_adjustment": {"AAPL": 1.4999},
        },
    )
    assert load_near_close_overrides(tmp_path) == {"base_buy_amount_usd": 5.0}


def test_load_strategy_config_near_close_overrides_cannot_bypass_inverse_martingale_validation(tmp_path):
    """End-to-end: even with a validated inverse_martingale.json present,
    near_close_dca.json cannot inject a different, unvalidated
    inverse_martingale mapping through the generic override path."""
    _write(tmp_path, "inverse_martingale.json", {
        "status": "active",
        "tickers": {"AAPL": {
            "enabled": True, "basis": "pct",
            "tiers": [{"drop_pct": 1.0, "units": 1.0}],
            "recovery_offset_pct": 1.0,
        }},
    })
    _write(tmp_path, "near_close_dca.json", {
        "inverse_martingale": {"MSFT": {"enabled": True}},
    })
    config = load_strategy_config(tmp_path)
    assert set(config.inverse_martingale) == {"AAPL"}


def test_load_backtest_sizing_tiers_valid(tmp_path):
    _write(tmp_path, "backtest_sizing_ladder.json", {
        "tiers": [
            {"min_drop_pct": 1.0, "multiplier": 1.5},
            {"min_drop_pct": 0.1, "multiplier": 1.0},
        ],
    })
    out = load_backtest_sizing_tiers(tmp_path)
    assert len(out) == 2
    assert out[0] == (pytest.approx(0.001), 1.0)  # sorted, percent->fraction
    assert out[1] == (pytest.approx(0.01), 1.5)

def test_load_backtest_sizing_tiers_missing_file(tmp_path):
    assert load_backtest_sizing_tiers(tmp_path) == ()

def test_load_backtest_sizing_tiers_no_tiers_list(tmp_path):
    _write(tmp_path, "backtest_sizing_ladder.json", {"other_key": "value"})
    assert load_backtest_sizing_tiers(tmp_path) == ()

def test_load_backtest_sizing_tiers_empty_tiers(tmp_path):
    _write(tmp_path, "backtest_sizing_ladder.json", {"tiers": []})
    assert load_backtest_sizing_tiers(tmp_path) == ()

def test_load_backtest_sizing_tiers_missing_keys(tmp_path):
    _write(tmp_path, "backtest_sizing_ladder.json", {"tiers": [{"min_drop_pct": 1.0}]})
    with pytest.raises(ValueError, match="missing min_drop_pct/multiplier"):
        load_backtest_sizing_tiers(tmp_path)

def test_load_backtest_sizing_tiers_negative_values(tmp_path):
    _write(tmp_path, "backtest_sizing_ladder.json", {"tiers": [{"min_drop_pct": -1.0, "multiplier": 1.0}]})
    with pytest.raises(ValueError, match="negative threshold/multiplier"):
        load_backtest_sizing_tiers(tmp_path)

def test_default_config_dir_basic(monkeypatch):
    """Test that get_config_dir returns an absolute path ending with 'config' when DCA_CONFIG_DIR is not set."""
    monkeypatch.delenv("DCA_CONFIG_DIR", raising=False)
    config_dir = get_config_dir()
    assert config_dir.is_absolute()
    assert config_dir.name == "config"

def test_default_config_dir_env_override(monkeypatch, tmp_path):
    """DCA_CONFIG_DIR, when set, is returned verbatim even if it doesn't end with 'config'."""
    override_dir = tmp_path / "etc" / "all-stock-dca"
    monkeypatch.setenv("DCA_CONFIG_DIR", str(override_dir))
    assert get_config_dir() == override_dir


# --- ${VAR} expansion in config values ---------------------------------------
# The live brokerage account number lives in the environment, not in git, so
# config/*.json carries "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}" and the loaders
# resolve it. These pin the resolution rules.

def test_expand_config_env_substitutes_a_set_variable(monkeypatch):
    monkeypatch.setenv("DCA_TEST_ACCOUNT", "123456789")
    assert expand_config_env("${DCA_TEST_ACCOUNT}", source="t") == "123456789"
    assert expand_config_env("robinhood:${DCA_TEST_ACCOUNT}", source="t") == "robinhood:123456789"


def test_expand_config_env_passes_through_plain_values(monkeypatch):
    """A value with no ${...} reference is returned verbatim.

    config/morning_dca.json's watchlist task carries the literal
    "read_only" in the same field the live task uses for the account reference.
    """
    monkeypatch.delenv("DCA_TEST_ACCOUNT", raising=False)
    assert expand_config_env("read_only", source="t") == "read_only"
    assert expand_config_env("", source="t") == ""
    # A bare $ is not a reference -- only the braced form is recognised, so a
    # note or a path containing '$' can never be misread as a variable.
    assert expand_config_env("cost is $5", source="t") == "cost is $5"


@pytest.mark.parametrize("value", ["", None], ids=["empty", "unset"])
def test_expand_config_env_raises_when_the_variable_is_missing(monkeypatch, value):
    """Fail loudly rather than substituting "".

    An empty account number is precisely the value that used to disable account
    pinning (near_close_dca.account_pinning_failure), so silently expanding to
    "" would reintroduce that hole through the config path.
    """
    if value is None:
        monkeypatch.delenv("DCA_TEST_ACCOUNT", raising=False)
    else:
        monkeypatch.setenv("DCA_TEST_ACCOUNT", value)
    with pytest.raises(MissingConfigEnvError, match="DCA_TEST_ACCOUNT"):
        expand_config_env("${DCA_TEST_ACCOUNT}", source="budgets.json broker_roles")


def test_expand_config_env_names_its_source_in_the_error(monkeypatch):
    monkeypatch.delenv("DCA_TEST_ACCOUNT", raising=False)
    with pytest.raises(MissingConfigEnvError, match="midday live_task.broker_account"):
        expand_config_env("${DCA_TEST_ACCOUNT}", source="midday live_task.broker_account")


# --- Per-mode broker-account binding -----------------------------------------
# load_morning_config used to read raw["live_task"]["broker_account"] for EVERY
# mode, so the read-only watchlist scan silently bound the live
# trading account. Once the account moved into the environment that became
# load-breaking: constructing a read-only executor raised MissingConfigEnvError
# on a host where the secret is scoped only to the trading jobs (Codex review,
# PR #219).

def test_live_morning_mode_still_requires_the_account_variable(monkeypatch):
    from dca_config import MissingConfigEnvError
    from morning_dca import load_morning_config

    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    with pytest.raises(MissingConfigEnvError, match="ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"):
        load_morning_config(mode="live")


def test_live_morning_mode_binds_the_configured_account(monkeypatch):
    from morning_dca import load_morning_config

    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "123456789")
    assert load_morning_config(mode="live").target_account_number == "123456789"


# --- Broker-account validation at pinning time -------------------------------
# expand_config_env only guarantees the variable was set to something non-empty,
# so a malformed value used to flow into execution AND into the artifact. The
# live classification schema requires account_number to match ^[0-9]{6,12}$ for
# every *_dca_live run, so emitting a malformed value -- or omitting the field to
# avoid emitting one -- produced schema-invalid evidence for exactly the runs
# that matter most (Codex review, PR #219).

@pytest.mark.parametrize(
    "bad",
    ["not-an-account", "12345", "1234567890123", "9990 00111", "acct-999", "-123456789"],
)
def test_malformed_accounts_are_rejected_at_load(monkeypatch, bad):
    from dca_config import InvalidAccountNumberError, resolve_broker_account

    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", bad)
    with pytest.raises(InvalidAccountNumberError):
        resolve_broker_account(
            "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", source="test broker_account"
        )

def test_surrounding_whitespace_is_stripped_not_rejected(monkeypatch):
    """An account exported with a stray newline is unambiguous; failing on it
    would be a needless outage."""
    from dca_config import resolve_broker_account

    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "  123456789\n")
    resolved = resolve_broker_account(
        "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", source="test broker_account"
    )
    assert resolved == "123456789"


def test_read_only_literal_passes_through(monkeypatch):
    from dca_config import resolve_broker_account

    assert resolve_broker_account("read_only", source="watchlist") == "read_only"


def test_the_live_loader_rejects_a_malformed_account(monkeypatch):
    from dca_config import InvalidAccountNumberError
    from morning_dca import load_morning_config

    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "not-an-account")
    with pytest.raises(InvalidAccountNumberError):
        load_morning_config(mode="live")


def test_the_midday_loader_rejects_a_malformed_account(monkeypatch):
    from dca_config import InvalidAccountNumberError
    from midday_dca import load_midday_config

    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "12")
    with pytest.raises(InvalidAccountNumberError):
        load_midday_config()


# --- load_tickers's whole_share_tickers ---------------------------------------
# BYDDY's category (near_close_dca.WHOLE_SHARE_TICKERS, docs/whole_share_accumulation.md)
# is sourced from tickers.json's "whole_share" array -- a single source of
# truth alongside "active"/"excluded", not a separate opt-in config file.


def test_load_tickers_missing_whole_share_key_is_a_no_op(tmp_path):
    _write(tmp_path, "tickers.json", {"active": ["AAPL"], "excluded": []})
    assert "whole_share_tickers" not in load_tickers(tmp_path)


def test_load_tickers_returns_whole_share_tickers(tmp_path):
    _write(tmp_path, "tickers.json", {"active": ["AAPL"], "excluded": [], "whole_share": ["BYDDY"]})
    assert load_tickers(tmp_path)["whole_share_tickers"] == frozenset({"BYDDY"})


def test_load_tickers_rejects_a_non_list_whole_share(tmp_path):
    """Codex review ("Reject a malformed whole-share collection at config
    load"): unlike "active"/"excluded" above, a present-but-malformed
    "whole_share" value must be rejected outright, not silently ignored --
    silently skipping it leaves whole_share_tickers at StrategyConfig's
    empty-set default, so the afternoon run starts successfully but never
    evaluates or accumulates the intended whole-share ticker, with no
    configuration error explaining the omission. Mirrors the same
    validation the whole-share-aware maintenance and ML loaders already
    apply to this same field.
    """
    _write(tmp_path, "tickers.json", {"active": ["AAPL"], "excluded": [], "whole_share": "BYDDY"})
    with pytest.raises(ValueError):
        load_tickers(tmp_path)


def test_load_tickers_whole_share_tickers_preserved_verbatim(tmp_path):
    # Matches tickers/excluded_tickers' own convention: entries are taken
    # verbatim (already expected to be uppercase in tickers.json), not
    # normalized here.
    _write(tmp_path, "tickers.json", {"active": ["AAPL"], "excluded": [], "whole_share": ["BYDDY"]})
    assert load_tickers(tmp_path)["whole_share_tickers"] == frozenset({"BYDDY"})


def test_whole_share_max_per_order_is_loaded_from_circuit_breakers(tmp_path):
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {
            "max_per_order_usd": 10.0,
            "whole_share_max_per_order_usd": 50.0,
            "max_consecutive_placement_failures": 3,
        }
    })
    assert load_strategy_config(tmp_path).whole_share_max_per_order_usd == 50.0


def test_an_omitted_whole_share_ceiling_collapses_to_the_ordinary_one(tmp_path):
    """An existing budgets.json that never opted into the whole-share
    allowance must not silently receive it. StrategyConfig defaults the
    whole-share ceiling to $50 -- introduced by this PR -- so a
    `$DCA_CONFIG_DIR` configuring only `max_per_order_usd: 10` would have
    authorized a $50 whole-share fire in an environment that never asked
    (Codex review, PR #786: "Require an explicit whole-share ceiling in
    existing budgets").

    The schema does not require the key, unlike
    max_consecutive_placement_failures, so there is nothing to fail closed
    against; the allowance collapses to the ceiling that config DID set, which
    is the behaviour before this knob existed."""
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {
            "max_per_order_usd": 10.0,
            "max_consecutive_placement_failures": 3,
        }
    })
    # The COLLAPSE is pinned here; where it can happen is a separate fact.
    # `load_budgets` deliberately does NOT resolve the number: "effective" has
    # to include the caller's own `base`, which that function cannot see, and
    # resolving it there substituted StrategyConfig's $10 class default and
    # then OVERRODE an explicit `base(max_per_order_usd=1,
    # whole_share_max_per_order_usd=1)` -- silently raising a ceiling the
    # caller had set (Codex review, PR #786: "Preserve explicit-base
    # whole-share limits without budgets"). So the loader records only that the
    # config did not opt in, and `load_strategy_config` resolves it once the
    # base is known. Asserting the collapsed number AT the loader boundary
    # would re-require the resolution that fix removed, so what is pinned at
    # that boundary is the marker and the ABSENCE of a resolved value.
    import dca_config

    budgets = load_budgets(tmp_path)
    assert budgets[dca_config._WHOLE_SHARE_CEILING_NOT_OPTED_IN] is True
    assert "whole_share_max_per_order_usd" not in budgets
    config = load_strategy_config(tmp_path)
    assert config.max_per_order_usd == 10.0
    assert config.whole_share_max_per_order_usd == 10.0

    # An explicit opt-in still gets the allowance...
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {
            "max_per_order_usd": 10.0,
            "whole_share_max_per_order_usd": 50.0,
            "max_consecutive_placement_failures": 3,
        }
    })
    assert load_strategy_config(tmp_path).whole_share_max_per_order_usd == 50.0

    # ... and the shipped config is an opt-in, so RVII stays buyable.
    shipped = load_strategy_config(get_config_dir())
    assert shipped.whole_share_max_per_order_usd > shipped.max_per_order_usd


@pytest.mark.parametrize("field", [
    "whole_share_max_per_order_usd",
    "max_per_order_usd",
])
def test_a_boolean_per_order_ceiling_is_rejected(tmp_path, field):
    """`float(True)` is `1.0`, so a schema-invalid `true` loaded silently as a
    real limit — `whole_share_max_per_order_usd: true` became a $1 whole-share
    allowance. `load_strategy_config` does not schema-validate an alternate
    `$DCA_CONFIG_DIR`, so that malformed safety limit reached live whole-share
    execution instead of failing closed (Codex review, PR #786: "Reject
    booleans for the whole-share ceiling").

    Parametrized over the SIBLING too: `max_per_order_usd` accepted `true`
    identically, which the finding did not name. `bool` is an `int` subclass,
    so each field's own `math.isfinite`/`> 0` check cannot catch it — `1.0` is
    finite and positive."""
    breakers = {
        "max_per_order_usd": 10.0,
        "whole_share_max_per_order_usd": 50.0,
        "max_consecutive_placement_failures": 3,
    }
    breakers[field] = True
    _write(tmp_path, "budgets.json", {"circuit_breakers": breakers})
    with pytest.raises(ValueError, match="boolean"):
        load_strategy_config(tmp_path)


def test_a_boolean_allocation_field_is_rejected(tmp_path):
    """The same rule reaches every numeric budget field, not only the two
    ceilings — the allocation, reserve and kill-switch values all went through
    a bare `float()` as well."""
    _write(tmp_path, "budgets.json", {
        "monthly_cash_allocation_usd": True,
        "circuit_breakers": {"max_consecutive_placement_failures": 3},
    })
    with pytest.raises(ValueError, match="boolean"):
        load_strategy_config(tmp_path)


def test_whole_share_max_per_order_below_max_per_order_is_rejected(tmp_path):
    """Below max_per_order_usd it is not an allowance at all -- it would
    silently TIGHTEN the ordinary ceiling on the whole-share path, which is the
    opposite of what this knob exists to do."""
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {
            "max_per_order_usd": 10.0,
            "whole_share_max_per_order_usd": 5.0,
            "max_consecutive_placement_failures": 3,
        }
    })
    with pytest.raises(ValueError, match="must not be below"):
        load_strategy_config(tmp_path)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_an_unusable_whole_share_max_per_order_is_rejected(tmp_path, bad):
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {
            "max_per_order_usd": 10.0,
            "whole_share_max_per_order_usd": bad,
            "max_consecutive_placement_failures": 3,
        }
    })
    with pytest.raises(ValueError, match="whole_share_max_per_order_usd"):
        load_strategy_config(tmp_path)


def test_shipped_budgets_json_clears_the_whole_share_universe_share_prices():
    """A whole-share ceiling below the category's real share prices makes
    `whole_share` a do-not-buy list in disguise -- the RVII defect (PR #786)."""
    loaded = load_strategy_config(get_config_dir())
    assert loaded.whole_share_max_per_order_usd >= loaded.max_per_order_usd
    assert loaded.whole_share_max_per_order_usd >= 25.0


def test_shipped_tickers_json_enrolls_byddy_as_whole_share():
    """The checked-in config/tickers.json's "whole_share" array enrolls
    BYDDY -- the ticker this category was built for -- and, per the AUG 24
    2026 update recorded in that file's own "notes", RVII too (not tradable
    for fractional/notional buys either). "excluded" carries QBUT and KVUE,
    which Robinhood returned not-found for on every live run (see
    config/tickers.json's exclusion_reasons)."""
    loaded = load_tickers(get_config_dir())
    assert loaded["whole_share_tickers"] == frozenset({"BYDDY", "RVII"})
    assert loaded["excluded_tickers"] == frozenset({"QBUT", "KVUE"})
    for whole_share in ("BYDDY", "RVII"):
        assert whole_share not in loaded["tickers"]


# --- load_tickers rejects overlapping categories --------------------------
# active/excluded/whole_share must be pairwise disjoint (Codex review: "Reject
# overlapping ticker categories at config load") -- test_config_invariants.py
# only checks the bundled config/tickers.json, so load_tickers itself must
# fail closed for any OTHER $DCA_CONFIG_DIR too.


def test_load_tickers_rejects_a_symbol_in_both_active_and_whole_share(tmp_path):
    _write(
        tmp_path,
        "tickers.json",
        {"active": ["AAPL", "BYDDY"], "excluded": [], "whole_share": ["BYDDY"]},
    )
    with pytest.raises(ValueError, match="pairwise disjoint"):
        load_tickers(tmp_path)


def test_load_tickers_rejects_a_case_only_overlap(tmp_path):
    """Codex review ("Normalize symbols before checking category overlap"):
    "aapl" active and "AAPL" whole_share are the SAME instrument to every
    consumer (near_close_dca normalizes tickers before use), so a
    case-insensitive collision must be rejected even though the raw
    strings differ. The lowercase spelling lives in "active" here (not
    itself symbol-validated) rather than "whole_share": since "Reject
    noncanonical whole-share symbols at config load" made whole_share
    entries themselves canonical-uppercase-only, a lowercase whole_share
    entry is now rejected earlier by that check instead of ever reaching
    this overlap comparison.
    """
    _write(
        tmp_path,
        "tickers.json",
        {"active": ["aapl"], "excluded": [], "whole_share": ["AAPL"]},
    )
    with pytest.raises(ValueError, match="pairwise disjoint"):
        load_tickers(tmp_path)


def test_load_tickers_rejects_a_noncanonical_whole_share_symbol(tmp_path):
    """Codex review ("Reject noncanonical whole-share symbols at config
    load"): a supported alternate $DCA_CONFIG_DIR is not schema-validated,
    so a lowercase entry like "byddy" would otherwise pass straight through
    into StrategyConfig.whole_share_tickers verbatim -- RobinhoodDataProvider's
    quote/bar lookups compare the broker's canonical UPPERCASE response
    symbol exactly, so the configured ticker would silently receive no
    Robinhood data and never reach accumulation or placement at all.
    """
    _write(
        tmp_path,
        "tickers.json",
        {"active": [], "excluded": [], "whole_share": ["byddy"]},
    )
    with pytest.raises(ValueError, match="invalid ticker symbol"):
        load_tickers(tmp_path)


def test_load_tickers_rejects_a_dots_only_whole_share_symbol(tmp_path):
    """Codex review ("Reject dot-only whole-share symbols at config
    load"): dca_whole_share_accumulator._SYMBOL_PATTERN was hardened to
    require at least one letter (rejecting a dots-only value like "..",
    path-traversal-risky as a filesystem component), but this loader's own
    _WHOLE_SHARE_SYMBOL_PATTERN still accepted it -- a value that would
    later fail only at ledger use, past the intended fail-closed config
    boundary, for an alternate $DCA_CONFIG_DIR not covered by the bundled
    schema check.
    """
    _write(
        tmp_path,
        "tickers.json",
        {"active": [], "excluded": [], "whole_share": [".."]},
    )
    with pytest.raises(ValueError, match="invalid ticker symbol"):
        load_tickers(tmp_path)


def test_load_tickers_rejects_a_symbol_in_both_active_and_excluded(tmp_path):
    _write(
        tmp_path,
        "tickers.json",
        {"active": ["AAPL"], "excluded": ["AAPL"], "whole_share": []},
    )
    with pytest.raises(ValueError, match="pairwise disjoint"):
        load_tickers(tmp_path)


def test_load_tickers_rejects_a_symbol_in_both_excluded_and_whole_share(tmp_path):
    _write(
        tmp_path,
        "tickers.json",
        {"active": ["AAPL"], "excluded": ["BYDDY"], "whole_share": ["BYDDY"]},
    )
    with pytest.raises(ValueError, match="pairwise disjoint"):
        load_tickers(tmp_path)


def test_load_tickers_accepts_disjoint_categories(tmp_path):
    _write(
        tmp_path,
        "tickers.json",
        {"active": ["AAPL"], "excluded": ["DEAD"], "whole_share": ["BYDDY"]},
    )
    loaded = load_tickers(tmp_path)
    assert loaded["tickers"] == ("AAPL",)
    assert loaded["excluded_tickers"] == frozenset({"DEAD"})
    assert loaded["whole_share_tickers"] == frozenset({"BYDDY"})


def test_absent_budgets_json_does_not_enable_the_whole_share_allowance(tmp_path):
    """A config directory with NO budgets.json must not get the raised ceiling.

    ``load_strategy_config`` explicitly supports that fallback and supplies the
    canonical whole_share ticker set in it, so an installation missing the file
    was handed StrategyConfig's own $50 ``whole_share_max_per_order_usd``
    beside its $10 ``max_per_order_usd`` -- authorizing a $50 whole-share fire
    in an environment that never opted into the raised ceiling (Codex review,
    PR #786: "Do not enable the allowance without budgets.json").

    This is round 26's finding in the case its fix could not see: the collapse
    lived inside ``load_budgets``, past an early return taken when the file is
    absent."""
    from near_close_dca import StrategyConfig

    assert StrategyConfig().whole_share_max_per_order_usd == 50.0  # the trap

    out = load_budgets(tmp_path)
    # The marker, not a resolved number -- see
    # test_budgets_maps_surviving_fields_only for why.
    assert out["_whole_share_ceiling_not_opted_in"] is True
    # ... alongside a marker saying the FILE was absent, which
    # `load_strategy_config` resolves into the shipped breaker and order cap.
    # Both are markers rather than numbers on purpose: only that layer sees the
    # caller's `base` and keyword overrides, and a default emitted here would
    # outrank an explicit one (Codex review, PR #786: "Keep a breaker in the
    # missing-budgets fallback", "Preserve an explicit base breaker in the
    # fallback"). The intent this test carries -- that the whole-share
    # allowance is NOT enabled by an absent file -- is unchanged.
    assert set(out) == {"_whole_share_ceiling_not_opted_in", "_budgets_file_absent"}

    config = load_strategy_config(config_dir=tmp_path)
    assert config.max_per_order_usd == 16.0  # 2026-10-04 ladder rescale: StrategyConfig default ceiling 10 -> 16
    # Collapsed to the ordinary ceiling -- exactly the behaviour before this
    # knob existed, which is what "never opted in" has to mean.
    assert config.whole_share_max_per_order_usd == 16.0


def test_absent_budgets_json_is_still_not_an_unvalidated_config(tmp_path):
    """The fall-through must not turn the supported fallback into a hard error.

    The ``max_consecutive_placement_failures`` refusal is deliberately scoped
    to "budgets.json EXISTS but omits the key". Letting the absent-file case
    fall through to the shared collapse must not also drag it into that raise
    -- and must not weaken it for a file that really is present."""
    import json

    # Absent file: loads, does not raise, breaker left at its prior default.
    config = load_strategy_config(config_dir=tmp_path)
    assert config.whole_share_max_per_order_usd == 16.0

    # Present file omitting the breaker: still refused, unchanged.
    (tmp_path / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_per_order_usd": 10}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="max_consecutive_placement_failures"):
        load_strategy_config(config_dir=tmp_path)


def test_an_explicit_base_keeps_its_own_whole_share_ceiling(tmp_path):
    """A caller's own `base` is part of the "effective ordinary ceiling".

    ``load_budgets`` cannot see it, so synthesizing the collapsed value there
    substituted StrategyConfig's class default and then OVERRODE the base --
    ``StrategyConfig(max_per_order_usd=1, whole_share_max_per_order_usd=1)``
    loaded as ordinary $1 / whole-share $10, silently authorizing an order
    between $1 and $10 that the caller had explicitly prohibited (Codex
    review, PR #786: "Preserve explicit-base whole-share limits without
    budgets").

    The marker moved the decision to ``load_strategy_config``, the only layer
    that sees budgets.json, the base, AND the caller's keyword overrides."""
    import json

    from near_close_dca import StrategyConfig

    base = StrategyConfig(max_per_order_usd=1.0, whole_share_max_per_order_usd=1.0)

    # No budgets.json at all -- the case the finding reported.
    config = load_strategy_config(config_dir=tmp_path, base=base)
    assert config.max_per_order_usd == 1.0
    assert config.whole_share_max_per_order_usd == 1.0

    # ... and a budgets.json that exists but omits both keys, which has the
    # identical defect for the identical reason. The finding named only the
    # absent-file case; this one came out of the sweep.
    (tmp_path / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_consecutive_placement_failures": 3}}),
        encoding="utf-8",
    )
    config = load_strategy_config(config_dir=tmp_path, base=base)
    assert config.whole_share_max_per_order_usd == 1.0

    # A ceiling budgets.json DOES set still wins over the base -- but a base
    # whose own whole-share cap sits BELOW it is a contradiction, and is
    # refused rather than silently widened.
    #
    # This assertion originally expected $5: the collapse assigned the ordinary
    # ceiling outright, RAISING the base's explicit $1 whole-share cap. That is
    # the very harm this test was written for, in the direction the first fix
    # did not cover, so the collapse now only ever lowers and the leftover
    # inversion is refused (Codex review, PR #786: "Validate the whole-share
    # ceiling after applying overrides"). The assertion was wrong, not the fix.
    (tmp_path / "budgets.json").write_text(
        json.dumps({
            "circuit_breakers": {
                "max_per_order_usd": 5, "max_consecutive_placement_failures": 3,
            }
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="whole_share_max_per_order_usd"):
        load_strategy_config(config_dir=tmp_path, base=base)

    # A base consistent with that ceiling loads, and the collapse follows the
    # config's own ordinary ceiling rather than either default.
    config = load_strategy_config(
        config_dir=tmp_path, base=StrategyConfig(max_per_order_usd=5.0)
    )
    assert config.max_per_order_usd == 5.0
    assert config.whole_share_max_per_order_usd == 5.0

    # ... and a caller's own keyword override is the highest layer.
    config = load_strategy_config(
        config_dir=tmp_path, base=StrategyConfig(max_per_order_usd=3.0),
        max_per_order_usd=3.0,
    )
    assert config.whole_share_max_per_order_usd == 3.0


def test_the_non_opt_in_marker_never_reaches_a_strategy_config(tmp_path):
    """The marker is internal and must not survive into the dataclass.

    It is deliberately not a ``StrategyConfig`` field: a standalone
    ``load_budgets`` caller that splatted the overlay verbatim would raise on
    it, which is the loud failure rather than a silently wrong ceiling."""
    from near_close_dca import StrategyConfig

    assert "_whole_share_ceiling_not_opted_in" in load_budgets(tmp_path)
    config = load_strategy_config(config_dir=tmp_path)
    assert not hasattr(config, "_whole_share_ceiling_not_opted_in")
    assert not any(
        f.name == "_whole_share_ceiling_not_opted_in"
        for f in dataclasses.fields(StrategyConfig)
    )


def test_the_ceiling_invariant_is_checked_on_the_final_resolved_values():
    """Keyword overrides must not be able to invert the allowance.

    The budgets.json-time check compares only JSON-derived numbers, so a
    caller passing both limits as overrides bypassed it entirely:
    ``load_strategy_config(max_per_order_usd=100,
    whole_share_max_per_order_usd=50)`` loaded with the whole-share ceiling
    BELOW the ordinary one, and ``_whole_share_executable_share_count`` then
    starts from the $100 ordinary limit and can authorize a whole-share order
    between $50 and $100 that the caller explicitly capped (Codex review,
    PR #786: "Validate the whole-share ceiling after applying overrides")."""
    import tempfile
    from pathlib import Path

    from near_close_dca import StrategyConfig

    with tempfile.TemporaryDirectory() as td:
        config_dir = Path(td)

        with pytest.raises(ValueError, match="whole_share_max_per_order_usd"):
            load_strategy_config(
                config_dir=config_dir,
                max_per_order_usd=100.0,
                whole_share_max_per_order_usd=50.0,
            )

        # An inverted `base` reaches the same check, rather than being
        # silently widened into validity by the collapse.
        with pytest.raises(ValueError, match="whole_share_max_per_order_usd"):
            load_strategy_config(
                config_dir=config_dir,
                base=StrategyConfig(
                    max_per_order_usd=100.0, whole_share_max_per_order_usd=50.0
                ),
            )

        # The valid direction is untouched: an allowance ABOVE the ordinary
        # ceiling is the whole point of the knob.
        config = load_strategy_config(
            config_dir=config_dir,
            max_per_order_usd=100.0,
            whole_share_max_per_order_usd=150.0,
        )
        assert config.whole_share_max_per_order_usd == 150.0

        # ... and so is a consistent base with no overrides at all.
        config = load_strategy_config(
            config_dir=config_dir,
            base=StrategyConfig(max_per_order_usd=1.0, whole_share_max_per_order_usd=1.0),
        )
        assert config.whole_share_max_per_order_usd == 1.0


def test_keyword_overrides_cannot_bypass_the_budget_field_validation():
    """The documented override layer walked past every rule ``load_budgets``
    applies.

    ``max_consecutive_placement_failures="3"`` reached ``StrategyConfig``
    intact and then raised ``TypeError`` at
    ``consecutive_placement_failures >= config.max_consecutive_placement_failures``
    on the FIRST placement failure, aborting the rest of the batch, while
    ``False``/``0``/``1.5`` silently changed what the breaker means (Codex
    review, PR #786: "Validate breaker overrides after merging them").

    The finding named that one field. Probing the override layer showed the
    bypass was systemic -- ``0``, ``-1``, ``True``, ``"8"`` and ``2.5`` all
    reached the config through ``max_orders_per_day`` too, and ``NaN``/``True``
    through the money fields. Swept as a class, mirroring the JSON path's own
    rules so a value refused from ``budgets.json`` is refused from an override
    identically."""
    import tempfile
    from pathlib import Path

    rejected: dict[str, list[Any]] = {
        "max_consecutive_placement_failures": ["3", False, True, 0, -1, 1.5],
        "max_orders_per_day": [0, -1, True, "8", 2.5],
        "max_per_order_usd": [0, -1, True, float("nan"), float("inf")],
        "whole_share_max_per_order_usd": [0, -1, True],
        "base_buy_amount_usd": [-1, True],
        "weekly_rollover_reserve_cap_usd": [-1, True],
        "monthly_cash_allocation_usd": [float("nan"), True],
        "kill_switch_7d_deploy_fraction": [-1, True, float("nan")],
    }
    with tempfile.TemporaryDirectory() as td:
        config_dir = Path(td)
        for field, values in rejected.items():
            for value in values:
                with pytest.raises(ValueError):
                    kwargs: dict[str, Any] = {field: value}
                    load_strategy_config(config_dir=config_dir, **kwargs)

        # The valid direction is untouched, including a deliberate zero on a
        # sizing field, where zero is meaningful rather than malformed.
        config = load_strategy_config(
            config_dir=config_dir,
            max_orders_per_day=8,
            max_per_order_usd=12.0,
            whole_share_max_per_order_usd=60.0,
            max_consecutive_placement_failures=3,
        )
        assert config.max_orders_per_day == 8
        assert config.max_per_order_usd == 12.0
        assert config.whole_share_max_per_order_usd == 60.0
        assert config.max_consecutive_placement_failures == 3
        assert load_strategy_config(
            config_dir=config_dir, base_buy_amount_usd=0.0
        ).base_buy_amount_usd == 0.0
        # Absent budgets.json is a supported fallback, not a malformed value,
        # so it does not raise -- but it no longer leaves the breaker UNSET
        # either. This assertion expected `None`, which is precisely the
        # behaviour identified as the defect: `_process_buy_candidates_batch`
        # enforces the threshold only when the field is non-null, so a live
        # caller on the fallback could attempt the whole batch through a
        # broker-wide outage (Codex review, PR #786: "Keep a breaker in the
        # missing-budgets fallback"). The assertion encoded the behaviour, not
        # the intent, so it is corrected rather than worked around -- the
        # seventh time in this series.
        assert load_strategy_config(
            config_dir=config_dir
        ).max_consecutive_placement_failures == DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES

    # ... and the UPPER bound too, for the one field whose JSON path has one.
    # Round 44 swept this class for the lower bound and stopped there, so
    # `kill_switch_7d_deploy_fraction=2` loaded a fraction `load_budgets`
    # refuses -- and `dca_risk` then computes the kill-switch threshold as
    # twice the monthly allocation, letting deployment pass 100% before the
    # guard engages (Codex review, PR #786: "Enforce the upper bound on
    # kill-switch overrides").
    for _bad in (2, 1.5, 1.0000001):
        with pytest.raises(ValueError, match="kill_switch_7d_deploy_fraction"):
            load_strategy_config(config_dir=config_dir, kill_switch_7d_deploy_fraction=_bad)
    # The bounds themselves still load, and so does anything inside them.
    for _ok in (0.0, 0.4, 1.0):
        assert (
            load_strategy_config(
                config_dir=config_dir, kill_switch_7d_deploy_fraction=_ok
            ).kill_switch_7d_deploy_fraction
            == _ok
        )


def test_per_ticker_sizing_loader_exposes_linear_interpolation_flag(tmp_path):
    tiers = [{"min_drop_pct": 0.5, "multiplier": 1.0}, {"min_drop_pct": 1.0, "multiplier": 2.0}]
    _write(tmp_path, "per_ticker_sizing.json", {
        "status": "active",
        "tickers": {
            "SMOOTH": {"base_amount_usd": 1.0, "base_amount_below_close_usd": 1.0, "tiers": tiers, "interpolation": "linear"},
            "STEPPED": {"base_amount_usd": 1.0, "base_amount_below_close_usd": 1.0, "tiers": tiers},
            "ODD": {"base_amount_usd": 1.0, "base_amount_below_close_usd": 1.0, "tiers": tiers, "interpolation": "cubic"},
        },
    })
    loaded = load_per_ticker_sizing(tmp_path)["per_ticker_sizing"]
    assert loaded["SMOOTH"]["interpolate"] is True
    assert loaded["STEPPED"]["interpolate"] is False
    assert loaded["ODD"]["interpolate"] is False  # anything but "linear" fails safe to the stepped lookup
