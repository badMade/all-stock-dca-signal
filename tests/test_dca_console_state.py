"""State & ledgers readers: lock-free, absent/corrupt/available kept apart, nothing path-shaped leaks."""

import json
from datetime import date
from pathlib import Path

import pytest

import dca_console_state as state_mod
from dca_console_reader import RootSpec
from dca_console_routes import walk_redact
from dca_console_state import (
    build_state_snapshot,
    default_state_path,
    ledger_view,
    maintenance_events,
    purchases_view,
    risk_view,
    strategy_state_view,
)

DAY = date(2026, 9, 8)
CONFIG = Path(__file__).resolve().parents[1] / "config"
ACCOUNT = "999000111"  # the conftest synthetic account; never a real one
STATE = Path("dca_runner") / "strategy_state.json"


@pytest.fixture(autouse=True)
def no_torn_write_pause(monkeypatch):
    # The retry pause is real in production; a test suite should not sleep for it.
    monkeypatch.setattr(state_mod, "TORN_WRITE_RETRY_SECONDS", 0)


@pytest.fixture
def roots(tmp_path):
    primary = tmp_path / "cron_tracking"
    primary.mkdir()
    extra = tmp_path / "artifacts"
    extra.mkdir()
    return (RootSpec.of(primary, "primary"), RootSpec.of(extra, "secondary"))


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def strategy_state_payload(**over):
    payload = {
        "schema_version": 3,
        "buys_by_ticker_by_day": {"NVDA": {"2026-09-04": 2, "2026-09-08": 1}},
        "submitted_notional_by_day": {"2026-09-04": 12.5, "2026-09-08": 7.25},
        "submitted_notional_by_week": {"2026-W37": 19.75},
        "submitted_order_count_by_day": {"2026-09-08": 1},
        "weekly_rollover_reserve_usd": 3.5,
        "reserve_week_key": "2026-W37",
        "finalized_budget_days": ["2026-09-03", "2026-09-04"],
        "recorded_attempt_order_ids": {"2026-09-08": ["order-abc"]},
        "selected_tier": {"name": "deep", "multiplier": 2.0},
    }
    payload.update(over)
    return payload


def cumulative_payload(day, **over):
    payload = {
        "trading_day": day,
        "pending_usd_by_ticker": {"NVDA": 0.6, "AAPL": 0.25},
        "settled_order_ids": {"order-1": {"ticker": "AAPL"}},
        "events": [
            {"ticker": "NVDA", "stream": "morning", "recorded_at": f"{day}T10:31:00-04:00", "final_session": False,
             "pending_before_usd": 0.0, "incremental_credit_usd": 0.6, "order_amount_usd": 0.0,
             "pending_after_usd": 0.6, "decision": "pending", "order_id": "order-0"},
            {"ticker": "AAPL", "stream": "afternoon", "recorded_at": f"{day}T15:31:00-04:00", "final_session": True,
             "pending_before_usd": 1.25, "incremental_credit_usd": 0.0, "order_amount_usd": 1.0,
             "pending_after_usd": 0.25, "decision": "order", "order_id": "order-1"},
        ],
    }
    payload.update(over)
    return payload


def whole_share_payload(**over):
    payload = {
        "ticker": "BYDDY", "pending_usd": 42.1, "shares_purchased_total": 3,
        "recorded_idempotency_keys": {
            "20260908_1530ET:BYDDY:tok-secret": {"resolved": False, "claimed_at": "2026-09-08T15:31:00-04:00",
                                                 "shares_credited": False, "claim_token": "tok-secret"},
            "20260904_1530ET:BYDDY:tok-older": {"resolved": True, "claimed_at": "2026-09-04T15:31:00-04:00",
                                                "shares_credited": True, "claim_token": "tok-older"},
        },
        "events": [
            {"trading_day": "2026-09-04", "stream": "afternoon", "recorded_at": "2026-09-04T15:31:00-04:00",
             "pending_before_usd": 10.0, "contribution_usd": 5.0, "pending_after_usd": 15.0, "decision": "pending",
             "order_id": "order-4"},
            {"trading_day": "2026-09-08", "stream": "afternoon", "recorded_at": "2026-09-08T15:31:00-04:00",
             "pending_before_usd": 60.0, "contribution_usd": 5.0, "current_share_price_usd": 22.9, "shares_to_buy": 1,
             "order_notional_usd": 22.9, "pending_after_usd": 42.1, "decision": "fire", "order_id": "order-8"},
        ],
    }
    payload.update(over)
    return payload


def tracker_payload(**over):
    payload = {
        "version": 1, "ticker": "NVDA", "status": "dipping", "accumulated_usd": 18.0,
        "cycle_start_trading_day": "2026-09-02", "settlement_pending": True,
        "settlement_ref_id": "order-secret", "settlement_account": ACCOUNT,
        "events": [
            {"status_before": "none", "status_after": "dipping", "accumulated_usd_before": 0.0,
             "accumulated_usd_after": 9.0, "newly_crossed_tiers": [1], "fired": False, "fire_amount_usd": 0.0,
             "reason": "crossed tier 1"},
            {"status_before": "dipping", "status_after": "dipping", "accumulated_usd_before": 9.0,
             "accumulated_usd_after": 18.0, "newly_crossed_tiers": [2], "fired": False, "fire_amount_usd": 0.0,
             "reason": "recovered claim", "recovery_metadata": {"order_id": "order-secret"}},
        ],
    }
    payload.update(over)
    return payload


def seed_maintenance(root, day, tmp_path, status="completed"):
    write(root / "ticker_history_update" / day / "ticker_update_event.json", {
        "event_type": "ticker_history_update", "status": status, "completed_at": f"{day}T06:05:00-04:00",
        "invocations": 2, "updated_total": 480, "remaining": 0, "errors": [str(tmp_path)],
        "changed_tickers": ["NVDA", "AAPL"], "changed_active_tickers": ["NVDA"], "execution_owner": "cron",
        "perplexity_involved": False, "breadth_fetch": {"status": "completed"}, "history_dir": str(tmp_path),
    })
    write(root / "per_ticker_calibration" / day / "calibration_run.json", {
        "calibrated_tickers": 12, "execution_owner": "cron", "fallback_tickers": [], "history_dir": str(tmp_path),
        "output": str(tmp_path / "out.json"), "run_at": f"{day}T06:20:00-04:00", "status": status,
        "trigger_source": "ticker_history_update", "triggering_tickers": ["NVDA"], "upstream_status": "partial",
        "upstream_ticker_update_event": str(tmp_path / "event.json"),
    })


def seed_purchase_run(root, *, name="morning_dca/20260908_1030ET", day="2026-09-08", order_id="order-example"):
    folder = root / name
    write(folder / "run_state.json", {
        "trading_day": day, "generated_at": f"{day}T10:30:00-04:00", "status": "completed", "dry_run": False,
        "counts": {"buy_candidates": 1, "submitted": 1},
        "run_classification": {"stream": "morning", "run_origin": "scheduled", "manual_run": False},
    })
    (folder / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        f"NVDA,{order_id},40,filled,10,2\n", encoding="utf-8")
    return folder


def seed_afternoon_state(root, day="2026-09-08", deployed=25.0):
    from scripts.run_classification import build_scheduled_classification

    write(root / "scheduled_runs" / f"{day.replace('-', '')}_1530ET" / "run_state.json", {
        "trading_day": day, "generated_at": f"{day}T15:20:00-04:00",
        "run_classification": build_scheduled_classification(day),
        "buy_candidates": [{"ticker": "AAA", "planned_amount_usd": deployed, "status": "submitted"}],
    })


def seed_everything(roots, tmp_path):
    primary, extra = roots[0].path, roots[1].path
    write(primary / STATE, strategy_state_payload())
    (primary / STATE).with_name("strategy_state.json.lock").write_text("")
    write(primary / "cumulative_dca_credits" / "2026-09-04" / "ledger.json", cumulative_payload("2026-09-04"))
    write(primary / "cumulative_dca_credits" / "2026-09-08" / "ledger.json", cumulative_payload("2026-09-08"))
    write(primary / "whole_share_accumulation" / "BYDDY" / "ledger.json", whole_share_payload())
    write(primary / "inverse_martingale_dca" / "NVDA" / "tracker.json", tracker_payload())
    write(extra / "inverse_martingale_dca" / "AAPL" / "tracker.json", tracker_payload(ticker="AAPL"))
    seed_maintenance(primary, "2026-09-07", tmp_path)
    seed_purchase_run(primary)
    seed_afternoon_state(primary)


def every_builder(roots):
    return [
        strategy_state_view(roots=roots),
        ledger_view(roots, "cumulative"),
        ledger_view(roots, "whole_share"),
        ledger_view(roots, "inverse_martingale"),
        purchases_view(roots),
        risk_view(roots, DAY, config_dir=CONFIG),
        maintenance_events(roots),
        build_state_snapshot(roots, DAY, config_dir=CONFIG),
    ]


# --- strategy_state_view ------------------------------------------------------

def test_strategy_state_absent_is_a_fresh_state_not_an_alarm(roots):
    view = strategy_state_view(roots=roots)
    assert view["status"] == "absent" and view["fields"] == {} and view["modified"] is None
    assert view["lock_file_present"] is False
    assert view["path_label"].startswith("strategy_state.json (")
    assert any("fresh state" in warning for warning in view["warnings"])


def test_strategy_state_forwards_only_the_allowlist_newest_first(roots):
    path = write(roots[0].path / STATE, strategy_state_payload())
    path.with_name("strategy_state.json.lock").write_text("")
    view = strategy_state_view(path)
    assert view["status"] == "available" and view["lock_file_present"] is True and view["warnings"] == []
    assert view["modified"] is not None and view["modified"].endswith(("-04:00", "-05:00"))
    fields = view["fields"]
    assert list(fields["submitted_notional_by_day"]) == ["2026-09-08", "2026-09-04"]
    assert fields["submitted_notional_by_day"]["2026-09-08"] == 7.25
    assert fields["submitted_order_count_by_day"] == {"2026-09-08": 1.0}
    assert fields["submitted_notional_by_week"] == {"2026-W37": 19.75}
    assert fields["finalized_budget_days"] == ["2026-09-04", "2026-09-03"]
    assert fields["buys_by_ticker_by_day"] == {"NVDA": {"2026-09-08": 1.0, "2026-09-04": 2.0}}
    assert fields["weekly_rollover_reserve_usd"] == 3.5 and fields["reserve_week_key"] == "2026-W37"
    assert fields["selected_tier"] == {"name": "deep", "multiplier": 2.0}
    # The order-id map is hidden by the allowlist and named as such.
    assert fields["omitted_keys"] == ["recorded_attempt_order_ids"]
    assert "order-abc" not in json.dumps(view)


@pytest.mark.parametrize("payload", ['{"schema_version": 3, "schema_version": 4}', "[1, 2]", "{not json", ""])
def test_strategy_state_corrupt_is_an_alarm_distinct_from_absent(roots, payload):
    view = strategy_state_view(write(roots[0].path / STATE, payload))
    assert view["status"] == "corrupt" and view["fields"] == {}
    assert view["modified"] is not None  # the file exists; absent would be None
    assert any("refuse to run" in warning for warning in view["warnings"])


def test_strategy_state_oversized_file_is_corrupt_not_partially_read(roots, monkeypatch):
    monkeypatch.setattr(state_mod, "MAX_BYTES", 16)
    view = strategy_state_view(write(roots[0].path / STATE, strategy_state_payload()))
    assert view["status"] == "corrupt"


def test_strategy_state_retries_once_after_a_torn_write(roots, monkeypatch):
    path = write(roots[0].path / STATE, '{"schema_version": 3, "weekly_')
    pauses = []

    def heal(seconds):
        pauses.append(seconds)
        path.write_text(json.dumps(strategy_state_payload()), encoding="utf-8")
    monkeypatch.setattr(state_mod.time, "sleep", heal)
    view = strategy_state_view(path)
    assert view["status"] == "available" and pauses == [0]
    # A second failure after the pause is real corruption: exactly one retry.
    path.write_text("{torn", encoding="utf-8")
    monkeypatch.setattr(state_mod.time, "sleep", lambda seconds: pauses.append(seconds))
    assert strategy_state_view(path)["status"] == "corrupt" and len(pauses) == 2


def test_strategy_state_reads_the_primary_root_not_the_runner_default(roots, tmp_path, monkeypatch):
    # Adversarial review round 2: a demo/--root console showed production's
    # budget figures because the default came from $DCA_CRON_TRACKING_ROOT.
    import dca_runner

    other = tmp_path / "production" / "dca_runner" / "strategy_state.json"
    write(other, strategy_state_payload(weekly_rollover_reserve_usd=99.0))
    write(roots[0].path / STATE, strategy_state_payload(weekly_rollover_reserve_usd=1.0))
    monkeypatch.setattr(dca_runner, "_default_state_path", lambda: other)
    assert default_state_path(roots) == roots[0].path / STATE
    assert strategy_state_view(roots=roots)["fields"]["weekly_rollover_reserve_usd"] == 1.0
    snapshot = build_state_snapshot(roots, DAY, config_dir=CONFIG)
    assert snapshot["strategy_state"]["fields"]["weekly_rollover_reserve_usd"] == 1.0
    # Only with no root at all does dca_runner's default (resolved at call time) apply.
    assert default_state_path(()) == other
    assert strategy_state_view()["fields"]["weekly_rollover_reserve_usd"] == 99.0


def test_strategy_state_hidden_ticker_keys_are_counted_not_silently_dropped(roots, monkeypatch):
    buys = {"BRK-B": {"2026-09-08": 1}, "NVDA": {"2026-09-08": 2}, "AAPL": {"2026-09-08": 3}}
    view = strategy_state_view(write(roots[0].path / STATE, strategy_state_payload(buys_by_ticker_by_day=buys)))
    assert set(view["fields"]["buys_by_ticker_by_day"]) == {"NVDA", "AAPL"}
    assert any("1 ticker key(s) in buys_by_ticker_by_day were hidden" in warning for warning in view["warnings"])
    assert "BRK-B" not in json.dumps(view["warnings"])
    monkeypatch.setattr(state_mod, "MAX_ROWS", 1)
    view = strategy_state_view(roots[0].path / STATE)
    assert view["fields"]["buys_by_ticker_by_day"] == {"AAPL": {"2026-09-08": 3.0}}
    assert any("2 ticker key(s)" in warning for warning in view["warnings"])


# --- ledger_view --------------------------------------------------------------

def test_cumulative_ledger_lists_days_newest_first_and_reads_the_file_directly(roots):
    primary = roots[0].path
    write(primary / "cumulative_dca_credits" / "2026-09-04" / "ledger.json", cumulative_payload("2026-09-04"))
    write(primary / "cumulative_dca_credits" / "2026-09-08" / "ledger.json", cumulative_payload("2026-09-08"))
    (primary / "cumulative_dca_credits" / "2026-09-08" / "ledger.lock").write_text("")
    (primary / "cumulative_dca_credits" / "not-a-day").mkdir()
    view = ledger_view(roots, "cumulative")
    assert view["kind"] == "cumulative" and view["directory"] == "cumulative_dca_credits"
    assert view["days"] == ["2026-09-08", "2026-09-04"] and view["roots"] == [root.label for root in roots]
    # Without a day each root reads its own newest ledger; a root that lists
    # none contributes no entry at all rather than an invented absent one.
    assert len(view["entries"]) == 1
    primary_entry = view["entries"][0]
    assert primary_entry["day"] == "2026-09-08" and primary_entry["status"] == "available"
    assert primary_entry["id"].startswith("run-") and len(primary_entry["id"]) == 16
    assert primary_entry["lock_file_present"] is True and primary_entry["at_risk"] is False
    assert primary_entry["pending_usd_by_ticker"] == {"AAPL": 0.25, "NVDA": 0.6}
    assert primary_entry["pending_total_usd"] == 0.85 and primary_entry["settled_order_count"] == 1
    assert [event["decision"] for event in primary_entry["events"]] == ["order", "pending"]
    assert primary_entry["event_count"] == 2 and "order_id" not in primary_entry["events"][0]
    assert primary_entry["events"][0]["recorded_at"] == "2026-09-08T15:31:00-04:00"
    # An explicit day asks every root: the secondary has no ledger for it --
    # absent, at risk, never zero.
    explicit = ledger_view(roots, "cumulative", day="2026-09-08")
    assert [entry["root_label"] for entry in explicit["entries"]] == [root.label for root in roots]
    secondary_entry = explicit["entries"][1]
    assert secondary_entry["status"] == "absent" and secondary_entry["at_risk"] is True
    assert secondary_entry["pending_total_usd"] is None and secondary_entry["events"] == []
    # An explicit day and a ticker filter narrow the map and the events without a warning.
    narrowed = ledger_view(roots, "cumulative", day="2026-09-04", ticker="NVDA")
    entry = narrowed["entries"][0]
    assert entry["day"] == "2026-09-04" and entry["pending_usd_by_ticker"] == {"NVDA": 0.6}
    assert entry["pending_total_usd"] == 0.6 and [event["ticker"] for event in entry["events"]] == ["NVDA"]
    assert narrowed["warnings"] == []
    assert "order-1" not in json.dumps(view)


def test_cumulative_ledger_corrupt_and_mismatched_are_kept_apart(roots):
    primary = roots[0].path
    write(primary / "cumulative_dca_credits" / "2026-09-08" / "ledger.json", "{broken")
    write(primary / "cumulative_dca_credits" / "2026-09-04" / "ledger.json", cumulative_payload("2026-09-03"))
    corrupt = ledger_view(roots, "cumulative", day="2026-09-08")
    assert corrupt["entries"][0]["status"] == "corrupt" and corrupt["entries"][0]["pending_total_usd"] is None
    assert any("ledger file is corrupt" in warning for warning in corrupt["warnings"])
    mismatched = ledger_view(roots, "cumulative", day="2026-09-04")
    assert mismatched["entries"][0]["status"] == "mismatched"
    assert mismatched["entries"][0]["pending_total_usd"] == 0.85  # the figures are still shown, flagged
    assert any("different trading day" in warning for warning in mismatched["warnings"])


def test_cumulative_pending_total_is_unavailable_when_a_key_is_hidden(roots, monkeypatch):
    # Adversarial review round 2: dca_cumulative applies no symbol pattern on
    # write, so a hidden key made a partial sum (or 0.0) pose as the balance.
    ledger = roots[0].path / "cumulative_dca_credits" / "2026-09-08" / "ledger.json"
    write(ledger, cumulative_payload("2026-09-08", pending_usd_by_ticker={"BRK-B": 0.4, "NVDA": 0.6}))
    view = ledger_view(roots, "cumulative", day="2026-09-08")
    entry = view["entries"][0]
    assert entry["status"] == "available" and entry["pending_usd_by_ticker"] == {"NVDA": 0.6}
    assert entry["pending_total_usd"] is None
    assert any("1 pending-credit key(s)" in warning for warning in view["warnings"])
    assert "BRK-B" not in json.dumps(view["warnings"])
    write(ledger, cumulative_payload("2026-09-08", pending_usd_by_ticker={"BRK-B": 0.4}))
    entry = ledger_view(roots, "cumulative", day="2026-09-08")["entries"][0]
    assert entry["pending_usd_by_ticker"] == {} and entry["pending_total_usd"] is None
    snapshot = build_state_snapshot(roots, DAY, config_dir=CONFIG)
    assert snapshot["ledgers"]["cumulative"]["pending_total_usd"] is None
    assert any("pending-credit key" in warning for warning in snapshot["warnings"])
    # Truncation past the row limit is a hidden key too.
    write(ledger, cumulative_payload("2026-09-08"))
    monkeypatch.setattr(state_mod, "MAX_ROWS", 1)
    entry = ledger_view(roots, "cumulative", day="2026-09-08")["entries"][0]
    assert entry["pending_usd_by_ticker"] == {"AAPL": 0.25} and entry["pending_total_usd"] is None


def test_whole_share_ledger_forwards_claims_without_tokens(roots):
    primary = roots[0].path
    write(primary / "whole_share_accumulation" / "BYDDY" / "ledger.json", whole_share_payload())
    (primary / "whole_share_accumulation" / "brk-b").mkdir()
    view = ledger_view(roots, "whole_share")
    assert view["tickers"] == ["BYDDY"] and len(view["entries"]) == 1
    entry = view["entries"][0]
    assert entry["ticker"] == "BYDDY" and entry["status"] == "available" and entry["lock_file_present"] is False
    assert entry["pending_usd"] == 42.1 and entry["shares_purchased_total"] == 3.0
    assert [event["decision"] for event in entry["events"]] == ["fire", "pending"] and entry["event_count"] == 2
    assert entry["claims"] == [
        {"resolved": False, "claimed_at": "2026-09-08T15:31:00-04:00", "shares_credited": False},
        {"resolved": True, "claimed_at": "2026-09-04T15:31:00-04:00", "shares_credited": True},
    ]
    assert entry["unresolved_claim_count"] == 1
    dumped = json.dumps(view)
    assert "tok-secret" not in dumped and "order-8" not in dumped and "20260908_1530ET" not in dumped
    filtered = ledger_view(roots, "whole_share", ticker="BYDDY", day="2026-09-04")["entries"][0]
    assert [event["trading_day"] for event in filtered["events"]] == ["2026-09-04"]
    write(primary / "whole_share_accumulation" / "BYDDY" / "ledger.json", whole_share_payload(ticker="OTHER"))
    mismatched = ledger_view(roots, "whole_share", ticker="BYDDY")
    assert mismatched["entries"][0]["status"] == "mismatched"
    assert any("different ticker" in warning for warning in mismatched["warnings"])


def test_inverse_martingale_tracker_allowlist_and_recovery_flag(roots):
    write(roots[0].path / "inverse_martingale_dca" / "NVDA" / "tracker.json", tracker_payload())
    (roots[0].path / "inverse_martingale_dca" / "NVDA" / "tracker.lock").write_text("")
    # A symbol directory whose tracker.json was unlinked is a resolved cycle.
    (roots[0].path / "inverse_martingale_dca" / "AMD").mkdir()
    view = ledger_view(roots, "inverse_martingale")
    assert view["tickers"] == ["AMD", "NVDA"]
    by_ticker = {(entry["ticker"], entry["root_label"]): entry for entry in view["entries"]}
    resolved = by_ticker[("AMD", roots[0].label)]
    assert resolved["status"] == "absent" and resolved["tracker"] is None and view["warnings"] == []
    entry = by_ticker[("NVDA", roots[0].label)]
    assert entry["status"] == "available" and entry["lock_file_present"] is True
    assert entry["tracker"]["status"] == "dipping" and entry["tracker"]["accumulated_usd"] == 18.0
    assert entry["tracker"]["settlement_pending"] is True
    assert "settlement_ref_id" not in entry["tracker"] and "settlement_account" not in entry["tracker"]
    assert [event["reason"] for event in entry["events"]] == ["recovered claim", "crossed tier 1"]
    assert [event["has_recovery_metadata"] for event in entry["events"]] == [True, False]
    assert entry["event_count"] == 2 and "recovery_metadata" not in entry["events"][0]
    assert "order-secret" not in json.dumps(view)


@pytest.mark.parametrize("payload", [{}, {"ticker": "NVDA"}, {"ticker": "NVDA", "events": "nope"},
                                     {"ticker": "NVDA", "events": [{"reason": "ok"}, "junk"]}])
def test_tracker_without_an_events_list_is_available_not_an_error(roots, payload):
    # Adversarial review round 2: state["events"] raised KeyError/TypeError
    # for a truncated-but-valid tracker and escaped as a 500.
    write(roots[0].path / "inverse_martingale_dca" / "NVDA" / "tracker.json", payload)
    entry = ledger_view(roots, "inverse_martingale", ticker="NVDA")["entries"][0]
    assert entry["status"] == "available" and entry["tracker"] is not None
    if isinstance(payload.get("events"), list):
        assert entry["events"] == [{"reason": "ok", "has_recovery_metadata": False}] and entry["event_count"] == 1
    else:
        assert entry["events"] == [] and entry["event_count"] is None
    snapshot = build_state_snapshot(roots, DAY, config_dir=CONFIG)
    assert snapshot["ledgers"]["inverse_martingale"] == {"entries": 1, "listed": 1, "corrupt": 0,
                                                         "lock_files_present": 0, "pending_total_usd": None}


@pytest.mark.parametrize("kwargs", [{"kind": "credits"}, {"kind": "cumulative", "ticker": "nvda"},
                                    {"kind": "whole_share", "ticker": "../x"}, {"kind": "whole_share", "ticker": "NVDA1"},
                                    {"kind": "cumulative", "day": "20260908"}, {"kind": "cumulative", "day": "../x"}])
def test_ledger_view_rejects_bad_inputs_before_any_path_is_built(roots, kwargs):
    with pytest.raises(ValueError):
        ledger_view(roots, **kwargs)


def test_ledgers_and_maintenance_without_roots_warn_instead_of_guessing():
    for kind in state_mod.LEDGER_KINDS:
        view = ledger_view((), kind)
        assert view["entries"] == [] and any("No artifact root" in warning for warning in view["warnings"])
    assert any("No artifact root" in warning for warning in maintenance_events(())["warnings"])
    assert any("No artifact root" in warning for warning in risk_view((), DAY, config_dir=CONFIG)["warnings"])


# --- purchases_view -----------------------------------------------------------

def test_purchases_view_carries_no_order_ids_and_counts_reader_evidence(roots):
    seed_purchase_run(roots[0].path)
    seed_purchase_run(roots[1].path, name="artifacts_run/20260904_1030ET", day="2026-09-04", order_id="order-two")
    write(roots[0].path / "midday_dca" / "broken" / "run_state.json", "{broken")
    view = purchases_view(roots)
    assert [record["trading_day"] for record in view["records"]] == ["2026-09-08", "2026-09-04"]
    record = view["records"][0]
    assert set(record) == {"trading_day", "ticker", "amount_usd", "price", "quantity", "stream", "origin",
                           "root_label", "at_risk"}
    assert record["ticker"] == "NVDA" and record["price"] == 10.0 and record["quantity"] == 2.0
    assert record["origin"] == "scheduled" and record["root_label"] == roots[0].label and record["at_risk"] is False
    assert view["records"][1]["root_label"] == roots[1].label and view["records"][1]["at_risk"] is True
    assert view["rollup"]["NVDA"]["buy_count"] == 2 and view["rollup"]["NVDA"]["total_quantity"] == 4.0
    assert view["summary"]["record_count"] == 2 and view["summary"]["last_trading_day"] == "2026-09-08"
    assert view["summary"]["total_usd"] == round(sum(record["amount_usd"] for record in view["records"]), 2)
    assert any("evidence warning(s)" in warning for warning in view["warnings"])
    dumped = json.dumps(view)
    assert "order-example" not in dumped and "order-two" not in dumped and "broken" not in dumped


def test_purchases_view_day_filter_and_empty_roots(roots):
    seed_purchase_run(roots[0].path)
    seed_purchase_run(roots[0].path, name="morning_dca/20260904_1030ET", day="2026-09-04", order_id="order-two")
    view = purchases_view(roots, day="2026-09-04")
    assert [record["trading_day"] for record in view["records"]] == ["2026-09-04"]
    assert view["summary"] == {"record_count": 1, "total_usd": view["records"][0]["amount_usd"],
                               "last_trading_day": "2026-09-04"}
    with pytest.raises(ValueError):
        purchases_view(roots, day="Sept 4")
    empty = purchases_view(())
    assert empty["records"] == [] and empty["rollup"] == {}
    assert empty["summary"] == {"record_count": 0, "total_usd": None, "last_trading_day": None}
    assert any("No artifact root" in warning for warning in empty["warnings"])


# --- risk_view ----------------------------------------------------------------

def test_risk_view_unusable_when_evidence_is_missing(roots, tmp_path):
    view = risk_view(roots, DAY, config_dir=CONFIG)
    assert view["usable"] is False
    assert view["evidence"]["state_root_exists"] is True and view["evidence"]["run_states_loaded"] == 0
    assert set(view["evidence"]) == set(state_mod.EVIDENCE_KEYS)
    # Non-gating like unusable_states_outside_window, but must still reach the
    # console so an operator can see files that left run_states_unclassifiable.
    assert view["evidence"]["unclassifiable_states_without_deployment"] == 0
    failing = [warning for warning in view["warnings"] if warning.startswith("Risk evidence check failed")]
    assert any("run_states_loaded > 0" in warning for warning in failing)
    assert any("afternoon_states_seen > 0" in warning for warning in failing)
    assert view["report"]["scheduled_live"]["kill_switch_engaged"] is False
    assert view["report"]["scheduled_deployed_by_day"] == {}
    assert view["scope"]["root_label"] == roots[0].label and view["scope"]["ignored_roots"] == [roots[1].label]
    missing = risk_view((RootSpec.of(tmp_path / "missing"),), DAY, config_dir=CONFIG)
    assert missing["evidence"]["state_root_exists"] is False
    assert any("state_root_exists is true" in warning for warning in missing["warnings"])


def test_risk_view_usable_over_the_primary_root_only(roots, monkeypatch):
    seed_afternoon_state(roots[1].path)  # a secondary root never feeds the kill switch
    view = risk_view(roots, DAY, config_dir=CONFIG)
    assert view["usable"] is False and view["report"]["scheduled_live"]["deployed_last_7d_usd"] == 0.0
    seed_afternoon_state(roots[0].path)
    view = risk_view(roots, DAY, config_dir=CONFIG)
    assert view["usable"] is True and view["warnings"] == []
    assert view["evidence"]["afternoon_states_seen"] == 1 and view["evidence"]["run_states_loaded"] == 1
    report = view["report"]
    assert report["as_of"] == "2026-09-08" and report["scheduled_live"]["deployed_last_7d_usd"] == 25.0
    assert report["scheduled_deployed_by_day"] == {"2026-09-08": 25.0}
    assert report["window_days"] == {"burn": 30, "kill_switch": 7, "evidence": 30}
    assert report["kill_switch_7d_threshold_usd"] > 0 and "scheduled" in report["deployed_last_7d_usd_by_stream"]
    assert report["scheduled_live"]["rolling_cash_burn"]["total_deployed_usd"] == 25.0
    import dca_config

    monkeypatch.setattr(dca_config, "load_strategy_config", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("x")))
    fallback = risk_view(roots, DAY, config_dir=CONFIG)
    assert fallback["usable"] is True and fallback["report"]["scheduled_live"]["deployed_last_7d_usd"] == 25.0
    assert any("built-in defaults" in warning for warning in fallback["warnings"])


# --- maintenance_events -------------------------------------------------------

def test_maintenance_events_summarise_allowlisted_keys_without_paths(roots, tmp_path):
    seed_maintenance(roots[0].path, "2026-09-07", tmp_path)
    seed_maintenance(roots[0].path, "2026-09-08", tmp_path, status="partial")
    seed_maintenance(roots[1].path, "2026-09-06", tmp_path)
    events = maintenance_events(roots)
    assert [row["day"] for row in events["ticker_history"]] == ["2026-09-08", "2026-09-07", "2026-09-06"]
    row = events["ticker_history"][0]
    assert row["file"] == "ticker_update_event.json" and row["file_status"] == "available"
    assert row["status"] == "partial" and row["invocations"] == 2 and row["updated_total"] == 480
    assert row["remaining"] == 0 and row["completed_at"] == "2026-09-08T06:05:00-04:00"
    assert row["errors_count"] == 1 and row["changed_tickers_count"] == 2 and row["changed_active_tickers_count"] == 1
    assert row["breadth_status"] == "completed" and "history_dir" not in row and "errors" not in row
    calibration = events["calibration"][0]
    assert calibration["upstream_status"] == "partial" and calibration["calibrated_tickers"] == 12
    assert calibration["triggering_tickers_count"] == 1 and calibration["fallback_tickers_count"] == 0
    assert "output" not in calibration and "upstream_ticker_update_event" not in calibration
    assert events["ticker_history"][2]["at_risk"] is True and events["ticker_history"][0]["at_risk"] is False
    assert str(tmp_path) not in json.dumps(events) and events["warnings"] == []
    # days is clamped to at least one newest directory per root.
    clamped = maintenance_events(roots, days=0)
    assert [row["day"] for row in clamped["ticker_history"]] == ["2026-09-08", "2026-09-06"]


def test_corrupt_maintenance_event_warns_and_reaches_the_snapshot(roots, tmp_path):
    # Adversarial review round 2: a corrupt newest event rendered as status
    # None ("unavailable") with no warning and no file_status on the card.
    seed_maintenance(roots[0].path, "2026-09-07", tmp_path)
    write(roots[0].path / "ticker_history_update" / "2026-09-08" / "ticker_update_event.json", "{not json")
    events = maintenance_events(roots)
    row = events["ticker_history"][0]
    assert row["day"] == "2026-09-08" and row["file_status"] == "corrupt" and "status" not in row
    assert any("maintenance event file is corrupt" in warning for warning in events["warnings"])
    snapshot = build_state_snapshot(roots, DAY, config_dir=CONFIG)
    assert snapshot["maintenance"]["ticker_history"] == {"day": "2026-09-08", "status": None, "file_status": "corrupt"}
    assert snapshot["maintenance"]["calibration"] == {"day": "2026-09-07", "status": "completed",
                                                      "file_status": "available"}
    assert any("maintenance event file is corrupt" in warning for warning in snapshot["warnings"])


# --- build_state_snapshot -----------------------------------------------------

def test_build_state_snapshot_has_counts_but_no_lists(roots, tmp_path):
    seed_everything(roots, tmp_path)
    snapshot = build_state_snapshot(roots, DAY, config_dir=CONFIG)
    assert snapshot["date"] == "2026-09-08"
    assert snapshot["strategy_state"]["status"] == "available" and snapshot["strategy_state"]["lock_file_present"]
    assert set(snapshot["ledgers"]) == set(state_mod.LEDGER_KINDS)
    # cumulative: the primary's newest day only (the secondary lists none);
    # inverse_martingale: NVDA under the primary plus AAPL under the secondary.
    assert snapshot["ledgers"]["cumulative"] == {"entries": 1, "listed": 2, "corrupt": 0, "lock_files_present": 0,
                                                 "pending_total_usd": 0.85}
    assert snapshot["ledgers"]["whole_share"]["pending_total_usd"] == 42.1
    assert snapshot["ledgers"]["inverse_martingale"] == {"entries": 2, "listed": 2, "corrupt": 0,
                                                         "lock_files_present": 0, "pending_total_usd": 36.0}
    assert set(snapshot["purchases"]) == {"record_count", "total_usd", "last_trading_day"}
    assert snapshot["purchases"]["record_count"] == 1
    assert snapshot["risk"]["usable"] is True
    assert snapshot["maintenance"]["ticker_history"]["file_status"] == "available"
    assert snapshot["warnings"] == list(dict.fromkeys(snapshot["warnings"]))

    def lists_of_dicts(value):
        if isinstance(value, dict):
            return any(lists_of_dicts(item) for item in value.values())
        return isinstance(value, list) and any(isinstance(item, dict) for item in value)
    assert not lists_of_dicts(snapshot)


def test_empty_snapshot_is_all_unavailable_never_zero(roots):
    snapshot = build_state_snapshot(roots, DAY, config_dir=CONFIG)
    assert snapshot["strategy_state"]["status"] == "absent"
    assert all(summary["pending_total_usd"] is None for summary in snapshot["ledgers"].values())
    assert snapshot["purchases"]["total_usd"] is None and snapshot["risk"]["usable"] is False
    assert snapshot["maintenance"] == {"ticker_history": None, "calibration": None}


# --- cross-cutting guarantees -------------------------------------------------


def test_no_account_number_or_absolute_path_reaches_the_payload(roots, tmp_path):
    seed_everything(roots, tmp_path)
    primary = roots[0].path
    leak = str(tmp_path)
    write(primary / STATE, strategy_state_payload(
        reserve_week_key=f"week {ACCOUNT}", recorded_attempt_order_ids={ACCOUNT: [leak]},
        buys_by_ticker_by_day={"NVDA": {"2026-09-08": 1}}))
    write(primary / "cumulative_dca_credits" / "2026-09-08" / "ledger.json", cumulative_payload(
        "2026-09-08", settled_order_ids={ACCOUNT: leak},
        events=[{"ticker": "NVDA", "stream": f"acct {ACCOUNT}", "recorded_at": leak, "decision": "order",
                 "order_id": leak}]))
    write(primary / "whole_share_accumulation" / "BYDDY" / "ledger.json", whole_share_payload(
        recorded_idempotency_keys={leak: {"resolved": False, "claimed_at": leak, "claim_token": ACCOUNT}}))
    write(primary / "inverse_martingale_dca" / "NVDA" / "tracker.json", tracker_payload(
        settlement_ref_id=leak, settlement_account=ACCOUNT, reason=leak,
        events=[{"reason": f"account {ACCOUNT}", "recovery_metadata": {"path": leak}}]))
    seed_purchase_run(primary, name="midday_dca/20260908_1230ET", order_id=ACCOUNT)
    for result in every_builder(roots):
        before = json.dumps(result)
        assert leak not in before
        after = json.dumps(walk_redact(result))
        assert ACCOUNT not in after and leak not in after


def test_builders_never_take_a_cross_process_lock_or_a_state_transaction(roots, tmp_path, monkeypatch):
    import dca_runner
    import scripts.file_lock

    def refuse(*args, **kwargs):
        raise AssertionError("a console reader took a lock")
    seed_everything(roots, tmp_path)
    monkeypatch.setattr(scripts.file_lock, "cross_process_lock", refuse)
    monkeypatch.setattr(dca_runner, "state_transaction", refuse)
    monkeypatch.setattr(dca_runner, "load_strategy_state", refuse)
    results = every_builder(roots)
    assert len(results) == 8 and all("warnings" in result for result in results)
    assert results[0]["status"] == "available" and results[0]["lock_file_present"] is True

def test_read_object_rejects_duplicate_keys(tmp_path):
    from dca_console_reader import BoundedReader
    from dca_console_state import _read_object

    (tmp_path / "dup.json").write_text('{"a": 1, "a": 2}')
    reader = BoundedReader(tmp_path, [])
    status, obj = _read_object(reader, "dup.json")
    assert status == "corrupt"
    assert obj is None
