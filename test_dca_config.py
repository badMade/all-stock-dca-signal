"""Tests for the config loader (dca_config)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from dca_config import (
    MissingConfigEnvError,
    _read_json,
    expand_config_env,
    get_config_dir,
    load_approved_dates,
    load_backtest_sizing_tiers,
    load_budgets,
    load_near_close_overrides,
    load_per_ticker_sizing,
    load_sizing_tiers,
    load_strategy_config,
    load_tickers,
)
from near_close_dca import StrategyConfig, graded_buy_multiplier, select_first_allocation_base_usd


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
