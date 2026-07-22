"""Tests for scripts/run_classification.py.

Runnable with ``pytest scripts/tests`` or ``python scripts/tests/test_run_classification.py``.

Covers the run-classification contract that keeps scheduled live runs of task
ce16ba6b from being confused with manual runs:
  * classification block builders and validation
  * scheduled vs manual write paths and the latest_scheduled_run pointer
  * manual order-id collection and exclusion
  * scheduled-state selection and the narrow broker time-window fallback
"""

import json
import sys
import tempfile
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_classification import (  # noqa: E402
    ACCEPTED_STATE_SCHEMA_VERSIONS,
    FINAL_MANUAL_OVERRIDE_RUN_TYPE,
    ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
    ORDER_SUBMIT_STATUS_ERROR,
    RECOVERY_TRIGGER_REASONS,
    STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION_V2,
    build_final_manual_override_classification,
    build_manual_classification,
    build_raw_persistence_state,
    build_recovery_state,
    build_scheduled_classification,
    collect_manual_order_ids,
    exclude_manual_orders,
    historicals_batch_filename,
    incremental_top_up_amount,
    is_confirmation_blocked,
    is_manual_state,
    is_preauthorized_v2,
    is_scheduled_state,
    is_top_up_context_state,
    latest_scheduled_run_path,
    list_raw_inputs_saved,
    load_same_day_top_up_context,
    manual_exclusion_roots,
    manual_run_id,
    normalize_connector_payload,
    order_submit_status,
    place_filename,
    quotes_batch_filename,
    raw_input_dir,
    raw_persistence_failure_run_state,
    recovery_escalation_run_state,
    save_raw_input,
    scheduled_run_id,
    select_scheduled_state_for_reporting,
    validate_classification,
    write_run_artifacts,
)

TRADING_DAY = "2026-07-06"


def _scheduled_state(order_ids=("SCHED-1", "SCHED-2")):
    return {
        "run_id": "2026-07-06-152001",
        "generated_at": "2026-07-06T15:20:01",
        "trading_day": TRADING_DAY,
        "broker": "robinhood",
        "budget": {"budget_tier": "normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
        "counts": {"submitted": len(order_ids)},
        "run_classification": build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z"),
        "buy_candidates": [
            {"ticker": f"T{i}", "buy_multiplier": 1.0, "planned_amount_usd": 3.0,
             "status": "submitted", "order_id": oid}
            for i, oid in enumerate(order_ids)
        ],
    }


def _manual_state(order_ids=("MAN-9",), ts="2026-07-06T11:00:00Z"):
    return {
        "run_id": "2026-07-06-110000",
        "generated_at": "2026-07-06T11:00:00",
        "trading_day": TRADING_DAY,
        "broker": "robinhood",
        "budget": {"budget_tier": "normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
        "counts": {"submitted": len(order_ids)},
        "run_classification": build_manual_classification(ts),
        "buy_candidates": [
            {"ticker": f"M{i}", "buy_multiplier": 1.0, "planned_amount_usd": 3.0,
             "status": "submitted", "order_id": oid}
            for i, oid in enumerate(order_ids)
        ],
    }


def _confirmation_blocked_state():
    """Scheduled run whose review passed but submission was blocked awaiting
    confirmation: valid scheduled classification, order_submit_status set, no order_id."""
    return {
        "run_id": "2026-07-06-152001",
        "generated_at": "2026-07-06T15:20:01",
        "trading_day": TRADING_DAY,
        "broker": "robinhood",
        "budget": {"budget_tier": "normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
        "counts": {"submitted": 0, "buy_candidates": 1},
        "order_submit_status": ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
        "run_classification": build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z"),
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "buy_multiplier": 1.0,
                "planned_amount_usd": 3.0,
                "status": "not_submitted_confirmation_required",
                "order_id": None,
                "reviewed_order": {"notional_usd": 3.0, "side": "buy", "review_passed": True},
            }
        ],
    }


def test_run_id_patterns():
    assert scheduled_run_id(TRADING_DAY) == "scheduled_20260706_1520ET"
    mid = manual_run_id("2026-07-06T11:00:00Z")
    assert mid.startswith("manual_20260706_")
    assert "1520ET" not in mid


def test_scheduled_classification_fields():
    rc = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z")
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "scheduled_live_test"
    assert rc["cron_id"] == "10ce362c"
    assert rc["supersedes_cron_id"] == "ce16ba6b"
    assert rc["scheduled_for_et"] == "15:20"
    assert rc["scheduled_window_start_et"] == "15:15"
    assert rc["scheduled_window_end_et"] == "15:35"
    assert rc["manual_run"] is False
    assert rc["run_id"] == "scheduled_20260706_1520ET"
    assert rc["triggered_at_utc"] == "2026-07-06T19:20:01Z"
    assert rc["state_schema_version"] == STATE_SCHEMA_VERSION
    assert validate_classification(rc).ok


def test_manual_classification_fields():
    rc = build_manual_classification("2026-07-06T11:00:00Z")
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert rc["cron_id"] == "10ce362c"
    assert rc["supersedes_cron_id"] == "ce16ba6b"
    assert rc["state_schema_version"] == STATE_SCHEMA_VERSION
    assert not scheduled_run_id(TRADING_DAY) == rc["run_id"]
    assert validate_classification(rc).ok


def test_scheduled_valid_by_supersedes_only():
    # A continuity artifact whose cron_id is not the repaired id is still valid
    # scheduled evidence as long as it carries the superseded-task marker.
    rc = build_scheduled_classification(TRADING_DAY)
    rc["cron_id"] = "someothertask"
    assert rc["supersedes_cron_id"] == "ce16ba6b"
    assert validate_classification(rc).ok


def test_scheduled_valid_by_cron_id_only():
    # cron_id of the repaired task alone (no supersedes marker) is valid too.
    rc = build_scheduled_classification(TRADING_DAY)
    rc.pop("supersedes_cron_id")
    assert rc["cron_id"] == "10ce362c"
    assert validate_classification(rc).ok


def test_scheduled_rejected_without_lineage():
    # Neither the current cron_id nor the supersedes marker -> not valid.
    rc = build_scheduled_classification(TRADING_DAY)
    rc["cron_id"] = "deadbeef"
    rc.pop("supersedes_cron_id")
    res = validate_classification(rc)
    assert not res.ok
    assert any("supersedes_cron_id" in e for e in res.errors)


def test_validation_rejects_placeholder_supersedes():
    # A placeholder/ambiguous supersedes marker (e.g. 'ce16ba6b_replacement') is
    # forbidden even though the canonical cron_id is present.
    rc = build_scheduled_classification(TRADING_DAY)
    rc["supersedes_cron_id"] = "ce16ba6b_replacement"
    res = validate_classification(rc)
    assert not res.ok
    assert any("supersedes_cron_id" in e and "forbidden" in e for e in res.errors)

    manual = build_manual_classification()
    manual["supersedes_cron_id"] = "ce16ba6b_replacement"
    assert not validate_classification(manual).ok

    # A placeholder/ambiguous cron_id is also forbidden.
    rc_bad_cron = build_scheduled_classification(TRADING_DAY)
    rc_bad_cron["cron_id"] = "ce16ba6b_replacement"
    assert not validate_classification(rc_bad_cron).ok


def test_validation_rejects_mismatched_flag():
    rc = build_scheduled_classification(TRADING_DAY)
    rc["manual_run"] = True  # contradiction
    res = validate_classification(rc)
    assert not res.ok
    assert any("manual_run=false" in e for e in res.errors)


def test_validation_rejects_bad_schema_version():
    rc = build_manual_classification()
    rc["state_schema_version"] = "v0"
    res = validate_classification(rc)
    assert not res.ok
    assert any("state_schema_version" in e for e in res.errors)


def test_validation_accepts_v2_preauthorized_scheduled():
    # The stricter successor version is accepted for scheduled runs when the
    # required scheduled identity fields are correct.
    rc = build_scheduled_classification(TRADING_DAY)
    rc["state_schema_version"] = STATE_SCHEMA_VERSION_V2
    res = validate_classification(rc)
    assert res.ok
    assert STATE_SCHEMA_VERSION_V2 in ACCEPTED_STATE_SCHEMA_VERSIONS
    assert is_preauthorized_v2(rc)
    assert not is_preauthorized_v2(build_scheduled_classification(TRADING_DAY))


def test_validation_accepts_v2_preauthorized_manual():
    rc = build_manual_classification()
    rc["state_schema_version"] = STATE_SCHEMA_VERSION_V2
    assert validate_classification(rc).ok
    assert is_preauthorized_v2({"run_classification": rc})


def test_validation_rejects_manual_run_id_matching_scheduled():
    rc = build_manual_classification()
    rc["run_id"] = "scheduled_20260706_1520ET"
    res = validate_classification(rc)
    assert not res.ok


def test_validation_rejects_non_string_scheduled_run_id():
    rc = build_scheduled_classification(TRADING_DAY)
    rc["run_id"] = 20260706  # non-string sneaks past the required-field presence check
    res = validate_classification(rc)
    assert not res.ok
    assert any("run_id must be a string" in e for e in res.errors)


def test_validation_rejects_manual_for_other_cron_id():
    rc = build_manual_classification()
    rc["cron_id"] = "deadbeef"
    rc.pop("supersedes_cron_id", None)
    res = validate_classification(rc)
    assert not res.ok
    assert any("cron_id" in e and "supersedes_cron_id" in e for e in res.errors)


def test_validation_manual_accepts_lineage_by_supersedes_only():
    rc = build_manual_classification()
    rc["cron_id"] = "someothertask"
    assert rc["supersedes_cron_id"] == "ce16ba6b"
    assert validate_classification(rc).ok


def test_collect_manual_ids_reads_referenced_orders_csv():
    # Manual run in the run_and_report style: order IDs live only in the CSV named
    # by artifacts.orders_csv_path, not embedded in the run_state.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        state = _manual_state(order_ids=())  # no embedded order IDs
        written = write_run_artifacts(state, base=base)
        run_dir = Path(written["run_state"]).parent
        csv_path = run_dir / "dca_manual_orders.csv"
        csv_path.write_text(
            "ticker,status,order_id,notional_usd\n"
            "AAPL,filled,MAN-CSV-1,3.0\n"
            "MSFT,filled,MAN-CSV-2,3.0\n"
            "GOOG,not_submitted,-,0\n"
        )
        state["artifacts"] = {"orders_csv_path": str(csv_path)}
        Path(written["run_state"]).write_text(json.dumps(state, indent=2))

        manual_ids = collect_manual_order_ids(base)
        assert manual_ids == {"MAN-CSV-1", "MAN-CSV-2"}  # '-' placeholder ignored
        kept = exclude_manual_orders(["SCHED-1", "MAN-CSV-1", "MAN-CSV-2"], base=base)
        assert kept == ["SCHED-1"]


def test_scheduled_write_updates_pointer():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        written = write_run_artifacts(_scheduled_state(), base=base)
        state_path = Path(written["run_state"])
        assert state_path.exists()
        assert "scheduled_runs" in str(state_path)
        pointer = latest_scheduled_run_path(base)
        assert pointer.exists()
        ptr = json.loads(pointer.read_text())
        assert ptr["run_id"] == "scheduled_20260706_1520ET"
        assert ptr["run_state_path"] == str(state_path)


def test_manual_write_never_updates_pointer():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        written = write_run_artifacts(_manual_state(), base=base)
        state_path = Path(written["run_state"])
        assert state_path.exists()
        assert "manual_runs" in str(state_path)
        assert "latest_scheduled_run" not in written
        assert not latest_scheduled_run_path(base).exists()


def test_manual_write_does_not_overwrite_existing_pointer():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_scheduled_state(), base=base)
        before = latest_scheduled_run_path(base).read_text()
        write_run_artifacts(_manual_state(), base=base)
        after = latest_scheduled_run_path(base).read_text()
        assert before == after  # manual run left the scheduled pointer untouched


def test_update_latest_scheduled_run_pointer_uses_given_path():
    # A caller (e.g. near_close_dca's live path) that already wrote run_state.json
    # to its own artifacts_dir can update the pointer to point at that exact path
    # without update_latest_scheduled_run_pointer recomputing/rewriting a second
    # copy anywhere else.
    from run_classification import update_latest_scheduled_run_pointer

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        state = _scheduled_state()
        external_path = base / "external" / "run_state.json"
        external_path.parent.mkdir(parents=True)
        external_path.write_text(json.dumps(state))

        pointer = update_latest_scheduled_run_pointer(state, external_path, base)

        assert pointer == latest_scheduled_run_path(base)
        ptr = json.loads(pointer.read_text())
        assert ptr["run_state_path"] == str(external_path)
        assert ptr["run_id"] == "scheduled_20260706_1520ET"
        # No second copy of run_state.json was created anywhere under base.
        assert list(base.rglob("run_state.json")) == [external_path]


def test_update_latest_scheduled_run_pointer_rejects_incomplete_classification():
    # An incomplete run_classification that says run_origin=scheduled but omits
    # manual_run entirely must not be treated as valid scheduled evidence --
    # manual_run must be the explicit boolean False, not merely "not True".
    from run_classification import update_latest_scheduled_run_pointer

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        state = _scheduled_state()
        state["run_classification"].pop("manual_run", None)
        state_path = base / "run_state.json"
        state_path.write_text(json.dumps(state))

        pointer = update_latest_scheduled_run_pointer(state, state_path, base)

        ptr = json.loads(pointer.read_text())
        assert ptr["valid_scheduled_evidence"] is False
        assert ptr["pointer_status"] == "pending"


def test_write_rejects_invalid_classification():
    bad = _scheduled_state()
    # Break both lineage signals so neither cron_id nor supersedes_cron_id holds.
    bad["run_classification"]["cron_id"] = "deadbeef"
    bad["run_classification"].pop("supersedes_cron_id", None)
    with tempfile.TemporaryDirectory() as d:
        try:
            write_run_artifacts(bad, base=Path(d))
        except ValueError as exc:
            assert "invalid run_classification" in str(exc)
        else:
            raise AssertionError("expected ValueError for invalid classification")


def test_origin_helpers_defensive_or():
    manual = _manual_state()
    assert is_manual_state(manual)
    assert not is_scheduled_state(manual)

    sched = _scheduled_state()
    assert is_scheduled_state(sched)
    assert not is_manual_state(sched)

    # A state marked scheduled but flagged manual is NOT scheduled (flag wins).
    hybrid = _scheduled_state()
    hybrid["run_classification"]["manual_run"] = True
    assert is_manual_state(hybrid)
    assert not is_scheduled_state(hybrid)


def test_collect_and_exclude_manual_order_ids():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_manual_state(order_ids=("MAN-9", "MAN-10")), base=base)
        manual_ids = collect_manual_order_ids(base)
        assert manual_ids == {"MAN-9", "MAN-10"}
        kept = exclude_manual_orders(["SCHED-1", "MAN-9", "SCHED-2", "MAN-10"], base=base)
        assert kept == ["SCHED-1", "SCHED-2"]


def test_select_scheduled_excludes_manual_ids():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # A manual run that shares an order id must be excluded from scheduled metrics.
        write_run_artifacts(_manual_state(order_ids=("SCHED-2",)), base=base)
        write_run_artifacts(_scheduled_state(order_ids=("SCHED-1", "SCHED-2")), base=base)
        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel.ok
        assert sel.order_ids == ["SCHED-1"]  # SCHED-2 excluded (also a manual id)
        assert "SCHED-2" in sel.excluded_manual_order_ids
        assert not sel.used_broker_fallback


def test_select_requires_valid_scheduled_state():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # Only a manual run exists — no scheduled inference allowed.
        write_run_artifacts(_manual_state(), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[{"order_id": "BRK-1", "submitted_at_et": "15:20"}],
            scheduled_state_write_failed=True,
        )
        assert not sel.ok
        assert sel.state is None
        assert sel.order_ids == []
        assert not sel.used_broker_fallback  # no fallback without a scheduled state


def test_broker_fallback_only_with_scheduled_state_and_excludes_manual():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # Scheduled state exists but has no order IDs (simulated write failure).
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        write_run_artifacts(_manual_state(order_ids=("MAN-9",)), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "BRK-1", "submitted_at_et": "15:20"},
                {"order_id": "MAN-9", "submitted_at_et": "15:21"},
                {"order_id": "BRK-2", "submitted_at_et": "15:34"},
            ],
            scheduled_state_write_failed=True,
        )
        assert sel.ok
        assert sel.used_broker_fallback
        assert sel.order_ids == ["BRK-1", "BRK-2"]  # MAN-9 still excluded


def test_broker_fallback_excludes_out_of_window_orders():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "EARLY", "submitted_at_et": "15:10"},   # before 15:15
                {"order_id": "IN-1", "submitted_at_et": "15:15"},    # inclusive start
                {"order_id": "IN-2", "submitted_at_et": "2026-07-06T15:35:00-04:00"},  # inclusive end, ISO
                {"order_id": "LATE", "submitted_at_et": "15:50"},    # after 15:35
                {"order_id": "NOTIME"},                              # no timestamp -> dropped
            ],
            scheduled_state_write_failed=True,
        )
        assert sel.ok
        assert sel.used_broker_fallback
        assert sel.order_ids == ["IN-1", "IN-2"]  # out-of-window and untimed dropped


def test_broker_fallback_converts_utc_timestamps_to_et():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "UTC-IN", "submitted_at_et": "2026-07-06T19:20:00Z"},   # 15:20 EDT -> in
                {"order_id": "UTC-OUT", "submitted_at_et": "2026-07-06T19:50:00Z"},  # 15:50 EDT -> out
            ],
            scheduled_state_write_failed=True,
        )
        assert sel.ok and sel.used_broker_fallback
        assert sel.order_ids == ["UTC-IN"]  # UTC correctly converted; out-of-window dropped


def test_no_fallback_when_not_flagged_write_failure():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[{"order_id": "BRK-1", "submitted_at_et": "15:20"}],
            scheduled_state_write_failed=False,
        )
        # Valid scheduled state, but no write-failure flag -> no fallback, no orders.
        assert sel.ok
        assert sel.order_ids == []
        assert not sel.used_broker_fallback


def test_confirmation_blocked_is_valid_scheduled_evidence():
    state = _confirmation_blocked_state()
    # Still a valid scheduled run (the classification block is unchanged).
    assert is_scheduled_state(state)
    assert not is_manual_state(state)
    # order_submit_status accessors surface the confirmation-blocked outcome.
    assert order_submit_status(state) == ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED
    assert is_confirmation_blocked(state)
    # No order was placed: no order_id on any candidate.
    assert all(c["order_id"] is None for c in state["buy_candidates"])


def test_confirmation_blocked_write_updates_pointer():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        written = write_run_artifacts(_confirmation_blocked_state(), base=base)
        state_path = Path(written["run_state"])
        assert state_path.exists()
        assert "scheduled_runs" in str(state_path)
        pointer = latest_scheduled_run_path(base)
        assert pointer.exists()
        ptr = json.loads(pointer.read_text())
        assert ptr["run_id"] == "scheduled_20260706_1520ET"


def test_confirmation_blocked_reports_no_fills_and_no_fallback():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_confirmation_blocked_state(), base=base)
        # Even with in-window broker orders and the write-failure flag, a
        # confirmation-blocked run must NOT attribute any orders via fallback.
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[{"order_id": "BRK-1", "submitted_at_et": "15:20"}],
            scheduled_state_write_failed=True,
        )
        assert sel.ok  # run_state stays valid; confirmation-block is not an error
        assert sel.confirmation_blocked
        assert sel.order_submit_status == ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED
        assert sel.order_ids == []  # no scheduled fills / order IDs
        assert not sel.used_broker_fallback


def test_confirmation_blocked_clears_stray_order_ids():
    # Defense in depth against a malformed/stale artifact: even if a candidate
    # somehow still carries an order_id despite the top-level status being
    # confirmation-required (e.g. a partial write before the status field was
    # finalized), the contract of zero scheduled orders for a confirmation-
    # blocked run must hold regardless.
    state = _confirmation_blocked_state()
    state["buy_candidates"][0]["order_id"] = "STRAY-1"
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(state, base=base)
        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel.ok
        assert sel.confirmation_blocked
        assert sel.order_ids == []


# --- Raw connector-input persistence (afternoon escalation fix, 2026-07-10) --


def test_raw_input_filenames_are_deterministic():
    assert quotes_batch_filename(1) == "quotes_batch1.json"
    assert quotes_batch_filename(12) == "quotes_batch12.json"
    assert historicals_batch_filename(2) == "historicals_batch2.json"
    assert place_filename("aapl") == "place_AAPL.json"
    assert place_filename("brk.b") == "place_BRK.B.json"
    # Path separators are stripped so the symbol cannot escape the raw/ dir.
    fname = place_filename("../ev/il")
    assert "/" not in fname and fname.startswith("place_") and fname.endswith(".json")


def test_raw_input_dir_under_scheduled_run():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        assert raw_input_dir(TRADING_DAY, base).name == "raw"
        assert raw_input_dir(TRADING_DAY, base).parent.name == "20260706_1520ET"


def test_save_raw_input_persists_before_compute():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        assert list_raw_inputs_saved(TRADING_DAY, base) == []
        p1 = save_raw_input(TRADING_DAY, quotes_batch_filename(1), {"AAPL": 190.0}, base=base)
        save_raw_input(TRADING_DAY, historicals_batch_filename(1), {"AAPL": [1, 2, 3]}, base=base)
        assert p1.exists()
        # Compute reads the persisted file, not an in-memory response.
        assert json.loads(p1.read_text())["AAPL"] == 190.0
        assert list_raw_inputs_saved(TRADING_DAY, base) == [
            "historicals_batch1.json",
            "quotes_batch1.json",
        ]


def test_save_raw_input_rejects_non_bare_filename():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        try:
            save_raw_input(TRADING_DAY, "sub/dir/quotes.json", {}, base=base)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for non-bare filename")


def test_normalize_connector_payload_resolves_output_path():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        pointer_file = base / "big_quotes.json"
        pointer_file.write_text(json.dumps({"AAPL": 190.0, "MSFT": 400.0}))
        payload = {"output_path": str(pointer_file), "status": "ok"}
        normalized = normalize_connector_payload(payload)
        assert normalized["normalized"] is True
        assert normalized["data"]["MSFT"] == 400.0
        # Original pointer is preserved.
        assert normalized["output_path"] == str(pointer_file)


def test_normalize_connector_payload_unreadable_pointer_raises():
    payload = {"output_path": "/no/such/file.json"}
    try:
        normalize_connector_payload(payload)
    except RuntimeError as exc:
        assert "/no/such/file.json" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for unreadable output_path")


def test_save_raw_input_fails_closed_on_unreadable_pointer():
    # Codex review #72: "fail closed when raw pointer normalization fails."
    # normalize_connector_payload() itself still just flags an unreadable
    # pointer (test above) since it's a general-purpose helper, but
    # save_raw_input() is the persistence entry point the "compute reads only
    # persisted files" contract depends on -- it must not silently write an
    # unresolved/broken output_path pointer as if persistence succeeded.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        try:
            save_raw_input(
                TRADING_DAY, historicals_batch_filename(1),
                {"output_path": "/no/such/file.json"}, base=base,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for an unreadable output_path pointer")
        # Nothing should be written for the failed persist.
        assert list_raw_inputs_saved(TRADING_DAY, base) == []


def test_save_raw_input_normalizes_pointer_payload():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        pointer_file = base / "hist.json"
        pointer_file.write_text(json.dumps({"AAPL": [1, 2, 3]}))
        path = save_raw_input(
            TRADING_DAY, historicals_batch_filename(1),
            {"output_path": str(pointer_file)}, base=base,
        )
        saved = json.loads(path.read_text())
        assert saved["normalized"] is True
        assert saved["data"]["AAPL"] == [1, 2, 3]


def test_build_raw_persistence_state_reflects_disk():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        save_raw_input(TRADING_DAY, quotes_batch_filename(1), {"AAPL": 1}, base=base)
        save_raw_input(TRADING_DAY, place_filename("AAPL"), {"id": "x"}, base=base)
        block = build_raw_persistence_state(TRADING_DAY, base=base, recovery_from_escalation=True)
        assert block["raw_input_dir"] == str(raw_input_dir(TRADING_DAY, base))
        assert block["raw_inputs_saved"] == ["place_AAPL.json", "quotes_batch1.json"]
        assert block["recovery_from_escalation"] is True


def test_raw_persistence_failure_still_writes_valid_scheduled_state():
    # Contract: a persistence/normalization failure must still yield a valid
    # scheduled run_state (no orders), not an escalation with no state at all.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        state = raw_persistence_failure_run_state(
            TRADING_DAY, "connector output_path unreadable", base=base,
            triggered_at_utc="2026-07-06T19:20:01Z",
        )
        assert order_submit_status(state) == ORDER_SUBMIT_STATUS_ERROR
        assert state["buy_candidates"] == []
        assert state["raw_inputs_saved"] == []
        assert state["raw_persistence_error"]
        assert is_scheduled_state(state)
        assert not is_manual_state(state)
        # It writes to the scheduled path and updates the pointer like any scheduled run.
        written = write_run_artifacts(state, base=base)
        assert "latest_scheduled_run" in written
        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel.ok
        assert sel.order_ids == []


# --- Automatic failure recovery (2026-07-15 failure_recovery_policy) ---------


def test_build_recovery_state_shape():
    block = build_recovery_state(
        "transient_connector_throttling",
        persisted_raw_first=True,
        recomputed_from_persisted=True,
    )
    assert block["recovery_rerun"] is True
    recovery = block["recovery"]
    assert recovery["trigger_reason"] == "transient_connector_throttling"
    assert recovery["attempt"] == 1
    assert recovery["persisted_raw_first"] is True
    assert recovery["recomputed_from_persisted"] is True
    assert recovery["escalated"] is False
    assert recovery["blocking_reason"] is None


def test_build_recovery_state_rejects_unknown_trigger():
    try:
        build_recovery_state("something_else", persisted_raw_first=True, recomputed_from_persisted=True)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unlisted trigger_reason")


def test_build_recovery_state_rejects_second_attempt():
    try:
        build_recovery_state(
            "raw_file_write_failure",
            persisted_raw_first=True,
            recomputed_from_persisted=True,
            attempt=2,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError: only one recovery attempt is permitted")


def test_recovery_escalation_run_state_shape():
    state = recovery_escalation_run_state(
        TRADING_DAY, "internal_persistence_failure", "recovery attempt also failed to persist run_state",
        triggered_at_utc="2026-07-15T19:20:01Z",
    )
    assert order_submit_status(state) == ORDER_SUBMIT_STATUS_ERROR
    assert state["buy_candidates"] == []
    assert state["recovery_rerun"] is True
    assert state["recovery"]["escalated"] is True
    assert state["recovery"]["blocking_reason"] == "recovery attempt also failed to persist run_state"
    assert is_scheduled_state(state)
    assert not is_manual_state(state)
    # It still writes to the scheduled path and updates the pointer like any scheduled run.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        written = write_run_artifacts(state, base=base)
        assert "latest_scheduled_run" in written
        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel.ok
        assert sel.order_ids == []


def test_recovery_triggers_match_config_and_schema():
    # Keep the runtime constant synchronized with the declarative policy/schema
    # (config/scheduled_tasks.json failure_recovery_policy.recovery_triggers and
    # schemas/run_state.schema.json recovery.trigger_reason.enum) so they can
    # never silently drift apart.
    repo_root = Path(__file__).resolve().parents[2]
    config = json.loads((repo_root / "config" / "scheduled_tasks.json").read_text(encoding="utf-8"))
    assert tuple(config["failure_recovery_policy"]["recovery_triggers"]) == RECOVERY_TRIGGER_REASONS
    schema = json.loads((repo_root / "schemas" / "run_state.schema.json").read_text(encoding="utf-8"))
    assert tuple(schema["properties"]["recovery"]["properties"]["trigger_reason"]["enum"]) == RECOVERY_TRIGGER_REASONS


def test_recovery_state_validates_against_json_schema():
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return

    repo_root = Path(__file__).resolve().parents[2]
    schemas_dir = repo_root / "schemas"
    main = json.loads((schemas_dir / "run_state.schema.json").read_text(encoding="utf-8"))
    resources = [
        (name, Resource.from_contents(json.loads((schemas_dir / name).read_text(encoding="utf-8"))))
        for name in ("run_state.schema.json", "run_classification.schema.json")
    ]
    registry = Registry().with_resources(resources)
    validator = jsonschema.Draft7Validator(main, registry=registry)

    state = _scheduled_state()
    state.update(build_recovery_state(
        "parsing_failure", persisted_raw_first=True, recomputed_from_persisted=True,
    ))
    validator.validate(state)

    escalated = recovery_escalation_run_state(
        TRADING_DAY, "raw_file_write_failure", "disk full while writing run_state.json",
    )
    validator.validate(escalated)

    # recovery_rerun=true with no recovery block must fail the schema's if/then.
    missing_recovery = _scheduled_state()
    missing_recovery["recovery_rerun"] = True
    try:
        validator.validate(missing_recovery)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("expected ValidationError: recovery is required when recovery_rerun=true")


# --- Final manual buy override (2026-07-13 final-buy behavior) ---------------


def test_final_manual_override_classification_is_manual():
    rc = build_final_manual_override_classification("2026-07-13T19:36:00Z")
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert rc["run_type"] == FINAL_MANUAL_OVERRIDE_RUN_TYPE
    assert rc["valid_afternoon_scheduled_evidence"] is False
    assert rc["same_day_top_up_context_only"] is True
    assert rc["cron_id"] == "10ce362c"
    assert rc["supersedes_cron_id"] == "ce16ba6b"
    # A manual classification block must still validate and never look scheduled.
    assert validate_classification(rc).ok
    assert not is_scheduled_state({"run_classification": rc})
    assert is_manual_state({"run_classification": rc})


def test_final_manual_override_custom_run_id_not_scheduled():
    rc = build_final_manual_override_classification(
        "2026-07-13T19:36:00Z", run_id="final_manual_override_20260713_1536ET"
    )
    assert rc["run_id"] == "final_manual_override_20260713_1536ET"
    # Must never collide with the scheduled_YYYYMMDD_1520ET pattern.
    assert validate_classification(rc).ok


def test_final_manual_override_classification_is_admitted_as_top_up_context():
    # Codex review r3626704356 ("Admit final-manual overrides as top-up
    # context"): is_top_up_context_state requires valid_scheduled_evidence is
    # False, but the builder previously only set
    # valid_afternoon_scheduled_evidence=False and left valid_scheduled_evidence
    # unset. A real final-manual-override artifact built by this function was
    # then rejected before its CSV/buy_candidates fills were ever read by the
    # same-day top-up guard.
    rc = build_final_manual_override_classification(
        "2026-07-13T19:36:00Z", run_id="final_manual_override_20260713_1536ET"
    )
    assert rc["valid_scheduled_evidence"] is False
    state = {"trading_day": "2026-07-13", "run_classification": rc}
    assert is_top_up_context_state(state, "2026-07-13")


def test_manual_exclusion_roots_cover_final_override_paths():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        roots = [str(r) for r in manual_exclusion_roots(base)]
        assert str(base / "manual_runs") in roots
        assert str(Path(d) / "final_manual_review") in roots
        assert str(Path(d) / "midday_dca" / "manual_reruns") in roots


def test_manual_exclusion_roots_cover_manual_recovery_paths():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        roots = [str(r) for r in manual_exclusion_roots(base)]
        assert str(Path(d) / "morning_dca" / "manual_recovery") in roots
        assert str(Path(d) / "midday_dca" / "manual_recovery") in roots


def test_manual_exclusion_roots_cover_midday_manual_runs():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        roots = [str(r) for r in manual_exclusion_roots(base)]
        assert str(Path(d) / "midday_dca" / "manual_runs") in roots


def _write_final_override_state(run_dir: Path, order_ids, ts="2026-07-13T19:36:00Z"):
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": "final_manual_override_20260713_1536ET",
        "generated_at": "2026-07-13T15:36:00",
        "trading_day": "2026-07-13",
        "broker": "robinhood",
        "budget": {"budget_tier": "normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
        "counts": {"submitted": len(order_ids)},
        "run_classification": build_final_manual_override_classification(ts),
        "buy_candidates": [
            {"ticker": f"F{i}", "planned_amount_usd": 3.0, "status": "submitted", "order_id": oid}
            for i, oid in enumerate(order_ids)
        ],
    }
    (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))


def test_collect_excludes_final_override_under_ce16ba6b_manual_runs():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        run_dir = base / "manual_runs" / "final_manual_override_20260713_1536ET"
        _write_final_override_state(run_dir, ("FMO-1", "FMO-2"))
        assert collect_manual_order_ids(base) == {"FMO-1", "FMO-2"}


def test_collect_excludes_midday_manual_reruns_final_override():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = Path(d) / "midday_dca" / "manual_reruns" / "final_manual_override_20260713_1536ET"
        _write_final_override_state(run_dir, ("MID-FMO-1",))
        # A filled_orders.csv beside the state (run_and_report style) is scanned too.
        (run_dir / "filled_orders.csv").write_text(
            "ticker,status,order_id,notional_usd\nDFTX,filled,MID-FMO-CSV,3.0\n"
        )
        ids = collect_manual_order_ids(base)
        assert ids == {"MID-FMO-1", "MID-FMO-CSV"}


def test_collect_excludes_final_manual_review_tree():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = Path(d) / "final_manual_review" / "20260713_1536ET"
        _write_final_override_state(run_dir, ("REVIEW-FMO-1",))
        assert collect_manual_order_ids(base) == {"REVIEW-FMO-1"}


def test_final_override_ids_excluded_from_scheduled_selection():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        # Scheduled afternoon run that (defensively) shares an id with a final override.
        write_run_artifacts(_scheduled_state(order_ids=("SCHED-1", "MID-FMO-1")), base=base)
        run_dir = Path(d) / "midday_dca" / "manual_reruns" / "final_manual_override_20260713_1536ET"
        _write_final_override_state(run_dir, ("MID-FMO-1",))
        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel.ok
        assert sel.order_ids == ["SCHED-1"]  # MID-FMO-1 excluded as a manual override id
        assert "MID-FMO-1" in sel.excluded_manual_order_ids


# --- Same-day prior-allocation / top-up context (Jul 21 2026 3:20 fix) --------

JUL21 = "2026-07-21"

# The five manual midday fills observed under
# midday_dca/manual_runs/20260721_1345ET on 2026-07-21.
_JUL21_MIDDAY_FILLS = [
    ("MSFT", 2.0, "6a5fb151-043b-4063-b317-40b666bb4bb7", 399.1364),
    ("GOOGL", 2.0, "6a5fb151-de49-4f8d-bd0e-2e98aeece284", 349.1689),
    ("GOOG", 2.0, "6a5fb151-330b-4d78-9fcc-3da0151ab289", 348.4855),
    ("MA", 1.0, "6a5fb151-653c-4dc9-ae1e-11515afcae27", 540.5445),
    ("IBM", 2.0, "6a5fb151-cdee-41e9-889e-8ff65762d394", 211.79),
]


def _write_jul21_manual_midday(base: Path) -> Path:
    """Write the Jul 21 manual midday run (state + submitted_orders.csv).

    Mirrors the real artifact: fills live only in submitted_orders.csv (symbol/
    amount/order_id/average_price columns), the state carries no buy_candidates,
    and run_classification is manual top-up context (run_origin='manual',
    manual_run=true, same_day_top_up_context_only=true, valid_scheduled_evidence=
    false, valid_afternoon_scheduled_evidence=false, stream='midday_manual')."""
    run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1345ET"
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": "manual_midday_20260721_1345ET",
        "generated_at": "2026-07-21T13:50:00",
        "trading_day": JUL21,
        "broker": "robinhood",
        "manual_run": True,
        "run_origin": "manual",
        "order_submit_status": "submitted",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "run_id": "manual_midday_20260721_1345ET",
            "stream": "midday_manual",
            "trading_day": JUL21,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "same_day_top_up_context_only": True,
            "valid_scheduled_evidence": False,
            "valid_afternoon_scheduled_evidence": False,
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
    header = "symbol,amount,order_id,status,average_price,quantity\n"
    rows = "".join(
        f"{t},{amt},{oid},filled,{price},0.0\n"
        for (t, amt, oid, price) in _JUL21_MIDDAY_FILLS
    )
    (run_dir / "submitted_orders.csv").write_text(header + rows)
    return run_dir


def test_top_up_context_loads_manual_midday_run():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_jul21_manual_midday(base)

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert set(ctx) == {"MSFT", "GOOGL", "GOOG", "MA", "IBM"}
        for ticker, amount, oid, price in _JUL21_MIDDAY_FILLS:
            alloc = ctx[ticker]
            assert alloc.prior_usd == amount  # prior_usd populated, not blank
            assert alloc.reference_price == price
            assert alloc.order_ids == [oid]
            assert alloc.sources == ["manual_midday_20260721_1345ET"]


def test_top_up_context_manual_ids_excluded_from_scheduled_selection():
    # A 3:20/final scheduled run for the same day must still keep the manual
    # midday order IDs out of scheduled evidence / submitted_orders even while
    # the top-up loader reads those same fills as prior-allocation context.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_jul21_manual_midday(base)
        manual_oids = [oid for (_, _, oid, _) in _JUL21_MIDDAY_FILLS]

        scheduled = _scheduled_state(order_ids=("SCHED-A",))
        scheduled["trading_day"] = JUL21
        scheduled["run_classification"] = build_scheduled_classification(
            JUL21, "2026-07-21T19:20:01Z"
        )
        write_run_artifacts(scheduled, base=base)

        sel = select_scheduled_state_for_reporting(JUL21, base=base)
        assert sel.ok
        assert sel.order_ids == ["SCHED-A"]
        for oid in manual_oids:
            assert oid not in sel.order_ids
            assert oid in sel.excluded_manual_order_ids

        # And the loader surfaced them as prior-allocation context regardless.
        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert set(ctx) == {"MSFT", "GOOGL", "GOOG", "MA", "IBM"}


def test_top_up_context_rejects_prior_day_and_scheduled_states():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_jul21_manual_midday(base)
        # Prior-day manual run under the same root must not be folded into today.
        assert load_same_day_top_up_context("2026-07-22", base=base) == {}

        # A run that is (or claims) scheduled evidence is never top-up context.
        good = json.loads(
            (base.parent / "midday_dca" / "manual_runs" / "20260721_1345ET"
             / "run_state.json").read_text()
        )
        scheduled_like = json.loads(json.dumps(good))
        scheduled_like["run_classification"]["valid_scheduled_evidence"] = True
        assert not is_top_up_context_state(scheduled_like, JUL21)
        afternoon_like = json.loads(json.dumps(good))
        afternoon_like["run_classification"]["valid_afternoon_scheduled_evidence"] = True
        assert not is_top_up_context_state(afternoon_like, JUL21)
        assert is_top_up_context_state(good, JUL21)


def _top_up_classification(run_id, **overrides):
    rc = {
        "run_origin": "manual",
        "manual_run": True,
        "run_id": run_id,
        "trading_day": JUL21,
        "same_day_top_up_context_only": True,
        "valid_scheduled_evidence": False,
        "valid_afternoon_scheduled_evidence": False,
    }
    rc.update(overrides)
    return rc


def test_top_up_context_skips_run_already_covered_by_buy_candidates():
    # Codex review r3626083286 ("Deduplicate manual context already present in
    # prior allocations"): near_close_dca.load_prior_same_day_allocations[_multi]
    # already reads a run's fills from embedded buy_candidates. If this loader
    # ALSO folded in the same run's sibling CSV, a real recovery/rerun would be
    # double-counted between the two loaders. Such a run must be skipped here
    # entirely, regardless of what its sibling CSV says.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
            "buy_candidates": [
                {
                    "ticker": "MSFT", "status": "filled", "order_id": "DUP-MSFT-1",
                    "planned_amount_usd": 2.0, "target_price": 399.0,
                },
            ],
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,average_price,quantity\n"
            "MSFT,2.0,DUP-MSFT-1,filled,399.0,0.005\n"
        )

        assert load_same_day_top_up_context(JUL21, base=base) == {}


def test_top_up_context_skips_run_already_covered_by_recorded_orders_csv_path():
    # Same double-count risk as above, via the OTHER mechanism
    # load_prior_same_day_allocations uses when buy_candidates is absent: a
    # recorded artifacts.orders_csv_path.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
            "artifacts": {"orders_csv_path": "submitted_orders.csv"},
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,average_price,quantity\n"
            "MSFT,2.0,DUP-MSFT-1,filled,399.0,0.005\n"
        )

        assert load_same_day_top_up_context(JUL21, base=base) == {}


def test_top_up_context_unconfirmed_row_poisons_reference_price():
    # Codex review r3626083291 ("Preserve an unknown price for submitted manual
    # orders"): a still-outstanding submitted row may yet be rejected, canceled,
    # or repriced, so its logged price must not be trusted as a confirmed
    # reference -- even when another row for the same ticker WAS filled.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,average_price,quantity\n"
            "MSFT,2.0,MSFT-FILLED,filled,399.0,0.005\n"
            "MSFT,1.0,MSFT-SUBMITTED,submitted,350.0,0.0\n"
            "GOOGL,2.0,GOOGL-SUBMITTED,submitted,349.0,0.0\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        # MSFT has one filled + one still-outstanding submitted row: prior_usd
        # sums both (conservative), but the reference price is poisoned unknown.
        assert ctx["MSFT"].prior_usd == 3.0
        assert ctx["MSFT"].reference_price is None
        # GOOGL is submitted-only: amount counts, price stays unknown.
        assert ctx["GOOGL"].prior_usd == 2.0
        assert ctx["GOOGL"].reference_price is None


def test_top_up_context_dedupes_same_order_across_sibling_csvs():
    # Codex review r3626294835 ("Deduplicate orders repeated in sibling CSVs"):
    # a CSV-only manual run directory can contain BOTH submitted_orders.csv and
    # filled_orders.csv, and the same order_id can appear in both (submitted
    # when placed, filled once confirmed). _referenced_orders_csvs returns both
    # files; without row-level dedup by order_id the same $2 fill would be
    # counted twice.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,average_price,quantity\n"
            "MSFT,2.0,MID-MSFT-1,submitted,399.0,0.0\n"
        )
        (run_dir / "filled_orders.csv").write_text(
            "ticker,order_id,status,avg_fill_price,quantity_filled,amount_usd\n"
            "MSFT,MID-MSFT-1,filled,399.0,0.005,2.0\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        # Counted once (from the authoritative filled row), not twice ($4).
        assert ctx["MSFT"].prior_usd == 2.0
        assert ctx["MSFT"].reference_price == 399.0
        assert ctx["MSFT"].order_ids == ["MID-MSFT-1"]


def test_top_up_context_statusless_filled_orders_csv_is_confirmed():
    # Codex review r3626402840 ("Treat statusless filled-orders rows as
    # confirmed fills"): a filled_orders.csv row with no status column (the
    # documented manual-fill shape near_close_dca._orders_csv_fills already
    # treats as confirmed) must still have its price trusted as a reference.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "filled_orders.csv").write_text(
            "ticker,amount_usd,avg_fill_price\nMSFT,2.0,399.0\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 2.0
        assert ctx["MSFT"].reference_price == 399.0


def test_top_up_context_falls_back_when_recorded_csv_path_is_stale():
    # Codex review r3626402842 ("Fall back when a recorded orders CSV path is
    # stale"): artifacts.orders_csv_path pointing at a nonexistent path (e.g.
    # an absolute path from a relocated environment) is NOT the same as the
    # core loader actually finding a fill there -- this loader must still pick
    # up the real sibling CSV rather than treating the run as already covered.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "artifacts": {"orders_csv_path": "/some/stale/nonexistent/path/orders.csv"},
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,avg_fill_price,quantity\n"
            "MSFT,2.0,ORD-1,filled,399.0,0.005\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 2.0
        assert ctx["MSFT"].reference_price == 399.0


def test_top_up_context_recorded_csv_path_that_exists_is_covered():
    # Companion to the stale-path test above: when the recorded path DOES
    # resolve to a real file, the core loader genuinely covers this run, so
    # this loader must still skip it (no double-count).
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        (run_dir / "orders.csv").write_text(
            "symbol,amount,order_id,status,avg_fill_price,quantity\n"
            "MSFT,2.0,ORD-1,filled,399.0,0.005\n"
        )
        state = {
            "trading_day": JUL21,
            "artifacts": {"orders_csv_path": "orders.csv"},
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))

        assert load_same_day_top_up_context(JUL21, base=base) == {}


def test_top_up_context_derives_amount_from_price_and_quantity():
    # Codex review r3626402843 ("Derive a prior notional from confirmed fill
    # quantity"): a confirmed row with avg_fill_price + quantity_filled but no
    # amount/notional column must still contribute a nonzero prior_usd, or the
    # ticker is silently discarded by merge_manual_top_up_context (prior_usd
    # <= 0) and the guard sees no_prior_run instead of the real prior fill.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "ticker,order_id,status,avg_fill_price,quantity_filled\n"
            "MSFT,ORD-1,filled,399.0,0.005\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 399.0 * 0.005
        assert ctx["MSFT"].reference_price == 399.0


def test_top_up_context_scans_afternoon_manual_runs_root():
    # Codex review r3626402847 ("Scan the afternoon manual-runs root for
    # CSV-only context"): a pre-3:20 CSV-only run under the canonical
    # ce16ba6b/manual_runs/ tree (the afternoon task's own manual/final-manual-
    # override root, NOT midday_dca) must be same-day top-up context too.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        run_dir = base / "manual_runs" / "20260721_1500ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_afternoon_20260721_1500ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,avg_fill_price,quantity\n"
            "MSFT,2.0,ORD-1,filled,399.0,0.005\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 2.0
        assert ctx["MSFT"].reference_price == 399.0


def test_top_up_context_scans_final_manual_review_root():
    # Codex review r3626569741 ("Include final_manual_review in the top-up
    # context roots"): config/scheduled_tasks.json's same_day_top_up_policy.
    # manual_override_handling explicitly documents final_manual_review/** as
    # same-day top-up context, alongside midday_dca/manual_reruns and
    # ce16ba6b/manual_runs/final_manual_override_*.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "final_manual_review" / "20260721_1500ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_final_review_20260721_1500ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,avg_fill_price,quantity\n"
            "MSFT,2.0,ORD-1,filled,399.0,0.005\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 2.0
        assert ctx["MSFT"].reference_price == 399.0


def test_top_up_context_reads_buy_candidates_under_final_manual_review():
    # Codex review r3626704360 ("Do not skip final-review artifacts outside the
    # core scan roots"): _already_covered_by_prior_allocation_loader treats any
    # run with buy_candidates as already read by
    # near_close_dca.load_prior_same_day_allocations[_multi]. But that loader is
    # never actually called against final_manual_review/** by run_dca.py or
    # morning_dca.py (see MANUAL_EXCLUSION_SUBROOTS's own note that this root
    # sits "OUTSIDE the ce16ba6b task root"), so a real final-manual-override
    # artifact there whose fills live ONLY in buy_candidates (no orders CSV at
    # all) was silently dropped by BOTH loaders. It must be read directly here
    # instead.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "final_manual_review" / "final_manual_override_20260721_1536ET"
        run_dir.mkdir(parents=True)
        rc = build_final_manual_override_classification(
            "2026-07-21T19:36:00Z", run_id="final_manual_override_20260721_1536ET"
        )
        state = {
            "trading_day": JUL21,
            "run_classification": rc,
            "buy_candidates": [
                {
                    "ticker": "MSFT", "status": "filled", "order_id": "FMO-1",
                    "planned_amount_usd": 2.0, "target_price": 399.0,
                },
            ],
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 2.0
        assert ctx["MSFT"].reference_price == 399.0
        assert ctx["MSFT"].order_ids == ["FMO-1"]


def test_top_up_context_buy_candidates_still_skipped_outside_final_manual_review():
    # Companion to the test above: a run WITH buy_candidates under a root the
    # core loader DOES reach (e.g. ce16ba6b/manual_runs, scanned by both
    # run_dca.py's cross_stream_roots and morning_dca.py's roots) must still be
    # skipped here, or it would now be double-counted between the two loaders.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        run_dir = base / "manual_runs" / "final_manual_override_20260721_1536ET"
        run_dir.mkdir(parents=True)
        rc = build_final_manual_override_classification(
            "2026-07-21T19:36:00Z", run_id="final_manual_override_20260721_1536ET"
        )
        state = {
            "trading_day": JUL21,
            "run_classification": rc,
            "buy_candidates": [
                {
                    "ticker": "MSFT", "status": "filled", "order_id": "FMO-2",
                    "planned_amount_usd": 2.0, "target_price": 399.0,
                },
            ],
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))

        assert load_same_day_top_up_context(JUL21, base=base) == {}


def test_top_up_context_skips_dry_run_artifacts():
    # Codex review r3626569745 ("Skip dry-run artifacts when loading manual CSV
    # context"): a dry-run artifact simulates submitted/filled rows against no
    # real capital -- near_close_dca.load_prior_same_day_allocations skips it
    # for exactly this reason, and this loader must too.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "dry_run": True,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,avg_fill_price,quantity\n"
            "MSFT,2.0,ORD-1,filled,399.0,0.005\n"
        )

        assert load_same_day_top_up_context(JUL21, base=base) == {}


def test_top_up_context_dedupes_copied_order_across_run_directories():
    # Codex review r3626569748 ("Deduplicate copied orders across manual run
    # directories"): a manual override backfilled/copied into TWO different
    # top-up context roots must not have its notional counted twice --
    # _dedupe_rows_by_order_id only dedupes within one run's sibling CSVs.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        for i, root_name in enumerate(
            ("midday_dca/manual_runs/20260721_1400ET", "midday_dca/manual_reruns/20260721_1401ET")
        ):
            run_dir = base.parent / root_name
            run_dir.mkdir(parents=True)
            state = {
                "trading_day": JUL21,
                "run_classification": _top_up_classification(f"manual_run_{i}"),
            }
            (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
            (run_dir / "submitted_orders.csv").write_text(
                "symbol,amount,order_id,status,avg_fill_price,quantity\n"
                "MSFT,2.0,ORD-SAME-1,filled,399.0,0.005\n"
            )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 2.0  # not 4.0
        assert ctx["MSFT"].order_ids == ["ORD-SAME-1"]


def test_top_up_context_parses_notional_usd_column():
    # Codex review r3626569754 ("Parse the standard notional_usd column"):
    # near_close_dca._CSV_FIELDS and existing manual-order CSV tests use
    # notional_usd as the amount column; without recognizing it, a filled row
    # using that convention has no usable amount and is silently discarded.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "run_classification": _top_up_classification("manual_midday_20260721_1400ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "ticker,status,order_id,notional_usd\nMSFT,filled,ORD-1,2.0\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        assert ctx["MSFT"].prior_usd == 2.0


def test_top_up_context_respects_boundary_et():
    # Codex review r3626083294 ("Bound manual top-up context to the scheduled
    # execution time"): a manual fill generated AT OR AFTER a replayed/backfilled
    # run's own execution time must not be treated as prior context for it.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1600ET"
        run_dir.mkdir(parents=True)
        state = {
            "trading_day": JUL21,
            "generated_at": "2026-07-21T16:00:00-04:00",
            "run_classification": _top_up_classification("manual_midday_20260721_1600ET"),
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))
        (run_dir / "submitted_orders.csv").write_text(
            "symbol,amount,order_id,status,average_price,quantity\n"
            "MSFT,2.0,MSFT-1600,filled,399.0,0.005\n"
        )

        # A 3:20 PM replay must not see a 4:00 PM fill as prior context.
        assert load_same_day_top_up_context(JUL21, base=base, boundary_et=time(15, 20)) == {}
        # A later same-day check (e.g. the real-time run) still sees it.
        ctx = load_same_day_top_up_context(JUL21, base=base, boundary_et=time(16, 30))
        assert ctx["MSFT"].prior_usd == 2.0
        # No boundary at all (default) is also inclusive.
        assert load_same_day_top_up_context(JUL21, base=base)["MSFT"].prior_usd == 2.0


def test_build_scheduled_classification_stamps_afternoon_stream():
    # Codex review ("Accept the valid afternoon dated state"):
    # same_day_pointer.select_same_day_run_state's direct-resolution path fails
    # closed when a target's own run_classification.stream is absent, so the
    # real scheduled-run producer must stamp it or a valid same-day afternoon
    # state is rejected instead of honored.
    rc = build_scheduled_classification(TRADING_DAY)
    assert rc["stream"] == "afternoon"


def test_incremental_top_up_amount_guard():
    # Below prior reference -> buy only the incremental difference.
    assert incremental_top_up_amount(3.0, 2.0, 390.0, 399.1364) == (1.0, "incremental_top_up")
    # No prior allocation -> full desired amount.
    assert incremental_top_up_amount(3.0, 0.0, 390.0, None) == (3.0, "no_prior_run")
    # Price not strictly below prior reference -> $0.
    assert incremental_top_up_amount(3.0, 2.0, 400.0, 399.1364) == (0.0, "price_not_below_prior_run")
    # Prior allocation but no comparable price -> conservative $0, manual review.
    assert incremental_top_up_amount(3.0, 2.0, None, None) == (0.0, "missing_prior_price")
    # Desired already covered by prior -> $0.
    assert incremental_top_up_amount(2.0, 3.0, 390.0, 399.1364) == (0.0, "no_incremental_difference")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
