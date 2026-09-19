"""State & ledgers page reader regressions: lock-free reads, corrupt != absent, no leaks."""

import json
from datetime import date
from pathlib import Path

import pytest

import dca_console_state as state_module
from dca_console_reader import RootSpec
from dca_console_routes import walk_redact
from dca_console_state import (
    build_state_snapshot,
    ledger_view,
    maintenance_events,
    purchases_view,
    risk_view,
    strategy_state_view,
)

DAY = date(2026, 9, 8)
CONFIG = Path(__file__).resolve().parents[1] / "config"
ACCOUNT = "999000111"
# A synthetic broker ORDER id, reused by the fixture that plants it and the
# assertion that proves it is redacted, so the two cannot drift apart. Taken
# from the set test_config_invariants.py already recognises as synthetic order
# ids: the nine-digit shape this previously used is exactly the shape a real
# Robinhood account has, and the account-literal guard rightly refuses to
# allowlist any nine-digit value.
SETTLEMENT_REF = "order-4184502"


@pytest.fixture(autouse=True)
def no_torn_write_pause(monkeypatch):
    # The torn-write retry sleeps 250 ms in production; every corrupt-state
    # test would otherwise pay it twice.
    monkeypatch.setattr(state_module, "TORN_WRITE_RETRY_SECONDS", 0)


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def roots(tmp_path) -> tuple[RootSpec, RootSpec]:
    return RootSpec.of(tmp_path / "cron", "primary"), RootSpec.of(tmp_path / "artifacts", "secondary")


def cumulative_ledger(root: Path, day: str, **overrides):
    payload = {
        "version": 1, "trading_day": day,
        "pending_usd_by_ticker": {"AAPL": 1.25, "MSFT": 0.5},
        "events": [
            {"ticker": "AAPL", "stream": "morning", "recorded_at": f"{day}T10:30:00-04:00", "final_session": False,
             "pending_before_usd": 0.0, "incremental_credit_usd": 1.25, "order_amount_usd": 0.0,
             "pending_after_usd": 1.25, "decision": "pending"},
            {"ticker": "MSFT", "stream": "afternoon", "recorded_at": f"{day}T15:30:00-04:00", "final_session": True,
             "pending_before_usd": 3.0, "incremental_credit_usd": 2.0, "order_amount_usd": 5.0,
             "pending_after_usd": 0.0, "decision": "order"},
        ],
        "settled_order_ids": {"order-123456789": {"order_amount_usd": 5.0}},
    }
    payload.update(overrides)
    return write_json(root / "cumulative_dca_credits" / day / "ledger.json", payload)


def whole_share_ledger(root: Path, symbol: str, **overrides):
    payload = {
        "version": 1, "ticker": symbol, "pending_usd": 12.5, "shares_purchased_total": 2,
        "events": [
            {"trading_day": "2026-09-07", "stream": "afternoon", "recorded_at": "2026-09-07T15:30:00-04:00",
             "pending_before_usd": 60.0, "contribution_usd": 8.0, "current_share_price_usd": 55.5, "shares_to_buy": 1,
             "order_notional_usd": 55.5, "pending_after_usd": 12.5, "decision": "fire"},
            {"trading_day": "2026-09-08", "stream": "afternoon", "recorded_at": "2026-09-08T15:30:00-04:00",
             "shares_purchased": 1, "submitted_notional_usd": 55.5, "restored_shortfall_usd": 0.0,
             "shares_purchased_total": 2, "decision": "settled"},
        ],
        "recorded_idempotency_keys": {
            "scheduled_20260907_1530ET": {"resolved": False, "claimed_at": "2026-09-07T15:30:00-04:00",
                                          "claim_token": "deadbeefcafe", "shares_credited": False},
            "scheduled_20260908_1530ET": {"resolved": True, "claimed_at": "2026-09-08T15:30:00-04:00",
                                          "claim_token": "feedface0000", "shares_credited": True},
        },
    }
    payload.update(overrides)
    return write_json(root / "whole_share_accumulation" / symbol / "ledger.json", payload)


def tracker(root: Path, symbol: str, **overrides):
    payload = {
        "version": 1, "ticker": symbol, "status": "dipping", "accumulated_usd": 40.0,
        "cycle_start_trading_day": "2026-09-01", "cycle_reference_close": 100.0, "lowest_drop_seen": -0.06,
        "crossed_tier_thresholds": [0.03, 0.05], "trading_days_active": 5, "fire_attempts": 0,
        "settlement_pending": False, "settlement_ref_id": SETTLEMENT_REF, "settlement_account": ACCOUNT,
        "events": [
            {"status_before": "none", "status_after": "dipping", "accumulated_usd_before": 0.0,
             "accumulated_usd_after": 20.0, "newly_crossed_tiers": [0.03], "fired": False, "fire_amount_usd": 0.0,
             "reason": "tier_crossed"},
            {"status_before": "dipping", "status_after": "dipping", "accumulated_usd_before": 20.0,
             "accumulated_usd_after": 40.0, "newly_crossed_tiers": [0.05], "fired": False, "fire_amount_usd": 0.0,
             "reason": "tier_crossed", "recovery_metadata": {"claim_run_id": "scheduled_20260905_1530ET"}},
        ],
    }
    payload.update(overrides)
    return write_json(root / "inverse_martingale_dca" / symbol / "tracker.json", payload)


def strategy_state(path: Path, **overrides):
    payload = {
        "schema_version": 3,
        "buys_by_ticker_by_day": {"AAPL": {"2026-09-08": 2}, "bad symbol": {"2026-09-08": 1}},
        "submitted_notional_by_day": {"2026-09-08": 25.0, "2026-09-05": 10.0},
        "submitted_notional_by_week": {"2026-W37": 35.0},
        "submitted_order_count_by_day": {"2026-09-08": 2},
        "recorded_attempt_order_ids": {"AAPL": ["order-abc123"]},
        "notional_applied_order_ids": {"AAPL": ["order-abc123"]},
        "weekly_rollover_reserve_usd": 4.5, "reserve_week_key": "2026-W37",
        "finalized_budget_days": ["2026-09-05", "2026-09-04"], "selected_tier": {"name": "T2"},
    }
    payload.update(overrides)
    return write_json(path, payload)


def run_state(root: Path, relative: str, **updates) -> Path:
    from scripts.run_classification import build_scheduled_classification

    state = {
        "trading_day": DAY.isoformat(), "generated_at": DAY.isoformat() + "T15:35:00-04:00",
        "status": "completed", "dry_run": False,
        "run_classification": build_scheduled_classification(DAY.isoformat()),
        "buy_candidates": [{"ticker": "AAPL", "planned_amount_usd": 40.0, "status": "submitted"}],
    }
    state.update(updates)
    folder = write_json(root / relative / "run_state.json", state).parent
    # The purchase reader deduplicates by order id across roots, so each run
    # directory gets its own.
    (folder / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        f"AAPL,order-example-{relative[-13:-6]},40,filled,10,4\n", encoding="utf-8")
    return folder


def maintenance(root: Path, day: str, tmp_path: Path):
    write_json(root / "ticker_history_update" / day / "ticker_update_event.json", {
        "event_type": "ticker_history_update", "status": "partial", "completed_at": f"{day}T17:45:01-04:00",
        "invocations": 40, "updated_total": 4074, "remaining": 1395, "errors": [], "changed_tickers": ["AAPL", "MSFT"],
        "changed_active_tickers": ["AAPL"], "execution_owner": "claude_only",
        "breadth_fetch": {"status": "failed", "detail": f"exited 1 under {tmp_path}"},
    })
    write_json(root / "per_ticker_calibration" / day / "calibration_run.json", {
        "calibrated_tickers": 67, "fallback_tickers": ["XYZ"], "history_dir": str(tmp_path / "ticker_data"),
        "output": "config/per_ticker_sizing.json", "run_at": f"{day}T19:40:49-04:00", "status": "completed",
        "trigger_source": "standalone", "triggering_tickers": [], "upstream_status": "partial",
        "upstream_ticker_update_event": str(tmp_path / "cron" / "ticker_history_update" / day / "x.json"),
        "account": ACCOUNT,
    })


# --- strategy_state_view ----------------------------------------------------

def test_strategy_state_absent_is_distinct_from_corrupt(tmp_path):
    result = strategy_state_view(tmp_path / "strategy_state.json")
    assert result["status"] == "absent"
    assert result["fields"] == {}
    assert result["modified"] is None
    assert result["lock_file_present"] is False
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize("payload", [
    "{", '{"a": 1, "a": 2}', "[1, 2]", '"text"',
])
def test_strategy_state_corrupt_variants_raise_the_alarm(tmp_path, payload):
    path = tmp_path / "strategy_state.json"
    path.write_text(payload, encoding="utf-8")
    result = strategy_state_view(path)
    assert result["status"] == "corrupt"
    assert result["fields"] == {}
    assert any("corrupt" in warning for warning in result["warnings"])


def test_strategy_state_available_fields_are_allowlisted(tmp_path):
    path = strategy_state(tmp_path / "strategy_state.json", weekly_rollover_reserve_usd=float("nan"))
    path.with_name("strategy_state.json.lock").write_text("", encoding="utf-8")
    result = strategy_state_view(path)
    fields = result["fields"]
    assert result["status"] == "available"
    assert result["lock_file_present"] is True
    assert result["modified"] is not None
    assert fields["submitted_notional_by_day"] == {"2026-09-08": 25.0, "2026-09-05": 10.0}
    assert list(fields["submitted_notional_by_day"]) == ["2026-09-08", "2026-09-05"]
    assert fields["buys_by_ticker_by_day"] == {"AAPL": {"2026-09-08": 2.0}}
    assert fields["weekly_rollover_reserve_usd"] is None
    assert fields["finalized_budget_days"] == ["2026-09-05", "2026-09-04"]
    assert fields["selected_tier"] == {"name": "T2"}
    assert "recorded_attempt_order_ids" in fields["omitted_keys"]
    assert "order-abc123" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)


def test_strategy_state_retries_once_after_a_torn_write(tmp_path, monkeypatch):
    path = strategy_state(tmp_path / "strategy_state.json")
    reads = [b'{"schema_version": 3, "submitted_notional_by_da', path.read_bytes()]
    monkeypatch.setattr(state_module, "_raw_bytes", lambda _path: reads.pop(0))
    result = strategy_state_view(path)
    assert result["status"] == "available"
    assert reads == []


def test_strategy_state_default_path_resolves_env_at_call_time(tmp_path, monkeypatch):
    root = tmp_path / "relocated"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(root))
    strategy_state(root / "dca_runner" / "strategy_state.json")
    result = strategy_state_view()
    assert result["status"] == "available"
    assert result["path_label"].startswith("strategy_state.json (")
    assert str(root) not in json.dumps(result)


# --- ledger_view ------------------------------------------------------------

def test_ledger_view_cumulative_reads_newest_day_and_filters_ticker(tmp_path):
    primary, secondary = roots(tmp_path)
    cumulative_ledger(primary.path, "2026-09-07")
    cumulative_ledger(primary.path, "2026-09-08")
    (primary.path / "cumulative_dca_credits" / "2026-09-08" / "ledger.lock").write_text("", encoding="utf-8")
    (primary.path / "cumulative_dca_credits" / "not-a-date").mkdir()
    result = ledger_view([primary, secondary], "cumulative")
    assert result["days"] == ["2026-09-08", "2026-09-07"]
    entry = result["entries"][0]
    assert entry["day"] == "2026-09-08" and entry["status"] == "available"
    assert entry["lock_file_present"] is True
    assert entry["pending_total_usd"] == 1.75
    assert [event["decision"] for event in entry["events"]] == ["order", "pending"]
    assert entry["settled_order_count"] == 1
    assert "order-123456789" not in json.dumps(result)
    filtered = ledger_view([primary], "cumulative", ticker="MSFT", day="2026-09-07")
    assert filtered["entries"][0]["pending_usd_by_ticker"] == {"MSFT": 0.5}
    assert [event["ticker"] for event in filtered["entries"][0]["events"]] == ["MSFT"]


def test_ledger_view_cumulative_absent_corrupt_and_mismatched_days(tmp_path):
    primary, _ = roots(tmp_path)
    folder = primary.path / "cumulative_dca_credits"
    write_json(folder / "2026-09-06" / "ledger.json", {"version": 1, "trading_day": "2026-09-05",
                                                        "pending_usd_by_ticker": {}, "events": []})
    (folder / "2026-09-07").mkdir(parents=True)
    (folder / "2026-09-07" / "ledger.json").write_text("{not json", encoding="utf-8")
    statuses = {day: ledger_view([primary], "cumulative", day=day)["entries"][0]["status"]
                for day in ("2026-09-05", "2026-09-06", "2026-09-07")}
    assert statuses == {"2026-09-05": "absent", "2026-09-06": "mismatched", "2026-09-07": "corrupt"}
    corrupt = ledger_view([primary], "cumulative", day="2026-09-07")
    assert corrupt["entries"][0]["pending_total_usd"] is None
    assert any("corrupt" in warning for warning in corrupt["warnings"])


def test_ledger_view_whole_share_claims_and_day_filter(tmp_path):
    primary, _ = roots(tmp_path)
    whole_share_ledger(primary.path, "BYDDY")
    whole_share_ledger(primary.path, "MISMA", ticker="OTHER")
    (primary.path / "whole_share_accumulation" / "lower").mkdir()
    result = ledger_view([primary], "whole_share")
    assert result["tickers"] == ["BYDDY", "MISMA"]
    by_ticker = {entry["ticker"]: entry for entry in result["entries"]}
    assert by_ticker["BYDDY"]["pending_usd"] == 12.5
    assert [event["decision"] for event in by_ticker["BYDDY"]["events"]] == ["settled", "fire"]
    assert by_ticker["BYDDY"]["unresolved_claim_count"] == 1
    assert by_ticker["MISMA"]["status"] == "mismatched"
    assert "deadbeefcafe" not in json.dumps(result)
    assert "scheduled_20260907_1530ET" not in json.dumps(result)
    one_day = ledger_view([primary], "whole_share", ticker="BYDDY", day="2026-09-08")
    assert [event["decision"] for event in one_day["entries"][0]["events"]] == ["settled"]
    assert one_day["entries"][0]["event_count"] == 1


def test_ledger_view_inverse_martingale_tracker_allowlist(tmp_path):
    primary, _ = roots(tmp_path)
    tracker(primary.path, "NVDA")
    (primary.path / "inverse_martingale_dca" / "AMD").mkdir(parents=True)
    result = ledger_view([primary], "inverse_martingale")
    assert result["tickers"] == ["AMD", "NVDA"]
    by_ticker = {entry["ticker"]: entry for entry in result["entries"]}
    assert by_ticker["AMD"]["status"] == "absent" and by_ticker["AMD"]["tracker"] is None
    nvda = by_ticker["NVDA"]
    assert nvda["tracker"]["status"] == "dipping"
    assert nvda["tracker"]["accumulated_usd"] == 40.0
    assert "settlement_ref_id" not in nvda["tracker"] and "settlement_account" not in nvda["tracker"]
    assert [event["has_recovery_metadata"] for event in nvda["events"]] == [True, False]
    assert "scheduled_20260905_1530ET" not in json.dumps(result)
    assert SETTLEMENT_REF not in json.dumps(result)


@pytest.mark.parametrize("kwargs", [
    {"kind": "orders"}, {"kind": "whole_share", "ticker": "../x"}, {"kind": "whole_share", "ticker": "aapl"},
    {"kind": "cumulative", "day": "2026/09/08"},
])
def test_ledger_view_rejects_invalid_inputs(tmp_path, kwargs):
    primary, _ = roots(tmp_path)
    with pytest.raises(ValueError):
        ledger_view([primary], **kwargs)


def test_ledger_view_empty_roots_and_secondary_at_risk(tmp_path):
    primary, secondary = roots(tmp_path)
    for kind in ("cumulative", "whole_share", "inverse_martingale"):
        result = ledger_view([primary], kind)
        assert result["entries"] == [] and result["warnings"] == []
    assert any("No artifact root" in warning for warning in ledger_view([], "cumulative")["warnings"])
    whole_share_ledger(secondary.path, "BYDDY")
    result = ledger_view([primary, secondary], "whole_share")
    assert [(entry["root_label"], entry["at_risk"]) for entry in result["entries"]] == [(secondary.label, True)]
    assert result["entries"][0]["id"].startswith("run-") and len(result["entries"][0]["id"]) == 16


# --- purchases_view ---------------------------------------------------------

def test_purchases_view_records_rollup_and_day_filter(tmp_path):
    primary, secondary = roots(tmp_path)
    run_state(primary.path, "scheduled_runs/20260908_1530ET")
    run_state(secondary.path, "20260907_1530ET", trading_day="2026-09-07",
              generated_at="2026-09-07T15:35:00-04:00",
              run_classification={"run_origin": "manual", "manual_run": True})
    result = purchases_view([primary, secondary])
    assert result["summary"] == {"record_count": 2, "total_usd": 80.0, "last_trading_day": "2026-09-08"}
    assert [row["trading_day"] for row in result["records"]] == ["2026-09-08", "2026-09-07"]
    assert result["records"][0]["root_label"] == primary.label and result["records"][0]["at_risk"] is False
    assert result["records"][1]["at_risk"] is True
    assert result["records"][0]["price"] == 10 and result["records"][0]["quantity"] == 4
    assert result["rollup"]["AAPL"]["buy_count"] == 2
    assert result["rollup"]["AAPL"]["average_cost_basis_usd"] == 10
    dumped = json.dumps(result)
    assert "order-example" not in dumped and "order_id" not in dumped and "source_run_dir" not in dumped
    assert str(tmp_path) not in dumped
    one_day = purchases_view([primary, secondary], day="2026-09-07")
    assert one_day["summary"]["record_count"] == 1
    assert purchases_view([primary], day="2026-01-01")["summary"]["total_usd"] is None
    assert any("No artifact root" in warning for warning in purchases_view([])["warnings"])


# --- risk_view --------------------------------------------------------------

def test_risk_view_is_unusable_without_evidence(tmp_path):
    primary, secondary = roots(tmp_path)
    result = risk_view([primary, secondary], DAY, config_dir=CONFIG)
    assert result["usable"] is False
    assert result["evidence"]["state_root_exists"] is False
    assert result["evidence"]["afternoon_states_seen"] == 0
    assert result["report"]["scheduled_live"]["deployed_last_7d_usd"] == 0.0
    assert result["scope"] == {"root_label": primary.label, "ignored_roots": [secondary.label],
                               "note": result["scope"]["note"]}
    failed = [warning for warning in result["warnings"] if warning.startswith("Risk evidence check failed")]
    assert any("state_root_exists" in warning for warning in failed)
    assert any("afternoon_states_seen" in warning for warning in failed)
    assert not any("run_states_unreadable" in warning for warning in failed)
    assert str(tmp_path) not in json.dumps(result)


def test_risk_view_usable_with_afternoon_evidence(tmp_path):
    primary, _ = roots(tmp_path)
    run_state(primary.path, "scheduled_runs/20260908_1530ET")
    result = risk_view([primary], DAY, config_dir=CONFIG)
    assert result["usable"] is True, result["warnings"]
    assert result["evidence"]["afternoon_states_seen"] == 1
    assert result["evidence"]["run_states_loaded"] == 1
    assert result["report"]["scheduled_live"]["deployed_last_7d_usd"] == 40.0
    assert result["report"]["scheduled_live"]["kill_switch_engaged"] is False
    assert result["report"]["scheduled_deployed_by_day"] == {"2026-09-08": 40.0}
    assert result["report"]["window_days"] == {"burn": 30, "kill_switch": 7, "evidence": 30}
    assert result["report"]["kill_switch_7d_threshold_usd"] > 0
    (primary.path / "scheduled_runs" / "20260908_1530ET" / "run_state.json").write_text("{", encoding="utf-8")
    broken = risk_view([primary], DAY, config_dir=CONFIG)
    assert broken["usable"] is False
    assert broken["evidence"]["run_states_unreadable"] == 1
    assert any("run_states_unreadable" in warning for warning in broken["warnings"])
    assert "unreadable_paths" not in broken["evidence"]


# --- maintenance_events -----------------------------------------------------

def test_maintenance_events_forwards_only_allowlisted_keys(tmp_path):
    primary, _ = roots(tmp_path)
    for day in ("2026-09-06", "2026-09-07", "2026-09-08"):
        maintenance(primary.path, day, tmp_path)
    (primary.path / "ticker_history_update" / "2026-09-05").mkdir()
    (primary.path / "ticker_history_update" / "2026-09-04" / "ticker_update_event.json").parent.mkdir()
    (primary.path / "ticker_history_update" / "2026-09-04" / "ticker_update_event.json").write_text("{", "utf-8")
    result = maintenance_events([primary], days=2)
    assert [row["day"] for row in result["ticker_history"]] == ["2026-09-08", "2026-09-07"]
    history = result["ticker_history"][0]
    assert history["status"] == "partial" and history["invocations"] == 40 and history["remaining"] == 1395
    assert history["changed_active_tickers_count"] == 1 and history["errors_count"] == 0
    assert history["breadth_status"] == "failed"
    calibration = result["calibration"][0]
    assert calibration["upstream_status"] == "partial" and calibration["calibrated_tickers"] == 67
    assert calibration["fallback_tickers_count"] == 1 and calibration["file"] == "calibration_run.json"
    assert "history_dir" not in calibration and "output" not in calibration
    everything = maintenance_events([primary], days=30)
    assert [row["file_status"] for row in everything["ticker_history"]][-1] == "corrupt"
    dumped = json.dumps(everything)
    assert str(tmp_path) not in dumped and ACCOUNT not in dumped
    assert maintenance_events([], days=30)["ticker_history"] == []


# --- build_state_snapshot ---------------------------------------------------

def test_build_state_snapshot_composes_summaries_without_events(tmp_path):
    primary, secondary = roots(tmp_path)
    cumulative_ledger(primary.path, "2026-09-08")
    whole_share_ledger(secondary.path, "BYDDY")
    tracker(primary.path, "NVDA")
    run_state(primary.path, "scheduled_runs/20260908_1530ET")
    maintenance(primary.path, "2026-09-08", tmp_path)
    state_path = strategy_state(tmp_path / "strategy_state.json")
    result = build_state_snapshot([primary, secondary], DAY, config_dir=CONFIG, state_path=state_path)
    assert result["strategy_state"]["status"] == "available"
    assert result["ledgers"]["cumulative"] == {"entries": 1, "listed": 1, "corrupt": 0, "lock_files_present": 0,
                                               "pending_total_usd": 1.75}
    assert result["ledgers"]["whole_share"]["pending_total_usd"] == 12.5
    assert result["ledgers"]["inverse_martingale"]["pending_total_usd"] == 40.0
    assert result["purchases"] == {"record_count": 1, "total_usd": 40.0, "last_trading_day": "2026-09-08"}
    assert result["risk"]["usable"] is True
    assert result["maintenance"] == {"ticker_history": {"day": "2026-09-08", "status": "partial"},
                                     "calibration": {"day": "2026-09-08", "status": "completed"}}
    dumped = json.dumps(result)
    assert "events" not in dumped and "records" not in dumped
    assert result["warnings"] == list(dict.fromkeys(result["warnings"]))
    empty = build_state_snapshot([], DAY, config_dir=CONFIG, state_path=tmp_path / "missing.json")
    assert empty["strategy_state"]["status"] == "absent" and empty["risk"]["usable"] is False
    assert empty["maintenance"] == {"ticker_history": None, "calibration": None}


# --- cross-cutting guarantees -----------------------------------------------

def test_no_account_number_or_absolute_path_leaks(tmp_path):
    primary, _ = roots(tmp_path)
    cumulative_ledger(primary.path, "2026-09-08", pending_usd_by_ticker={"AAPL": 1.0},
                      events=[{"ticker": "AAPL", "stream": f"acct {ACCOUNT}", "decision": "pending",
                               "recorded_at": str(tmp_path)}])
    tracker(primary.path, "NVDA", basis=f"account_{ACCOUNT}")
    maintenance(primary.path, "2026-09-08", tmp_path)
    run_state(primary.path, "scheduled_runs/20260908_1530ET", account_number=ACCOUNT)
    state_path = strategy_state(tmp_path / "strategy_state.json", selected_tier=f"tier {ACCOUNT}",
                                state_root=str(tmp_path))
    views = [
        strategy_state_view(state_path),
        *(ledger_view([primary], kind) for kind in ("cumulative", "whole_share", "inverse_martingale")),
        purchases_view([primary]), risk_view([primary], DAY, config_dir=CONFIG),
        maintenance_events([primary]),
        build_state_snapshot([primary], DAY, config_dir=CONFIG, state_path=state_path),
    ]
    for view in views:
        # Paths never enter the structures at all; the account shape is gone
        # once the route-level redaction has run (and, via _text, before it).
        assert str(tmp_path) not in json.dumps(view)
        assert ACCOUNT not in json.dumps(walk_redact(view))


def test_no_lock_is_ever_taken(tmp_path, monkeypatch):
    import dca_runner
    import scripts.file_lock as file_lock

    def refuse(*args, **kwargs):
        raise AssertionError("a console reader tried to take a lock")

    monkeypatch.setattr(file_lock, "cross_process_lock", refuse)
    monkeypatch.setattr(dca_runner, "state_transaction", refuse)
    monkeypatch.setattr(dca_runner, "load_strategy_state", refuse)
    monkeypatch.setattr(dca_runner, "_acquire_exclusive_lock", refuse)
    primary, secondary = roots(tmp_path)
    cumulative_ledger(primary.path, "2026-09-08")
    whole_share_ledger(primary.path, "BYDDY")
    tracker(secondary.path, "NVDA")
    run_state(primary.path, "scheduled_runs/20260908_1530ET")
    maintenance(primary.path, "2026-09-08", tmp_path)
    state_path = strategy_state(tmp_path / "strategy_state.json")
    state_path.with_name("strategy_state.json.lock").write_text("", encoding="utf-8")
    (primary.path / "cumulative_dca_credits" / "2026-09-08" / "ledger.lock").write_text("", encoding="utf-8")
    all_roots = [primary, secondary]
    assert strategy_state_view(state_path)["lock_file_present"] is True
    for kind in ("cumulative", "whole_share", "inverse_martingale"):
        assert ledger_view(all_roots, kind)["entries"]
    assert purchases_view(all_roots)["summary"]["record_count"] == 1
    assert risk_view(all_roots, DAY, config_dir=CONFIG)["usable"] is True
    assert maintenance_events(all_roots)["calibration"]
    snapshot = build_state_snapshot(all_roots, DAY, config_dir=CONFIG, state_path=state_path)
    assert snapshot["ledgers"]["cumulative"]["lock_files_present"] == 1
