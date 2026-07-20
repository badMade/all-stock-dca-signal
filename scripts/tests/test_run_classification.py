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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_classification import (  # noqa: E402
    ACCEPTED_STATE_SCHEMA_VERSIONS,
    FINAL_MANUAL_OVERRIDE_RUN_TYPE,
    ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
    ORDER_SUBMIT_STATUS_ERROR,
    STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION_V2,
    build_final_manual_override_classification,
    build_manual_classification,
    build_raw_persistence_state,
    build_scheduled_classification,
    collect_manual_order_ids,
    exclude_manual_orders,
    historicals_batch_filename,
    is_confirmation_blocked,
    is_manual_state,
    is_preauthorized_v2,
    is_scheduled_state,
    latest_scheduled_run_path,
    list_raw_inputs_saved,
    manual_exclusion_roots,
    manual_run_id,
    normalize_connector_payload,
    order_submit_status,
    place_filename,
    quotes_batch_filename,
    raw_input_dir,
    raw_persistence_failure_run_state,
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


def test_manual_exclusion_roots_cover_final_override_paths():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        roots = [str(r) for r in manual_exclusion_roots(base)]
        assert str(base / "manual_runs") in roots
        assert str(Path(d) / "final_manual_review") in roots
        assert str(Path(d) / "midday_dca" / "manual_reruns") in roots


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


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
