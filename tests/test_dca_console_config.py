"""Config-page reader regressions: catalog, parity, effective config, gates, redaction, no locks."""

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_config
import dca_console_config as reader
from dca_console_reader import RootSpec
from dca_console_routes import walk_redact

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 17)
NOW = datetime(2026, 9, 17, 12, tzinfo=ET)
SYNTHETIC_ACCOUNT = "999000111"


@pytest.fixture
def cfg(tmp_path):
    """A private copy of the committed config so a test can mutate one file."""
    target = tmp_path / "config"
    shutil.copytree(CONFIG, target)
    return target


def write(cfg, stem, value):
    (cfg / f"{stem}.json").write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def load(cfg, stem):
    return json.loads((cfg / f"{stem}.json").read_bytes())


def approved_window_days(cfg):
    approved = sorted(dca_config.load_approved_dates(Path(cfg))["approved_test_dates"])
    assert approved
    return approved


def first_trading_day_after_window(cfg):
    day = approved_window_days(cfg)[-1] + timedelta(days=1)
    while day.weekday() >= 5 or day in reader.near_close_dca.nyse_holidays(day.year):
        day += timedelta(days=1)
    return day


def every_builder(cfg):
    # Builders take the RootSpec's path: the label is for the browser, the
    # path for the reader, exactly as the other console readers split them.
    cfg = RootSpec.of(cfg, "config").path
    return [
        reader.config_catalog(cfg, REPO),
        reader.config_file_view(cfg, "tickers"),
        reader.parity_checks(cfg),
        reader.effective_config(cfg, NOW),
        reader.ladder_preview(cfg),
        reader.ladder_point(cfg, 2.0, "AAPL"),
        reader.stream_flags(cfg),
        reader.sizing_sources(cfg, NOW),
        reader.live_gate_summary(cfg, DAY),
        reader.window_countdown(cfg, DAY),
        reader.build_config_snapshot(cfg, REPO, NOW, DAY),
    ]


def test_config_files_enumerates_the_committed_configs_and_schemas():
    stems = {stem for stem, _schema, _publisher in reader.CONFIG_FILES}
    assert stems == {path.stem for path in CONFIG.glob("*.json")}
    assert len(reader.CONFIG_FILES) == 21
    for stem, schema, _publisher in reader.CONFIG_FILES:
        committed = json.loads((CONFIG / f"{stem}.json").read_bytes()).get("$schema")
        if schema is None:
            assert committed is None, stem
        else:
            assert committed.endswith(f"{schema}.schema.json"), stem
            assert (REPO / "schemas" / f"{schema}.schema.json").is_file(), stem
    assert {s for s, _sc, publisher in reader.CONFIG_FILES if publisher} == {
        "per_ticker_sizing",
        "ml_sizing_adjustment",
    }
    assert sum(1 for _s, schema, _p in reader.CONFIG_FILES if schema is None) == 7


def test_catalog_on_committed_config_parses_every_file(cfg):
    result = reader.config_catalog(cfg, REPO)
    rows = {row["stem"]: row for row in result["files"]}
    assert len(rows) == 21 and result["warnings"] == []
    assert all(row["exists"] and row["parse_ok"] and row["error"] is None for row in rows.values())
    assert rows["tickers"]["schema_exists"] is True and rows["tickers"]["schema_ref"].endswith("tickers.schema.json")
    assert rows["per_ticker_sizing"]["schema_exists"] is None and rows["per_ticker_sizing"]["publisher_only"] is True
    assert rows["budgets"]["fail_mode"] == "raises" and rows["ml_sizing_adjustment"]["fail_mode"] == "degrades"
    assert rows["intraday_dca"]["fail_mode"] == "fails closed"
    assert all(row["fail_mode"] != "unknown" for row in rows.values())
    assert rows["budgets"]["loader"] == "dca_config.load_budgets"
    assert isinstance(rows["budgets"]["size"], int) and rows["budgets"]["modified"].endswith("+00:00")


def test_catalog_reports_absent_and_rejected_files_without_paths(cfg, tmp_path):
    write(cfg, "tickers", '{"active": ["A"], "active": ["B"]}')
    write(cfg, "budgets", "[1, 2]")
    (cfg / "self_heal.json").unlink()
    rows = {row["stem"]: row for row in reader.config_catalog(cfg, REPO)["files"]}
    assert rows["tickers"]["parse_ok"] is False and "duplicate key" in rows["tickers"]["error"]
    assert rows["budgets"]["parse_ok"] is False and "JSON object" in rows["budgets"]["error"]
    assert rows["self_heal"] == {**rows["self_heal"], "exists": False, "parse_ok": None, "error": None, "size": None}
    assert str(tmp_path) not in json.dumps(rows)


def test_file_view_neutralises_env_references_and_secret_values(cfg, monkeypatch):
    monkeypatch.setenv("CONSOLE_TEST_SET_VAR", "value-that-must-not-appear")
    monkeypatch.delenv("CONSOLE_TEST_UNSET_VAR", raising=False)
    write(
        cfg,
        "near_close_dca",
        {
            "a": "${CONSOLE_TEST_SET_VAR}",
            "b": "${CONSOLE_TEST_UNSET_VAR}",
            "account_number": SYNTHETIC_ACCOUNT,
            "n": 1.5,
        },
    )
    view = reader.config_file_view(cfg, "near_close_dca")
    assert view["found"] is True and view["truncated"] is False and view["error"] is None
    assert "env reference CONSOLE_TEST_SET_VAR (set)" in view["text"]
    assert "env reference CONSOLE_TEST_UNSET_VAR (unset)" in view["text"]
    assert "value-that-must-not-appear" not in view["text"] and SYNTHETIC_ACCOUNT not in view["text"]
    assert json.loads(view["text"])["n"] == 1.5


def test_file_view_rejects_unknown_stems_and_reports_rejected_files(cfg):
    assert reader.config_file_view(cfg, "../etc/passwd")["found"] is False
    assert reader.config_file_view(cfg, "tickers/../tickers")["found"] is False
    (cfg / "self_heal.json").unlink()
    assert reader.config_file_view(cfg, "self_heal")["found"] is False
    write(cfg, "budgets", "not json at all")
    view = reader.config_file_view(cfg, "budgets")
    assert view["found"] is True and view["text"] is None and "could not parse" in view["error"]
    write(cfg, "scheduled_tasks", {"rows": ["x" * 100] * 3000})
    big = reader.config_file_view(cfg, "scheduled_tasks")
    assert big["truncated"] is True and len(big["text"]) == reader.MAX_VIEW_CHARS


def test_parity_checks_agree_on_committed_config_and_diverge_on_mutation(cfg):
    committed = reader.parity_checks(cfg)
    assert {check["name"]: check["ok"] for check in committed["checks"]} == {
        "active_tickers_order": True,
        "excluded_tickers_set": True,
        "whole_share_tickers_set": True,
        "categories_disjoint": True,
        "whole_share_sizing_entries": True,
        "budgets_breaker": True,
        "schema_files_exist": True,
    }
    assert "config-invariants" in committed["note"]
    tickers = load(cfg, "tickers")
    tickers["active"] = list(reversed(tickers["active"]))
    tickers["excluded"] = tickers["excluded"] + ["ZZZZ"]
    tickers["whole_share"] = tickers["whole_share"] + [tickers["active"][0]]
    tickers["$schema"] = "../schemas/does_not_exist.schema.json"
    write(cfg, "tickers", tickers)
    sizing = load(cfg, "per_ticker_sizing")
    del sizing["tickers"]["BYDDY"]
    write(cfg, "per_ticker_sizing", sizing)
    budgets = load(cfg, "budgets")
    del budgets["circuit_breakers"]["max_consecutive_placement_failures"]
    write(cfg, "budgets", budgets)
    mutated = {check["name"]: check for check in reader.parity_checks(cfg)["checks"]}
    assert all(mutated[name]["ok"] is False for name in mutated)
    assert "ZZZZ" in mutated["excluded_tickers_set"]["detail"]
    assert "BYDDY" in mutated["whole_share_sizing_entries"]["detail"]
    assert "tickers" in mutated["schema_files_exist"]["detail"]


def test_parity_checks_are_none_when_inputs_are_unreadable(cfg):
    for stem in ("tickers", "per_ticker_sizing", "budgets"):
        (cfg / f"{stem}.json").unlink()
    result = reader.parity_checks(cfg)
    verdicts = {check["name"]: check["ok"] for check in result["checks"]}
    assert verdicts["active_tickers_order"] is None and verdicts["categories_disjoint"] is None
    assert verdicts["whole_share_sizing_entries"] is None and verdicts["budgets_breaker"] is None
    assert verdicts["schema_files_exist"] is True


def test_effective_config_labels_sources_and_never_exposes_the_account(cfg):
    write(cfg, "near_close_dca", {"trend_lookback_minutes": 90, "dry_run": False})
    result = reader.effective_config(cfg, NOW)
    fields = {field["name"]: field for field in result["fields"]}
    assert len(fields) == len(reader.dataclasses.fields(reader.near_close_dca.StrategyConfig))
    assert fields["trend_lookback_minutes"] == {
        "name": "trend_lookback_minutes",
        "value": 90,
        "default_differs": True,
        "source": "json",
        "note": None,
    }
    assert fields["dry_run"]["value"] is True and fields["dry_run"]["note"] == "never settable from JSON"
    assert fields["broker"]["value"] == "robinhood" and fields["broker"]["note"] == "never settable from JSON"
    assert fields["target_account_number"]["value"] in {"configured", "unset"}
    assert (
        isinstance(fields["approved_test_dates"]["value"], list)
        and fields["approved_test_dates"]["value"][0] == "2026-07-02"
    )
    assert isinstance(fields["sizing_tiers"]["value"][0], list)
    encoded = json.dumps(result, allow_nan=False)
    assert SYNTHETIC_ACCOUNT not in encoded and result["unresolved_env"] == []


def test_effective_config_reports_unresolved_env_by_name_and_loader_failures(cfg, monkeypatch, tmp_path):
    def raise_missing(*args, **kwargs):
        raise dca_config.MissingConfigEnvError(f"{tmp_path} references ${{CONSOLE_TEST_SECRET}} but it is unset")

    monkeypatch.setattr(dca_config, "load_strategy_config", raise_missing)
    result = reader.effective_config(cfg, NOW)
    assert result["fields"] == [] and result["unresolved_env"] == ["CONSOLE_TEST_SECRET"]
    assert str(tmp_path) not in json.dumps(result)
    monkeypatch.undo()
    budgets = load(cfg, "budgets")
    del budgets["circuit_breakers"]["max_consecutive_placement_failures"]
    write(cfg, "budgets", budgets)
    failed = reader.effective_config(cfg, NOW)
    assert failed["fields"] == [] and any("max_consecutive_placement_failures" in w for w in failed["warnings"])
    assert str(tmp_path) not in json.dumps(failed)


def test_ladder_preview_matches_the_strategy_functions(cfg):
    result = reader.ladder_preview(cfg)
    grid = {row["drop_pct"]: row for row in result["grid"]}
    assert len(grid) == 65 and result["warnings"] == []
    assert grid[0.0] == {
        "drop_pct": 0.0,
        "multiplier": 0.0,
        "base_usd": 0.0,
        "base_reason": "not_below_prior_close_no_buy",
        "candidate_usd": 0.0,
    }
    config = dca_config.load_strategy_config(cfg)
    for drop in (0.25, 2.0, 4.0, 12.0, 16.0):
        assert grid[drop]["multiplier"] == reader.near_close_dca.graded_buy_multiplier(drop / 100, config)
        assert grid[drop]["base_usd"] == 1.0 and grid[drop]["base_reason"] == "below_prior_close_only"
    assert [tier["name"] for tier in result["tiers"]] == ["normal", "elevated", "high_volatility", "stress"]
    assert result["tiers"][3]["uses_rollover_reserve"] is True
    assert result["per_ticker"] and all(row["entries"] > 0 and row["version"] for row in result["per_ticker"])
    json.dumps(result, allow_nan=False)


def test_ladder_point_validates_drop_and_ticker(cfg):
    for bad in (
        (-1, "AAPL"),
        (50.5, "AAPL"),
        ("x", "AAPL"),
        (float("nan"), "AAPL"),
        (3, "NOPE"),
        (3, None),
        (3, "QBUT"),
    ):
        assert reader.ladder_point(cfg, *bad)["found"] is False
    point = reader.ladder_point(cfg, 3.0, " aapl ")
    assert point["found"] is True and point["ticker"] == "AAPL" and point["base_usd"] == 1.0
    config = dca_config.load_strategy_config(cfg)
    assert point["multiplier"] == reader.near_close_dca.graded_buy_multiplier(0.03, config, ticker="AAPL")
    assert reader.ladder_point(cfg, 0, "AAPL")["multiplier"] == 0.0
    (cfg / "tickers.json").unlink()
    assert reader.ladder_point(cfg, 3.0, "AAPL")["found"] is False


def test_stream_flags_come_from_raw_json(cfg, tmp_path):
    result = reader.stream_flags(cfg)
    streams = {row["stream"]: row for row in result["streams"]}
    assert (
        streams["midday"]["live_execution_enabled"] is True
        and streams["market_open"]["live_execution_enabled"] is True
        and streams["intraday"]["live_execution_enabled"] is True
        and streams["morning"]["live_execution_enabled"] is True
    )
    assert streams["intraday"]["slots"] == ["1130", "1330", "1430"] and streams["midday"]["slots"] == ["12:30"]
    assert all(
        row["trend_min_bars"] == 6 and len(row["confidence_rule"]) <= reader.MAX_TEXT for row in streams.values()
    )
    assert result["run_classification"]["scheduled"]["run_origin"] == "scheduled"
    assert result["run_classification"]["manual"]["manual_run"] is True
    write(cfg, "midday_dca", "{broken")
    (cfg / "run_classification.json").unlink()
    broken = reader.stream_flags(cfg)
    assert {row["stream"]: row["found"] for row in broken["streams"]}["midday"] is False
    assert broken["run_classification"] is None and any("midday_dca.json" in w for w in broken["warnings"])
    assert str(tmp_path) not in json.dumps(broken)


def test_sizing_sources_report_staleness_and_publisher_only(cfg):
    # Reset to the safe inactive template regardless of whatever is
    # currently published live in config/ml_sizing_adjustment.json: this
    # test exercises reader.sizing_sources()'s staleness/publisher-only
    # logic, not the live-published state, which changes independently
    # whenever scripts/publish_octopus_sizing.py is next run.
    ml = load(cfg, "ml_sizing_adjustment")
    ml.update({"status": "inactive", "generated_at": None, "adjustments": {}})
    write(cfg, "ml_sizing_adjustment", ml)
    committed = reader.sizing_sources(cfg, NOW)
    assert committed["publisher_only"] is True and committed["ml"]["status"] == "inactive"
    assert committed["ml"]["stale"] is None and committed["ml"]["applied_count"] == 0
    assert committed["per_ticker"]["status"] == "active" and committed["per_ticker"]["entry_count"] > 0
    assert committed["per_ticker"]["loaded_count"] == committed["per_ticker"]["entry_count"]
    # 11 since QUBT joined fallback_tickers on 2026-09-24 (the corrected QBUT
    # symbol, no archived history yet to calibrate against).
    assert committed["per_ticker"]["fallback_count"] == 11
    ml = load(cfg, "ml_sizing_adjustment")
    ml.update(
        {"status": "active", "generated_at": (NOW - timedelta(hours=1)).isoformat(), "adjustments": {"AAPL": 1.1}}
    )
    write(cfg, "ml_sizing_adjustment", ml)
    fresh = reader.sizing_sources(cfg, NOW)["ml"]
    assert fresh["stale"] is False and fresh["applied_count"] == 1 and fresh["adjustment_count"] == 1
    ml["generated_at"] = (NOW - timedelta(days=20)).isoformat()
    write(cfg, "ml_sizing_adjustment", ml)
    stale = reader.sizing_sources(cfg, NOW)["ml"]
    assert stale["stale"] is True and stale["applied_count"] == 0
    ml["generated_at"] = "2026-09-17T10:00:00"
    write(cfg, "ml_sizing_adjustment", ml)
    assert reader.sizing_sources(cfg, NOW)["ml"]["stale"] is None
    write(cfg, "per_ticker_sizing", {"status": "active", "tickers": {"AAPL": {"tiers": []}}})
    broken = reader.sizing_sources(cfg, NOW)
    assert broken["per_ticker"]["loaded_count"] is None and any(
        "per_ticker_sizing.json" in w for w in broken["warnings"]
    )


def test_live_gate_summary_reports_names_never_values(cfg, monkeypatch):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    result = reader.live_gate_summary(cfg, DAY)
    gates = {gate["name"]: gate for gate in result["gates"]}
    assert result["banner"] == reader.BANNER and len(gates) == 6
    assert (
        gates["strategy_dry_run"]["state"] == "unconfigured" and "literally {}" in gates["strategy_dry_run"]["detail"]
    )
    # The broker's enable_live_orders is injected by the external runner, so a checkout can
    # never observe it: the gate is "unknown" whatever this process's environment says
    # (ed4745aa dropped the env/stream-policy inference that used to report "configured").
    assert gates["broker_enable_live_orders"]["state"] == "unknown"
    for env_value in ("false", "true"):
        monkeypatch.setenv("ROBINHOOD_ENABLE_LIVE_ORDERS", env_value)
        assert reader.live_gate_summary(cfg, DAY)["gates"][1]["state"] == "unknown"
    monkeypatch.delenv("ROBINHOOD_ENABLE_LIVE_ORDERS", raising=False)
    assert (
        gates["stream_live_execution_enabled"]["state"] == "configured"
        and "midday" in gates["stream_live_execution_enabled"]["detail"]
    )
    assert gates["account_number_env"]["state"] == "unconfigured"
    assert gates["approved_trading_window"]["state"] == "configured"
    assert gates["slot_windows"]["state"] == "configured" and "7 slot" in gates["slot_windows"]["detail"]
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", SYNTHETIC_ACCOUNT)
    write(cfg, "near_close_dca", {"trend_lookback_minutes": 90})
    armed = {gate["name"]: gate for gate in reader.live_gate_summary(cfg, DAY)["gates"]}
    assert armed["account_number_env"]["state"] == "configured" and SYNTHETIC_ACCOUNT not in json.dumps(armed)
    assert "not empty" in armed["strategy_dry_run"]["detail"] and armed["strategy_dry_run"]["state"] == "unconfigured"
    assert reader.live_gate_summary(cfg, first_trading_day_after_window(cfg))["gates"][4]["state"] == "unconfigured"


def test_window_countdown_covers_expiry_and_calendar(cfg):
    approved = approved_window_days(cfg)
    window_end = approved[-1]
    expected_years = list(range(approved[0].year, window_end.year + 2))
    inside = reader.window_countdown(cfg, DAY)
    assert inside["start"] == approved[0].isoformat() and inside["end"] == window_end.isoformat()
    assert inside["in_window"] is True and inside["expired"] is False and inside["expiring_warning"] is False
    assert inside["days_remaining"] > reader.near_close_dca.APPROVED_WINDOW_EXPIRY_WARN_DAYS
    assert inside["calendar_years"] == expected_years and inside["calendar_covers_day"] is True
    near_end = reader.window_countdown(cfg, approved[-5])
    assert near_end["in_window"] is True and near_end["expiring_warning"] is True and near_end["days_remaining"] == 5
    after = reader.window_countdown(cfg, first_trading_day_after_window(cfg))
    assert after["in_window"] is False and after["expired"] is True and after["expiring_warning"] is True
    assert after["days_remaining"] == 0
    assert reader.window_countdown(cfg, date(2030, 1, 3))["calendar_covers_day"] is False
    write(cfg, "trading_window", {"start_date": "2026-01-02"})
    rejected = reader.window_countdown(cfg, DAY)
    assert rejected["in_window"] is None and rejected["days_remaining"] is None and rejected["warnings"]


def test_absent_config_dir_is_unavailable_not_zero(tmp_path):
    absent = tmp_path / "nope"
    catalog = reader.config_catalog(absent, REPO)
    assert all(row["exists"] is False and row["parse_ok"] is None for row in catalog["files"])
    assert all(
        check["ok"] is None for check in reader.parity_checks(absent)["checks"] if check["name"] != "schema_files_exist"
    )
    countdown = reader.window_countdown(absent, DAY)
    assert countdown["in_window"] is None and countdown["calendar_years"] is None
    assert all(row["found"] is False for row in reader.stream_flags(absent)["streams"])
    sizing = reader.sizing_sources(absent, NOW)
    assert sizing["ml"]["adjustment_count"] is None and sizing["per_ticker"]["entry_count"] is None
    assert reader.ladder_preview(absent)["per_ticker"] == [] and len(reader.ladder_preview(absent)["grid"]) == 65
    snapshot = reader.build_config_snapshot(absent, REPO, NOW, DAY)
    assert snapshot["gates"]["gates"][4]["state"] == "unknown"
    # Zero readable stream files is no evidence either way, never "blocks live money".
    assert snapshot["gates"]["gates"][2]["state"] == "unknown"
    json.dumps(snapshot, allow_nan=False)


def test_snapshot_never_leaks_paths_or_the_account(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", SYNTHETIC_ACCOUNT)
    leak = f"{tmp_path} account {SYNTHETIC_ACCOUNT}"
    write(cfg, "near_close_dca", {"target_account_number": SYNTHETIC_ACCOUNT, "note": leak})
    midday = load(cfg, "midday_dca")
    midday["live_execution_enabled_note"] = leak
    midday["confidence_rule"] = leak
    write(cfg, "midday_dca", midday)
    ml = load(cfg, "ml_sizing_adjustment")
    ml["model_version"] = leak
    write(cfg, "ml_sizing_adjustment", ml)
    write(cfg, "budgets", "[" + leak)
    write(cfg, "self_heal", "{" + leak)
    everything = every_builder(cfg) + [
        reader.config_file_view(cfg, "midday_dca"),
        reader.config_file_view(cfg, "near_close_dca"),
    ]
    raw = json.dumps(everything, allow_nan=False)
    assert str(tmp_path) not in raw
    redacted = json.dumps(walk_redact(everything), allow_nan=False)
    assert SYNTHETIC_ACCOUNT not in redacted and str(tmp_path) not in redacted
    # A documented artifact path keeps its file name and loses its location.
    view = reader.config_file_view(cfg, "midday_dca")["text"]
    assert "[path]/run_state.json" in view and "/home/user" not in view
    assert "../schemas/tickers.schema.json" in reader.config_file_view(cfg, "tickers")["text"]


def test_builders_never_take_a_cross_process_lock(cfg, monkeypatch):
    import dca_runner
    import scripts.file_lock

    def refuse(*args, **kwargs):
        raise AssertionError("a console reader took a lock")

    monkeypatch.setattr(scripts.file_lock, "cross_process_lock", refuse)
    monkeypatch.setattr(dca_runner, "state_transaction", refuse)
    results = every_builder(cfg)
    assert len(results) == 11 and all("warnings" in result for result in results)


def test_text_redacts_and_scrubs_before_bounding():
    # Adversarial review: a cut before redaction split a 12-digit account literal
    # into a 9-digit tail the account regex no longer matched, and split a
    # documented path so its second segment was never scrubbed.
    # Whitespace before the digits: redact_value's account regex refuses a run
    # glued to a word (a job id's date stamp) by design, and the conftest export
    # of the synthetic account must not be what makes this pass.
    account = "x" * 495 + " 999000111"
    bounded = reader._text(account)
    assert bounded is not None
    assert len(bounded) <= reader.MAX_TEXT and "9990" not in bounded and bounded.endswith(" [red")
    assert "999000111" not in bounded
    # Prose separates a path with whitespace (a token glued to a word, like a
    # run-dir name "abc/def", is deliberately not a path); the cut lands inside
    # the path so only the redact-first ordering keeps "/home/" out.
    path = "y" * 493 + " /home/user/workspace/secretdir/f.json"
    scrubbed = reader._text(path)
    assert scrubbed is not None
    assert "/home/" not in scrubbed and len(scrubbed) <= reader.MAX_TEXT and scrubbed.endswith(" [path]")
    assert reader._text("short " + SYNTHETIC_ACCOUNT) == "short [redacted]"
    assert reader._text("see /home/user/workspace/run_state.json") == "see [path]/run_state.json"
    # A shorter limit still bounds the redacted text, and None stays None.
    assert len(reader._text("z" * 5000, 40)) == 40 and reader._text(None) is None
    # A pathological field is scanned only up to the pre-slice, never in full.
    huge = "w" * (reader.MAX_TEXT * reader._SCAN_LIMIT_FACTOR + 10) + SYNTHETIC_ACCOUNT
    assert len(reader._text(huge)) == reader.MAX_TEXT


def test_per_ticker_sizing_type_error_degrades_instead_of_raising(cfg):
    # dca_config.load_per_ticker_sizing does float(tier["min_drop_pct"]) (line 672):
    # null there is a TypeError, the failure a bad publish actually produces.
    write(
        cfg,
        "per_ticker_sizing",
        {
            "status": "active",
            "version": "v9",
            "tickers": {"AAPL": {"tiers": [{"min_drop_pct": None, "multiplier": 1.0}]}},
        },
    )
    with pytest.raises(TypeError):
        dca_config.load_per_ticker_sizing(cfg)
    preview = reader.ladder_preview(cfg)
    assert preview["per_ticker"] == [] and len(preview["grid"]) == 65
    assert any(w.startswith("per_ticker_sizing.json:") for w in preview["warnings"])
    sizing = reader.sizing_sources(cfg, NOW)
    assert sizing["per_ticker"]["loaded_count"] is None and sizing["per_ticker"]["entry_count"] == 1
    assert any(w.startswith("per_ticker_sizing.json:") for w in sizing["warnings"])
    snapshot = reader.build_config_snapshot(cfg, REPO, NOW, DAY)
    assert any(w.startswith("per_ticker_sizing.json:") for w in snapshot["warnings"])
    json.dumps(snapshot, allow_nan=False)


def test_stream_gate_distinguishes_unread_from_disabled(cfg):
    # Every stream file unreadable: no observation, so the gate is unknown.
    for stream in reader.STREAMS:
        write(cfg, f"{stream}_dca", "{broken")
    gates = {gate["name"]: gate for gate in reader.live_gate_summary(cfg, DAY)["gates"]}
    assert gates["stream_live_execution_enabled"]["state"] == "unknown"
    assert "no stream policy file could be read" in gates["stream_live_execution_enabled"]["detail"]
    # One file read and it disables live execution: that is evidence of "blocks".
    write(cfg, "market_open_dca", {"live_execution_enabled": False})
    gates = {gate["name"]: gate for gate in reader.live_gate_summary(cfg, DAY)["gates"]}
    assert gates["stream_live_execution_enabled"]["state"] == "unconfigured"
    assert "1 of 4 stream files read" in gates["stream_live_execution_enabled"]["detail"]
    # One file read and it enables live execution: permits live money.
    write(cfg, "market_open_dca", {"live_execution_enabled": True})
    gates = {gate["name"]: gate for gate in reader.live_gate_summary(cfg, DAY)["gates"]}
    assert gates["stream_live_execution_enabled"]["state"] == "configured"
    assert "market_open" in gates["stream_live_execution_enabled"]["detail"]
