"""Tests for the config loader (dca_config)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from dca_config import (
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


def test_load_strategy_config_leaves_inverse_martingale_off_by_default(tmp_path):
    config = load_strategy_config(tmp_path)
    assert config.inverse_martingale_enabled_default is False
    assert config.inverse_martingale == {}


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
    data = _FixedSignalData(prev_close=100.0, decision_price=99.0)  # -1.0% -> 1x tier
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
        "circuit_breakers": {"max_per_order_usd": 10.0},
    })
    out = load_budgets(tmp_path)
    assert out == {
        "base_buy_amount_usd": 2.0,
        "base_buy_amount_below_close_usd": 1.0,
        "weekly_rollover_reserve_cap_usd": 900.0,
        "max_per_order_usd": 10.0,
    }


def test_budgets_overlays_max_orders_per_day(tmp_path):
    """The roster-wide daily order-count circuit breaker
    (near_close_dca.StrategyConfig.max_orders_per_day, enforced by
    within_caps()) is overlaid the same way max_per_order_usd is, so a real
    config load picks it up without every caller wiring it explicitly (Codex
    review: "Apply the order-count cap to every live placement path")."""
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {"max_orders_per_day": 8},
    })
    out = load_budgets(tmp_path)
    assert out == {"max_orders_per_day": 8}
    config = load_strategy_config(tmp_path)
    assert config.max_orders_per_day == 8


@pytest.mark.parametrize("bad_value", [0, -1, True, 3.5, "8", None])
def test_max_orders_per_day_rejects_non_positive_int(tmp_path, bad_value):
    _write(tmp_path, "budgets.json", {
        "circuit_breakers": {"max_orders_per_day": bad_value},
    })
    with pytest.raises(ValueError, match="max_orders_per_day must be a positive integer"):
        load_budgets(tmp_path)


def test_max_orders_per_day_defaults_to_uncapped_when_absent(tmp_path):
    """A config directory with no circuit_breakers.max_orders_per_day key must
    leave StrategyConfig's own None (uncapped) default untouched, matching
    every other optional overlay in this module."""
    _write(tmp_path, "budgets.json", {"circuit_breakers": {"max_per_order_usd": 10.0}})
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

    config/morning_dca.json's watchlist/preflight tasks carry the literal
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
# mode, so the read-only watchlist/preflight scans silently bound the live
# trading account. Once the account moved into the environment that became
# load-breaking: constructing a read-only executor raised MissingConfigEnvError
# on a host where the secret is scoped only to the trading jobs -- and the 9:45
# preflight artifact is a hard dependency of the 10:30 live run (Codex review,
# PR #219).

@pytest.mark.parametrize("mode", ["watchlist", "preflight"])
def test_read_only_morning_modes_do_not_need_the_account_variable(monkeypatch, mode):
    from morning_dca import load_morning_config

    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    config = load_morning_config(mode=mode)
    assert config.target_account_number == "read_only"


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
    ["not-an-account", "12345", "1234567890123", "5620 57042", "acct-562", "-123456789"],
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
