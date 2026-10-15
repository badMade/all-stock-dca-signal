#!/usr/bin/env python3
"""Comprehensive tests for scripts/stream_candidates.py edge cases and validation rules."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from near_close_dca import SourceName
from scripts.stream_candidates import (
    StreamMarketData,
    StreamSpec,
    _allocation_priority_key,
    _primary_payload_is_fresh,
    _require_config_number,
    _require_parseable_stream_config,
    _run_with_args,
    _validate_tickers,
    build_document,
    run_multi_slot_cli,
)


def test_require_config_number_validations():
    # 1. Boolean check
    with pytest.raises(ValueError, match="must be a number, got the boolean True"):
        _require_config_number(True, "test_field", positive=True)

    # 2. Non-finite / non-number
    with pytest.raises(ValueError, match="must be a finite number"):
        _require_config_number("string_val", "test_field", positive=True)
    with pytest.raises(ValueError, match="must be a finite number"):
        _require_config_number(float("nan"), "test_field", positive=True)
    with pytest.raises(ValueError, match="must be a finite number"):
        _require_config_number(float("inf"), "test_field", positive=True)

    # 3. Positive check
    with pytest.raises(ValueError, match="must be greater than zero"):
        _require_config_number(0, "test_field", positive=True)
    with pytest.raises(ValueError, match="must be greater than zero"):
        _require_config_number(-1.5, "test_field", positive=True)

    # 4. Non-negative check
    with pytest.raises(ValueError, match="must not be negative"):
        _require_config_number(-0.01, "test_field", positive=False)

    # Valid values pass without raising
    _require_config_number(10, "test_field", positive=True)
    _require_config_number(0.0, "test_field", positive=False)


def test_require_parseable_stream_config_validations(tmp_path):
    cfg_file = tmp_path / "stream_cfg.json"

    # 1. Corrupt json
    cfg_file.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable, malformed, or has a duplicate JSON key"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 2. Top-level not a dict
    cfg_file.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON object"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 3. Missing required policy keys
    cfg_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required policy key"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 4. Stream mismatch
    valid_base: dict[str, Any] = {
        "stream": "midday",
        "live_task": "task",
        "sizing_ladder": {"base_amount_usd": 1.0, "tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]},
        "budget_ledger": {"morning_daily_cap_usd": 100.0, "morning_weekly_cap_usd": 500.0},
    }
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="declares stream 'midday', but this entry point is 'morning'"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 5. Hollow blocks
    valid_base["stream"] = "morning"
    valid_base["live_task"] = ""
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="has empty required policy block"):
        _require_parseable_stream_config(cfg_file, "morning")
    valid_base["live_task"] = "task"

    # 6. sizing_ladder not a dict
    valid_base["sizing_ladder"] = "not_a_dict"
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="'sizing_ladder' must be an object"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 7. missing ladder keys
    valid_base["sizing_ladder"] = {"base_amount_usd": 1.0}
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="'sizing_ladder' is missing or empty"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 8. tiers not a list or tier not a dict or missing tier key
    valid_base["sizing_ladder"] = {"base_amount_usd": 1.0, "tiers": "not_a_list"}
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="'sizing_ladder.tiers' must be a list"):
        _require_parseable_stream_config(cfg_file, "morning")

    valid_base["sizing_ladder"] = {"base_amount_usd": 1.0, "tiers": ["not_a_dict"]}
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="'sizing_ladder.tiers\\[0\\]' must be an object"):
        _require_parseable_stream_config(cfg_file, "morning")

    valid_base["sizing_ladder"] = {"base_amount_usd": 1.0, "tiers": [{"min_drop_pct": 1.0}]}
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="has no 'multiplier'"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 9. trend_min_bars validation. The field is REQUIRED, not merely validated
    # when present: every loader applies `int(raw["trend_min_bars"])` with a
    # hardcoded default behind it, so an absent key silently validates the
    # 60-minute downtrend on the fallback bar count rather than the checked-in
    # one (PR #898, "Require explicit trend bar minimum").
    valid_base["sizing_ladder"] = {"base_amount_usd": 1.0, "tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]}
    trend_min_bars_message = "'trend_min_bars' must be present and be an integer of at least 1"
    valid_base["trend_min_bars"] = True
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match=trend_min_bars_message):
        _require_parseable_stream_config(cfg_file, "morning")

    valid_base["trend_min_bars"] = 0
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match=trend_min_bars_message):
        _require_parseable_stream_config(cfg_file, "morning")

    del valid_base["trend_min_bars"]
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match=trend_min_bars_message):
        _require_parseable_stream_config(cfg_file, "morning")
    # A valid minimum from here on, so the ledger checks below are reached
    # rather than short-circuited by the presence requirement just proven.
    valid_base["trend_min_bars"] = 6

    # 10. budget_ledger not a dict
    valid_base["budget_ledger"] = "not_a_dict"
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="'budget_ledger' must be an object"):
        _require_parseable_stream_config(cfg_file, "morning")

    # 11. budget_ledger missing stream caps
    valid_base["budget_ledger"] = {"other_stream_daily_cap_usd": 100.0}
    cfg_file.write_text(json.dumps(valid_base), encoding="utf-8")
    with pytest.raises(ValueError, match="has no 'morning_daily_cap_usd' value"):
        _require_parseable_stream_config(cfg_file, "morning")


def test_validate_tickers():
    with pytest.raises(ValueError, match="must be a list of ticker strings"):
        _validate_tickers("label", "string_not_sequence", ("AAPL",))
    with pytest.raises(ValueError, match="must be a list of ticker strings"):
        _validate_tickers("label", cast(Any, 123), ("AAPL",))
    with pytest.raises(ValueError, match="names ticker\\(s\\) not in this config's active universe"):
        _validate_tickers("label", ["UNKNOWN"], ("AAPL",))
    # Excluded or extra recognized tickers pass when allow_extra is provided
    _validate_tickers("label", ["AAPL", "QBUT"], ("AAPL",), allow_extra=frozenset({"QBUT"}))


def test_ranking_key_edge_cases():
    # None or NaN drop falls back to 0.0
    row_none = {"multiplier_drop_pct": None, "buy_multiplier": 1.5, "ticker": "AAPL"}
    row_nan = {"multiplier_drop_pct": float("nan"), "buy_multiplier": 1.5, "ticker": "AAPL"}
    assert _allocation_priority_key(row_none) == (-1.5, -0.0, "AAPL")
    assert _allocation_priority_key(row_nan) == (-1.5, -0.0, "AAPL")


def test_run_multi_slot_cli_divergent_specs():
    spec1 = StreamSpec(
        slot="1130ET",
        runner_slot_id="1130",
        stream="intraday",
        cron_id="cron",
        executor_cls=object,
        config_loader=lambda *a, **kw: None,
        live_mode="live",
        stream_config_filename="cfg.json",
        source=SourceName.FINANCE,
        requires_deployed_last_7d_usd=True,
    )
    spec2 = StreamSpec(
        slot="1330ET",
        runner_slot_id="1330",
        stream="intraday",
        cron_id="cron",
        executor_cls=object,
        config_loader=lambda *a, **kw: None,
        live_mode="live",
        stream_config_filename="cfg.json",
        source=SourceName.ROBINHOOD,  # divergent source
        requires_deployed_last_7d_usd=True,
    )
    with pytest.raises(ValueError, match="disagree with '1130' on requires_deployed_last_7d_usd and/or source"):
        run_multi_slot_cli({"1130": spec1, "1330": spec2}, argv=[], description="test")


def test_run_with_args_payload_validation(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    config_dir = repo_root / "config"

    spec = StreamSpec(
        slot="1130ET",
        runner_slot_id="1130",
        stream="intraday",
        cron_id="cron",
        executor_cls=object,
        config_loader=lambda *a, **kw: None,
        live_mode="live",
        stream_config_filename="intraday_dca.json",
        source=SourceName.FINANCE,
        requires_deployed_last_7d_usd=False,
    )
    market_data_file = tmp_path / "market_data.json"

    # 1. Payload not a dict
    market_data_file.write_text("[1, 2, 3]", encoding="utf-8")
    args = argparse.Namespace(
        trading_day="2026-08-05",
        config_dir=config_dir,
        market_data=market_data_file,
    )
    with pytest.raises(ValueError, match="must contain a top-level JSON object"):
        _run_with_args(spec, args)

    # 2. source_errors not a list
    payload = {"sources": {spec.source.value: {}}, "source_errors": "not_a_list"}
    market_data_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="source_errors.*must be a list"):
        _run_with_args(spec, args)

    # 3. ticker_errors not an object
    payload = {"sources": {spec.source.value: {}}, "ticker_errors": "not_an_object"}
    market_data_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="ticker_errors.*must be an object"):
        _run_with_args(spec, args)

    # 4. ticker_errors has unsupported sources
    payload = {"sources": {spec.source.value: {}}, "ticker_errors": {"unknown_source": []}}
    market_data_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="has unsupported key"):
        _run_with_args(spec, args)

    # 5. ticker_errors for spec source is None
    payload = {"sources": {spec.source.value: {}}, "ticker_errors": {spec.source.value: None}}
    market_data_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="got null"):
        _run_with_args(spec, args)

    # 6. os.environ restore when DCA_CONFIG_DIR was already set (line 1455)
    orig_env = os.environ.get("DCA_CONFIG_DIR")
    try:
        os.environ["DCA_CONFIG_DIR"] = "custom_env_val"
        with pytest.raises(ValueError, match="must contain a top-level JSON object"):
            market_data_file.write_text("[]", encoding="utf-8")
            _run_with_args(spec, args)
        assert os.environ.get("DCA_CONFIG_DIR") == "custom_env_val"
    finally:
        if orig_env is None:
            os.environ.pop("DCA_CONFIG_DIR", None)
        else:
            os.environ["DCA_CONFIG_DIR"] = orig_env


def test_build_document_market_data_not_a_mapping():
    from intraday_dca import IntradayConfig
    repo_root = Path(__file__).resolve().parents[2]
    config_dir = repo_root / "config"

    spec = StreamSpec(
        slot="1130ET",
        runner_slot_id="1130",
        stream="intraday",
        cron_id="cron",
        executor_cls=object,
        config_loader=lambda *a, **kw: None,
        live_mode="live",
        stream_config_filename="intraday_dca.json",
        source=SourceName.FINANCE,
        requires_deployed_last_7d_usd=False,
    )
    config = IntradayConfig()
    with pytest.raises(ValueError, match="must be an object"):
        build_document(
            spec,
            trading_day=date(2026, 8, 5),
            config=config,
            market_data=cast(Any, "not_a_map"),
            config_dir=config_dir,
        )


def test_run_multi_slot_cli_success(tmp_path, monkeypatch):
    import scripts.generate_intraday_candidates as gic
    repo_root = Path(__file__).resolve().parents[2]
    config_dir = repo_root / "config"

    # Create dummy market data file with empty universe
    market_data_file = tmp_path / "market_data.json"
    market_data_file.write_text(
        json.dumps({"sources": {SourceName.FINANCE.value: {}}}),
        encoding="utf-8",
    )

    # Calling run_multi_slot_cli with --slot
    # It should reach _run_with_args and execute line 1406
    # Let's mock _run_with_args to return 42
    import scripts.stream_candidates as sc
    monkeypatch.setattr(sc, "_run_with_args", lambda spec, args: 42)
    ret = run_multi_slot_cli(
        gic.INTRADAY_SPECS,
        argv=["--slot", "1130", "--trading-day", "2026-08-05", "--config-dir", str(config_dir), "--market-data", str(market_data_file)],
        description="test",
    )
    assert ret == 42


def test_primary_payload_is_fresh_accepts_robinhood_nanosecond_zulu_quote_timestamp():
    """_primary_payload_is_fresh re-parses quote.as_of independently of
    _parse_quote -- confirmed it silently returns False (not a crash) under
    Python 3.10 for this exact Robinhood-shaped timestamp, misclassifying
    valid signal-time evidence as stale."""
    payload = {
        "previous_close_bar": {"close": 100.0},
        "quote": {"price": 100.0, "as_of": "2026-08-05T13:34:00.201412148Z"},
    }
    assert _primary_payload_is_fresh(
        payload,
        trading_day=date(2026, 8, 5),
        nominal_time=time(9, 35),
        interval_minutes=5,
        evaluation_delay_minutes=0,
    )


def test_stream_market_data_finance_quote_accepts_robinhood_nanosecond_zulu_timestamp():
    """StreamMarketData's Finance-quote override re-implements the same
    fromisoformat call inline rather than reusing _parse_quote -- must
    accept the same nanosecond+Z shape."""
    as_of = datetime(2026, 8, 5, 13, 35, 0, 201412, tzinfo=timezone.utc)
    provider = StreamMarketData(
        SourceName.FINANCE,
        {"TEST": {"quote": {"price": 123.45, "as_of": "2026-08-05T13:35:00.201412148Z"}}},
        quote_staleness_minutes=5,
        trading_day=date(2026, 8, 5),
    )
    assert provider.get_quote("TEST", as_of) == (123.45, as_of)



def test_cli_rejects_non_dictionary_payload(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps(["not", "a", "dictionary"]), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a top-level JSON object"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_missing_sources_key(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({"not_sources": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a top-level 'sources' object"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_unsupported_sources_key(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}, "unsupported_source": {}}
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="has unsupported key.*unsupported_source"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_aborts_when_primary_is_unavailable_without_fallback(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "source_errors": ["robinhood"],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="is unavailable/incomplete and no fallback source is configured"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_yahoo_finance_fallback_source(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC

    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {
            MIDDAY_SPEC.source.value: {},
            "yahoo_finance_yfinance": {"MSFT": {}},
        },
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="has unsupported key.*yahoo_finance_yfinance"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_explicit_null_in_ticker_errors(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "ticker_errors": None
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object, got null"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_explicit_null_in_ticker_errors_list(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "ticker_errors": {"robinhood": None}
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="if present, must be a list, got null"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_unsupported_error_source_in_ticker_errors(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "ticker_errors": {"finance": [], "unsupported_source": []}
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="has unsupported key.*unsupported_source"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_unsupported_source_in_source_errors(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "source_errors": ["unsupported_source"]
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="names unsupported source.*unsupported_source"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )


def test_cli_rejects_empty_sources_when_missing_primary_and_fallback(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {} # missing both robinhood (primary) and yahoo_finance_yfinance (fallback)
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="is missing the required 'robinhood' object"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_malformed_source_error_objects(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "source_errors": [{"not_source": "finance"}]
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="entries must be source-name strings"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_malformed_source_error_types(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "source_errors": [123]
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="entries must be source-name strings"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_structured_source_error_entry_aborts_primary_outage(tmp_path):
    from scripts.generate_midday_candidates import MIDDAY_SPEC
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"robinhood": {}},
        "source_errors": [{"source": "robinhood", "error": "Finance connector not available"}]
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="is unavailable/incomplete and no fallback source is configured"):
        _run_with_args(
            MIDDAY_SPEC,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_aborts_when_finance_primary_is_incomplete(tmp_path):
    import dataclasses

    from scripts.generate_morning_candidates import MORNING_SPEC
    spec = dataclasses.replace(MORNING_SPEC, source=SourceName.FINANCE)

    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"finance": {}, "robinhood": {}},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="finance data is unavailable/incomplete and no fallback source is configured"):
        _run_with_args(
            spec,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )

def test_cli_rejects_null_ticker_errors_on_effective_source(tmp_path):
    import dataclasses

    from scripts.generate_morning_candidates import MORNING_SPEC
    spec = dataclasses.replace(MORNING_SPEC, source=SourceName.FINANCE)

    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({
        "sources": {"finance": {}},
        "ticker_errors": {"finance": None},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="ticker_errors.finance.*must be a list, got null"):
        _run_with_args(
            spec,
            argparse.Namespace(trading_day="2026-08-05", market_data=path, config_dir=Path("config"), already_covered_tickers=[], decision_time_et=None, deployed_last_7d_usd=0)
        )
